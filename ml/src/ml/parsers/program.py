"""公式配布 番組表(B)ファイルのパーサー。

入力: 番組表(B)ファイルの生バイト列（cp932、LZH展開後の固定長テキスト、
CRLF区切り）。
出力: races / race_entries に対応する dataclass のリスト（ParsedProgram）。

ファイル全体は次のブロック構造を持ち、状態機械でパースする。

    STARTB
      {場コード}BBGN            場ヘッダ開始
        ...（場名・開催日を含む自由形式のヘッダ行）...
        　１Ｒ  ...  電話投票締切予定HH:MM   レースヘッダ
        ------------------------------------- 罫線(79桁)
        (列見出し2行、固定文言)
        ------------------------------------- 罫線(79桁)
        (選手6行、各79バイト固定長)
        ...                                    レースヘッダ〜選手6行の繰り返し
      {場コード}BEND            場ヘッダ終了
      ...                                       場ブロックの繰り返し
    FINALB

締切時刻はレースヘッダの時刻文字列と、場ヘッダで確定した開催日を組み合わせて
JST の aware datetime にする。想定外の行・桁位置の不整合・値域外の値は
すべて ProgramParseError を送出する（黙ってスキップしない）。

選手6行は全角(2バイト)と半角(1バイト)が混在する固定長レコードのため、
デコード後の文字列ではなく生バイト列に対してバイト単位でスライスする。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

_VALID_CLASSES = {"A1", "A2", "B1", "B2"}

_SEPARATOR = "-" * 79
_HEADER_LINE_1 = "艇 選手 選手  年 支 体級    全国      当地     モーター   ボート   今節成績  早"
_HEADER_LINE_2 = "番 登番  名   齢 部 重別 勝率  2率  勝率  2率  NO  2率  NO  2率  １２３４５６見"

_RACER_LINE_LENGTH = 79
# 選手行のバイトオフセット（実ファイルから実測して確定したもの）
_OFF_LANE = slice(0, 1)
_OFF_REG_NO = slice(2, 6)
_OFF_NAME = slice(6, 14)
_OFF_AGE = slice(14, 16)
_OFF_BRANCH = slice(16, 20)
_OFF_WEIGHT = slice(20, 22)
_OFF_CLASS = slice(22, 24)
_OFF_NATIONAL_WIN_RATE = slice(25, 29)
_OFF_NATIONAL_WIN_RATE_2 = slice(30, 35)
_OFF_LOCAL_WIN_RATE = slice(36, 40)
_OFF_LOCAL_WIN_RATE_2 = slice(41, 46)
_OFF_MOTOR_NO = slice(47, 49)
_OFF_MOTOR_WIN_RATE_2 = slice(51, 55)
_OFF_BOAT_NO = slice(56, 58)
_OFF_BOAT_WIN_RATE_2 = slice(59, 64)

_STADIUM_MARK_RE = re.compile(r"^(\d{2})B(BGN|END)$")
_DATE_RE = re.compile(r"第\s*\d+日\s+(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")
_RACE_HEADER_RE = re.compile(
    r"^\s*(\d{1,2})R\s+(.*?)\s*H(\d+)m.*?締切予定(\d{1,2}):(\d{2})\s*$"
)


class ProgramParseError(ValueError):
    """番組表(B)ファイルのパースに失敗した場合に送出する。"""


@dataclass(frozen=True)
class RacerSnapshot:
    """racer_periods に反映するための、番組表発表時点の選手プロフィール。

    級別・勝率はこの時点のスナップショットであり、race_entries には
    直接保存しない（CLAUDE.md: 選手の級別・勝率は racer_periods の
    期間スナップショットから引く）。
    """

    registration_number: int
    name: str
    age: int
    branch: str
    racer_class: str
    weight: Decimal
    national_win_rate: Decimal
    national_win_rate_2: Decimal
    local_win_rate: Decimal
    local_win_rate_2: Decimal


@dataclass(frozen=True)
class RaceEntryRecord:
    """race_entries 1行に対応するレコード。"""

    stadium_code: int
    race_date: date
    race_no: int
    lane: int
    racer: RacerSnapshot
    motor_no: int
    motor_win_rate_2: Decimal
    boat_no: int
    boat_win_rate_2: Decimal


@dataclass(frozen=True)
class RaceRecord:
    """races 1行に対応するレコード。"""

    stadium_code: int
    race_date: date
    race_no: int
    title: str
    distance_m: int
    deadline_at: datetime  # JST aware


@dataclass(frozen=True)
class ParsedProgram:
    races: list[RaceRecord]
    entries: list[RaceEntryRecord]


def parse_program_path(path: Path) -> ParsedProgram:
    return parse_program_bytes(Path(path).read_bytes())


def parse_program_bytes(raw: bytes) -> ParsedProgram:
    lines = raw.split(b"\r\n")
    races: list[RaceRecord] = []
    entries: list[RaceEntryRecord] = []
    n = len(lines)

    def text_at(idx: int) -> str:
        if idx >= n:
            raise ProgramParseError(f"unexpected end of file at line {idx}")
        try:
            return lines[idx].decode("cp932")
        except UnicodeDecodeError as exc:
            raise ProgramParseError(f"line {idx}: cp932 decode failed: {exc}") from exc

    def normalized_at(idx: int) -> str:
        return unicodedata.normalize("NFKC", text_at(idx))

    def expect_literal(idx: int, expected: str) -> None:
        actual = text_at(idx)
        if actual != expected:
            raise ProgramParseError(f"line {idx}: expected {expected!r}, got {actual!r}")

    if text_at(0) != "STARTB":
        raise ProgramParseError("file does not start with STARTB")

    i = 1
    while True:
        text = text_at(i)
        if text == "FINALB":
            i += 1
            break

        m = _STADIUM_MARK_RE.match(text)
        if not m or m.group(2) != "BGN":
            raise ProgramParseError(f"line {i}: expected '{{code}}BBGN', got {text!r}")
        stadium_code = int(m.group(1))
        i += 1

        race_date: date | None = None
        placeholder_no_data = False
        while race_date is None:
            # 開催日が一度も見つからないまま自場の{code}BENDに到達した場合、
            # その場のデータがファイル生成時点でまだ確定していなかった
            # プレースホルダーブロックとみなす。判定はBEND到達のみで行い、
            # 中の文言（"この場のデータ更新は..."等、将来変わりうる）には
            # 依存しない。これを見逃すと、日付探索が場の境界を越えて次の
            # 場の見出し行から誤った日付を拾い、以降のレースを全て隣の場の
            # ものとして誤登録してしまう。
            end_m = _STADIUM_MARK_RE.match(text_at(i))
            if end_m and end_m.group(1) == f"{stadium_code:02d}" and end_m.group(2) == "END":
                i += 1
                placeholder_no_data = True
                break

            dm = _DATE_RE.search(normalized_at(i))
            if dm:
                race_date = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            i += 1
            if i >= n:
                raise ProgramParseError(
                    f"race date not found in stadium header (code={stadium_code:02d})"
                )

        if placeholder_no_data:
            logger.warning(
                "stadium_code=%02d: reached %02dBEND before any race date was found; "
                "treating as an unconfirmed/placeholder block with 0 races",
                stadium_code, stadium_code,
            )
            continue

        # 全レース中止（台風等）の場合、場ブロックに開催日はあってもレースが
        # 1つも無く、次の {code}BEND に直接到達することがある。この場合は
        # この場を0件として次の場ブロックへ進む（対応する終了マーカーで
        # あることを確認した上で許容する。黙ってスキップするのではない）。
        no_races_this_stadium = False
        while not _RACE_HEADER_RE.match(normalized_at(i)):
            end_m = _STADIUM_MARK_RE.match(text_at(i))
            if end_m:
                if end_m.group(1) != f"{stadium_code:02d}" or end_m.group(2) != "END":
                    raise ProgramParseError(
                        f"line {i}: expected '{stadium_code:02d}BEND' or a race header, "
                        f"got {text_at(i)!r}"
                    )
                i += 1
                no_races_this_stadium = True
                break
            i += 1
            if i >= n:
                raise ProgramParseError("race header not found before end of file")

        if no_races_this_stadium:
            continue

        while True:
            rm = _RACE_HEADER_RE.match(normalized_at(i))
            if not rm:
                raise ProgramParseError(f"line {i}: expected race header, got {text_at(i)!r}")
            race_no = int(rm.group(1))
            title = rm.group(2).strip()
            distance_m = int(rm.group(3))
            hour, minute = int(rm.group(4)), int(rm.group(5))
            deadline_at = datetime(
                race_date.year, race_date.month, race_date.day, hour, minute, tzinfo=JST
            )
            races.append(
                RaceRecord(
                    stadium_code=stadium_code,
                    race_date=race_date,
                    race_no=race_no,
                    title=title,
                    distance_m=distance_m,
                    deadline_at=deadline_at,
                )
            )
            i += 1

            expect_literal(i, _SEPARATOR)
            i += 1
            expect_literal(i, _HEADER_LINE_1)
            i += 1
            expect_literal(i, _HEADER_LINE_2)
            i += 1
            expect_literal(i, _SEPARATOR)
            i += 1

            for lane in range(1, 7):
                entries.append(
                    _parse_racer_line(
                        lines[i],
                        line_no=i,
                        stadium_code=stadium_code,
                        race_date=race_date,
                        race_no=race_no,
                        expected_lane=lane,
                    )
                )
                i += 1

            while text_at(i) == "":
                i += 1

            text = text_at(i)
            end_m = _STADIUM_MARK_RE.match(text)
            if end_m:
                if end_m.group(1) != f"{stadium_code:02d}" or end_m.group(2) != "END":
                    raise ProgramParseError(
                        f"line {i}: expected '{stadium_code:02d}BEND', got {text!r}"
                    )
                i += 1
                break

            if not _RACE_HEADER_RE.match(normalized_at(i)):
                raise ProgramParseError(
                    f"line {i}: expected race header or "
                    f"'{stadium_code:02d}BEND', got {text!r}"
                )

    return ParsedProgram(races=races, entries=entries)


def _parse_racer_line(
    raw_line: bytes,
    *,
    line_no: int,
    stadium_code: int,
    race_date: date,
    race_no: int,
    expected_lane: int,
) -> RaceEntryRecord:
    if len(raw_line) != _RACER_LINE_LENGTH:
        raise ProgramParseError(
            f"line {line_no}: racer line must be {_RACER_LINE_LENGTH} bytes, "
            f"got {len(raw_line)} bytes: {raw_line!r}"
        )

    def field(off: slice) -> str:
        try:
            return raw_line[off].decode("cp932").strip()
        except UnicodeDecodeError as exc:
            raise ProgramParseError(f"line {line_no}: cp932 decode failed: {exc}") from exc

    def as_int(off: slice, name: str) -> int:
        s = field(off)
        try:
            return int(s)
        except ValueError as exc:
            raise ProgramParseError(f"line {line_no}: invalid int for {name}: {s!r}") from exc

    def as_decimal(off: slice, name: str) -> Decimal:
        s = field(off)
        try:
            return Decimal(s)
        except InvalidOperation as exc:
            raise ProgramParseError(f"line {line_no}: invalid decimal for {name}: {s!r}") from exc

    lane = as_int(_OFF_LANE, "lane")
    if lane != expected_lane:
        raise ProgramParseError(
            f"line {line_no}: expected lane {expected_lane}, got {lane} "
            f"(row misaligned or missing)"
        )

    racer_class = field(_OFF_CLASS)
    if racer_class not in _VALID_CLASSES:
        raise ProgramParseError(
            f"line {line_no}: invalid racer_class {racer_class!r} "
            f"(expected one of {sorted(_VALID_CLASSES)})"
        )

    racer = RacerSnapshot(
        registration_number=as_int(_OFF_REG_NO, "registration_number"),
        name=field(_OFF_NAME),
        age=as_int(_OFF_AGE, "age"),
        branch=field(_OFF_BRANCH),
        racer_class=racer_class,
        weight=as_decimal(_OFF_WEIGHT, "weight"),
        national_win_rate=as_decimal(_OFF_NATIONAL_WIN_RATE, "national_win_rate"),
        national_win_rate_2=as_decimal(_OFF_NATIONAL_WIN_RATE_2, "national_win_rate_2"),
        local_win_rate=as_decimal(_OFF_LOCAL_WIN_RATE, "local_win_rate"),
        local_win_rate_2=as_decimal(_OFF_LOCAL_WIN_RATE_2, "local_win_rate_2"),
    )

    return RaceEntryRecord(
        stadium_code=stadium_code,
        race_date=race_date,
        race_no=race_no,
        lane=lane,
        racer=racer,
        motor_no=as_int(_OFF_MOTOR_NO, "motor_no"),
        motor_win_rate_2=as_decimal(_OFF_MOTOR_WIN_RATE_2, "motor_win_rate_2"),
        boat_no=as_int(_OFF_BOAT_NO, "boat_no"),
        boat_win_rate_2=as_decimal(_OFF_BOAT_WIN_RATE_2, "boat_win_rate_2"),
    )
