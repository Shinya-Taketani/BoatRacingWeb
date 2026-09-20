"""racers テーブルへの get-or-create ヘルパー（racer_snapshots / races 共通）。"""

from __future__ import annotations

import psycopg


def get_or_create_racer(cur: psycopg.Cursor, registration_number: int, name: str) -> int:
    cur.execute(
        """
        INSERT INTO racers (registration_number, name, created_at, updated_at)
        VALUES (%s, %s, now(), now())
        ON CONFLICT (registration_number) DO UPDATE SET
            name = EXCLUDED.name,
            updated_at = now()
        RETURNING id
        """,
        (registration_number, name),
    )
    return cur.fetchone()[0]
