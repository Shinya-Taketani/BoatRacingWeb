"""beforeinfoのパース結果を race_before_info / race_weather_info へ upsert するローダー。

race_entries が存在しない(race_id, lane)への投入は黙ってスキップせず
LoaderError を送出する。beforeinfoは番組表(races/race_entries)より後に
公開される情報のため、対応するrace_entriesが無いのは「まだ番組表を
取り込んでいない」等の設定ミスを示すシグナルであり、隠すと後から
気づけなくなる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg

from ml.fetchers.beforeinfo import BeforeInfoPage


class LoaderError(ValueError):
    """race_before_info / race_weather_info への投入に失敗した場合に送出する。"""


@dataclass(frozen=True)
class BeforeInfoLoadResult:
    race_id: int
    boats_upserted: int
    weather_upserted: bool


def _fetch_race_id(
    cur: psycopg.Cursor, stadium_code: int, race_no: int, race_date: date
) -> int | None:
    cur.execute(
        """
        SELECT races.id
        FROM races
        JOIN stadiums ON stadiums.id = races.stadium_id
        WHERE stadiums.code = %s AND races.race_no = %s AND races.race_date = %s
        """,
        (stadium_code, race_no, race_date),
    )
    row = cur.fetchone()
    return row[0] if row is not None else None


def _fetch_entry_lanes(cur: psycopg.Cursor, race_id: int) -> set[int]:
    cur.execute("SELECT lane FROM race_entries WHERE race_id = %s", (race_id,))
    return {row[0] for row in cur.fetchall()}


_VALID_SOURCES = frozenset({"live", "backfill"})


def load_before_info(
    conn: psycopg.Connection,
    stadium_code: int,
    race_no: int,
    race_date: date,
    page: BeforeInfoPage,
    *,
    source: str,
) -> BeforeInfoLoadResult:
    """source: 'live'(T-12分予約ジョブでの取得) または 'backfill'(過去分の後日取得)。

    captured_atは「取得した時刻」であって「サイトが公開した時刻」ではない。
    backfillではcaptured_atが取得作業を行った日時になり対象レースの締切より
    ずっと後になるため、ml.features.exhibitionのリーク検証はこのsource列で
    分岐する(2026-09-27に発覚)。呼び出し側で明示させることで、どちらか
    忘れたまま投入するミスを防ぐ。
    """
    if source not in _VALID_SOURCES:
        raise LoaderError(f"source must be one of {sorted(_VALID_SOURCES)}, got {source!r}")

    context = f"jcd={stadium_code}, rno={race_no}, hd={race_date:%Y%m%d}"

    try:
        with conn.cursor() as cur:
            race_id = _fetch_race_id(cur, stadium_code, race_no, race_date)
            if race_id is None:
                raise LoaderError(
                    f"race not found: {context} (racesを先に投入すること)"
                )

            entry_lanes = _fetch_entry_lanes(cur, race_id)
            missing_lanes = sorted(set(page.boats) - entry_lanes)
            if missing_lanes:
                raise LoaderError(
                    f"race_entries not found for race_id={race_id} lanes={missing_lanes} "
                    f"({context}; race_entriesを先に投入すること)"
                )

            boats_upserted = 0
            for lane, boat in sorted(page.boats.items()):
                cur.execute(
                    """
                    INSERT INTO race_before_info (
                        race_id, lane, weight, adjusted_weight, exhibit_time, tilt,
                        propeller_changed, parts_exchanged, course_predicted, st_exhibit,
                        captured_at, source, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                    ON CONFLICT (race_id, lane) DO UPDATE SET
                        weight = EXCLUDED.weight,
                        adjusted_weight = EXCLUDED.adjusted_weight,
                        exhibit_time = EXCLUDED.exhibit_time,
                        tilt = EXCLUDED.tilt,
                        propeller_changed = EXCLUDED.propeller_changed,
                        parts_exchanged = EXCLUDED.parts_exchanged,
                        course_predicted = EXCLUDED.course_predicted,
                        st_exhibit = EXCLUDED.st_exhibit,
                        captured_at = EXCLUDED.captured_at,
                        source = EXCLUDED.source,
                        updated_at = now()
                    """,
                    (
                        race_id,
                        lane,
                        boat.weight,
                        boat.adjusted_weight,
                        boat.exhibit_time,
                        boat.tilt,
                        boat.propeller_changed,
                        boat.parts_exchanged,
                        boat.course_predicted,
                        boat.st_exhibit,
                        page.captured_at,
                        source,
                    ),
                )
                boats_upserted += 1

            weather = page.weather
            cur.execute(
                """
                INSERT INTO race_weather_info (
                    race_id, temperature, weather_condition, wind_speed, wind_direction_code,
                    water_temperature, wave_height, measured_at, captured_at, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (race_id) DO UPDATE SET
                    temperature = EXCLUDED.temperature,
                    weather_condition = EXCLUDED.weather_condition,
                    wind_speed = EXCLUDED.wind_speed,
                    wind_direction_code = EXCLUDED.wind_direction_code,
                    water_temperature = EXCLUDED.water_temperature,
                    wave_height = EXCLUDED.wave_height,
                    measured_at = EXCLUDED.measured_at,
                    captured_at = EXCLUDED.captured_at,
                    updated_at = now()
                """,
                (
                    race_id,
                    weather.temperature,
                    weather.weather_condition,
                    weather.wind_speed,
                    weather.wind_direction_code,
                    weather.water_temperature,
                    weather.wave_height,
                    weather.measured_at,
                    page.captured_at,
                ),
            )
    except Exception:
        conn.rollback()
        raise

    conn.commit()
    return BeforeInfoLoadResult(race_id=race_id, boats_upserted=boats_upserted, weather_upserted=True)
