"""racer_daily_snapshots / racer_periods 投入用CLI。

使い方:
    uv run python -m ml.loaders.cli load-program data/raw/B260916.TXT
    uv run python -m ml.loaders.cli compact-periods
    uv run python -m ml.loaders.cli compact-periods --rebuild
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ml.loaders.db import get_connection
from ml.loaders.racer_periods import CompactionError, compact_racer_periods
from ml.loaders.racer_snapshots import upsert_daily_snapshots
from ml.parsers.program import ProgramParseError, parse_program_path


def _cmd_load_program(args: argparse.Namespace) -> int:
    path = Path(args.path)
    parsed = parse_program_path(path)

    conn = get_connection()
    try:
        result = upsert_daily_snapshots(conn, parsed.entries, source_file=path.name)
    finally:
        conn.close()

    print(f"upserted {result.upserted} racer_daily_snapshots rows from {path.name}")
    if result.warnings:
        print(f"{len(result.warnings)} warning(s):")
        for w in result.warnings:
            print(f"  - {w}")
    return 0


def _cmd_compact_periods(args: argparse.Namespace) -> int:
    racer_ids = [int(x) for x in args.racer_id] if args.racer_id else None

    conn = get_connection()
    try:
        summary = compact_racer_periods(conn, racer_ids=racer_ids, rebuild=args.rebuild)
    finally:
        conn.close()

    print(
        f"processed {summary.racers_processed} racer(s): "
        f"inserted={summary.inserted} updated={summary.updated}"
    )
    if summary.warnings:
        print(f"{len(summary.warnings)} warning(s):")
        for w in summary.warnings:
            print(f"  - {w}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="ml.loaders.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load-program", help="番組表(B)ファイルをracer_daily_snapshotsへupsert")
    p_load.add_argument("path", help="B*.TXT のパス")
    p_load.set_defaults(func=_cmd_load_program)

    p_compact = sub.add_parser("compact-periods", help="racer_daily_snapshots -> racer_periods")
    p_compact.add_argument(
        "--racer-id", action="append", help="対象racerを絞り込む（省略時は全racer）"
    )
    p_compact.add_argument(
        "--rebuild", action="store_true", help="対象racerのracer_periodsを全削除してから再構築"
    )
    p_compact.set_defaults(func=_cmd_compact_periods)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ProgramParseError, CompactionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
