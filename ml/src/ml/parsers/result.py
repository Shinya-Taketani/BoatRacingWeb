"""公式配布 競走成績(K)ファイルのパーサー。

入力: 競走成績(K)ファイルの生バイト列（cp932、LZH展開後の固定長テキスト、
CRLF区切り）。program.py（番組表(B)）と対になるファイルで、同日・同場・
同レースの艇番の集合は完全に一致するはずである。

ファイル全体は次のブロック構造を持ち、状態機械でパースする。

    STARTK
      {場コード}KBGN            場ヘッダ開始
        ...（場名・開催日・払戻金一覧などの自由形式ヘッダ）...
        NR       レース名           H1800m  天候情報          レース見出し行
        (列見出し1行、末尾は決まり手でレースごとに変化するためプレフィックスのみ検証)
        ------------------------------------- 罫線(79桁)
        (着順6行、各66バイト固定長)
        ...（払戻金の自由形式明細。次のレース見出しか場ヘッダ終了まで読み飛ばす）...
        NR       ...                                          次のレースへ繰り返し
      {場コード}KEND            場ヘッダ終了
      ...                                       場ブロックの繰り返し
    FINALK

着順6行は全角(2バイト)と半角(1バイト)が混在する固定長レコードのため、
デコード後の文字列ではなく生バイト列に対してバイト単位でスライスする。

着順が数値でない行（フライング・出遅れ・欠場・失格等）は finish_pos=None
とし、記号は status に格納する。想定していない記号は ResultParseError を
送出する（黙ってスキップしない）。ST(スタートタイミング)は F 表記
（フライング、例: F0.01）を負値に変換する。L 表記（出遅れ）はフライングと
符号の意味が逆（信号後の遅れ）なので負値化せず、そのままの値として扱う
（別扱い）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

_VALID_STATUSES = {
    "F",   # フライング
    "L0",  # 出遅（救済なし）
    "L1",  # 出遅（救済あり）
    "K0",  # 欠場（前日）
    "K1",  # 欠場（当日）
    "S0",  # 失格（一般）
    "S1",  # 失格（進入後）
    "S2",  # 失格（妨害）
    "S3",  # 失格（その他）
}

_SEPARATOR = "-" * 79
_HEADER_PREFIX = '  着 艇 登番 \u3000選\u3000手\u3000名\u3000\u3000ﾓｰﾀｰ ﾎﾞｰﾄ 展示 進入 ｽﾀｰﾄﾀｲﾐﾝｸ ﾚｰｽﾀｲﾑ '

_RESULT_LINE_LENGTH = 66
# 着順行のバイトオフセット（実ファイルから実測して確定したもの）
_OFF_FINISH = slice(2, 6)
_OFF_LANE = slice(6, 7)
_OFF_REG_NO = slice(8, 12)
_OFF_NAME = slice(13, 29)
_OFF_MOTOR_NO = slice(30, 32)
_OFF_BOAT_NO = slice(35, 37)
_OFF_EXHIBITION_TIME = slice(38, 43)
_OFF_START_COURSE = slice(46, 47)
_OFF_ST = slice(50, 55)
_OFF_RACE_TIME = slice(60, 66)

_STADIUM_MARK_RE = re.compile(r"^(\d{2})K(BGN|END)$")
_DATE_RE = re.compile(r"第\s*\d+日\s+(\d{4})/\s*(\d{1,2})/\s*(\d{1,2})")
_RACE_HEADER_RE = re.compile(r"^\s*(\d{1,2})R\s+.*?H\d+m")
_RACE_TIME_RE = re.compile(r"^(\d+)\.(\d{2})\.(\d)$")


class ResultParseError(ValueError):
    """競走成績(K)ファイルのパースに失敗した場合に送出する。"""


@dataclass(frozen=True)
class RaceResultRecord:
    """race_results 1行に対応するレコード（race_entries との突合キー付き）。"""

    stadium_code: int
    race_date: date
    race_no: int
    lane: int  # 枠番。start_course（進入コース）とは別物
    registration_number: int
    name: str
    start_course: int | None
    st: Decimal | None
    finish_pos: int | None
    status: str | None  # abnormal_code 相当。フライング/出遅/欠場/失格等の記号
    race_time: Decimal | None  # 秒に換算した走破タイム


@dataclass(frozen=True)
class ParsedResult:
    results: list[RaceResultRecord]


def parse_result_path(path) -> ParsedResult:
    from pathlib import Path

    return parse_result_bytes(Path(path).read_bytes())


def parse_result_bytes(raw: bytes) -> ParsedResult:
    lines = raw.split(b"\r\n")
    results: list[RaceResultRecord] = []
    n = len(lines)

    def text_at(idx: int) -> str:
        if idx >= n:
            raise ResultParseError(f"unexpected end of file at line {idx}")
        try:
            return lines[idx].decode("cp932")
        except UnicodeDecodeError as exc:
            raise ResultParseError(f"line {idx}: cp932 decode failed: {exc}") from exc

    def normalized_at(idx: int) -> str:
        return unicodedata.normalize("NFKC", text_at(idx))

    def expect_literal(idx: int, expected: str) -> None:
        actual = text_at(idx)
        if actual != expected:
            raise ResultParseError(f"line {idx}: expected {expected!r}, got {actual!r}")

    if text_at(0) != "STARTK":
        raise ResultParseError("file does not start with STARTK")

    i = 1
    while True:
        text = text_at(i)
        if text == "FINALK":
            i += 1
            break

        m = _STADIUM_MARK_RE.match(text)
        if not m or m.group(2) != "BGN":
            raise ResultParseError(f"line {i}: expected '{{code}}KBGN', got {text!r}")
        stadium_code = int(m.group(1))
        i += 1

        race_date: date | None = None
        while race_date is None:
            dm = _DATE_RE.search(text_at(i))
            if dm:
                race_date = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            i += 1
            if i >= n:
                raise ResultParseError(
                    f"race date not found in stadium header (code={stadium_code:02d})"
                )

        while not _RACE_HEADER_RE.match(normalized_at(i)):
            if _STADIUM_MARK_RE.match(text_at(i)):
                raise ResultParseError(
                    f"stadium {stadium_code:02d} block has no races (line {i})"
                )
            i += 1
            if i >= n:
                raise ResultParseError("race header not found before end of file")

        while True:
            rm = _RACE_HEADER_RE.match(normalized_at(i))
            if not rm:
                raise ResultParseError(f"line {i}: expected race header, got {text_at(i)!r}")
            race_no = int(rm.group(1))
            i += 1

            header_text = text_at(i)
            if not header_text.startswith(_HEADER_PREFIX):
                raise ResultParseError(
                    f"line {i}: expected header line starting with {_HEADER_PREFIX!r}, "
                    f"got {header_text!r}"
                )
            i += 1
            expect_literal(i, _SEPARATOR)
            i += 1

            # 着順6行は着順(1着〜6着)順に並んでおり、B(番組表)の艇番順とは異なる。
            # 行順で艇番を決め打ちできないため、6行読み終えた時点で艇番の集合が
            # {1,2,3,4,5,6} と過不足なく一致することを検証する。
            race_results = [
                _parse_result_line(
                    lines[i + offset],
                    line_no=i + offset,
                    stadium_code=stadium_code,
                    race_date=race_date,
                    race_no=race_no,
                )
                for offset in range(6)
            ]
            i += 6

            lanes = [r.lane for r in race_results]
            if sorted(lanes) != [1, 2, 3, 4, 5, 6]:
                raise ResultParseError(
                    f"line {i - 6}..{i - 1}: expected lanes {{1..6}} exactly once each, "
                    f"got {sorted(lanes)}"
                )
            results.extend(race_results)

            # 払戻金明細は自由形式のため、次のレース見出しか場終了マーカーまで読み飛ばす
            stadium_done = False
            while True:
                text = text_at(i)
                end_m = _STADIUM_MARK_RE.match(text)
                if end_m:
                    if end_m.group(1) != f"{stadium_code:02d}" or end_m.group(2) != "END":
                        raise ResultParseError(
                            f"line {i}: expected '{stadium_code:02d}KEND', got {text!r}"
                        )
                    i += 1
                    stadium_done = True
                    break
                if _RACE_HEADER_RE.match(normalized_at(i)):
                    break
                i += 1
                if i >= n:
                    raise ResultParseError(
                        "unexpected end of file while skipping payout section"
                    )

            if stadium_done:
                break

    return ParsedResult(results=results)


def _parse_result_line(
    raw_line: bytes,
    *,
    line_no: int,
    stadium_code: int,
    race_date: date,
    race_no: int,
) -> RaceResultRecord:
    if len(raw_line) != _RESULT_LINE_LENGTH:
        raise ResultParseError(
            f"line {line_no}: result line must be {_RESULT_LINE_LENGTH} bytes, "
            f"got {len(raw_line)} bytes: {raw_line!r}"
        )

    def field(off: slice) -> str:
        try:
            return raw_line[off].decode("cp932")
        except UnicodeDecodeError as exc:
            raise ResultParseError(f"line {line_no}: cp932 decode failed: {exc}") from exc

    def as_int(off: slice, name: str) -> int:
        s = field(off).strip()
        try:
            return int(s)
        except ValueError as exc:
            raise ResultParseError(f"line {line_no}: invalid int for {name}: {s!r}") from exc

    lane = as_int(_OFF_LANE, "lane")
    if not 1 <= lane <= 6:
        raise ResultParseError(f"line {line_no}: lane out of range: {lane}")

    finish_pos, status = _parse_finish(field(_OFF_FINISH), line_no)
    start_course = _parse_start_course(field(_OFF_START_COURSE), line_no)
    st = _parse_st(field(_OFF_ST), line_no)
    race_time = _parse_race_time(field(_OFF_RACE_TIME), line_no)

    name = field(_OFF_NAME).replace("　", "").strip()

    return RaceResultRecord(
        stadium_code=stadium_code,
        race_date=race_date,
        race_no=race_no,
        lane=lane,
        registration_number=as_int(_OFF_REG_NO, "registration_number"),
        name=name,
        start_course=start_course,
        st=st,
        finish_pos=finish_pos,
        status=status,
        race_time=race_time,
    )


def _parse_finish(raw: str, line_no: int) -> tuple[int | None, str | None]:
    s = raw.strip()
    if s.isdigit():
        return int(s), None
    if s in _VALID_STATUSES:
        return None, s
    raise ResultParseError(
        f"line {line_no}: unknown finish_pos/status symbol {s!r} "
        f"(expected a digit or one of {sorted(_VALID_STATUSES)})"
    )


def _parse_start_course(raw: str, line_no: int) -> int | None:
    s = raw.strip()
    if not s:
        return None
    try:
        course = int(s)
    except ValueError as exc:
        raise ResultParseError(f"line {line_no}: invalid start_course: {s!r}") from exc
    if not 1 <= course <= 6:
        raise ResultParseError(f"line {line_no}: start_course out of range: {course}")
    return course


def _parse_st(raw: str, line_no: int) -> Decimal | None:
    s = raw.strip()
    if not s or s.startswith("K"):
        return None
    if s.startswith("F"):
        digits = s[1:].strip()
        try:
            return -Decimal(digits)
        except InvalidOperation as exc:
            raise ResultParseError(f"line {line_no}: invalid F-prefixed ST: {s!r}") from exc
    if s.startswith("L"):
        # 出遅れ: フライングとは符号の意味が逆（信号後の遅れ）のため負値化しない
        digits = s[1:].strip()
        try:
            return Decimal(digits)
        except InvalidOperation as exc:
            raise ResultParseError(f"line {line_no}: invalid L-prefixed ST: {s!r}") from exc
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ResultParseError(f"line {line_no}: invalid ST value: {s!r}") from exc


def _parse_race_time(raw: str, line_no: int) -> Decimal | None:
    s = raw.strip()
    if not any(ch.isdigit() for ch in s):
        return None
    m = _RACE_TIME_RE.match(s)
    if not m:
        raise ResultParseError(f"line {line_no}: invalid race_time: {s!r}")
    minutes, seconds, tenths = m.groups()
    return Decimal(minutes) * 60 + Decimal(seconds) + Decimal(tenths) / Decimal(10)
