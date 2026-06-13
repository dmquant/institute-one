#!/usr/bin/env python3
"""Export institute-one candidates into judgement_engine staging."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.institute import judgement_bridge  # noqa: E402


async def _amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="", help="work date YYYY-MM-DD; default is current institute work date")
    parser.add_argument("--output-dir", default="", help="override judgement_engine staging output dir")
    parser.add_argument("--max-claims", type=int, default=40)
    args = parser.parse_args()

    await db.init()
    try:
        result = await judgement_bridge.build_review_queue(
            date=args.date or None,
            output_dir=Path(args.output_dir).expanduser() if args.output_dir else None,
            max_claims=args.max_claims,
        )
    finally:
        await db.close()

    print(
        f"path={result.path}\n"
        f"topics={result.topics}\n"
        f"claims={result.claims}\n"
        f"verdict_counts={result.verdict_counts}"
    )


if __name__ == "__main__":
    asyncio.run(_amain())
