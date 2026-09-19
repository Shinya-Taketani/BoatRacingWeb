"""特徴量生成 第2層: 選手の直近成績・適性など、race_date より前の履歴のみから
導出する特徴量。feature_version = 'v2_recent'。

CLAUDE.md ルール1（リーク防止）:
- 全ての集計は対象レースの race_date より「厳密に前」の日付のレースのみを使う。
  同日の前のレースは含めない（まず除外で実装する。将来含める場合は要検討）。
- start_course（進入コース）は結果が出て初めて確定する値であり、当該レース
  自身の start_course は締切時点では未知（lane とは別物、CLAUDE.mdルール3）。
  そのため「当該進入コースでの過去成績」のような、今回のレースの結果値を
  条件に使う特徴量は作らない。start_course を使うのは、あくまで「過去の
  レース（すでに結果が出ている）でどのコースから進入していたか」という
  過去の事実の集計のみであり、これはリークではない。

構成（2026-09-19 ユーザー確定仕様）:
1. 直近成績: 直近10走の平均着順・1着率・2連対率・3連対率・平均ST
2. 枠番別適性・進入傾向:
   A. 当該枠番での過去1着率（直近50走）
   B1. 当該枠番における過去の平均進入コース（直近50走）
   B2. 過去に前付け（lane より内側の start_course で進入）した割合（直近50走、lane不問）
3. 場別適性: 当該場での過去1着率（直近30走）
4. モーター直近調子: 当該モーター(stadium_id, motor_no)の直近20走の2連対率
   （motor_win_rate_2 は期の累積値であり、これとは別物）
5. 節の進行度: その節の何日目か（同一場で連続した開催日のまとまりを節とみなす）
C. レース単位: 同一レースの6艇のうち、前付け傾向（B2 >= MAEDUKE_THRESHOLD）の
   選手が何人いるか

パフォーマンス設計:
「racer_id ごとに直近N走」は同日除外の都合上、素朴な window の ROWS BETWEEN
では表現できないため、一時テーブル + 索引 + LATERAL/LIMIT の相関サブクエリで
実装する。索引が効けば1行あたりインデックスシーク+LIMITで済み、100万行規模
でも現実的な時間で終わる（まず2024年1ヶ月分で計測する）。
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

FEATURE_VERSION = "v2_recent"
CUTOFF_MARGIN = timedelta(minutes=10)
DEFAULT_BATCH_SIZE = 2000

RECENT_N = 10
LANE_WINDOW_N = 50
MAEDUKE_WINDOW_N = 50
STADIUM_WINDOW_N = 30
MOTOR_WINDOW_N = 20

# 「前付け傾向がある」とみなす過去の前付け率のしきい値。運用しながら見直す前提の
# 初期値であり、公式な基準値ではない。
MAEDUKE_THRESHOLD = 0.2

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
    """LATERAL相関サブクエリの元になる、選手ごとの全履歴を一時テーブルに展開する。"""
    cur.execute("DROP TABLE IF EXISTS racer_race_history")
    cur.execute(
        """
        CREATE TEMP TABLE racer_race_history AS
        SELECT
            re.racer_id, r.race_date, r.id AS race_id, re.lane,
            r.stadium_id, re.motor_no,
            rr.start_course, rr.finish_pos, rr.st
        FROM race_entries re
        JOIN races r ON r.id = re.race_id
        LEFT JOIN race_results rr ON rr.race_entry_id = re.id
        """
    )
    cur.execute(
        "CREATE INDEX ON racer_race_history (racer_id, race_date DESC, race_id DESC)"
    )
    cur.execute(
        "CREATE INDEX ON racer_race_history (racer_id, lane, race_date DESC, race_id DESC)"
    )
    cur.execute(
        "CREATE INDEX ON racer_race_history (racer_id, stadium_id, race_date DESC, race_id DESC)"
    )
    cur.execute(
        "CREATE INDEX ON racer_race_history (stadium_id, motor_no, race_date DESC, race_id DESC)"
    )
    cur.execute("ANALYZE racer_race_history")


def _prepare_day_of_meet_table(cur: psycopg.Cursor) -> None:
    """場ごとに連続した開催日のまとまり（節）を検出し、その節の何日目かを求める。

    gaps-and-islands: 日付から連番*1日を引くと、連続日は同じ値になることを利用する。
    """
    cur.execute("DROP TABLE IF EXISTS stadium_day_of_meet")
    cur.execute(
        """
        CREATE TEMP TABLE stadium_day_of_meet AS
        WITH stadium_days AS (
            SELECT DISTINCT stadium_id, race_date FROM races
        ),
        grp AS (
            SELECT
                stadium_id, race_date,
                race_date - (
                    ROW_NUMBER() OVER (PARTITION BY stadium_id ORDER BY race_date)
                )::int AS island
            FROM stadium_days
        )
        SELECT
            stadium_id, race_date,
            ROW_NUMBER() OVER (PARTITION BY stadium_id, island ORDER BY race_date) AS day_of_meet
        FROM grp
        """
    )
    cur.execute("CREATE INDEX ON stadium_day_of_meet (stadium_id, race_date)")
    cur.execute("ANALYZE stadium_day_of_meet")


_SELECT_SQL = """
    SELECT
        re.race_id, re.lane, r.race_date, r.deadline_at,

        recent.avg_finish_pos, recent.win_rate, recent.place2_rate,
        recent.place3_rate, recent.avg_st, recent.most_recent_history_date,

        lane_stats.lane_win_rate, lane_stats.lane_avg_start_course,

        maeduke.maeduke_rate,

        stadium_stats.stadium_win_rate,

        motor_stats.motor_place2_rate,

        dom.day_of_meet

    FROM race_entries re
    JOIN races r ON r.id = re.race_id
    LEFT JOIN LATERAL (
        SELECT
            avg(finish_pos) AS avg_finish_pos,
            avg((finish_pos = 1)::int::float8) AS win_rate,
            avg((finish_pos <= 2)::int::float8) AS place2_rate,
            avg((finish_pos <= 3)::int::float8) AS place3_rate,
            avg(st) AS avg_st,
            max(race_date) AS most_recent_history_date
        FROM (
            SELECT finish_pos, st, race_date
            FROM racer_race_history h
            WHERE h.racer_id = re.racer_id AND h.race_date < r.race_date
            ORDER BY h.race_date DESC, h.race_id DESC
            LIMIT %(recent_n)s
        ) x
    ) recent ON true
    LEFT JOIN LATERAL (
        SELECT
            avg((finish_pos = 1)::int::float8) AS lane_win_rate,
            avg(start_course) AS lane_avg_start_course
        FROM (
            SELECT finish_pos, start_course
            FROM racer_race_history h
            WHERE h.racer_id = re.racer_id AND h.lane = re.lane AND h.race_date < r.race_date
            ORDER BY h.race_date DESC, h.race_id DESC
            LIMIT %(lane_window_n)s
        ) x
    ) lane_stats ON true
    LEFT JOIN LATERAL (
        SELECT avg((start_course < lane)::int::float8) AS maeduke_rate
        FROM (
            SELECT start_course, lane
            FROM racer_race_history h
            WHERE h.racer_id = re.racer_id AND h.race_date < r.race_date
              AND h.start_course IS NOT NULL
            ORDER BY h.race_date DESC, h.race_id DESC
            LIMIT %(maeduke_window_n)s
        ) x
    ) maeduke ON true
    LEFT JOIN LATERAL (
        SELECT avg((finish_pos = 1)::int::float8) AS stadium_win_rate
        FROM (
            SELECT finish_pos
            FROM racer_race_history h
            WHERE h.racer_id = re.racer_id AND h.stadium_id = r.stadium_id
              AND h.race_date < r.race_date
            ORDER BY h.race_date DESC, h.race_id DESC
            LIMIT %(stadium_window_n)s
        ) x
    ) stadium_stats ON true
    LEFT JOIN LATERAL (
        SELECT avg((finish_pos <= 2)::int::float8) AS motor_place2_rate
        FROM (
            SELECT finish_pos
            FROM racer_race_history h
            WHERE h.stadium_id = r.stadium_id AND h.motor_no = re.motor_no
              AND h.race_date < r.race_date
            ORDER BY h.race_date DESC, h.race_id DESC
            LIMIT %(motor_window_n)s
        ) x
    ) motor_stats ON true
    LEFT JOIN stadium_day_of_meet dom
        ON dom.stadium_id = r.stadium_id AND dom.race_date = r.race_date
    WHERE r.race_date BETWEEN %(start_date)s AND %(end_date)s
    ORDER BY r.race_date, re.race_id, re.lane
"""


def _build_row_payload(row: tuple) -> tuple:
    (
        race_id, lane, race_date, deadline_at,
        avg_finish_pos, win_rate, place2_rate, place3_rate, avg_st, most_recent_history_date,
        lane_win_rate, lane_avg_start_course,
        maeduke_rate,
        stadium_win_rate,
        motor_place2_rate,
        day_of_meet,
    ) = row

    cutoff_at = deadline_at - CUTOFF_MARGIN

    payload = {
        "avg_finish_pos_recent10": float(avg_finish_pos) if avg_finish_pos is not None else None,
        "win_rate_recent10": float(win_rate) if win_rate is not None else None,
        "place2_rate_recent10": float(place2_rate) if place2_rate is not None else None,
        "place3_rate_recent10": float(place3_rate) if place3_rate is not None else None,
        "avg_st_recent10": float(avg_st) if avg_st is not None else None,
        "lane_win_rate_recent50": float(lane_win_rate) if lane_win_rate is not None else None,
        "lane_avg_start_course_recent50": (
            float(lane_avg_start_course) if lane_avg_start_course is not None else None
        ),
        "maeduke_rate_recent50": float(maeduke_rate) if maeduke_rate is not None else None,
        "stadium_win_rate_recent30": (
            float(stadium_win_rate) if stadium_win_rate is not None else None
        ),
        "motor_place2_rate_recent20": (
            float(motor_place2_rate) if motor_place2_rate is not None else None
        ),
        "day_of_meet": day_of_meet,
        # maeduke_count_in_race はこの時点では未確定（同一レースの他5艇の
        # maeduke_rateが必要）。generate_recent_features_range側で後埋めする。
        "maeduke_count_in_race": None,
    }

    return race_id, lane, cutoff_at, most_recent_history_date, race_date, maeduke_rate, payload


def _assert_no_leak_one(
    race_id: int, lane: int, cutoff_at: datetime, most_recent_history_date: date | None
) -> None:
    if most_recent_history_date is None:
        return  # 履歴が無い（新人等）ので検証対象なし
    # 履歴側の日付をJST 00:00のtz-aware datetimeにしてcutoff_atと比較する。
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
        label=f"racer_race_history(race_id={race_id}, lane={lane})",
    )


def _bulk_upsert(cur: psycopg.Cursor, batch: list[tuple]) -> None:
    """batch: (race_id, lane, cutoff_at, payload) のリスト。"""
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


def generate_recent_features_range(
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
        _prepare_day_of_meet_table(prep_cur)
    conn.commit()
    if progress:
        print(f"  prep done ({time.time() - t0:.1f}s)", flush=True)

    with conn.cursor() as read_cur:
        read_cur.execute(
            _SELECT_SQL,
            {
                "start_date": start_date,
                "end_date": end_date,
                "recent_n": RECENT_N,
                "lane_window_n": LANE_WINDOW_N,
                "maeduke_window_n": MAEDUKE_WINDOW_N,
                "stadium_window_n": STADIUM_WINDOW_N,
                "motor_window_n": MOTOR_WINDOW_N,
            },
        )
        raw_rows = read_cur.fetchall()

    if progress:
        print(f"  fetched {len(raw_rows)} rows ({time.time() - t0:.1f}s)", flush=True)

    # レース単位でmaeduke_count_in_raceを後埋めする（6艇分のmaeduke_rateが必要なため）。
    built = [_build_row_payload(row) for row in raw_rows]

    maeduke_by_race: dict[int, list[float]] = {}
    for race_id, _lane, _cutoff, _hist_date, _race_date, maeduke_rate, _payload in built:
        if maeduke_rate is not None:
            maeduke_by_race.setdefault(race_id, []).append(float(maeduke_rate))

    maeduke_count_by_race = {
        race_id: sum(1 for r in rates if r >= MAEDUKE_THRESHOLD)
        for race_id, rates in maeduke_by_race.items()
    }

    batch: list[tuple] = []
    row_count = 0
    upserted = 0

    with conn.cursor() as write_cur:
        for race_id, lane, cutoff_at, hist_date, _race_date, _maeduke_rate, payload in built:
            payload["maeduke_count_in_race"] = maeduke_count_by_race.get(race_id)

            _assert_no_leak_one(race_id, lane, cutoff_at, hist_date)

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
        result = generate_recent_features_range(
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
