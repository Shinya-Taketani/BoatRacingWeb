"""racer_periods コンパクション(_compact_one_racer)の統合テスト。

Laravel側(.env)と同じPostgreSQLに接続する統合テスト。実データと衝突しない
よう registration_number に大きな連番を割り当てたテスト専用racerを都度作成し、
テスト終了時に関連行(race_entries -> racer_periods -> racer_daily_snapshots ->
racers)を削除する。DBに接続できない環境ではスキップする。

検証対象:
- 計算済み期間に完全に吸収される既存行が race_entries から未参照なら
  削除して作り直す（自動吸収）
- 参照済みなら CompactionError で停止する（自動修正しない）
- 部分的にしか重ならない行は削除せず CompactionError で停止する
- 日付を昇順/降順/一括(rebuild)のどの順序で投入しても、最終的な
  racer_periods の内容(valid_from/valid_to/値)が一致する
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ml.loaders.db import get_connection
from ml.loaders.racer_periods import CompactionError, compact_racer_periods
from ml.loaders.racer_snapshots import period_key_for

try:
    _probe = get_connection()
    _probe.close()
    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DBに接続できないためスキップ")

# 実データ(登録番号は概ね数千まで)と衝突しないテスト専用の登録番号帯。
_TEST_REG_NO_BASE = 9_000_000

# (racer_class, branch, national_win_rate, national_win_rate_2, local_win_rate, local_win_rate_2)
V1 = ("A1", None, Decimal("6.50"), Decimal("7.00"), Decimal("6.80"), Decimal("7.10"))
V2 = ("A1", None, Decimal("5.00"), Decimal("5.50"), Decimal("5.20"), Decimal("5.60"))


@pytest.fixture
def conn():
    connection = get_connection()
    yield connection
    connection.close()


@pytest.fixture
def racer_factory(conn):
    """テスト用racerを作るファクトリ。終了時に関連行を削除する。"""
    created: list[int] = []
    counter = 0

    def _make() -> int:
        nonlocal counter
        counter += 1
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO racers (registration_number, name, created_at, updated_at)
                VALUES (%s, %s, now(), now())
                RETURNING id
                """,
                (_TEST_REG_NO_BASE + counter, f"test-racer-{counter}"),
            )
            racer_id = cur.fetchone()[0]
        conn.commit()
        created.append(racer_id)
        return racer_id

    yield _make

    # テスト本体が例外(CompactionError等)で失敗すると、そのSQLエラーで
    # 接続がaborted transaction状態のまま残る。ロールバックしないと
    # 後始末のDELETEも全て失敗し、残留行が次回以降のテストを巻き込んで
    # 壊す(登録番号やrace一意制約の衝突)ため、必ず先に立て直す。
    conn.rollback()

    with conn.cursor() as cur:
        for racer_id in created:
            cur.execute("SELECT DISTINCT race_id FROM race_entries WHERE racer_id = %s", (racer_id,))
            race_ids = [row[0] for row in cur.fetchall()]

            cur.execute(
                "DELETE FROM race_entries WHERE racer_id = %s OR racer_period_id IN "
                "(SELECT id FROM racer_periods WHERE racer_id = %s)",
                (racer_id, racer_id),
            )
            if race_ids:
                cur.execute("DELETE FROM races WHERE id = ANY(%s)", (race_ids,))
            cur.execute("DELETE FROM racer_periods WHERE racer_id = %s", (racer_id,))
            cur.execute("DELETE FROM racer_daily_snapshots WHERE racer_id = %s", (racer_id,))
            cur.execute("DELETE FROM racers WHERE id = %s", (racer_id,))
    conn.commit()


def _insert_daily_snapshot(conn, racer_id: int, observed_date: date, values: tuple) -> None:
    racer_class, branch, nwr, nwr2, lwr, lwr2 = values
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO racer_daily_snapshots (
                racer_id, observed_date, period_key, racer_class, branch,
                national_win_rate, national_win_rate_2, local_win_rate, local_win_rate_2,
                source_file, imported_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (racer_id, observed_date) DO UPDATE SET
                racer_class = EXCLUDED.racer_class,
                national_win_rate = EXCLUDED.national_win_rate,
                national_win_rate_2 = EXCLUDED.national_win_rate_2,
                local_win_rate = EXCLUDED.local_win_rate,
                local_win_rate_2 = EXCLUDED.local_win_rate_2
            """,
            (
                racer_id, observed_date, period_key_for(observed_date), racer_class, branch,
                nwr, nwr2, lwr, lwr2, "test",
            ),
        )
    conn.commit()


def _insert_existing_period(
    conn, racer_id: int, valid_from: date, valid_to: date | None, values: tuple
) -> int:
    racer_class, branch, nwr, nwr2, lwr, lwr2 = values
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO racer_periods (
                racer_id, valid_from, valid_to, racer_class, branch,
                national_win_rate, national_win_rate_2, local_win_rate, local_win_rate_2,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
            RETURNING id
            """,
            (racer_id, valid_from, valid_to, racer_class, branch, nwr, nwr2, lwr, lwr2),
        )
        period_id = cur.fetchone()[0]
    conn.commit()
    return period_id


def _link_race_entry(conn, racer_id: int, racer_period_id: int, race_date: date) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM stadiums ORDER BY id LIMIT 1")
        stadium_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO races (race_date, stadium_id, race_no, deadline_at, created_at, updated_at)
            VALUES (%s, %s, 1, %s, now(), now())
            RETURNING id
            """,
            (race_date, stadium_id, datetime.combine(race_date, datetime.min.time(), tzinfo=timezone.utc)),
        )
        race_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO race_entries (race_id, racer_id, racer_period_id, lane, created_at, updated_at)
            VALUES (%s, %s, %s, 1, now(), now())
            RETURNING id
            """,
            (race_id, racer_id, racer_period_id),
        )
        entry_id = cur.fetchone()[0]
    conn.commit()
    return entry_id


def _fetch_periods(conn, racer_id: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT valid_from, valid_to, racer_class, branch,
                   national_win_rate, national_win_rate_2, local_win_rate, local_win_rate_2
            FROM racer_periods WHERE racer_id = %s ORDER BY valid_from
            """,
            (racer_id,),
        )
        return [
            (row[0].date(), row[1].date() if row[1] else None, *row[2:])
            for row in cur.fetchall()
        ]


# --- 1. 未参照の吸収行は削除して作り直す -------------------------------------

def test_unreferenced_absorbed_row_is_deleted_and_reinserted(conn, racer_factory):
    racer_id = racer_factory()

    old_period_id = _insert_existing_period(
        conn, racer_id, date(2030, 6, 10), None, V1
    )
    # race_entriesからは参照させない（未参照のまま）。
    _insert_daily_snapshot(conn, racer_id, date(2030, 6, 8), V1)

    summary = compact_racer_periods(conn, racer_ids=[racer_id])

    assert summary.deleted == 1
    assert summary.inserted == 1
    periods = _fetch_periods(conn, racer_id)
    assert periods == [(date(2030, 6, 8), None, *V1)]

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM racer_periods WHERE id = %s", (old_period_id,))
        assert cur.fetchone()[0] == 0


# --- 2. 参照済みの吸収行は CompactionError で停止する -------------------------

def test_referenced_absorbed_row_raises_compaction_error(conn, racer_factory):
    racer_id = racer_factory()

    old_period_id = _insert_existing_period(
        conn, racer_id, date(2030, 6, 10), None, V1
    )
    _link_race_entry(conn, racer_id, old_period_id, date(2030, 6, 15))
    _insert_daily_snapshot(conn, racer_id, date(2030, 6, 8), V1)

    with pytest.raises(CompactionError) as exc_info:
        compact_racer_periods(conn, racer_ids=[racer_id])

    message = str(exc_info.value)
    assert f"id={old_period_id}" in message
    assert "valid_from=2030-06-10" in message
    assert "referenced by 1 race_entries row" in message

    # ロールバックされ、既存行はそのまま残っている。
    periods = _fetch_periods(conn, racer_id)
    assert periods == [(date(2030, 6, 10), None, *V1)]


# --- 3. 部分的にしか重ならない行は CompactionError で停止する -----------------

def test_partial_overlap_raises_compaction_error(conn, racer_factory):
    racer_id = racer_factory()

    old_period_id = _insert_existing_period(
        conn, racer_id, date(2030, 6, 10), date(2030, 6, 20), V1
    )
    # computed period は [6/8, 6/15) V1 になり、既存行[6/10,6/20)をはみ出して重なる。
    _insert_daily_snapshot(conn, racer_id, date(2030, 6, 8), V1)
    _insert_daily_snapshot(conn, racer_id, date(2030, 6, 15), V2)

    with pytest.raises(CompactionError) as exc_info:
        compact_racer_periods(conn, racer_ids=[racer_id])

    message = str(exc_info.value)
    assert f"id={old_period_id}" in message
    assert "partially overlaps" in message

    periods = _fetch_periods(conn, racer_id)
    assert periods == [(date(2030, 6, 10), date(2030, 6, 20), *V1)]


# --- 4. 投入順序(昇順/降順/一括)によらず最終結果が一致する ---------------------

_ORDER_DATES = [
    date(2030, 6, 1), date(2030, 6, 3), date(2030, 6, 5),
    date(2030, 6, 7), date(2030, 6, 9), date(2030, 6, 11),
]
_ORDER_VALUES = [V1, V1, V2, V2, V1, V1]
_EXPECTED_PERIODS = [
    (date(2030, 6, 1), date(2030, 6, 5), *V1),
    (date(2030, 6, 5), date(2030, 6, 9), *V2),
    (date(2030, 6, 9), None, *V1),
]


def test_forward_order_backfill(conn, racer_factory):
    racer_id = racer_factory()
    for d, values in zip(_ORDER_DATES, _ORDER_VALUES):
        _insert_daily_snapshot(conn, racer_id, d, values)
        compact_racer_periods(conn, racer_ids=[racer_id])

    assert _fetch_periods(conn, racer_id) == _EXPECTED_PERIODS


def test_backward_order_backfill_matches_forward_order(conn, racer_factory):
    racer_id = racer_factory()
    for d, values in reversed(list(zip(_ORDER_DATES, _ORDER_VALUES))):
        _insert_daily_snapshot(conn, racer_id, d, values)
        summary = compact_racer_periods(conn, racer_ids=[racer_id])
        # 逆順で投入すると、後から追加した過去日付が既存の「境界」行を
        # 吸収するタイミングが必ず一度は発生するはず（未参照なので自動削除される）。
        assert summary.deleted >= 0  # 発生有無はステップに依存するため件数は問わない

    assert _fetch_periods(conn, racer_id) == _EXPECTED_PERIODS


def test_single_shot_bulk_insert_matches_forward_order(conn, racer_factory):
    racer_id = racer_factory()
    for d, values in zip(_ORDER_DATES, _ORDER_VALUES):
        _insert_daily_snapshot(conn, racer_id, d, values)

    compact_racer_periods(conn, racer_ids=[racer_id])

    assert _fetch_periods(conn, racer_id) == _EXPECTED_PERIODS


def test_rebuild_flag_reproduces_same_periods(conn, racer_factory):
    racer_id = racer_factory()
    for d, values in zip(_ORDER_DATES, _ORDER_VALUES):
        _insert_daily_snapshot(conn, racer_id, d, values)
    compact_racer_periods(conn, racer_ids=[racer_id])
    assert _fetch_periods(conn, racer_id) == _EXPECTED_PERIODS

    # 既存periodsをすべて削除してから一括で作り直しても同じ結果になる
    # （このテストのracerはrace_entriesから参照されていないのでDELETEは通る）。
    compact_racer_periods(conn, racer_ids=[racer_id], rebuild=True)

    assert _fetch_periods(conn, racer_id) == _EXPECTED_PERIODS


def test_backward_order_actually_exercises_the_delete_path(conn, racer_factory):
    """降順投入では最低1回は「未参照の吸収→自動削除」が起きることを確認する。"""
    racer_id = racer_factory()
    total_deleted = 0
    for d, values in reversed(list(zip(_ORDER_DATES, _ORDER_VALUES))):
        _insert_daily_snapshot(conn, racer_id, d, values)
        summary = compact_racer_periods(conn, racer_ids=[racer_id])
        total_deleted += summary.deleted

    assert total_deleted >= 1
    assert _fetch_periods(conn, racer_id) == _EXPECTED_PERIODS
