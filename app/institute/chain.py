"""Chain graph — entities, typed relations, mentions (ROADMAP Phase 4).

The vault IS the graph: every ``chain_nodes`` row projects to a
``Chain/<entity>.md`` note (managed region, writer rule 4 — human notes
outside the markers survive every regeneration), typed edges render as
Dataview inline fields (``supplier_of:: [[台积电]]``), and Obsidian backlinks
replace a dedicated graph UI.

Two taggers feed the graph:

1. **INSTR backstop** (ships first, no model): ``backstop_tag`` runs ONE
   compound SELECT — case-sensitive ``instr()`` substring match of every node
   name and alias against the artifact text (CJK-safe: instr is exact byte
   match on UTF-8, no case folding) — then records hits in ``chain_mentions``.
   ``UNIQUE(node_id, artifact_kind, artifact_ref)`` makes re-tagging
   idempotent, so the live bus handler and the hourly catch-up tick can both
   run it. (ROADMAP wants "one SQL statement"; the hit-scan is one statement,
   the few hit rows then land via INSERT OR IGNORE so each mention carries a
   snippet and each newly-mentioned node gets a ``chain.node_updated`` event.)
2. **Entity/property extraction** (opencode-style, model call):
   ``extract_graph_facts`` submits ``ENTITY_EXTRACT_PROMPT`` through
   ``executor.submit`` (the one execution path; cheap
   ``settings.default_hand``) and parses ``ENTITY:`` lines into
   ``chain_candidates`` plus optional ``PROPERTY:`` assertions into the
   durable ``chain_property_staging`` table, applied into
   ``chain_properties`` once their entity resolves to a durable node. Every
   DISTINCT source artifact lands one ``chain_candidate_sightings`` row and
   ``mention_count`` aggregates those rows — a crash-replayed artifact never
   double-counts (REVIEW-C2 M5), and the sightings are the full source set
   that promotion backfills into ``chain_mentions`` (REVIEW-C2 M2).
   Candidates are promoted to nodes manually (``promote_candidate`` / the
   API) or automatically once ``mention_count`` reaches the ``admin_state``
   threshold (key ``chain:promote_threshold``, default 3). The echo hand
   echoes the prompt back, so fixture text containing protocol lines exercises
   the whole path in tests.

``tick()`` (hourly, gated=True — it spends model quota) consumes finished
artifacts through a monotonic events.id cursor (``admin_state`` key
``chain:extract_cursor``): each new research/whiteboard/analyst-daily
   completion gets a catch-up backstop pass plus one extraction task, whose
   parsed output is persisted (sightings + property staging) BEFORE that
   event's cursor advances — a paid-for extraction is never lost and never
   re-run once staged. Assembly/extraction failures still advance the cursor
   (best-effort enrichment — the live handler already backstop-tagged the
   artifact; a stuck artifact must not wedge the queue); persistence failures
   halt the batch before the event's cursor instead, bounded by
   TICK_PERSIST_FAILURE_LIMIT before the poison event is dropped with an
   operator card (LOOP-P4). After the batch, a
   light-weight cluster pass (``_auto_cluster`` — the ROADMAP "auto-cluster /
periodic merge of aliases": pending candidates whose normalized name matches
or contains/is contained by an existing node's name/alias fold into that node
as an alias, REVIEW-C2 M1) and auto-promotion sweep pending candidates, then
   pending staged assertions are applied (failures stay pending and retry
   next tick without a model call).
   Overlap safety: APScheduler ``max_instances=1`` plus an in-process lock;
   candidate/property status transitions use the conditional-claim idiom.

Vault paths use the PERSISTED ``chain_nodes.slug`` (assigned at insert,
UNIQUE): ``_slug()`` is not injective, so colliding names get a stable
node-id suffix instead of overwriting each other's note (REVIEW-C2 M3).

Vault writes all flow through ``vault.writer`` (never-clobber ledger).
Historical notes exported BEFORE a node existed are healed on demand by
``reproject_footers()`` (``POST /api/chain/reproject``): footers are
recomputed from the on-disk bodies and rewritten through the writer.
The app lifespan calls ``chain.register()``, the scheduler mounts the hourly
tick, the API router is mounted, and ``vault/exporter.py`` injects
``entity_footer`` / ``## Entities`` projections.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..router import executor
from ..vault import writer as vault_writer
from ..vault.writer import get_writer
from .operator import _fold_line, open_action
from .prompts import work_date

log = logging.getLogger("institute.chain")

SOURCE = "chain"

KINDS = ("company", "product", "technology", "commodity", "person", "org", "other")

# Suggested edge vocabulary (open set — chain_edges.relation has no CHECK):
RELATION_VOCABULARY = ("supplier_of", "customer_of", "competitor_of", "subsidiary_of", "produces")

ARTIFACT_EVENT_TYPES = ("research.completed", "whiteboard.board_completed", "analyst_daily.completed")

CURSOR_KEY = "chain:extract_cursor"
THRESHOLD_KEY = "chain:promote_threshold"
PERSIST_FAILURES_KEY = "chain:extract_persist_failures"
DEFAULT_PROMOTE_THRESHOLD = 3

EXTRACT_TEXT_CAP = 6000       # chars of artifact text fed to the extraction prompt
EXTRACT_TIMEOUT_S = 600
MAX_EXTRACT_ENTITIES = 20
MAX_EXTRACT_PROPERTIES = 20
TICK_EVENT_BATCH = 10         # artifacts per tick (one model call each; hourly)
TICK_PERSIST_FAILURE_LIMIT = 3  # halted-batch retries for ONE event's persistence
                                # failure before the event is dropped with an
                                # operator card (LOOP-P4: a poison event must not
                                # buy an extraction model call every hour forever)
ARTIFACT_READ_CAP = 512 * 1024  # bytes per session-workspace artifact file read
                                # (LOOP-P11c: a runaway report must not flood the
                                # INSTR scan / text assembly)
CLUSTER_COMPARE_BUDGET = 20000  # surface-term comparisons per cluster sweep
                                # (LOOP-P8b / R3 P2: bounds candidates × nodes ×
                                # aliases work per tick regardless of graph size)
CLUSTER_NODE_WINDOW = 500     # nodes fetched per rotation window (IO batching only;
                              # the budget is the work bound)
CLUSTER_ROTATION_KEY = "chain:cluster_rotation"
GRAPH_GENERATION_KEY = "chain:graph_generation"
CLUSTER_ROTATION_VERSION = 1
MAX_SQLITE_ROWID = 2**63 - 1
CANDIDATE_TTL_DAYS = 30       # below-threshold pending candidates age out after this
AUTO_PROMOTE_BATCH = 20       # promotions per tick (each is a transaction + vault fan-out)
MENTIONS_IN_NOTE = 8
FOOTER_MAX_LINKS = 50
MIN_TERM_LEN = 2              # single CJK chars over-match; names/aliases must be >= 2
MIN_CLUSTER_CONTAIN_LEN = 4   # containment-based clustering needs a term this long
                              # ("电池" must not absorb "固态电池"; "宁德时代" may
                              # absorb "宁德时代股份有限公司")

# ---- prompt constants (verbatim-stable once written; CLAUDE.md rule 4) ------
# No line in this template may START with "ENTITY:" or "PROPERTY:" — the echo
# hand echoes the whole prompt back and the parsers read those line prefixes,
# so bare example lines would parse as hits. Examples stay inline after 示例：.

ENTITY_EXTRACT_PROMPT = """\
你是产业链实体抽取器。从下面的研究文本中抽取值得长期跟踪的实体：公司、产品、技术、大宗商品、人物、组织。

【输出格式】每找到一个实体输出一行，行首写 ENTITY: 后接实体规范名，再接「 | 」与类型。类型只能取 company / product / technology / commodity / person / org / other 之一。示例：`ENTITY: 台积电 | company`

若文本明确给出实体属性及其适用期间，可另输出 PROPERTY 行，依次为实体规范名、snake_case 属性键、值、as_of 期间。只记录文本明确陈述的事实；没有明确期间就不输出属性。示例：`PROPERTY: 台积电 | monthly_revenue | 2074.3 亿新台币 | 2026-06`

除 ENTITY / PROPERTY 格式行外不要输出任何其他内容。

【抽取规则】
1. 只抽取有产业链跟踪价值的具体实体；泛称（如「市场」「政策」「行业」）不抽。
2. 实体名用文本中最常见的规范写法，公司优先用通用简称。
3. 每个实体只输出一次，最多输出 20 个；一个没有就输出 NONE。
4. 拿不准类型时用 other。

【文本】
{text}\
"""

_ENTITY_LINE = re.compile(r"^\s*ENTITY:\s*(.+?)\s*\|\s*([A-Za-z_]+)\s*$", re.MULTILINE)
_PROPERTY_LINE = re.compile(
    r"^\s*PROPERTY:\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*$",
    re.MULTILINE,
)

DASHBOARDS_BODY = """\
# Institute Dashboards

需要 Obsidian Dataview 插件。链图实体笔记在 `Chain/`，导出笔记尾部的 `## Entities` wikilink 使这里的反链统计生效。

## 实体总览（按被引用次数）

```dataview
TABLE kind AS 类型, length(file.inlinks) AS 被引用
FROM "Chain"
SORT length(file.inlinks) DESC
```

## 最近更新的实体

```dataview
LIST
FROM "Chain"
SORT file.mtime DESC
LIMIT 20
```

## 供应链关系（supplier_of）

```dataview
TABLE supplier_of AS 供货给
FROM "Chain"
WHERE supplier_of
```

## 竞争关系（competitor_of）

```dataview
TABLE competitor_of AS 竞争对手
FROM "Chain"
WHERE competitor_of
```\
"""


# ---- errors ------------------------------------------------------------------

class ChainError(ValueError):
    """Validation / lookup-shape errors (API maps to 400)."""


class PromoteConflict(RuntimeError):
    """A candidate status transition lost its conditional claim (API: 409)."""


class PropertyConflict(RuntimeError):
    """A property transition/resolution lost its conditional claim (API: 409)."""


class ClusterGenerationChanged(RuntimeError):
    """The chain-node surface changed while an auto-cluster decision was parked."""


# ---- small helpers -------------------------------------------------------------

_PATH_HOSTILE = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')


def _slug(text: str, max_len: int = 80) -> str:
    """Filename-safe slug: keep CJK, replace path-hostile chars with - (same
    contract as vault/exporter._slug; copied so this partition stays decoupled).
    NOT injective ("A/B" and "A:B" both become "A-B") — vault paths therefore
    use the persisted UNIQUE ``chain_nodes.slug`` assigned at insert, never a
    recomputed slug (REVIEW-C2 M3)."""
    s = _PATH_HOSTILE.sub("-", str(text or "").strip())
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"-{2,}", "-", s).strip(" -.")
    return s[:max_len].strip(" -.") or "untitled"


def _norm_term(text: str) -> str:
    """Casefold + strip ALL whitespace — the equality key for light clustering
    ("Tesla Inc" clusters with "tesla  inc"; CJK strings pass through)."""
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _parse_aliases(raw: Any) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(a) for a in data if isinstance(a, str) and a.strip()]


def _node_out(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["aliases"] = _parse_aliases(row.get("aliases"))
    return out


async def _session_workspace(session_id: Any) -> Path | None:
    if not session_id:
        return None
    row = await db.query_one("SELECT workspace_dir FROM sessions WHERE id = ?", (str(session_id),))
    if row and row["workspace_dir"]:
        ws = Path(row["workspace_dir"]).expanduser()
        if ws.is_dir():
            return ws
    return None


def _read_text(path: Path) -> str | None:
    """Clamped artifact read (LOOP-P11c): at most ARTIFACT_READ_CAP bytes, so
    a runaway report file cannot flood the INSTR backstop scan or the
    extraction text assembly. A multi-byte char split at the clamp boundary
    degrades to the U+FFFD replacement char, which no matcher depends on."""
    try:
        if path.is_file():
            with path.open("rb") as fh:
                return fh.read(ARTIFACT_READ_CAP).decode("utf-8", errors="replace")
    except OSError:
        log.warning("could not read %s", path)
    return None


# ---- node / edge CRUD -----------------------------------------------------------

async def _graph_generation(conn: Any | None = None) -> int:
    """Monotonic generation of the searchable chain-node surface.

    Every production INSERT and every name/aliases mutation bumps this row in
    the SAME transaction as the graph write. There is currently no production
    node-delete or rename path; adding one must call ``_bump_graph_generation``
    in its transaction. Rotation evidence is valid only at one generation.
    """
    if conn is None:
        row = await db.query_one(
            "SELECT value FROM admin_state WHERE key = ?", (GRAPH_GENERATION_KEY,),
        )
    else:
        cur = await conn.execute(
            "SELECT value FROM admin_state WHERE key = ?", (GRAPH_GENERATION_KEY,),
        )
        raw = await cur.fetchone()
        await cur.close()
        row = dict(raw) if raw is not None else None
    if row is None:
        return 0
    try:
        value = int(row["value"])
    except (TypeError, ValueError):
        return 0
    return value if 0 <= value <= MAX_SQLITE_ROWID else 0


async def _bump_graph_generation(conn: Any) -> int:
    """Atomically bump graph generation inside a graph mutation transaction."""
    cur = await conn.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = "
        "CASE WHEN CAST(value AS INTEGER) BETWEEN 0 AND ? "
        "THEN CAST(value AS INTEGER) + 1 ELSE 1 END",
        (GRAPH_GENERATION_KEY, MAX_SQLITE_ROWID - 1),
    )
    changed = cur.rowcount
    await cur.close()
    if changed != 1:
        raise RuntimeError("graph generation bump was lost")
    return await _graph_generation(conn)


async def get_node(node_id: str) -> dict[str, Any] | None:
    row = await db.query_one("SELECT * FROM chain_nodes WHERE id = ?", (node_id,))
    return _node_out(row) if row else None


async def _assign_slug(name: str, node_id: str, conn: Any) -> str:
    """Persisted-unique vault slug for a NEW node: plain ``_slug(name)`` when
    free, else suffixed with the node id (stable and unique by construction —
    "A/B" and "A:B" must not overwrite each other's note, REVIEW-C2 M3).
    Runs inside the caller's insert transaction (``conn``), so check + insert
    share the write lock."""
    cur = await conn.execute("SELECT 1 FROM chain_nodes WHERE slug = ?", (_slug(name),))
    row = await cur.fetchone()
    await cur.close()
    if row is None:
        return _slug(name)
    return f"{_slug(name, max_len=80 - len(node_id) - 1)}-{node_id}"


def _validate_kind(kind: str) -> str:
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise ChainError(f"unknown kind '{kind}' (expected one of {', '.join(KINDS)})")
    return kind


async def _validate_security(security_id: str | None) -> str | None:
    if security_id is None or str(security_id).strip() == "":
        return None
    security_id = str(security_id).strip()
    row = await db.query_one("SELECT id FROM securities WHERE id = ?", (security_id,))
    if row is None:
        raise ChainError(f"security '{security_id}' not found")
    return security_id


_TERM_TAKEN_SQL = """\
SELECT id FROM chain_nodes WHERE name = ?1 AND id <> ?2
UNION
SELECT n.id FROM chain_nodes n, json_each(n.aliases) a
WHERE a.type = 'text' AND a.value = ?1 AND n.id <> ?2 LIMIT 1\
"""


async def _term_taken_txn(conn: Any, term: str, exclude_id: str = "") -> bool:
    """In-transaction: does ``term`` already resolve (name or alias) to some
    other node? The check and the dependent write share the write lock, so no
    concurrent alias/create can slip in between (REVIEW-C2 M4 TOCTOU note)."""
    cur = await conn.execute(_TERM_TAKEN_SQL, (term, exclude_id))
    row = await cur.fetchone()
    await cur.close()
    return row is not None


async def create_node(
    name: str, kind: str, *, security_id: str | None = None, aliases: list[str] | None = None,
) -> dict[str, Any]:
    """Insert a chain node. Raises ChainError on validation problems — including
    a name that already resolves to another node as its name OR one of its
    aliases (REVIEW-C2 M4: one term must resolve to exactly one node; the JSON
    alias arrays carry no DB constraint, so this is checked inside the insert
    transaction, which shares the write lock — no TOCTOU window)."""
    name = str(name or "").strip()
    if len(name) < MIN_TERM_LEN:
        raise ChainError(f"node name must be at least {MIN_TERM_LEN} chars")
    kind = _validate_kind(kind)
    security_id = await _validate_security(security_id)
    clean_aliases: list[str] = []
    for a in aliases or []:
        a = str(a or "").strip()
        if len(a) < MIN_TERM_LEN or a == name or a in clean_aliases:
            continue
        clean_aliases.append(a)

    node_id = _new_id()
    now = bus.now_iso()
    async with db.transaction() as conn:
        if await _term_taken_txn(conn, name):
            raise ChainError(f"name '{name}' already resolves to another node")
        for a in clean_aliases:
            if await _term_taken_txn(conn, a):
                raise ChainError(f"alias '{a}' already resolves to another node")
        slug = await _assign_slug(name, node_id, conn)
        cur = await conn.execute(
            "INSERT INTO chain_nodes (id, name, kind, security_id, aliases, slug, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (node_id, name, kind, security_id,
             json.dumps(clean_aliases, ensure_ascii=False), slug, now, now),
        )
        inserted = cur.rowcount
        await cur.close()
        if inserted != 1:
            raise ChainError("chain node insert was lost")
        await _bump_graph_generation(conn)
    await bus.emit("chain.node_updated", "chain_node", node_id, {"reason": "created", "name": name})
    log.info("chain node created: %s (%s, %s)", name, kind, node_id)
    return await get_node(node_id)  # type: ignore[return-value]


async def merge_aliases(node_id: str, alias: str) -> dict[str, Any]:
    """Attach one alias to a node (idempotent). Ambiguous aliases — already
    resolving to a different node — are rejected. Check and write share one
    transaction (write lock), closing the TOCTOU window two concurrent alias
    writes had (REVIEW-C2 M4)."""
    alias = str(alias or "").strip()
    if len(alias) < MIN_TERM_LEN:
        raise ChainError(f"alias must be at least {MIN_TERM_LEN} chars")
    changed = False
    async with db.transaction() as conn:
        cur = await conn.execute("SELECT * FROM chain_nodes WHERE id = ?", (node_id,))
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise LookupError(f"unknown chain node: {node_id}")
        node = _node_out(dict(row))
        if alias != node["name"] and alias not in node["aliases"]:
            if await _term_taken_txn(conn, alias, exclude_id=node_id):
                raise ChainError(f"alias '{alias}' already resolves to another node")
            aliases = node["aliases"] + [alias]
            cur = await conn.execute(
                "UPDATE chain_nodes SET aliases = ?, updated_at = ? "
                "WHERE id = ? AND aliases = ?",
                (
                    json.dumps(aliases, ensure_ascii=False), bus.now_iso(),
                    node_id, row["aliases"],
                ),
            )
            claimed = cur.rowcount
            await cur.close()
            if claimed != 1:
                raise ChainError("chain alias update claim was lost")
            await _bump_graph_generation(conn)
            changed = True
    if changed:
        await bus.emit("chain.node_updated", "chain_node", node_id, {"reason": "alias", "alias": alias})
    return await get_node(node_id)  # type: ignore[return-value]


async def add_edge(
    src_id: str, dst_id: str, relation: str, *,
    confidence: float | None = None, evidence_ref: str | None = None,
) -> dict[str, Any]:
    """Assert a typed directed edge. Idempotent on (src, dst, relation): the
    existing row wins and is returned with created=False."""
    relation = str(relation or "").strip()
    if not relation:
        raise ChainError("relation must be non-empty")
    if src_id == dst_id:
        raise ChainError("self-loop edges carry no chain information")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise ChainError("confidence must be a number in [0, 1]") from exc
        if not (0.0 <= confidence <= 1.0):
            raise ChainError("confidence must be a number in [0, 1]")
    for nid in (src_id, dst_id):
        if await db.query_one("SELECT id FROM chain_nodes WHERE id = ?", (nid,)) is None:
            raise LookupError(f"unknown chain node: {nid}")

    edge_id = _new_id()
    created = await db.execute(
        "INSERT OR IGNORE INTO chain_edges "
        "(id, src_id, dst_id, relation, confidence, evidence_ref, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (edge_id, src_id, dst_id, relation, confidence, evidence_ref, bus.now_iso()),
    )
    if created:
        for nid in (src_id, dst_id):
            await bus.emit("chain.node_updated", "chain_node", nid, {"reason": "edge", "relation": relation})
    row = await db.query_one(
        "SELECT * FROM chain_edges WHERE src_id = ? AND dst_id = ? AND relation = ?",
        (src_id, dst_id, relation),
    )
    return {**(row or {}), "created": bool(created)}


async def list_nodes(
    q: str | None = None, kind: str | None = None, limit: int = 50, offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = [], []
    if q and str(q).strip():
        where.append("(instr(name, ?1) > 0 OR instr(aliases, ?1) > 0)")
        params.append(str(q).strip())
    if kind:
        where.append(f"kind = ?{len(params) + 1}")
        params.append(_validate_kind(kind))
    sql = "SELECT * FROM chain_nodes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY name LIMIT ?{len(params) + 1} OFFSET ?{len(params) + 2}"
    params.extend([min(max(int(limit), 1), 500), max(int(offset), 0)])
    return [_node_out(r) for r in await db.query(sql, params)]


async def node_detail(node_id: str) -> dict[str, Any] | None:
    """Node + outgoing/incoming edges (with peer names) + recent mentions."""
    node = await get_node(node_id)
    if node is None:
        return None
    node["edges_out"] = await db.query(
        "SELECT e.*, n.name AS dst_name, n.slug AS dst_slug FROM chain_edges e "
        "JOIN chain_nodes n ON n.id = e.dst_id WHERE e.src_id = ? ORDER BY e.relation, n.name",
        (node_id,),
    )
    node["edges_in"] = await db.query(
        "SELECT e.*, n.name AS src_name, n.slug AS src_slug FROM chain_edges e "
        "JOIN chain_nodes n ON n.id = e.src_id WHERE e.dst_id = ? ORDER BY e.relation, n.name",
        (node_id,),
    )
    node["mentions"] = await db.query(
        "SELECT * FROM chain_mentions WHERE node_id = ? ORDER BY created_at DESC, id DESC LIMIT 50",
        (node_id,),
    )
    return node


MAX_GRAPH_DEPTH = 3


async def graph(center: str, depth: int = 1) -> dict[str, Any]:
    """Adjacency JSON around one node: BFS over edges (both directions) up to
    ``depth`` hops (clamped to 1..MAX_GRAPH_DEPTH). ``center`` accepts a node
    id or an exact name. Nodes carry their hop distance."""
    center = str(center or "").strip()
    row = await db.query_one("SELECT * FROM chain_nodes WHERE id = ?", (center,))
    if row is None:
        row = await db.query_one("SELECT * FROM chain_nodes WHERE name = ?", (center,))
    if row is None:
        raise LookupError(f"unknown chain node: {center}")
    try:
        depth = int(depth)
    except (TypeError, ValueError):
        depth = 1
    depth = max(1, min(depth, MAX_GRAPH_DEPTH))

    distance: dict[str, int] = {row["id"]: 0}
    frontier = [row["id"]]
    edges: dict[str, dict[str, Any]] = {}
    for hop in range(1, depth + 1):
        if not frontier:
            break
        marks = ",".join("?" for _ in frontier)
        rows = await db.query(
            f"SELECT * FROM chain_edges WHERE src_id IN ({marks}) OR dst_id IN ({marks})",
            (*frontier, *frontier),
        )
        nxt: list[str] = []
        for e in rows:
            edges[e["id"]] = e
            for peer in (e["src_id"], e["dst_id"]):
                if peer not in distance:
                    distance[peer] = hop
                    nxt.append(peer)
        frontier = nxt

    marks = ",".join("?" for _ in distance)
    node_rows = await db.query(f"SELECT * FROM chain_nodes WHERE id IN ({marks})", tuple(distance))
    nodes = []
    for n in node_rows:
        out = _node_out(n)
        out["distance"] = distance[n["id"]]
        nodes.append(out)
    nodes.sort(key=lambda n: (n["distance"], n["name"]))
    return {
        "center": row["id"], "depth": depth,
        "nodes": nodes, "edges": sorted(edges.values(), key=lambda e: e["id"]),
    }


# ---- entity properties: hybrid supersede / conflict -------------------------------

PROPERTY_STATUSES = ("active", "superseded", "conflicted", "retired")


def _property_field(value: Any, field: str, cap: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ChainError(f"property {field} must be non-empty")
    if len(text) > cap:
        raise ChainError(f"property {field} must be at most {cap} chars")
    return text


_YEAR_PERIOD = re.compile(r"^(\d{4})$")
_QUARTER_PERIOD = re.compile(r"^(\d{4})-[Qq](\d{1,2})$")
_MONTH_PERIOD = re.compile(r"^(\d{4})-(\d{1,2})$")
_DAY_PERIOD = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def _period_parts(as_of: str) -> tuple[str, tuple[int, int, int, int]]:
    """Return ``(canonical, chronological sort key)`` for a supported period.

    Periods of different precision are compared by their inclusive end date:
    a year ends on Dec 31, a quarter on its final month's final day, a month
    on its final day, and a full date on that date.  The precision rank is a
    deterministic tie-breaker when two representations share an end date
    (date > month > quarter > year).  This makes cross-family comparisons
    chronological instead of dependent on punctuation/letters in the stored
    spelling (for example, ``2026-07`` follows ``2026-Q2``).

    Unknown or invalid formats fail closed.  Letting an opaque string into a
    property key's live horizon would make every later comparison ambiguous.
    """
    raw = str(as_of or "").strip()
    match = _DAY_PERIOD.fullmatch(raw)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            parsed = datetime(year, month, day)
        except ValueError as exc:
            raise ChainError(f"unsupported property as_of period: {raw!r}") from exc
        return f"{year:04d}-{month:02d}-{day:02d}", (year, month, day, 3)

    match = _QUARTER_PERIOD.fullmatch(raw)
    if match:
        year, quarter = (int(part) for part in match.groups())
        if not 1 <= year <= 9999 or not 1 <= quarter <= 4:
            raise ChainError(f"unsupported property as_of period: {raw!r}")
        end_month = quarter * 3
        end_day = 31 if end_month == 12 else (
            datetime(year, end_month + 1, 1) - timedelta(days=1)
        ).day
        return f"{year:04d}-Q{quarter:02d}", (year, end_month, end_day, 1)

    match = _MONTH_PERIOD.fullmatch(raw)
    if match:
        year, month = (int(part) for part in match.groups())
        if not 1 <= year <= 9999 or not 1 <= month <= 12:
            raise ChainError(f"unsupported property as_of period: {raw!r}")
        end_day = 31 if month == 12 else (
            datetime(year, month + 1, 1) - timedelta(days=1)
        ).day
        return f"{year:04d}-{month:02d}", (year, month, end_day, 2)

    match = _YEAR_PERIOD.fullmatch(raw)
    if match:
        year = int(match.group(1))
        if not 1 <= year <= 9999:
            raise ChainError(f"unsupported property as_of period: {raw!r}")
        return f"{year:04d}", (year, 12, 31, 0)

    raise ChainError(
        "property as_of must be YYYY, YYYY-Qn, YYYY-MM, or YYYY-MM-DD"
    )


def _normalize_period(as_of: str) -> str:
    """Validate and canonicalize a supported property period."""
    return _period_parts(as_of)[0]


def _period_sort_key(as_of: str) -> tuple[int, int, int, int]:
    """Chronological key shared by every supported period format."""
    return _period_parts(as_of)[1]


def _property_inputs(
    prop_key: Any, value: Any, as_of: Any, source_ref: Any,
) -> tuple[str, str, str, str]:
    """Validated (key, value, as_of, source_ref) — ``as_of`` is stored and
    deduplicated in canonical form, then ordered through ``_period_sort_key``."""
    key = _property_field(prop_key, "key", 80)
    key = re.sub(r"\s+", "_", key).casefold()
    return (
        key,
        _property_field(value, "value", 2000),
        _property_field(_normalize_period(str(as_of or "")), "as_of", 64),
        _property_field(source_ref, "source_ref", 300),
    )


def _property_value_fingerprint(value: Any) -> str:
    """Formatting-insensitive equality used by the substantive-conflict gate."""
    return " ".join(str(value or "").split()).casefold()


def _property_action_ref(conflict_group: str) -> str:
    return f"chain-property:{conflict_group}"


PROPERTY_ACTION_PRIORITY = 2


async def _ensure_conflict_action(
    conn: Any, conflict_group: str, entity_id: str, entity_name: str, now: str,
) -> tuple[int, list[str]]:
    """Open/idempotently recover the operator card for one live conflict and
    link it onto the group's conflicted rows — INSIDE the caller's property
    transaction, so a card-creation failure rolls the whole assertion back
    (a committed conflicted row can never lack its action again). Writes
    ``operator_actions`` directly with ``open_action``'s exact field
    semantics (kind vocabulary, live-ref idempotency backed by the 0018
    partial unique index, ``_fold_line`` title, detail cap, priority):
    ``operator.py`` has no transaction-aware helper and ``open_action`` takes
    the process write lock this transaction already holds.

    Returns ``(action_id, conflicted property ids)``.
    """
    cur = await conn.execute(
        "SELECT * FROM chain_properties "
        "WHERE conflict_group = ? AND status = 'conflicted' ORDER BY created_at, rowid",
        (conflict_group,),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    await cur.close()
    if not rows:
        raise PropertyConflict(f"property conflict {conflict_group} has no live rows")
    first = rows[0]
    ref = _property_action_ref(conflict_group)
    cur = await conn.execute(
        "SELECT id FROM operator_actions WHERE ref = ? AND status IN ('open','in_progress')",
        (ref,),
    )
    live = await cur.fetchone()
    await cur.close()
    if live is not None:
        action_id = int(live["id"])
    else:
        detail_lines = "\n".join(
            f"{r['id']}: {r['value']} (source={r['source_ref']})" for r in rows
        )
        detail = (
            f"conflict_group: {conflict_group}\nentity_id: {entity_id}\n"
            f"property: {first['prop_key']}\nas_of: {first['as_of']}\n{detail_lines}"
        )
        cur = await conn.execute(
            "INSERT INTO operator_actions "
            "(kind, ref, title, detail, status, priority, created_at, updated_at) "
            "VALUES (?,?,?,?,'open',?,?,?)",
            (
                "other", ref,
                _fold_line(
                    f"Property conflict: {entity_name}.{first['prop_key']} @ {first['as_of']}",
                    200,
                ),
                detail[:2000], PROPERTY_ACTION_PRIORITY, now, now,
            ),
        )
        action_id = int(cur.lastrowid or 0)
        await cur.close()
    await conn.execute(
        "UPDATE chain_properties SET operator_action_id = ? "
        "WHERE conflict_group = ? AND status = 'conflicted'",
        (action_id, conflict_group),
    )
    return action_id, [r["id"] for r in rows]


async def upsert_property(
    entity_id: str, prop_key: str, value: str, as_of: str, source_ref: str,
) -> dict[str, Any]:
    """Record one sourced entity-property assertion.

    Default path: conditionally move the prior active value to ``superseded``
    and insert the new value as ``active``. Hybrid exception: when a DIFFERENT
    source's current word about the SAME ``as_of`` period — its latest
    non-retired assertion, active OR superseded (a row displaced by a NEWER
    period was never retracted) — carries a materially DIFFERENT value, all
    disputed assertions move to one ``conflicted`` group. A LATE assertion
    (``as_of`` earlier than the greatest structured period key among active /
    conflicted rows for the property) only writes that period's history: it
    lands as ``superseded`` and never displaces the newer current value. Exact
    replays are idempotent. The conflict's operator card commits in the SAME
    transaction as the property rows; the ``chain.property_conflict`` bus
    event is emitted post-commit best-effort (log-only on failure). The
    transaction-wide write lock plus rowcount checks implement the
    repository's conditional-claim idiom; the partial unique index is the
    final one-active-value backstop.
    """
    entity_id = str(entity_id or "").strip()
    if not entity_id:
        raise ChainError("property entity_id must be non-empty")
    node = await get_node(entity_id)
    if node is None:
        raise LookupError(f"unknown chain node: {entity_id}")
    key, value, as_of, source_ref = _property_inputs(prop_key, value, as_of, source_ref)

    now = bus.now_iso()
    prop_id = _new_id()
    created = False
    conflict_created = False
    conflict_group: str | None = None
    action_id: int | None = None
    conflict_property_ids: list[str] = []
    async with db.transaction() as conn:
        cur = await conn.execute(
            "SELECT * FROM chain_properties WHERE entity_id = ? AND prop_key = ? "
            "AND value = ? AND as_of = ? AND source_ref = ?",
            (entity_id, key, value, as_of, source_ref),
        )
        exact = await cur.fetchone()
        await cur.close()
        if exact is not None:
            prop_id = exact["id"]
            if exact["status"] == "conflicted" and exact["conflict_group"]:
                # replay of a live conflict: recover the card if a human
                # disposed of it without resolving the rows
                await _ensure_conflict_action(
                    conn, str(exact["conflict_group"]), entity_id, node["name"], now,
                )
        else:
            cur = await conn.execute(
                "SELECT * FROM chain_properties WHERE entity_id = ? AND prop_key = ? "
                "AND as_of = ? AND status IN ('active','conflicted','superseded') "
                "ORDER BY created_at DESC, rowid DESC",
                (entity_id, key, as_of),
            )
            period_rows = [dict(r) for r in await cur.fetchall()]
            await cur.close()
            # A source's current word about this period is its LATEST
            # non-retired row (rows ordered newest-first; the implicit
            # monotonic rowid — not the random uuid id — breaks same-second
            # created_at ties, so a correction is never mistaken for the
            # retracted value it replaced, REVIEW-R2 P1-2): superseded rows
            # count — a Q1 assertion displaced by a Q2 update was never
            # retracted — but a source's own older corrections and resolution
            # losers (retired) stay out of the comparison.
            comparable: list[dict[str, Any]] = []
            seen_sources: set[str] = set()
            for r in period_rows:
                if r["source_ref"] in seen_sources:
                    continue
                seen_sources.add(r["source_ref"])
                comparable.append(r)
            substantive = [
                r for r in comparable
                if r["source_ref"] != source_ref
                and _property_value_fingerprint(r["value"]) != _property_value_fingerprint(value)
            ]
            if substantive:
                conflict_group = next(
                    (str(r["conflict_group"]) for r in period_rows if r.get("conflict_group")),
                    _new_id(),
                )
                # other sources' current words join the dispute (active or
                # superseded); their non-latest rows stay plain history
                join_ids = [
                    r["id"] for r in comparable
                    if r["status"] in ("active", "superseded") and r["source_ref"] != source_ref
                ]
                if join_ids:
                    marks = ",".join("?" for _ in join_ids)
                    cur = await conn.execute(
                        f"UPDATE chain_properties SET status='conflicted', conflict_group=?, "
                        f"updated_at=? WHERE id IN ({marks}) AND status IN ('active','superseded')",
                        (conflict_group, now, *join_ids),
                    )
                    claimed = cur.rowcount
                    await cur.close()
                    if claimed != len(join_ids):
                        raise PropertyConflict("property conflict claim was lost")
                conflicted_ids = [r["id"] for r in period_rows if r["status"] == "conflicted"]
                if conflicted_ids:
                    marks = ",".join("?" for _ in conflicted_ids)
                    await conn.execute(
                        f"UPDATE chain_properties SET conflict_group=?, updated_at=? "
                        f"WHERE id IN ({marks}) AND status='conflicted'",
                        (conflict_group, now, *conflicted_ids),
                    )
                await conn.execute(
                    "INSERT INTO chain_properties "
                    "(id, entity_id, prop_key, value, as_of, source_ref, status, "
                    "conflict_group, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,'conflicted',?,?,?)",
                    (prop_id, entity_id, key, value, as_of, source_ref,
                     conflict_group, now, now),
                )
                conflict_created = True
                action_id, conflict_property_ids = await _ensure_conflict_action(
                    conn, conflict_group, entity_id, node["name"], now,
                )
            else:
                cur = await conn.execute(
                    "SELECT as_of FROM chain_properties "
                    "WHERE entity_id = ? AND prop_key = ? AND status IN ('active','conflicted')",
                    (entity_id, key),
                )
                horizon_rows = await cur.fetchall()
                await cur.close()
                live_max = max(
                    (_period_sort_key(row["as_of"]) for row in horizon_rows),
                    default=None,
                )
                if live_max is not None and _period_sort_key(as_of) < live_max:
                    # LATE assertion: all supported period families share the
                    # structured chronological key. History only — the newer
                    # current value stands.
                    await conn.execute(
                        "INSERT INTO chain_properties "
                        "(id, entity_id, prop_key, value, as_of, source_ref, status, "
                        "created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,'superseded',?,?)",
                        (prop_id, entity_id, key, value, as_of, source_ref, now, now),
                    )
                else:
                    cur = await conn.execute(
                        "SELECT id FROM chain_properties WHERE entity_id = ? AND prop_key = ? "
                        "AND status = 'active' ORDER BY created_at DESC, rowid DESC",
                        (entity_id, key),
                    )
                    active_rows = await cur.fetchall()
                    await cur.close()
                    supersedes_id = active_rows[0]["id"] if active_rows else None
                    if active_rows:
                        cur = await conn.execute(
                            "UPDATE chain_properties SET status='superseded', updated_at=? "
                            "WHERE entity_id=? AND prop_key=? AND status='active'",
                            (now, entity_id, key),
                        )
                        claimed = cur.rowcount
                        await cur.close()
                        if claimed != len(active_rows):
                            raise PropertyConflict("property supersede claim was lost")
                    await conn.execute(
                        "INSERT INTO chain_properties "
                        "(id, entity_id, prop_key, value, as_of, source_ref, status, "
                        "supersedes_id, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,'active',?,?,?)",
                        (prop_id, entity_id, key, value, as_of, source_ref,
                         supersedes_id, now, now),
                    )
            created = True

    row = await db.query_one("SELECT * FROM chain_properties WHERE id = ?", (prop_id,))
    assert row is not None
    out = dict(row)
    out["created"] = created
    out["conflict"] = out["status"] == "conflicted"
    if conflict_created and conflict_group is not None:
        # rows + card are already durable; the bus mirror is best-effort
        # (roadmap._record_event pattern: audit in transaction, mirror after)
        try:
            await bus.emit(
                "chain.property_conflict", "chain_property", conflict_group,
                {
                    "conflict_group": conflict_group,
                    "entity_id": entity_id,
                    "prop_key": key,
                    "as_of": as_of,
                    "property_ids": conflict_property_ids,
                    "operator_action_id": action_id,
                },
            )
        except Exception:  # noqa: BLE001 - the assertion committed; never unwind it for the mirror
            log.exception("chain.property_conflict emit failed for %s", conflict_group)
    return out


async def get_properties(entity_id: str) -> dict[str, Any]:
    """Return current assertions (active + unresolved conflicts) and history."""
    entity_id = str(entity_id or "").strip()
    node = await get_node(entity_id)
    if node is None:
        raise LookupError(f"unknown chain node: {entity_id}")
    rows = await db.query(
        "SELECT * FROM chain_properties WHERE entity_id = ? "
        "ORDER BY prop_key, created_at DESC, id DESC",
        (entity_id,),
    )
    return {
        "entity_id": entity_id,
        "entity_name": node["name"],
        "current": [r for r in rows if r["status"] in ("active", "conflicted")],
        "history": [r for r in rows if r["status"] in ("superseded", "retired")],
    }


async def list_conflicts(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Grouped unresolved property conflicts, newest group first."""
    try:
        limit = min(max(int(limit), 1), 500)
        offset = max(int(offset), 0)
    except (TypeError, ValueError) as exc:
        raise ChainError("limit/offset must be integers") from exc
    groups = await db.query(
        "SELECT conflict_group, MAX(created_at) AS opened_at "
        "FROM chain_properties WHERE status='conflicted' AND conflict_group IS NOT NULL "
        "GROUP BY conflict_group ORDER BY opened_at DESC, conflict_group LIMIT ? OFFSET ?",
        (limit, offset),
    )
    if not groups:
        return []
    group_ids = [g["conflict_group"] for g in groups]
    marks = ",".join("?" for _ in group_ids)
    rows = await db.query(
        f"SELECT p.*, n.name AS entity_name FROM chain_properties p "
        f"JOIN chain_nodes n ON n.id = p.entity_id "
        f"WHERE p.status='conflicted' AND p.conflict_group IN ({marks}) "
        f"ORDER BY p.created_at, p.id",
        group_ids,
    )
    grouped: dict[str, list[dict[str, Any]]] = {gid: [] for gid in group_ids}
    for row in rows:
        grouped[row["conflict_group"]].append(row)
    result = []
    for group_id in group_ids:
        values = grouped[group_id]
        if not values:
            continue
        first = values[0]
        result.append({
            "conflict_group": group_id,
            "entity_id": first["entity_id"],
            "entity_name": first["entity_name"],
            "prop_key": first["prop_key"],
            "as_of": first["as_of"],
            "operator_action_id": next(
                (v["operator_action_id"] for v in values if v["operator_action_id"] is not None),
                None,
            ),
            "values": values,
        })
    return result


async def resolve_property_conflict(
    conflict_group: str, winner_id: str,
) -> dict[str, Any]:
    """Conditionally choose one conflicted assertion and retire the rest."""
    conflict_group = str(conflict_group or "").strip()
    winner_id = str(winner_id or "").strip()
    if not conflict_group or not winner_id:
        raise ChainError("conflict_group and winner_id must be non-empty")
    now = bus.now_iso()
    retired_ids: list[str] = []
    action_id: int | None = None
    winner_status = "active"
    async with db.transaction() as conn:
        cur = await conn.execute(
            "SELECT * FROM chain_properties "
            "WHERE conflict_group=? AND status='conflicted' ORDER BY created_at, rowid",
            (conflict_group,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        await cur.close()
        if not rows:
            cur = await conn.execute(
                "SELECT 1 FROM chain_properties WHERE conflict_group=? LIMIT 1",
                (conflict_group,),
            )
            existed = await cur.fetchone()
            await cur.close()
            if existed is not None:
                raise PropertyConflict(f"property conflict {conflict_group} is already resolved")
            raise LookupError(f"unknown property conflict: {conflict_group}")
        if len(rows) < 2:
            raise ChainError(f"property conflict {conflict_group} has fewer than two values")
        winner = next((r for r in rows if r["id"] == winner_id), None)
        if winner is None:
            raise ChainError(f"winner {winner_id} is not in conflict {conflict_group}")
        signature = {(r["entity_id"], r["prop_key"], r["as_of"]) for r in rows}
        if len(signature) != 1:
            raise ChainError(f"property conflict {conflict_group} mixes property periods")
        entity_id, key, as_of = next(iter(signature))
        cur = await conn.execute(
            "SELECT id FROM chain_properties WHERE entity_id=? AND prop_key=? "
            "AND status='active' AND conflict_group IS NOT ? LIMIT 1",
            (entity_id, key, conflict_group),
        )
        other_active = await cur.fetchone()
        await cur.close()
        # A newer period may have become current while this older dispute sat
        # open. The selected historical winner is then preserved as superseded
        # rather than displacing the live value.
        winner_status = "superseded" if other_active is not None else "active"
        cur = await conn.execute(
            "UPDATE chain_properties SET status=?, updated_at=? "
            "WHERE id=? AND conflict_group=? AND status='conflicted'",
            (winner_status, now, winner_id, conflict_group),
        )
        won = cur.rowcount
        await cur.close()
        if won != 1:
            raise PropertyConflict(f"property conflict {conflict_group} claim was lost")
        retired_ids = [r["id"] for r in rows if r["id"] != winner_id]
        cur = await conn.execute(
            "UPDATE chain_properties SET status='retired', updated_at=? "
            "WHERE conflict_group=? AND id<>? AND status='conflicted'",
            (now, conflict_group, winner_id),
        )
        retired = cur.rowcount
        await cur.close()
        if retired != len(retired_ids):
            raise PropertyConflict(f"property conflict {conflict_group} claim was lost")
        action_id = next(
            (int(r["operator_action_id"]) for r in rows if r["operator_action_id"] is not None),
            None,
        )
        if action_id is not None:
            # operator.resolve_action's conditional claim, inlined so the card
            # closes in the SAME transaction as the rows it tracks (it cannot
            # stay open for a committed resolution, and a card failure rolls
            # the resolution back for a clean retry)
            await conn.execute(
                "UPDATE operator_actions SET status='done', resolution=?, resolved_at=?, "
                "updated_at=? WHERE id=? AND status IN ('open','in_progress')",
                (f"property winner: {winner_id}", now, now, action_id),
            )

    try:
        await bus.emit(
            "chain.property_resolved", "chain_property", conflict_group,
            {
                "conflict_group": conflict_group,
                "entity_id": entity_id,
                "prop_key": key,
                "as_of": as_of,
                "winner_id": winner_id,
                "winner_status": winner_status,
                "retired_ids": retired_ids,
            },
        )
    except Exception:  # noqa: BLE001 - the resolution committed; never unwind it for the mirror
        log.exception("chain.property_resolved emit failed for %s", conflict_group)
    winner = await db.query_one("SELECT * FROM chain_properties WHERE id = ?", (winner_id,))
    return {
        "conflict_group": conflict_group,
        "winner": winner,
        "retired_ids": retired_ids,
        "operator_action_id": action_id,
    }


# ---- INSTR backstop tagger --------------------------------------------------------
#
# One compound SELECT scans every node name AND every alias (json_each over the
# aliases JSON array) against the artifact text with instr() — exact,
# case-sensitive substring semantics, which is what Chinese entity names need
# (no tokenizer, no case folding; ASCII aliases match case-sensitively, so
# "CATL" never fires on "catlike"... but does on any exact occurrence).

_MATCH_SQL = """\
SELECT n.id AS node_id, n.name AS node_name, n.slug AS node_slug, n.name AS term
FROM chain_nodes n WHERE instr(?1, n.name) > 0
UNION
SELECT n.id, n.name, n.slug, a.value FROM chain_nodes n, json_each(n.aliases) a
WHERE a.type = 'text' AND length(a.value) >= 2 AND instr(?1, a.value) > 0\
"""


async def _match_hits(text: str) -> list[dict[str, Any]]:
    """[{node_id, node_name, node_slug, term, pos}] — one row per node,
    earliest hit wins."""
    if not text:
        return []
    rows = await db.query(_MATCH_SQL, (text,))
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        pos = text.find(r["term"])
        if pos < 0:  # defensive: instr matched, find must too
            continue
        cur = best.get(r["node_id"])
        if cur is None or pos < cur["pos"]:
            best[r["node_id"]] = {
                "node_id": r["node_id"], "node_name": r["node_name"],
                "node_slug": r["node_slug"], "term": r["term"], "pos": pos,
            }
    return sorted(best.values(), key=lambda h: (h["pos"], h["node_name"]))


def _snippet(text: str, term: str, pos: int, radius: int = 60) -> str:
    start = max(0, pos - radius)
    end = min(len(text), pos + len(term) + radius)
    return re.sub(r"\s+", " ", text[start:end]).strip()[:200]


async def backstop_tag(artifact_kind: str, artifact_ref: str, text: str) -> list[str]:
    """Tag every known node mentioned in ``text``. Returns the node ids that
    gained a NEW mention (idempotent: same node + artifact never duplicates —
    UNIQUE index + INSERT OR IGNORE). Each newly-mentioned node emits
    ``chain.node_updated`` so its vault note refreshes."""
    artifact_kind = str(artifact_kind or "").strip()
    artifact_ref = str(artifact_ref or "").strip()
    text = text or ""
    if not artifact_kind or not artifact_ref or not text.strip():
        return []
    new_nodes: list[str] = []
    for hit in await _match_hits(text):
        inserted = await db.execute(
            "INSERT OR IGNORE INTO chain_mentions "
            "(id, node_id, artifact_kind, artifact_ref, snippet, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (_new_id(), hit["node_id"], artifact_kind, artifact_ref,
             _snippet(text, hit["term"], hit["pos"]), bus.now_iso()),
        )
        if inserted:
            new_nodes.append(hit["node_id"])
    for node_id in new_nodes:
        await bus.emit("chain.node_updated", "chain_node", node_id, {
            "reason": "mention", "artifact_kind": artifact_kind, "artifact_ref": artifact_ref,
        })
    if new_nodes:
        log.info("backstop tagged %d node(s) in %s:%s", len(new_nodes), artifact_kind, artifact_ref)
    return new_nodes


# ---- artifact text assembly (bus handlers + tick share this) ---------------------

async def _artifact_from_event(event: bus.Event) -> tuple[str, str, str] | None:
    """(artifact_kind, artifact_ref, text) for a completion event, or None.

    Kinds/refs mirror the archive + vault exporter conventions so mentions,
    archive rows and notes all point at the same artifact.
    """
    p = event.payload or {}
    if event.type == "research.completed":
        parts = [str(p.get("topic") or ""), str(p.get("summary") or "")]
        ws = await _session_workspace(p.get("session_id"))
        if ws:
            report = _read_text(ws / "06_深度报告.md")
            if report:
                parts.append(report)
        return "research", str(event.ref_id), "\n\n".join(s for s in parts if s.strip())

    if event.type == "whiteboard.board_completed":
        board_id = str(event.ref_id or "")
        if not board_id:
            return None
        board = await db.query_one(
            "SELECT topic, question FROM whiteboard_boards WHERE id = ?", (board_id,)
        )
        parts = [str(p.get("topic") or (board or {}).get("topic") or ""),
                 str((board or {}).get("question") or "")]
        cards = await db.query(
            "SELECT summary FROM whiteboard_cards WHERE board_id = ? ORDER BY idx", (board_id,)
        )
        parts.extend(str(c["summary"] or "") for c in cards)
        return "whiteboard", board_id, "\n\n".join(s for s in parts if s.strip())

    if event.type == "analyst_daily.completed":
        analyst_id = str(event.ref_id or "")
        date = str(p.get("date") or "") or work_date()
        text: str | None = None
        ws = await _session_workspace(p.get("session_id"))
        if ws:
            text = _read_text(ws / str(p.get("file") or f"{analyst_id}.md"))
        if not (text and text.strip()) and p.get("task_id"):
            row = await db.query_one("SELECT output FROM tasks WHERE id = ?", (str(p["task_id"]),))
            text = (row or {}).get("output")
        return "analyst-daily", f"{analyst_id}:{date}", text or ""

    return None


async def _on_artifact_event(event: bus.Event) -> None:
    """Live backstop pass over a finished artifact. Bus handler: never raises."""
    try:
        art = await _artifact_from_event(event)
        if art is None:
            return
        kind, ref, text = art
        if text.strip():
            await backstop_tag(kind, ref, text)
    except Exception:  # noqa: BLE001 - handlers must never break the emitter
        log.exception("chain backstop failed for %s %s", event.type, event.ref_id)


# ---- entity extraction (opencode-style tagger) ------------------------------------

def parse_extraction(output: str) -> list[dict[str, str]]:
    """``ENTITY: <name> | <kind>`` lines → [{"name", "kind"}]. Unknown kinds
    degrade to 'other'; duplicates collapse (first wins); hard cap applies."""
    seen: dict[str, str] = {}
    for m in _ENTITY_LINE.finditer(output or ""):
        name = m.group(1).strip()
        if len(name) < MIN_TERM_LEN or len(name) > 80 or name in seen:
            continue
        kind = m.group(2).strip().lower()
        seen[name] = kind if kind in KINDS else "other"
        if len(seen) >= MAX_EXTRACT_ENTITIES:
            break
    return [{"name": n, "kind": k} for n, k in seen.items()]


def parse_properties(output: str) -> list[dict[str, str]]:
    """Optional PROPERTY lines → normalized, deduplicated sourced assertions."""
    seen: set[tuple[str, str, str, str]] = set()
    properties: list[dict[str, str]] = []
    for match in _PROPERTY_LINE.finditer(output or ""):
        entity = " ".join(match.group(1).split())
        if len(entity) < MIN_TERM_LEN or len(entity) > 80:
            continue
        try:
            key, value, as_of, _ = _property_inputs(
                match.group(2), match.group(3), match.group(4), "parse",
            )
        except ChainError:
            continue
        signature = (entity, key, value, as_of)
        if signature in seen:
            continue
        seen.add(signature)
        properties.append({
            "entity": entity, "key": key, "value": value, "as_of": as_of,
        })
        if len(properties) >= MAX_EXTRACT_PROPERTIES:
            break
    return properties


async def extract_graph_facts(
    text: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """One model call → candidate entities plus optional property assertions."""
    text = (text or "").strip()
    if not text:
        return [], []
    prompt = ENTITY_EXTRACT_PROMPT.format(text=text[:EXTRACT_TEXT_CAP])
    settings = get_settings()
    task = await executor.submit(
        settings.default_hand, prompt, source=SOURCE, timeout_s=EXTRACT_TIMEOUT_S,
    )
    if task.status != "completed":
        log.warning("entity extraction task %s ended %s", task.id, task.status)
        return [], []
    output = task.output or ""
    return parse_extraction(output), parse_properties(output)


async def extract_entities(text: str) -> list[dict[str, str]]:
    """Compatibility wrapper returning only candidates from one extraction."""
    candidates, _ = await extract_graph_facts(text)
    return candidates


async def _resolve_node_term(name: str) -> str | None:
    row = await db.query_one(
        "SELECT id FROM chain_nodes WHERE name = ?1 "
        "UNION "
        "SELECT n.id FROM chain_nodes n, json_each(n.aliases) a "
        "WHERE a.type = 'text' AND a.value = ?1 LIMIT 1",
        (name,),
    )
    return str(row["id"]) if row is not None else None


STAGING_APPLY_BATCH = 200  # pending assertions applied per tick (a fresh batch
                           # is at most TICK_EVENT_BATCH * MAX_EXTRACT_PROPERTIES)
STAGING_UNKNOWN_ATTEMPTS = 24  # unknown-entity grace: one hourly-tick day before
                               # the assertion is terminally skipped


async def _stage_properties(
    event_id: int, source_ref: str, properties: list[dict[str, str]],
) -> int:
    """Persist one event's extracted assertions durably, BEFORE the event's
    cursor advances: the model call already happened, so its output must
    survive any later failure. The staging UNIQUE key makes a crash-replayed
    event's re-staging a no-op. Returns the number of NEW rows."""
    staged = 0
    now = bus.now_iso()
    for prop in properties:
        staged += await db.execute(
            "INSERT OR IGNORE INTO chain_property_staging "
            "(id, event_id, entity, prop_key, value, as_of, source_ref, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,'pending',?,?)",
            (_new_id(), int(event_id), str(prop.get("entity") or ""),
             str(prop.get("key") or ""), str(prop.get("value") or ""),
             str(prop.get("as_of") or ""), source_ref, now, now),
        )
    return staged


async def _apply_staged_properties() -> int:
    """Apply pending staged assertions whose entity now resolves to a node.

    Runs after the promotion/cluster sweep in ``tick``, so a property can land
    in the same sweep that promotes its entity. An entity that does NOT
    resolve costs the row one ``attempts`` point; only after
    ``STAGING_UNKNOWN_ATTEMPTS`` misses is it terminally ``skipped``
    (``chain_properties.entity_id`` points only at durable graph nodes). The
    miss/skip decision is ONE atomic UPDATE against the row's current
    counter, so a promotion racing the resolve check merely costs one attempt
    instead of swallowing a legitimate assertion (REVIEW-R2 P1-3) — the next
    sweep resolves the freshly promoted entity and applies the row.
    Deterministically invalid assertions are skipped outright. Any other
    failure keeps the row ``pending`` without spending attempts, so the next
    tick retries it without a new model call. Returns the number of NEW
    ``chain_properties`` rows.
    """
    rows = await db.query(
        "SELECT * FROM chain_property_staging WHERE status = 'pending' "
        "ORDER BY id LIMIT ?",
        (STAGING_APPLY_BATCH,),
    )
    applied = 0
    for row in rows:
        now = bus.now_iso()
        try:
            node_id = await _resolve_node_term(str(row["entity"]))
            if node_id is None:
                await db.execute(
                    "UPDATE chain_property_staging SET attempts = attempts + 1, "
                    "status = CASE WHEN attempts + 1 >= ? THEN 'skipped' ELSE 'pending' END, "
                    "updated_at = ? WHERE id = ? AND status = 'pending'",
                    (STAGING_UNKNOWN_ATTEMPTS, now, row["id"]),
                )
                continue
            result = await upsert_property(
                node_id, row["prop_key"], row["value"], row["as_of"], row["source_ref"],
            )
            await db.execute(
                "UPDATE chain_property_staging SET status='applied', updated_at=? "
                "WHERE id=? AND status='pending'",
                (now, row["id"]),
            )
            if result["created"]:
                applied += 1
        except (ChainError, LookupError):
            # deterministic refusal (validation shape / vanished node): a
            # retry can never succeed, so park it out of the pending scan
            log.exception("staged property %s rejected; marking skipped", row["id"])
            await db.execute(
                "UPDATE chain_property_staging SET status='skipped', updated_at=? "
                "WHERE id=? AND status='pending'",
                (now, row["id"]),
            )
        except Exception:  # noqa: BLE001 - transient (incl. lost claims): stays pending for retry
            log.exception("staged property %s application failed; will retry", row["id"])
    return applied


async def _is_known_entity(name: str) -> bool:
    row = await db.query_one(
        "SELECT id FROM chain_nodes WHERE name = ?1 "
        "UNION "
        "SELECT n.id FROM chain_nodes n, json_each(n.aliases) a "
        "WHERE a.type = 'text' AND a.value = ?1 LIMIT 1",
        (name,),
    )
    return row is not None


async def record_candidates(
    candidates: list[dict[str, str]], first_seen_ref: str, *, text: str | None = None,
) -> dict[str, int]:
    """Fold extraction output into chain_candidates. Names already resolving
    to a node (name or alias) are skipped — the backstop owns known-entity
    mentions. Each (candidate, source artifact) lands ONE
    chain_candidate_sightings row (UNIQUE key), and mention_count is
    recomputed as the count of those rows — so a crash-replayed artifact
    (events cursor written after the candidate work, REVIEW-C2 M5) never
    double-counts toward the auto-promote threshold. kind_guess and
    first_seen_ref keep the first sighting; ``text`` (when the caller has the
    artifact text at hand) captures a snippet for the promotion-time mention
    backfill. Counts: new = fresh candidate, bumped = re-sighted (whether or
    not this exact artifact was already recorded), known = skipped."""
    counts = {"new": 0, "bumped": 0, "known": 0}
    artifact_kind, _, artifact_ref = str(first_seen_ref or "").partition(":")
    for cand in candidates:
        name = str(cand.get("name") or "").strip()
        if len(name) < MIN_TERM_LEN:
            continue
        if await _is_known_entity(name):
            counts["known"] += 1
            continue
        kind_guess = str(cand.get("kind") or "other").strip().lower()
        if kind_guess not in KINDS:
            kind_guess = "other"
        snippet = None
        if text:
            pos = text.find(name)
            if pos >= 0:
                snippet = _snippet(text, name, pos)
        now = bus.now_iso()
        async with db.transaction() as conn:
            cur = await conn.execute("SELECT id FROM chain_candidates WHERE name = ?", (name,))
            row = await cur.fetchone()
            await cur.close()
            fresh = row is None
            if fresh:
                cand_id = _new_id()
                await conn.execute(
                    "INSERT INTO chain_candidates "
                    "(id, name, kind_guess, first_seen_ref, mention_count, status, created_at) "
                    "VALUES (?,?,?,?,0,'pending',?)",
                    (cand_id, name, kind_guess, first_seen_ref, now),
                )
            else:
                cand_id = row["id"]
            cur = await conn.execute(
                "INSERT OR IGNORE INTO chain_candidate_sightings "
                "(id, candidate_id, artifact_kind, artifact_ref, snippet, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (_new_id(), cand_id, artifact_kind, artifact_ref, snippet, now),
            )
            await cur.close()
            # derived, idempotent: replaying an already-recorded artifact is a no-op
            await conn.execute(
                "UPDATE chain_candidates SET mention_count = "
                "(SELECT COUNT(*) FROM chain_candidate_sightings WHERE candidate_id = ?1) "
                "WHERE id = ?1",
                (cand_id,),
            )
        counts["new" if fresh else "bumped"] += 1
    return counts


async def list_candidates(status: str = "pending", limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    status = str(status or "pending").strip()
    if status not in ("pending", "promoted", "rejected", "merged", "all"):
        raise ChainError("status must be pending/promoted/rejected/merged/all")
    sql = "SELECT * FROM chain_candidates"
    params: list[Any] = []
    if status != "all":
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY mention_count DESC, created_at ASC LIMIT ? OFFSET ?"
    params.extend([min(max(int(limit), 1), 500), max(int(offset), 0)])
    return await db.query(sql, params)


_BACKFILL_MENTIONS_SQL = """\
INSERT OR IGNORE INTO chain_mentions
  (id, node_id, artifact_kind, artifact_ref, snippet, created_at)
SELECT lower(hex(randomblob(6))), ?1, s.artifact_kind, s.artifact_ref, s.snippet, ?2
FROM chain_candidate_sightings s
WHERE s.candidate_id = ?3 AND s.artifact_kind <> '' AND s.artifact_ref <> ''\
"""


async def promote_candidate(
    candidate_id: str, kind: str, security_id: str | None = None,
) -> dict[str, Any]:
    """Promote a pending candidate to a chain node. ONE transaction covers the
    conditional status claim, node resolution/creation, ``merged_into`` and
    the sightings→``chain_mentions`` backfill (REVIEW-C2 M2: the artifacts
    that earned the promotion become real mentions; REVIEW-C2 S1: a crash
    can no longer strand a 'promoted' candidate without a node — everything
    commits or rolls back together). A name already resolving to an existing
    node — by name OR alias (REVIEW-C2 M4) — merges into it instead of
    failing. Raises LookupError (unknown id), ChainError (validation),
    PromoteConflict (not pending / lost the claim)."""
    cand = await db.query_one("SELECT * FROM chain_candidates WHERE id = ?", (candidate_id,))
    if cand is None:
        raise LookupError(f"unknown chain candidate: {candidate_id}")
    kind = _validate_kind(kind)
    security_id = await _validate_security(security_id)

    now = bus.now_iso()
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE chain_candidates SET status = 'promoted' WHERE id = ? AND status = 'pending'",
            (candidate_id,),
        )
        claimed = cur.rowcount
        await cur.close()
        if not claimed:
            raise PromoteConflict(f"candidate {candidate_id} is '{cand['status']}', not pending")

        cur = await conn.execute(
            "SELECT n.* FROM chain_nodes n WHERE n.name = ?1 "
            "UNION "
            "SELECT n.* FROM chain_nodes n, json_each(n.aliases) a "
            "WHERE a.type = 'text' AND a.value = ?1 LIMIT 1",
            (cand["name"],),
        )
        existing = await cur.fetchone()
        await cur.close()
        if existing is not None:
            node_id, merged = existing["id"], True
        else:
            node_id, merged = _new_id(), False
            slug = await _assign_slug(cand["name"], node_id, conn)
            cur = await conn.execute(
                "INSERT INTO chain_nodes (id, name, kind, security_id, aliases, slug, created_at, updated_at) "
                "VALUES (?,?,?,?,'[]',?,?,?)",
                (node_id, cand["name"], kind, security_id, slug, now, now),
            )
            inserted = cur.rowcount
            await cur.close()
            if inserted != 1:
                raise PromoteConflict("chain node insert was lost")
            await _bump_graph_generation(conn)
        await conn.execute(
            "UPDATE chain_candidates SET merged_into = ? WHERE id = ?", (node_id, candidate_id),
        )
        await conn.execute(_BACKFILL_MENTIONS_SQL, (node_id, now, candidate_id))

    await bus.emit("chain.node_updated", "chain_node", node_id, {
        "reason": "merged" if merged else "created", "name": cand["name"],
    })
    if not merged:
        log.info("chain node created: %s (%s, %s)", cand["name"], kind, node_id)
    node = await get_node(node_id)
    return {"node": node, "merged": merged, "candidate_id": candidate_id}


async def reject_candidate(candidate_id: str) -> dict[str, Any]:
    """Mark a pending candidate rejected (conditional claim, same idiom)."""
    cand = await db.query_one("SELECT * FROM chain_candidates WHERE id = ?", (candidate_id,))
    if cand is None:
        raise LookupError(f"unknown chain candidate: {candidate_id}")
    claimed = await db.execute(
        "UPDATE chain_candidates SET status = 'rejected' WHERE id = ? AND status = 'pending'",
        (candidate_id,),
    )
    if not claimed:
        raise PromoteConflict(f"candidate {candidate_id} is '{cand['status']}', not pending")
    return {**cand, "status": "rejected"}


# ---- promotion threshold (admin_state config row) ----------------------------------

async def get_promote_threshold() -> int:
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (THRESHOLD_KEY,))
    if row is None:
        return DEFAULT_PROMOTE_THRESHOLD
    try:
        value = json.loads(row["value"])
    except ValueError:
        return DEFAULT_PROMOTE_THRESHOLD
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_PROMOTE_THRESHOLD
    return max(1, int(value))


async def set_promote_threshold(value: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ChainError("threshold must be an integer >= 1") from exc
    if value < 1:
        raise ChainError("threshold must be an integer >= 1")
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (THRESHOLD_KEY, json.dumps(value)),
    )
    return value


# ---- periodic auto-cluster (ROADMAP "auto-cluster / periodic merge of aliases") ----

async def _merge_candidate_into_node(
    cand: dict[str, Any], node_id: str, *, expected_generation: int | None = None,
) -> bool:
    """Fold one pending candidate into an existing node: conditional claim to
    'merged' (+ merged_into) and the sightings→mentions backfill commit in ONE
    transaction; the candidate's surface form then joins the node's aliases
    (best-effort — an ambiguous alias is skipped, the merge stands)."""
    now = bus.now_iso()
    async with db.transaction() as conn:
        if (
            expected_generation is not None
            and await _graph_generation(conn) != expected_generation
        ):
            raise ClusterGenerationChanged("chain graph changed before cluster merge")
        cur = await conn.execute(
            "UPDATE chain_candidates SET status = 'merged', merged_into = ? "
            "WHERE id = ? AND status = 'pending'",
            (node_id, cand["id"]),
        )
        claimed = cur.rowcount
        await cur.close()
        if not claimed:
            return False
        await conn.execute(_BACKFILL_MENTIONS_SQL, (node_id, now, cand["id"]))
    try:
        await merge_aliases(node_id, cand["name"])
    except ChainError:
        log.info("clustered candidate %r into node %s without alias (ambiguous)",
                 cand["name"], node_id)
    await bus.emit("chain.node_updated", "chain_node", node_id, {
        "reason": "cluster", "candidate": cand["name"],
    })
    return True


async def _age_out_candidates() -> int:
    """Aging half of LOOP-P8b: pending candidates that sat below the promote
    threshold for CANDIDATE_TTL_DAYS stop occupying every future cluster
    scan — one conditional bulk claim to 'rejected' (``WHERE
    status='pending'``; rowcount is the aged count), so the pending pool the
    per-tick matching iterates is bounded by live interest, not by history."""
    threshold = await get_promote_threshold()
    cutoff = (
        datetime.fromisoformat(bus.now_iso()) - timedelta(days=CANDIDATE_TTL_DAYS)
    ).isoformat(timespec="seconds")
    aged = await db.execute(
        "UPDATE chain_candidates SET status = 'rejected' "
        "WHERE status = 'pending' AND mention_count < ? AND created_at < ?",
        (threshold, cutoff),
    )
    if aged:
        log.info("aged out %d stale candidate(s) below threshold %d", aged, threshold)
    return aged


async def _clear_cluster_rotation() -> None:
    await db.execute(
        "DELETE FROM admin_state WHERE key = ?", (CLUSTER_ROTATION_KEY,),
    )


def _default_rotation_state(generation: int) -> dict[str, Any]:
    return {
        "version": CLUSTER_ROTATION_VERSION,
        "generation": generation,
        "cand_cursor": 0,
        "candidate_id": None,
        "node_cursor": 0,
        "node_id": None,
        "term_offset": 0,
        "matches": [],
    }


def _cluster_terms(node: dict[str, Any]) -> list[str]:
    """Stable searchable terms for one graph node.

    Every stored alias is a legal graph surface (production writes do not cap
    alias count), so correctness requires scanning ALL of them. Per-tick work
    remains bounded by CLUSTER_COMPARE_BUDGET; ``term_offset`` carries a long
    node across ticks. Sorting normalized, de-duplicated terms gives that
    offset a stable meaning for the lifetime of one graph generation.
    """
    aliases = _parse_aliases(node.get("aliases"))
    return sorted({
        norm for term in (str(node.get("name") or ""), *aliases)
        if (norm := _norm_term(term))
    })


def _cluster_term_matches(candidate_norm: str, term: str) -> bool:
    return term == candidate_norm or (
        min(len(term), len(candidate_norm)) >= MIN_CLUSTER_CONTAIN_LEN
        and (term in candidate_norm or candidate_norm in term)
    )


def _rotation_scalar_id(value: Any, *, nullable: bool) -> bool:
    if value is None:
        return nullable
    return isinstance(value, str) and 1 <= len(value) <= 300


def _rotation_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_SQLITE_ROWID
    )


def _rotation_schema_valid(state: Any) -> bool:
    expected = {
        "version", "generation", "cand_cursor", "candidate_id",
        "node_cursor", "node_id", "term_offset", "matches",
    }
    if not isinstance(state, dict) or set(state) != expected:
        return False
    if (
        not isinstance(state["version"], int)
        or isinstance(state["version"], bool)
        or state["version"] != CLUSTER_ROTATION_VERSION
    ):
        return False
    if not all(_rotation_int(state[key]) for key in (
        "generation", "cand_cursor", "node_cursor", "term_offset",
    )):
        return False
    if not _rotation_scalar_id(state["candidate_id"], nullable=True):
        return False
    if not _rotation_scalar_id(state["node_id"], nullable=True):
        return False
    matches = state["matches"]
    if (
        not isinstance(matches, list)
        or len(matches) > 2
        or not all(_rotation_scalar_id(item, nullable=False) for item in matches)
        or len(set(matches)) != len(matches)
    ):
        return False
    if state["candidate_id"] is None:
        return (
            state["node_cursor"] == 0
            and state["node_id"] is None
            and state["term_offset"] == 0
            and not matches
        )
    if state["node_id"] is None and state["term_offset"] != 0:
        return False
    if state["node_id"] is not None and state["term_offset"] == 0:
        return False
    return True


async def _load_cluster_rotation(generation: int) -> dict[str, Any]:
    """Load and semantically validate parked matching evidence.

    Malformed or stale state is deleted and reset instead of being repeatedly
    rebound into SQLite (R4 P2). In addition to strict JSON types/ranges, the
    cursors must fit current rowid bounds, the in-flight candidate/node must
    exist, and every remembered match must still exist AND match the current
    candidate. Graph-generation mismatch invalidates all cursor/evidence.
    """
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (CLUSTER_ROTATION_KEY,),
    )
    if row is None:
        return _default_rotation_state(generation)
    try:
        state = json.loads(row["value"])
    except (TypeError, ValueError):
        state = None
    if not _rotation_schema_valid(state) or state["generation"] != generation:
        await _clear_cluster_rotation()
        return _default_rotation_state(generation)

    bounds = await db.query_one(
        "SELECT "
        "COALESCE((SELECT MAX(rowid) FROM chain_candidates), 0) AS max_candidate, "
        "COALESCE((SELECT MAX(rowid) FROM chain_nodes), 0) AS max_node",
    )
    if (
        state["cand_cursor"] > bounds["max_candidate"]
        or state["node_cursor"] > bounds["max_node"]
    ):
        await _clear_cluster_rotation()
        return _default_rotation_state(generation)

    candidate_id = state["candidate_id"]
    if candidate_id is None:
        return state
    cand = await db.query_one(
        "SELECT rowid AS rid, name FROM chain_candidates "
        "WHERE id = ? AND status = 'pending'",
        (candidate_id,),
    )
    if cand is None or cand["rid"] <= state["cand_cursor"]:
        await _clear_cluster_rotation()
        return _default_rotation_state(generation)
    candidate_norm = _norm_term(cand["name"])

    if state["node_id"] is not None:
        node = await db.query_one(
            "SELECT rowid AS rid, id, name, aliases FROM chain_nodes WHERE id = ?",
            (state["node_id"],),
        )
        if (
            node is None
            or node["rid"] <= state["node_cursor"]
            or state["term_offset"] >= len(_cluster_terms(node))
        ):
            await _clear_cluster_rotation()
            return _default_rotation_state(generation)

    for match_id in state["matches"]:
        match = await db.query_one(
            "SELECT rowid AS rid, id, name, aliases FROM chain_nodes WHERE id = ?",
            (match_id,),
        )
        if (
            match is None
            or match["rid"] > state["node_cursor"]
            or not any(
                _cluster_term_matches(candidate_norm, term)
                for term in _cluster_terms(match)
            )
        ):
            await _clear_cluster_rotation()
            return _default_rotation_state(generation)
    return state


async def _save_cluster_rotation(state: dict[str, Any]) -> bool:
    """Persist advisory rotation state only while its generation is current."""
    if await _graph_generation() != state["generation"]:
        await _clear_cluster_rotation()
        return False
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (CLUSTER_ROTATION_KEY, json.dumps(state)),
    )
    return True


async def _auto_cluster() -> int:
    """Light periodic clustering pass (REVIEW-C2 M1 — the ROADMAP
    "auto-cluster/merge" / "periodic merge of aliases" item): a pending
    candidate folds into an existing node when its name is a near-certain
    surface form of that node —

    - **normalized-equal**: casefolded, whitespace-stripped name matches the
      node's name or one of its aliases ("Tesla Inc" ≡ "tesla  inc"), or
    - **containment**: one term contains the other and the shorter side is at
      least MIN_CLUSTER_CONTAIN_LEN chars ("宁德时代股份有限公司" ⊃
      "宁德时代"; a 2-char fragment absorbs nothing).

    A candidate matching ZERO or MULTIPLE nodes is left alone (conservative:
    ambiguity goes to the human / threshold path).

    Bounded work (LOOP-P8b, hardened for R3 P2): stale low-interest
    candidates age out first; then one sweep spends at most
    CLUSTER_COMPARE_BUDGET surface-term comparisons TOTAL — candidates ×
    nodes × aliases — regardless of graph size. Candidates and nodes are
    walked as a persistent rowid rotation (admin_state
    ``chain:cluster_rotation``): a candidate whose node/alias scan outgrows
    one sweep's budget parks at a stable ``node_id`` + ``term_offset`` and
    resumes next tick. Its merge/ambiguity DECISION is made only after its
    FULL node rotation completes (two matches short-circuit as ambiguous) —
    a plain stateless node LIMIT would merge into an early match while
    missing a late ambiguous one.

    R4 hardening binds every parked cursor and remembered match to a
    monotonic graph generation. Any production node insert/name/alias change
    invalidates the evidence and restarts from node zero; the generation is
    checked again inside the final merge transaction. Strict cursor schema
    and semantic validation self-heal corrupt admin_state. The rotation state
    is advisory scan progress; the merge itself stays a conditional claim
    inside ``_merge_candidate_into_node``. Returns merged count."""
    await _age_out_candidates()
    budget = CLUSTER_COMPARE_BUDGET
    merged = 0
    generation = await _graph_generation()
    state = await _load_cluster_rotation(generation)
    while budget > 0:
        live_generation = await _graph_generation()
        if live_generation != generation:
            generation = live_generation
            state = _default_rotation_state(generation)
            await _clear_cluster_rotation()
        cand = None
        if state["candidate_id"]:
            cand = await db.query_one(
                "SELECT rowid AS rid, * FROM chain_candidates "
                "WHERE id = ? AND status = 'pending'",
                (state["candidate_id"],),
            )
            if cand is None:  # decided elsewhere meanwhile: drop the stale scan
                state = {
                    **_default_rotation_state(generation),
                    "cand_cursor": state["cand_cursor"],
                }
        if cand is None:
            cand = await db.query_one(
                "SELECT rowid AS rid, * FROM chain_candidates "
                "WHERE status = 'pending' AND rowid > ? ORDER BY rowid LIMIT 1",
                (state["cand_cursor"],),
            )
            if cand is None:  # rotation lap complete; next sweep starts fresh
                await _clear_cluster_rotation()
                break
            budget -= 1  # floor charge per candidate visit (a node-less graph
                         # must not make the rotation free and unbounded)
            state = {
                **_default_rotation_state(generation),
                "cand_cursor": state["cand_cursor"],
                "candidate_id": cand["id"],
            }
        norm = _norm_term(cand["name"])
        matches = {str(m) for m in state["matches"]}
        node_cursor = int(state["node_cursor"])
        node_id = state["node_id"]
        term_offset = int(state["term_offset"])
        rotation_done = False
        while budget > 0 and len(matches) < 2:
            if node_id is not None:
                current = await db.query_one(
                    "SELECT rowid AS rid, id, name, aliases FROM chain_nodes WHERE id = ?",
                    (node_id,),
                )
                window = [current] if current is not None else []
            else:
                window = await db.query(
                    "SELECT rowid AS rid, id, name, aliases FROM chain_nodes "
                    "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (node_cursor, CLUSTER_NODE_WINDOW),
                )
            if not window:
                if await _graph_generation() != generation:
                    break
                rotation_done = True
                break
            for n in window:
                if budget <= 0 or len(matches) >= 2:
                    break
                terms = _cluster_terms(n)
                offset = term_offset if n["id"] == node_id else 0
                hit = False
                while offset < len(terms):
                    if budget <= 0:
                        break
                    budget -= 1
                    term = terms[offset]
                    offset += 1
                    if _cluster_term_matches(norm, term):
                        hit = True
                        break
                if hit:
                    matches.add(str(n["id"]))
                    node_cursor = n["rid"]
                    node_id, term_offset = None, 0
                elif offset == len(terms):
                    node_cursor = n["rid"]  # node fully cleared, no match
                    node_id, term_offset = None, 0
                else:
                    # Budget died mid-node: resume at the first unchecked term.
                    node_id, term_offset = n["id"], offset
                    break
        if len(matches) >= 2:
            rotation_done = True  # ambiguous — decided without finishing the lap
        live_generation = await _graph_generation()
        if live_generation != generation:
            generation = live_generation
            state = _default_rotation_state(generation)
            await _clear_cluster_rotation()
            continue
        if not rotation_done:
            # budget exhausted mid-rotation: park the scan for the next sweep
            await _save_cluster_rotation({
                "version": CLUSTER_ROTATION_VERSION,
                "generation": generation,
                "cand_cursor": state["cand_cursor"],
                "candidate_id": cand["id"],
                "node_cursor": node_cursor,
                "node_id": node_id,
                "term_offset": term_offset,
                "matches": sorted(matches),
            })
            break
        if len(matches) == 1:
            try:
                if await _merge_candidate_into_node(
                    cand, next(iter(matches)), expected_generation=generation,
                ):
                    merged += 1
            except ClusterGenerationChanged:
                generation = await _graph_generation()
                state = _default_rotation_state(generation)
                await _clear_cluster_rotation()
                continue
            except Exception:  # noqa: BLE001 - one candidate must not break the sweep
                log.exception("auto-cluster failed for candidate %s", cand["id"])
        # decided (merged / no match / ambiguous): rotate to the next candidate
        current_generation = await _graph_generation()
        if current_generation != generation:
            generation = current_generation
            state = _default_rotation_state(generation)
        else:
            state = {
                **_default_rotation_state(generation),
                "cand_cursor": cand["rid"],
            }
        await _save_cluster_rotation(state)
    if merged:
        log.info("auto-clustered %d candidate(s) into existing nodes", merged)
    return merged


async def _auto_promote() -> int:
    """Promote pending candidates at/over the mention threshold, using their
    kind_guess (validated, degrading to 'other'). Lost claims are skipped.
    LOOP-P11b: at most AUTO_PROMOTE_BATCH per tick — each promotion is a
    transaction plus vault-export fan-out, so a backlog drains across sweeps
    instead of monopolizing one."""
    threshold = await get_promote_threshold()
    rows = await db.query(
        "SELECT id, kind_guess FROM chain_candidates "
        "WHERE status = 'pending' AND mention_count >= ? ORDER BY mention_count DESC "
        "LIMIT ?",
        (threshold, AUTO_PROMOTE_BATCH),
    )
    promoted = 0
    for r in rows:
        kind = str(r["kind_guess"] or "other").strip().lower()
        if kind not in KINDS:
            kind = "other"
        try:
            await promote_candidate(r["id"], kind)
            promoted += 1
        except PromoteConflict:
            continue  # claimed by a concurrent promote — fine
        except Exception:  # noqa: BLE001 - one candidate must not break the sweep
            log.exception("auto-promotion failed for candidate %s", r["id"])
    if promoted:
        log.info("auto-promoted %d candidate(s) at threshold %d", promoted, threshold)
    return promoted


# ---- hourly tick (scheduler job body) -----------------------------------------------

_tick_lock = asyncio.Lock()


async def _get_cursor() -> int:
    row = await db.query_one("SELECT value FROM admin_state WHERE key = ?", (CURSOR_KEY,))
    if row is None:
        return 0
    try:
        value = json.loads(row["value"])
    except ValueError:
        return 0
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


async def _advance_cursor(prev: int, event_id: int) -> bool:
    """Conditional-claim cursor advance (compare-and-swap on the admin_state
    row, REVIEW-R2 P2-1): the UPDATE only lands while the cursor still holds
    ``prev``, so a second tick owner — another process sharing the DB; the
    in-process case is already excluded by ``_tick_lock`` — that read the
    same starting cursor loses the swap, can never REGRESS the cursor, and
    must abandon its batch instead of re-running the winner's model calls.
    Returns True when this owner still holds the cursor."""
    if prev <= 0:
        # seed the row so the very first advance is also a plain CAS
        await db.execute(
            "INSERT OR IGNORE INTO admin_state (key, value) VALUES (?, ?)",
            (CURSOR_KEY, json.dumps(0)),
        )
    # CAST mirrors _get_cursor's tolerant decode (non-numeric values read as
    # 0), so a malformed stored value stays healable instead of wedging every
    # future swap
    claimed = await db.execute(
        "UPDATE admin_state SET value = ? WHERE key = ? AND CAST(value AS INTEGER) = ?",
        (json.dumps(int(event_id)), CURSOR_KEY, int(prev)),
    )
    return claimed > 0


async def _persist_failures(event_id: int) -> int:
    """Durable persistence-failure count for ONE event (LOOP-P4 / R3 P1).
    ``count >= TICK_PERSIST_FAILURE_LIMIT`` IS the drop-pending marker: the
    tick reads it BEFORE extracting, so an exhausted event can never buy
    another model call, no matter where an earlier drop attempt crashed."""
    row = await db.query_one(
        "SELECT value FROM admin_state WHERE key = ?", (PERSIST_FAILURES_KEY,),
    )
    if row is None:
        return 0
    try:
        state = json.loads(row["value"])
        if isinstance(state, dict) and int(state.get("event_id", -1)) == int(event_id):
            return max(int(state.get("count", 0)), 0)
    except (ValueError, TypeError):
        return 0
    return 0


async def _note_persist_failure(event_id: int) -> int:
    """Record one persistence failure for the halted head-of-queue event.

    ONE admin_state row suffices — only the head event can hold the cursor,
    and a different event id resets the row. The increment is a single
    in-place ``json_set`` UPDATE guarded by the stored event id (R3 P1:
    read-then-overwrite lost counts across processes; a statement-atomic
    increment cannot), so concurrent owners can only push the count HIGHER —
    the drop bound still holds. Advisory telemetry: the actual skip still
    gates through the ``_advance_cursor`` conditional claim. Returns the
    count including this failure."""
    bumped = await db.execute(
        "UPDATE admin_state SET value = json_set(value, '$.count', "
        "COALESCE(json_extract(value, '$.count'), 0) + 1) "
        "WHERE key = ? AND json_valid(value) AND json_extract(value, '$.event_id') = ?",
        (PERSIST_FAILURES_KEY, int(event_id)),
    )
    if not bumped:
        # different event / missing / malformed row: this event owns the
        # counter now (two processes racing the reset converge on count 1 —
        # at worst one extra bounded retry)
        await db.execute(
            "INSERT INTO admin_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (PERSIST_FAILURES_KEY, json.dumps({"event_id": int(event_id), "count": 1})),
        )
        return 1
    return await _persist_failures(event_id)


async def _clear_persist_failures(event_id: int) -> None:
    """Retire the counter row once ITS event advanced past the cursor — the
    event-id guard cannot erase a concurrent owner's fresh count for a
    different event."""
    await db.execute(
        "DELETE FROM admin_state WHERE key = ? AND json_valid(value) "
        "AND json_extract(value, '$.event_id') = ?",
        (PERSIST_FAILURES_KEY, int(event_id)),
    )


async def _open_extract_drop_action(event_id: int, event_type: str, failures: int) -> None:
    """Operator card for a dropped poison event (LOOP-P4): the receipt that
    the tick gave up persisting this event's extraction after the bounded
    retries and advanced the cursor past it. ``open_action`` is safe here —
    tick holds no transaction, and the card is idempotent per ref, so a
    crash between the card and the cursor advance re-converges on retry."""
    await open_action(
        "failed_run",
        f"chain-extract:{int(event_id)}",
        f"Chain extraction dropped: event {int(event_id)} ({event_type})",
        f"event_id: {int(event_id)}\nevent_type: {event_type}\n"
        f"persistence failures: {failures} (limit {TICK_PERSIST_FAILURE_LIMIT})\n"
        "The extraction output for this artifact could not be persisted and "
        "the event was skipped past the cursor to stop hourly model-call "
        "replays. The live backstop pass already tagged the artifact; re-run "
        "extraction manually if its candidates/properties matter.",
    )


async def tick() -> dict[str, Any]:
    """Hourly chain sweep: extraction over new artifacts + auto-promotion.

    Consumes ARTIFACT_EVENT_TYPES completions through the monotonic events.id
    cursor (oldest first, TICK_EVENT_BATCH per run — the backlog drains across
    runs). Per artifact: a catch-up backstop pass (idempotent) + ONE
    extraction model call, whose parsed output is persisted BEFORE that
    event's cursor advances — candidates through the idempotent sightings
    key (REVIEW-C2 M5), property assertions through the durable
    ``chain_property_staging`` table — so a paid-for extraction is never
    lost. Assembly/extraction failures still advance the cursor (best-effort
    enrichment: the live handler already ran the backstop; a stuck artifact
    must not wedge the queue), but a PERSISTENCE failure halts the batch
    before that event's cursor, so only the un-persisted event replays next
    tick — bounded by TICK_PERSIST_FAILURE_LIMIT (LOOP-P4): a deterministic
    poison event is dropped past the cursor with an operator card instead of
    buying one extraction model call per hour forever. Every advance is a
    conditional claim (``_advance_cursor`` CAS): a
    concurrent tick owner in another process can never regress the cursor,
    and the claim loser abandons its batch instead of re-running the
    winner's model calls. After the batch, auto-cluster and auto-promotion
    sweep pending candidates, then pending staged assertions are applied (a
    failed application stays pending and retries WITHOUT re-running the
    model). Gated job: it submits model calls, so it must respect the
    maintenance pause.
    """
    async with _tick_lock:
        cursor = await _get_cursor()
        marks = ",".join("?" for _ in ARTIFACT_EVENT_TYPES)
        rows = await db.query(
            f"SELECT id, type, ref_id, payload, created_at FROM events "
            f"WHERE id > ? AND type IN ({marks}) ORDER BY id ASC LIMIT ?",
            (cursor, *ARTIFACT_EVENT_TYPES, TICK_EVENT_BATCH),
        )
        processed, candidates_seen = 0, 0
        for r in rows:
            # R3 P1: an event that already exhausted its persistence retries
            # is dropped BEFORE extraction — however an earlier drop attempt
            # crashed (after the count, after the card, before the cursor),
            # recovery must never buy another model call for it. Card first
            # (idempotent receipt), then the cursor claim.
            exhausted = await _persist_failures(r["id"])
            if exhausted >= TICK_PERSIST_FAILURE_LIMIT:
                try:
                    await _open_extract_drop_action(r["id"], r["type"], exhausted)
                except Exception:  # noqa: BLE001 - no card, no silent drop: keep holding the cursor
                    log.exception(
                        "could not open the drop card for exhausted event %s; keeping "
                        "the cursor held", r["id"],
                    )
                    break
                if not await _advance_cursor(cursor, r["id"]):
                    log.warning(
                        "chain tick lost the cursor claim at event %s; abandoning the batch",
                        r["id"],
                    )
                    break
                await _clear_persist_failures(r["id"])
                log.warning(
                    "dropped exhausted event %s past the cursor without re-extraction "
                    "(%d persistence failures)", r["id"], exhausted,
                )
                cursor = r["id"]
                processed += 1
                continue
            try:
                payload = json.loads(r["payload"] or "{}")
            except ValueError:
                payload = {}
            event = bus.Event(
                id=r["id"], type=r["type"], ref_id=r["ref_id"],
                payload=payload, created_at=r["created_at"],
            )
            kind = ref = text = ""
            cands: list[dict[str, str]] = []
            properties: list[dict[str, str]] = []
            try:
                art = await _artifact_from_event(event)
                if art is not None:
                    kind, ref, text = art
                    if text.strip():
                        await backstop_tag(kind, ref, text)
                        cands, properties = await extract_graph_facts(text)
            except Exception:  # noqa: BLE001 - one artifact must not wedge the cursor
                log.exception("chain tick failed on event %s (%s)", r["id"], r["type"])
                cands, properties = [], []
            dropped = False
            if cands or properties:
                try:
                    if cands:
                        await record_candidates(cands, f"{kind}:{ref}", text=text)
                        candidates_seen += len(cands)
                    if properties:
                        await _stage_properties(r["id"], f"{kind}:{ref}", properties)
                except Exception:  # noqa: BLE001 - the model already ran; losing its output is worse than a retry
                    failures = await _note_persist_failure(r["id"])
                    if failures < TICK_PERSIST_FAILURE_LIMIT:
                        log.exception(
                            "extraction persistence failed on event %s (attempt %d/%d); "
                            "halting the batch before its cursor",
                            r["id"], failures, TICK_PERSIST_FAILURE_LIMIT,
                        )
                        break
                    # LOOP-P4: a deterministic poison event must not buy one
                    # extraction model call per hour forever. Card first (the
                    # drop's receipt), then the cursor skip — both idempotent,
                    # so a crash between them re-converges next tick.
                    try:
                        await _open_extract_drop_action(r["id"], r["type"], failures)
                    except Exception:  # noqa: BLE001 - no card, no silent drop: keep holding the cursor
                        log.exception(
                            "could not open the drop card for event %s; keeping the "
                            "cursor held for another retry", r["id"],
                        )
                        break
                    log.exception(
                        "extraction persistence failed %d times on event %s; dropping "
                        "it past the cursor (operator card opened)", failures, r["id"],
                    )
                    dropped = True
            if not await _advance_cursor(cursor, r["id"]):
                # another tick owner moved the cursor first: abandon the rest
                # of this batch — its events belong to the winner now (the
                # work above was idempotent), and re-extracting them here
                # would double-spend model quota
                log.warning(
                    "chain tick lost the cursor claim at event %s; abandoning the batch",
                    r["id"],
                )
                break
            if dropped:
                await _clear_persist_failures(r["id"])
            cursor = r["id"]
            processed += 1
        clustered = await _auto_cluster()
        promoted = await _auto_promote()
        properties_seen = await _apply_staged_properties()
        result = {
            "events": processed, "candidates": candidates_seen,
            "properties": properties_seen,
            "clustered": clustered, "auto_promoted": promoted,
        }
        if processed or clustered or promoted:
            log.info("chain tick: %s", result)
        return result


# ---- vault projection ------------------------------------------------------------

def _wikilink(name: str, slug: str) -> str:
    """[[slug]] targeting the node's PERSISTED unique slug (REVIEW-C2 M3 —
    recomputing _slug(name) here could point two names at one note), with an
    alias display part when the slug differs from the name."""
    return f"[[{slug}]]" if slug == name else f"[[{slug}|{name}]]"


async def _render_note(node: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """(vault relpath, frontmatter, region body) for one node."""
    detail = await node_detail(node["id"]) or node
    lines: list[str] = [f"# {node['name']}", "", f"kind:: {node['kind']}"]
    if node.get("security_id"):
        lines.append(f"security:: {node['security_id']}")
    if node["aliases"]:
        lines.append(f"别名：{'、'.join(node['aliases'])}")

    edges_out = detail.get("edges_out") or []
    edges_in = detail.get("edges_in") or []
    if edges_out or edges_in:
        lines += ["", "## 关系", ""]
        # Dataview inline typed relations — one `relation:: [[dst]]` per edge
        for e in edges_out:
            lines.append(f"{e['relation']}:: {_wikilink(e['dst_name'], e['dst_slug'])}")
        for e in edges_in:
            lines.append(f"- {_wikilink(e['src_name'], e['src_slug'])} —{e['relation']}→ 本实体")

    mentions = (detail.get("mentions") or [])[:MENTIONS_IN_NOTE]
    if mentions:
        lines += ["", "## 最近提及", ""]
        for m in mentions:
            when = str(m["created_at"] or "")[:10]
            snippet = (m["snippet"] or "").strip() or "（无摘录）"
            lines.append(f"- `{m['artifact_kind']}:{m['artifact_ref']}`（{when}）：{snippet}")

    relpath = f"Chain/{node['slug']}.md"
    frontmatter = {
        "type": "chain-node", "kind": node["kind"], "aliases": node["aliases"],
        "security": node.get("security_id"), "node_id": node["id"],
    }
    return relpath, frontmatter, "\n".join(lines)


async def export_entity_note(node_id: str) -> str | None:
    """Project one node to Chain/<entity>.md (managed region: kind/aliases/
    edges/mentions live between the markers; human notes outside survive)."""
    writer = get_writer()
    if not writer.enabled:
        return None
    node = await get_node(node_id)
    if node is None:
        log.warning("no chain node %s to export", node_id)
        return None
    relpath, frontmatter, body = await _render_note(node)
    return await writer.write_note(
        relpath, frontmatter, body,
        artifact_kind="chain-node", artifact_id=node_id, region=True,
    )


async def export_dashboards() -> str | None:
    """_meta/Dashboards.md — starter Dataview queries over the chain notes.
    Region-mode: the operator can add their own dashboards outside the markers."""
    writer = get_writer()
    if not writer.enabled:
        return None
    return await writer.write_note(
        "_meta/Dashboards.md", {"type": "chain-dashboards"}, DASHBOARDS_BODY,
        artifact_kind="chain-dashboards", artifact_id="chain-dashboards", region=True,
    )


async def _on_node_updated(event: bus.Event) -> None:
    """chain.node_updated → refresh the entity note (+ ensure the dashboards
    note exists; skip-if-unchanged makes that free). Never raises."""
    try:
        if not get_writer().enabled:
            return
        await export_entity_note(str(event.ref_id))
        await export_dashboards()
    except Exception:  # noqa: BLE001 - handlers must never break the emitter
        log.exception("chain vault projection failed for %s", event.ref_id)


async def entity_footer(text: str) -> str:
    """``## Entities`` wikilink footer for an exported note body: every known
    node whose name/alias appears in ``text``, in first-appearance order.
    Links target each node's persisted unique slug (REVIEW-C2 M3). Empty
    string when nothing matches — callers append only non-empty footers."""
    hits = await _match_hits(text or "")
    if not hits:
        return ""
    links = [_wikilink(h["node_name"], h["node_slug"]) for h in hits[:FOOTER_MAX_LINKS]]
    return "## Entities\n" + " ".join(links)


# ---- historical footer reprojection (REVIEW-C2 M2 backfill channel) -----------------
#
# The entity_footer injection points only fire when a source note is exported,
# so notes written BEFORE a node existed never gain its wikilink. reproject_
# footers() closes that: it walks the vault_index rows the exporter owns,
# re-reads each note FROM DISK (disk is the historical truth — workspaces may
# be gone, so re-assembling bodies from rows could destroy content), strips
# the old ## Entities block, recomputes the footer against the CURRENT node
# set and rewrites through the writer. Notes whose disk bytes no longer match
# the ledger (human edits, conflict rows) are counted and left untouched —
# a maintenance sweep must never manufacture conflict siblings.

REPROJECT_KINDS = ("research", "briefing", "daily", "whiteboard", "analyst-daily", "memory")

_FOOTER_HEADING = "## Entities\n"
_PARSE_FAIL: Any = object()


def _parse_yaml_scalar(tok: str) -> Any:
    """Inverse of writer._yaml_scalar for values WE wrote. _PARSE_FAIL on any
    shape _yaml_scalar cannot have produced (reproject never guesses)."""
    if tok.startswith('"'):
        if len(tok) < 2 or not tok.endswith('"'):
            return _PARSE_FAIL
        inner, out, i = tok[1:-1], [], 0
        unescape = {"\\": "\\", '"': '"', "n": "\n", "t": "\t"}
        while i < len(inner):
            ch = inner[i]
            if ch == "\\":
                mapped = unescape.get(inner[i + 1]) if i + 1 < len(inner) else None
                if mapped is None:
                    return _PARSE_FAIL
                out.append(mapped)
                i += 2
            elif ch == '"':
                return _PARSE_FAIL  # a bare quote inside is not our encoding
            else:
                out.append(ch)
                i += 1
        return "".join(out)
    if tok == "true":
        return True
    if tok == "false":
        return False
    if vault_writer._NUMBER_LIKE.match(tok):
        try:
            return int(tok)
        except ValueError:
            return float(tok)
    return tok


def _split_flat_list(inner: str) -> list[str] | None:
    """Split the element text of writer's ``[a, b, "c, d"]`` on top-level
    ``, `` separators (quote-aware; bare elements never contain commas —
    _yaml_scalar quotes them)."""
    parts, buf, in_quote, i = [], [], False, 0
    while i < len(inner):
        ch = inner[i]
        if in_quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(inner):
                buf.append(inner[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quote = False
            i += 1
            continue
        if ch == '"':
            in_quote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "," and inner[i + 1:i + 2] == " ":
            parts.append("".join(buf))
            buf, i = [], i + 2
            continue
        buf.append(ch)
        i += 1
    if in_quote:
        return None
    parts.append("".join(buf))
    return parts


def _parse_frontmatter_block(text: str) -> tuple[dict[str, Any], str] | None:
    """Inverse of VaultWriter.compose for notes WE wrote: (frontmatter dict,
    body). Round-trips created/type/tags/etc so a footer rewrite preserves the
    original metadata. None when the shape is not compose()'s output."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    fm: dict[str, Any] = {}
    for line in text[4:end].split("\n"):
        key, sep, raw = line.partition(": ")
        if not sep or not key or key != key.strip():
            return None
        if raw.startswith("[") and raw.endswith("]"):
            value: Any = []
            if raw != "[]":
                items = _split_flat_list(raw[1:-1])
                if items is None:
                    return None
                for tok in items:
                    parsed = _parse_yaml_scalar(tok)
                    if parsed is _PARSE_FAIL:
                        return None
                    value.append(parsed)
        else:
            value = _parse_yaml_scalar(raw)
            if value is _PARSE_FAIL:
                return None
        fm[key] = value
    body = text[end + 5:]
    # compose() is exactly "---\n{yaml}\n---\n\n{stripped body}\n"
    if not body.startswith("\n") or not body.endswith("\n"):
        return None
    return fm, body[1:-1]


def _strip_entity_footer(body: str) -> str:
    """Drop the trailing ``## Entities`` block. Every injection point appends
    the footer LAST, so everything from the last heading on is ours."""
    if body.startswith(_FOOTER_HEADING):
        return ""
    pos = body.rfind(f"\n{_FOOTER_HEADING}")
    if pos < 0:
        return body
    return body[:pos].rstrip()


async def _reproject_one(writer: vault_writer.VaultWriter, row: dict[str, Any]) -> str:
    """Recompute one ledgered note's footer against the current node set.
    Returns 'reprojected' | 'skipped' | 'conflicts'."""
    assert writer.root is not None
    target = writer.root / row["path"]
    if row["state"] == "conflict":
        return "conflicts"  # already flagged: a human owns this file now
    if not target.exists():
        return "skipped"

    if row["mode"] == "region":
        text = vault_writer._read_exact(target)
        if text is None:
            return "skipped"
        region = vault_writer._extract_region(text)
        if (region is None or not vault_writer._has_ownership(text)
                or vault_writer._sha_text(region) != row["sha256"]):
            return "conflicts"  # region/markers/ownership no longer as we left them
        old_body = region
        parsed = _parse_frontmatter_block(text)
        # region rewrites are in-place (fingerprint just verified); the
        # frontmatter only matters on the writer's fallback paths
        fm = parsed[0] if parsed else {"type": row["artifact_kind"]}
    else:
        if vault_writer._sha_file(target) != row["sha256"]:
            return "conflicts"  # human edited since our last write
        parsed = _parse_frontmatter_block(target.read_text(encoding="utf-8"))
        if parsed is None:
            return "skipped"
        fm, old_body = parsed

    stripped = _strip_entity_footer(old_body)
    footer = await entity_footer(stripped)
    new_body = f"{stripped}\n\n{footer}" if footer else stripped
    if new_body == old_body:
        return "skipped"  # footer already current — nothing to write

    written = await writer.write_note(
        row["path"], fm, new_body,
        artifact_kind=row["artifact_kind"], artifact_id=row["artifact_id"],
        region=(row["mode"] == "region"),
    )
    return "reprojected" if written == row["path"] else "conflicts"


async def reproject_footers(kind: str | None = None, cap: int = 50) -> dict[str, int]:
    """Backfill/refresh ``## Entities`` footers on already-exported source
    notes (REVIEW-C2 M2: nodes promoted after an export never reached the
    historical vault). Walks the managed ``vault_index`` rows (optionally one
    ``kind``), rewrites through the writer ONLY when the recomputed footer
    differs (skip-if-unchanged keeps repeat sweeps free), and stops after
    ``cap`` rewrites so one call can never rewrite the whole vault at once —
    call again until ``reprojected`` comes back 0. Human-edited/conflict notes
    are reported, never touched. Returns {reprojected, skipped, conflicts}."""
    writer = get_writer()
    counts = {"reprojected": 0, "skipped": 0, "conflicts": 0}
    if not writer.enabled:
        return counts
    if kind is not None:
        kind = str(kind).strip()
        if kind not in REPROJECT_KINDS:
            raise ChainError(
                f"unknown reproject kind '{kind}' (expected one of {', '.join(REPROJECT_KINDS)})"
            )
    try:
        cap = int(cap)
    except (TypeError, ValueError) as exc:
        raise ChainError("cap must be an integer >= 1") from exc
    cap = min(max(cap, 1), 500)

    kinds = (kind,) if kind else REPROJECT_KINDS
    marks = ",".join("?" for _ in kinds)
    rows = await db.query(
        f"SELECT path, artifact_kind, artifact_id, sha256, state, mode "
        f"FROM vault_index WHERE artifact_kind IN ({marks}) ORDER BY path",
        kinds,
    )
    for row in rows:
        if counts["reprojected"] >= cap:
            break  # cap guards the write volume; the rest waits for the next call
        try:
            outcome = await _reproject_one(writer, row)
        except Exception:  # noqa: BLE001 - one bad note must not stop the sweep
            log.exception("footer reprojection failed for %s", row["path"])
            outcome = "skipped"
        counts[outcome] += 1
    if counts["reprojected"] or counts["conflicts"]:
        log.info("chain reproject: %s", counts)
    return counts


# ---- wiring -------------------------------------------------------------------------

def register() -> None:
    """Hook the chain tagger + vault projection into the bus. Called once from
    the app lifespan."""
    for event_type in ARTIFACT_EVENT_TYPES:
        bus.on(event_type, _on_artifact_event)
    bus.on("chain.node_updated", _on_node_updated)
    log.info("chain graph registered (backstop tagger + vault projection)")
