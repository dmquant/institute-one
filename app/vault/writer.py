"""Obsidian vault writer — the ONLY component that writes under settings.vault_dir.

The five rules:
(a) atomic: write a tmp file in the same directory, then ``os.replace``;
(b) ownership: every note carries YAML frontmatter with ``managed: institute``;
(c) hash ledger (``vault_index``): a human-edited note is NEVER overwritten —
    the update lands beside it as ``<stem> (institute update <work_date>)<suffix>``
    and the original row is marked ``conflict``;
(d) skip-if-unchanged: identical content hash + file present means no write;
(e) rebuildable: ``doctor()`` compares ledger vs disk at any time.

If ``settings.vault_dir`` is None every method is a silent no-op returning None.
"""
from __future__ import annotations

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

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @property
    def root(self) -> Path | None:
        return self._root

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

    async def _upsert(self, relpath: str, artifact_kind: str, artifact_id: str, sha: str, state: str) -> None:
        await db.execute(
            "INSERT INTO vault_index (path, artifact_kind, artifact_id, sha256, state, written_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET artifact_kind=excluded.artifact_kind, "
            "artifact_id=excluded.artifact_id, sha256=excluded.sha256, "
            "state=excluded.state, written_at=excluded.written_at",
            (relpath, artifact_kind, artifact_id, sha, state, bus.now_iso()),
        )

    async def write_note(
        self, relpath: str, frontmatter: dict, body: str, *, artifact_kind: str, artifact_id: str
    ) -> str | None:
        """Write a managed note. Returns the vault-relative path actually holding
        the new content (the conflict-sibling path when a human edit is detected),
        or None when the vault is disabled."""
        if self._root is None:
            return None
        rel, target = self._resolve(relpath)
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

    # ---- ledger vs disk ------------------------------------------------------

    async def doctor(self) -> dict[str, int] | None:
        if self._root is None:
            return None
        rows = await db.query("SELECT path, sha256, state FROM vault_index")
        counts = {"total": len(rows), "clean": 0, "conflict": 0, "missing": 0, "drifted": 0}
        for r in rows:
            path = self._root / r["path"]
            if not path.exists():
                counts["missing"] += 1
            elif r["state"] == "conflict":
                counts["conflict"] += 1
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
