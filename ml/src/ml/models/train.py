"""本番の2モデル構成を学習し、ml/models/{model_version}.pkl に保存する。

2026-10-04、「1着予測モデル」と「3着以内予測モデル」の2本立てに変更した
（CLAUDE.md「p_top3の直接学習モデル」「Bの本番組み込み検討」参照）:
- winner（is_winner, 1着）: 既存のv3_binaryと同じ。勝負レース・妙味レース・
  1号艇危険など、p_firstを使う機能はすべてこちらを使う。
- top3（is_top3, 3着以内）: 新規。confident_top3(軸艇)専用。
  同じ件数で揃えるとwinnerモデル由来のPlackett-Luce展開とほぼ同精度だが、
  キャリブレーション(10分位の予測-実績のズレ)が最大±1.14ptとwinner経由の
  最大13pt超のズレに比べて桁違いに正直なため、confident_top3側だけ
  切り替える。

特徴量(v1+v2+v3)・学習期間・パラメータはどちらも完全に同じで、目的変数と
学習関数だけが違う。model_version はファイル名(拡張子抜き)と一致させる。
predict.py がこの model_version をそのまま predictions.model_version に
書き込むため、後からどのモデルがどの予測を出したか常に一意に辿れる。

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
    CATEGORICAL_FEATURES,
    DEFAULT_NUM_BOOST_ROUND,
    DEFAULT_PARAMS,
    train_model,
)
from ml.models.top3 import FEATURE_SETS, add_is_top3, train_top3_model

_ML_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_DIR = _ML_ROOT / "models"

MODEL_KINDS = {
    "winner": {"target_column": "is_winner"},
    "top3": {"target_column": "is_top3"},
}

# prefix は (feature_set, kind) で決まる。FEATURE_SETS(ml.models.top3と共有)の
# キーと命名・構造を揃える: v3=現行(v3_binary/v3_top3)、
# v5=v5_exhibitionを含む(v5_binary/v5_top3)。
MODEL_PREFIXES = {
    "v3": {"winner": "v3_binary", "top3": "v3_top3"},
    "v5": {"winner": "v5_binary", "top3": "v5_top3"},
}


def default_model_version(kind: str, feature_set: str = "v3", today: date | None = None) -> str:
    today = today or date.today()
    return f"{MODEL_PREFIXES[feature_set][kind]}_{today.strftime('%Y%m%d')}"


def train_and_save(
    *,
    kind: str,
    train_df: pl.DataFrame,
    feature_set: str = "v3",
    model_version: str | None = None,
    train_start: date = TRAIN_START,
    train_end: date = TRAIN_END,
    num_boost_round: int = DEFAULT_NUM_BOOST_ROUND,
    model_dir: Path = DEFAULT_MODEL_DIR,
) -> tuple[Path, dict]:
    if kind not in MODEL_KINDS:
        raise ValueError(f"kind must be one of {sorted(MODEL_KINDS)}, got {kind!r}")
    if feature_set not in FEATURE_SETS:
        raise ValueError(f"feature_set must be one of {sorted(FEATURE_SETS)}, got {feature_set!r}")

    model_version = model_version or default_model_version(kind, feature_set)
    target_column = MODEL_KINDS[kind]["target_column"]
    _, feature_columns = FEATURE_SETS[feature_set]

    if kind == "top3":
        train_df = add_is_top3(train_df)
        booster = train_top3_model(train_df, feature_columns, num_boost_round=num_boost_round)
    else:
        booster = train_model(train_df, feature_columns, num_boost_round=num_boost_round)

    artifact = {
        "model_version": model_version,
        "kind": kind,
        # 推論側がv3/v5モデルを取り違えて使わないよう、学習時に選んだ
        # feature_setをそのまま保存する（CLAUDE.md「v5モデルのpkl保存」参照）。
        "feature_set": feature_set,
        "target_column": target_column,
        "objective": "binary",
        "feature_columns": feature_columns,
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


def _print_artifact(out_path: Path, artifact: dict) -> None:
    print(f"saved: {out_path}")
    print(
        f"model_version: {artifact['model_version']} "
        f"(kind={artifact['kind']}, feature_set={artifact['feature_set']})"
    )
    print(
        f"train: {artifact['train_start']}..{artifact['train_end']} "
        f"races={artifact['train_race_count']} rows={artifact['train_row_count']}"
    )
    print(
        f"feature_columns ({len(artifact['feature_columns'])}): "
        f"{', '.join(artifact['feature_columns'])}"
    )
    print(f"categorical_features: {artifact['categorical_features']}")
    print(f"params: {artifact['params']}")
    print(f"num_boost_round: {artifact['num_boost_round']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kind",
        choices=["winner", "top3", "both"],
        default="both",
        help="省略時は両方(winner+top3)を学習・保存する",
    )
    parser.add_argument(
        "--model-version", default=None, help="kind=winner/top3の時のみ有効。省略時は自動命名"
    )
    parser.add_argument(
        "--feature-set",
        choices=sorted(FEATURE_SETS),
        default="v3",
        help=(
            "v3(デフォルト): 現行と完全に同じ(ALL_FEATURE_COLUMNS/fetch_all_dataset、"
            "prefix=v3_binary/v3_top3)。v5: v5_exhibitionを含む"
            "(WITH_V5_FEATURE_COLUMNS/fetch_v5_dataset、prefix=v5_binary/v5_top3)。"
            "ml.models.top3のFEATURE_SETSと共有しており命名・構造を揃えている"
        ),
    )
    parser.add_argument("--train-start", default=str(TRAIN_START))
    parser.add_argument("--train-end", default=str(TRAIN_END))
    parser.add_argument("--num-boost-round", type=int, default=DEFAULT_NUM_BOOST_ROUND)
    args = parser.parse_args(argv)

    if args.kind == "both" and args.model_version:
        parser.error("--model-version は --kind winner か --kind top3 と一緒に使ってください")

    train_start = date.fromisoformat(args.train_start)
    train_end = date.fromisoformat(args.train_end)

    fetch_fn, _ = FEATURE_SETS[args.feature_set]

    conn = get_connection()
    try:
        train_df = fetch_fn(conn, train_start, train_end)
    finally:
        conn.close()

    kinds = ["winner", "top3"] if args.kind == "both" else [args.kind]

    for kind in kinds:
        out_path, artifact = train_and_save(
            kind=kind,
            train_df=train_df,
            feature_set=args.feature_set,
            model_version=args.model_version,
            train_start=train_start,
            train_end=train_end,
            num_boost_round=args.num_boost_round,
        )
        print(f"\n=== {kind} ===")
        _print_artifact(out_path, artifact)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
