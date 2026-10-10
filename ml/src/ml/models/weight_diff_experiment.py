"""v6(体重差)特徴量の検証実験（2026-10-10）。

## 背景・第1段階(新規性調査)の結論

学習手続きの最適化（時間減衰重み・early stopping・ハイパーパラメータ探索
×2・top3正規化、いずれもnull）が打ち止めとなったため、新しい特徴量として
「節間成績」と「体重差」の2つを検討した。実装前の調査(コード変更なし)で
以下の通り判断した:

- **節間成績: 中止（新規性が乏しいと判断）**。v2_recentの「直近10走」は
  race_date基準の直近N走（全場・全節を問わない、race数ベースの窓）であり、
  節(meet)の区切りとは無関係に見える。しかし実測(2026-08の1ヶ月、29,592
  race_entries)で検証したところ、**day_of_meet(節の何日目か)を問わず、
  その節のこれまでの全レースが「直近10走」の窓に漏れなく含まれていた
  （重複率100%、0件の例外なし）**。理由は、競艇の節は最長でも7日程度・
  1日あたり1〜2走のため、節の累積レース数がv2_recentの窓(10走)を
  超えることが(今回の観測範囲では)無かったため。
  さらに「節のみで平均した値」と「直近10走(他の節のレースを含みうる
  希釈済み)の値」の乖離を見ると、day_of_meet=2(節内レース数平均1.49件)
  では乖離average|diff|=0.196・相関0.44と確かに値は違うが、これは
  単に標本数が1〜2件しかないことによる高分散（コイントスに近い）であり、
  day_of_meet=6(節内レース数平均7.78件)では乖離0.041・相関0.92まで収束し、
  既存特徴量とほぼ同じ値になる。つまり「節だけで分離した値」が既存特徴量と
  有意に違う区間は、その値自体が最も信頼できない（標本数が少ない）区間と
  完全に重なっており、**新規性と信頼性が両立する「使える区間」が存在
  しない**。これは学習手続きの最適化と同種（既存情報の重み付け変更に
  近い）の介入であり、本セッションのこれまでの実験(v4_stadium・
  ハイパーパラメータ探索等、いずれもnull)のパターンと整合する。
  そのため実装・検証に進まず中止した。

- **体重差: 新規性を確認、実装に進む**。v1_basic.weight(番組表発表時点の
  公表体重)とv5_exhibition.exhibit_weight(直前計量の実測値)の差分は
  既存のどの特徴量層にも存在しない（grep確認済み）。LightGBMは2列から差を
  学習できるが、木は単一特徴量で分割するため明示した方が分割しやすいという
  想定。adjusted_weight(調整重量)は体重そのものではなくクラス別規定重量に
  満たない場合のハンデ用おもりの重量であり別概念のため対象外とした。

## 第2段階: v6_weight_diffの実装・検証

`ml/src/ml/features/weight_diff.py`(feature_version='v6_weight_diff')で
`weight_diff_from_program = exhibit_weight - weight` の1特徴量を追加した。
既存のv1/v2/v3/v4/v5層は無変更。

比較: v1+v2+v3+v5(既存) vs v1+v2+v3+v5+v6。walk-forward(FOLDS、6fold)で
1着的中率・log loss・Brierを比較し、確立した判定ルール（実験ごとに
その場でペア差標準偏差から目安を算出 かつ 全6fold一貫）で判定する。
confident_top3は本番API定義(レースごとにp_top3最大の1艇、race_resultsが
ある艇のみ)で0.70〜0.99を0.01刻みで再スイープし、「実績90%を保ったまま
該当数を最大化する閾値」を比較する(top3_normalization_experimentの
sweep_thresholds/best_at_min_accuracyを再利用、重複実装しない)。
p_top3はis_winnerモデル→Plackett-Luce展開（本番の軸艇選定とは別モデル
(v3_top3直接学習)を使っているが、v5 vs v6の比較軸を揃えるため、ここでは
既存のv5 vs v3比較(CLAUDE.md「全期間バックフィル完了とv5の本番split検証」)
と同じPlackett-Luce方式で統一する）。

feature importanceはfold6(学習窓が最大、2023-09-01〜2026-06-30)の
boosterで確認する。
"""

from __future__ import annotations

import argparse

import polars as pl

from ml.loaders.db import get_connection
from ml.models.baseline import dummy_lane1_hit_rate
from ml.models.lgbm import (
    DEFAULT_NUM_BOOST_ROUND,
    WITH_V5_FEATURE_COLUMNS,
    WITH_V6_FEATURE_COLUMNS,
    evaluate,
    feature_importance,
    fetch_v5_dataset,
    fetch_v6_dataset,
    predict_race_normalized,
    train_model,
)
from ml.models.top3 import top3_from_plackett_luce
from ml.models.top3_normalization_experiment import THRESHOLDS, best_at_min_accuracy, sweep_thresholds
from ml.models.walk_forward import FOLDS, FoldResult, compare, print_fold_table


def run_experiment(num_boost_round: int = DEFAULT_NUM_BOOST_ROUND) -> dict:
    """v5/v6それぞれについて、fold毎に1回だけ学習し、的中率系の結果(FoldResult)と
    confident_top3用のPlackett-Luce p_top3データフレームの両方をそこから作る
    （2回学習して重複コストをかけないため）。
    """
    conn = get_connection()
    fold_results: dict[str, list[FoldResult]] = {"v5": [], "v6": []}
    top3_dfs: dict[str, list[pl.DataFrame]] = {"v5": [], "v6": []}
    last_boosters: dict[str, object] = {}

    try:
        for i, (train_start, train_end, val_start, val_end) in enumerate(FOLDS, start=1):
            for fs, fetch_fn, cols in (
                ("v5", fetch_v5_dataset, WITH_V5_FEATURE_COLUMNS),
                ("v6", fetch_v6_dataset, WITH_V6_FEATURE_COLUMNS),
            ):
                train_df = fetch_fn(conn, train_start, train_end)
                val_df = fetch_fn(conn, val_start, val_end)

                booster = train_model(train_df, cols, num_boost_round=num_boost_round)
                winner_result = predict_race_normalized(booster, val_df, cols)
                metrics = evaluate(winner_result)
                top3_df = top3_from_plackett_luce(winner_result, val_df)
                top3_dfs[fs].append(top3_df)
                last_boosters[fs] = booster

                lane1_rate = dummy_lane1_hit_rate(val_df)
                # confident_countここでは使わない(閾値再スイープで別途算出する)ため0埋め。
                fold_results[fs].append(
                    FoldResult(
                        fold=i, train_start=train_start, train_end=train_end,
                        val_start=val_start, val_end=val_end,
                        train_races=train_df.select(pl.col("race_id").n_unique()).item(),
                        val_races=val_df.select(pl.col("race_id").n_unique()).item(),
                        hit_rate=metrics["hit_rate"], lane1_hit_rate=lane1_rate,
                        log_loss=metrics["log_loss"], brier_score=metrics["brier_score"],
                        confident_count=0, confident_hits=0, confident_accuracy=float("nan"),
                    )
                )
            print(f"  [fold{i}] done", flush=True)
    finally:
        conn.close()

    pooled_top3 = {fs: pl.concat(dfs) for fs, dfs in top3_dfs.items()}
    return {"fold_results": fold_results, "pooled_top3": pooled_top3, "last_boosters": last_boosters}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-boost-round", type=int, default=DEFAULT_NUM_BOOST_ROUND)
    args = parser.parse_args(argv)

    result = run_experiment(num_boost_round=args.num_boost_round)
    fold_results = result["fold_results"]
    pooled_top3 = result["pooled_top3"]
    last_boosters = result["last_boosters"]

    print_fold_table("v5(v1+v2+v3+v5)", fold_results["v5"])
    print_fold_table("v6(v1+v2+v3+v5+v6)", fold_results["v6"])
    verdict = compare("v5", fold_results["v5"], "v6", fold_results["v6"])

    print("\n=== confident_top3: 閾値再スイープ(0.70〜0.99、全6foldプール) ===")
    for fs in ("v5", "v6"):
        sweep = sweep_thresholds(pooled_top3[fs], THRESHOLDS)
        best = best_at_min_accuracy(sweep)
        print(f"\n--- {fs} ---")
        for row in sweep:
            print(
                f"  threshold={row['threshold']:.2f} count={row['count']:>6d} "
                f"accuracy={row['accuracy'] * 100:>6.2f}%"
            )
        if best:
            print(
                f"  -> 実績90%を保ったまま該当数を最大化する閾値: {best['threshold']:.2f} "
                f"(該当数={best['count']}, 実績={best['accuracy'] * 100:.2f}%)"
            )
        else:
            print("  -> 実績90%以上を満たす閾値が見つからなかった")

    print("\n=== feature importance: v6 (fold6 booster, gain上位20) ===")
    fi_v6 = feature_importance(last_boosters["v6"])
    for row in fi_v6.head(20).iter_rows(named=True):
        marker = " <-- v6新規" if row["feature"] == "weight_diff_from_program" else ""
        print(f"  {row['feature']:<40s} gain={row['gain']:>14.1f} split={row['split']:>6d}{marker}")

    rank = None
    for i, row in enumerate(fi_v6.iter_rows(named=True), start=1):
        if row["feature"] == "weight_diff_from_program":
            rank = i
            break
    print(
        f"\nweight_diff_from_programの重要度ランク(gain基準、全{fi_v6.height}特徴量中): "
        f"{rank if rank else '圏外/0件'}"
    )

    print(
        f"\n=== 総合判定(的中率、確立した判定ルール: 目安{verdict['threshold'] * 100:.2f}pt超 "
        f"かつ 全fold一貫) ===\n"
        f"平均差{verdict['mean_diff'] * 100:+.2f}pt, 全fold一貫={verdict['all_consistent']} "
        f"-> {'有意' if verdict['significant'] else '誤差の範囲/不採用'}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
