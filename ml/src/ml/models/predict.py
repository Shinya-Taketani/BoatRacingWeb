"""train.py で保存した2モデル(winner/top3)を使って、指定期間のレースを推論し、
predictions / prediction_entries に書き込む。

2026-10-04、1着予測モデルと3着以内予測モデルの2本立てに変更した
（CLAUDE.md「p_top3の直接学習モデル」参照）。レースごとに**2件の
predictions レコード**を書く:
- winner_model_version の行: p_first を埋め、p_top2/p_top3 はNULLのまま
  （既存のまま。勝負レース・妙味レース・1号艇危険はこの行のp_firstを使う）。
- top3_model_version の行: p_top3 だけを埋め、p_first/p_top2 はNULLのまま
  （新規。confident_top3(軸艇)専用）。
  p_top2は当面出さない。Plackett-Luce由来の値を残すと「このp_top2はどの
  モデルの考え方で出たのか」の定義が混在するため、やめた。

- model_version は読み込んだpickleのファイル名(拡張子抜き)と一致させる
  （predictions.model_version にそのまま書き込むので、後から
  どのモデルがどの予測を出したか一意に辿れる）。
- stage は当面 1 のみ（前日・当日朝の予測。締切直前の再予測は未実装）。
- published_at は書き込み時刻、cutoff_at は races.deadline_at - 10分。
- predictions は published_at 設定後イミュータブル（CLAUDE.md ルール5）
  なので、既に (race_id, model_version, stage) の予測が存在するレースは
  上書きせずスキップする（winner/top3それぞれ独立に判定する）。
- top3モデルの素のシグモイド出力をレース内合計3.0に正規化した後、個々の
  値が[0,1]を超えることがある（2026-10-03の検証で0.54%で発生）。
  単純に[0,1]へclipし、clipした件数をログに出す
  (ml.models.top3.predict_race_sum3_for_inference)。

2026-10-08、stage2(v5_exhibitionを含む直前再予測)対応を追加した
（CLAUDE.md「stage2構成」参照）:
- feature_set(v3/v5)はCLIで明示指定せず、読み込んだpickleのartifactの
  "feature_set"キー(ml.models.train.train_and_saveが保存)から自動判定する。
  winner/top3で feature_set が食い違う場合（v3モデルとv5モデルを誤って
  混ぜた場合）はエラーで停止する。古いpickle(feature_setキー追加前に
  保存したもの)は "v3" とみなす(後方互換)。
- feature_set="v5" の場合、v5_exhibition特徴量をINNER JOINして取得する
  ため、v5特徴量がまだ生成されていないレースは自然に対象外になる
  （ライブのbeforeinfo取得が終わっていないレースを誤って推論しない
  ための構造的なガード）。
- --only-before-cutoff を指定すると、cutoff_at(=deadline_at-10分)を
  過ぎたレースを対象から除外する。stage2は毎分実行される想定のため、
  既に締切が過ぎた（今さら予測しても無意味な）レースを毎回処理しない
  ようにする。
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
from ml.models.top3 import predict_race_sum3_for_inference
from ml.models.train import DEFAULT_MODEL_DIR

STAGE = 1
CUTOFF_MARGIN = timedelta(minutes=10)
DEFAULT_BATCH_SIZE = 500  # レース単位（1レース=6 prediction_entries行）

# feature_set別のrow取得SQL。v5はv5_exhibition特徴量をINNER JOINするため、
# 対象期間内でもv5特徴量が無いレースは自然に除外される。{cutoff_clause}は
# --only-before-cutoff指定時のみ埋める(CUTOFF_MARGINと同じ10分)。
_TARGET_ROWS_SQL = {
    "v3": """
        SELECT f1.race_id, f1.lane, r.deadline_at,
               f1.payload AS payload_v1, f2.payload AS payload_v2, f3.payload AS payload_v3
        FROM features f1
        JOIN features f2
            ON f2.race_id = f1.race_id AND f2.lane = f1.lane AND f2.feature_version = 'v2_recent'
        JOIN features f3
            ON f3.race_id = f1.race_id AND f3.lane = f1.lane AND f3.feature_version = 'v3_relative'
        JOIN races r ON r.id = f1.race_id
        WHERE f1.feature_version = 'v1_basic' AND r.race_date BETWEEN %s AND %s
        {cutoff_clause}
        ORDER BY r.race_date, f1.race_id, f1.lane
    """,
    "v5": """
        SELECT f1.race_id, f1.lane, r.deadline_at,
               f1.payload AS payload_v1, f2.payload AS payload_v2, f3.payload AS payload_v3,
               f5.payload AS payload_v5
        FROM features f1
        JOIN features f2
            ON f2.race_id = f1.race_id AND f2.lane = f1.lane AND f2.feature_version = 'v2_recent'
        JOIN features f3
            ON f3.race_id = f1.race_id AND f3.lane = f1.lane AND f3.feature_version = 'v3_relative'
        JOIN features f5
            ON f5.race_id = f1.race_id AND f5.lane = f1.lane AND f5.feature_version = 'v5_exhibition'
        JOIN races r ON r.id = f1.race_id
        WHERE f1.feature_version = 'v1_basic' AND r.race_date BETWEEN %s AND %s
        {cutoff_clause}
        ORDER BY r.race_date, f1.race_id, f1.lane
    """,
}


class PredictionError(ValueError):
    """推論・書き込みに失敗した場合に送出する。"""


@dataclass(frozen=True)
class PredictionRunResult:
    start_date: date
    end_date: date
    winner_races: int
    winner_entries: int
    winner_skipped_races: int
    top3_races: int
    top3_entries: int
    top3_skipped_races: int
    top3_clipped_count: int


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


def _fetch_target_rows(
    conn: psycopg.Connection,
    start_date: date,
    end_date: date,
    *,
    feature_set: str = "v3",
    only_before_cutoff: bool = False,
) -> list[tuple]:
    """feature_columns の payload をマージし、さらに races.deadline_at も
    一緒に取得する（predict専用。lgbm.fetch_all_dataset/fetch_v5_datasetは
    race_resultsまでJOINするため、cutoff_atの算出に必要なdeadline_atが
    含まれておらず、ここでは別途組み立てる）。

    feature_set="v5"はv5_exhibitionをINNER JOINするため、v5特徴量が未生成の
    レースは自然に除外される。only_before_cutoff=Trueならcutoff_at
    (=deadline_at-10分、CUTOFF_MARGINと同じ)を過ぎたレースも除外する。
    """
    if feature_set not in _TARGET_ROWS_SQL:
        raise PredictionError(
            f"feature_set must be one of {sorted(_TARGET_ROWS_SQL)}, got {feature_set!r}"
        )
    cutoff_clause = (
        "AND r.deadline_at - interval '10 minutes' > now()" if only_before_cutoff else ""
    )
    sql = _TARGET_ROWS_SQL[feature_set].format(cutoff_clause=cutoff_clause)
    with conn.cursor() as cur:
        cur.execute(sql, (start_date, end_date))
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


def _write_predictions(
    conn: psycopg.Connection,
    *,
    model_version: str,
    stage: int,
    race_order: list[int],
    deadline_by_race: dict[int, datetime],
    published_at: datetime,
    batch_size: int,
    entries_by_race: dict[int, list[tuple]],
    progress: bool,
    progress_label: str,
) -> tuple[int, int, int]:
    """1モデル分のpredictions/prediction_entriesを書き込む共通処理。

    entries_by_race[race_id] は [(lane, p_first, p_top2, p_top3), ...] の
    リスト（呼び出し側で片方の値がNoneのタプルを渡せば、そのままNULLとして
    挿入される）。
    """
    already = _existing_prediction_race_ids(conn, race_order, model_version, stage)
    target_race_ids = [rid for rid in race_order if rid not in already]
    skipped = len(already)

    if not target_race_ids:
        return 0, 0, skipped

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

            entry_rows = [
                (prediction_id_by_race[race_id], lane, p_first, p_top2, p_top3)
                for race_id in chunk
                for lane, p_first, p_top2, p_top3 in entries_by_race[race_id]
            ]
            entry_values_sql = ", ".join(["(%s, %s, %s, %s, %s)"] * len(entry_rows))
            entry_params: list[object] = [v for row in entry_rows for v in row]

            cur.execute(
                f"""
                INSERT INTO prediction_entries (prediction_id, lane, p_first, p_top2, p_top3)
                VALUES {entry_values_sql}
                """,
                entry_params,
            )
            entries_inserted += len(entry_rows)

            conn.commit()
            if progress:
                print(
                    f"  [{progress_label}] progress: {races_inserted}/{len(target_race_ids)} races",
                    flush=True,
                )

    return races_inserted, entries_inserted, skipped


def generate_predictions_for_range(
    conn: psycopg.Connection,
    winner_artifact: dict,
    top3_artifact: dict,
    start_date: date,
    end_date: date,
    *,
    stage: int = STAGE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: bool = False,
    feature_set: str = "v3",
    only_before_cutoff: bool = False,
) -> PredictionRunResult:
    t0 = time.time()
    rows = _fetch_target_rows(
        conn, start_date, end_date, feature_set=feature_set, only_before_cutoff=only_before_cutoff
    )
    if progress:
        print(f"  fetched {len(rows)} rows ({time.time() - t0:.1f}s)", flush=True)

    if not rows:
        return PredictionRunResult(
            start_date, end_date,
            winner_races=0, winner_entries=0, winner_skipped_races=0,
            top3_races=0, top3_entries=0, top3_skipped_races=0, top3_clipped_count=0,
        )

    records = []
    deadline_by_race: dict[int, datetime] = {}
    race_order: list[int] = []
    for row in rows:
        if feature_set == "v5":
            race_id, lane, deadline_at, payload_v1, payload_v2, payload_v3, payload_v5 = row
            record = {**payload_v1, **payload_v2, **payload_v3, **payload_v5}
        else:
            race_id, lane, deadline_at, payload_v1, payload_v2, payload_v3 = row
            record = {**payload_v1, **payload_v2, **payload_v3}
        record["race_id"] = race_id
        record["lane"] = lane
        record["is_winner"] = 0  # 未使用ダミー（predict_race_normalizedがselectするため必要）
        records.append(record)
        if race_id not in deadline_by_race:
            race_order.append(race_id)
        deadline_by_race[race_id] = deadline_at

    df = pl.DataFrame(records)
    published_at = datetime.now(timezone.utc)

    # --- winner モデル: p_first ---
    winner_version = winner_artifact["model_version"]
    winner_already = _existing_prediction_race_ids(conn, race_order, winner_version, stage)
    winner_target_ids = [rid for rid in race_order if rid not in winner_already]
    winner_races = winner_entries = 0
    if winner_target_ids:
        winner_df = df.filter(pl.col("race_id").is_in(winner_target_ids))
        winner_result = predict_race_normalized(
            winner_artifact["booster"], winner_df, winner_artifact["feature_columns"]
        )
        if progress:
            print(f"  [winner] predicted {winner_df.height} rows ({time.time() - t0:.1f}s)", flush=True)

        winner_entries_by_race: dict[int, list[tuple]] = {}
        for race_id, race_rows in winner_result.group_by("race_id", maintain_order=True):
            race_id = race_id[0] if isinstance(race_id, tuple) else race_id
            winner_entries_by_race[race_id] = [
                (lane, p_first, None, None)
                for lane, p_first in zip(race_rows["lane"].to_list(), race_rows["pred_prob"].to_list())
            ]

        winner_races, winner_entries, _ = _write_predictions(
            conn,
            model_version=winner_version,
            stage=stage,
            race_order=winner_target_ids,
            deadline_by_race=deadline_by_race,
            published_at=published_at,
            batch_size=batch_size,
            entries_by_race=winner_entries_by_race,
            progress=progress,
            progress_label="winner",
        )
    winner_skipped = len(winner_already)

    # --- top3 モデル: p_top3 ---
    top3_version = top3_artifact["model_version"]
    top3_already = _existing_prediction_race_ids(conn, race_order, top3_version, stage)
    top3_target_ids = [rid for rid in race_order if rid not in top3_already]
    top3_races = top3_entries = 0
    top3_clipped_count = 0
    if top3_target_ids:
        top3_df = df.filter(pl.col("race_id").is_in(top3_target_ids))
        top3_result, top3_clipped_count = predict_race_sum3_for_inference(
            top3_artifact["booster"], top3_df, top3_artifact["feature_columns"]
        )
        if progress:
            print(
                f"  [top3] predicted {top3_df.height} rows, clipped={top3_clipped_count} "
                f"({time.time() - t0:.1f}s)",
                flush=True,
            )

        top3_entries_by_race: dict[int, list[tuple]] = {}
        for race_id, race_rows in top3_result.group_by("race_id", maintain_order=True):
            race_id = race_id[0] if isinstance(race_id, tuple) else race_id
            top3_entries_by_race[race_id] = [
                (lane, None, None, p_top3)
                for lane, p_top3 in zip(race_rows["lane"].to_list(), race_rows["p_top3"].to_list())
            ]

        top3_races, top3_entries, _ = _write_predictions(
            conn,
            model_version=top3_version,
            stage=stage,
            race_order=top3_target_ids,
            deadline_by_race=deadline_by_race,
            published_at=published_at,
            batch_size=batch_size,
            entries_by_race=top3_entries_by_race,
            progress=progress,
            progress_label="top3",
        )
    top3_skipped = len(top3_already)

    return PredictionRunResult(
        start_date=start_date,
        end_date=end_date,
        winner_races=winner_races,
        winner_entries=winner_entries,
        winner_skipped_races=winner_skipped,
        top3_races=top3_races,
        top3_entries=top3_entries,
        top3_skipped_races=top3_skipped,
        top3_clipped_count=top3_clipped_count,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("winner_model_version", help="ml/models/{winner_model_version}.pkl (kind=winner)")
    parser.add_argument("top3_model_version", help="ml/models/{top3_model_version}.pkl (kind=top3)")
    parser.add_argument("start", help="YYYY-MM-DD")
    parser.add_argument(
        "end", nargs="?", default=None, help="YYYY-MM-DD（省略時はstartと同じ=1日分）"
    )
    parser.add_argument("--stage", type=int, default=STAGE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument(
        "--only-before-cutoff",
        action="store_true",
        help=(
            "cutoff_at(=deadline_at-10分)を過ぎたレースを対象から除外する"
            "(毎分実行するstage2用。stage1の日次実行では使わない)"
        ),
    )
    args = parser.parse_args(argv)

    winner_artifact = load_model(args.winner_model_version)
    top3_artifact = load_model(args.top3_model_version)
    if winner_artifact.get("kind") not in (None, "winner"):
        raise PredictionError(
            f"{args.winner_model_version} は kind={winner_artifact.get('kind')!r} で、winnerではありません"
        )
    if top3_artifact.get("kind") != "top3":
        raise PredictionError(
            f"{args.top3_model_version} は kind={top3_artifact.get('kind')!r} で、top3ではありません"
        )

    # feature_setはCLIで明示指定せず、pickleのartifactから自動判定する
    # (v3モデルとv5モデルの取り違えを事故ではなく起動時エラーにするため。
    # feature_setキーが無い古いpickleは"v3"とみなす)。
    winner_feature_set = winner_artifact.get("feature_set", "v3")
    top3_feature_set = top3_artifact.get("feature_set", "v3")
    if winner_feature_set != top3_feature_set:
        raise PredictionError(
            f"winner({args.winner_model_version})のfeature_set={winner_feature_set!r}と "
            f"top3({args.top3_model_version})のfeature_set={top3_feature_set!r}が一致しません。"
            "v3モデルとv5モデルを混ぜて使うことはできません。"
        )
    feature_set = winner_feature_set

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end) if args.end else start_date

    conn = get_connection()
    try:
        result = generate_predictions_for_range(
            conn,
            winner_artifact,
            top3_artifact,
            start_date,
            end_date,
            stage=args.stage,
            batch_size=args.batch_size,
            progress=args.progress,
            feature_set=feature_set,
            only_before_cutoff=args.only_before_cutoff,
        )
    finally:
        conn.close()

    print(
        f"{result.start_date}..{result.end_date}: feature_set={feature_set} "
        f"stage={args.stage} only_before_cutoff={args.only_before_cutoff}"
    )
    print(
        f"{result.start_date}..{result.end_date}: "
        f"winner races={result.winner_races} entries={result.winner_entries} "
        f"skipped={result.winner_skipped_races} model_version={winner_artifact['model_version']}"
    )
    print(
        f"{result.start_date}..{result.end_date}: "
        f"top3 races={result.top3_races} entries={result.top3_entries} "
        f"skipped={result.top3_skipped_races} model_version={top3_artifact['model_version']} "
        f"clipped={result.top3_clipped_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
