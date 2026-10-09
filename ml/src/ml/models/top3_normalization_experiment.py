"""top3モデル(is_top3)の正規化方式を3通り比較する(2026-10-09)。

背景: 現行の正規化(レース内合計3.0への線形スケーリング→[0,1]へclip)は、
244,152件中1,318件(0.54%)でclipが発生し、それが「最も確信度の高い艇」に
集中する。1.16/1.09/1.03のような値がすべて1.0に潰れ、confident_top3が
レースごとにp_top3最大の1艇を選ぶ際に順位情報が失われうる。

比較する3方式（同じboosterから算出、学習は1回のみ）:
  A) 現行: 線形スケーリング + clip(0,1)
  B) 線形スケーリング（clipなし、[0,1]超えをそのまま）
  C) ロジット空間での平行移動(ml.models.top3.predict_race_logit_shift)

BはAとの差分から「clipの有害性」単体を見るために入れている。A=clip(B)なので
A/Bは常にレース内順位が一致し、Cは単調変換(全艇に同じdeltaを加算)のため
B/Cも理論上レース内順位が一致するはずで、実際に一致するかを確認する
（本スクリプトのcheck_rank_preserved）。

walk-forward(ml.models.walk_forward.FOLDS、6fold、v1+v2+v3)でis_top3モデルを
fold毎に学習し、各fold検証窓のpredictionsを3方式で算出した上で全6fold分を
プールし(84,336レース相当)、本番API定義のconfident_top3(レースごとに
p_top3最大の1艇、race_resultsがある艇のみ)で0.70〜0.99を0.01刻みで
再スイープする。閾値そのものの選定はプール済みの大きな検証集合に対する
記述的な最適化であり、既存のwalk-forward式の有意性判定(fold独立性を前提に
その場でペア差標準偏差から目安を算出する方式)とは性質が異なるため、
fold別の「選んだ閾値での的中率」は参考情報として併記するに留める。
"""

from __future__ import annotations

import argparse
from datetime import date

import numpy as np
import polars as pl

from ml.loaders.db import get_connection
from ml.models.lgbm import ALL_FEATURE_COLUMNS, DEFAULT_NUM_BOOST_ROUND, fetch_all_dataset
from ml.models.top3 import (
    TARGET_COLUMN,
    add_is_top3,
    calibration_deciles,
    predict_race_logit_shift,
    predict_race_sum3,
    train_top3_model,
)
import random as _random

from ml.models.walk_forward import FOLDS, sample_params

THRESHOLDS = [round(0.70 + 0.01 * i, 2) for i in range(30)]  # 0.70..0.99
MIN_ACCURACY = 0.90


def check_rank_preserved(df_a: pl.DataFrame, df_b: pl.DataFrame, col_a: str, col_b: str) -> dict:
    """race_idごとにcol_a(df_a)とcol_b(df_b)での艇の順位が一致するかを確認する。
    同点(タイ)は元の行順で安定ソートされるため、df_a/df_bが同じ元データ
    (lane昇順)由来であれば同点部分の扱いも揃う。
    """
    def ranking(df: pl.DataFrame, col: str) -> pl.DataFrame:
        return (
            df.sort(["race_id", col], descending=[False, True])
            .group_by("race_id", maintain_order=True)
            .agg(pl.col("lane"))
        )

    r_a = ranking(df_a, col_a)
    r_b = ranking(df_b, col_b)
    joined = r_a.join(r_b, on="race_id", suffix="_b")
    mismatches = joined.filter(pl.col("lane") != pl.col("lane_b"))
    return {"total_races": joined.height, "mismatches": mismatches.height}


def sweep_thresholds(df: pl.DataFrame, thresholds: list[float]) -> list[dict]:
    """confident_top3_production(本番API定義: レースごとにp_top3最大の1艇、
    has_result_rowが真の艇のみ)と同じ結果を返すが、rank()をpolarsで1回だけ
    計算してから閾値ごとに単純フィルタするベクトル化版。confident_top3_production
    をそのまま30閾値×3方式=90回呼ぶとPython側のgroup_byループが積み重なって
    著しく遅い(1呼び出し約1秒×90回×規模6倍で動作確認、結果はconfident_top3_production
    と全閾値で完全一致することを確認済み)ため、この関数を使う。
    """
    top_picks = (
        df.with_columns(
            pl.col("p_top3").rank(method="ordinal", descending=True).over("race_id").alias("rnk")
        )
        .filter((pl.col("rnk") == 1) & pl.col("has_result_row"))
    )
    rows = []
    for th in thresholds:
        sub = top_picks.filter(pl.col("p_top3") >= th)
        count = sub.height
        hits = int(sub.select(pl.col(TARGET_COLUMN).sum()).item()) if count else 0
        rows.append(
            {"threshold": th, "count": count, "hits": hits, "accuracy": hits / count if count else float("nan")}
        )
    return rows


def best_at_min_accuracy(sweep: list[dict], min_accuracy: float = MIN_ACCURACY) -> dict | None:
    candidates = [r for r in sweep if r["count"] and r["accuracy"] >= min_accuracy]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r["count"])


def _total_val_days(folds: list[tuple[str, str, str, str]]) -> int:
    total = 0
    for _, _, val_start, val_end in folds:
        total += (date.fromisoformat(val_end) - date.fromisoformat(val_start)).days + 1
    return total


def run_experiment(num_boost_round: int = DEFAULT_NUM_BOOST_ROUND) -> dict[str, pl.DataFrame]:
    """fold毎にis_top3モデルを学習し、3方式(A/B/C)それぞれのpooled DataFrame
    (全6fold分の検証データを縦結合したもの)を返す。
    """
    pooled: dict[str, list[pl.DataFrame]] = {"A": [], "B": [], "C": []}
    rank_checks_ab = []
    rank_checks_bc = []

    conn = get_connection()
    try:
        for i, (train_start, train_end, val_start, val_end) in enumerate(FOLDS, start=1):
            train_df = fetch_all_dataset(conn, train_start, train_end)
            val_df = fetch_all_dataset(conn, val_start, val_end)
            train_df = add_is_top3(train_df)
            val_df = add_is_top3(val_df)

            booster = train_top3_model(train_df, ALL_FEATURE_COLUMNS, num_boost_round=num_boost_round)

            b_df = predict_race_sum3(booster, val_df, ALL_FEATURE_COLUMNS)
            a_df = b_df.with_columns(pl.col("p_top3").clip(0.0, 1.0))
            c_df = predict_race_logit_shift(booster, val_df, ALL_FEATURE_COLUMNS)

            rc_ab = check_rank_preserved(a_df, b_df, "p_top3", "p_top3")
            rc_bc = check_rank_preserved(b_df, c_df, "p_top3", "p_top3")
            rank_checks_ab.append(rc_ab)
            rank_checks_bc.append(rc_bc)
            print(
                f"  [fold{i}] rank match A/B: {rc_ab['total_races'] - rc_ab['mismatches']}/{rc_ab['total_races']} "
                f"  B/C: {rc_bc['total_races'] - rc_bc['mismatches']}/{rc_bc['total_races']}",
                flush=True,
            )

            pooled["A"].append(a_df)
            pooled["B"].append(b_df)
            pooled["C"].append(c_df)
    finally:
        conn.close()

    print("\n=== 順位保存チェック(全fold合計) ===")
    total_ab = sum(r["total_races"] for r in rank_checks_ab)
    mismatch_ab = sum(r["mismatches"] for r in rank_checks_ab)
    total_bc = sum(r["total_races"] for r in rank_checks_bc)
    mismatch_bc = sum(r["mismatches"] for r in rank_checks_bc)
    print(f"  A vs B: 不一致 {mismatch_ab}/{total_ab} レース（clipによる同値化のみのはず）")
    print(f"  B vs C: 不一致 {mismatch_bc}/{total_bc} レース（0のはず、単調変換のため）")

    return {label: pl.concat(dfs) for label, dfs in pooled.items()}


# --- 第2段階: top3モデルのハイパーパラメータ探索 ----------------------------
# 第1段階の結論（A=B、Cは該当数がむしろ減り明確な優位なし）を受け、正規化は
# 現行(A、線形スケーリング+clip)のまま、ハイパーパラメータだけを探索する。
# 探索範囲はwinnerモデルの探索(ml.models.walk_forward.PARAM_SEARCH_RANGES/
# sample_params)をそのまま再利用する(同じLightGBM二値分類モデルのため)。
# 主指標は「閾値を再スイープした上で実績90%での該当数」(1着的中率ではない)。


def _fold_level_a_dfs(num_boost_round: int, params: dict | None = None) -> list[pl.DataFrame]:
    """fold毎にis_top3モデルを学習し、方式A(線形+clip)のDataFrameを
    fold別のリストで返す(pooled済みの1本にまとめない版)。
    run_random_search_top3のbaseline算出とfold別内訳の両方に使う。
    """
    conn = get_connection()
    try:
        dfs = []
        for train_start, train_end, val_start, val_end in FOLDS:
            train_df = fetch_all_dataset(conn, train_start, train_end)
            val_df = fetch_all_dataset(conn, val_start, val_end)
            train_df = add_is_top3(train_df)
            val_df = add_is_top3(val_df)
            booster = train_top3_model(
                train_df, ALL_FEATURE_COLUMNS, num_boost_round=num_boost_round, params=params
            )
            b_df = predict_race_sum3(booster, val_df, ALL_FEATURE_COLUMNS)
            dfs.append(b_df.with_columns(pl.col("p_top3").clip(0.0, 1.0)))
    finally:
        conn.close()
    return dfs


def run_random_search_top3(
    n_trials: int, *, num_boost_round: int = DEFAULT_NUM_BOOST_ROUND, seed: int = 0
) -> list[dict]:
    """n_trials個のパラメータをランダムサンプリングし、is_top3モデルを
    fold毎に学習、方式A(線形+clip)でpoolingした上で閾値スイープし、
    「実績90%を保ったまま該当数最大化」をtrialのスコアとする。
    """
    rng = _random.Random(seed)
    param_list = [sample_params(rng) for _ in range(n_trials)]

    trial_pooled: list[list[pl.DataFrame]] = [[] for _ in range(n_trials)]
    trial_fold_best: list[list[dict | None]] = [[] for _ in range(n_trials)]

    conn = get_connection()
    try:
        for i, (train_start, train_end, val_start, val_end) in enumerate(FOLDS, start=1):
            train_df = fetch_all_dataset(conn, train_start, train_end)
            val_df = fetch_all_dataset(conn, val_start, val_end)
            train_df = add_is_top3(train_df)
            val_df = add_is_top3(val_df)

            for t, params in enumerate(param_list):
                booster = train_top3_model(
                    train_df, ALL_FEATURE_COLUMNS, num_boost_round=num_boost_round, params=params
                )
                b_df = predict_race_sum3(booster, val_df, ALL_FEATURE_COLUMNS)
                a_df = b_df.with_columns(pl.col("p_top3").clip(0.0, 1.0))
                trial_pooled[t].append(a_df)

                fold_sweep = sweep_thresholds(a_df, THRESHOLDS)
                trial_fold_best[t].append(best_at_min_accuracy(fold_sweep))
                print(
                    f"  [fold{i} trial{t}] fold単体の該当数={trial_fold_best[t][-1]['count'] if trial_fold_best[t][-1] else 0}",
                    flush=True,
                )
    finally:
        conn.close()

    days = _total_val_days(FOLDS)
    trials = []
    for t, params in enumerate(param_list):
        pooled_df = pl.concat(trial_pooled[t])
        sweep = sweep_thresholds(pooled_df, THRESHOLDS)
        best = best_at_min_accuracy(sweep)
        trials.append(
            {
                "trial": t,
                "params": params,
                "best": best,
                "fold_best": trial_fold_best[t],
                "days": days,
            }
        )
    return trials


def print_search_top_n(trials: list[dict], n: int = 5) -> None:
    ranked = sorted(
        trials, key=lambda t: (t["best"]["count"] if t["best"] else -1), reverse=True
    )
    print(f"\n=== top3モデル ランダムサーチ 上位{n}件(該当数降順、実績90%以上条件) ===")
    print(f"{'順位':>4s} {'trial':>6s} {'該当数':>8s} {'実績':>8s} {'閾値':>6s}")
    for rank, t in enumerate(ranked[:n], start=1):
        b = t["best"]
        if b is None:
            print(f"{rank:>4d} {t['trial']:>6d}  該当する閾値なし")
            continue
        print(f"{rank:>4d} {t['trial']:>6d} {b['count']:>8d} {b['accuracy']*100:>7.2f}% {b['threshold']:>6.2f}")

    valid = [t for t in ranked if t["best"] is not None]
    if len(valid) >= 2:
        c1 = valid[0]["best"]["count"]
        c2 = valid[1]["best"]["count"]
        cn = valid[min(n, len(valid)) - 1]["best"]["count"]
        print(
            f"\n1位と2位の該当数差: {c1 - c2:+d}件 / 1位と{min(n, len(valid))}位の差: {c1 - cn:+d}件 "
            "(僅差=団子状態なら、1位は試行回数分のノイズの最大値に過ぎず偶然の可能性が高い)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-boost-round", type=int, default=DEFAULT_NUM_BOOST_ROUND)
    parser.add_argument(
        "--random-search",
        action="store_true",
        help="第2段階: top3モデルのハイパーパラメータをランダムサーチする(正規化は方式Aに固定)",
    )
    parser.add_argument("--n-trials", type=int, default=20, help="--random-search時の試行回数")
    parser.add_argument("--search-seed", type=int, default=0, help="--random-search時の乱数シード")
    args = parser.parse_args(argv)

    if args.random_search:
        trials = run_random_search_top3(
            args.n_trials, num_boost_round=args.num_boost_round, seed=args.search_seed
        )
        days = _total_val_days(FOLDS)

        # ベースライン(DEFAULT_PARAMS、方式A)。fold別dfも保持し、
        # 全fold一貫チェックにそのまま使う。
        baseline_fold_dfs = _fold_level_a_dfs(args.num_boost_round, params=None)
        baseline_fold_best = [best_at_min_accuracy(sweep_thresholds(df, THRESHOLDS)) for df in baseline_fold_dfs]
        baseline_pooled = pl.concat(baseline_fold_dfs)
        baseline_best = best_at_min_accuracy(sweep_thresholds(baseline_pooled, THRESHOLDS))

        print("\n=== ベースライン(DEFAULT_PARAMS、方式A) ===")
        if baseline_best:
            print(
                f"閾値{baseline_best['threshold']:.2f} 該当数={baseline_best['count']} "
                f"実績={baseline_best['accuracy']*100:.2f}% (1日あたり={baseline_best['count']/days:.2f}艇)"
            )

        print_search_top_n(trials, n=5)

        ranked = sorted(trials, key=lambda t: (t["best"]["count"] if t["best"] else -1), reverse=True)
        best_trial = ranked[0]
        if best_trial["best"] is None or baseline_best is None:
            print("\n-> 比較不能(該当する閾値が見つからない候補があるため)。不採用。")
            return 0

        print(f"\n=== 最良候補(trial{best_trial['trial']}) vs ベースライン ===")
        print(f"採用候補のパラメータ: {best_trial['params']}")
        print(
            f"ベースライン: 該当数={baseline_best['count']} 実績={baseline_best['accuracy']*100:.2f}%"
        )
        print(
            f"最良候補:     該当数={best_trial['best']['count']} 実績={best_trial['best']['accuracy']*100:.2f}%"
        )
        count_diff = best_trial["best"]["count"] - baseline_best["count"]
        print(f"見かけの改善幅(該当数): {count_diff:+d}件")

        # 全fold一貫チェック: fold単体(そのfoldの検証窓のみ)でthreshold sweepを
        # 独立に行い、「実績90%での最良該当数」をfoldごとに比較する。
        # 1着的中率の時の「同一閾値でのペア差」とは異なり、各fold・各候補で
        # 独立に最適閾値を選んだ上での該当数比較である点に注意(hyperparameter
        # 探索の目的=運用時の該当数そのものを最大化することに合わせた定義)。
        print("\n=== 全fold一貫チェック(fold単体で独立にsweepした該当数) ===")
        improved = 0
        for i in range(len(FOLDS)):
            b_count = baseline_fold_best[i]["count"] if baseline_fold_best[i] else 0
            t_count = (
                best_trial["fold_best"][i]["count"] if best_trial["fold_best"][i] else 0
            )
            mark = "改善" if t_count > b_count else ("同等" if t_count == b_count else "悪化")
            print(f"  fold{i + 1}: baseline={b_count} best_trial={t_count} -> {mark}")
            if t_count > b_count:
                improved += 1
        print(f"\n{len(FOLDS)}fold中{improved}foldで改善 -> {'全fold一貫' if improved == len(FOLDS) else '不一致'}")

        return 0

    pooled = run_experiment(num_boost_round=args.num_boost_round)
    days = _total_val_days(FOLDS)
    print(f"\n検証窓合計日数: {days}日 (6fold分、重複なし)")

    labels = {"A": "A) 現行(線形+clip)", "B": "B) 線形(clipなし)", "C": "C) ロジット平行移動"}

    best_by_method = {}
    for key, label in labels.items():
        df = pooled[key]
        sweep = sweep_thresholds(df, THRESHOLDS)
        print(f"\n=== {label}: 閾値スイープ(0.70〜0.99、全6foldプール{df['race_id'].n_unique()}レース) ===")
        print(f"{'threshold':>10s}  {'該当数':>8s}  {'実績':>8s}")
        for r in sweep:
            acc_str = f"{r['accuracy'] * 100:.2f}%" if r["count"] else "該当なし"
            print(f"{r['threshold']:>10.2f}  {r['count']:>8d}  {acc_str:>8s}")

        best = best_at_min_accuracy(sweep)
        if best is None:
            print(f"  -> 実績{MIN_ACCURACY*100:.0f}%以上を満たす閾値が範囲内に見つからなかった")
            best_by_method[key] = None
        else:
            per_day = best["count"] / days
            print(
                f"  -> 実績{MIN_ACCURACY*100:.0f}%を保ったまま該当数を最大化する閾値: "
                f"{best['threshold']:.2f} (該当数={best['count']}, 実績={best['accuracy']*100:.2f}%, "
                f"1日あたり={per_day:.2f}艇)"
            )
            best_by_method[key] = best

        calibration_deciles(df, label)

    print("\n=== 主指標: 同じ実績(90%前後)での該当数比較(現行A@0.84=22,646件・90.78%が基準) ===")
    for key, label in labels.items():
        b = best_by_method[key]
        if b is None:
            print(f"  {label}: 該当閾値なし")
        else:
            print(
                f"  {label}: 閾値{b['threshold']:.2f} 該当数={b['count']} 実績={b['accuracy']*100:.2f}%"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
