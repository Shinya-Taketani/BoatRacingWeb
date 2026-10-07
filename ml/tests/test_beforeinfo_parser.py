"""beforeinfoパーサ(ml.fetchers.beforeinfo)のユニットテスト。

ネットワークアクセスは一切行わない。実際にboatrace.jpへ取得しに行った際の
調査報告に含まれていた観測値(展示タイム6.97、体重53.3kg、チルト-0.5)を
lane=1の期待値として使い、_beforeinfo_fixtures.py の固定HTML文字列だけで
検証する（2026-09-21のbeforeinfo一時停止中、財団への確認待ちのため新規
リクエストは送らない方針）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from ml.fetchers.beforeinfo import (
    BeforeInfoFetchError,
    is_no_data_page,
    parse_before_info_html,
)

from _beforeinfo_fixtures import BEFOREINFO_HTML, BOATS_TABLE, NO_DATA_HTML, RACE_DATE, _exhibit_start_row

_CAPTURED_AT = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)


def _parse():
    return parse_before_info_html(BEFOREINFO_HTML, RACE_DATE, captured_at=_CAPTURED_AT)


def test_is_no_data_page_detects_marker():
    assert is_no_data_page(NO_DATA_HTML) is True
    assert is_no_data_page(BEFOREINFO_HTML) is False


def test_all_six_lanes_parsed():
    page = _parse()
    assert set(page.boats) == {1, 2, 3, 4, 5, 6}
    assert page.captured_at == _CAPTURED_AT


def test_lane1_matches_investigation_report_values():
    boat = _parse().boats[1]
    assert boat.weight == 53.3
    assert boat.exhibit_time == 6.97
    assert boat.tilt == -0.5
    assert boat.adjusted_weight == 0.5
    assert boat.propeller_changed is True
    assert boat.parts_exchanged == "キャブ"


def test_multiple_parts_exchanged_are_comma_joined():
    boat = _parse().boats[3]
    assert boat.parts_exchanged == "ピストン,リング"
    assert boat.propeller_changed is False


def test_exhibition_absent_values_become_none():
    boat = _parse().boats[2]
    assert boat.weight is None
    assert boat.exhibit_time is None
    assert boat.tilt is None
    assert boat.adjusted_weight is None
    assert boat.parts_exchanged is None
    assert boat.propeller_changed is False


def test_start_course_can_differ_from_lane():
    # コース1に入るのはlane=3（前付け）。lane(枠)とstart_course(進入コース)を
    # 混同しないというCLAUDE.mdのルールと同じ区別が展示予想でも成立する。
    boats = _parse().boats
    assert boats[3].course_predicted == 1
    assert boats[3].st_exhibit == 0.04
    assert boats[1].course_predicted == 2
    assert boats[1].st_exhibit == 0.10


def test_leading_dot_and_negative_st_are_parsed():
    boats = _parse().boats
    assert boats[2].st_exhibit == -0.01  # フライングスタート相当の負値


def test_unpublished_course_row_leaves_lane_unset():
    # 6行目(コース6)はスタート展示が未公開の行で、対応する艇番spanが無い。
    boat = _parse().boats[6]
    assert boat.course_predicted is None
    assert boat.st_exhibit is None


def test_weather_parsed_with_jst_to_utc_conversion():
    weather = _parse().weather
    assert weather.temperature == 24.0
    assert weather.weather_condition == "晴"
    assert weather.wind_speed == 3.0
    assert weather.wind_direction_code == 3
    assert weather.water_temperature == 22.0
    assert weather.wave_height == 2.0
    # JST 2026-09-19 12:34 -> UTC 2026-09-19 03:34
    assert weather.measured_at == datetime(2026, 9, 19, 3, 34, tzinfo=timezone.utc)


def test_missing_boat_table_raises():
    html = "<html><body><p>no boats here</p></body></html>"
    try:
        parse_before_info_html(html, RACE_DATE, captured_at=_CAPTURED_AT)
        raise AssertionError("expected BeforeInfoFetchError")
    except BeforeInfoFetchError:
        pass


def test_missing_weather_div_returns_all_none():
    html = BEFOREINFO_HTML.replace('<div class="weather1">', '<div class="weather1-removed">')
    page = parse_before_info_html(html, RACE_DATE, captured_at=_CAPTURED_AT)
    weather = page.weather
    assert weather.temperature is None
    assert weather.weather_condition is None
    assert weather.wind_speed is None
    assert weather.wind_direction_code is None
    assert weather.water_temperature is None
    assert weather.wave_height is None
    assert weather.measured_at is None


def test_exhibit_absent_lane_leaves_nbsp_only_span_without_raising():
    # 実データ(jcd=23,rno=4,hd=20260622)で確認したケース: 枠1が展示欠場し、
    # コース6の枠番spanが消えず"&nbsp;"だけの中身で残っていた。
    # int('')でValueErrorを送出せず、そのコースは艇なしとしてスキップすること。
    exhibit_table_with_nbsp_slot = f"""
    <table class="is-w238">
      <tbody>
        {_exhibit_start_row("2", ".10")}
        {_exhibit_start_row("3", ".12")}
        {_exhibit_start_row("4", ".14")}
        {_exhibit_start_row("5", ".16")}
        {_exhibit_start_row("6", ".18")}
        <tr><td><span class="table1_boatImage1Number">&#160;</span></td></tr>
      </tbody>
    </table>
    """
    html = f"<html><body>{BOATS_TABLE}{exhibit_table_with_nbsp_slot}</body></html>"

    page = parse_before_info_html(html, RACE_DATE, captured_at=_CAPTURED_AT)

    assert page.boats[1].course_predicted is None
    assert page.boats[1].st_exhibit is None
    assert page.boats[2].course_predicted == 1
    assert page.boats[6].course_predicted == 5
