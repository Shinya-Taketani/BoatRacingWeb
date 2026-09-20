"""番組表(B)ファイルパーサーのテスト。

data/raw/ に置かれた実ファイル(B260916.TXT、2026-09-16 分)を使って
実データでの動作確認を行う。実ファイルが無い環境（CI等でダウンロード
していない場合）ではスキップする。
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ml.parsers.program import (
    ProgramParseError,
    parse_program_bytes,
    parse_program_path,
)

JST = timezone(timedelta(hours=9))

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "raw" / "B260916.TXT"

pytestmark = pytest.mark.skipif(
    not DATA_PATH.exists(),
    reason=f"real fixture not found: {DATA_PATH} (see ml/data/raw/ download instructions)",
)


@pytest.fixture(scope="module")
def parsed():
    return parse_program_path(DATA_PATH)


def test_race_and_entry_counts(parsed):
    assert len(parsed.races) == 156
    assert len(parsed.entries) == 936


def test_every_race_has_exactly_six_entries(parsed):
    counts = Counter((e.stadium_code, e.race_no) for e in parsed.entries)
    assert counts
    assert all(v == 6 for v in counts.values())


def test_first_race_of_karatsu_matches_known_values(parsed):
    race = next(r for r in parsed.races if r.stadium_code == 23 and r.race_no == 1)

    assert race.race_date == date(2026, 9, 16)
    assert race.title == "朝1戦"


def test_first_race_deadline_is_jst_aware(parsed):
    race = next(r for r in parsed.races if r.stadium_code == 23 and r.race_no == 1)

    assert race.deadline_at.tzinfo == JST
    assert race.deadline_at.hour == 8
    assert race.deadline_at.minute == 44
    assert race.deadline_at.year == 2026
    assert race.deadline_at.month == 9
    assert race.deadline_at.day == 16


def test_known_entry_lane1_race1_karatsu(parsed):
    entry = next(
        e
        for e in parsed.entries
        if e.stadium_code == 23 and e.race_no == 1 and e.lane == 1
    )

    assert entry.racer.registration_number == 3484
    assert entry.racer.name == "芝田浩治"
    assert entry.racer.age == 54
    assert entry.racer.branch == "兵庫"
    assert entry.racer.racer_class == "A2"
    assert entry.racer.weight == Decimal("53")
    assert entry.racer.national_win_rate == Decimal("5.89")
    assert entry.racer.national_win_rate_2 == Decimal("45.63")
    assert entry.racer.local_win_rate == Decimal("6.43")
    assert entry.racer.local_win_rate_2 == Decimal("42.86")
    assert entry.motor_no == 34
    assert entry.motor_win_rate_2 == Decimal("0.00")
    assert entry.boat_no == 60
    assert entry.boat_win_rate_2 == Decimal("28.81")


def test_motor_win_rate_2_two_digit_value_not_truncated(parsed):
    """motor_win_rate_2が10%以上(十の位あり)のケース。過去に_OFF_MOTOR_WIN_RATE_2の
    バイトオフセットが1つずれており、十の位が欠落するバグがあった
    （例: 22.11 -> 2.11）。上の0.00のような1桁の値ではこのバグを検出できない
    ため、2桁のケースを別途固定する。
    """
    entry = next(e for e in parsed.entries if e.racer.registration_number == 4856)

    assert entry.motor_no == 24
    assert entry.motor_win_rate_2 == Decimal("22.11")
    assert entry.boat_no == 46
    assert entry.boat_win_rate_2 == Decimal("27.45")


def test_all_entries_have_valid_racer_class(parsed):
    valid = {"A1", "A2", "B1", "B2"}
    assert all(e.racer.racer_class in valid for e in parsed.entries)


def test_all_entries_lane_in_range(parsed):
    assert all(1 <= e.lane <= 6 for e in parsed.entries)


def test_truncated_racer_line_raises():
    raw = DATA_PATH.read_bytes()
    lines = raw.split(b"\r\n")
    lines[18] = lines[18][:-1]  # 1バイト欠落させて桁ズレを起こす

    with pytest.raises(ProgramParseError):
        parse_program_bytes(b"\r\n".join(lines))


def test_swapped_lane_order_raises():
    raw = DATA_PATH.read_bytes()
    lines = raw.split(b"\r\n")
    lines[18], lines[19] = lines[19], lines[18]

    with pytest.raises(ProgramParseError):
        parse_program_bytes(b"\r\n".join(lines))


def test_invalid_racer_class_raises():
    raw = DATA_PATH.read_bytes()
    lines = raw.split(b"\r\n")
    b = bytearray(lines[18])
    b[22:24] = b"ZZ"
    lines[18] = bytes(b)

    with pytest.raises(ProgramParseError):
        parse_program_bytes(b"\r\n".join(lines))


def test_missing_startb_marker_raises():
    with pytest.raises(ProgramParseError):
        parse_program_bytes(b"NOTSTARTB\r\nFINALB")
