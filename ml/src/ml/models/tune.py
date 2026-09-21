"""LightGBMのハイパーパラメータチューニング(Optuna)。

CLAUDE.md ルール2（時系列split、ランダムKFold禁止）と、検証期間
(2026-01-01〜09-17)を汚染しないという要件を守るため、学習期間
(2023-09-01〜2025-12-31)自体をさらに時系列で2分割してチューニング専用の
学習/検証に使う。検証期間はチューニング中は一切参照しない。

    チューニング用学習: 2023-09-01〜2025-06-30
    チューニング用検証: 2025-07-01〜2025-12-31

目的関数は lgbm.evaluate() が返す log loss（レース単位で正規化した確率の
うち実際の1着艇に割り当てた確率のみを使う、プロダクト方針の主要指標と
同じ定義）。LightGBM自身のbinary_logloss(行単位)ではなくこちらを最適化する。

最後に、最良パラメータで学習期間全体(2023-09-01〜2025-12-31)を再学習し、
検証期間で一度だけ評価して現行モデルと比較する。この評価は一度きりで、
結果を見てパラメータを調整し直すことは禁止（CLAUDE.mdの時系列split
ルールの趣旨に反するため）。

使い方:
    uv run python -m ml.models.tune --n-trials 40
"""

from __future__ import annotations

import argparse
import pickle
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import optuna
import polars as pl

from ml.loaders.db import get_connection
from ml.models.baseline import TRAIN_END, TRAIN_START, VAL_END, VAL_START
from ml.models.lgbm import (
    ALL_FEATURE_COLUMNS,
    CATEGORICAL_FEATURES,
    DEFAULT_NUM_BOOST_ROUND,
    DEFAULT_PARAMS,
    evaluate,
    fetch_all_dataset,
    predict_race_normalized,
    train_model,
)
from ml.models.train import DEFAULT_MODEL_DIR, MODEL_KIND

TUNE_TRAIN_START = date(2023, 9, 1)
TUNE_TRAIN_END = date(2025, 6, 30)
TUNE_VAL_START = date(2025, 7, 1)
TUNE_VAL_END = date(2025, 12, 31)


@dataclass(frozen=True)
class TuneResult:
    best_params: dict
    best_log_loss: float
    study: optuna.Study


def _suggest_params(trial: optuna.Trial) -> dict:
    return {
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
        "num_leaves": trial.suggest_int("num_leaves", 15, 255),
        "min_child_samples": trial.suggest_int("min_child_samples", 5, 200),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
        "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 1.0),
        "bagging_freq": 1,  # bagging_fractionを効かせるために固定で立てる
        "lambda_l2": trial.suggest_float("lambda_l2", 1e-8, 10.0, log=True),
    }


def _make_objective(tune_train_df: pl.DataFrame, tune_val_df: pl.DataFrame):
    def objective(trial: optuna.Trial) -> float:
        params = _suggest_params(trial)
        booster = train_model(
            tune_train_df,
            ALL_FEATURE_COLUMNS,
            params=params,
            num_boost_round=DEFAULT_NUM_BOOST_ROUND,
        )
        result = predict_race_normalized(booster, tune_val_df, ALL_FEATURE_COLUMNS)
        metrics = evaluate(result)
        return metrics["log_loss"]

    return objective


def run_tuning(n_trials: int, *, seed: int = 0) -> TuneResult:
    conn = get_connection()
    try:
        tune_train_df = fetch_all_dataset(conn, TUNE_TRAIN_START, TUNE_TRAIN_END)
        tune_val_df = fetch_all_dataset(conn, TUNE_VAL_START, TUNE_VAL_END)
    finally:
        conn.close()

    print(
        f"チューニング用学習: {TUNE_TRAIN_START}..{TUNE_TRAIN_END} rows={tune_train_df.height}",
        flush=True,
    )
    print(
        f"チューニング用検証: {TUNE_VAL_START}..{TUNE_VAL_END} rows={tune_val_df.height}",
        flush=True,
    )
    print(
        "(検証期間2026-01-01..09-17はここでは一切参照しない)",
        flush=True,
    )

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(
        direction="minimize", sampler=optuna.samplers.TPESampler(seed=seed)
    )
    objective = _make_objective(tune_train_df, tune_val_df)

    def _log_trial(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        print(
            f"  trial {trial.number + 1}/{n_trials}: log_loss={trial.value:.5f} "
            f"best={study.best_value:.5f}",
            flush=True,
        )

    study.optimize(objective, n_trials=n_trials, callbacks=[_log_trial])

    return TuneResult(best_params=study.best_params, best_log_loss=study.best_value, study=study)


def _default_tuned_model_version(today: date | None = None) -> str:
    today = today or date.today()
    return f"{MODEL_KIND}_tuned_{today.strftime('%Y%m%d')}"


def retrain_and_evaluate(
    best_params: dict,
    *,
    model_version: str | None = None,
    model_dir: Path = DEFAULT_MODEL_DIR,
    compare_model_version: str | None = None,
) -> None:
    model_version = model_version or _default_tuned_model_version()

    conn = get_connection()
    try:
        full_train_df = fetch_all_dataset(conn, TRAIN_START, TRAIN_END)
        val_df = fetch_all_dataset(conn, VAL_START, VAL_END)
    finally:
        conn.close()

    print(
        f"\n学習期間全体で再学習: {TRAIN_START}..{TRAIN_END} rows={full_train_df.height}",
        flush=True,
    )
    booster = train_model(
        full_train_df,
        ALL_FEATURE_COLUMNS,
        params=best_params,
        num_boost_round=DEFAULT_NUM_BOOST_ROUND,
    )

    artifact = {
        "model_version": model_version,
        "objective": "binary",
        "feature_columns": ALL_FEATURE_COLUMNS,
        "categorical_features": CATEGORICAL_FEATURES,
        "params": {**DEFAULT_PARAMS, **best_params},
        "num_boost_round": DEFAULT_NUM_BOOST_ROUND,
        "train_start": TRAIN_START,
        "train_end": TRAIN_END,
        "train_row_count": full_train_df.height,
        "train_race_count": full_train_df.select(pl.col("race_id").n_unique()).item(),
        "trained_at": datetime.now(timezone.utc),
        "tuning": {
            "tune_train_start": TUNE_TRAIN_START,
            "tune_train_end": TUNE_TRAIN_END,
            "tune_val_start": TUNE_VAL_START,
            "tune_val_end": TUNE_VAL_END,
        },
        "booster": booster,
    }
    model_dir.mkdir(parents=True, exist_ok=True)
    out_path = model_dir / f"{model_version}.pkl"
    with open(out_path, "wb") as f:
        pickle.dump(artifact, f)
    print(f"saved: {out_path}", flush=True)

    print(
        f"\n=== 検証期間での評価（{VAL_START}..{VAL_END}、一度きり） ===",
        flush=True,
    )
    result = predict_race_normalized(booster, val_df, ALL_FEATURE_COLUMNS)
    metrics = evaluate(result)
    print(f"[チューニング後: {model_version}]", flush=True)
    print(
        f"  的中率={metrics['hit_rate'] * 100:.2f}%  log_loss={metrics['log_loss']:.5f}  "
        f"Brier={metrics['brier_score']:.5f}",
        flush=True,
    )

    if compare_model_version:
        model_path = model_dir / f"{compare_model_version}.pkl"
        if model_path.exists():
            with open(model_path, "rb") as f:
                prod_artifact = pickle.load(f)
            prod_result = predict_race_normalized(
                prod_artifact["booster"], val_df, prod_artifact["feature_columns"]
            )
            prod_metrics = evaluate(prod_result)
            print(f"[現行: {compare_model_version}]", flush=True)
            print(
                f"  的中率={prod_metrics['hit_rate'] * 100:.2f}%  "
                f"log_loss={prod_metrics['log_loss']:.5f}  "
                f"Brier={prod_metrics['brier_score']:.5f}",
                flush=True,
            )
            print("\n[差分: チューニング後 - 現行]", flush=True)
            print(
                f"  的中率={(metrics['hit_rate'] - prod_metrics['hit_rate']) * 100:+.2f}pt  "
                f"log_loss={metrics['log_loss'] - prod_metrics['log_loss']:+.5f}  "
                f"Brier={metrics['brier_score'] - prod_metrics['brier_score']:+.5f}",
                flush=True,
            )
        else:
            print(f"  比較対象モデルが見つかりません: {model_path}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-trials", type=int, default=40)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--model-version", default=None)
    parser.add_argument(
        "--compare-to",
        default="v3_binary_20260920",
        help="検証期間評価で比較する現行モデルのmodel_version",
    )
    args = parser.parse_args(argv)

    print(f"=== Optunaチューニング開始 (n_trials={args.n_trials}) ===", flush=True)
    tune_result = run_tuning(args.n_trials, seed=args.seed)

    print("\n=== チューニング結果 ===", flush=True)
    print(f"best log_loss(チューニング用検証): {tune_result.best_log_loss:.5f}", flush=True)
    print("best params:", flush=True)
    for k, v in tune_result.best_params.items():
        print(f"  {k}: {v}", flush=True)

    retrain_and_evaluate(
        tune_result.best_params,
        model_version=args.model_version,
        compare_model_version=args.compare_to,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
