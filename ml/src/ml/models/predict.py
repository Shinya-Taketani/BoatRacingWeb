"""train.py で保存したモデル(pickle)を使って、指定期間のレースを推論し、
predictions / prediction_entries に書き込む。

- model_version は読み込んだpickleのファイル名(拡張子抜き)と一致させる
  （predictions.model_version にそのまま書き込むので、後から
  どのモデルがどの予測を出したか一意に辿れる）。
- stage は当面 1 のみ（前日・当日朝の予測。締切直前の再予測は未実装）。
- published_at は書き込み時刻、cutoff_at は races.deadline_at - 10分。
- predictions は published_at 設定後イミュータブル（CLAUDE.md ルール5）
  なので、既に (race_id, model_version, stage) の予測が存在するレースは
  上書きせずスキップする。
- レースごとに1回ずつINSERTすると期間が長いと遅いため、batch_size件ずつ
  まとめてバルクINSERTする（predictions側はRETURNINGでidを一括取得し、
  それをprediction_entries側のprediction_idとして使う）。

現在のモデルは is_winner（1着）のみを学習しているため、p_top2/p_top3は
まだ計算できずNULLのままにする（今後の層で対応）。
"""

from __future__ import annotations

import argparse
import pickle
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import psycopg

from ml.loaders.db import get_connection
from ml.models.lgbm import predict_race_normalized
from ml.models.tickets import marginal_top_n, plackett_luce_trifecta
from ml.models.train import DEFAULT_MODEL_DIR

STAGE = 1
CUTOFF_MARGIN = timedelta(minutes=10)
DEFAULT_BATCH_SIZE = 500  # レース単位（1レース=6 prediction_entries行）


class PredictionError(ValueError):
    """推論・書き込みに失敗した場合に送出する。"""


@dataclass(frozen=True)
class PredictionRunResult:
    start_date: date
    end_date: date
    races: int
    entries: int
    skipped_races: int


def load_model(model_version: str, model_dir: Path = DEFAULT_MODEL_DIR) -> dict:
    path = model_dir / f"{model_version}.pkl"
    if not path.exists():
        raise PredictionError(f"model not found: {path}")

    with open(path, "rb") as f:
        artifact = pickle.load(f)

    if artifact["model_version"] != model_version:
        raise PredictionError(
            f"pickle内のmodel_version({artifact['model_version']!r})とファイル名から "
            f"導出したmodel_version({model_version!r})が一致しません"
        )
    return artifact


def _fetch_target_rows(conn: psycopg.Connection, start_date: date, end_date: date) -> list[tuple]:
    """v1_basic + v2_recent + v3_relative の payload をマージし、
    さらに races.deadline_at も一緒に取得する（predict専用。lgbm.fetch_all_dataset
    はrace_resultsまでJOINするため、cutoff_atの算出に必要なdeadline_atが
    含まれておらず、ここでは別途組み立てる）。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f1.race_id, f1.lane, r.deadline_at,
                   f1.payload AS payload_v1, f2.payload AS payload_v2, f3.payload AS payload_v3
            FROM features f1
            JOIN features f2
                ON f2.race_id = f1.race_id AND f2.lane = f1.lane
               AND f2.feature_version = 'v2_recent'
            JOIN features f3
                ON f3.race_id = f1.race_id AND f3.lane = f1.lane
               AND f3.feature_version = 'v3_relative'
            JOIN races r ON r.id = f1.race_id
            WHERE f1.feature_version = 'v1_basic' AND r.race_date BETWEEN %s AND %s
            ORDER BY r.race_date, f1.race_id, f1.lane
            """,
            (start_date, end_date),
        )
        return cur.fetchall()


def _existing_prediction_race_ids(
    conn: psycopg.Connection, race_ids: list[int], model_version: str, stage: int
) -> set[int]:
    if not race_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT race_id FROM predictions
            WHERE race_id = ANY(%s) AND model_version = %s AND stage = %s
            """,
            (race_ids, model_version, stage),
        )
        return {row[0] for row in cur.fetchall()}


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def generate_predictions_for_range(
    conn: psycopg.Connection,
    artifact: dict,
    start_date: date,
    end_date: date,
    *,
    stage: int = STAGE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: bool = False,
) -> PredictionRunResult:
    t0 = time.time()
    rows = _fetch_target_rows(conn, start_date, end_date)
    if progress:
        print(f"  fetched {len(rows)} rows ({time.time() - t0:.1f}s)", flush=True)

    if not rows:
        return PredictionRunResult(start_date, end_date, races=0, entries=0, skipped_races=0)

    records = []
    deadline_by_race: dict[int, datetime] = {}
    race_order: list[int] = []
    for race_id, lane, deadline_at, payload_v1, payload_v2, payload_v3 in rows:
        record = {**payload_v1, **payload_v2, **payload_v3}
        record["race_id"] = race_id
        record["lane"] = lane
        record["is_winner"] = 0  # 未使用ダミー（predict_race_normalizedがselectするため必要）
        records.append(record)
        if race_id not in deadline_by_race:
            race_order.append(race_id)
        deadline_by_race[race_id] = deadline_at

    df = pl.DataFrame(records)

    model_version = artifact["model_version"]
    already = _existing_prediction_race_ids(conn, race_order, model_version, stage)
    target_race_ids = [rid for rid in race_order if rid not in already]
    skipped = len(already)

    if not target_race_ids:
        return PredictionRunResult(
            start_date, end_date, races=0, entries=0, skipped_races=skipped
        )

    df = df.filter(pl.col("race_id").is_in(target_race_ids))

    booster = artifact["booster"]
    feature_columns = artifact["feature_columns"]
    result = predict_race_normalized(booster, df, feature_columns)
    if progress:
        print(f"  predicted {df.height} rows ({time.time() - t0:.1f}s)", flush=True)

    published_at = datetime.now(timezone.utc)
    races_inserted = 0
    entries_inserted = 0

    with conn.cursor() as cur:
        for chunk in _chunks(target_race_ids, batch_size):
            pred_values_sql = ", ".join(["(%s, %s, %s, %s, %s, now(), now())"] * len(chunk))
            pred_params: list[object] = []
            for race_id in chunk:
                cutoff_at = deadline_by_race[race_id] - CUTOFF_MARGIN
                pred_params.extend([race_id, model_version, stage, published_at, cutoff_at])

            cur.execute(
                f"""
                INSERT INTO predictions (race_id, model_version, stage, published_at, cutoff_at,
                                          created_at, updated_at)
                VALUES {pred_values_sql}
                RETURNING race_id, id
                """,
                pred_params,
            )
            prediction_id_by_race = dict(cur.fetchall())
            races_inserted += len(chunk)

            chunk_result = result.filter(pl.col("race_id").is_in(chunk))

            # p_top2/p_top3はprediction_entries挿入後は不変化トリガーで
            # 更新できないため、挿入時にPlackett-Luce展開から確定させる。
            p_top2_top3_by_race: dict[int, tuple[dict[int, float], dict[int, float]]] = {}
            for race_id in chunk:
                race_rows = chunk_result.filter(pl.col("race_id") == race_id)
                p_first = dict(zip(race_rows["lane"].to_list(), race_rows["pred_prob"].to_list()))
                perm_probs = plackett_luce_trifecta(p_first)
                p_top2_top3_by_race[race_id] = (
                    marginal_top_n(perm_probs, 2),
                    marginal_top_n(perm_probs, 3),
                )

            entry_values_sql = ", ".join(["(%s, %s, %s, %s, %s)"] * chunk_result.height)
            entry_params: list[object] = []
            for row in chunk_result.iter_rows(named=True):
                prediction_id = prediction_id_by_race[row["race_id"]]
                p_top2, p_top3 = p_top2_top3_by_race[row["race_id"]]
                entry_params.extend([
                    prediction_id, row["lane"], row["pred_prob"],
                    p_top2[row["lane"]], p_top3[row["lane"]],
                ])

            cur.execute(
                f"""
                INSERT INTO prediction_entries (prediction_id, lane, p_first, p_top2, p_top3)
                VALUES {entry_values_sql}
                """,
                entry_params,
            )
            entries_inserted += chunk_result.height

            conn.commit()
            if progress:
                print(
                    f"  progress: {races_inserted}/{len(target_race_ids)} races "
                    f"({time.time() - t0:.1f}s)",
                    flush=True,
                )

    return PredictionRunResult(
        start_date=start_date,
        end_date=end_date,
        races=races_inserted,
        entries=entries_inserted,
        skipped_races=skipped,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_version", help="ml/models/{model_version}.pkl を使う")
    parser.add_argument("start", help="YYYY-MM-DD")
    parser.add_argument(
        "end", nargs="?", default=None, help="YYYY-MM-DD（省略時はstartと同じ=1日分）"
    )
    parser.add_argument("--stage", type=int, default=STAGE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args(argv)

    artifact = load_model(args.model_version)
    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end) if args.end else start_date

    conn = get_connection()
    try:
        result = generate_predictions_for_range(
            conn,
            artifact,
            start_date,
            end_date,
            stage=args.stage,
            batch_size=args.batch_size,
            progress=args.progress,
        )
    finally:
        conn.close()

    print(
        f"{result.start_date}..{result.end_date}: races={result.races} entries={result.entries} "
        f"skipped(既存予測あり)={result.skipped_races} "
        f"model_version={artifact['model_version']} stage={args.stage}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
