"""ベースラインモデル: features(v1_basic) + race_results から学習/検証データを構築する。

CLAUDE.md ルール2（時系列split）:
- 学習: 2023-09-01〜2025-12-31
- 検証: 2026-01-01〜2026-09-17
ランダムKFold等の時系列を無視した分割は行わない。

目的変数 is_winner は finish_pos = 1 なら 1、それ以外（2着以下・失格等で
finish_pos が NULL の場合を含む）は 0。

このファイルはまずダミーモデル（常に lane=1 を1着と予測）の的中率を出す
だけの段階。学習器（LightGBM等）は別途実装する。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date

import polars as pl
import psycopg

from ml.features.basic import FEATURE_VERSION
from ml.loaders.db import get_connection

TRAIN_START = date(2023, 9, 1)
TRAIN_END = date(2025, 12, 31)
VAL_START = date(2026, 1, 1)
VAL_END = date(2026, 9, 17)

_SELECT_SQL = """
    SELECT f.race_id, f.lane, r.race_date, f.payload, rr.finish_pos
    FROM features f
    JOIN races r ON r.id = f.race_id
    JOIN race_entries re ON re.race_id = f.race_id AND re.lane = f.lane
    LEFT JOIN race_results rr ON rr.race_entry_id = re.id
    WHERE f.feature_version = %s AND r.race_date BETWEEN %s AND %s
    ORDER BY f.race_id, f.lane
"""


@dataclass(frozen=True)
class SplitSummary:
    name: str
    start: date
    end: date
    race_count: int
    row_count: int


def fetch_dataset(conn: psycopg.Connection, start: date, end: date) -> pl.DataFrame:
    """features(payload) を展開し、finish_pos / is_winner を付けた DataFrame を返す。"""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL, (FEATURE_VERSION, start, end))
        rows = cur.fetchall()

    records = []
    for race_id, lane, race_date, payload, finish_pos in rows:
        record = dict(payload)  # racer_class, national_win_rate, ... lane, stadium_id 等
        record["race_id"] = race_id
        record["lane"] = lane
        record["race_date"] = race_date
        record["finish_pos"] = finish_pos
        record["is_winner"] = 1 if finish_pos == 1 else 0
        records.append(record)

    return pl.DataFrame(records)


def summarize(name: str, start: date, end: date, df: pl.DataFrame) -> SplitSummary:
    race_count = df.select(pl.col("race_id").n_unique()).item() if df.height else 0
    return SplitSummary(name=name, start=start, end=end, race_count=race_count, row_count=df.height)


def dummy_lane1_hit_rate(df: pl.DataFrame) -> float:
    """常に lane=1 を1着と予測した場合の、レース単位の的中率。

    レースごとに lane=1 の is_winner を見て、1(=実際に1着)なら的中。
    lane=1 の結果が取れないレース（通常は起きないはずだが防御的に）は不的中として数える。
    """
    total_races = df.select(pl.col("race_id").n_unique()).item()
    if total_races == 0:
        return float("nan")

    hits = (
        df.filter(pl.col("lane") == 1)
        .select(pl.col("is_winner").sum())
        .item()
    )
    return hits / total_races


def lane_win_rates(df: pl.DataFrame) -> pl.DataFrame:
    return (
        df.group_by("lane")
        .agg(
            pl.len().alias("entries"),
            pl.col("is_winner").mean().alias("win_rate"),
        )
        .sort("lane")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    conn = get_connection()
    try:
        train_df = fetch_dataset(conn, TRAIN_START, TRAIN_END)
        val_df = fetch_dataset(conn, VAL_START, VAL_END)
    finally:
        conn.close()

    train_summary = summarize("train", TRAIN_START, TRAIN_END, train_df)
    val_summary = summarize("val", VAL_START, VAL_END, val_df)

    for s in (train_summary, val_summary):
        print(f"{s.name} ({s.start}..{s.end}): races={s.race_count} rows={s.row_count}")

    hit_rate = dummy_lane1_hit_rate(val_df)
    print(
        f"\nダミーモデル（常にlane=1が1着と予測）の検証期間 的中率: "
        f"{hit_rate:.4f} ({hit_rate * 100:.2f}%)"
    )

    print("\n検証期間 lane別 1着率:")
    for row in lane_win_rates(val_df).iter_rows(named=True):
        print(
            f"  lane={row['lane']}: entries={row['entries']} "
            f"win_rate={row['win_rate']:.4f} ({row['win_rate'] * 100:.2f}%)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
