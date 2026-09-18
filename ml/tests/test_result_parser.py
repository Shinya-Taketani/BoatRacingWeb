"""競走成績(K)ファイルパーサーのテスト。

data/raw/ に置かれた実ファイル(K260916.TXT、2026-09-16 分)を使って
実データでの動作確認を行う。program.py（番組表(B)）との突合テストも
ここに含める。実ファイルが無い環境では自動的にスキップする。
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from ml.parsers.program import parse_program_path
from ml.parsers.result import ResultParseError, parse_result_bytes, parse_result_path

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"
B_PATH = DATA_DIR / "B260916.TXT"
K_PATH = DATA_DIR / "K260916.TXT"

pytestmark = pytest.mark.skipif(
    not K_PATH.exists(),
    reason=f"real fixture not found: {K_PATH} (see ml/data/raw/ download instructions)",
)


@pytest.fixture(scope="module")
def parsed():
    return parse_result_path(K_PATH)


def test_result_count(parsed):
    assert len(parsed.results) == 936


def test_every_race_has_exactly_six_results_with_distinct_lanes(parsed):
    groups: dict[tuple[int, int], list[int]] = {}
    for r in parsed.results:
        groups.setdefault((r.stadium_code, r.race_no), []).append(r.lane)

    assert len(groups) == 156
    for lanes in groups.values():
        assert sorted(lanes) == [1, 2, 3, 4, 5, 6]


def test_normal_row_known_values(parsed):
    r = next(
        x
        for x in parsed.results
        if x.stadium_code == 23 and x.race_no == 1 and x.lane == 1
    )

    assert r.registration_number == 3484
    assert r.name == "芝田浩治"
    assert r.start_course == 1
    assert r.st == Decimal("0.03")
    assert r.finish_pos == 1
    assert r.status is None
    assert r.race_time == Decimal("111.2")  # 1分51.2秒


def test_flying_start_st_is_negated(parsed):
    rows = [r for r in parsed.results if r.status == "F"]
    assert rows
    for r in rows:
        assert r.finish_pos is None
        assert r.st is not None
        assert r.st < 0


def test_disqualification_after_start_keeps_st_but_no_finish_pos(parsed):
    rows = [r for r in parsed.results if r.status in {"S0", "S1"}]
    assert rows
    for r in rows:
        assert r.finish_pos is None
        assert r.start_course is not None
        assert r.st is not None
        assert r.st >= 0
        assert r.race_time is None


def test_absence_has_no_start_data(parsed):
    rows = [r for r in parsed.results if r.status in {"K0", "K1"}]
    assert rows
    for r in rows:
        assert r.finish_pos is None
        assert r.start_course is None
        assert r.st is None
        assert r.race_time is None


L_SAMPLE_PATH = DATA_DIR / "K231007.TXT"


@pytest.mark.skipif(
    not L_SAMPLE_PATH.exists(),
    reason=f"real fixture not found: {L_SAMPLE_PATH} (出遅れ(L)実例のバックフィル取得時に確認したファイル)",
)
def test_delayed_start_has_no_st_like_absence():
    # 実データで確認: L(出遅れ)は "L ." というK(欠場)と同じ「値なし」表記であり、
    # Fのような数値+符号反転(-0.01等)ではない。当初はFと同様に数値として
    # パースしようとして ResultParseError になっていた不具合の再発防止。
    result = parse_result_path(L_SAMPLE_PATH)
    rows = [r for r in result.results if r.status in {"L0", "L1"}]
    assert rows
    for r in rows:
        assert r.finish_pos is None
        assert r.st is None


def test_finish_pos_and_status_are_mutually_exclusive(parsed):
    # finish_pos と status はどちらか一方のみが埋まり、両方None/両方値ありにはならない
    for r in parsed.results:
        assert (r.finish_pos is None) != (r.status is None)


def test_unknown_status_symbol_raises():
    raw = K_PATH.read_bytes()
    lines = raw.split(b"\r\n")

    # 実在する異常行(K1)を見つけて未知の記号に書き換える
    target = next(i for i, l in enumerate(lines) if l[2:4] == b"K1")
    b = bytearray(lines[target])
    b[2:4] = b"Z9"
    lines[target] = bytes(b)

    with pytest.raises(ResultParseError):
        parse_result_bytes(b"\r\n".join(lines))


def test_truncated_result_line_raises():
    raw = K_PATH.read_bytes()
    lines = raw.split(b"\r\n")
    lines[31] = lines[31][:-1]

    with pytest.raises(ResultParseError):
        parse_result_bytes(b"\r\n".join(lines))


def test_duplicate_lane_raises():
    raw = K_PATH.read_bytes()
    lines = raw.split(b"\r\n")
    # レース1の艇番3の行を艇番1の行で上書きし、艇番集合を壊す
    lines[36] = lines[31]

    with pytest.raises(ResultParseError):
        parse_result_bytes(b"\r\n".join(lines))


def test_missing_startk_marker_raises():
    with pytest.raises(ResultParseError):
        parse_result_bytes(b"NOTSTARTK\r\nFINALK")


# --- B(番組表) / K(競走成績) 突合テスト -------------------------------------

pytestmark_b = pytest.mark.skipif(
    not B_PATH.exists(),
    reason=f"real fixture not found: {B_PATH} (see ml/data/raw/ download instructions)",
)


@pytestmark_b
def test_program_and_result_entries_match_on_stadium_race_lane():
    program = parse_program_path(B_PATH)
    result = parse_result_path(K_PATH)

    b_keys = {(e.stadium_code, e.race_no, e.lane) for e in program.entries}
    k_keys = {(r.stadium_code, r.race_no, r.lane) for r in result.results}

    assert b_keys == k_keys
    assert len(b_keys) == 936


@pytestmark_b
def test_program_and_result_agree_on_registration_number_per_lane():
    program = parse_program_path(B_PATH)
    result = parse_result_path(K_PATH)

    b_reg = {
        (e.stadium_code, e.race_no, e.lane): e.racer.registration_number
        for e in program.entries
    }
    k_reg = {
        (r.stadium_code, r.race_no, r.lane): r.registration_number
        for r in result.results
    }

    assert b_reg == k_reg
