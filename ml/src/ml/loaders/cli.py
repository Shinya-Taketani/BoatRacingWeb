"""racer_daily_snapshots / racer_periods / races / race_entries / race_results 投入用CLI。

使い方:
    uv run python -m ml.loaders.cli load-program data/raw/B260916.TXT
    uv run python -m ml.loaders.cli compact-periods
    uv run python -m ml.loaders.cli compact-periods --rebuild
    uv run python -m ml.loaders.cli load-day 2026-09-16

load-day は依存順（racer_daily_snapshots -> compact-periods ->
races/race_entries -> race_results）を守って一括投入する。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from ml.fetchers.mbrace import FetchError, fetch_and_extract
from ml.loaders.db import get_connection
from ml.loaders.races import LoaderError as RaceLoaderError
from ml.loaders.races import upsert_races_and_entries
from ml.loaders.racer_periods import CompactionError, compact_racer_periods
from ml.loaders.racer_snapshots import upsert_daily_snapshots
from ml.loaders.results import LoaderError as ResultLoaderError
from ml.loaders.results import upsert_race_results
from ml.parsers.program import ProgramParseError, parse_program_path
from ml.parsers.result import ResultParseError, parse_result_path

_DATA_RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw"


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
        f"inserted={summary.inserted} updated={summary.updated} deleted={summary.deleted}"
    )
    if summary.warnings:
        print(f"{len(summary.warnings)} warning(s):")
        for w in summary.warnings:
            print(f"  - {w}")
    return 0


def _load_program_pipeline(conn, b_path: Path):
    """racer_daily_snapshots -> compact-periods -> races/race_entries。

    Bファイルのみで完結する（Kファイル・結果は不要）。当日朝の時点では
    まだ結果が存在しないため、レース一覧だけを取り込みたい場合に使う。
    """
    program = parse_program_path(b_path)

    snapshot_result = upsert_daily_snapshots(conn, program.entries, source_file=b_path.name)
    print(f"racer_daily_snapshots: upserted {snapshot_result.upserted}")
    for w in snapshot_result.warnings:
        print(f"  warning: {w}")

    compaction = compact_racer_periods(conn, racer_ids=snapshot_result.racer_ids)
    print(
        f"racer_periods: processed {compaction.racers_processed} racer(s), "
        f"inserted={compaction.inserted} updated={compaction.updated} deleted={compaction.deleted}"
    )
    for w in compaction.warnings:
        print(f"  warning: {w}")

    race_result = upsert_races_and_entries(conn, program)
    print(
        f"races/race_entries: races={race_result.races_upserted} "
        f"entries={race_result.entries_upserted}"
    )


def _cmd_load_races(args: argparse.Namespace) -> int:
    d = date.fromisoformat(args.date)
    b_path = fetch_and_extract("B", d, _DATA_RAW_DIR, force=args.force)
    print(f"fetched {b_path.name}")

    conn = get_connection()
    try:
        _load_program_pipeline(conn, b_path)
    finally:
        conn.close()

    return 0


def _cmd_load_day(args: argparse.Namespace) -> int:
    d = date.fromisoformat(args.date)

    b_path = fetch_and_extract("B", d, _DATA_RAW_DIR, force=args.force)
    k_path = fetch_and_extract("K", d, _DATA_RAW_DIR, force=args.force)
    print(f"fetched {b_path.name}, {k_path.name}")

    conn = get_connection()
    try:
        _load_program_pipeline(conn, b_path)

        parsed_result = parse_result_path(k_path)
        result_load = upsert_race_results(conn, parsed_result)
        print(f"race_results: upserted {result_load.results_upserted}")
    finally:
        conn.close()

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

    p_races = sub.add_parser(
        "load-races",
        help="指定日のBのみ取得・パースし、races/race_entriesまで投入（K不要、朝のレース一覧取得用）",
    )
    p_races.add_argument("date", help="YYYY-MM-DD")
    p_races.add_argument(
        "--force", action="store_true", help="既に展開済みでも再ダウンロードする"
    )
    p_races.set_defaults(func=_cmd_load_races)

    p_day = sub.add_parser(
        "load-day", help="指定日のB/Kを取得・パースし、依存順で一括投入"
    )
    p_day.add_argument("date", help="YYYY-MM-DD")
    p_day.add_argument(
        "--force", action="store_true", help="既に展開済みでも再ダウンロードする"
    )
    p_day.set_defaults(func=_cmd_load_day)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        ProgramParseError,
        ResultParseError,
        CompactionError,
        RaceLoaderError,
        ResultLoaderError,
        FetchError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
