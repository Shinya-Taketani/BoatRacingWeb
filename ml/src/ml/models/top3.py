"""p_top3(3着以内)を直接二値分類で学習し、現行手法(p_first→Plackett-Luce展開)
と比較する。

A) 現行: v1+v2+v3でis_winner(1着)を学習したモデル → 予測p_firstを
   ml.models.tickets.plackett_luce_trifecta で3連単120通りに展開し、
   marginal_top_n(...,3)で艇ごとのp_top3を導出する。
B) 直接: v1+v2+v3は同じだが、目的変数をis_top3(finish_posが1,2,3の
   いずれか)に変えて直接二値分類で学習する。

特徴量・split・パラメータはAと完全に揃える(lgbm.ALL_FEATURE_COLUMNS,
本番splitと同じ学習2023-09-01〜2025-12-31/検証2026-01-01〜09-17、
lgbm.DEFAULT_PARAMS)。異なるのは目的変数(is_winner vs is_top3)だけ。

正規化: Aは1着予測を合計1に正規化(既存のpredict_race_normalizedと同じ)した
上でPlackett-Luce展開するため、導出されるp_top3は艇ごとに[0,1]の値を取り、
レース内6艇の合計は必ず3になる(marginal probabilityの性質)。Bは
is_top3の素のシグモイド出力(0〜1、艇間に制約なし)をレース内合計が3.0に
なるよう線形スケーリングする（3着以内は必ず3艇という制約を反映）。
この正規化では個々の値が1を超えることがあり得る(1艇に極端に予測が
偏った場合)。log loss/Brier計算時は[eps, 1-eps]にクリップし、
クリップが発生した件数を報告する。

confident_top3は本番API(PerformanceController::confidentTop3Overall)と
同じ定義(レースごとにp_top3最大の1艇のみ、race_resultsが存在する艇のみ
分母に含める)で評価する。
"""

from __future__ import annotations

import argparse
from datetime import date

import lightgbm as lgb
import numpy as np
import polars as pl

from ml.loaders.db import get_connection
from ml.models.lgbm import (
    ALL_FEATURE_COLUMNS,
    CATEGORICAL_FEATURES,
    DEFAULT_PARAMS,
    DEFAULT_NUM_BOOST_ROUND,
    V3_FEATURE_COLUMNS,
    V5_FEATURE_COLUMNS,
    WITH_V5_FEATURE_COLUMNS,
    feature_importance,
    fetch_all_dataset,
    fetch_v5_dataset,
    predict_race_normalized,
    train_model,
)
from ml.models.tickets import marginal_top_n, plackett_luce_trifecta

# --feature-set の選択肢。v3=現行(ALL_FEATURE_COLUMNS/fetch_all_dataset)、
# v5=v5_exhibitionを含む(WITH_V5_FEATURE_COLUMNS/fetch_v5_dataset)。
# fetch_v5_datasetはv5_exhibition特徴量をINNER JOINするため、v5の無い
# レースは丸ごと落ちる(レース数が学習129,684/検証40,692と一致するか
# 必ず確認すること)。
FEATURE_SETS = {
    "v3": (fetch_all_dataset, ALL_FEATURE_COLUMNS),
    "v5": (fetch_v5_dataset, WITH_V5_FEATURE_COLUMNS),
}

TRAIN_START = "2023-09-01"
TRAIN_END = "2025-12-31"
VAL_START = "2026-01-01"
VAL_END = "2026-09-17"

TARGET_COLUMN = "is_top3"
SWEEP_THRESHOLDS = [0.90, 0.92, 0.94, 0.96, 0.98]
EPS = 1e-15


def add_is_top3(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        pl.col("finish_pos").is_in([1, 2, 3]).fill_null(False).cast(pl.Int8).alias(TARGET_COLUMN)
    )


def _to_xy(df: pl.DataFrame, feature_columns: list[str]) -> tuple:
    X = df.select([pl.col(c).cast(pl.Float64) for c in feature_columns]).to_numpy()
    y = df.select(TARGET_COLUMN).to_series().to_numpy()
    return X, y


def train_top3_model(
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


def predict_race_sum3(
    booster: lgb.Booster, df: pl.DataFrame, feature_columns: list[str]
) -> pl.DataFrame:
    """is_top3の素の予測をレース内合計3.0に正規化し、p_top3列として付与する。"""
    X, _ = _to_xy(df, feature_columns)
    raw_pred = booster.predict(X)

    result = df.select(
        ["race_id", "lane", "finish_pos", "has_result_row", TARGET_COLUMN]
    ).with_columns(pl.Series("raw_pred", raw_pred))
    return result.with_columns(
        (pl.col("raw_pred") / pl.col("raw_pred").sum().over("race_id") * 3.0).alias("p_top3")
    )


def predict_race_sum3_for_inference(
    booster: lgb.Booster, df: pl.DataFrame, feature_columns: list[str]
) -> tuple[pl.DataFrame, int]:
    """本番推論用。df は race_id/lane/特徴量列だけでよい(is_top3等のラベル列は
    不要。predict_race_sum3は評価用で検証ラベル列を前提にしているため別に
    用意する)。

    素のシグモイド出力は艇間に制約が無いため、レース内合計3.0への正規化後に
    個々の値が[0,1]を超えることがある(2026-10-03の検証で244,152件中1,323件
    =0.54%で発生)。本番では単純に[0,1]へclipする。「3着以内に入る確率」という
    表示上の意味を保つ以上、1を超える値をそのまま出すわけにはいかないが、
    他に正規化方法を精緻化する根拠もまだ無いため、まずは単純clipで対応し、
    clipが発生した件数を呼び出し側がログに出せるよう返す。
    """
    X = df.select([pl.col(c).cast(pl.Float64) for c in feature_columns]).to_numpy()
    raw_pred = booster.predict(X)

    result = df.select(["race_id", "lane"]).with_columns(pl.Series("raw_pred", raw_pred))
    result = result.with_columns(
        (pl.col("raw_pred") / pl.col("raw_pred").sum().over("race_id") * 3.0).alias("p_top3_raw")
    )
    clipped_count = int(
        result.select(((pl.col("p_top3_raw") < 0) | (pl.col("p_top3_raw") > 1)).sum()).item()
    )
    result = result.with_columns(pl.col("p_top3_raw").clip(0.0, 1.0).alias("p_top3"))
    return result.select(["race_id", "lane", "p_top3"]), clipped_count


def top3_from_plackett_luce(winner_result: pl.DataFrame, val_df: pl.DataFrame) -> pl.DataFrame:
    """is_winnerモデルのpred_prob(p_first)からPlackett-Luce展開でp_top3を導出する。"""
    info_by_key = {
        (row["race_id"], row["lane"]): (row["finish_pos"], row["has_result_row"])
        for row in val_df.select(
            ["race_id", "lane", "finish_pos", "has_result_row"]
        ).iter_rows(named=True)
    }

    rows = []
    for race_id, race_rows in winner_result.group_by("race_id", maintain_order=True):
        race_id = race_id[0] if isinstance(race_id, tuple) else race_id
        p_first = dict(zip(race_rows["lane"].to_list(), race_rows["pred_prob"].to_list()))
        perm_probs = plackett_luce_trifecta(p_first)
        p_top3 = marginal_top_n(perm_probs, 3)
        for lane, p in p_top3.items():
            finish_pos, has_result_row = info_by_key[(race_id, lane)]
            rows.append(
                {
                    "race_id": race_id,
                    "lane": lane,
                    "finish_pos": finish_pos,
                    "has_result_row": has_result_row,
                    TARGET_COLUMN: 1 if finish_pos in (1, 2, 3) else 0,
                    "p_top3": p,
                }
            )

    return pl.DataFrame(rows)


def per_boat_top3_selection_accuracy(df: pl.DataFrame) -> dict:
    """レースごとにp_top3上位3艇を選び、的中艇数(実際も3着以内)÷予測艇数を求める。"""
    picks = df.with_columns(
        pl.col("p_top3").rank(method="ordinal", descending=True).over("race_id").alias("rnk")
    ).filter(pl.col("rnk") <= 3)
    predicted = picks.height
    hits = picks.select(pl.col(TARGET_COLUMN).sum()).item()
    return {"predicted": predicted, "hits": hits, "accuracy": hits / predicted if predicted else float("nan")}


def confident_top3_production(df: pl.DataFrame, threshold: float) -> dict:
    """本番API(confidentTop3Overall)と同じ定義: レースごとにp_top3最大の1艇のみを
    候補にし、その艇の値が閾値以上かつrace_resultsが存在する場合のみ分母に含める。
    """
    count = 0
    hits = 0
    for _race_id, race_rows in df.group_by("race_id", maintain_order=True):
        top = race_rows.sort("p_top3", descending=True).row(0, named=True)
        if top["p_top3"] < threshold:
            continue
        if not top["has_result_row"]:
            continue
        count += 1
        if top[TARGET_COLUMN]:
            hits += 1
    return {"count": count, "hits": hits, "accuracy": hits / count if count else float("nan")}


def binary_log_loss_and_brier(df: pl.DataFrame) -> dict:
    """is_top3を2値ラベルとして、艇単位(レースでグループ化しない)でlog loss/Brierを
    計算する。p_top3は[0,1]を超えることがあるためクリップし、クリップ発生件数を
    報告する(目安: B(直接学習)でどれだけ正規化が[0,1]を外れたか)。
    """
    p = df["p_top3"].to_numpy()
    y = df[TARGET_COLUMN].to_numpy().astype(float)
    clipped_count = int(np.sum((p < 0) | (p > 1)))
    p_clipped = np.clip(p, EPS, 1 - EPS)
    log_loss = float(-np.mean(y * np.log(p_clipped) + (1 - y) * np.log(1 - p_clipped)))
    brier = float(np.mean((p_clipped - y) ** 2))
    return {"log_loss": log_loss, "brier": brier, "clipped_count": clipped_count, "n": len(p)}


def collect_top_candidates(df: pl.DataFrame) -> pl.DataFrame:
    """レースごとにp_top3最大の1艇だけを集める（本番APIのrnk=1と同じ単位）。
    has_result_rowの判定はここでは行わず、呼び出し側で絞る
    (本番APIも「ランキングは全艇対象、分母に入れるかはその1艇の結果有無で
    判定」なので、ランキング自体はresult有無に関わらず決める)。
    """
    rows = [
        race_rows.sort("p_top3", descending=True).row(0, named=True)
        for _race_id, race_rows in df.group_by("race_id", maintain_order=True)
    ]
    return pl.DataFrame(rows)


def confident_top3_at_count(candidates: pl.DataFrame, target_count: int) -> dict:
    """候補(collect_top_candidatesの出力)から、has_result_rowが真の艇のうち
    p_top3が高い方からtarget_count件を選んだ場合の実績を返す
    （「この件数になる閾値」を逆算して本番定義と一致させる）。
    """
    valid = candidates.filter(pl.col("has_result_row")).sort("p_top3", descending=True)
    selected = valid.head(target_count)
    count = selected.height
    hits = int(selected.select(pl.col(TARGET_COLUMN).sum()).item()) if count else 0
    threshold = float(selected["p_top3"][-1]) if count else None
    return {
        "threshold": threshold,
        "count": count,
        "hits": hits,
        "accuracy": hits / count if count else float("nan"),
    }


def calibration_deciles(df: pl.DataFrame, label: str) -> pl.DataFrame:
    """has_result_rowが真の全艇(confident_top3に絞らない全体)をp_top3で10分位
    (quantile、艇数が均等になるよう分割)し、各ビンの予測平均と実績3着以内率を
    比較する。
    """
    valid = df.filter(pl.col("has_result_row"))
    with_bin = valid.with_columns(
        pl.col("p_top3").qcut(10, labels=[str(i) for i in range(1, 11)]).alias("bin")
    )
    agg = (
        with_bin.group_by("bin")
        .agg(
            pl.len().alias("n"),
            pl.col("p_top3").min().alias("p_min"),
            pl.col("p_top3").max().alias("p_max"),
            pl.col("p_top3").mean().alias("predicted_mean"),
            pl.col(TARGET_COLUMN).mean().alias("actual_rate"),
        )
        .sort("p_min")  # 予測確率の低い方から表示する（qcutの内部ラベル順ではない）
        .with_columns(pl.int_range(1, pl.len() + 1).alias("bin"))
    )

    print(f"\n--- キャリブレーション(10分位): {label} ---")
    print(f"{'bin':>4s} {'範囲':>17s} {'n':>8s} {'予測平均':>10s} {'実績':>8s} {'差(実績-予測)':>14s}")
    for row in agg.iter_rows(named=True):
        diff = row["actual_rate"] - row["predicted_mean"]
        print(
            f"{row['bin']:>4d} {row['p_min']:.3f}-{row['p_max']:.3f} {row['n']:>8d} "
            f"{row['predicted_mean'] * 100:>9.2f}% {row['actual_rate'] * 100:>7.2f}% {diff * 100:>+13.2f}pt"
        )
    return agg


def _days_in_period(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


MARGIN_THRESHOLD = 0.003  # 0.3pt。月次変動で容易に割り込まない安全マージンの下限。


def low_threshold_sweep(
    b_df: pl.DataFrame, thresholds: list[float], days: int
) -> None:
    """Bをthresholds(降順ではなく指定順)でスイープし、該当数・実績・1日あたり件数を
    出す。90%を下回る境界、90%を保ったまま該当数を最大化する閾値に加え、
    「90%までのマージンがMARGIN_THRESHOLD以上」を満たす閾値のうち該当数が
    最大のものも報告する（単純な90%ラインぎりぎりだと月次変動で容易に割り込む
    ため、運用上はこちらをより重視する）。
    """
    print("\n=== Bの閾値スイープ（下方向拡張、本番API定義） ===")
    print(f"{'threshold':>10s}  {'該当数':>8s}  {'実績':>8s}  {'1日あたり':>10s}")

    results = []
    for threshold in thresholds:
        r = confident_top3_production(b_df, threshold)
        acc_str = f"{r['accuracy'] * 100:.2f}%" if r["count"] else "該当なし"
        per_day = r["count"] / days if r["count"] else 0.0
        print(f"{threshold:>10.2f}  {r['count']:>8d}  {acc_str:>8s}  {per_day:>10.2f}")
        results.append((threshold, r["count"], r["accuracy"]))

    # thresholdは降順(大→小)で渡される想定。小さい閾値ほど該当数が増え、
    # 実績が下がっていく傾向を前提に、90%を下回る最初の境界を探す。
    boundary = None
    for i in range(1, len(results)):
        prev_th, _prev_count, prev_acc = results[i - 1]
        th, count, acc = results[i]
        if prev_acc >= 0.90 and (not count or acc < 0.90):
            boundary = (prev_th, th)
            break

    over_90 = [r for r in results if r[1] and r[2] >= 0.90]
    if over_90:
        best = max(over_90, key=lambda r: r[1])
        best_th, best_count, best_acc = best
        print(
            f"\n90%を保ったまま該当数を最大化する閾値: {best_th:.2f} "
            f"(該当数={best_count}, 実績={best_acc * 100:.2f}%, "
            f"1日あたり={best_count / days:.2f}艇)"
        )
    else:
        print("\nスイープした範囲内で実績90%以上の閾値は見つからなかった")

    if boundary:
        print(f"実績が90%を下回る境界: 閾値{boundary[0]:.2f}(>=90%) と {boundary[1]:.2f}(<90%)の間")
    else:
        print("スイープした範囲内では90%を下回る境界は見つからなかった")

    margin_ok = [r for r in results if r[1] and (r[2] - 0.90) >= MARGIN_THRESHOLD]
    if margin_ok:
        best_m_th, best_m_count, best_m_acc = max(margin_ok, key=lambda r: r[1])
        print(
            f"\nマージン{MARGIN_THRESHOLD * 100:.1f}pt以上"
            f"(実績{(0.90 + MARGIN_THRESHOLD) * 100:.1f}%以上)で該当数を最大化する閾値: "
            f"{best_m_th:.2f} (該当数={best_m_count}, 実績={best_m_acc * 100:.2f}%, "
            f"1日あたり={best_m_count / days:.2f}艇)"
        )
    else:
        print(
            f"\nスイープした範囲内でマージン{MARGIN_THRESHOLD * 100:.1f}pt以上"
            "の閾値は見つからなかった"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-boost-round", type=int, default=DEFAULT_NUM_BOOST_ROUND)
    parser.add_argument(
        "--low-sweep",
        action="store_true",
        help="Aの学習・比較をスキップし、Bのみ0.70〜0.96を--sweep-step刻みでスイープする",
    )
    parser.add_argument(
        "--sweep-step",
        type=float,
        default=0.02,
        help="--low-sweepの閾値刻み幅（デフォルト0.02。0.01等でより細かく刻める）",
    )
    parser.add_argument(
        "--deep-dive",
        action="store_true",
        help="該当数を揃えた厳密比較とキャリブレーション(10分位)を追加で出す",
    )
    parser.add_argument(
        "--feature-set",
        choices=["v3", "v5"],
        default="v3",
        help=(
            "v3(デフォルト): ALL_FEATURE_COLUMNS/fetch_all_dataset(現行と同じ)。"
            "v5: WITH_V5_FEATURE_COLUMNS/fetch_v5_dataset(v5_exhibitionを含む。"
            "INNER JOINのためv5の無いレースは落ちる)"
        ),
    )
    args = parser.parse_args(argv)

    fetch_fn, feature_columns = FEATURE_SETS[args.feature_set]

    conn = get_connection()
    try:
        train_df = fetch_fn(conn, TRAIN_START, TRAIN_END)
        val_df = fetch_fn(conn, VAL_START, VAL_END)
    finally:
        conn.close()

    train_df = add_is_top3(train_df)
    val_df = add_is_top3(val_df)

    print(
        f"train ({TRAIN_START}..{TRAIN_END}): races={train_df.select(pl.col('race_id').n_unique()).item()} "
        f"rows={train_df.height}"
    )
    print(
        f"val   ({VAL_START}..{VAL_END}): races={val_df.select(pl.col('race_id').n_unique()).item()} "
        f"rows={val_df.height}"
    )

    # B) 直接: is_top3モデル（--low-sweepでもAとの比較本編でも必ず使う）
    top3_booster = train_top3_model(train_df, feature_columns, num_boost_round=args.num_boost_round)
    b_df = predict_race_sum3(top3_booster, val_df, feature_columns)

    if args.low_sweep:
        days = _days_in_period(VAL_START, VAL_END)
        step = args.sweep_step
        n_steps = round((0.96 - 0.70) / step) + 1
        thresholds = [round(0.96 - step * i, 2) for i in range(n_steps)]  # 0.96, ..., 0.70
        low_threshold_sweep(b_df, thresholds, days)
        return 0

    # A) 現行: is_winnerモデル → Plackett-Luce展開
    winner_booster = train_model(train_df, feature_columns, num_boost_round=args.num_boost_round)
    winner_result = predict_race_normalized(winner_booster, val_df, feature_columns)
    a_df = top3_from_plackett_luce(winner_result, val_df)

    print("\n=== 艇単位の3着以内的中率(上位3艇選択) ===")
    for label, df in [("A) 現行(Plackett-Luce)", a_df), ("B) 直接学習", b_df)]:
        r = per_boat_top3_selection_accuracy(df)
        print(
            f"  {label}: 的中艇数={r['hits']} / 予測艇数={r['predicted']} "
            f"的中率={r['accuracy']:.4f} ({r['accuracy'] * 100:.2f}%)"
        )

    print("\n=== log loss / Brier (is_top3を2値ラベルとした艇単位評価) ===")
    for label, df in [("A) 現行(Plackett-Luce)", a_df), ("B) 直接学習", b_df)]:
        r = binary_log_loss_and_brier(df)
        print(
            f"  {label}: log loss={r['log_loss']:.4f} Brier={r['brier']:.4f} "
            f"([0,1]外へのクリップ={r['clipped_count']}/{r['n']})"
        )

    print("\n=== confident_top3 (本番API定義、閾値0.96) ===")
    for label, df in [("A) 現行(Plackett-Luce)", a_df), ("B) 直接学習", b_df)]:
        r = confident_top3_production(df, 0.96)
        print(
            f"  {label}: 該当数={r['count']} 的中={r['hits']} "
            f"実績的中率={r['accuracy']:.4f} ({r['accuracy'] * 100:.2f}%)"
            if r["count"]
            else f"  {label}: 該当なし"
        )

    print("\n=== 閾値スイープ (confident_top3、本番API定義) ===")
    print(f"{'threshold':>10s}  {'A:該当数':>10s}  {'A:実績':>8s}  {'B:該当数':>10s}  {'B:実績':>8s}")
    b_min_threshold_over_90 = None
    for threshold in SWEEP_THRESHOLDS:
        ra = confident_top3_production(a_df, threshold)
        rb = confident_top3_production(b_df, threshold)
        a_acc = f"{ra['accuracy'] * 100:.2f}%" if ra["count"] else "該当なし"
        b_acc = f"{rb['accuracy'] * 100:.2f}%" if rb["count"] else "該当なし"
        print(f"{threshold:>10.2f}  {ra['count']:>10d}  {a_acc:>8s}  {rb['count']:>10d}  {b_acc:>8s}")
        if rb["count"] and rb["accuracy"] > 0.90 and b_min_threshold_over_90 is None:
            b_min_threshold_over_90 = (threshold, rb["count"], rb["accuracy"])

    if b_min_threshold_over_90:
        th, cnt, acc = b_min_threshold_over_90
        print(
            f"\nB(直接学習)で実績的中率が90%を超える最小の閾値: {th:.2f} "
            f"(該当数={cnt}, 実績的中率={acc * 100:.2f}%)"
        )
    else:
        print("\nB(直接学習)はスイープした閾値の範囲内で実績的中率90%を超えなかった")

    if args.deep_dive:
        a_candidates = collect_top_candidates(a_df)
        b_candidates = collect_top_candidates(b_df)

        print("\n=== 該当数を揃えた厳密比較（同じ件数になる閾値同士） ===")
        for label, target_count, a_ref_acc in [
            ("A=0.96(22,428件)相当", 22428, 0.9037),
            ("A=0.98(13,212件)相当", 13212, 0.9246),
            ("A=0.92(30,670件)相当", 30670, 0.8754),
            ("A=0.90(33,179件)相当", 33179, 0.8669),
        ]:
            ra = confident_top3_at_count(a_candidates, target_count)
            rb = confident_top3_at_count(b_candidates, target_count)
            print(f"\n  -- {label} --")
            print(
                f"    A: 件数={ra['count']} 閾値>={ra['threshold']:.4f} "
                f"実績={ra['accuracy'] * 100:.2f}% (参考値{a_ref_acc * 100:.2f}%)"
            )
            print(
                f"    B: 件数={rb['count']} 閾値>={rb['threshold']:.4f} "
                f"実績={rb['accuracy'] * 100:.2f}%"
            )
            print(f"    差分(B-A): {(rb['accuracy'] - ra['accuracy']) * 100:+.2f}pt")

        calibration_deciles(a_df, "A) 現行(Plackett-Luce)")
        calibration_deciles(b_df, "B) 直接学習")

    print("\n=== feature importance: A(is_winner) vs B(is_top3) ===")
    importance_a = feature_importance(winner_booster)
    importance_b = feature_importance(top3_booster)
    rank_a = {row["feature"]: i + 1 for i, row in enumerate(importance_a.iter_rows(named=True))}
    rank_b = {row["feature"]: i + 1 for i, row in enumerate(importance_b.iter_rows(named=True))}
    print(f"  {'feature':<32s} {'A順位':>6s} {'A gain':>14s}  {'B順位':>6s} {'B gain':>14s}")
    gain_a = {row["feature"]: row["gain"] for row in importance_a.iter_rows(named=True)}
    gain_b = {row["feature"]: row["gain"] for row in importance_b.iter_rows(named=True)}
    for feature in sorted(feature_columns, key=lambda f: rank_b.get(f, 999)):
        tag = " [v3]" if feature in V3_FEATURE_COLUMNS else " [v5]" if feature in V5_FEATURE_COLUMNS else ""
        print(
            f"  {feature:<32s} {rank_a.get(feature, '-'):>6} {gain_a.get(feature, 0):>14.1f}  "
            f"{rank_b.get(feature, '-'):>6} {gain_b.get(feature, 0):>14.1f}{tag}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
