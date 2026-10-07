"""公式サイト(boatrace.jp)の直前情報ページ(beforeinfo)を取得・パースする。

URL: https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={race_no}&jcd={stadium_code:02d}&hd={yyyymmdd}

app/Services/Boatrace/BeforeInfoScraper.php (PHP側の既存実装) と同じレイアウト
前提・同じ抽出ロジックをPythonに移植したもの。実測(2026-09-21)で検証済みの
固定レイアウトに依存しているため、パース対象のHTML構造はPHP側のコメントを
参照。

HTML取得(fetch_before_info_html/fetch_before_info)とパース
(parse_before_info_html)を分離してある。パースはネットワークアクセスなしで
HTML文字列だけからテスト可能。

注意: 「展示不出走」等でセルの値がそもそも欠けているケースの実際の表記
(空文字なのか、専用のマーカー文字列があるのか)は実データで未検証。
_numeric() は空文字・空白のみのセルを None として扱うため、少なくとも
「セルが空」というケースは安全に処理できる。実データでの検証は
CaptureBeforeInfoJob 再開後に行うこと。
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

_BASE_URL = "https://www.boatrace.jp/owpc/pc/race/beforeinfo"
_USER_AGENT = "Mozilla/5.0 (compatible; BoatRacingWeb beforeinfo collector)"
_JST = ZoneInfo("Asia/Tokyo")

_NUMERIC_RE = re.compile(r"-?[\d.]+")
_MEASURED_AT_RE = re.compile(r"(\d{1,2}):(\d{2})現在")
_WIND_DIRECTION_RE = re.compile(r"is-wind(\d+)")


class BeforeInfoFetchError(RuntimeError):
    """取得またはパースに失敗した場合に送出する。"""


class BeforeInfoNotAvailable(BeforeInfoFetchError):
    """「データがありません」等、対象レースのデータがそもそも存在しない場合。"""


@dataclass
class BoatBeforeInfo:
    lane: int
    weight: float | None
    adjusted_weight: float | None
    exhibit_time: float | None
    tilt: float | None
    propeller_changed: bool
    parts_exchanged: str | None
    course_predicted: int | None = None
    st_exhibit: float | None = None


@dataclass(frozen=True)
class WeatherInfo:
    temperature: float | None
    weather_condition: str | None
    wind_speed: float | None
    wind_direction_code: int | None
    water_temperature: float | None
    wave_height: float | None
    measured_at: datetime | None


@dataclass(frozen=True)
class BeforeInfoPage:
    captured_at: datetime
    boats: dict[int, BoatBeforeInfo]
    weather: WeatherInfo


def fetch_before_info_html(
    stadium_code: int, race_no: int, race_date: date, *, timeout: float = 15.0
) -> str:
    """beforeinfoページの生HTMLを取得する。「データがありません」ならBeforeInfoNotAvailableを送出。"""
    context = f"jcd={stadium_code}, rno={race_no}, hd={race_date:%Y%m%d}"
    url = f"{_BASE_URL}?rno={race_no}&jcd={stadium_code:02d}&hd={race_date:%Y%m%d}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except urllib.error.URLError as exc:
        raise BeforeInfoFetchError(f"beforeinfo request failed: {exc} ({context})") from exc

    if status != 200:
        raise BeforeInfoFetchError(f"beforeinfo request failed: HTTP {status} ({context})")

    html = body.decode("utf-8", errors="replace")
    if is_no_data_page(html):
        raise BeforeInfoNotAvailable(f"no beforeinfo data ({context}); race may not exist")

    return html


def is_no_data_page(html: str) -> bool:
    """公式サイトの「データがありません」ページかどうかを判定する。"""
    return "データがありません" in html


def fetch_before_info(
    stadium_code: int, race_no: int, race_date: date, *, timeout: float = 15.0
) -> BeforeInfoPage:
    """beforeinfoページを取得しパースまで行う。"""
    html = fetch_before_info_html(stadium_code, race_no, race_date, timeout=timeout)
    captured_at = datetime.now(timezone.utc)
    return parse_before_info_html(html, race_date, captured_at=captured_at)


def parse_before_info_html(html: str, race_date: date, *, captured_at: datetime) -> BeforeInfoPage:
    """HTML文字列からBeforeInfoPageを組み立てる（ネットワークアクセスなし）。"""
    soup = BeautifulSoup(html, "lxml")
    boats = _parse_boats(soup)
    _merge_exhibit_start(soup, boats)
    weather = _parse_weather(soup, race_date)
    return BeforeInfoPage(captured_at=captured_at, boats=boats, weather=weather)


def _parse_boats(soup: BeautifulSoup) -> dict[int, BoatBeforeInfo]:
    tables = soup.select("table.is-w748")
    if not tables:
        raise BeforeInfoFetchError("艇情報テーブル(table.is-w748)が見つかりません")

    # 固定レイアウト(PHP版と同じ): 0=枠 1=写真 2=名前 3=体重 4=展示タイム 5=チルト
    # 6=プロペラ 7=部品交換 8="R" 9=空 10="進入" 11=空
    # 12=調整重量 13="ST" 14=空 15="着順" 16=空
    boats: dict[int, BoatBeforeInfo] = {}
    for tbody in tables[0].find_all("tbody"):
        tds = tbody.find_all("td")
        try:
            lane = int(tds[0].get_text(strip=True))
            boats[lane] = BoatBeforeInfo(
                lane=lane,
                weight=_numeric(tds[3].get_text()),
                adjusted_weight=_numeric(tds[12].get_text()),
                exhibit_time=_numeric(tds[4].get_text()),
                tilt=_numeric(tds[5].get_text()),
                propeller_changed=tds[6].get_text(strip=True) == "新",
                parts_exchanged=_parse_parts(tds[7]),
            )
        except IndexError as exc:
            raise BeforeInfoFetchError(
                f"艇情報テーブルの列数が想定(17列)と異なります: got {len(tds)} tds"
            ) from exc

    return boats


def _parse_parts(cell: Tag) -> str | None:
    labels = []
    for li in cell.find_all("li"):
        text = li.get_text().strip(" \t\n\r\x0b\x0c　")
        if text:
            labels.append(text)
    return ",".join(labels) if labels else None


def _merge_exhibit_start(soup: BeautifulSoup, boats: dict[int, BoatBeforeInfo]) -> None:
    table = soup.select_one("table.is-w238")
    if table is None:
        return

    for index, row in enumerate(table.select("tbody tr")):
        course = index + 1  # 行番号=コース(1-6)
        number_span = row.select_one(".table1_boatImage1Number")
        if number_span is None:
            continue  # 未公開（空行）

        # 展示不参加艇があるとspan自体は残るが中身が"\xa0"(&nbsp;)のみになる
        # ケースがある(実データで確認: jcd=23,rno=4,hd=20260622、枠1が展示欠場し
        # コース6の枠が空のまま残っていた)。_numeric()と同じ要領でnbspを除去し、
        # 空になった行はこのコースに艇が入らなかったものとしてスキップする。
        lane_text = number_span.get_text().replace(" ", "").strip()
        if not lane_text:
            continue
        lane = int(lane_text)
        time_span = row.select_one(".table1_boatImage1Time")
        st = _parse_st_time(time_span.get_text()) if time_span is not None else None

        if lane in boats:
            boats[lane].course_predicted = course
            boats[lane].st_exhibit = st


def _parse_weather(soup: BeautifulSoup, race_date: date) -> WeatherInfo:
    weather_div = soup.select_one("div.weather1")
    if weather_div is None:
        return WeatherInfo(None, None, None, None, None, None, None)

    title_node = weather_div.select_one(".weather1_title")
    title = title_node.get_text(strip=True) if title_node is not None else ""
    measured_at = None
    match = _MEASURED_AT_RE.search(title)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        # Asia/Tokyoで組み立ててからUTCへ変換する。ml側loaderがrace_weather_info.
        # measured_atへnaive UTC値として書き込むため、ここでJST->UTC変換を
        # 済ませておく必要がある(BeforeInfoScraper.phpの同名バグ修正と同じ理由)。
        local = datetime(race_date.year, race_date.month, race_date.day, hour, minute, tzinfo=_JST)
        measured_at = local.astimezone(timezone.utc)

    def label_data(selector: str) -> float | None:
        node = weather_div.select_one(f"{selector} .weather1_bodyUnitLabelData")
        return _numeric(node.get_text()) if node is not None else None

    condition_node = weather_div.select_one(".is-weather .weather1_bodyUnitLabelTitle")
    condition = condition_node.get_text(strip=True) if condition_node is not None else None
    condition = condition or None

    wind_direction_code = None
    wind_img = weather_div.select_one(".is-windDirection .weather1_bodyUnitImage")
    if wind_img is not None:
        for cls in wind_img.get("class") or []:
            wind_match = _WIND_DIRECTION_RE.match(cls)
            if wind_match:
                wind_direction_code = int(wind_match.group(1))
                break

    return WeatherInfo(
        temperature=label_data(".is-direction"),
        weather_condition=condition,
        wind_speed=label_data(".is-wind"),
        wind_direction_code=wind_direction_code,
        water_temperature=label_data(".is-waterTemperature"),
        wave_height=label_data(".is-wave"),
        measured_at=measured_at,
    )


def _numeric(text: str | None) -> float | None:
    if text is None:
        return None
    trimmed = text.replace(" ", "").strip()
    if trimmed in ("", "&nbsp;"):
        return None
    match = _NUMERIC_RE.search(trimmed)
    return float(match.group(0)) if match else None


def _parse_st_time(text: str) -> float | None:
    """".04"のように整数部が省略された表記を0.04として解釈する。"""
    trimmed = text.strip()
    if not trimmed:
        return None
    if trimmed.startswith("-."):
        trimmed = "-0" + trimmed[1:]
    elif trimmed.startswith("."):
        trimmed = "0" + trimmed
    return _numeric(trimmed)
