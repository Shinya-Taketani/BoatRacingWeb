"""beforeinfoパーサ/ローダーのテストで共有するHTML固定文字列(フィクスチャ)。

実際のページをリクエストせずに検証するため、boatrace.jp beforeinfoページの
実測レイアウト(app/Services/Boatrace/BeforeInfoScraper.php のコメント参照:
table.is-w748=艇ごとの17列固定テーブル、table.is-w238=スタート展示、
div.weather1=気象情報)を模して組み立てた固定HTML。

lane=1の数値(weight=53.3, exhibit_time=6.97, tilt=-0.5)は、事前の
boatrace.jp調査報告に含まれていた実際の観測値を期待値として採用した。
lane=2は展示不出走を想定し体重/展示タイム/チルトを意図的に空にしてある。
"""

from __future__ import annotations

from datetime import date

RACE_DATE = date(2026, 9, 19)


def _boat_tbody(
    lane: int,
    *,
    weight: str = "",
    exhibit_time: str = "",
    tilt: str = "",
    propeller_new: bool = False,
    parts: list[str] | None = None,
    adjusted_weight: str = "",
) -> str:
    parts_html = "".join(f"<li>{p}</li>" for p in (parts or []))
    propeller_text = "新" if propeller_new else ""
    return f"""
    <tbody>
      <tr>
        <td>{lane}</td>
        <td><img src="racer{lane}.png"></td>
        <td>選手{lane}号</td>
        <td>{weight}</td>
        <td>{exhibit_time}</td>
        <td>{tilt}</td>
        <td>{propeller_text}</td>
        <td><ul>{parts_html}</ul></td>
        <td>6.55</td>
        <td></td>
        <td></td>
        <td></td>
        <td>{adjusted_weight}</td>
        <td></td>
        <td></td>
        <td></td>
        <td></td>
      </tr>
    </tbody>
    """


def _exhibit_start_row(number: str | None, time_text: str) -> str:
    if number is None:
        return "<tr><td>未公開</td></tr>"
    return f"""
    <tr>
      <td>
        <span class="table1_boatImage1Number">{number}</span>
        <span class="table1_boatImage1Time">{time_text}</span>
      </td>
    </tr>
    """


BOATS_TABLE = f"""
<table class="is-w748">
  {_boat_tbody(1, weight="53.3", exhibit_time="6.97", tilt="-0.5", propeller_new=True, parts=["キャブ"], adjusted_weight="0.5")}
  {_boat_tbody(2)}
  {_boat_tbody(3, weight="52.0", exhibit_time="6.85", tilt="0.0", parts=["ピストン", "リング"])}
  {_boat_tbody(4, weight="54.1", exhibit_time="6.90", tilt="-0.5")}
  {_boat_tbody(5, weight="51.2", exhibit_time="7.05", tilt="0.5")}
  {_boat_tbody(6, weight="55.0", exhibit_time="6.80", tilt="-0.5")}
</table>
"""

# コース1に3号艇が前付けする例（lane != start_courseの実例）、
# コース3は".01"形式のフライングスタート、コース6は未公開（skip確認）。
EXHIBIT_START_TABLE = f"""
<table class="is-w238">
  <tbody>
    {_exhibit_start_row("3", ".04")}
    {_exhibit_start_row("1", ".10")}
    {_exhibit_start_row("2", "-.01")}
    {_exhibit_start_row("4", ".15")}
    {_exhibit_start_row("5", ".18")}
    {_exhibit_start_row(None, "")}
  </tbody>
</table>
"""

WEATHER_DIV = """
<div class="weather1">
  <div class="weather1_title">気象情報  12:34現在</div>
  <div class="weather1_body">
    <div class="weather1_bodyUnit is-direction">
      <span class="weather1_bodyUnitLabelTitle">気温</span>
      <span class="weather1_bodyUnitLabelData">24.0<span class="weather1_bodyUnitLabelUnit">&#8451;</span></span>
    </div>
    <div class="weather1_bodyUnit is-weather">
      <span class="weather1_bodyUnitLabelTitle">晴</span>
    </div>
    <div class="weather1_bodyUnit is-wind">
      <span class="weather1_bodyUnitLabelTitle">風速</span>
      <span class="weather1_bodyUnitLabelData">3<span class="weather1_bodyUnitLabelUnit">m</span></span>
    </div>
    <div class="weather1_bodyUnit is-windDirection">
      <p class="weather1_bodyUnitImage is-wind3"></p>
    </div>
    <div class="weather1_bodyUnit is-waterTemperature">
      <span class="weather1_bodyUnitLabelTitle">水温</span>
      <span class="weather1_bodyUnitLabelData">22.0<span class="weather1_bodyUnitLabelUnit">&#8451;</span></span>
    </div>
    <div class="weather1_bodyUnit is-wave">
      <span class="weather1_bodyUnitLabelTitle">波高</span>
      <span class="weather1_bodyUnitLabelData">2<span class="weather1_bodyUnitLabelUnit">cm</span></span>
    </div>
  </div>
</div>
"""

BEFOREINFO_HTML = f"""
<!DOCTYPE html>
<html>
<head><title>直前情報</title></head>
<body>
{BOATS_TABLE}
{EXHIBIT_START_TABLE}
{WEATHER_DIV}
</body>
</html>
"""

NO_DATA_HTML = """
<!DOCTYPE html>
<html><body><div class="l-title">データがありません</div></body></html>
"""
