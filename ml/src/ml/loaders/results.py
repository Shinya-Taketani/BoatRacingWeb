"""競走成績(K)のパース結果を race_results へ upsert するローダー。

race_results は race_entries への1:1参照（race_entry_id が一意）であり、
K側の (stadium_code, race_date, race_no, lane) から races/race_entries を
逆引きする。対応する race_entries が存在しない場合は黙って捨てず
LoaderError を送出する（先に loaders.races で番組表を投入しておく必要がある）。
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ml.parsers.result import ParsedResult, RaceResultRecord


class LoaderError(ValueError):
    """race_results への投入に失敗した場合に送出する。"""


@dataclass(frozen=True)
class ResultLoadResult:
    results_upserted: int


def _find_race_entry_id(cur: psycopg.Cursor, stadium_code: int, result: RaceResultRecord) -> int:
    cur.execute(
        """
        SELECT re.id
        FROM race_entries re
        JOIN races r ON r.id = re.race_id
        JOIN stadiums s ON s.id = r.stadium_id
        WHERE s.code = %s AND r.race_date = %s AND r.race_no = %s AND re.lane = %s
        """,
        (stadium_code, result.race_date, result.race_no, result.lane),
    )
    row = cur.fetchone()
    if row is None:
        raise LoaderError(
            f"no race_entries found for stadium_code={stadium_code} "
            f"race_date={result.race_date} race_no={result.race_no} lane={result.lane}. "
            "Load races/race_entries (loaders.races) for this day first."
        )
    return row[0]


def _upsert_race_result(cur: psycopg.Cursor, race_entry_id: int, result: RaceResultRecord) -> None:
    cur.execute(
        """
        INSERT INTO race_results (
            race_entry_id, start_course, st, finish_pos, abnormal_code, race_time,
            created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (race_entry_id) DO UPDATE SET
            start_course = EXCLUDED.start_course,
            st = EXCLUDED.st,
            finish_pos = EXCLUDED.finish_pos,
            abnormal_code = EXCLUDED.abnormal_code,
            race_time = EXCLUDED.race_time,
            updated_at = now()
        """,
        (
            race_entry_id,
            result.start_course,
            result.st,
            result.finish_pos,
            result.status,
            result.race_time,
        ),
    )


def upsert_race_results(conn: psycopg.Connection, parsed: ParsedResult) -> ResultLoadResult:
    try:
        with conn.cursor() as cur:
            upserted = 0
            for result in parsed.results:
                race_entry_id = _find_race_entry_id(cur, result.stadium_code, result)
                _upsert_race_result(cur, race_entry_id, result)
                upserted += 1
    except Exception:
        conn.rollback()
        raise

    conn.commit()
    return ResultLoadResult(results_upserted=upserted)
