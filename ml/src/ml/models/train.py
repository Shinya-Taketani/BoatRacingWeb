"""現在のv3モデル(v1_basic + v2_recent + v3_relative, binary)を学習し、
ml/models/{model_version}.pkl に保存する。

model_version はファイル名(拡張子抜き)と一致させる。predict.py がこの
model_version をそのまま predictions.model_version に書き込むため、
後からどのモデルがどの予測を出したか常に一意に辿れる。

再現性のため、学習器本体だけでなく学習に使った期間・特徴量リスト・
カテゴリ変数・パラメータも一緒にpickleへ保存する。
"""

from __future__ import annotations

import argparse
import pickle
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from ml.loaders.db import get_connection
from ml.models.baseline import TRAIN_END, TRAIN_START
from ml.models.lgbm import (
    ALL_FEATURE_COLUMNS,
    CATEGORICAL_FEATURES,
    DEFAULT_NUM_BOOST_ROUND,
    DEFAULT_PARAMS,
    fetch_all_dataset,
    train_model,
)

_ML_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_DIR = _ML_ROOT / "models"

MODEL_KIND = "v3_binary"  # v1_basic + v2_recent + v3_relative, objective=binary


def default_model_version(today: date | None = None) -> str:
    today = today or date.today()
    return f"{MODEL_KIND}_{today.strftime('%Y%m%d')}"


def train_and_save(
    *,
    model_version: str | None = None,
    train_start: date = TRAIN_START,
    train_end: date = TRAIN_END,
    num_boost_round: int = DEFAULT_NUM_BOOST_ROUND,
    model_dir: Path = DEFAULT_MODEL_DIR,
) -> Path:
    model_version = model_version or default_model_version()

    conn = get_connection()
    try:
        train_df = fetch_all_dataset(conn, train_start, train_end)
    finally:
        conn.close()

    booster = train_model(train_df, ALL_FEATURE_COLUMNS, num_boost_round=num_boost_round)

    artifact = {
        "model_version": model_version,
        "objective": "binary",
        "feature_columns": ALL_FEATURE_COLUMNS,
        "categorical_features": CATEGORICAL_FEATURES,
        "params": dict(DEFAULT_PARAMS),
        "num_boost_round": num_boost_round,
        "train_start": train_start,
        "train_end": train_end,
        "train_row_count": train_df.height,
        "train_race_count": train_df.select(pl.col("race_id").n_unique()).item(),
        "trained_at": datetime.now(timezone.utc),
        "booster": booster,
    }

    model_dir.mkdir(parents=True, exist_ok=True)
    out_path = model_dir / f"{model_version}.pkl"
    with open(out_path, "wb") as f:
        pickle.dump(artifact, f)

    return out_path, artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-version", default=None, help="省略時は v3_binary_YYYYMMDD")
    parser.add_argument("--train-start", default=str(TRAIN_START))
    parser.add_argument("--train-end", default=str(TRAIN_END))
    parser.add_argument("--num-boost-round", type=int, default=DEFAULT_NUM_BOOST_ROUND)
    args = parser.parse_args(argv)

    out_path, artifact = train_and_save(
        model_version=args.model_version,
        train_start=date.fromisoformat(args.train_start),
        train_end=date.fromisoformat(args.train_end),
        num_boost_round=args.num_boost_round,
    )

    print(f"saved: {out_path}")
    print(f"model_version: {artifact['model_version']}")
    print(f"train: {artifact['train_start']}..{artifact['train_end']} "
          f"races={artifact['train_race_count']} rows={artifact['train_row_count']}")
    print(f"feature_columns ({len(artifact['feature_columns'])}): "
          f"{', '.join(artifact['feature_columns'])}")
    print(f"categorical_features: {artifact['categorical_features']}")
    print(f"params: {artifact['params']}")
    print(f"num_boost_round: {artifact['num_boost_round']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
