"""racesテーブルを対象にbeforeinfoを過去日付までバックフィルするスクリプト。

    uv run python -m ml.fetchers.beforeinfo_backfill
    uv run python -m ml.fetchers.beforeinfo_backfill --start 2023-09-01 --end 2026-09-20

- races.race_date の新しい順（降順）に1レースずつ処理する。過去に遡るほど
  優先度が下がる想定のため、直近のデータから先に埋める。
- 実行前提: 停止中の状態を解除しBOATRACE振興会の利用許諾が得られたこと。
  CLAUDE.md「現在ステータス」を参照。
- 状態ファイル(JSON)にレースごとの結果を記録し、再実行時は完了済み/失敗済みの
  レースをスキップして続きから再開できる（--retry-failed で失敗分のみ再試行）。
- 「データがありません」等の1レースの失敗はバッチ全体を止めず記録して継続する。
- 「サーバー応答が遅くこれ自体がレート制御になる」という前提は誤りと判明した
  (2026-09-27)。curlでは実測8〜10秒/リクエストだが、本スクリプトが使う
  Python(urllib)経由では同一URL・同一内容が0.2〜0.3秒で返る。原因不明の
  ツール依存差でサーバー自体は速いため、明示的なsleepなしでは短時間に
  大量リクエストを送りつける形になり「大量アクセス」リスクを高める。
  そのため既定のsleepを**2.0秒**に変更した(--sleep-secondsで上書き可)。
- 長時間かかる前提のため、nohupでのバックグラウンド実行を想定している。
  標準出力はflushしながら1行ずつ進捗を出す。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ml.fetchers.beforeinfo import BeforeInfoFetchError, BeforeInfoNotAvailable, fetch_before_info
from ml.loaders.beforeinfo import LoaderError, load_before_info
from ml.loaders.db import get_connection

_ML_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STATE_PATH = _ML_ROOT / "data" / "beforeinfo_backfill_state.json"
DEFAULT_START = date(2023, 9, 1)
DEFAULT_END = date(2026, 9, 20)

_KNOWN_ERRORS = (BeforeInfoFetchError, LoaderError)


@dataclass(frozen=True)
class RaceTarget:
    race_id: int
    stadium_code: int
    race_no: int
    race_date: date


def _fetch_targets(conn, start: date, end: date) -> list[RaceTarget]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT races.id, stadiums.code, races.race_no, races.race_date
            FROM races
            JOIN stadiums ON stadiums.id = races.stadium_id
            WHERE races.race_date BETWEEN %s AND %s
            ORDER BY races.race_date DESC, races.race_no ASC, races.id ASC
            """,
            (start, end),
        )
        return [
            RaceTarget(race_id=row[0], stadium_code=row[1], race_no=row[2], race_date=row[3])
            for row in cur.fetchall()
        ]


def _load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def run_backfill(
    start: date = DEFAULT_START,
    end: date = DEFAULT_END,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    sleep_seconds: float = 2.0,
    retry_failed: bool = False,
    limit: int | None = None,
) -> dict:
    conn = get_connection()
    try:
        targets = _fetch_targets(conn, start, end)
    finally:
        conn.close()

    if limit is not None:
        targets = targets[:limit]

    state = _load_state(state_path)
    total = len(targets)
    started_at = time.monotonic()

    ok = failed = skipped = not_available = 0

    for i, target in enumerate(targets, start=1):
        key = str(target.race_id)
        prev = state.get(key, {}).get("status") if isinstance(state.get(key), dict) else None

        if prev == "done" or (prev in ("failed", "not_available") and not retry_failed):
            skipped += 1
            continue

        conn = get_connection()
        try:
            page = fetch_before_info(target.stadium_code, target.race_no, target.race_date)
            result = load_before_info(
                conn, target.stadium_code, target.race_no, target.race_date, page,
                source="backfill",
            )
            state[key] = {
                "status": "done",
                "race_date": target.race_date.isoformat(),
                "boats_upserted": result.boats_upserted,
            }
            ok += 1
            _print_progress(i, total, started_at, target, "OK", f"boats={result.boats_upserted}")
        except _KNOWN_ERRORS as exc:
            status = "not_available" if isinstance(exc, BeforeInfoNotAvailable) else "failed"
            state[key] = {
                "status": status,
                "race_date": target.race_date.isoformat(),
                "error": str(exc),
            }
            if status == "not_available":
                not_available += 1
            else:
                failed += 1
            _print_progress(i, total, started_at, target, status.upper(), str(exc), err=True)
        except Exception as exc:  # noqa: BLE001 - 1レースの想定外の失敗でバッチ全体を止めない
            state[key] = {
                "status": "failed",
                "race_date": target.race_date.isoformat(),
                "error": f"unexpected: {exc!r}",
            }
            failed += 1
            _print_progress(i, total, started_at, target, "FAILED", f"unexpected: {exc!r}", err=True)
        finally:
            conn.close()

        _save_state(state_path, state)

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    elapsed = time.monotonic() - started_at
    print(
        f"done: total={total} ok={ok} failed={failed} not_available={not_available} "
        f"skipped={skipped} elapsed={_format_duration(elapsed)}"
    )
    return {
        "total": total,
        "ok": ok,
        "failed": failed,
        "not_available": not_available,
        "skipped": skipped,
    }


def _print_progress(
    i: int, total: int, started_at: float, target: RaceTarget, status: str, detail: str, *, err: bool = False
) -> None:
    elapsed = time.monotonic() - started_at
    per_item = elapsed / i
    remaining = per_item * (total - i)
    line = (
        f"[{i}/{total}] jcd={target.stadium_code:02d} rno={target.race_no} "
        f"hd={target.race_date:%Y%m%d}: {status} {detail} "
        f"elapsed={_format_duration(elapsed)} remaining={_format_duration(remaining)}"
    )
    print(line, file=sys.stderr if err else sys.stdout, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=DEFAULT_START.isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--end", default=DEFAULT_END.isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_PATH))
    parser.add_argument("--sleep-seconds", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=None, help="対象レース数の上限（動作確認用）")
    parser.add_argument(
        "--retry-failed", action="store_true", help="前回failed/not_availableだったレースも再試行する"
    )
    args = parser.parse_args(argv)

    run_backfill(
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        state_path=Path(args.state_file),
        sleep_seconds=args.sleep_seconds,
        retry_failed=args.retry_failed,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
