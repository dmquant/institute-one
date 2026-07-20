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
2. **Entity extraction** (opencode-style, model call): ``extract_entities``
   submits ``ENTITY_EXTRACT_PROMPT`` through ``executor.submit`` (the one
   execution path; cheap ``settings.default_hand``) and parses ``ENTITY:``
   lines into ``chain_candidates``. Every DISTINCT source artifact lands one
   ``chain_candidate_sightings`` row and ``mention_count`` aggregates those
   rows — a crash-replayed artifact never double-counts (REVIEW-C2 M5), and
   the sightings are the full source set that promotion backfills into
   ``chain_mentions`` (REVIEW-C2 M2). Candidates are promoted to nodes
   manually (``promote_candidate`` / the API) or automatically once
   ``mention_count`` reaches the ``admin_state`` threshold (key
   ``chain:promote_threshold``, default 3). The echo hand echoes the prompt
   back, so a fixture text that already contains ``ENTITY:`` lines exercises
   the whole path in tests.

``tick()`` (hourly, gated=True — it spends model quota) consumes finished
artifacts through a monotonic events.id cursor (``admin_state`` key
``chain:extract_cursor``): each new research/whiteboard/analyst-daily
completion gets a catch-up backstop pass plus one extraction task, then a
light-weight cluster pass (``_auto_cluster`` — the ROADMAP "auto-cluster /
periodic merge of aliases": pending candidates whose normalized name matches
or contains/is contained by an existing node's name/alias fold into that node
as an alias, REVIEW-C2 M1) and auto-promotion sweep pending candidates. The
cursor advances even when extraction fails (best-effort enrichment — the live
handler already backstop-tagged the artifact; a stuck artifact must not wedge
the queue). Overlap safety: APScheduler ``max_instances=1`` plus an in-process
lock; candidate/status transitions use the conditional-claim idiom.

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
from pathlib import Path
from typing import Any

from .. import bus, db
from ..config import get_settings
from ..router import executor
from ..vault import writer as vault_writer
from ..vault.writer import get_writer
from .prompts import work_date

log = logging.getLogger("institute.chain")

SOURCE = "chain"

KINDS = ("company", "product", "technology", "commodity", "person", "org", "other")

# Suggested edge vocabulary (open set — chain_edges.relation has no CHECK):
RELATION_VOCABULARY = ("supplier_of", "customer_of", "competitor_of", "subsidiary_of", "produces")

ARTIFACT_EVENT_TYPES = ("research.completed", "whiteboard.board_completed", "analyst_daily.completed")

CURSOR_KEY = "chain:extract_cursor"
THRESHOLD_KEY = "chain:promote_threshold"
DEFAULT_PROMOTE_THRESHOLD = 3

EXTRACT_TEXT_CAP = 6000       # chars of artifact text fed to the extraction prompt
EXTRACT_TIMEOUT_S = 600
MAX_EXTRACT_ENTITIES = 20
TICK_EVENT_BATCH = 10         # artifacts per tick (one model call each; hourly)
MENTIONS_IN_NOTE = 8
FOOTER_MAX_LINKS = 50
MIN_TERM_LEN = 2              # single CJK chars over-match; names/aliases must be >= 2
MIN_CLUSTER_CONTAIN_LEN = 4   # containment-based clustering needs a term this long
                              # ("电池" must not absorb "固态电池"; "宁德时代" may
                              # absorb "宁德时代股份有限公司")

# ---- prompt constants (verbatim-stable once written; CLAUDE.md rule 4) ------
# No line in this template may START with "ENTITY:" — the echo hand echoes the
# whole prompt back and the parser reads ^ENTITY: lines, so a bare example
# line would parse as a hit. The example stays inline after 示例：.

ENTITY_EXTRACT_PROMPT = """\
你是产业链实体抽取器。从下面的研究文本中抽取值得长期跟踪的实体：公司、产品、技术、大宗商品、人物、组织。

【输出格式】每找到一个实体输出一行，行首写 ENTITY: 后接实体规范名，再接「 | 」与类型。类型只能取 company / product / technology / commodity / person / org / other 之一。除这些行外不要输出任何其他内容。示例：`ENTITY: 台积电 | company`

【抽取规则】
1. 只抽取有产业链跟踪价值的具体实体；泛称（如「市场」「政策」「行业」）不抽。
2. 实体名用文本中最常见的规范写法，公司优先用通用简称。
3. 每个实体只输出一次，最多输出 20 个；一个没有就输出 NONE。
4. 拿不准类型时用 other。

【文本】
{text}\
"""

_ENTITY_LINE = re.compile(r"^\s*ENTITY:\s*(.+?)\s*\|\s*([A-Za-z_]+)\s*$", re.MULTILINE)

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
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.warning("could not read %s", path)
    return None


# ---- node / edge CRUD -----------------------------------------------------------

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
        await conn.execute(
            "INSERT INTO chain_nodes (id, name, kind, security_id, aliases, slug, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (node_id, name, kind, security_id,
             json.dumps(clean_aliases, ensure_ascii=False), slug, now, now),
        )
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
            await conn.execute(
                "UPDATE chain_nodes SET aliases = ?, updated_at = ? WHERE id = ?",
                (json.dumps(aliases, ensure_ascii=False), bus.now_iso(), node_id),
            )
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


async def extract_entities(text: str) -> list[dict[str, str]]:
    """One model call (executor path, cheap default hand) → parsed candidates.
    Failures degrade to an empty list — extraction is best-effort enrichment."""
    text = (text or "").strip()
    if not text:
        return []
    prompt = ENTITY_EXTRACT_PROMPT.format(text=text[:EXTRACT_TEXT_CAP])
    settings = get_settings()
    task = await executor.submit(
        settings.default_hand, prompt, source=SOURCE, timeout_s=EXTRACT_TIMEOUT_S,
    )
    if task.status != "completed":
        log.warning("entity extraction task %s ended %s", task.id, task.status)
        return []
    return parse_extraction(task.output or "")


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
            await conn.execute(
                "INSERT INTO chain_nodes (id, name, kind, security_id, aliases, slug, created_at, updated_at) "
                "VALUES (?,?,?,?,'[]',?,?,?)",
                (node_id, cand["name"], kind, security_id, slug, now, now),
            )
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

async def _merge_candidate_into_node(cand: dict[str, Any], node_id: str) -> bool:
    """Fold one pending candidate into an existing node: conditional claim to
    'merged' (+ merged_into) and the sightings→mentions backfill commit in ONE
    transaction; the candidate's surface form then joins the node's aliases
    (best-effort — an ambiguous alias is skipped, the merge stands)."""
    now = bus.now_iso()
    async with db.transaction() as conn:
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
    ambiguity goes to the human / threshold path). Returns merged count."""
    pending = await db.query("SELECT * FROM chain_candidates WHERE status = 'pending'")
    if not pending:
        return 0
    nodes = await db.query("SELECT id, name, aliases FROM chain_nodes")
    if not nodes:
        return 0
    surfaces = [
        (n["id"], {_norm_term(t) for t in (n["name"], *_parse_aliases(n["aliases"]))})
        for n in nodes
    ]
    merged = 0
    for cand in pending:
        norm = _norm_term(cand["name"])
        matched_ids: set[str] = set()
        for node_id, terms in surfaces:
            if norm in terms:
                matched_ids.add(node_id)
                continue
            for t in terms:
                if min(len(t), len(norm)) >= MIN_CLUSTER_CONTAIN_LEN and (t in norm or norm in t):
                    matched_ids.add(node_id)
                    break
        if len(matched_ids) != 1:
            continue
        try:
            if await _merge_candidate_into_node(cand, next(iter(matched_ids))):
                merged += 1
        except Exception:  # noqa: BLE001 - one candidate must not break the sweep
            log.exception("auto-cluster failed for candidate %s", cand["id"])
    if merged:
        log.info("auto-clustered %d candidate(s) into existing nodes", merged)
    return merged


async def _auto_promote() -> int:
    """Promote every pending candidate at/over the mention threshold, using
    its kind_guess (validated, degrading to 'other'). Lost claims are skipped."""
    threshold = await get_promote_threshold()
    rows = await db.query(
        "SELECT id, kind_guess FROM chain_candidates "
        "WHERE status = 'pending' AND mention_count >= ? ORDER BY mention_count DESC",
        (threshold,),
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


async def _set_cursor(event_id: int) -> None:
    await db.execute(
        "INSERT INTO admin_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (CURSOR_KEY, json.dumps(int(event_id))),
    )


async def tick() -> dict[str, Any]:
    """Hourly chain sweep: extraction over new artifacts + auto-promotion.

    Consumes ARTIFACT_EVENT_TYPES completions through the monotonic events.id
    cursor (oldest first, TICK_EVENT_BATCH per run — the backlog drains across
    runs). Per artifact: a catch-up backstop pass (idempotent) + ONE
    extraction model call. Candidate sightings are idempotent per (candidate,
    artifact) — a crash between the candidate writes and _set_cursor() replays
    the event without double-counting (REVIEW-C2 M5). The cursor advances per
    event even when that event's work failed — extraction is best-effort
    enrichment and must never wedge the queue (the live handler already ran
    the backstop). After the batch: the auto-cluster pass folds near-certain
    candidate surface forms into existing nodes, then auto-promotion sweeps
    the remaining pending candidates over the threshold. Gated job: it
    submits model calls, so it must respect the maintenance pause.
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
            try:
                payload = json.loads(r["payload"] or "{}")
            except ValueError:
                payload = {}
            event = bus.Event(
                id=r["id"], type=r["type"], ref_id=r["ref_id"],
                payload=payload, created_at=r["created_at"],
            )
            try:
                art = await _artifact_from_event(event)
                if art is not None:
                    kind, ref, text = art
                    if text.strip():
                        await backstop_tag(kind, ref, text)
                        cands = await extract_entities(text)
                        if cands:
                            await record_candidates(cands, f"{kind}:{ref}", text=text)
                            candidates_seen += len(cands)
            except Exception:  # noqa: BLE001 - one artifact must not wedge the cursor
                log.exception("chain tick failed on event %s (%s)", r["id"], r["type"])
            await _set_cursor(r["id"])
            processed += 1
        clustered = await _auto_cluster()
        promoted = await _auto_promote()
        result = {
            "events": processed, "candidates": candidates_seen,
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
