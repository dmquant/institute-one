"""Obsidian vault writer — the ONLY component that writes under settings.vault_dir.

The five rules:
(a) atomic: write a tmp file in the same directory, then ``os.replace``;
(b) ownership: every note carries YAML frontmatter with ``managed: institute``;
(c) hash ledger (``vault_index``): a human-edited note is NEVER overwritten —
    the update lands beside it as ``<stem> (institute update <work_date>)<suffix>``
    and the original row is marked ``conflict``;
(d) skip-if-unchanged: identical content hash + file present means no write;
(e) rebuildable: ``doctor()`` compares ledger vs disk at any time.

Managed regions (rule 4 of the proposal, ``write_note(..., region=True)``):
the institute owns ONLY the content between ``%% institute:begin %%`` and
``%% institute:end %%`` marker lines; everything outside belongs to the human
and survives rewrites byte-for-byte (files are re-read without newline
translation and re-assembled by exact slices). For these notes the ledger row
carries ``mode='region'`` and its sha256 covers the EXACT region text (no
normalization), so human annotations outside the markers change neither the
write path's clobber check nor ``doctor()``. In-place updates additionally
require the marker structure to be strict (exactly one begin, one end, in
order), the frontmatter to still carry ``managed: institute`` (ownership may
not be edited away), and the on-disk region to match the ledger fingerprint
exactly — ANY mismatch takes the rule-(c) conflict-sibling path, and sibling
names are always fresh (never reuse an existing file). Whole-file notes
(``mode='file'``) behave exactly as before.

If ``settings.vault_dir`` is None every method is a silent no-op returning None.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from .. import bus, db
from ..config import Settings, get_settings
from ..institute.prompts import work_date

log = logging.getLogger("institute.vault")

# ---- flat YAML rendering -------------------------------------------------

_QUOTE_TRIGGER = re.compile(r'[:#\[\]{},&*!?|>%@`"\'\\\n\t]')
_NUMBER_LIKE = re.compile(r"^[-+]?(\.\d+|\d+\.?\d*)([eE][-+]?\d+)?$")
_BOOL_NULL = {"true", "false", "null", "~", "yes", "no", "on", "off", "none"}


def _needs_quote(s: str) -> bool:
    if s == "" or s != s.strip():
        return True
    if s.lower() in _BOOL_NULL or _NUMBER_LIKE.match(s):
        return True
    if _QUOTE_TRIGGER.search(s):
        return True
    return s[0] in "-? "


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if not _needs_quote(s):
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def _yaml_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_scalar(v) for v in value) + "]"
    return _yaml_scalar(value)


def _snake(key: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).strip("_").lower()
    return s or "key"


def _sha_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---- managed regions (rule 4) ----------------------------------------------
# Obsidian renders %% … %% as comments, so the markers stay invisible in
# preview while remaining greppable plain lines in source mode.
#
# Region-mode five-rules compliance (REVIEW-B3):
# (a) atomic       — every region write goes through the same _atomic_write
#                    (tmp file in the target directory + os.replace).
# (b) ownership    — fresh/upgrade/sibling files get frontmatter via compose();
#                    in-place region updates REQUIRE the ownership marker line
#                    ("managed: institute") to still be present — a human who
#                    edits it away gets a conflict sibling, never an in-place
#                    write that would silently launder an unowned file.
# (c) never-clobber— the in-place path needs an exact fingerprint match of the
#                    on-disk region bytes vs the ledger; ANY mismatch (edited
#                    region, moved/duplicated/nested/missing markers, unknown
#                    file, unreadable file) diverts to a conflict sibling, and
#                    the sibling name is guaranteed fresh (uniqueness suffix)
#                    so an already-existing — possibly human-edited — sibling
#                    is never overwritten either.
# (d) skip-if-unchanged — exact-text hash equality (ledger AND disk region vs
#                    the new body); whitespace-only region edits are real
#                    changes and correctly fail the equality.
# (e) rebuildable  — doctor() audits mode='region' rows against the exact
#                    region hash plus structure/ownership, so annotations
#                    outside markers stay clean while edits inside, malformed
#                    markers, lost ownership or undecodable files count as
#                    drift; ledger vs disk remains fully recomputable.
#
# Byte fidelity: files are read with newline="" (no universal-newline
# translation) and re-assembled by pure string slicing, so bytes outside the
# markers — including CRLF line endings and trailing whitespace — survive
# in-place updates unchanged. Hashes cover the exact region text between the
# marker lines (own newlines excluded), with no strip()/normalization.

REGION_BEGIN = "%% institute:begin %%"
REGION_END = "%% institute:end %%"
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_OWNERSHIP_LINE = re.compile(r"^managed:\s*institute\s*$", re.MULTILINE)


def _iter_lines(text: str):
    """Yield (start, end, break_end) per line: content is text[start:end], its
    terminator text[end:break_end] (empty for a final unterminated line)."""
    pos, n = 0, len(text)
    while pos < n:
        m = _LINE_BREAK.search(text, pos)
        if m is None:
            yield pos, n, n
            return
        yield pos, m.start(), m.end()
        pos = m.end()


def _region_span(text: str) -> tuple[int, int] | None:
    """Character span (start, end) of the region content, or None.

    Strict structure only: exactly ONE begin marker line and exactly ONE end
    marker line in the whole file, begin before end, begin terminated by a
    line break. Anything else — missing, duplicated, nested or out-of-order
    markers — returns None so callers take the conservative "human content"
    path. Marker lines must contain nothing else (surrounding whitespace
    tolerated). The span covers exactly the text between BEGIN's line break
    and the line break preceding END, i.e. precisely what a rewrite replaces.
    """
    lines = list(_iter_lines(text))
    begins = [i for i, (s, e, _) in enumerate(lines) if text[s:e].strip() == REGION_BEGIN]
    ends = [i for i, (s, e, _) in enumerate(lines) if text[s:e].strip() == REGION_END]
    if len(begins) != 1 or len(ends) != 1:
        return None
    bi, ei = begins[0], ends[0]
    if ei <= bi:
        return None
    b_start, b_end, b_brk = lines[bi]
    if b_brk == b_end:  # begin is the last, unterminated line
        return None
    if ei == bi + 1:    # empty region: END follows BEGIN directly
        return b_brk, b_brk
    return b_brk, lines[ei - 1][1]


def _extract_region(text: str) -> str | None:
    """The EXACT region text (no normalization), or None when malformed."""
    span = _region_span(text)
    if span is None:
        return None
    return text[span[0]:span[1]]


def _replace_region(text: str, new_body: str) -> str:
    """Swap the region content by exact slicing; all other bytes survive."""
    span = _region_span(text)
    assert span is not None, "caller must have verified the region is well-formed"
    start, end = span
    if start == end and new_body:
        # empty region: BEGIN's terminator doubles as the break before END, so
        # a non-empty body needs its own trailing break (reuse BEGIN's style)
        brk = "\n"
        for m in _LINE_BREAK.finditer(text, 0, start):
            brk = m.group()
        return text[:start] + new_body + brk + text[end:]
    return text[:start] + new_body + text[end:]


def _has_ownership(text: str) -> bool:
    """True when the note still carries its 'managed: institute' frontmatter line."""
    return bool(_OWNERSHIP_LINE.search(text))


def _read_exact(path: Path) -> str | None:
    """Read preserving line endings exactly; None when unreadable/undecodable."""
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _sha_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class VaultWriter:
    """All vault writes flow through here so the hash ledger stays authoritative."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._root: Path | None = settings.vault_dir.expanduser() if settings.vault_dir else None
        # Coordinates the disk os.replace -> ledger upsert interval with the
        # operator's final drift recheck. Without this shared lock, a sweep
        # could observe new bytes against the old ledger and open a false
        # conflict card while a perfectly normal writer was paused in that
        # tiny interval (R5 P3).
        self._coordination_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def coordination_lock(self) -> asyncio.Lock:
        """Writer/sweep critical-section lock for disk + ledger consistency."""
        return self._coordination_lock

    # ---- note composition ------------------------------------------------

    def _merge_frontmatter(self, frontmatter: dict, artifact_kind: str) -> dict:
        raw = {_snake(k): v for k, v in (frontmatter or {}).items() if v is not None}
        raw.pop("managed", None)
        note_type = str(raw.pop("type", "") or artifact_kind)
        created = str(raw.pop("created", "") or work_date())
        tags = raw.pop("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        tags = [str(t) for t in tags]
        own_tag = f"institute/{note_type}"
        if own_tag not in tags:
            tags.append(own_tag)
        merged: dict[str, Any] = {"managed": "institute", "type": note_type, "created": created, "tags": tags}
        merged.update(raw)
        return merged

    def compose(self, frontmatter: dict, body: str, *, artifact_kind: str) -> str:
        fm = self._merge_frontmatter(frontmatter, artifact_kind)
        lines = ["---"]
        lines.extend(f"{k}: {_yaml_value(v)}" for k, v in fm.items())
        lines.append("---")
        return "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"

    # ---- ledgered writes ---------------------------------------------------

    def _resolve(self, relpath: str) -> tuple[str, Path]:
        rel = PurePosixPath(str(relpath).replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ValueError(f"unsafe vault path: {relpath!r}")
        assert self._root is not None
        return str(rel), self._root / rel

    async def _upsert(
        self, relpath: str, artifact_kind: str, artifact_id: str, sha: str, state: str,
        mode: str = "file",
    ) -> None:
        await db.execute(
            "INSERT INTO vault_index (path, artifact_kind, artifact_id, sha256, state, written_at, mode) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET artifact_kind=excluded.artifact_kind, "
            "artifact_id=excluded.artifact_id, sha256=excluded.sha256, "
            "state=excluded.state, written_at=excluded.written_at, mode=excluded.mode",
            (relpath, artifact_kind, artifact_id, sha, state, bus.now_iso(), mode),
        )

    async def write_note(
        self, relpath: str, frontmatter: dict, body: str, *, artifact_kind: str, artifact_id: str,
        region: bool = False,
    ) -> str | None:
        """Write a managed note. Returns the vault-relative path actually holding
        the new content (the conflict-sibling path when a human edit is detected),
        or None when the vault is disabled.

        ``region=True`` switches to managed-region semantics: only the content
        between the ``%% institute:begin/end %%`` markers is replaced and the
        ledger hashes the region, not the file (see module docstring)."""
        if self._root is None:
            return None
        async with self._coordination_lock:
            return await self._write_note_locked(
                relpath, frontmatter, body,
                artifact_kind=artifact_kind, artifact_id=artifact_id, region=region,
            )

    async def _write_note_locked(
        self, relpath: str, frontmatter: dict, body: str, *, artifact_kind: str,
        artifact_id: str, region: bool,
    ) -> str:
        """write_note body; caller holds ``coordination_lock``."""
        rel, target = self._resolve(relpath)
        if region:
            return await self._write_region(
                rel, target, frontmatter, body,
                artifact_kind=artifact_kind, artifact_id=artifact_id,
            )
        content = self.compose(frontmatter, body, artifact_kind=artifact_kind)
        new_sha = _sha_text(content)
        row = await db.query_one("SELECT sha256, state FROM vault_index WHERE path = ?", (rel,))

        if row and new_sha == row["sha256"] and target.exists():
            return rel  # rule (d): unchanged

        if row and target.exists():
            disk_sha = _sha_file(target)
            if disk_sha is not None and disk_sha != row["sha256"]:
                # rule (c): human edited — never overwrite, write a sibling instead
                p = PurePosixPath(rel)
                alt_rel = str(p.parent / f"{p.stem} (institute update {work_date()}){p.suffix}")
                assert self._root is not None
                _atomic_write(self._root / alt_rel, content)
                await db.execute(
                    "UPDATE vault_index SET state='conflict', written_at=? WHERE path=?",
                    (bus.now_iso(), rel),
                )
                await self._upsert(alt_rel, artifact_kind, artifact_id, new_sha, "clean")
                await bus.emit("vault.conflict", "vault", rel, {})
                log.warning("vault conflict on %s; wrote %s", rel, alt_rel)
                return alt_rel

        _atomic_write(target, content)
        await self._upsert(rel, artifact_kind, artifact_id, new_sha, "clean")
        log.info("vault write: %s (%s %s)", rel, artifact_kind, artifact_id)
        return rel

    # ---- region-mode writes (rule 4) ----------------------------------------

    def _fresh_sibling(self, rel: str) -> str:
        """A conflict-sibling path guaranteed not to exist on disk yet.

        Rule (c) applies to siblings too: an existing (possibly human-edited)
        sibling from an earlier conflict the same day must never be reused.
        """
        p = PurePosixPath(rel)
        base = f"{p.stem} (institute update {work_date()})"
        assert self._root is not None
        for n in range(100):
            name = f"{base}{'' if n == 0 else f' {n + 1}'}{p.suffix}"
            alt_rel = str(p.parent / name)
            if not (self._root / alt_rel).exists():
                return alt_rel
        return str(p.parent / f"{base} {uuid.uuid4().hex[:6]}{p.suffix}")

    async def _write_region(
        self, rel: str, target: Path, frontmatter: dict, body: str,
        *, artifact_kind: str, artifact_id: str,
    ) -> str:
        """Region-aware write: replace only the marked region, keep the rest.

        Ledger semantics: sha256 (mode='region') hashes the EXACT region text
        between the marker lines — no strip, no newline normalization — so any
        byte change inside the region reads as a human edit, while any amount
        of human content outside the markers stays invisible to the clobber
        check. In-place updates additionally require strict marker structure
        and a surviving ``managed: institute`` line; every mismatch takes the
        rule-(c) conflict-sibling path with a fresh sibling name.
        """
        body_text = (body or "").strip()  # normalize OUR content; disk is never normalized
        region_sha = _sha_text(body_text)
        row = await db.query_one(
            "SELECT sha256, state, mode FROM vault_index WHERE path = ?", (rel,)
        )

        exists = target.exists()
        disk_text = _read_exact(target) if exists else None  # None: missing OR unreadable
        disk_region = _extract_region(disk_text) if disk_text is not None else None
        # in-place eligibility: strict markers + ownership marker still present
        disk_ok = disk_region is not None and _has_ownership(disk_text or "")

        # rule (d): ledger AND the intact on-disk region carry exactly this content
        if (
            row and row["sha256"] == region_sha
            and disk_ok and _sha_text(disk_region) == region_sha
        ):
            return rel

        fresh = self.compose(
            frontmatter, f"{REGION_BEGIN}\n{body_text}\n{REGION_END}",
            artifact_kind=artifact_kind,
        )

        if not exists:
            _atomic_write(target, fresh)
            await self._upsert(rel, artifact_kind, artifact_id, region_sha, "clean", mode="region")
            log.info("vault write (region): %s (%s %s)", rel, artifact_kind, artifact_id)
            return rel

        if disk_ok and row and _sha_text(disk_region) == row["sha256"]:
            # structure strict, ownership intact, region bytes exactly as we
            # left them -> swap the region only; all other bytes survive
            _atomic_write(target, _replace_region(disk_text, body_text))
            await self._upsert(rel, artifact_kind, artifact_id, region_sha, "clean", mode="region")
            log.info("vault write (region): %s (%s %s)", rel, artifact_kind, artifact_id)
            return rel

        if disk_text is not None and disk_region is None and row and _sha_file(target) == row["sha256"]:
            # marker-less file byte-identical to its ledger entry: an institute
            # whole-file note with no human edits -> safe to upgrade in place
            _atomic_write(target, fresh)
            await self._upsert(rel, artifact_kind, artifact_id, region_sha, "clean", mode="region")
            log.info("vault write (region upgrade): %s (%s %s)", rel, artifact_kind, artifact_id)
            return rel

        # every remaining case is content we must not clobber: an edited or
        # structurally altered region, a removed ownership marker, a marker-less
        # file drifted from its ledger row, a file the ledger has never seen,
        # or a file we cannot even decode -> rule (c) conflict sibling
        alt_rel = self._fresh_sibling(rel)
        assert self._root is not None
        _atomic_write(self._root / alt_rel, fresh)
        if row:
            await db.execute(
                "UPDATE vault_index SET state='conflict', written_at=? WHERE path=?",
                (bus.now_iso(), rel),
            )
        await self._upsert(alt_rel, artifact_kind, artifact_id, region_sha, "clean", mode="region")
        await bus.emit("vault.conflict", "vault", rel, {})
        log.warning("vault region conflict on %s; wrote %s", rel, alt_rel)
        return alt_rel

    # ---- ledger vs disk ------------------------------------------------------

    async def doctor(self) -> dict[str, int] | None:
        if self._root is None:
            return None
        rows = await db.query("SELECT path, sha256, state, mode FROM vault_index")
        counts = {"total": len(rows), "clean": 0, "conflict": 0, "missing": 0, "drifted": 0}
        for r in rows:
            path = self._root / r["path"]
            if not path.exists():
                counts["missing"] += 1
            elif r["state"] == "conflict":
                counts["conflict"] += 1
            elif r["mode"] == "region":
                # region rows hash the exact region text only: annotations
                # outside the markers are legitimate; an edited region,
                # malformed/duplicated markers, a removed ownership line or an
                # undecodable file is drift
                text = _read_exact(path)
                if text is None:
                    # deleted between exists() and read (race) vs unreadable
                    counts["missing" if not path.exists() else "drifted"] += 1
                    continue
                region = _extract_region(text)
                if region is None or _sha_text(region) != r["sha256"] or not _has_ownership(text):
                    counts["drifted"] += 1
                else:
                    counts["clean"] += 1
            elif _sha_file(path) != r["sha256"]:
                counts["drifted"] += 1
            else:
                counts["clean"] += 1
        return counts


# ---- singleton -------------------------------------------------------------

_writer: VaultWriter | None = None


def get_writer() -> VaultWriter:
    global _writer
    if _writer is None:
        _writer = VaultWriter(get_settings())
    return _writer


def reset_writer() -> None:
    """Tests repoint settings, then call this."""
    global _writer
    _writer = None
