"""v5_exhibition特徴量(直前情報由来)の効果検証。

データが存在する期間(2026-06-21〜09-20)のみで検証するため、baseline.pyの
学習期間(2023-09-01〜2025-12-31)/検証期間(2026-01-01〜09-17)とは別に、
この実験専用の時系列split(学習: 06-21〜08-20 / 検証: 08-21〜09-20)を使う。
本番モデルの学習期間・検証期間はこのスクリプトの影響を受けない。

比較:
  A) v1_basic + v2_recent + v3_relative のみ
  B) v1_basic + v2_recent + v3_relative + v5_exhibition

同一split・同一パラメータ(lgbm.DEFAULT_PARAMS, num_boost_round)で学習し、
的中率/log loss/Brierを比較する。さらに、p_first(1着確率)から
Plackett-Luce展開でp_top3(3着以内確率)を求め、p_top3>=0.96(「3着以内90%
保証」の閾値。CLAUDE.md「p_top3の精度検証」参照)に該当する艇の該当数・
実績的中率がv5追加でどう変わるかを比較する。
"""

from __future__ import annotations

import argparse

import polars as pl

from ml.loaders.db import get_connection
from ml.models.lgbm import (
    ALL_FEATURE_COLUMNS,
    V3_FEATURE_COLUMNS,
    V5_FEATURE_COLUMNS,
    WITH_V5_FEATURE_COLUMNS,
    RunResult,
    evaluate,
    feature_importance,
    fetch_all_dataset,
    fetch_v5_dataset,
    missing_rate_report,
    predict_race_normalized,
    train_model,
)
from ml.models.tickets import marginal_top_n, plackett_luce_trifecta

DEFAULT_TRAIN_START = "2026-06-21"
DEFAULT_TRAIN_END = "2026-08-20"
DEFAULT_VAL_START = "2026-08-21"
DEFAULT_VAL_END = "2026-09-20"

CONFIDENT_TOP3_THRESHOLD = 0.96


def _run(label: str, train_df: pl.DataFrame, val_df: pl.DataFrame, feature_columns: list[str],
         num_boost_round: int) -> RunResult:
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


def _confident_top3_report(
    label: str, val_df: pl.DataFrame, result: pl.DataFrame, threshold: float
) -> dict:
    """result(race_id, lane, is_winner, pred_prob)をp_firstとしてPlackett-Luce展開し、
    「軸艇(confident_top3)」該当数・実績的中率を求める。

    本番API(PerformanceController::confidentTop3Overall)と厳密に同じ定義に
    揃えてある(2026-10-03、89.38%/90.37%の食い違い調査で判明した2点の
    メソッド差異を修正):
    1. レースごとに「p_top3が最大の1艇」だけを対象にする
       (本番SQLの`rank() OVER (PARTITION BY race_id ORDER BY p_top3 DESC)`
       `WHERE rnk = 1`と同じ)。当初はp_top3>=thresholdを満たす艇を
       レース内の順位を無視して全艇カウントしており、同一レースで複数艇が
       閾値を超えるケースを余分に数えて母数・的中率の両方がズレていた。
    2. race_resultsの行が存在しない(結果未確定・未送信等)艇は分母からも
       除外する(本番SQLはrace_resultsにINNER JOINしているため)。
       finish_pos IS NULL のうち「失格でNULL」と「行自体が無い」を
       has_result_row(fetch_all_dataset/fetch_v5_datasetで追加)で区別する。
    """
    info_by_key = {
        (row["race_id"], row["lane"]): (row["finish_pos"], row["has_result_row"])
        for row in val_df.select(["race_id", "lane", "finish_pos", "has_result_row"]).iter_rows(
            named=True
        )
    }

    count = 0
    hits = 0
    total_races = 0
    for race_id, race_rows in result.group_by("race_id", maintain_order=True):
        race_id = race_id[0] if isinstance(race_id, tuple) else race_id
        total_races += 1
        p_first = dict(zip(race_rows["lane"].to_list(), race_rows["pred_prob"].to_list()))
        perm_probs = plackett_luce_trifecta(p_first)
        p_top3 = marginal_top_n(perm_probs, 3)

        top_lane = max(p_top3, key=p_top3.get)
        if p_top3[top_lane] < threshold:
            continue

        finish_pos, has_result_row = info_by_key[(race_id, top_lane)]
        if not has_result_row:
            continue  # 本番と同じくrace_results行が無い艇は分母からも除外

        count += 1
        if finish_pos in (1, 2, 3):
            hits += 1

    hit_rate = hits / count if count else float("nan")

    print(f"\n--- confident_top3 (p_top3>={threshold:.2f}) : {label} ---")
    print(f"該当数: {count} / {total_races}レース")
    print(f"実績的中率: {hit_rate:.4f} ({hit_rate * 100:.2f}%)" if count else "該当なし")

    return {"count": count, "hit_rate": hit_rate}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-boost-round", type=int, default=200)
    parser.add_argument("--train-start", default=DEFAULT_TRAIN_START)
    parser.add_argument("--train-end", default=DEFAULT_TRAIN_END)
    parser.add_argument("--val-start", default=DEFAULT_VAL_START)
    parser.add_argument("--val-end", default=DEFAULT_VAL_END)
    args = parser.parse_args(argv)

    train_start, train_end = args.train_start, args.train_end
    val_start, val_end = args.val_start, args.val_end

    conn = get_connection()
    try:
        a_train_df = fetch_all_dataset(conn, train_start, train_end)
        a_val_df = fetch_all_dataset(conn, val_start, val_end)
        b_train_df = fetch_v5_dataset(conn, train_start, train_end)
        b_val_df = fetch_v5_dataset(conn, val_start, val_end)
    finally:
        conn.close()

    print(
        f"train ({train_start}..{train_end}): "
        f"races={a_train_df.select(pl.col('race_id').n_unique()).item()} "
        f"rows(A:v1+v2+v3)={a_train_df.height} rows(B:v1+v2+v3+v5)={b_train_df.height}"
    )
    print(
        f"val   ({val_start}..{val_end}): "
        f"races={a_val_df.select(pl.col('race_id').n_unique()).item()} "
        f"rows(A:v1+v2+v3)={a_val_df.height} rows(B:v1+v2+v3+v5)={b_val_df.height}"
    )

    run_a = _run("A) v1+v2+v3", a_train_df, a_val_df, ALL_FEATURE_COLUMNS, args.num_boost_round)
    run_b = _run(
        "B) v1+v2+v3+v5", b_train_df, b_val_df, WITH_V5_FEATURE_COLUMNS, args.num_boost_round
    )

    # C) Bと同じbooster(v5ありで学習)を使い、推論時だけv5列を全てNaNにして評価する。
    # ライブ取得が止まっている間にv5モデルを本番投入した場合の挙動を模する
    # （2026-10-03、ライブ再開前にv5モデルを投入してよいか判断するための検証）。
    c_val_df = b_val_df.with_columns(
        [pl.lit(None).cast(pl.Float64).alias(c) for c in V5_FEATURE_COLUMNS]
    )
    result_c = predict_race_normalized(run_b.booster, c_val_df, WITH_V5_FEATURE_COLUMNS)
    run_c = RunResult(
        label="C) v1+v2+v3+v5モデル、推論時v5=NaN",
        metrics=evaluate(result_c),
        result=result_c,
        booster=run_b.booster,
    )

    print(f"\n=== 評価指標比較(検証期間 {val_start}〜{val_end}) ===")
    _print_metrics(run_a)
    _print_metrics(run_b)
    _print_metrics(run_c)
    print(
        f"\n差分(B-A): 的中率={run_b.metrics['hit_rate'] - run_a.metrics['hit_rate']:+.4f} "
        f"log loss={run_b.metrics['log_loss'] - run_a.metrics['log_loss']:+.5f} "
        f"Brier={run_b.metrics['brier_score'] - run_a.metrics['brier_score']:+.5f}"
    )
    print(
        f"差分(C-A): 的中率={run_c.metrics['hit_rate'] - run_a.metrics['hit_rate']:+.4f} "
        f"log loss={run_c.metrics['log_loss'] - run_a.metrics['log_loss']:+.5f} "
        f"Brier={run_c.metrics['brier_score'] - run_a.metrics['brier_score']:+.5f}"
    )

    print("\n=== feature importance (B: v1+v2+v3+v5) ===")
    for row in feature_importance(run_b.booster).iter_rows(named=True):
        tag = (
            " [v5]" if row["feature"] in V5_FEATURE_COLUMNS
            else " [v3]" if row["feature"] in V3_FEATURE_COLUMNS
            else ""
        )
        print(f"  {row['feature']:<32s} gain={row['gain']:>14.1f} split={row['split']}{tag}")

    print("\n=== 欠損率(v5特徴量、検証期間) ===")
    for row in missing_rate_report(b_val_df, V5_FEATURE_COLUMNS).iter_rows(named=True):
        print(
            f"  {row['feature']:<32s} missing={row['missing_count']} "
            f"rate={row['missing_rate']:.4f} ({row['missing_rate'] * 100:.2f}%)"
        )

    print(f"\n=== confident_top3 (p_top3>={CONFIDENT_TOP3_THRESHOLD:.2f}) 比較 ===")
    report_a = _confident_top3_report(
        "A) v1+v2+v3", a_val_df, run_a.result, CONFIDENT_TOP3_THRESHOLD
    )
    report_b = _confident_top3_report(
        "B) v1+v2+v3+v5", b_val_df, run_b.result, CONFIDENT_TOP3_THRESHOLD
    )
    report_c = _confident_top3_report(
        "C) v1+v2+v3+v5モデル、推論時v5=NaN", c_val_df, run_c.result, CONFIDENT_TOP3_THRESHOLD
    )
    print(
        f"\n差分(B-A): 該当数={report_b['count'] - report_a['count']:+d} "
        f"実績的中率={report_b['hit_rate'] - report_a['hit_rate']:+.4f}"
    )
    print(
        f"差分(C-A): 該当数={report_c['count'] - report_a['count']:+d} "
        f"実績的中率={report_c['hit_rate'] - report_a['hit_rate']:+.4f}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
