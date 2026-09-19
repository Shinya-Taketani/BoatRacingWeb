"""公式サイト(mbrace)から番組表(B)/競走成績(K)ファイルを取得・展開する。

URL規則: https://www1.mbrace.or.jp/od2/{B|K}/{yyyymm}/{b|k}{yy}{mm}{dd}.lzh
LZH展開には 7z (p7zip) を使う。
"""

from __future__ import annotations

import subprocess
import urllib.request
from datetime import date
from pathlib import Path
from typing import Literal

Kind = Literal["B", "K"]

_BASE_URL = "https://www1.mbrace.or.jp/od2"
_USER_AGENT = "Mozilla/5.0"


class FetchError(RuntimeError):
    """ダウンロードまたは展開に失敗した場合に送出する。"""


def _lzh_filename(kind: Kind, d: date) -> str:
    prefix = kind.lower()
    return f"{prefix}{d.strftime('%y%m%d')}.lzh"


def _txt_filename(kind: Kind, d: date) -> str:
    return f"{kind.upper()}{d.strftime('%y%m%d')}.TXT"


def build_url(kind: Kind, d: date) -> str:
    yyyymm = d.strftime("%Y%m")
    return f"{_BASE_URL}/{kind.upper()}/{yyyymm}/{_lzh_filename(kind, d)}"


def fetch_and_extract(kind: Kind, d: date, dest_dir: Path, *, force: bool = False) -> Path:
    """指定日の B または K ファイルを取得・展開し、展開済み .TXT のパスを返す。

    dest_dir に展開済みファイルが既に存在すればダウンロードをスキップする
    （force=True で強制再取得）。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    txt_path = dest_dir / _txt_filename(kind, d)
    if txt_path.exists() and not force:
        return txt_path

    lzh_path = dest_dir / _lzh_filename(kind, d)
    url = build_url(kind, d)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
    except Exception as exc:
        raise FetchError(f"failed to download {url}: {exc}") from exc

    lzh_path.write_bytes(data)

    proc = subprocess.run(
        ["7z", "x", "-y", f"-o{dest_dir}", str(lzh_path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise FetchError(
            f"failed to extract {lzh_path}: {proc.stdout}\n{proc.stderr}"
        )

    if not txt_path.exists():
        raise FetchError(f"expected {txt_path} after extracting {lzh_path}, but it is missing")

    return txt_path
