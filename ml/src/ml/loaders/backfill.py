"""B/Kファイルを日付範囲で順次投入するバックフィルスクリプト。

    uv run python -m ml.loaders.backfill 2023-09-01 2026-09-09

- 1日ごとにload-day相当（racer_daily_snapshots -> compact-periods ->
  races/race_entries -> race_results）を実行し、1〜2秒のsleepを挟む。
- 状態ファイル(JSON)に日付ごとの結果を記録し、再実行時は完了済み/失敗済みの
  日付をスキップして続きから再開できる（--retry-failed で失敗分のみ再試行）。
- 開催がない日・ファイル欠損等での失敗は記録した上で処理を継続する
  （1日の失敗でバッチ全体を止めない）。
- 進捗は標準出力に1行ずつ表示する。

--defer-compaction: 増分でcompact-periodsを回すと、後から過去日付を追加
した際に既存のrace_entries参照済みracer_periodsと境界が食い違い、
自動解決できない衝突（部分重複）が発生しうる。このオプションを付けると
compact-periodsとracer_period_idの解決をスキップして投入だけを行うため、
衝突は起こらない。全期間の投入が終わったら、別途
`compact-periods --rebuild` で一括再構築し、race_entries.racer_period_id
をrace_dateに基づいて貼り直すこと（本ファイルは貼り直し処理を含まない）。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from ml.fetchers.mbrace import FetchError, fetch_and_extract
from ml.loaders.db import get_connection
from ml.loaders.racer_periods import CompactionError, compact_racer_periods
from ml.loaders.racer_snapshots import upsert_daily_snapshots
from ml.loaders.races import LoaderError as RaceLoaderError
from ml.loaders.races import upsert_races_and_entries
from ml.loaders.results import LoaderError as ResultLoaderError
from ml.loaders.results import upsert_race_results
from ml.parsers.program import ProgramParseError, parse_program_path
from ml.parsers.result import ResultParseError, parse_result_path

_ML_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STATE_PATH = _ML_ROOT / "data" / "backfill_state.json"
DEFAULT_DATA_DIR = _ML_ROOT / "data" / "raw"

_KNOWN_ERRORS = (
    FetchError,
    ProgramParseError,
    ResultParseError,
    CompactionError,
    RaceLoaderError,
    ResultLoaderError,
)


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    tmp.replace(path)


def _load_one_day(conn, d: date, data_dir: Path, *, defer_compaction: bool = False) -> dict:
    b_path = fetch_and_extract("B", d, data_dir)
    k_path = fetch_and_extract("K", d, data_dir)

    program = parse_program_path(b_path)
    snapshot_result = upsert_daily_snapshots(conn, program.entries, source_file=b_path.name)

    if defer_compaction:
        # racer_periodsの期間衝突を増分バックフィル中に起こさないため、compactionを
        # 一旦スキップする。race_entries.racer_period_idはNULLのまま投入し、全日程の
        # 投入完了後に一括で(compact-periods --rebuild →貼り直し)確定させる。
        compaction_warnings = 0
        compaction_deleted = 0
    else:
        compaction = compact_racer_periods(conn, racer_ids=snapshot_result.racer_ids)
        compaction_warnings = len(compaction.warnings)
        compaction_deleted = compaction.deleted

    race_result = upsert_races_and_entries(
        conn, program, require_racer_period=not defer_compaction
    )

    parsed_result = parse_result_path(k_path)
    result_load = upsert_race_results(conn, parsed_result)

    return {
        "races": race_result.races_upserted,
        "entries": race_result.entries_upserted,
        "results": result_load.results_upserted,
        "snapshot_warnings": len(snapshot_result.warnings),
        "compaction_warnings": compaction_warnings,
        "compaction_deleted": compaction_deleted,
    }


def run_backfill(
    start: date,
    end: date,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    data_dir: Path = DEFAULT_DATA_DIR,
    sleep_min: float = 1.0,
    sleep_max: float = 2.0,
    retry_failed: bool = False,
    defer_compaction: bool = False,
) -> dict:
    state = _load_state(state_path)
    all_dates = list(_daterange(start, end))
    total = len(all_dates)

    ok = failed = skipped = 0

    for i, d in enumerate(all_dates, start=1):
        key = d.isoformat()
        prev = state.get(key, {}).get("status") if isinstance(state.get(key), dict) else None

        if prev == "done" or (prev == "failed" and not retry_failed):
            skipped += 1
            print(f"[{i}/{total}] {key}: skip (already {prev})")
            continue

        conn = get_connection()
        try:
            stats = _load_one_day(conn, d, data_dir, defer_compaction=defer_compaction)
            state[key] = {"status": "done", **stats}
            _save_state(state_path, state)
            ok += 1
            deleted_note = (
                f" compaction_deleted={stats['compaction_deleted']}"
                if stats["compaction_deleted"]
                else ""
            )
            print(
                f"[{i}/{total}] {key}: OK "
                f"races={stats['races']} entries={stats['entries']} results={stats['results']}"
                f"{deleted_note}"
            )
        except _KNOWN_ERRORS as exc:
            state[key] = {"status": "failed", "error": str(exc)}
            _save_state(state_path, state)
            failed += 1
            print(f"[{i}/{total}] {key}: FAILED ({exc})", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - 1日の想定外の失敗でバッチ全体を止めない
            state[key] = {"status": "failed", "error": f"unexpected: {exc!r}"}
            _save_state(state_path, state)
            failed += 1
            print(f"[{i}/{total}] {key}: FAILED (unexpected: {exc!r})", file=sys.stderr)
        finally:
            conn.close()

        time.sleep(random.uniform(sleep_min, sleep_max))

    print(f"done: total={total} ok={ok} failed={failed} skipped={skipped}")
    return {"total": total, "ok": ok, "failed": failed, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start", help="YYYY-MM-DD")
    parser.add_argument("end", help="YYYY-MM-DD")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--sleep-min", type=float, default=1.0)
    parser.add_argument("--sleep-max", type=float, default=2.0)
    parser.add_argument(
        "--retry-failed", action="store_true", help="前回failedだった日付も再試行する"
    )
    parser.add_argument(
        "--defer-compaction",
        action="store_true",
        help=(
            "racer_daily_snapshots/races/race_entries/race_resultsの投入のみ行い、"
            "compact-periodsを実行しない（race_entries.racer_period_idはNULLのまま）。"
            "全期間投入後に compact-periods --rebuild で一括再構築する運用を想定。"
        ),
    )
    args = parser.parse_args(argv)

    run_backfill(
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        state_path=Path(args.state_file),
        data_dir=Path(args.data_dir),
        sleep_min=args.sleep_min,
        sleep_max=args.sleep_max,
        retry_failed=args.retry_failed,
        defer_compaction=args.defer_compaction,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
