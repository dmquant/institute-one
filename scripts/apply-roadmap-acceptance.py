#!/usr/bin/env python3
"""Plan or apply one audited roadmap reconciliation/acceptance batch.

The default does not alter roadmap rows or events (normal ``db.init()`` may
still apply pending schema migrations). ``--apply`` requires an
integrity-checked backup, maintenance mode, and an empty active task queue.
New seed cards are always staged in ``inbox`` before pass evidence/checklists
drive normal ``move()`` gates. The manifest and idempotency keys make an
interrupted run retry-safe.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True, help="Institute home containing institute.db")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--backup", type=Path, help="Required pre-apply SQLite backup")
    parser.add_argument("--apply", action="store_true", help="Apply after printing the plan")
    parser.add_argument("--export-path", type=Path, help="Write the accepted DB snapshot as backlog JSON")
    return parser.parse_args()


def _load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not str(data.get("batch_id") or "").strip():
        raise SystemExit("manifest needs a non-empty batch_id")
    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        raise SystemExit("manifest needs a non-empty groups list")
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or group.get("target_status") not in {"done", "review", "inbox"}:
            raise SystemExit("every manifest group needs target_status done|review|inbox")
        ids = group.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(cid, str) and cid for cid in ids):
            raise SystemExit("every manifest group needs non-empty string ids")
        overlap = seen.intersection(ids)
        if overlap:
            raise SystemExit(f"duplicate manifest card ids: {', '.join(sorted(overlap))}")
        seen.update(ids)
    return data


def _check_backup(path: Path | None, live_db: Path) -> None:
    if path is None:
        raise SystemExit("--apply requires --backup")
    backup = path.expanduser().resolve()
    if backup == live_db.resolve() or not backup.is_file() or backup.stat().st_size == 0:
        raise SystemExit("backup must be a distinct, non-empty SQLite file")
    conn = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()
    finally:
        conn.close()
    if not result or result[0] != "ok":
        raise SystemExit(f"backup quick_check failed: {result!r}")


def _expand_groups(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cards: dict[str, dict[str, Any]] = {}
    for group in manifest["groups"]:
        for card_id in group["ids"]:
            cards[card_id] = {
                "target_status": group["target_status"],
                "summary": str(group.get("summary") or "").strip(),
                "verification": str(group.get("verification") or "").strip(),
            }
    return cards


async def _run(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    # Imports happen only after INSTITUTE_HOME is pinned for this process.
    from app import db
    from app.institute import roadmap

    await db.init()
    try:
        source = json.loads(roadmap.default_backlog_path().read_text(encoding="utf-8"))
        plan = await roadmap.import_backlog(
            dry_run=True,
            new_card_status_policy="inbox",
        )
        print(json.dumps({"reconciliation_plan": plan}, ensure_ascii=False, indent=2))

        decisions = _expand_groups(manifest)
        expected_ids = {card["id"] for card in source["cards"]} | {
            card["card_id"] for card in plan["live_only"]
        }
        if set(decisions) != expected_ids:
            missing = sorted(expected_ids - set(decisions))
            extra = sorted(set(decisions) - expected_ids)
            raise SystemExit(f"manifest/board mismatch; missing={missing}, extra={extra}")
        if not args.apply:
            return

        maintenance = await db.query_one("SELECT value FROM admin_state WHERE key = 'maintenance'")
        try:
            paused = bool(json.loads(maintenance["value"]).get("paused")) if maintenance else False
        except (TypeError, ValueError):
            paused = False
        active = await db.query_one(
            "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('queued','running')"
        )
        if not paused or (active and active["n"]):
            raise SystemExit(
                f"apply requires maintenance paused and zero queued/running tasks; "
                f"paused={paused}, active={active['n'] if active else 'unknown'}"
            )

        applied = await roadmap.import_backlog(new_card_status_policy="inbox")
        batch_id = str(manifest["batch_id"])
        artifact_ref = str(manifest.get("artifact_ref") or args.manifest)

        # Backfill one durable operator session and evidence row per card. The
        # create APIs and their idempotency records commit atomically.
        for card_id in sorted(decisions):
            decision = decisions[card_id]
            target = decision["target_status"]
            session = await roadmap.create_session(
                card_id,
                actor="operator",
                goal=f"Operator acceptance batch {batch_id}",
                planned_files=[],
                idempotency_key=batch_id,
            )
            if session is None:
                raise RuntimeError(f"missing roadmap card after import: {card_id}")
            # An idempotency replay returns the original creation response;
            # reload the mutable session before deciding whether to finish it.
            session = await roadmap.get_session(session["id"])
            if session["status"] == "active":
                session = await roadmap.update_session(
                    session["id"],
                    {
                        "status": "completed" if target == "done" else "partial",
                        "summary": decision["summary"] or f"Operator verdict: {target}",
                    },
                )

            card = await roadmap.get_card(card_id)
            if card is None:
                raise RuntimeError(f"missing roadmap card after session: {card_id}")
            evidence_status = "pass" if target == "done" else "info"
            body = "\n".join(
                part for part in (
                    decision["summary"],
                    f"Verification: {decision['verification']}" if decision["verification"] else "",
                ) if part
            )
            await roadmap.add_evidence(
                card_id,
                "operator",
                f"Operator acceptance {batch_id}: {target}",
                body=body,
                status=evidence_status,
                artifact_ref=artifact_ref,
                idempotency_key=batch_id,
            )
            if target == "done":
                for item in card["checklists"]:
                    if item["kind"] == "acceptance" and not item["checked"]:
                        await roadmap.update_checklist_item(item["id"], {"checked": True})

        # Move passing cards in dependency order without override. A stalled
        # set is a hard failure, not a reason to bypass the gate.
        pending = {
            cid for cid, decision in decisions.items()
            if decision["target_status"] == "done"
            and (await roadmap.get_card(cid))["status"] != "done"
        }
        while pending:
            progressed = False
            errors: dict[str, str] = {}
            for card_id in sorted(pending):
                card = await roadmap.get_card(card_id)
                try:
                    await roadmap.move(
                        card_id,
                        "done",
                        expected_status=card["status"],
                        reason=f"operator acceptance batch {batch_id}",
                    )
                except roadmap.RoadmapError as exc:
                    errors[card_id] = str(exc)
                    continue
                pending.remove(card_id)
                progressed = True
            if not progressed:
                raise RuntimeError(f"operator acceptance stalled: {errors}")

        for card_id, decision in decisions.items():
            if decision["target_status"] != "review":
                continue
            card = await roadmap.get_card(card_id)
            if card["status"] != "review":
                await roadmap.move(
                    card_id,
                    "review",
                    expected_status=card["status"],
                    reason=f"operator review batch {batch_id}",
                )

        snapshot = await roadmap.export_backlog()
        if args.export_path:
            args.export_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        integrity = await db.query_one("PRAGMA quick_check")
        statuses = await db.query(
            "SELECT status, COUNT(*) AS n FROM roadmap_cards GROUP BY status ORDER BY status"
        )
        print(json.dumps({
            "applied": applied,
            "cards": len(snapshot["cards"]),
            "statuses": statuses,
            "quick_check": integrity,
        }, ensure_ascii=False, indent=2))
    finally:
        await db.close()


def main() -> None:
    args = _args()
    home = args.home.expanduser().resolve()
    live_db = home / "institute.db"
    if not live_db.is_file():
        raise SystemExit(f"database not found: {live_db}")
    manifest = _load_manifest(args.manifest)
    if args.apply:
        _check_backup(args.backup, live_db)
    os.environ["INSTITUTE_HOME"] = str(home)
    asyncio.run(_run(args, manifest))


if __name__ == "__main__":
    main()
