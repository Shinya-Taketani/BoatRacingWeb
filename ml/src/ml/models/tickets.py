"""3連単の買い目生成。

1. Plackett-Luce展開: p_first(6艇, 合計1)から3連単120通りの確率を計算する。
   P(i→j→k) = p_i * p_j/(1-p_i) * p_k/(1-p_i-p_j)

2. 展開結果からp_top2(2着以内)/p_top3(3着以内)を各艇について集計する。
   prediction_entries は挿入後、親predictionのpublished_atが設定されている
   限りUPDATE不可（不変化トリガー）なので、p_top2/p_top3は
   predict.py が prediction_entries を「挿入する時点」で一緒に計算・確定
   させる必要がある。本モジュールの plackett_luce_trifecta/marginal_top_n
   を predict.py から呼び出す。

3. race_confidence: p_firstのエントロピー(低いほど確率が特定艇に集中=
   予測しやすい)を算出する。S/A/B/見送りの閾値は実データでの的中率との
   クロス集計をもとに決めるため、analyze_confidence_distribution() で
   まず分布と的中率の関係を確認できるようにしてある。

4. 累積確率が閾値を超えるまで買い目(確率降順)を積み、
   prediction_tickets に書き込む。
"""

from __future__ import annotations

import argparse
import itertools
from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl
import psycopg

from ml.loaders.db import get_connection
from ml.models.baseline import VAL_END, VAL_START

BET_TYPE = "3連単"
DEFAULT_CUM_PROB_THRESHOLD = 0.50
_LN6 = float(np.log(6))


# --- 1. Plackett-Luce展開 -----------------------------------------------

def plackett_luce_trifecta(
    p_first: dict[int, float],
) -> list[tuple[tuple[int, int, int], float]]:
    """6艇のp_firstから3連単120通りの確率を計算し、確率降順で返す。"""
    lanes = sorted(p_first)
    results: list[tuple[tuple[int, int, int], float]] = []
    for i, j, k in itertools.permutations(lanes, 3):
        pi, pj, pk = p_first[i], p_first[j], p_first[k]
        denom2 = 1.0 - pi
        denom3 = 1.0 - pi - pj
        # 6艇正規化済みのp_firstなら通常起きないが、数値誤差でpiやpi+pjが
        # ほぼ1になるケースへの防御（0確率として扱う。負値を作らない）。
        prob = 0.0 if denom2 <= 1e-12 or denom3 <= 1e-12 else pi * (pj / denom2) * (pk / denom3)
        results.append(((i, j, k), prob))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def plackett_luce_exacta(
    p_first: dict[int, float],
) -> list[tuple[tuple[int, int], float]]:
    """6艇のp_firstから2連単30通りの確率を計算し、確率降順で返す。
    P(i→j) = p_i * p_j/(1-p_i)（3連単展開の3着分を周辺化したものと一致）。
    """
    lanes = sorted(p_first)
    results: list[tuple[tuple[int, int], float]] = []
    for i, j in itertools.permutations(lanes, 2):
        pi, pj = p_first[i], p_first[j]
        denom = 1.0 - pi
        prob = 0.0 if denom <= 1e-12 else pi * (pj / denom)
        results.append(((i, j), prob))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def marginal_top_n(
    perm_probs: list[tuple[tuple[int, int, int], float]], n: int
) -> dict[int, float]:
    """3連単の展開結果から、各艇がtop-n(1〜n着)に入る確率を集計する。"""
    totals: dict[int, float] = {}
    for combo, prob in perm_probs:
        for lane in combo[:n]:
            totals[lane] = totals.get(lane, 0.0) + prob
    return totals


def race_entropy(p_first: dict[int, float]) -> tuple[float, float]:
    """(エントロピー(nats), 正規化エントロピー(0-1, ln(6)で割った値))を返す。"""
    ps = np.array([p for p in p_first.values() if p > 0])
    h = float(-np.sum(ps * np.log(ps)))
    return h, h / _LN6


# --- 3. race_confidence（分布の可視化。閾値はここから決める） -----------

def fetch_p_first_and_hit(
    conn: psycopg.Connection, model_version: str, stage: int, start_date: date, end_date: date
) -> pl.DataFrame:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pe.prediction_id, pe.lane, pe.p_first, pj.hit
            FROM prediction_entries pe
            JOIN predictions p ON p.id = pe.prediction_id
            JOIN races r ON r.id = p.race_id
            LEFT JOIN prediction_judgments pj ON pj.prediction_id = p.id
            WHERE p.model_version = %s AND p.stage = %s
              AND r.race_date BETWEEN %s AND %s
            """,
            (model_version, stage, start_date, end_date),
        )
        rows = cur.fetchall()
    return pl.DataFrame(
        rows, schema=["prediction_id", "lane", "p_first", "hit"], orient="row"
    )


def analyze_confidence_distribution(df: pl.DataFrame, n_bins: int = 10) -> pl.DataFrame:
    """予測(レース)ごとの正規化エントロピーを計算し、的中率とのクロス集計を
    n_bins等分位(件数が揃う分位ビン)で返す。閾値決定の材料にする。
    """
    per_race = (
        df.with_columns((-pl.col("p_first") * pl.col("p_first").log()).alias("neg_p_log_p"))
        .group_by("prediction_id")
        .agg(
            pl.col("neg_p_log_p").sum().alias("entropy"),
            pl.col("hit").first().alias("hit"),
        )
        .with_columns((pl.col("entropy") / _LN6).alias("normalized_entropy"))
        .filter(pl.col("hit").is_not_null())  # 未判定(結果未確定)は除外
        .sort("normalized_entropy")
    )

    total = per_race.height
    per_race = per_race.with_columns(
        (pl.arange(0, total) * n_bins // total).alias("bin")
    )

    return (
        per_race.group_by("bin")
        .agg(
            pl.len().alias("n"),
            pl.col("normalized_entropy").min().alias("entropy_min"),
            pl.col("normalized_entropy").max().alias("entropy_max"),
            pl.col("hit").cast(pl.Int32).mean().alias("hit_rate"),
        )
        .sort("bin")
    )


def propose_confidence_thresholds(per_race_binned: pl.DataFrame) -> dict[str, float]:
    """分位ビンの結果から、S/A/B/見送りの初期閾値案(正規化エントロピーの
    上限値)を四分位で機械的に決める。あくまで初期案であり、実運用前に
    要調整。
    """
    entropy_max_values = per_race_binned.sort("bin")["entropy_max"].to_list()
    n = len(entropy_max_values)
    return {
        "S": entropy_max_values[max(0, n // 4 - 1)],
        "A": entropy_max_values[max(0, n // 2 - 1)],
        "B": entropy_max_values[max(0, 3 * n // 4 - 1)],
        # これを超えるものは「見送り」
    }


def grade_confidence(normalized_entropy: float, thresholds: dict[str, float]) -> str:
    if normalized_entropy <= thresholds["S"]:
        return "S"
    if normalized_entropy <= thresholds["A"]:
        return "A"
    if normalized_entropy <= thresholds["B"]:
        return "B"
    return "見送り"


# --- 4. prediction_tickets への書き込み ----------------------------------

@dataclass(frozen=True)
class TicketGenerationResult:
    predictions: int
    tickets: int


def _fetch_predictions_with_p_first(
    conn: psycopg.Connection, model_version: str, stage: int, target_date: date
) -> dict[int, dict[int, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pe.prediction_id, pe.lane, pe.p_first
            FROM prediction_entries pe
            JOIN predictions p ON p.id = pe.prediction_id
            JOIN races r ON r.id = p.race_id
            WHERE p.model_version = %s AND p.stage = %s AND r.race_date = %s
            ORDER BY pe.prediction_id, pe.lane
            """,
            (model_version, stage, target_date),
        )
        rows = cur.fetchall()

    by_prediction: dict[int, dict[int, float]] = {}
    for prediction_id, lane, p_first in rows:
        by_prediction.setdefault(prediction_id, {})[lane] = float(p_first)
    return by_prediction


def _tickets_until_threshold(
    perm_probs: list[tuple[tuple[int, int, int], float]], cum_prob_threshold: float
) -> list[tuple[tuple[int, int, int], float, int]]:
    """確率降順のperm_probsから、累積確率がthresholdを超えるまで買い目を積む。
    (combo, prob, rank) のリストを返す（rank=1が最有力）。
    """
    picked = []
    cum = 0.0
    for rank, (combo, prob) in enumerate(perm_probs, start=1):
        picked.append((combo, prob, rank))
        cum += prob
        if cum >= cum_prob_threshold:
            break
    return picked


def _existing_ticket_prediction_ids(conn: psycopg.Connection, prediction_ids: list[int]) -> set[int]:
    if not prediction_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT prediction_id FROM prediction_tickets WHERE prediction_id = ANY(%s)",
            (prediction_ids,),
        )
        return {row[0] for row in cur.fetchall()}


def generate_tickets_for_date(
    conn: psycopg.Connection,
    model_version: str,
    stage: int,
    target_date: date,
    *,
    cum_prob_threshold: float = DEFAULT_CUM_PROB_THRESHOLD,
) -> TicketGenerationResult:
    by_prediction = _fetch_predictions_with_p_first(conn, model_version, stage, target_date)
    return _write_tickets(conn, by_prediction, cum_prob_threshold=cum_prob_threshold)


def _fetch_predictions_with_p_first_range(
    conn: psycopg.Connection, model_version: str, stage: int, start_date: date, end_date: date
) -> dict[int, dict[int, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pe.prediction_id, pe.lane, pe.p_first
            FROM prediction_entries pe
            JOIN predictions p ON p.id = pe.prediction_id
            JOIN races r ON r.id = p.race_id
            WHERE p.model_version = %s AND p.stage = %s AND r.race_date BETWEEN %s AND %s
            ORDER BY pe.prediction_id, pe.lane
            """,
            (model_version, stage, start_date, end_date),
        )
        rows = cur.fetchall()

    by_prediction: dict[int, dict[int, float]] = {}
    for prediction_id, lane, p_first in rows:
        by_prediction.setdefault(prediction_id, {})[lane] = float(p_first)
    return by_prediction


def _write_tickets(
    conn: psycopg.Connection,
    by_prediction: dict[int, dict[int, float]],
    *,
    cum_prob_threshold: float,
    batch_size: int = 500,
    progress: bool = False,
) -> TicketGenerationResult:
    if not by_prediction:
        return TicketGenerationResult(predictions=0, tickets=0)

    prediction_ids = list(by_prediction)
    already = _existing_ticket_prediction_ids(conn, prediction_ids)
    target_ids = [pid for pid in prediction_ids if pid not in already]

    tickets_written = 0
    with conn.cursor() as cur:
        for start in range(0, len(target_ids), batch_size):
            chunk = target_ids[start : start + batch_size]
            values_sql_parts: list[str] = []
            params: list[object] = []
            for prediction_id in chunk:
                perm_probs = plackett_luce_trifecta(by_prediction[prediction_id])
                picked = _tickets_until_threshold(perm_probs, cum_prob_threshold)
                for combo, prob, rank in picked:
                    combination = "-".join(str(lane) for lane in combo)
                    values_sql_parts.append("(%s, %s, %s, %s, %s)")
                    params.extend([prediction_id, BET_TYPE, combination, prob, rank])
                    tickets_written += 1

            if not values_sql_parts:
                continue

            cur.execute(
                f"""
                INSERT INTO prediction_tickets (prediction_id, bet_type, combination, est_prob, rank)
                VALUES {', '.join(values_sql_parts)}
                """,
                params,
            )
            conn.commit()
            if progress:
                print(
                    f"  progress: {min(start + batch_size, len(target_ids))}/{len(target_ids)} "
                    f"predictions, tickets={tickets_written}",
                    flush=True,
                )

    return TicketGenerationResult(predictions=len(target_ids), tickets=tickets_written)


def generate_tickets_for_range(
    conn: psycopg.Connection,
    model_version: str,
    stage: int,
    start_date: date,
    end_date: date,
    *,
    cum_prob_threshold: float = DEFAULT_CUM_PROB_THRESHOLD,
    batch_size: int = 500,
    progress: bool = False,
) -> TicketGenerationResult:
    by_prediction = _fetch_predictions_with_p_first_range(conn, model_version, stage, start_date, end_date)
    return _write_tickets(
        conn, by_prediction, cum_prob_threshold=cum_prob_threshold, batch_size=batch_size, progress=progress
    )


# --- 回収率（実際の払戻金、payoutsテーブル由来） --------------------------

def fetch_race_stake_payout(
    conn: psycopg.Connection,
    model_version: str,
    stage: int,
    target_date: date,
    end_date: date | None = None,
) -> pl.DataFrame:
    """3連単の購入額(点数×100円)と実際の払戻額をレース単位で集計する。

    end_dateを指定すると[target_date, end_date]の範囲で集計する
    （race_dateも一緒に返すので月別集計に使える）。
    """
    range_end = end_date if end_date is not None else target_date
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id AS prediction_id,
                   r.race_date,
                   count(t.id) AS n_tickets,
                   count(t.id) * 100 AS stake,
                   COALESCE(sum(po.payout), 0) AS payout
            FROM predictions p
            JOIN prediction_tickets t ON t.prediction_id = p.id
            JOIN races r ON r.id = p.race_id
            LEFT JOIN payouts po
                ON po.race_id = p.race_id AND po.bet_type = %s AND po.combination = t.combination
            WHERE p.model_version = %s AND p.stage = %s AND r.race_date BETWEEN %s AND %s
            GROUP BY p.id, r.race_date
            """,
            (BET_TYPE, model_version, stage, target_date, range_end),
        )
        rows = cur.fetchall()
    return pl.DataFrame(
        rows, schema=["prediction_id", "race_date", "n_tickets", "stake", "payout"], orient="row"
    )


def overall_recovery(stake_payout: pl.DataFrame) -> dict:
    total_stake = int(stake_payout["stake"].sum())
    total_payout = int(stake_payout["payout"].sum())
    return {
        "tickets": int(stake_payout["n_tickets"].sum()),
        "total_stake": total_stake,
        "total_payout": total_payout,
        "recovery_rate": total_payout / total_stake if total_stake else float("nan"),
    }


def recovery_by_confidence(
    conn: psycopg.Connection,
    model_version: str,
    stage: int,
    target_date: date,
    n_bins: int = 10,
    end_date: date | None = None,
) -> pl.DataFrame:
    """エントロピー分位ごとに購入額・払戻額・回収率・的中率を集計する。

    end_dateを指定すると[target_date, end_date]の範囲で集計する。
    """
    range_end = end_date if end_date is not None else target_date
    p_first_by_prediction = _fetch_predictions_with_p_first_range(
        conn, model_version, stage, target_date, range_end
    )
    stake_payout = fetch_race_stake_payout(conn, model_version, stage, target_date, range_end)

    entropy_rows = []
    for prediction_id, p_first in p_first_by_prediction.items():
        _, hn = race_entropy(p_first)
        entropy_rows.append({"prediction_id": prediction_id, "normalized_entropy": hn})
    entropy_df = pl.DataFrame(entropy_rows)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT pj.prediction_id, pj.hit
            FROM prediction_judgments pj
            JOIN predictions p ON p.id = pj.prediction_id
            JOIN races r ON r.id = p.race_id
            WHERE p.model_version = %s AND p.stage = %s AND r.race_date BETWEEN %s AND %s
            """,
            (model_version, stage, target_date, range_end),
        )
        hit_rows = cur.fetchall()
    hit_df = pl.DataFrame(hit_rows, schema=["prediction_id", "hit"], orient="row")

    merged = (
        entropy_df.join(stake_payout, on="prediction_id", how="inner")
        .join(hit_df, on="prediction_id", how="left")
        .sort("normalized_entropy")
    )

    total = merged.height
    merged = merged.with_columns((pl.arange(0, total) * n_bins // total).alias("bin"))

    return (
        merged.group_by("bin")
        .agg(
            pl.len().alias("n"),
            pl.col("normalized_entropy").min().alias("entropy_min"),
            pl.col("normalized_entropy").max().alias("entropy_max"),
            pl.col("hit").cast(pl.Int32).mean().alias("hit_rate"),
            pl.col("stake").sum().alias("stake"),
            pl.col("payout").sum().alias("payout"),
        )
        .with_columns((pl.col("payout") / pl.col("stake")).alias("recovery_rate"))
        .sort("bin")
    )


def recovery_by_month(stake_payout: pl.DataFrame) -> pl.DataFrame:
    """race_date列を持つstake_payout(fetch_race_stake_payoutの範囲版)から
    月別の購入額・払戻額・回収率を集計する。"""
    return (
        stake_payout.with_columns(pl.col("race_date").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(
            pl.len().alias("n_races"),
            pl.col("n_tickets").sum().alias("n_tickets"),
            pl.col("stake").sum().alias("stake"),
            pl.col("payout").sum().alias("payout"),
        )
        .with_columns((pl.col("payout") / pl.col("stake")).alias("recovery_rate"))
        .sort("month")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_dist = sub.add_parser("analyze", help="検証期間全体でエントロピー分布と的中率を確認する")
    p_dist.add_argument("model_version")
    p_dist.add_argument("--stage", type=int, default=1)

    p_gen = sub.add_parser("generate", help="指定日の買い目をprediction_ticketsに書き込む")
    p_gen.add_argument("model_version")
    p_gen.add_argument("date")
    p_gen.add_argument("--stage", type=int, default=1)
    p_gen.add_argument("--cum-prob-threshold", type=float, default=DEFAULT_CUM_PROB_THRESHOLD)

    p_rec = sub.add_parser("recovery", help="指定日の回収率（全体・エントロピー分位別）を集計する")
    p_rec.add_argument("model_version")
    p_rec.add_argument("date")
    p_rec.add_argument("--stage", type=int, default=1)

    args = parser.parse_args(argv)

    conn = get_connection()
    try:
        if args.command == "analyze":
            df = fetch_p_first_and_hit(conn, args.model_version, args.stage, VAL_START, VAL_END)
            binned = analyze_confidence_distribution(df)
            total_races = binned["n"].sum()
            print(f"=== 正規化エントロピー分布 x 的中率（検証期間、{total_races}レース） ===")
            print(f"{'bin':>4s} {'n':>6s} {'entropy範囲':>18s} {'的中率':>8s}")
            for row in binned.iter_rows(named=True):
                print(
                    f"{row['bin']:>4d} {row['n']:>6d} "
                    f"[{row['entropy_min']:.4f}, {row['entropy_max']:.4f}] "
                    f"{row['hit_rate'] * 100:>7.2f}%"
                )
            thresholds = propose_confidence_thresholds(binned)
            print(f"\n初期案の閾値(正規化エントロピー上限、要調整): {thresholds}")

        elif args.command == "generate":
            target_date = date.fromisoformat(args.date)
            result = generate_tickets_for_date(
                conn,
                args.model_version,
                args.stage,
                target_date,
                cum_prob_threshold=args.cum_prob_threshold,
            )
            print(
                f"{target_date}: predictions={result.predictions} tickets={result.tickets} "
                f"cum_prob_threshold={args.cum_prob_threshold}"
            )

        elif args.command == "recovery":
            target_date = date.fromisoformat(args.date)
            stake_payout = fetch_race_stake_payout(conn, args.model_version, args.stage, target_date)
            overall = overall_recovery(stake_payout)
            print(f"=== {target_date} 全体の回収率（{BET_TYPE}） ===")
            print(f"チケット数: {overall['tickets']}")
            print(f"総購入額:   {overall['total_stake']:,}円")
            print(f"総払戻額:   {overall['total_payout']:,}円")
            print(f"回収率:     {overall['recovery_rate'] * 100:.2f}%")

            print(f"\n=== エントロピー分位別の回収率（{target_date}） ===")
            binned = recovery_by_confidence(conn, args.model_version, args.stage, target_date)
            print(
                f"{'bin':>4s} {'n':>4s} {'entropy範囲':>18s} {'的中率':>8s} "
                f"{'購入額':>9s} {'払戻額':>9s} {'回収率':>8s}"
            )
            for row in binned.iter_rows(named=True):
                print(
                    f"{row['bin']:>4d} {row['n']:>4d} "
                    f"[{row['entropy_min']:.3f}, {row['entropy_max']:.3f}] "
                    f"{row['hit_rate'] * 100:>7.2f}% "
                    f"{row['stake']:>9,d} {row['payout']:>9,d} "
                    f"{row['recovery_rate'] * 100:>7.2f}%"
                )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
