"""特徴量生成 第6層: 番組表発表時点の公表体重と直前計量の実測体重の差分。

feature_version = 'v6_weight_diff'

第1段階の調査（2026-10-10）で新規性を確認済み:
- v1_basic.weight（番組表発表時点の公表体重）とv5_exhibition.exhibit_weight
  （直前計量の実測値、race_before_info.weight由来）は既存のどの特徴量層でも
  差分を取っていない。LightGBMは2列から差を学習できるが、木は単一特徴量で
  分割するため差分を明示した方が分割しやすいという想定で追加する。
- adjusted_weight（調整重量）は体重そのものではなく、クラス別の規定重量に
  満たない場合に追加されるハンデ用のおもりの重量であり、「体重の変化」とは
  別概念のため、本層ではweightのみを対象にする（adjusted_weightとの差分は
  作らない）。

リーク防止・source分岐はv5_exhibitionと完全に同じ（同じrace_before_info
由来のため）:
- source='live': captured_at <= cutoff_at を機械的に検証する。
- source='backfill': captured_atでの検証はスキップし、「beforeinfoは
  締切T-14〜16分に公開され締切後も内容が変わらない」というサイト挙動を
  根拠にする（詳細はCLAUDE.md「captured_at の意味とsource列の追加」参照）。

特徴量:
- weight_diff_from_program: exhibit_weight(直前計量の実測値) -
  weight(番組表発表時点の公表体重)。プラスなら番組表発表後に体重が増えた、
  マイナスなら減ったことを意味する。
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg
from psycopg.types.json import Jsonb

from ml.cutoff import CutoffViolationError, assert_no_leak
from ml.loaders.db import get_connection

FEATURE_VERSION = "v6_weight_diff"
CUTOFF_MARGIN = timedelta(minutes=10)
DEFAULT_BATCH_SIZE = 2000


class FeatureGenerationError(ValueError):
    """特徴量生成に失敗した場合に送出する。"""


@dataclass(frozen=True)
class FeatureGenerationResult:
    start_date: date
    end_date: date
    row_count: int
    upserted: int
    elapsed_seconds: float


# race_ids_clauseはstage2用(--race-ids)でのみ埋める。省略時(日付範囲のみ、
# バックフィル・過去分の一括生成で使う既存挙動)は空文字のまま変化なし。
_SELECT_SQL = """
    SELECT
        re.race_id, re.lane, r.deadline_at,
        re.weight AS program_weight,
        rbi.weight AS exhibit_weight,
        rbi.captured_at, rbi.source
    FROM race_entries re
    JOIN races r ON r.id = re.race_id
    JOIN race_before_info rbi ON rbi.race_id = re.race_id AND rbi.lane = re.lane
    WHERE r.race_date BETWEEN %(start_date)s AND %(end_date)s
    {race_ids_clause}
    ORDER BY re.race_id, re.lane
"""


def _build_payloads(rows: list[tuple]) -> list[tuple[int, int, object, dict]]:
    results: list[tuple[int, int, object, dict]] = []

    for row in rows:
        race_id, lane, deadline_at, program_weight, exhibit_weight, captured_at, source = row
        cutoff_at = deadline_at - CUTOFF_MARGIN

        if source == "live":
            try:
                assert_no_leak(
                    [{"captured_at": captured_at}],
                    cutoff_at=cutoff_at,
                    time_field="captured_at",
                    label=f"race_before_info(race_id={race_id}, lane={lane})",
                )
            except CutoffViolationError as exc:
                raise FeatureGenerationError(str(exc)) from exc
        elif source == "backfill":
            pass
        else:
            raise FeatureGenerationError(
                f"race_before_info(race_id={race_id}, lane={lane}): "
                f"unknown source={source!r} (expected 'live' or 'backfill')"
            )

        weight_diff = (
            float(exhibit_weight) - float(program_weight)
            if exhibit_weight is not None and program_weight is not None
            else None
        )
        payload = {"weight_diff_from_program": weight_diff}

        results.append((race_id, lane, cutoff_at, payload))

    return results


def _bulk_upsert(cur: psycopg.Cursor, batch: list[tuple]) -> None:
    if not batch:
        return
    values_sql = ", ".join(["(%s, %s, %s, %s, %s, now(), now())"] * len(batch))
    params: list[object] = []
    for race_id, lane, cutoff_at, payload in batch:
        params.extend([race_id, lane, FEATURE_VERSION, cutoff_at, Jsonb(payload)])

    cur.execute(
        f"""
        INSERT INTO features (race_id, lane, feature_version, cutoff_at, payload,
                               created_at, updated_at)
        VALUES {values_sql}
        ON CONFLICT (race_id, lane, feature_version) DO UPDATE SET
            cutoff_at = EXCLUDED.cutoff_at,
            payload = EXCLUDED.payload,
            updated_at = now()
        """,
        params,
    )


def generate_weight_diff_features_range(
    conn: psycopg.Connection,
    start_date: date,
    end_date: date,
    *,
    race_ids: list[int] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: bool = False,
) -> FeatureGenerationResult:
    """race_ids を指定すると、日付範囲の中でもそのrace_idだけに絞る
    (stage2用。省略時は日付範囲の全レースが対象で、既存のバックフィル/
    過去分一括生成の挙動と完全に同じ)。
    """
    t0 = time.time()

    params: dict[str, object] = {"start_date": start_date, "end_date": end_date}
    race_ids_clause = ""
    if race_ids:
        race_ids_clause = "AND re.race_id = ANY(%(race_ids)s)"
        params["race_ids"] = list(race_ids)
    sql = _SELECT_SQL.format(race_ids_clause=race_ids_clause)

    with conn.cursor() as read_cur:
        read_cur.execute(sql, params)
        raw_rows = read_cur.fetchall()

    if progress:
        print(f"  fetched {len(raw_rows)} rows ({time.time() - t0:.1f}s)", flush=True)

    payloads = _build_payloads(raw_rows)

    batch: list[tuple] = []
    upserted = 0

    with conn.cursor() as write_cur:
        for race_id, lane, cutoff_at, payload in payloads:
            batch.append((race_id, lane, cutoff_at, payload))

            if len(batch) >= batch_size:
                _bulk_upsert(write_cur, batch)
                conn.commit()
                upserted += len(batch)
                batch.clear()
                if progress:
                    print(
                        f"  progress: {upserted} rows upserted ({time.time() - t0:.1f}s)",
                        flush=True,
                    )

        if batch:
            _bulk_upsert(write_cur, batch)
            conn.commit()
            upserted += len(batch)

    elapsed = time.time() - t0
    return FeatureGenerationResult(
        start_date=start_date,
        end_date=end_date,
        row_count=len(payloads),
        upserted=upserted,
        elapsed_seconds=elapsed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start", help="YYYY-MM-DD")
    parser.add_argument(
        "end", nargs="?", default=None, help="YYYY-MM-DD（省略時はstartと同じ=1日分）"
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--show", type=int, default=3)
    parser.add_argument(
        "--race-ids",
        type=int,
        nargs="+",
        default=None,
        help="指定したrace_idのみ対象にする(省略時は日付範囲の全レース)。stage2用",
    )
    args = parser.parse_args(argv)

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end) if args.end else start_date

    conn = get_connection()
    try:
        result = generate_weight_diff_features_range(
            conn,
            start_date,
            end_date,
            race_ids=args.race_ids,
            batch_size=args.batch_size,
            progress=args.progress,
        )
        print(
            f"{result.start_date}..{result.end_date}: rows={result.row_count} "
            f"upserted={result.upserted} elapsed={result.elapsed_seconds:.1f}s "
            f"feature_version={FEATURE_VERSION}"
        )

        if args.show:
            import json

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT race_id, lane, cutoff_at, payload
                    FROM features f
                    JOIN races r ON r.id = f.race_id
                    WHERE r.race_date BETWEEN %s AND %s AND f.feature_version = %s
                    ORDER BY race_id, lane
                    LIMIT %s
                    """,
                    (start_date, end_date, FEATURE_VERSION, args.show),
                )
                for race_id, lane, cutoff_at, payload in cur.fetchall():
                    print(f"--- race_id={race_id} lane={lane} cutoff_at={cutoff_at} ---")
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
