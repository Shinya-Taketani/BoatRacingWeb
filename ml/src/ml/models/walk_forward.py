"""walk-forward検証。単一の時系列split（学習〜2025-12-31/検証2026-01-01〜
09-17）だけでは、オッズ調査で見た0.27pt(的中率)程度の差を判定する検出力が
無い（標準誤差1.15pt、0.23SE）。今後の施策評価（v5特徴量・オッズ統合等）の
「有意な改善かどうか」を判定する土台として、複数foldでの評価を導入する。

## foldの設計

- **学習窓: 固定開始(2023-09-01)のexpanding window**を採用した（固定幅の
  rolling windowは採用しなかった）。理由:
  1. 本番モデルは常に「存在する全履歴」を学習に使う運用方針であり
     （tune.pyのチューニング用splitも含め、過去に固定幅の学習窓を使った
     前例が無い）、walk-forwardもこの運用方針と一致させた方が、各foldの
     結果が実際の本番運用の近似になる。
  2. 固定幅にすると初期foldの学習データが大きく削られる（本データセットは
     2023-09-01〜が全履歴であり、固定幅にすると直近foldほど古いデータを
     捨てることになるが、競艇のレース構造自体が3年間で大きく変化した
     根拠は無く、古いデータを捨てる積極的な理由が無い）。
  3. expanding windowの欠点（foldごとに学習データ量が変わるため、学習量の
     違いが性能差に混入する可能性）は認識している。もしこれが問題になる
     ようであれば、固定幅版を別途追加する余地を残す。
- **検証窓: 3ヶ月、重複なく前進**。
- **fold数: 6**。データ全期間(2023-09-01〜2026-09-30、実データで確認済みの
  安全な上限。2026-10-01以降は結果未確定のため対象外)が37ヶ月あり、
  3ヶ月刻みで重複なく6fold(18ヶ月分)の検証窓を取ると、最初のfoldの学習
  データが19ヶ月(2023-09-01〜2025-03-31)確保できる。これより少ないfold数
  (4〜5)では直近の変化を捉える機会が減り、これより多いfold数(7以上)では
  最初のfoldの学習データが短くなりすぎる（本番の学習期間は2年4ヶ月=28ヶ月
  なので、19ヶ月はまだ大きく見劣りしない下限と判断した）。
- fold4の学習窓(2023-09-01〜2025-12-31)は本番モデルの学習期間と完全に一致し、
  fold4の検証窓(2026-01-01〜03-31)は本番の検証期間(2026-01-01〜09-17)の
  最初の3ヶ月に相当する。既存の単一split記録値との整合性チェックに使える。

## 評価指標の定義（レース単位の多クラス式を正とする）

is_winner(1着、6艇中1艇のみ正)は相互排他的な6値分類であり、
`ml.models.lgbm.evaluate()`が実装する**レース単位の多クラス式**
（log loss = 実際の1着艇に割り当てた確率のみのcross entropy、
Brier = 6艇分の(予測確率-実際)^2の合計をレース単位で平均）が正しい定義
である。これが本番モデルの全既存記録（例: v1+v2+v3+v5で検証期間
log loss=1.1799、v1+v2+v3で1.1974）のスケール。

一方、オッズ調査(2026-10-08)やtop3.pyでは**艇単位の二値log loss**
（6艇それぞれを独立な二値分類として評価する通常のbinary cross entropy、
0.32前後のスケール）を使っている。これはis_top3（3着以内、6艇中3艇が
正でどの艇が独立に正/負かを問う問題）には正しい定義だが、is_winner
（6艇中必ず1艇だけが正）に対して使うと、5艇の「外れた」確率分まで
cross entropyに加算してしまい、多クラス問題の構造を無視した別の指標に
なる。両者は値の意味も尺度も異なり、直接比較できない
（本モジュールのlog lossとオッズ調査のlog lossを並べて「改善/悪化」と
読んではならない）。

本モジュールは`ml.models.lgbm.evaluate()`を**唯一の算出箇所**として呼び、
新たに重複実装しない。confident_top3の判定方法は
`ml.models.top3.confident_top3_production()`（本番API定義）をそのまま使う。

## ベースライン相対優位（advantage）とfold間変動の切り分け（2026-10-09）

初回のベースライン取得で、fold2(的中率54.79%)とfold3(56.30%)の間に
+1.5ptの段差が出た。v5の効果(+0.42pt)の3倍以上の大きさで、このままでは
fold平均が何を表すか解釈できない。そこで各foldの検証窓について
「常に1号艇を1着予測」した場合の的中率(`ml.models.baseline.dummy_lane1_hit_rate()`、
全期間の既知値54.26%)を`lane1_hit_rate`として併算し、
`advantage = hit_rate - lane1_hit_rate`（期間要因を相殺した「モデルの
純粋な上乗せ」）を主指標に追加した。

**fold2→fold3の段差の原因**: 同じ検証窓でlane1_hit_rateを見ると
52.89%→54.78%で+1.89pt変動しており、モデルの的中率の変動(+1.51pt)と
ほぼ同じ向き・同程度の大きさ。つまり段差の主因は**期間要因**（この時期は
1号艇が勝ちやすい巡り合わせだった）であり、学習データ量やデータ品質の
変化ではない。事実、advantage(=モデルの上乗せ分だけ)で見ると
fold2(+1.90pt)→fold3(+1.52pt)はむしろ微減しており、「fold3でモデルが
急に賢くなった」わけではないことが分かる。advantageのfold間標準偏差は
生の的中率の標準偏差より大幅に小さく(v3: 0.70pt→0.25pt程度)、期間要因が
fold間変動の大部分を占めていたことを裏付ける。

**v5 vs v3比較への影響**: advantageはモデルごとに同じ期間効果
(lane1_hit_rate)を引いているだけなので、同一fold内でのv5-v3差分
(ペア差)は数値的にraw的中率の差分と完全に一致する
(advantage_v5 - advantage_v3 = (hit_v5 - lane1) - (hit_v3 - lane1) =
hit_v5 - hit_v3、lane1項が厳密に相殺するため)。したがって**v5の優位性の
有意性判定(下記)はadvantageに基づいてもraw的中率に基づいても同じ結論**
になる。advantageの価値は比較の検出力を上げることではなく、fold単体の
解釈性（期間要因とモデルの実力を分離できる）とfold間の段差の原因診断に
ある。

## 有意性基準の注意点（fold間相関、expanding windowの副作用）

0.26pt(n=6, t(df=5)=2.571)という有意性の目安は、各foldが独立である
ことを前提にした値である。しかし本モジュールのexpanding window設計では、
foldは独立ではない。例えばfold4の学習窓(2023-09-01〜2025-12-31)には
fold1〜3の検証窓(2025-04〜2025-12)がそのまま含まれており、fold4以降の
モデルはfold1〜3で評価した「未知のデータ」を既に学習済みになる。
この学習データの重複により、fold間の誤差は互いに独立ではなく正の相関を
持ちうる（学習データが近いfold同士は似た誤差特性を持ちやすい）。
相関のあるサンプルに通常のt検定(独立同一分布を仮定)を適用すると、
実際より自信過剰な(甘い)判定になる。そのため**0.26ptという数値は
下限(必要な差の最小値)として扱うべきであり、実際に必要な差はこれより
大きい可能性がある**。この限界を補うため、単一の閾値判定だけでなく
**「全fold(6fold中6fold)で一貫して同方向」という条件を必ず併用する**
こと。both conditions（閾値を超える、かつ全fold一貫）が揃って初めて
「有意」と呼べる、という運用ルールとする。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import random as _random
from datetime import date, timedelta

import numpy as np
import polars as pl
import psycopg

from ml.loaders.db import get_connection
from ml.models.baseline import dummy_lane1_hit_rate
from ml.models.lgbm import (
    ALL_FEATURE_COLUMNS,
    DEFAULT_NUM_BOOST_ROUND,
    WITH_V5_FEATURE_COLUMNS,
    WITH_V6_FEATURE_COLUMNS,
    compute_time_decay_weights,
    evaluate,
    fetch_all_dataset,
    fetch_v5_dataset,
    fetch_v6_dataset,
    predict_race_normalized,
    train_model,
    train_model_with_early_stopping,
)
from ml.models.top3 import confident_top3_production, top3_from_plackett_luce

# (train_start, train_end, val_start, val_end)。train_startは全fold共通
# (expanding window、固定開始)。
FOLDS: list[tuple[str, str, str, str]] = [
    ("2023-09-01", "2025-03-31", "2025-04-01", "2025-06-30"),
    ("2023-09-01", "2025-06-30", "2025-07-01", "2025-09-30"),
    ("2023-09-01", "2025-09-30", "2025-10-01", "2025-12-31"),
    ("2023-09-01", "2025-12-31", "2026-01-01", "2026-03-31"),
    ("2023-09-01", "2026-03-31", "2026-04-01", "2026-06-30"),
    ("2023-09-01", "2026-06-30", "2026-07-01", "2026-09-30"),
]

# 既存のv5特徴量比較(CLAUDE.md「全期間バックフィル完了とv5の本番split検証」等)
# と揃える。本番で実際に使っているtop3_confident_threshold(0.84、v5_top3直接
# 学習モデル用)とは別軸なので混同しないこと。
CONFIDENT_TOP3_THRESHOLD = 0.96

FEATURE_SETS = {
    "v3": (fetch_all_dataset, ALL_FEATURE_COLUMNS),
    "v5": (fetch_v5_dataset, WITH_V5_FEATURE_COLUMNS),
    "v6": (fetch_v6_dataset, WITH_V6_FEATURE_COLUMNS),
}


@dataclass(frozen=True)
class FoldResult:
    fold: int
    train_start: str
    train_end: str
    val_start: str
    val_end: str
    train_races: int
    val_races: int
    hit_rate: float
    lane1_hit_rate: float
    log_loss: float
    brier_score: float
    confident_count: int
    confident_hits: int
    confident_accuracy: float

    @property
    def advantage(self) -> float:
        """期間要因(lane1_hit_rate)を相殺した、モデルの純粋な上乗せ分。"""
        return self.hit_rate - self.lane1_hit_rate


def _train_and_eval(
    train_df: pl.DataFrame,
    val_df: pl.DataFrame,
    feature_columns: list[str],
    *,
    num_boost_round: int,
    sample_weight: np.ndarray | None,
    params: dict | None = None,
) -> dict:
    """1回の学習・推論・評価をまとめた内部ヘルパー。run_fold/run_fold_weight_sweep/
    run_random_searchから使う（候補ごとにtrain_df/val_dfのfetchを繰り返さない
    ため、この関数だけを候補ごとに繰り返し呼ぶ）。paramsを渡すとDEFAULT_PARAMSを
    上書きする(ハイパーパラメータ探索用。省略時はDEFAULT_PARAMSのみ＝従来通り)。
    """
    booster = train_model(
        train_df, feature_columns, num_boost_round=num_boost_round,
        sample_weight=sample_weight, params=params,
    )
    winner_result = predict_race_normalized(booster, val_df, feature_columns)
    metrics = evaluate(winner_result)
    top3_df = top3_from_plackett_luce(winner_result, val_df)
    confident = confident_top3_production(top3_df, CONFIDENT_TOP3_THRESHOLD)
    return {
        "hit_rate": metrics["hit_rate"],
        "log_loss": metrics["log_loss"],
        "brier_score": metrics["brier_score"],
        "confident_count": confident["count"],
        "confident_hits": confident["hits"],
        "confident_accuracy": confident["accuracy"],
    }


def run_fold(
    conn: psycopg.Connection,
    fold_idx: int,
    train_start: str,
    train_end: str,
    val_start: str,
    val_end: str,
    feature_columns: list[str],
    fetch_fn,
    *,
    num_boost_round: int = DEFAULT_NUM_BOOST_ROUND,
    decay: str | None = None,
    half_life_days: float | None = None,
) -> FoldResult:
    """decay/half_life_daysを指定すると、学習時に時間減衰サンプル重みを使う
    (reference_date=train_end)。省略時(デフォルト)は重み無し＝従来と完全に
    同じ挙動。
    """
    train_df = fetch_fn(conn, train_start, train_end)
    val_df = fetch_fn(conn, val_start, val_end)

    sample_weight = None
    if half_life_days is not None:
        sample_weight = compute_time_decay_weights(
            train_df["race_date"].to_list(),
            date.fromisoformat(train_end),
            decay=decay or "exponential",
            half_life_days=half_life_days,
        )

    winner_result = _train_and_eval(
        train_df, val_df, feature_columns, num_boost_round=num_boost_round, sample_weight=sample_weight
    )
    lane1_rate = dummy_lane1_hit_rate(val_df)

    return FoldResult(
        fold=fold_idx,
        train_start=train_start,
        train_end=train_end,
        val_start=val_start,
        val_end=val_end,
        train_races=train_df.select(pl.col("race_id").n_unique()).item(),
        val_races=val_df.select(pl.col("race_id").n_unique()).item(),
        hit_rate=winner_result["hit_rate"],
        lane1_hit_rate=lane1_rate,
        log_loss=winner_result["log_loss"],
        brier_score=winner_result["brier_score"],
        confident_count=winner_result["confident_count"],
        confident_hits=winner_result["confident_hits"],
        confident_accuracy=winner_result["confident_accuracy"],
    )


def run_fold_weight_sweep(
    conn: psycopg.Connection,
    fold_idx: int,
    train_start: str,
    train_end: str,
    val_start: str,
    val_end: str,
    feature_columns: list[str],
    fetch_fn,
    weight_candidates: list[tuple[str, str | None, float | None]],
    *,
    num_boost_round: int = DEFAULT_NUM_BOOST_ROUND,
) -> dict[str, FoldResult]:
    """weight_candidates: (label, decay, half_life_days)のリスト。
    half_life_days=Noneは重み無し。train_df/val_dfは1回だけfetchし、
    候補ごとに学習だけを繰り返す（DBアクセスを候補数分繰り返さないため）。
    """
    train_df = fetch_fn(conn, train_start, train_end)
    val_df = fetch_fn(conn, val_start, val_end)
    lane1_rate = dummy_lane1_hit_rate(val_df)
    train_races = train_df.select(pl.col("race_id").n_unique()).item()
    val_races = val_df.select(pl.col("race_id").n_unique()).item()
    reference_date = date.fromisoformat(train_end)

    results: dict[str, FoldResult] = {}
    for label, decay, half_life_days in weight_candidates:
        sample_weight = None
        if half_life_days is not None:
            sample_weight = compute_time_decay_weights(
                train_df["race_date"].to_list(),
                reference_date,
                decay=decay or "exponential",
                half_life_days=half_life_days,
            )
        metrics = _train_and_eval(
            train_df, val_df, feature_columns, num_boost_round=num_boost_round, sample_weight=sample_weight
        )
        results[label] = FoldResult(
            fold=fold_idx,
            train_start=train_start,
            train_end=train_end,
            val_start=val_start,
            val_end=val_end,
            train_races=train_races,
            val_races=val_races,
            hit_rate=metrics["hit_rate"],
            lane1_hit_rate=lane1_rate,
            log_loss=metrics["log_loss"],
            brier_score=metrics["brier_score"],
            confident_count=metrics["confident_count"],
            confident_hits=metrics["confident_hits"],
            confident_accuracy=metrics["confident_accuracy"],
        )
    return results


# early stoppingの内部検証に割く学習窓末尾の日数。約3ヶ月(90日)とした。
# fold1の学習窓が19ヶ月と最短のため、長すぎると学習データが大きく削られる。
# 既存のwalk-forward検証窓自体が3ヶ月であり、長さを揃えることで
# 1号艇勝率の月次変動(CLAUDE.md「lane1勝率の構造変化」参照)のような
# 短期ノイズをある程度均せる最小限の長さとして妥当と判断した。
DEFAULT_INNER_VAL_TAIL_DAYS = 90
DEFAULT_EARLY_STOPPING_ROUNDS = 50
DEFAULT_MAX_BOOST_ROUND = 2000


def split_inner_validation(
    train_df: pl.DataFrame, train_end: str, *, tail_days: int = DEFAULT_INNER_VAL_TAIL_DAYS
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """学習窓の末尾tail_days日を内部検証セットとして切り出す。

    walk-forwardの検証fold(val_start..val_end、最終的な評価対象)とは
    完全に別物で、ここでは一切参照しない。あくまで学習窓の中だけで完結する
    「学習窓終端より前のデータで学習→学習窓終端に近い期間で検証」という
    通常のearly stopping用splitであり、リークにはならない。
    """
    train_end_date = date.fromisoformat(train_end)
    inner_val_start = train_end_date - timedelta(days=tail_days - 1)
    inner_train_df = train_df.filter(pl.col("race_date") < inner_val_start)
    inner_val_df = train_df.filter(pl.col("race_date") >= inner_val_start)
    return inner_train_df, inner_val_df


def run_fold_boosting_compare(
    conn: psycopg.Connection,
    fold_idx: int,
    train_start: str,
    train_end: str,
    val_start: str,
    val_end: str,
    feature_columns: list[str],
    fetch_fn,
    *,
    num_boost_round_fixed: int = DEFAULT_NUM_BOOST_ROUND,
    early_stopping_rounds: int = DEFAULT_EARLY_STOPPING_ROUNDS,
    inner_val_tail_days: int = DEFAULT_INNER_VAL_TAIL_DAYS,
    max_boost_round: int = DEFAULT_MAX_BOOST_ROUND,
    params: dict | None = None,
) -> tuple[FoldResult, FoldResult, int]:
    """「num_boost_round固定」と「early stoppingで選んだ本数」を同じfoldで
    比較する。early stopping側は、学習窓末尾(inner_val_df)で本数
    (best_iteration)だけを決め、そのあとtrain_df全体(inner_train_df+
    inner_val_df、内部検証に使った分も含む)で再学習する—内部検証用に
    データを失わないため。

    返り値は(固定本数のFoldResult, early stopping選定本数のFoldResult,
    選ばれたbest_iteration)。
    """
    train_df = fetch_fn(conn, train_start, train_end)
    val_df = fetch_fn(conn, val_start, val_end)
    lane1_rate = dummy_lane1_hit_rate(val_df)
    train_races = train_df.select(pl.col("race_id").n_unique()).item()
    val_races = val_df.select(pl.col("race_id").n_unique()).item()

    def _build(metrics: dict) -> FoldResult:
        return FoldResult(
            fold=fold_idx,
            train_start=train_start,
            train_end=train_end,
            val_start=val_start,
            val_end=val_end,
            train_races=train_races,
            val_races=val_races,
            hit_rate=metrics["hit_rate"],
            lane1_hit_rate=lane1_rate,
            log_loss=metrics["log_loss"],
            brier_score=metrics["brier_score"],
            confident_count=metrics["confident_count"],
            confident_hits=metrics["confident_hits"],
            confident_accuracy=metrics["confident_accuracy"],
        )

    fixed_metrics = _train_and_eval(
        train_df, val_df, feature_columns, num_boost_round=num_boost_round_fixed,
        sample_weight=None, params=params,
    )
    fixed_result = _build(fixed_metrics)

    inner_train_df, inner_val_df = split_inner_validation(
        train_df, train_end, tail_days=inner_val_tail_days
    )
    _, best_iter = train_model_with_early_stopping(
        inner_train_df, inner_val_df, feature_columns,
        params=params, max_boost_round=max_boost_round,
        early_stopping_rounds=early_stopping_rounds,
    )
    es_metrics = _train_and_eval(
        train_df, val_df, feature_columns, num_boost_round=best_iter,
        sample_weight=None, params=params,
    )
    es_result = _build(es_metrics)

    return fixed_result, es_result, best_iter


def run_walk_forward(
    conn: psycopg.Connection, feature_set: str, *, num_boost_round: int = DEFAULT_NUM_BOOST_ROUND
) -> list[FoldResult]:
    fetch_fn, feature_columns = FEATURE_SETS[feature_set]
    results = []
    for i, (train_start, train_end, val_start, val_end) in enumerate(FOLDS, start=1):
        r = run_fold(
            conn, i, train_start, train_end, val_start, val_end, feature_columns, fetch_fn,
            num_boost_round=num_boost_round,
        )
        results.append(r)
    return results


# --- ハイパーパラメータのランダムサーチ(第2段階) --------------------------
# num_boost_round(木の本数)は対象外: 第1段階(early stopping)の結論に従い
# main()側で固定値またはfold別の値として渡す。ここで同時に探索すると
# 「少ない本数+強い正則化」と「多い本数+弱い正則化」が区別できなくなり、
# 解釈が難しくなるため。

PARAM_SEARCH_RANGES = {
    "num_leaves": (15, 255),  # int
    "min_data_in_leaf": (10, 200),  # int
    "feature_fraction": (0.5, 1.0),  # float, uniform
    "bagging_fraction": (0.5, 1.0),  # float, uniform
    "bagging_freq": (1, 10),  # int
    "lambda_l1": (-4, 1),  # float, log10-uniform (10^-4 〜 10^1)
    "lambda_l2": (-4, 1),  # float, log10-uniform
    "learning_rate": (-2, -0.7),  # float, log10-uniform (約0.01〜0.2)
}


def sample_params(rng: _random.Random) -> dict:
    return {
        "num_leaves": rng.randint(*PARAM_SEARCH_RANGES["num_leaves"]),
        "min_data_in_leaf": rng.randint(*PARAM_SEARCH_RANGES["min_data_in_leaf"]),
        "feature_fraction": rng.uniform(*PARAM_SEARCH_RANGES["feature_fraction"]),
        "bagging_fraction": rng.uniform(*PARAM_SEARCH_RANGES["bagging_fraction"]),
        "bagging_freq": rng.randint(*PARAM_SEARCH_RANGES["bagging_freq"]),
        "lambda_l1": 10 ** rng.uniform(*PARAM_SEARCH_RANGES["lambda_l1"]),
        "lambda_l2": 10 ** rng.uniform(*PARAM_SEARCH_RANGES["lambda_l2"]),
        "learning_rate": 10 ** rng.uniform(*PARAM_SEARCH_RANGES["learning_rate"]),
    }


def run_random_search(
    conn: psycopg.Connection,
    feature_set: str,
    n_trials: int,
    *,
    boost_rounds: int | dict[int, int],
    seed: int = 0,
) -> list[dict]:
    """n_trials個のパラメータをランダムサンプリングし、全6foldで評価する。
    fold単位でtrain_df/val_dfを1回だけfetchし、同じfoldデータに対して
    全試行を順に学習する（DBアクセスをn_trials倍にしない）。
    boost_roundsは全fold共通のintか、fold番号->本数のdict
    （第1段階でearly stoppingが有効だった場合、fold別のbest_iterationを
    そのまま渡せる）。

    返り値は各試行について
    {"trial": int, "params": dict, "fold_results": list[FoldResult],
     "mean_hit_rate": float} のリスト（サンプリング順）。
    """
    fetch_fn, feature_columns = FEATURE_SETS[feature_set]
    rng = _random.Random(seed)
    param_list = [sample_params(rng) for _ in range(n_trials)]

    trial_fold_results: list[list[FoldResult]] = [[] for _ in range(n_trials)]

    for i, (train_start, train_end, val_start, val_end) in enumerate(FOLDS, start=1):
        train_df = fetch_fn(conn, train_start, train_end)
        val_df = fetch_fn(conn, val_start, val_end)
        lane1_rate = dummy_lane1_hit_rate(val_df)
        train_races = train_df.select(pl.col("race_id").n_unique()).item()
        val_races = val_df.select(pl.col("race_id").n_unique()).item()
        nb = boost_rounds[i] if isinstance(boost_rounds, dict) else boost_rounds

        for t, params in enumerate(param_list):
            metrics = _train_and_eval(
                train_df, val_df, feature_columns, num_boost_round=nb,
                sample_weight=None, params=params,
            )
            trial_fold_results[t].append(
                FoldResult(
                    fold=i,
                    train_start=train_start,
                    train_end=train_end,
                    val_start=val_start,
                    val_end=val_end,
                    train_races=train_races,
                    val_races=val_races,
                    hit_rate=metrics["hit_rate"],
                    lane1_hit_rate=lane1_rate,
                    log_loss=metrics["log_loss"],
                    brier_score=metrics["brier_score"],
                    confident_count=metrics["confident_count"],
                    confident_hits=metrics["confident_hits"],
                    confident_accuracy=metrics["confident_accuracy"],
                )
            )
            print(
                f"  [fold{i} trial{t}] hit_rate={metrics['hit_rate'] * 100:.2f}% "
                f"log_loss={metrics['log_loss']:.4f}",
                flush=True,
            )

    trials = []
    for t, params in enumerate(param_list):
        fold_results = trial_fold_results[t]
        mean_hr = float(np.mean([r.hit_rate for r in fold_results]))
        trials.append(
            {"trial": t, "params": params, "fold_results": fold_results, "mean_hit_rate": mean_hr}
        )
    return trials


def print_search_top_n(trials: list[dict], n: int = 5) -> None:
    """mean_hit_rate降順で上位n件を表示する。多重比較の目安として、
    最良が突出しているか団子状態かをここで目視できるようにする。
    """
    ranked = sorted(trials, key=lambda t: t["mean_hit_rate"], reverse=True)
    print(f"\n=== ランダムサーチ 上位{n}件(平均的中率降順) ===")
    print(f"{'順位':>4s} {'trial':>6s} {'平均的中率':>10s} {'主なパラメータ':<80s}")
    for rank, t in enumerate(ranked[:n], start=1):
        p = t["params"]
        p_str = (
            f"num_leaves={p['num_leaves']} min_data_in_leaf={p['min_data_in_leaf']} "
            f"feature_fraction={p['feature_fraction']:.3f} bagging_fraction={p['bagging_fraction']:.3f} "
            f"bagging_freq={p['bagging_freq']} lambda_l1={p['lambda_l1']:.4g} "
            f"lambda_l2={p['lambda_l2']:.4g} learning_rate={p['learning_rate']:.4g}"
        )
        print(f"{rank:>4d} {t['trial']:>6d} {t['mean_hit_rate'] * 100:>9.2f}% {p_str}")

    if len(ranked) >= 2:
        gap = (ranked[0]["mean_hit_rate"] - ranked[1]["mean_hit_rate"]) * 100
        spread = (ranked[0]["mean_hit_rate"] - ranked[min(n, len(ranked)) - 1]["mean_hit_rate"]) * 100
        print(
            f"\n1位と2位の差: {gap:+.3f}pt / 1位と{min(n, len(ranked))}位の差: {spread:+.3f}pt "
            "(1位と2位以下が僅差=団子状態なら、1位は40試行から選んだ見かけの最大値に"
            "過ぎず偶然の可能性が高い。はっきり突出していなければ採用に慎重になること)"
        )


def _mean_std(values: list[float]) -> tuple[float, float]:
    arr = np.array(values, dtype=float)
    return float(arr.mean()), float(arr.std(ddof=1))


def print_fold_table(label: str, results: list[FoldResult]) -> None:
    print(f"\n=== {label}: fold別結果 ===")
    print(
        f"{'fold':>4s} {'train窓':>22s} {'val窓':>22s} {'train races':>11s} "
        f"{'val races':>9s} {'的中率':>8s} {'lane1率':>8s} {'優位':>7s} "
        f"{'log loss':>9s} {'Brier':>8s} {'top3該当':>8s} {'top3実績':>8s}"
    )
    for r in results:
        acc_str = f"{r.confident_accuracy * 100:.2f}%" if r.confident_count else "該当なし"
        print(
            f"{r.fold:>4d} {r.train_start}..{r.train_end:>10s} {r.val_start}..{r.val_end:>10s} "
            f"{r.train_races:>11d} {r.val_races:>9d} {r.hit_rate * 100:>7.2f}% "
            f"{r.lane1_hit_rate * 100:>7.2f}% {r.advantage * 100:>+6.2f}pt "
            f"{r.log_loss:>9.4f} {r.brier_score:>8.4f} {r.confident_count:>8d} {acc_str:>8s}"
        )

    hit_rates = [r.hit_rate for r in results]
    lane1_rates = [r.lane1_hit_rate for r in results]
    advantages = [r.advantage for r in results]
    log_losses = [r.log_loss for r in results]
    briers = [r.brier_score for r in results]
    mean_hr, std_hr = _mean_std(hit_rates)
    mean_l1, std_l1 = _mean_std(lane1_rates)
    mean_adv, std_adv = _mean_std(advantages)
    mean_ll, std_ll = _mean_std(log_losses)
    mean_br, std_br = _mean_std(briers)
    print(
        f"\nfold間 平均±標準偏差: 的中率={mean_hr * 100:.2f}±{std_hr * 100:.2f}pt  "
        f"lane1率={mean_l1 * 100:.2f}±{std_l1 * 100:.2f}pt  "
        f"優位(advantage)={mean_adv * 100:+.2f}±{std_adv * 100:.2f}pt  "
        f"log loss={mean_ll:.4f}±{std_ll:.4f}  Brier={mean_br:.4f}±{std_br:.4f}"
    )
    print(
        "  (優位の標準偏差が的中率の標準偏差より明確に小さければ、fold間の"
        "変動は主に期間要因(lane1率の変動)によるもので、モデルの実力自体は"
        "より安定していることを示す)"
    )


def compare(
    label_a: str,
    results_a: list[FoldResult],
    label_b: str,
    results_b: list[FoldResult],
    *,
    show_data_volume_note: bool = True,
) -> dict:
    """label_bからlabel_aを引いた差を比較し、確立した判定ルール
    （的中率差が0.26pt超 かつ 全fold一貫して同方向）で有意性を判定する。
    呼び出し側がこの判定を使って後続処理を分岐できるよう、判定結果を
    dictで返す。
    """
    print(f"\n=== {label_a} vs {label_b}: fold別の差({label_b}-{label_a}) ===")
    print(f"{'fold':>4s} {'的中率差':>10s} {'log loss差':>12s} {'Brier差':>10s}")
    hr_diffs, ll_diffs, br_diffs = [], [], []
    for ra, rb in zip(results_a, results_b):
        hr_diff = rb.hit_rate - ra.hit_rate
        ll_diff = rb.log_loss - ra.log_loss
        br_diff = rb.brier_score - ra.brier_score
        hr_diffs.append(hr_diff)
        ll_diffs.append(ll_diff)
        br_diffs.append(br_diff)
        print(f"{ra.fold:>4d} {hr_diff * 100:>+9.2f}pt {ll_diff:>+12.4f} {br_diff:>+10.4f}")

    n = len(hr_diffs)
    mean_hr_diff, std_hr_diff = _mean_std(hr_diffs)
    se_hr_diff = std_hr_diff / np.sqrt(n)
    # t分布の97.5%点(両側95%、自由度n-1)。fold数が少ないため正規近似ではなく
    # t分布を使う。scipyへの依存を避け、よく使われる自由度5のt臨界値を使う
    # （fold数6=自由度5を前提にした近似。fold数を変える場合はこの値も
    # 見直すこと）。
    t_critical_df5 = 2.571
    threshold_95 = t_critical_df5 * se_hr_diff

    all_positive = all(d > 0 for d in hr_diffs)
    all_negative = all(d < 0 for d in hr_diffs)
    consistent = "一貫して改善" if all_positive else ("一貫して悪化" if all_negative else "fold間で不一致")

    print(
        f"\n的中率差: 平均{mean_hr_diff * 100:+.2f}pt, 標準偏差{std_hr_diff * 100:.2f}pt, "
        f"標準誤差(平均){se_hr_diff * 100:.2f}pt (n={n})"
    )
    print(f"全{n}fold中、改善={sum(1 for d in hr_diffs if d > 0)}件 / 悪化={sum(1 for d in hr_diffs if d < 0)}件 → {consistent}")
    print(
        f"有意性の目安(両側95%, t(df={n - 1})={t_critical_df5}): "
        f"|平均差| > {threshold_95 * 100:.2f}pt なら偶然とは言いにくい"
    )
    if abs(mean_hr_diff) > threshold_95:
        print(f"-> 平均差{mean_hr_diff * 100:+.2f}ptは目安({threshold_95 * 100:.2f}pt)を超えており、有意とみなせる")
    else:
        print(f"-> 平均差{mean_hr_diff * 100:+.2f}ptは目安({threshold_95 * 100:.2f}pt)以内で、誤差の範囲と考えるべき")
    print(
        "  (注: advantage(=的中率-lane1率)ベースで同じ比較をしても、同一fold内で"
        "lane1率の項が厳密に相殺するため、上記と完全に同じ差分・同じ判定になる。"
        "advantageの価値はこの比較の検出力ではなく、fold単体の解釈性にある)"
    )
    print(
        f"  (注意: 上記の有意性の目安はfold独立を前提にしており、本モジュールの"
        f"expanding window設計ではfold間に学習データの重複があるため相関しうる。"
        f"0.26pt等の閾値は下限として扱い、「全fold一貫して同方向」という条件も"
        f"必ず併用すること)"
    )

    # 学習データ量(train_races)と的中率差に相関があるかの簡易チェック。
    # expanding window設計では train_races は暦時間の経過と完全に相関する
    # （foldが進むほど両方とも単調増加）ため、ここで見つかる相関は
    # 「データ量の効果」と「暦時間の効果(季節性・経年変化等)」を
    # 区別できない点に注意。
    if show_data_volume_note:
        train_races = [ra.train_races for ra in results_a]
        if len(train_races) >= 3:
            corr = float(np.corrcoef(train_races, hr_diffs)[0, 1])
            print(
                f"\n学習データ量(train races)と{label_b}-{label_a}の的中率差の相関係数: "
                f"{corr:+.3f} (n={len(train_races)}。train racesはfoldが進むほど暦時間と"
                "ともに単調増加するため、データ量の効果と時間経過の効果は本設計では"
                "分離できない)"
            )

    return {
        "mean_diff": mean_hr_diff,
        "std_diff": std_hr_diff,
        "se_diff": se_hr_diff,
        "threshold": threshold_95,
        "all_consistent": all_positive or all_negative,
        "all_positive": all_positive,
        "significant": abs(mean_hr_diff) > threshold_95 and (all_positive or all_negative),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--feature-set",
        choices=["v3", "v5", "v6", "both", "v5_vs_v6"],
        default="both",
        help="v3/v5/v6/both(デフォルト、v3とv5を比較)/v5_vs_v6(v5とv6を比較)。"
        "--weight-sweep等ではv3/v5/v6のいずれか単体のみ指定可(bothとv5_vs_v6は不可)",
    )
    parser.add_argument("--num-boost-round", type=int, default=DEFAULT_NUM_BOOST_ROUND)
    parser.add_argument(
        "--weight-sweep",
        action="store_true",
        help="時間減衰サンプル重みの半減期候補(--half-life-months)を重み無しと比較する",
    )
    parser.add_argument(
        "--half-life-months",
        type=str,
        default="6,12,18,24",
        help="カンマ区切りのhalf-life候補(月)。--weight-sweep時のみ使用。"
        "1ヶ月=30日として日数に換算する",
    )
    parser.add_argument(
        "--decay",
        choices=["exponential", "linear", "step"],
        default="exponential",
        help="時間減衰の形。--weight-sweep時のみ使用（デフォルトexponential）",
    )
    parser.add_argument(
        "--boosting-compare",
        action="store_true",
        help="num_boost_round固定とearly stoppingを比較する(--feature-setはv3/v5単体のみ)",
    )
    parser.add_argument(
        "--early-stopping-rounds", type=int, default=DEFAULT_EARLY_STOPPING_ROUNDS,
        help="--boosting-compare時のearly_stopping_rounds",
    )
    parser.add_argument(
        "--inner-val-tail-days", type=int, default=DEFAULT_INNER_VAL_TAIL_DAYS,
        help="--boosting-compare時、学習窓末尾何日を内部検証に使うか",
    )
    parser.add_argument(
        "--random-search",
        action="store_true",
        help="ハイパーパラメータのランダムサーチを行う(--feature-setはv3/v5単体のみ)",
    )
    parser.add_argument(
        "--n-trials", type=int, default=40, help="--random-search時の試行回数",
    )
    parser.add_argument(
        "--search-seed", type=int, default=0, help="--random-search時の乱数シード",
    )
    parser.add_argument(
        "--boost-rounds",
        type=str,
        default=None,
        help="--random-search時のnum_boost_round。単一の整数、または"
        "fold番号:本数をカンマ区切りで指定(例: 1:150,2:180,...)。"
        "省略時は--num-boost-round(デフォルト200)を全fold共通で使う",
    )
    args = parser.parse_args(argv)

    if args.boosting_compare:
        if args.feature_set in ("both", "v5_vs_v6"):
            parser.error("--boosting-compare では --feature-set は v3/v5/v6 のいずれかを指定してください(bothとv5_vs_v6は不可)")

        fixed_results: list[FoldResult] = []
        es_results: list[FoldResult] = []
        best_iters: list[int] = []

        conn = get_connection()
        try:
            fetch_fn, feature_columns = FEATURE_SETS[args.feature_set]
            for i, (train_start, train_end, val_start, val_end) in enumerate(FOLDS, start=1):
                fixed_r, es_r, best_iter = run_fold_boosting_compare(
                    conn, i, train_start, train_end, val_start, val_end, feature_columns, fetch_fn,
                    num_boost_round_fixed=args.num_boost_round,
                    early_stopping_rounds=args.early_stopping_rounds,
                    inner_val_tail_days=args.inner_val_tail_days,
                )
                fixed_results.append(fixed_r)
                es_results.append(es_r)
                best_iters.append(best_iter)
                print(f"  [fold{i}] best_iteration(early stopping)={best_iter} (固定={args.num_boost_round})")
        finally:
            conn.close()

        print_fold_table(f"{args.feature_set}/固定{args.num_boost_round}本", fixed_results)
        print_fold_table(f"{args.feature_set}/early_stopping(本数={best_iters})", es_results)
        compare(
            f"固定{args.num_boost_round}本", fixed_results,
            "early_stopping", es_results, show_data_volume_note=False,
        )
        return 0

    if args.random_search:
        if args.feature_set in ("both", "v5_vs_v6"):
            parser.error("--random-search では --feature-set は v3/v5/v6 のいずれかを指定してください(bothとv5_vs_v6は不可)")

        if args.boost_rounds is None:
            boost_rounds: int | dict[int, int] = args.num_boost_round
        elif ":" in args.boost_rounds:
            boost_rounds = {
                int(part.split(":")[0]): int(part.split(":")[1])
                for part in args.boost_rounds.split(",")
            }
        else:
            boost_rounds = int(args.boost_rounds)

        conn = get_connection()
        try:
            trials = run_random_search(
                conn, args.feature_set, args.n_trials,
                boost_rounds=boost_rounds, seed=args.search_seed,
            )

            # ベースライン(DEFAULT_PARAMS、同じboost_roundsポリシー)も同条件で評価する。
            fetch_fn, feature_columns = FEATURE_SETS[args.feature_set]
            baseline_results: list[FoldResult] = []
            for i, (train_start, train_end, val_start, val_end) in enumerate(FOLDS, start=1):
                train_df = fetch_fn(conn, train_start, train_end)
                val_df = fetch_fn(conn, val_start, val_end)
                lane1_rate = dummy_lane1_hit_rate(val_df)
                nb = boost_rounds[i] if isinstance(boost_rounds, dict) else boost_rounds
                metrics = _train_and_eval(
                    train_df, val_df, feature_columns, num_boost_round=nb, sample_weight=None
                )
                baseline_results.append(
                    FoldResult(
                        fold=i, train_start=train_start, train_end=train_end,
                        val_start=val_start, val_end=val_end,
                        train_races=train_df.select(pl.col("race_id").n_unique()).item(),
                        val_races=val_df.select(pl.col("race_id").n_unique()).item(),
                        hit_rate=metrics["hit_rate"], lane1_hit_rate=lane1_rate,
                        log_loss=metrics["log_loss"], brier_score=metrics["brier_score"],
                        confident_count=metrics["confident_count"],
                        confident_hits=metrics["confident_hits"],
                        confident_accuracy=metrics["confident_accuracy"],
                    )
                )
        finally:
            conn.close()

        print_fold_table(f"{args.feature_set}/DEFAULT_PARAMS(ベースライン)", baseline_results)
        print_search_top_n(trials, n=5)

        ranked = sorted(trials, key=lambda t: t["mean_hit_rate"], reverse=True)
        best = ranked[0]
        print(f"\n=== 最良候補(trial{best['trial']}) vs DEFAULT_PARAMSベースライン ===")
        print(f"採用候補のパラメータ: {best['params']}")
        print_fold_table(f"{args.feature_set}/best(trial{best['trial']})", best["fold_results"])
        verdict = compare(
            "baseline", baseline_results, "best_candidate", best["fold_results"],
            show_data_volume_note=False,
        )
        print(
            f"\n見かけの改善幅(40試行中の最良値、選択バイアスを含む): "
            f"{(best['mean_hit_rate'] - float(np.mean([r.hit_rate for r in baseline_results]))) * 100:+.3f}pt"
        )
        print(
            f"選択バイアスを考慮した判定(baseline比、全fold一貫 かつ 実験ごとの目安超え): "
            f"{'有意' if verdict['significant'] else '誤差の範囲/不採用'}"
        )
        return 0

    if args.weight_sweep:
        if args.feature_set in ("both", "v5_vs_v6"):
            parser.error("--weight-sweep では --feature-set は v3/v5/v6 のいずれかを指定してください(bothとv5_vs_v6は不可)")

        half_life_months = [float(x) for x in args.half_life_months.split(",")]
        candidates: list[tuple[str, str | None, float | None]] = [("no_weight", None, None)] + [
            (f"half_life_{m:g}mo", args.decay, m * 30) for m in half_life_months
        ]

        conn = get_connection()
        try:
            fetch_fn, feature_columns = FEATURE_SETS[args.feature_set]
            results_by_label: dict[str, list[FoldResult]] = {label: [] for label, _, _ in candidates}
            for i, (train_start, train_end, val_start, val_end) in enumerate(FOLDS, start=1):
                fold_results = run_fold_weight_sweep(
                    conn, i, train_start, train_end, val_start, val_end, feature_columns, fetch_fn,
                    candidates, num_boost_round=args.num_boost_round,
                )
                for label, r in fold_results.items():
                    results_by_label[label].append(r)
        finally:
            conn.close()

        for label, results in results_by_label.items():
            print_fold_table(f"{args.feature_set}/{label}", results)

        baseline = results_by_label["no_weight"]
        verdicts = {}
        for label, _, _ in candidates[1:]:
            verdicts[label] = compare(
                "no_weight", baseline, label, results_by_label[label], show_data_volume_note=False
            )

        print("\n=== weight-sweep 総合判定（的中率差>0.26pt目安 かつ 全fold一貫） ===")
        any_significant = False
        for label, v in verdicts.items():
            mark = "効果あり" if v["significant"] else "効果なし"
            if v["significant"]:
                any_significant = True
            print(
                f"  {label}: 平均差{v['mean_diff'] * 100:+.2f}pt (目安{v['threshold'] * 100:.2f}pt), "
                f"全fold一貫={v['all_consistent']} -> {mark}"
            )
        if not any_significant:
            print(
                "\n-> 時間減衰重みは確立した判定ルールで「効果なし」。"
                "lane1勝率の変動はモデルの学習に影響しない程度のものだったと結論できる。"
            )

        return 0

    conn = get_connection()
    try:
        results_by_set: dict[str, list[FoldResult]] = {}
        if args.feature_set == "both":
            feature_sets = ["v3", "v5"]
        elif args.feature_set == "v5_vs_v6":
            feature_sets = ["v5", "v6"]
        else:
            feature_sets = [args.feature_set]
        for fs in feature_sets:
            results = run_walk_forward(conn, fs, num_boost_round=args.num_boost_round)
            results_by_set[fs] = results
            print_fold_table(f"{fs}特徴量セット", results)
    finally:
        conn.close()

    if args.feature_set == "both":
        compare("v3", results_by_set["v3"], "v5", results_by_set["v5"])
    elif args.feature_set == "v5_vs_v6":
        compare("v5", results_by_set["v5"], "v6", results_by_set["v6"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
