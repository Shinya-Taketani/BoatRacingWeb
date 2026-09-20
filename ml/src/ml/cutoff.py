"""特徴量生成時のカットオフ（配信時刻 = 締切10分前）検証ユーティリティ。

CLAUDE.md の絶対ルール1（リーク防止）を機械的にチェックするための雛形。
特徴量生成パイプラインは、この検証を通らないデータで学習・推論用の
特徴量を作ってはならない。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime


class CutoffViolationError(ValueError):
    """cutoff_at より後の情報が特徴量生成に混入した場合に送出する。"""


def assert_no_leak(
    rows: Iterable[Mapping[str, object]],
    *,
    cutoff_at: datetime,
    time_field: str,
    label: str,
) -> None:
    """rows の time_field が全て cutoff_at 以前であることを検証する。

    1件でも time_field > cutoff_at のレコードが混入していれば
    CutoffViolationError を送出する（フィルタして握りつぶすのではなく fail させる）。

    Parameters
    ----------
    rows: 検証対象のレコード列（odds_snapshots や race_results から取得した行を想定）
    cutoff_at: 特徴量生成に用いるカットオフ時刻（tz-aware であること）
    time_field: 各行のうちカットオフと比較する時刻カラム名
        （例: odds_snapshots.captured_at, race_results.created_at）
    label: エラーメッセージ用のデータソース名（例: "odds_snapshots"）
    """
    for row in rows:
        ts = row[time_field]
        if not isinstance(ts, datetime):
            raise TypeError(f"{label}.{time_field} must be datetime, got {type(ts)!r}")
        if ts.tzinfo is None or cutoff_at.tzinfo is None:
            raise ValueError(f"{label}.{time_field} and cutoff_at must both be tz-aware")
        if ts > cutoff_at:
            raise CutoffViolationError(
                f"{label}: {time_field}={ts!r} is after cutoff_at={cutoff_at!r} (row={row!r})"
            )
