"""特徴量生成 第3層: レース内での相対値。feature_version = 'v3_relative'。

v1_basic / v2_recent の payload に格納済みの値だけから計算できるため、
新たなDB参照（racer_periods等）は不要。同一レースの6艇間の順位・偏差・
最上位との差・レース全体の性質を求めるだけであり、締切前に確定している
値のみを使っているv1/v2の上に乗る計算なので新たなリークは生じない。

CLAUDE.md ルール1（リーク防止）:
- v1/v2それぞれのcutoff_atが一致することを確認した上でJOINする
  （異なる配信時点のデータが混ざっていないことの保証）。
- 生成後 assert_no_leak() で検証する。
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import date, timedelta

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from ml.cutoff import assert_no_leak
from ml.loaders.db import get_connection

FEATURE_VERSION = "v3_relative"
CUTOFF_MARGIN = timedelta(minutes=10)
DEFAULT_BATCH_SIZE = 3000

_SELECT_SQL = """
    SELECT
        f1.race_id, f1.lane, r.deadline_at,
        f1.cutoff_at AS cutoff_at_v1, f2.cutoff_at AS cutoff_at_v2,
        (f1.payload->>'national_win_rate')::float8 AS national_win_rate,
        (f1.payload->>'local_win_rate')::float8 AS local_win_rate,
        (f1.payload->>'racer_class')::int AS racer_class,
        (f2.payload->>'lane_win_rate_recent50')::float8 AS lane_win_rate_recent50,
        (f2.payload->>'win_rate_recent10')::float8 AS win_rate_recent10
    FROM features f1
    JOIN features f2
        ON f2.race_id = f1.race_id AND f2.lane = f1.lane AND f2.feature_version = 'v2_recent'
    JOIN races r ON r.id = f1.race_id
    WHERE f1.feature_version = 'v1_basic' AND r.race_date BETWEEN %(start_date)s AND %(end_date)s
    ORDER BY f1.race_id, f1.lane
"""


class FeatureGenerationError(ValueError):
    """特徴量生成に失敗した場合に送出する。"""


@dataclass(frozen=True)
class FeatureGenerationResult:
    start_date: date
    end_date: date
    row_count: int
    upserted: int
    elapsed_seconds: float


def _fetch_base_df(conn: psycopg.Connection, start_date: date, end_date: date) -> pl.DataFrame:
    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL, {"start_date": start_date, "end_date": end_date})
        rows = cur.fetchall()

    if not rows:
        return pl.DataFrame()

    return pl.DataFrame(
        rows,
        schema=[
            "race_id", "lane", "deadline_at", "cutoff_at_v1", "cutoff_at_v2",
            "national_win_rate", "local_win_rate", "racer_class",
            "lane_win_rate_recent50", "win_rate_recent10",
        ],
        orient="row",
    )


def _compute_relative(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        # 1. レース内順位（1=最も高い値。nullは順位も自動的にnullになる）
        pl.col("national_win_rate").rank(method="ordinal", descending=True).over("race_id")
        .alias("national_win_rate_rank"),
        pl.col("local_win_rate").rank(method="ordinal", descending=True).over("race_id")
        .alias("local_win_rate_rank"),
        pl.col("lane_win_rate_recent50").rank(method="ordinal", descending=True).over("race_id")
        .alias("lane_win_rate_recent50_rank"),
        pl.col("win_rate_recent10").rank(method="ordinal", descending=True).over("race_id")
        .alias("win_rate_recent10_rank"),
        # 2. レース内平均との偏差
        (pl.col("national_win_rate") - pl.col("national_win_rate").mean().over("race_id"))
        .alias("national_win_rate_dev"),
        (pl.col("win_rate_recent10") - pl.col("win_rate_recent10").mean().over("race_id"))
        .alias("win_rate_recent10_dev"),
        # 3. レース内最大値との差（トップは0、他は負値）
        (pl.col("national_win_rate") - pl.col("national_win_rate").max().over("race_id"))
        .alias("national_win_rate_gap_from_max"),
        # 4. レース全体の性質（6艇とも同じ値になる）
        pl.col("national_win_rate").std().over("race_id").alias("national_win_rate_std_in_race"),
        (pl.col("racer_class") == 1).sum().over("race_id").alias("a1_count_in_race"),
    )


def generate_relative_features_range(
    conn: psycopg.Connection,
    start_date: date,
    end_date: date,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: bool = False,
) -> FeatureGenerationResult:
    t0 = time.time()

    base_df = _fetch_base_df(conn, start_date, end_date)
    if progress:
        print(f"  fetched {base_df.height} rows ({time.time() - t0:.1f}s)", flush=True)

    if base_df.height == 0:
        return FeatureGenerationResult(start_date, end_date, 0, 0, time.time() - t0)

    # v1とv2が別々の配信時点を指していないか確認（本来一致するはず）。
    mismatched = base_df.filter(pl.col("cutoff_at_v1") != pl.col("cutoff_at_v2")).height
    if mismatched:
        raise FeatureGenerationError(
            f"{mismatched} 行で v1_basic と v2_recent の cutoff_at が一致しません。"
            "配信時点の異なるデータが混ざっている可能性があります。"
        )

    relative_df = _compute_relative(base_df)

    # 生成後のリーク検証: このレースのcutoff_at(=v1/v2と同一)が
    # races.deadline_at - 10分と一致していることを行ごとに確認する。
    for row in relative_df.select(
        "race_id", "lane", "deadline_at", "cutoff_at_v1"
    ).iter_rows(named=True):
        expected_cutoff = row["deadline_at"] - CUTOFF_MARGIN
        # 等号(ts == cutoff_at、通常はこちらのはず)はassert_no_leak内で許容される
        # （raiseされるのは ts > cutoff_at のときのみ）。
        assert_no_leak(
            [{"cutoff_at": row["cutoff_at_v1"]}],
            cutoff_at=expected_cutoff,
            time_field="cutoff_at",
            label=f"v3_relative(race_id={row['race_id']}, lane={row['lane']})",
        )

    payload_columns = [
        "national_win_rate_rank",
        "local_win_rate_rank",
        "lane_win_rate_recent50_rank",
        "win_rate_recent10_rank",
        "national_win_rate_dev",
        "win_rate_recent10_dev",
        "national_win_rate_gap_from_max",
        "national_win_rate_std_in_race",
        "a1_count_in_race",
    ]

    row_count = 0
    upserted = 0
    batch: list[tuple] = []

    with conn.cursor() as write_cur:
        for row in relative_df.iter_rows(named=True):
            payload = {c: (None if row[c] is None else _to_native(row[c])) for c in payload_columns}
            cutoff_at = row["cutoff_at_v1"]
            batch.append((row["race_id"], row["lane"], cutoff_at, payload))
            row_count += 1

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
        row_count=row_count,
        upserted=upserted,
        elapsed_seconds=elapsed,
    )


def _to_native(value):
    # polarsのrank()はUInt32を返すため、json化できるようPythonのintに落とす。
    if hasattr(value, "item"):
        return value.item()
    return value


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start", help="YYYY-MM-DD")
    parser.add_argument(
        "end", nargs="?", default=None, help="YYYY-MM-DD（省略時はstartと同じ=1日分）"
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--show", type=int, default=3)
    args = parser.parse_args(argv)

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end) if args.end else start_date

    conn = get_connection()
    try:
        result = generate_relative_features_range(
            conn, start_date, end_date, batch_size=args.batch_size, progress=args.progress
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
