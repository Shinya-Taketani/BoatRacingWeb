"""番組表(B)のパース結果を races / race_entries へ upsert するローダー。

前提: このローダーを呼ぶ前に、対象レース日の racer_daily_snapshots が
取り込まれ、compact_racer_periods が実行済みであること。
race_entries.racer_period_id はその race_date に有効な racer_periods を
検索して設定し、見つからない場合は黙って NULL にせず LoaderError を送出する
（CLAUDE.md ルール1のリーク防止・配信時点スナップショット固定の要）。

例外: require_racer_period=False（backfillの --defer-compaction）の場合のみ、
見つからなくてもエラーにせず racer_period_id を NULL のまま挿入する。
これは「投入だけ先に済ませ、全期間を一括で貼り直す」運用を想定した一時的な
状態で、通常の投入経路（races:fetch-today 等）では使わない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg

from ml.loaders.racers import get_or_create_racer
from ml.parsers.program import ParsedProgram, RaceEntryRecord, RaceRecord

logger = logging.getLogger(__name__)


class LoaderError(ValueError):
    """races / race_entries への投入に失敗した場合に送出する。"""


@dataclass(frozen=True)
class RaceLoadResult:
    races_upserted: int
    entries_upserted: int
    warnings: list[str] = field(default_factory=list)


def _fetch_stadium_ids(cur: psycopg.Cursor) -> dict[int, int]:
    cur.execute("SELECT code, id FROM stadiums")
    return dict(cur.fetchall())


def _stadium_id(stadium_ids: dict[int, int], code: int) -> int:
    try:
        return stadium_ids[code]
    except KeyError as exc:
        raise LoaderError(
            f"stadium code={code} is not seeded in stadiums (run StadiumSeeder first)"
        ) from exc


def _upsert_race(cur: psycopg.Cursor, race: RaceRecord, stadium_id: int) -> int:
    cur.execute(
        """
        INSERT INTO races (race_date, stadium_id, race_no, deadline_at, distance_m, title,
                            event_name, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (race_date, stadium_id, race_no) DO UPDATE SET
            deadline_at = EXCLUDED.deadline_at,
            distance_m = EXCLUDED.distance_m,
            title = EXCLUDED.title,
            event_name = EXCLUDED.event_name,
            updated_at = now()
        RETURNING id
        """,
        (
            race.race_date, stadium_id, race.race_no, race.deadline_at, race.distance_m,
            race.title, race.event_name,
        ),
    )
    return cur.fetchone()[0]


def _find_racer_period_id(
    cur: psycopg.Cursor, racer_id: int, race_date, *, required: bool = True
) -> int | None:
    cur.execute(
        """
        SELECT id FROM racer_periods
        WHERE racer_id = %s AND valid_from <= %s AND (valid_to IS NULL OR %s < valid_to)
        ORDER BY valid_from DESC
        LIMIT 1
        """,
        (racer_id, race_date, race_date),
    )
    row = cur.fetchone()
    if row is None:
        if not required:
            return None
        raise LoaderError(
            f"no racer_periods found for racer_id={racer_id} valid at race_date={race_date}. "
            "Load racer_daily_snapshots and run compact_racer_periods for this racer/date first."
        )
    return row[0]


def _upsert_race_entry(
    cur: psycopg.Cursor, race_id: int, entry: RaceEntryRecord, *, require_racer_period: bool = True
) -> None:
    racer_id = get_or_create_racer(cur, entry.racer.registration_number, entry.racer.name)
    racer_period_id = _find_racer_period_id(
        cur, racer_id, entry.race_date, required=require_racer_period
    )

    cur.execute(
        """
        INSERT INTO race_entries (
            race_id, racer_id, racer_period_id, age, lane,
            motor_no, motor_win_rate_2, boat_no, boat_win_rate_2, weight,
            created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (race_id, lane) DO UPDATE SET
            racer_id = EXCLUDED.racer_id,
            racer_period_id = EXCLUDED.racer_period_id,
            age = EXCLUDED.age,
            motor_no = EXCLUDED.motor_no,
            motor_win_rate_2 = EXCLUDED.motor_win_rate_2,
            boat_no = EXCLUDED.boat_no,
            boat_win_rate_2 = EXCLUDED.boat_win_rate_2,
            weight = EXCLUDED.weight,
            updated_at = now()
        """,
        (
            race_id,
            racer_id,
            racer_period_id,
            entry.racer.age,
            entry.lane,
            entry.motor_no,
            entry.motor_win_rate_2,
            entry.boat_no,
            entry.boat_win_rate_2,
            entry.racer.weight,
        ),
    )


def upsert_races_and_entries(
    conn: psycopg.Connection, program: ParsedProgram, *, require_racer_period: bool = True
) -> RaceLoadResult:
    try:
        with conn.cursor() as cur:
            stadium_ids = _fetch_stadium_ids(cur)

            race_ids: dict[tuple, int] = {}
            races_upserted = 0
            for race in program.races:
                stadium_id = _stadium_id(stadium_ids, race.stadium_code)
                race_id = _upsert_race(cur, race, stadium_id)
                race_ids[(race.stadium_code, race.race_no)] = race_id
                races_upserted += 1

            entries_upserted = 0
            for entry in program.entries:
                race_id = race_ids[(entry.stadium_code, entry.race_no)]
                _upsert_race_entry(cur, race_id, entry, require_racer_period=require_racer_period)
                entries_upserted += 1
    except Exception:
        conn.rollback()
        raise

    conn.commit()
    return RaceLoadResult(races_upserted=races_upserted, entries_upserted=entries_upserted)
