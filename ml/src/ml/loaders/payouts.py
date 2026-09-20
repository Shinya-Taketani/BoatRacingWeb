"""競走成績(K)のパース結果を payouts へ upsert するローダー。"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ml.parsers.result import PayoutRecord

logger_name = __name__


class LoaderError(ValueError):
    """payouts への投入に失敗した場合に送出する。"""


@dataclass(frozen=True)
class PayoutLoadResult:
    payouts_upserted: int


def _fetch_stadium_ids(cur: psycopg.Cursor) -> dict[int, int]:
    cur.execute("SELECT code, id FROM stadiums")
    return dict(cur.fetchall())


def _fetch_race_ids(cur: psycopg.Cursor, race_dates: list) -> dict[tuple, int]:
    cur.execute(
        "SELECT race_date, stadium_id, race_no, id FROM races WHERE race_date = ANY(%s)",
        (race_dates,),
    )
    return {(row[0], row[1], row[2]): row[3] for row in cur.fetchall()}


def upsert_payouts(conn: psycopg.Connection, payouts: list[PayoutRecord]) -> PayoutLoadResult:
    if not payouts:
        return PayoutLoadResult(payouts_upserted=0)

    try:
        with conn.cursor() as cur:
            stadium_ids = _fetch_stadium_ids(cur)
            race_dates = sorted({po.race_date for po in payouts})
            race_ids = _fetch_race_ids(cur, race_dates)

            upserted = 0
            for po in payouts:
                stadium_id = stadium_ids.get(po.stadium_code)
                if stadium_id is None:
                    raise LoaderError(
                        f"stadium code={po.stadium_code} is not seeded in stadiums"
                    )

                race_id = race_ids.get((po.race_date, stadium_id, po.race_no))
                if race_id is None:
                    raise LoaderError(
                        f"race not found: race_date={po.race_date} stadium_code={po.stadium_code} "
                        f"race_no={po.race_no} (races/race_entriesを先に投入すること)"
                    )

                cur.execute(
                    """
                    INSERT INTO payouts (race_id, bet_type, combination, payout, popularity_rank,
                                          created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, now(), now())
                    ON CONFLICT (race_id, bet_type, combination) DO UPDATE SET
                        payout = EXCLUDED.payout,
                        popularity_rank = EXCLUDED.popularity_rank,
                        updated_at = now()
                    """,
                    (race_id, po.bet_type, po.combination, po.payout, po.popularity_rank),
                )
                upserted += 1
    except Exception:
        conn.rollback()
        raise

    conn.commit()
    return PayoutLoadResult(payouts_upserted=upserted)
