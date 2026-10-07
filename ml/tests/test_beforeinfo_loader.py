"""ml.loaders.beforeinfo の統合テスト（実PostgreSQLに接続）。

_beforeinfo_fixtures.py の固定HTMLをパースして得たBeforeInfoPageを、実際に
race_before_info / race_weather_info へupsertできるかを検証する。新規HTTP
リクエストは行わない。

test_racer_periods_compaction.py と同じ方針で、テスト専用のraces行を
作って検証後に削除する（races削除でrace_entries/race_before_info/
race_weather_infoはcascadeで消える）。race_entriesのracer_idには実データの
racersを読み取り専用で参照する（racers自体は変更しない）。DBに接続できない
環境ではスキップする。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ml.fetchers.beforeinfo import parse_before_info_html
from ml.loaders.beforeinfo import LoaderError, load_before_info

from _beforeinfo_fixtures import BEFOREINFO_HTML, RACE_DATE

try:
    from ml.loaders.db import get_connection

    _probe = get_connection()
    _probe.close()
    _DB_AVAILABLE = True
except Exception:
    _DB_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="DBに接続できないためスキップ")

# 実データと衝突しないよう、遠い未来日付をテスト専用のrace_dateとして使う。
_TEST_RACE_DATE = date(2099, 1, 1)
_TEST_RACE_NO = 1
_CAPTURED_AT = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    connection = get_connection()
    yield connection
    connection.close()


@pytest.fixture
def page():
    return parse_before_info_html(BEFOREINFO_HTML, RACE_DATE, captured_at=_CAPTURED_AT)


@pytest.fixture
def stadium_code(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM stadiums ORDER BY code LIMIT 1")
        return cur.fetchone()[0]


def _make_race(conn, stadium_code: int, race_no: int, race_date: date) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO races (race_date, stadium_id, race_no, deadline_at, created_at, updated_at)
            SELECT %s, stadiums.id, %s, %s, now(), now()
            FROM stadiums WHERE stadiums.code = %s
            RETURNING id
            """,
            (
                race_date,
                race_no,
                datetime.combine(race_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=9),
                stadium_code,
            ),
        )
        race_id = cur.fetchone()[0]
    conn.commit()
    return race_id


def _make_entries(conn, race_id: int, lanes: list[int]) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM racers ORDER BY id LIMIT %s", (len(lanes),))
        racer_ids = [row[0] for row in cur.fetchall()]
        assert len(racer_ids) == len(lanes), "racersテーブルに十分なテストデータがありません"
        for lane, racer_id in zip(lanes, racer_ids):
            cur.execute(
                """
                INSERT INTO race_entries (race_id, racer_id, lane, created_at, updated_at)
                VALUES (%s, %s, %s, now(), now())
                """,
                (race_id, racer_id, lane),
            )
    conn.commit()


def _delete_race(conn, race_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM races WHERE id = %s", (race_id,))
    conn.commit()


def test_load_before_info_upserts_boats_and_weather(conn, page, stadium_code):
    race_id = _make_race(conn, stadium_code, _TEST_RACE_NO, _TEST_RACE_DATE)
    try:
        _make_entries(conn, race_id, [1, 2, 3, 4, 5, 6])

        result = load_before_info(
            conn, stadium_code, _TEST_RACE_NO, _TEST_RACE_DATE, page, source="live"
        )
        assert result.race_id == race_id
        assert result.boats_upserted == 6

        with conn.cursor() as cur:
            cur.execute(
                "SELECT lane, weight, exhibit_time, tilt, propeller_changed, parts_exchanged, "
                "course_predicted, st_exhibit, source "
                "FROM race_before_info WHERE race_id = %s ORDER BY lane",
                (race_id,),
            )
            rows = {row[0]: row for row in cur.fetchall()}

        assert len(rows) == 6
        lane1 = rows[1]
        assert float(lane1[1]) == 53.3  # weight
        assert float(lane1[2]) == 6.97  # exhibit_time
        assert float(lane1[3]) == -0.5  # tilt
        assert lane1[4] is True  # propeller_changed
        assert lane1[5] == "キャブ"  # parts_exchanged
        assert rows[3][6] == 1  # lane3.course_predicted (前付け)
        assert rows[6][6] is None  # lane6は展示未公開のまま
        assert lane1[8] == "live"  # source

        with conn.cursor() as cur:
            cur.execute(
                "SELECT temperature, weather_condition, wind_speed, wind_direction_code, "
                "water_temperature, wave_height, measured_at FROM race_weather_info WHERE race_id = %s",
                (race_id,),
            )
            weather_row = cur.fetchone()
        assert weather_row is not None
        assert float(weather_row[0]) == 24.0
        assert weather_row[1] == "晴"
        assert weather_row[6] == datetime(2026, 9, 19, 3, 34, tzinfo=timezone.utc)

        # 再投入してもON CONFLICTでupdateされ、行数が増えないことを確認
        load_before_info(
            conn, stadium_code, _TEST_RACE_NO, _TEST_RACE_DATE, page, source="live"
        )
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM race_before_info WHERE race_id = %s", (race_id,))
            assert cur.fetchone()[0] == 6
    finally:
        _delete_race(conn, race_id)


def test_load_before_info_raises_when_race_entries_missing(conn, page, stadium_code):
    race_id = _make_race(conn, stadium_code, _TEST_RACE_NO, _TEST_RACE_DATE)
    try:
        # lane 6のrace_entriesをわざと欠かす
        _make_entries(conn, race_id, [1, 2, 3, 4, 5])

        with pytest.raises(LoaderError, match=r"lanes=\[6\]"):
            load_before_info(
                conn, stadium_code, _TEST_RACE_NO, _TEST_RACE_DATE, page, source="backfill"
            )

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM race_before_info WHERE race_id = %s", (race_id,))
            assert cur.fetchone()[0] == 0
    finally:
        _delete_race(conn, race_id)


def test_load_before_info_raises_when_race_missing(conn, page, stadium_code):
    with pytest.raises(LoaderError, match="race not found"):
        load_before_info(conn, stadium_code, 99, date(2099, 12, 31), page, source="backfill")


def test_load_before_info_rejects_invalid_source(conn, page, stadium_code):
    with pytest.raises(LoaderError, match="source must be one of"):
        load_before_info(
            conn, stadium_code, _TEST_RACE_NO, _TEST_RACE_DATE, page, source="something-else"
        )
