"""特徴量生成 第4層: 場の特性・選手の場適性。feature_version = 'v4_stadium'。

CLAUDE.md ルール1（リーク防止）:
- 場×枠番の基礎統計（1着率・平均進入コース・前付け発生率）は、対象レースの
  race_date より「厳密に前」のレースのみから、その時点までの累積
  （expanding window）で計算する。全期間の集計値を使うとリークになる
  （2026-09-21 ユーザー指示）。race_date < r.race_date で絞る点はv2_recent
  と同じ方針。
- 選手の場適性（直近30走）もv2_recentと同じLATERAL/LIMIT方式で、
  race_date が厳密に前の履歴のみを使う。

気象情報について:
Kファイルのレースヘッダ行には天候・風向・風速・波高が含まれている
（例: "H1800m  晴　  風  北西　 2m  波　  1cm"）。ただしKファイルは
レース結果ファイルであり、この気象情報はレース施行時点（＝結果確定後）の
ものであって、締切10分前の配信時刻には存在しない。よってこれを特徴量に
使うと未来の情報を使うことになりリークするため、v4では気象特徴量は
実装しない（2026-09-21 調査の結果、スキップと判断）。将来的に
beforeinfoページ等、締切前に取得できる気象情報のソースが使えるように
なった場合はv5以降で追加を検討する。

構成:
1. 場×枠番の基礎統計（race_dateより前の全履歴、expanding window）
   A. 当該場・当該枠番の1着率
   B. 当該場・当該枠番の平均進入コース
   C. 当該場の前付け発生率（枠番問わず、全艇の前付け=start_course<laneの割合）
2. 選手の場適性（直近30走、v2_recentと同じLATERAL/LIMIT方式）
   D. 当該選手の当該場・当該枠番での1着率
   E. 当該選手の当該場での平均進入コース

パフォーマンス設計:
expanding windowはPostgreSQLのウィンドウ関数(RANGE BETWEEN UNBOUNDED
PRECEDING AND '1 day' PRECEDING)で1パス計算する。LATERAL+LIMITの
相関サブクエリ（v2_recentと同方式）より、全履歴を毎回集計する
expanding統計にはこちらの方が適している。まず1ヶ月分で計測してから
全期間を実行する。
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from ml.cutoff import assert_no_leak
from ml.loaders.db import get_connection

FEATURE_VERSION = "v4_stadium"
CUTOFF_MARGIN = timedelta(minutes=10)
DEFAULT_BATCH_SIZE = 2000

RACER_STADIUM_WINDOW_N = 30

JST = timezone(timedelta(hours=9))


class FeatureGenerationError(ValueError):
    """特徴量生成に失敗した場合に送出する。"""


@dataclass(frozen=True)
class FeatureGenerationResult:
    start_date: date
    end_date: date
    row_count: int
    upserted: int
    elapsed_seconds: float


def _prepare_history_table(cur: psycopg.Cursor) -> None:
    cur.execute("DROP TABLE IF EXISTS stadium_race_history")
    cur.execute(
        """
        CREATE TEMP TABLE stadium_race_history AS
        SELECT
            re.racer_id, re.lane, r.stadium_id, r.race_date, r.id AS race_id,
            rr.start_course, rr.finish_pos
        FROM race_entries re
        JOIN races r ON r.id = re.race_id
        LEFT JOIN race_results rr ON rr.race_entry_id = re.id
        """
    )
    cur.execute(
        "CREATE INDEX ON stadium_race_history (stadium_id, lane, race_date, race_id)"
    )
    cur.execute(
        "CREATE INDEX ON stadium_race_history (stadium_id, race_date, race_id)"
    )
    cur.execute(
        "CREATE INDEX ON stadium_race_history "
        "(racer_id, stadium_id, lane, race_date DESC, race_id DESC)"
    )
    cur.execute(
        "CREATE INDEX ON stadium_race_history "
        "(racer_id, stadium_id, race_date DESC, race_id DESC)"
    )
    cur.execute("ANALYZE stadium_race_history")


def _prepare_expanding_stats_table(cur: psycopg.Cursor) -> None:
    """場×枠番/場単位の累積統計を1パスで計算する（対象日より厳密に前のみ）。

    RANGE BETWEEN UNBOUNDED PRECEDING AND '1 day' PRECEDING は、
    race_dateが同日の行を全て除外しつつ、それより前の全履歴を集計する
    （同日中の順序に依存しない。gaps-and-islandsと同じくPostgreSQLの
    ウィンドウフレームをそのまま活用する）。
    """
    cur.execute("DROP TABLE IF EXISTS stadium_expanding_stats")
    cur.execute(
        """
        CREATE TEMP TABLE stadium_expanding_stats AS
        SELECT
            race_id, lane, stadium_id, race_date,
            avg((finish_pos = 1)::int::float8) OVER lane_w AS stadium_lane_win_rate,
            avg(start_course) OVER lane_w AS stadium_lane_avg_start_course,
            avg((start_course < lane)::int::float8) OVER stadium_w AS stadium_maeduke_rate
        FROM stadium_race_history
        WINDOW
            lane_w AS (
                PARTITION BY stadium_id, lane ORDER BY race_date
                RANGE BETWEEN UNBOUNDED PRECEDING AND '1 day' PRECEDING
            ),
            stadium_w AS (
                PARTITION BY stadium_id ORDER BY race_date
                RANGE BETWEEN UNBOUNDED PRECEDING AND '1 day' PRECEDING
            )
        """
    )
    cur.execute("CREATE INDEX ON stadium_expanding_stats (race_id, lane)")
    cur.execute("ANALYZE stadium_expanding_stats")


_SELECT_SQL = """
    SELECT
        re.race_id, re.lane, r.race_date, r.deadline_at,

        exp_stats.stadium_lane_win_rate, exp_stats.stadium_lane_avg_start_course,
        exp_stats.stadium_maeduke_rate,

        racer_lane_stats.racer_stadium_lane_win_rate,
        racer_lane_stats.most_recent_lane_history_date,

        racer_stadium_stats.racer_stadium_avg_start_course,
        racer_stadium_stats.most_recent_stadium_history_date

    FROM race_entries re
    JOIN races r ON r.id = re.race_id
    LEFT JOIN stadium_expanding_stats exp_stats
        ON exp_stats.race_id = re.race_id AND exp_stats.lane = re.lane
    LEFT JOIN LATERAL (
        SELECT
            avg((finish_pos = 1)::int::float8) AS racer_stadium_lane_win_rate,
            max(race_date) AS most_recent_lane_history_date
        FROM (
            SELECT finish_pos, race_date
            FROM stadium_race_history h
            WHERE h.racer_id = re.racer_id AND h.stadium_id = r.stadium_id
              AND h.lane = re.lane AND h.race_date < r.race_date
            ORDER BY h.race_date DESC, h.race_id DESC
            LIMIT %(racer_stadium_window_n)s
        ) x
    ) racer_lane_stats ON true
    LEFT JOIN LATERAL (
        SELECT
            avg(start_course) AS racer_stadium_avg_start_course,
            max(race_date) AS most_recent_stadium_history_date
        FROM (
            SELECT start_course, race_date
            FROM stadium_race_history h
            WHERE h.racer_id = re.racer_id AND h.stadium_id = r.stadium_id
              AND h.race_date < r.race_date
            ORDER BY h.race_date DESC, h.race_id DESC
            LIMIT %(racer_stadium_window_n)s
        ) x
    ) racer_stadium_stats ON true
    WHERE r.race_date BETWEEN %(start_date)s AND %(end_date)s
    ORDER BY r.race_date, re.race_id, re.lane
"""


def _build_row_payload(row: tuple) -> tuple:
    (
        race_id, lane, race_date, deadline_at,
        stadium_lane_win_rate, stadium_lane_avg_start_course, stadium_maeduke_rate,
        racer_stadium_lane_win_rate, most_recent_lane_history_date,
        racer_stadium_avg_start_course, most_recent_stadium_history_date,
    ) = row

    cutoff_at = deadline_at - CUTOFF_MARGIN

    payload = {
        "stadium_lane_win_rate": (
            float(stadium_lane_win_rate) if stadium_lane_win_rate is not None else None
        ),
        "stadium_lane_avg_start_course": (
            float(stadium_lane_avg_start_course)
            if stadium_lane_avg_start_course is not None
            else None
        ),
        "stadium_maeduke_rate": (
            float(stadium_maeduke_rate) if stadium_maeduke_rate is not None else None
        ),
        "racer_stadium_lane_win_rate_recent30": (
            float(racer_stadium_lane_win_rate)
            if racer_stadium_lane_win_rate is not None
            else None
        ),
        "racer_stadium_avg_start_course_recent30": (
            float(racer_stadium_avg_start_course)
            if racer_stadium_avg_start_course is not None
            else None
        ),
    }

    most_recent_history_date = None
    for d in (most_recent_lane_history_date, most_recent_stadium_history_date):
        if d is not None and (most_recent_history_date is None or d > most_recent_history_date):
            most_recent_history_date = d

    return race_id, lane, cutoff_at, most_recent_history_date, payload


def _assert_no_leak_one(
    race_id: int, lane: int, cutoff_at: datetime, most_recent_history_date: date | None
) -> None:
    if most_recent_history_date is None:
        return
    history_dt = datetime(
        most_recent_history_date.year,
        most_recent_history_date.month,
        most_recent_history_date.day,
        tzinfo=JST,
    )
    assert_no_leak(
        [{"most_recent_history_date": history_dt}],
        cutoff_at=cutoff_at,
        time_field="most_recent_history_date",
        label=f"stadium_race_history(race_id={race_id}, lane={lane})",
    )


def _bulk_upsert(cur: psycopg.Cursor, batch: list[tuple]) -> None:
    if not batch:
        return
    values_sql = ", ".join(["(%s, %s, %s, %s, %s, now(), now())"] * len(batch))
    params: list[object] = []
    for race_id, lane, cutoff_at, payload in batch:
        params.extend([race_id, lane, FEATURE_VERSION, cutoff_at, Jsonb(payload)])

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


def generate_stadium_features_range(
    conn: psycopg.Connection,
    start_date: date,
    end_date: date,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: bool = False,
) -> FeatureGenerationResult:
    t0 = time.time()

    with conn.cursor() as prep_cur:
        _prepare_history_table(prep_cur)
        _prepare_expanding_stats_table(prep_cur)
    conn.commit()
    if progress:
        print(f"  prep done ({time.time() - t0:.1f}s)", flush=True)

    with conn.cursor() as read_cur:
        read_cur.execute(
            _SELECT_SQL,
            {
                "start_date": start_date,
                "end_date": end_date,
                "racer_stadium_window_n": RACER_STADIUM_WINDOW_N,
            },
        )
        raw_rows = read_cur.fetchall()

    if progress:
        print(f"  fetched {len(raw_rows)} rows ({time.time() - t0:.1f}s)", flush=True)

    batch: list[tuple] = []
    row_count = 0
    upserted = 0

    with conn.cursor() as write_cur:
        for row in raw_rows:
            race_id, lane, cutoff_at, most_recent_history_date, payload = _build_row_payload(row)
            _assert_no_leak_one(race_id, lane, cutoff_at, most_recent_history_date)

            batch.append((race_id, lane, cutoff_at, payload))
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

    elapsed = time.time() - t0
    return FeatureGenerationResult(
        start_date=start_date,
        end_date=end_date,
        row_count=row_count,
        upserted=upserted,
        elapsed_seconds=elapsed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start", help="YYYY-MM-DD")
    parser.add_argument(
        "end", nargs="?", default=None, help="YYYY-MM-DD（省略時はstartと同じ=1日分）"
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--show", type=int, default=3)
    args = parser.parse_args(argv)

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end) if args.end else start_date

    conn = get_connection()
    try:
        result = generate_stadium_features_range(
            conn, start_date, end_date, batch_size=args.batch_size, progress=args.progress
        )
        print(
            f"{result.start_date}..{result.end_date}: rows={result.row_count} "
            f"upserted={result.upserted} elapsed={result.elapsed_seconds:.1f}s "
            f"feature_version={FEATURE_VERSION}"
        )

        if args.show:
            import json

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
