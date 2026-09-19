"""確率のキャリブレーション検証。

1. 現在の本番モデル(predictions.model_version)が検証期間で実際に出した
   p_first を使い、10分位(0-10%, 10-20%, ... 90-100%、予測確率の値そのものを
   等幅に区切る)ごとに「予測確率の平均」と「実際の1着率」を比較する。

2. Isotonic回帰で補正する。ただし現行モデル(v3_binary_20260919)は
   2025年を含む2023-09-01〜2025-12-31全体で学習済みのため、その2025年を
   そのままcalibrator(Isotonic回帰)の学習にも使うと、モデルが既に
   fitした(=過学習気味な)in-sampleの予測を校正することになり、
   本来の意味でのキャリブレーションにならない（校正用データはモデルの
   学習に使っていないものでなければならない）。
   そのため、この検証専用に2025年を除いた期間(2023-09-01〜2024-12-31)で
   別途モデルを再学習し、2025年(このモデルにとって未学習の期間)で
   Isotonic回帰を fit する。検証期間(2026-01-01〜2026-09-17)はモデルの
   学習にもcalibratorのfitにも一切使っていない、純粋なテスト集合のまま。

3. 信頼性曲線(reliability diagram)のデータを補正前後で出力する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl
import psycopg
from sklearn.isotonic import IsotonicRegression

from ml.loaders.db import get_connection
from ml.models.baseline import VAL_END, VAL_START
from ml.models.lgbm import (
    ALL_FEATURE_COLUMNS,
    evaluate,
    fetch_all_dataset,
    log_loss_lane_empirical,
    predict_race_normalized,
    train_model,
)

N_BINS = 10

# 現行モデルの学習期間(2023-09-01〜2025-12-31)から2025年を切り出し、
# 残りをこの検証専用モデルの学習期間にする。
CALIB_TRAIN_START = date(2023, 9, 1)
CALIB_TRAIN_END = date(2024, 12, 31)
CALIB_FIT_START = date(2025, 1, 1)
CALIB_FIT_END = date(2025, 12, 31)


@dataclass(frozen=True)
class ReliabilityBin:
    bin_label: str
    n: int
    mean_predicted: float
    actual_rate: float


def reliability_bins(p: np.ndarray, y: np.ndarray, n_bins: int = N_BINS) -> list[ReliabilityBin]:
    """p(予測確率, 0-1)をn_bins等幅ビンに分け、ビンごとの平均予測確率と実績率を返す。"""
    bin_idx = np.clip((p * n_bins).astype(int), 0, n_bins - 1)
    bins = []
    for b in range(n_bins):
        mask = bin_idx == b
        n = int(mask.sum())
        bins.append(
            ReliabilityBin(
                bin_label=f"{b * 100 // n_bins:>3d}-{(b + 1) * 100 // n_bins:>3d}%",
                n=n,
                mean_predicted=float(p[mask].mean()) if n else float("nan"),
                actual_rate=float(y[mask].mean()) if n else float("nan"),
            )
        )
    return bins


def _print_bins(bins: list[ReliabilityBin]) -> None:
    print(f"  {'bin':>8s}  {'n':>7s}  {'mean_pred':>10s}  {'actual_rate':>11s}  {'diff':>8s}")
    for b in bins:
        diff = b.actual_rate - b.mean_predicted if b.n else float("nan")
        print(
            f"  {b.bin_label:>8s}  {b.n:>7d}  {b.mean_predicted:>10.4f}  "
            f"{b.actual_rate:>11.4f}  {diff:>+8.4f}"
        )


def fetch_stored_predictions(
    conn: psycopg.Connection,
    model_version: str,
    stage: int,
    start_date: date,
    end_date: date,
) -> pl.DataFrame:
    """本番運用で predictions/prediction_entries に既に書き込まれている
    p_first と実際の着順を突き合わせて取得する（現行モデルの実績確認用）。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pe.p_first, COALESCE(rr.finish_pos = 1, false) AS is_winner
            FROM prediction_entries pe
            JOIN predictions p ON p.id = pe.prediction_id
            JOIN races r ON r.id = p.race_id
            JOIN race_entries re ON re.race_id = p.race_id AND re.lane = pe.lane
            LEFT JOIN race_results rr ON rr.race_entry_id = re.id
            WHERE p.model_version = %s AND p.stage = %s
              AND r.race_date BETWEEN %s AND %s
              AND EXISTS (
                SELECT 1 FROM race_entries re2 JOIN race_results rr2
                    ON rr2.race_entry_id = re2.id
                WHERE re2.race_id = p.race_id
              )
            """,
            (model_version, stage, start_date, end_date),
        )
        rows = cur.fetchall()
    return pl.DataFrame(
        {
            "p_first": [float(r[0]) for r in rows],
            "is_winner": [1 if r[1] else 0 for r in rows],
        }
    )


def _print_metrics_row(label: str, m: dict) -> None:
    print(
        f"  {label:<12s} 的中率={m['hit_rate']:.4f} ({m['hit_rate'] * 100:.2f}%)  "
        f"log_loss={m['log_loss']:.4f}  brier={m['brier_score']:.4f}"
    )


def main() -> int:
    conn = get_connection()
    try:
        # === 1. 現行モデルの実績から信頼性曲線 ===
        print("=== 1. 現行モデルの信頼性曲線（検証期間の実績、predictions由来） ===")

        import os

        model_version = os.environ.get("PREDICTION_MODEL_VERSION") or _latest_model_version()
        print(f"model_version={model_version}")

        stored = fetch_stored_predictions(conn, model_version, 1, VAL_START, VAL_END)
        print(f"対象: {stored.height}行")
        p_before_raw = stored["p_first"].to_numpy()
        y_before_raw = stored["is_winner"].to_numpy()
        bins_before_raw = reliability_bins(p_before_raw, y_before_raw)
        _print_bins(bins_before_raw)

        # === 2. Isotonic回帰での補正（2025年を除いて再学習したモデルで） ===
        print(
            f"\n=== 2. Isotonic回帰補正 "
            f"(学習: {CALIB_TRAIN_START}..{CALIB_TRAIN_END} / 校正: {CALIB_FIT_START}..{CALIB_FIT_END}) ==="
        )

        calib_train_df = fetch_all_dataset(conn, CALIB_TRAIN_START, CALIB_TRAIN_END)
        calib_fit_df = fetch_all_dataset(conn, CALIB_FIT_START, CALIB_FIT_END)
        val_df = fetch_all_dataset(conn, VAL_START, VAL_END)

        print(
            f"calib_train races={calib_train_df.select(pl.col('race_id').n_unique()).item()} "
            f"rows={calib_train_df.height}"
        )
        print(
            f"calib_fit    races={calib_fit_df.select(pl.col('race_id').n_unique()).item()} "
            f"rows={calib_fit_df.height}"
        )

        booster = train_model(calib_train_df, ALL_FEATURE_COLUMNS)

        # 校正前(このモデル自身の出力、renormalize後)
        result_before = predict_race_normalized(booster, val_df, ALL_FEATURE_COLUMNS)
        metrics_before = evaluate(result_before)

        # calibratorをfitする（2025年、このモデルにとって未学習の期間）
        fit_result = predict_race_normalized(booster, calib_fit_df, ALL_FEATURE_COLUMNS)
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(fit_result["pred_prob"].to_numpy(), fit_result["is_winner"].to_numpy())

        # 検証期間に適用し、レース内で再正規化
        calibrated_raw = iso.predict(result_before["pred_prob"].to_numpy())
        result_after = result_before.with_columns(
            pl.Series("calibrated_raw", calibrated_raw).clip(lower_bound=1e-6)
        ).with_columns(
            (pl.col("calibrated_raw") / pl.col("calibrated_raw").sum().over("race_id"))
            .alias("pred_prob")
        )
        metrics_after = evaluate(result_after)

        print(
            f"\n参考: lane固定モデル(枠番だけ) log loss = "
            f"{log_loss_lane_empirical(result_before):.4f}"
        )
        print("\n--- 補正前後の比較(検証期間、2025年除外で再学習したモデル基準) ---")
        _print_metrics_row("補正前", metrics_before)
        _print_metrics_row("補正後", metrics_after)

        # === 3. 信頼性曲線データ(補正前・補正後、10ビン) ===
        print("\n=== 3. 信頼性曲線(検証期間、2025年除外モデル基準) ===")
        print("-- 補正前 --")
        p_before = result_before["pred_prob"].to_numpy()
        y_before = result_before["is_winner"].to_numpy()
        _print_bins(reliability_bins(p_before, y_before))

        print("-- 補正後 --")
        p_after = result_after["pred_prob"].to_numpy()
        y_after = result_after["is_winner"].to_numpy()
        _print_bins(reliability_bins(p_after, y_after))

    finally:
        conn.close()

    return 0


def _latest_model_version() -> str:
    from pathlib import Path

    from ml.models.train import DEFAULT_MODEL_DIR

    files = sorted(Path(DEFAULT_MODEL_DIR).glob("*.pkl"))
    if not files:
        raise RuntimeError("ml/models/ にpklが見つかりません")
    return files[-1].stem


if __name__ == "__main__":
    raise SystemExit(main())
