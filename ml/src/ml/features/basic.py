"""特徴量生成 第1層: 番組表(B)由来の生の値のみを使った基礎特徴量。

導出特徴量（勝率差・偏差値化・ランキング等）はこの層では作らない。
racer_periods / race_entries / races に格納済みの値をそのまま payload に
詰めるだけの層であり、以降の層はこの上に積む想定。

CLAUDE.md ルール1（リーク防止）との関係:
- cutoff_at は races.deadline_at - 10分（締切10分前 = 配信時刻）で固定する。
- 選手の級別・勝率は race_entries.racer_period_id という固定FK経由で
  racer_periods から引く。race_date から都度再検索すると、後から
  racer_periods が更新された場合に配信時点と異なる値を拾ってしまう
  （このFKはロード時点で「配信時刻に有効だった期間」に解決済み）。
- 生成後は assert_no_leak() で、参照した racer_periods.valid_from が
  そのレースの cutoff_at 以前であることを行ごとに機械的に検証する。
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from ml.cutoff import assert_no_leak
from ml.loaders.db import get_connection

FEATURE_VERSION = "v1_basic"
CUTOFF_MARGIN = timedelta(minutes=10)
DEFAULT_BATCH_SIZE = 2000

# 級別の順序尺度化。A1が最上位なので1、以降数字が大きいほど格下。
RACER_CLASS_ORDINAL = {"A1": 1, "A2": 2, "B1": 3, "B2": 4}


class FeatureGenerationError(ValueError):
    """特徴量生成に失敗した場合に送出する。"""


@dataclass(frozen=True)
class _BuiltFeature:
    race_id: int
    lane: int
    cutoff_at: object  # datetime（tz-aware）
    period_valid_from: object  # datetime（tz-aware）。リーク検証専用で payload には含めない
    payload: dict


@dataclass(frozen=True)
class FeatureGenerationResult:
    start_date: date
    end_date: date
    row_count: int
    upserted: int


def _to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


_SELECT_SQL = """
    SELECT
        re.race_id, re.lane,
        r.deadline_at, r.stadium_id, r.race_no, r.distance_m,
        re.age, re.weight, re.motor_win_rate_2, re.boat_win_rate_2,
        rp.valid_from,
        rp.racer_class, rp.national_win_rate, rp.national_win_rate_2,
        rp.local_win_rate, rp.local_win_rate_2
    FROM race_entries re
    JOIN races r ON r.id = re.race_id
    JOIN racer_periods rp ON rp.id = re.racer_period_id
    WHERE r.race_date BETWEEN %s AND %s
    ORDER BY r.race_date, r.id, re.lane
"""


def _build_feature(row: tuple) -> _BuiltFeature:
    (
        race_id, lane, deadline_at, stadium_id, race_no, distance_m,
        age, weight, motor_win_rate_2, boat_win_rate_2,
        valid_from,
        racer_class, national_win_rate, national_win_rate_2,
        local_win_rate, local_win_rate_2,
    ) = row

    if racer_class not in RACER_CLASS_ORDINAL:
        raise FeatureGenerationError(
            f"race_id={race_id} lane={lane}: unknown racer_class {racer_class!r}"
        )

    cutoff_at = deadline_at - CUTOFF_MARGIN

    payload = {
        "racer_class": RACER_CLASS_ORDINAL[racer_class],
        "national_win_rate": _to_float(national_win_rate),
        "national_win_rate_2": _to_float(national_win_rate_2),
        "local_win_rate": _to_float(local_win_rate),
        "local_win_rate_2": _to_float(local_win_rate_2),
        "motor_win_rate_2": _to_float(motor_win_rate_2),
        "boat_win_rate_2": _to_float(boat_win_rate_2),
        "age": age,
        "weight": _to_float(weight),
        "lane": lane,
        "stadium_id": stadium_id,
        "race_no": race_no,
        "distance_m": distance_m,
    }

    return _BuiltFeature(
        race_id=race_id,
        lane=lane,
        cutoff_at=cutoff_at,
        period_valid_from=valid_from,
        payload=payload,
    )


def _assert_no_leak_one(f: _BuiltFeature) -> None:
    assert_no_leak(
        [{"valid_from": f.period_valid_from}],
        cutoff_at=f.cutoff_at,
        time_field="valid_from",
        label=f"racer_periods(race_id={f.race_id}, lane={f.lane})",
    )


def _bulk_upsert(cur: psycopg.Cursor, batch: list[_BuiltFeature]) -> None:
    """batch分をまとめて1回のINSERTで書き込む（1行ずつのラウンドトリップを避ける）。"""
    if not batch:
        return
    values_sql = ", ".join(["(%s, %s, %s, %s, %s, now(), now())"] * len(batch))
    params: list[object] = []
    for f in batch:
        params.extend([f.race_id, f.lane, FEATURE_VERSION, f.cutoff_at, Jsonb(f.payload)])

    cur.execute(
        f"""
        INSERT INTO features (race_id, lane, feature_version, cutoff_at, payload,
                               created_at, updated_at)
        VALUES {values_sql}
        ON CONFLICT (race_id, lane, feature_version) DO UPDATE SET
            cutoff_at = EXCLUDED.cutoff_at,
            payload = EXCLUDED.payload,
            updated_at = now()
        """,
        params,
    )


def generate_basic_features_range(
    conn: psycopg.Connection,
    start_date: date,
    end_date: date,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: bool = False,
) -> FeatureGenerationResult:
    """race_date が [start_date, end_date] の特徴量をバッチ単位でまとめて生成する。

    行ごとに構築・リーク検証した上で、batch_size件たまるごとに1回のバルクINSERTで
    書き込む（1行ずつのINSERTは日数・行数が多いと極端に遅いため避ける）。
    """
    row_count = 0
    upserted = 0
    batch: list[_BuiltFeature] = []
    t0 = time.time()

    with conn.cursor() as read_cur, conn.cursor() as write_cur:
        read_cur.execute(_SELECT_SQL, (start_date, end_date))

        for row in read_cur:
            f = _build_feature(row)
            _assert_no_leak_one(f)
            batch.append(f)
            row_count += 1

            if len(batch) >= batch_size:
                _bulk_upsert(write_cur, batch)
                conn.commit()
                upserted += len(batch)
                batch.clear()
                if progress:
                    print(
                        f"  progress: {upserted} rows upserted ({time.time() - t0:.1f}s)",
                        flush=True,
                    )

        if batch:
            _bulk_upsert(write_cur, batch)
            conn.commit()
            upserted += len(batch)

    return FeatureGenerationResult(
        start_date=start_date, end_date=end_date, row_count=row_count, upserted=upserted
    )


def generate_basic_features(conn: psycopg.Connection, race_date: date) -> FeatureGenerationResult:
    """1日分だけ生成する薄いラッパー（動作確認・単発実行用）。"""
    return generate_basic_features_range(conn, race_date, race_date)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start", help="YYYY-MM-DD")
    parser.add_argument(
        "end", nargs="?", default=None, help="YYYY-MM-DD（省略時はstartと同じ=1日分）"
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--progress", action="store_true", help="batch-size件書き込むごとに進捗を表示する"
    )
    parser.add_argument(
        "--show", type=int, default=3, help="生成後にpayloadを何件表示するか（デフォルト3）"
    )
    args = parser.parse_args(argv)
    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end) if args.end else start_date

    conn = get_connection()
    try:
        result = generate_basic_features_range(
            conn, start_date, end_date, batch_size=args.batch_size, progress=args.progress
        )

        print(
            f"{result.start_date}..{result.end_date}: rows={result.row_count} "
            f"upserted={result.upserted} feature_version={FEATURE_VERSION}"
        )

        if args.show:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT race_id, lane, cutoff_at, payload
                    FROM features f
                    JOIN races r ON r.id = f.race_id
                    WHERE r.race_date BETWEEN %s AND %s AND f.feature_version = %s
                    ORDER BY race_id, lane
                    LIMIT %s
                    """,
                    (start_date, end_date, FEATURE_VERSION, args.show),
                )
                for race_id, lane, cutoff_at, payload in cur.fetchall():
                    print(f"--- race_id={race_id} lane={lane} cutoff_at={cutoff_at} ---")
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
