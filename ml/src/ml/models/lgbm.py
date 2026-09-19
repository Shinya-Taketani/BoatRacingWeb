"""LightGBM二値分類（素の状態、チューニングなし）。

baseline.py と同じ時系列split（学習 2023-09-01〜2025-12-31 / 検証
2026-01-01〜2026-09-17）を使う。目的変数は is_winner（finish_pos=1 なら1）。

v1_basic（番組表の生の値）のみのモデルと、v1_basic + v2_recent（直近成績・
枠番/進入傾向・場別適性・モーター直近調子・節の進行度）を結合したモデルを
同じsplit・同じパラメータで学習し、並べて比較する。

stadium_id のみ categorical_feature として扱う。欠損値は0埋めせず NaN の
まま LightGBM に渡す（LightGBMはNaNをネイティブに扱えるため、0で埋めると
「勝率0%」のような偽の情報になってしまう）。

予測確率はレース単位で合計1になるよう正規化し、最大確率の艇を1着予測とする
（6艇のうち必ず1艇が1着になる、という制約をここで明示的に反映する）。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import lightgbm as lgb
import numpy as np
import polars as pl
import psycopg

from ml.loaders.db import get_connection
from ml.models.baseline import (
    TRAIN_END,
    TRAIN_START,
    VAL_END,
    VAL_START,
    dummy_lane1_hit_rate,
    fetch_dataset,
)

V1_FEATURE_COLUMNS = [
    "racer_class",
    "national_win_rate",
    "national_win_rate_2",
    "local_win_rate",
    "local_win_rate_2",
    "motor_win_rate_2",
    "boat_win_rate_2",
    "age",
    "weight",
    "lane",
    "stadium_id",
    "race_no",
    "distance_m",
]
V2_FEATURE_COLUMNS = [
    "avg_finish_pos_recent10",
    "win_rate_recent10",
    "place2_rate_recent10",
    "place3_rate_recent10",
    "avg_st_recent10",
    "lane_win_rate_recent50",
    "lane_avg_start_course_recent50",
    "maeduke_rate_recent50",
    "stadium_win_rate_recent30",
    "motor_place2_rate_recent20",
    "day_of_meet",
    "maeduke_count_in_race",
]
V3_FEATURE_COLUMNS = [
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
COMBINED_FEATURE_COLUMNS = V1_FEATURE_COLUMNS + V2_FEATURE_COLUMNS
ALL_FEATURE_COLUMNS = V1_FEATURE_COLUMNS + V2_FEATURE_COLUMNS + V3_FEATURE_COLUMNS
CATEGORICAL_FEATURES = ["stadium_id"]
TARGET_COLUMN = "is_winner"

DEFAULT_PARAMS = {
    "objective": "binary",
    "metric": "binary_logloss",
    "verbosity": -1,
    "seed": 0,
}
DEFAULT_NUM_BOOST_ROUND = 200

_COMBINED_SELECT_SQL = """
    SELECT f1.race_id, f1.lane, f1.payload AS payload_v1, f2.payload AS payload_v2,
           rr.finish_pos
    FROM features f1
    JOIN features f2
        ON f2.race_id = f1.race_id AND f2.lane = f1.lane AND f2.feature_version = 'v2_recent'
    JOIN races r ON r.id = f1.race_id
    JOIN race_entries re ON re.race_id = f1.race_id AND re.lane = f1.lane
    LEFT JOIN race_results rr ON rr.race_entry_id = re.id
    WHERE f1.feature_version = 'v1_basic' AND r.race_date BETWEEN %s AND %s
    ORDER BY f1.race_id, f1.lane
"""

_ALL_SELECT_SQL = """
    SELECT f1.race_id, f1.lane, f1.payload AS payload_v1, f2.payload AS payload_v2,
           f3.payload AS payload_v3, rr.finish_pos
    FROM features f1
    JOIN features f2
        ON f2.race_id = f1.race_id AND f2.lane = f1.lane AND f2.feature_version = 'v2_recent'
    JOIN features f3
        ON f3.race_id = f1.race_id AND f3.lane = f1.lane AND f3.feature_version = 'v3_relative'
    JOIN races r ON r.id = f1.race_id
    JOIN race_entries re ON re.race_id = f1.race_id AND re.lane = f1.lane
    LEFT JOIN race_results rr ON rr.race_entry_id = re.id
    WHERE f1.feature_version = 'v1_basic' AND r.race_date BETWEEN %s AND %s
    ORDER BY f1.race_id, f1.lane
"""


def fetch_combined_dataset(conn: psycopg.Connection, start, end) -> pl.DataFrame:
    """v1_basic と v2_recent の payload を (race_id, lane) でマージしたDataFrameを返す。"""
    with conn.cursor() as cur:
        cur.execute(_COMBINED_SELECT_SQL, (start, end))
        rows = cur.fetchall()

    records = []
    for race_id, lane, payload_v1, payload_v2, finish_pos in rows:
        record = {**payload_v1, **payload_v2}
        record["race_id"] = race_id
        record["lane"] = lane
        record["finish_pos"] = finish_pos
        record["is_winner"] = 1 if finish_pos == 1 else 0
        records.append(record)

    return pl.DataFrame(records)


def fetch_all_dataset(conn: psycopg.Connection, start, end) -> pl.DataFrame:
    """v1_basic + v2_recent + v3_relative の payload を (race_id, lane) でマージする。"""
    with conn.cursor() as cur:
        cur.execute(_ALL_SELECT_SQL, (start, end))
        rows = cur.fetchall()

    records = []
    for race_id, lane, payload_v1, payload_v2, payload_v3, finish_pos in rows:
        record = {**payload_v1, **payload_v2, **payload_v3}
        record["race_id"] = race_id
        record["lane"] = lane
        record["finish_pos"] = finish_pos
        record["is_winner"] = 1 if finish_pos == 1 else 0
        records.append(record)

    return pl.DataFrame(records)


def _to_xy(df: pl.DataFrame, feature_columns: list[str]) -> tuple:
    # pandas変換にはpyarrowが必要(未導入)なため、polarsのto_numpy()で直接渡す。
    # Float64にキャストしてからto_numpy()すると、null(欠損)は自動的にNaNになる
    # （0埋めしない。LightGBMはNaNをネイティブに欠損として扱える）。
    X = df.select([pl.col(c).cast(pl.Float64) for c in feature_columns]).to_numpy()
    y = df.select(TARGET_COLUMN).to_series().to_numpy()
    return X, y


def train_model(
    train_df: pl.DataFrame,
    feature_columns: list[str],
    *,
    params: dict | None = None,
    num_boost_round: int = DEFAULT_NUM_BOOST_ROUND,
) -> lgb.Booster:
    X_train, y_train = _to_xy(train_df, feature_columns)
    train_set = lgb.Dataset(
        X_train,
        label=y_train,
        feature_name=feature_columns,
        categorical_feature=[
            feature_columns.index(c) for c in CATEGORICAL_FEATURES if c in feature_columns
        ],
        free_raw_data=False,
    )
    return lgb.train(
        {**DEFAULT_PARAMS, **(params or {})}, train_set, num_boost_round=num_boost_round
    )


def predict_race_normalized(
    booster: lgb.Booster, df: pl.DataFrame, feature_columns: list[str]
) -> pl.DataFrame:
    """レース単位で6艇の予測確率を合計1に正規化し、pred_prob列として付与する。"""
    X, _ = _to_xy(df, feature_columns)
    raw_pred = booster.predict(X)

    result = df.select(["race_id", "lane", "is_winner"]).with_columns(
        pl.Series("raw_pred", raw_pred)
    )
    return result.with_columns(
        (pl.col("raw_pred") / pl.col("raw_pred").sum().over("race_id")).alias("pred_prob")
    )


def evaluate(result: pl.DataFrame) -> dict:
    """result: race_id, lane, is_winner, pred_prob を持つDataFrame。"""
    total_races = result.select(pl.col("race_id").n_unique()).item()

    predicted_winners = (
        result.with_columns(
            pl.col("pred_prob")
            .rank(method="ordinal", descending=True)
            .over("race_id")
            .alias("rank_in_race")
        )
        .filter(pl.col("rank_in_race") == 1)
    )
    hits = predicted_winners.select(pl.col("is_winner").sum()).item()
    hit_rate = hits / total_races if total_races else float("nan")

    # log loss: 多クラス(6艇のうち1艇が1着)の交差エントロピーなので、
    # 各レースで「実際に1着だった艇」に割り当てた確率のみを使う
    # （全艇失格等でis_winner=1の行が無いレースは対象外）。
    eps = 1e-15
    actual_winner_probs = result.filter(pl.col("is_winner") == 1)["pred_prob"].to_numpy()
    races_with_winner = len(actual_winner_probs)
    log_loss = (
        float(-np.mean(np.log(np.clip(actual_winner_probs, eps, 1 - eps))))
        if races_with_winner
        else float("nan")
    )

    # Brier score: レースごとに6艇分の(予測確率-実際)^2を合計し、レース数で平均する
    # （多クラスBrier scoreの標準的な定義）。
    per_race_brier = (
        result.with_columns(((pl.col("pred_prob") - pl.col("is_winner")) ** 2).alias("sq_err"))
        .group_by("race_id")
        .agg(pl.col("sq_err").sum().alias("race_brier"))
    )
    brier_score = float(per_race_brier.select(pl.col("race_brier").mean()).item())

    lane_distribution = (
        predicted_winners.group_by("lane")
        .agg(pl.len().alias("predicted_count"))
        .sort("lane")
        .with_columns((pl.col("predicted_count") / total_races).alias("predicted_share"))
    )

    return {
        "total_races": total_races,
        "races_with_winner": races_with_winner,
        "hit_rate": hit_rate,
        "log_loss": log_loss,
        "brier_score": brier_score,
        "lane_distribution": lane_distribution,
    }


LANE_EMPIRICAL_WIN_RATE = {
    1: 0.5426,
    2: 0.1341,
    3: 0.1258,
    4: 0.0975,
    5: 0.0582,
    6: 0.0299,
}


def log_loss_uniform(result: pl.DataFrame) -> float:
    """全艇1/6の一様分布を割り当てた場合のlog loss（p_winner=1/6で固定）。"""
    races_with_winner = result.filter(pl.col("is_winner") == 1).height
    if not races_with_winner:
        return float("nan")
    return float(-np.log(1.0 / 6.0))


def log_loss_lane_empirical(result: pl.DataFrame) -> float:
    """lane別の経験的1着率を固定確率として割り当てた場合のlog loss
    （特徴量を一切見ない、枠番だけのモデルに相当する）。
    """
    eps = 1e-15
    winners = result.filter(pl.col("is_winner") == 1).select("lane")
    if winners.height == 0:
        return float("nan")
    probs = np.array([LANE_EMPIRICAL_WIN_RATE[lane] for lane in winners["lane"].to_list()])
    return float(-np.mean(np.log(np.clip(probs, eps, 1 - eps))))


def feature_importance(booster: lgb.Booster) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "feature": booster.feature_name(),
            "gain": booster.feature_importance(importance_type="gain"),
            "split": booster.feature_importance(importance_type="split"),
        }
    ).sort("gain", descending=True)


def missing_rate_report(df: pl.DataFrame, feature_columns: list[str]) -> pl.DataFrame:
    """特徴量ごとの欠損（NaN/null）割合。新人・初出場等で履歴が無い行が対象。"""
    total = df.height
    rows = []
    for c in feature_columns:
        n_null = df.select(pl.col(c).is_null().sum()).item()
        rows.append(
            {
                "feature": c,
                "missing_count": n_null,
                "missing_rate": (n_null / total) if total else float("nan"),
            }
        )
    return pl.DataFrame(rows).sort("missing_rate", descending=True)


@dataclass(frozen=True)
class RunResult:
    label: str
    metrics: dict
    result: pl.DataFrame
    booster: lgb.Booster


def _run(
    label: str,
    train_df: pl.DataFrame,
    val_df: pl.DataFrame,
    feature_columns: list[str],
    num_boost_round: int,
) -> RunResult:
    booster = train_model(train_df, feature_columns, num_boost_round=num_boost_round)
    result = predict_race_normalized(booster, val_df, feature_columns)
    metrics = evaluate(result)
    return RunResult(label=label, metrics=metrics, result=result, booster=booster)


def _print_metrics(run: RunResult) -> None:
    m = run.metrics
    print(f"\n--- {run.label} ---")
    print(f"的中率: {m['hit_rate']:.4f} ({m['hit_rate'] * 100:.2f}%)")
    print(
        f"log loss: {m['log_loss']:.4f} "
        f"(winner記録済み{m['races_with_winner']}/{m['total_races']}レース対象)"
    )
    print(f"Brier score: {m['brier_score']:.4f}")
    print("予測1着のlane別分布:")
    for row in m["lane_distribution"].iter_rows(named=True):
        print(
            f"  lane={row['lane']}: predicted_count={row['predicted_count']} "
            f"share={row['predicted_share']:.4f} ({row['predicted_share'] * 100:.2f}%)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-boost-round", type=int, default=DEFAULT_NUM_BOOST_ROUND)
    args = parser.parse_args(argv)

    conn = get_connection()
    try:
        v1_train_df = fetch_dataset(conn, TRAIN_START, TRAIN_END)
        v1_val_df = fetch_dataset(conn, VAL_START, VAL_END)
        combined_train_df = fetch_combined_dataset(conn, TRAIN_START, TRAIN_END)
        combined_val_df = fetch_combined_dataset(conn, VAL_START, VAL_END)
        all_train_df = fetch_all_dataset(conn, TRAIN_START, TRAIN_END)
        all_val_df = fetch_all_dataset(conn, VAL_START, VAL_END)
    finally:
        conn.close()

    print(
        f"train ({TRAIN_START}..{TRAIN_END}): "
        f"races={v1_train_df.select(pl.col('race_id').n_unique()).item()} "
        f"rows(v1)={v1_train_df.height} rows(v1+v2)={combined_train_df.height} "
        f"rows(v1+v2+v3)={all_train_df.height}"
    )
    print(
        f"val   ({VAL_START}..{VAL_END}): "
        f"races={v1_val_df.select(pl.col('race_id').n_unique()).item()} "
        f"rows(v1)={v1_val_df.height} rows(v1+v2)={combined_val_df.height} "
        f"rows(v1+v2+v3)={all_val_df.height}"
    )

    dummy_hit_rate = dummy_lane1_hit_rate(v1_val_df)
    print(f"\nダミー(lane=1固定)的中率: {dummy_hit_rate:.4f} ({dummy_hit_rate * 100:.2f}%)")

    run_v1 = _run("v1_basicのみ", v1_train_df, v1_val_df, V1_FEATURE_COLUMNS, args.num_boost_round)
    run_combined = _run(
        "v1_basic + v2_recent",
        combined_train_df,
        combined_val_df,
        COMBINED_FEATURE_COLUMNS,
        args.num_boost_round,
    )
    run_all = _run(
        "v1_basic + v2_recent + v3_relative",
        all_train_df,
        all_val_df,
        ALL_FEATURE_COLUMNS,
        args.num_boost_round,
    )

    print("\n=== 評価指標比較(検証期間) ===")
    _print_metrics(run_v1)
    _print_metrics(run_combined)
    _print_metrics(run_all)

    print("\n=== feature importance (v1_basic + v2_recent + v3_relative) ===")
    for row in feature_importance(run_all.booster).iter_rows(named=True):
        tag = (
            " [v3]" if row["feature"] in V3_FEATURE_COLUMNS
            else " [v2]" if row["feature"] in V2_FEATURE_COLUMNS
            else ""
        )
        print(f"  {row['feature']:<32s} gain={row['gain']:>14.1f} split={row['split']}{tag}")

    print("\n=== 欠損率(v3特徴量、検証期間) ===")
    for row in missing_rate_report(all_val_df, V3_FEATURE_COLUMNS).iter_rows(named=True):
        print(
            f"  {row['feature']:<32s} missing={row['missing_count']} "
            f"rate={row['missing_rate']:.4f} ({row['missing_rate'] * 100:.2f}%)"
        )

    print("\n=== 欠損率(v2特徴量、検証期間) ===")
    for row in missing_rate_report(all_val_df, V2_FEATURE_COLUMNS).iter_rows(named=True):
        print(
            f"  {row['feature']:<32s} missing={row['missing_count']} "
            f"rate={row['missing_rate']:.4f} ({row['missing_rate'] * 100:.2f}%)"
        )

    print("\n=== 欠損率(v1特徴量、検証期間、参考) ===")
    for row in missing_rate_report(all_val_df, V1_FEATURE_COLUMNS).iter_rows(named=True):
        print(
            f"  {row['feature']:<32s} missing={row['missing_count']} "
            f"rate={row['missing_rate']:.4f} ({row['missing_rate'] * 100:.2f}%)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
