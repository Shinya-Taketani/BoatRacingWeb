"""番組表(B)のパース結果を racer_daily_snapshots へ upsert するローダー。

racer_daily_snapshots は追記専用（PK: racer_id, observed_date）で、
同じキーへの再取込みは ON CONFLICT DO UPDATE で冪等に上書きする。
racer_periods への反映は行わない（loaders.racer_periods の役割）。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import psycopg

from ml.loaders.racers import get_or_create_racer
from ml.parsers.program import RaceEntryRecord, RacerSnapshot

logger = logging.getLogger(__name__)


def period_key_for(d: date) -> str:
    """公式レーサーランク改定期（前期1-6月/後期7-12月）のキーを返す。

    当初は前期5-10月/後期11-4月と仮定していたが、racer_daily_snapshots の
    実データで racer_class の変化日を集計したところ1月・7月に9割以上が
    集中しており（5月・11月付近はほぼ0件）、この仮定が誤りだったと判明した
    ため1月/7月境界に修正した（2026-09-18の調査）。

    例: 2026-03-01 -> "2026H1"（2026年前期）
        2026-09-01 -> "2026H2"（2026年後期）
    """
    if 1 <= d.month <= 6:
        return f"{d.year}H1"
    return f"{d.year}H2"


def _value_tuple(racer: RacerSnapshot) -> tuple:
    # racer_daily_snapshots への記録内容の一貫性チェック用（同一ファイル内で
    # 同じ選手・同じ日の値が食い違っていないか）。racer_periods の区切り判定
    # （_PERIOD_TRACKED_COLUMNS、weightを含まない）とは別物なので混同しないこと。
    return (
        racer.racer_class,
        racer.branch,
        racer.weight,
        racer.national_win_rate,
        racer.national_win_rate_2,
        racer.local_win_rate,
        racer.local_win_rate_2,
    )


@dataclass(frozen=True)
class UpsertResult:
    upserted: int
    racer_ids: set[int] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


_UPSERT_SQL = """
    INSERT INTO racer_daily_snapshots (
        racer_id, observed_date, period_key, racer_class, branch, weight,
        national_win_rate, national_win_rate_2, local_win_rate, local_win_rate_2,
        source_file, imported_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
    ON CONFLICT (racer_id, observed_date) DO UPDATE SET
        period_key = EXCLUDED.period_key,
        racer_class = EXCLUDED.racer_class,
        branch = EXCLUDED.branch,
        weight = EXCLUDED.weight,
        national_win_rate = EXCLUDED.national_win_rate,
        national_win_rate_2 = EXCLUDED.national_win_rate_2,
        local_win_rate = EXCLUDED.local_win_rate,
        local_win_rate_2 = EXCLUDED.local_win_rate_2,
        source_file = EXCLUDED.source_file,
        imported_at = EXCLUDED.imported_at
"""


def upsert_daily_snapshots(
    conn: psycopg.Connection,
    entries: list[RaceEntryRecord],
    *,
    source_file: str,
) -> UpsertResult:
    """番組表エントリを racer_daily_snapshots に upsert する。

    同一 (registration_number, race_date) が同一ファイル内で複数回
    （同日複数レース出走）現れた場合、本来は同じ値であるはずだが、
    値が食い違う場合は警告ログを出し、最初に現れた値を採用する。
    """
    groups: dict[tuple[int, date], list[RacerSnapshot]] = defaultdict(list)
    for entry in entries:
        groups[(entry.racer.registration_number, entry.race_date)].append(entry.racer)

    warnings: list[str] = []
    upserted = 0
    racer_ids: set[int] = set()

    with conn.cursor() as cur:
        for (registration_number, observed_date), racers in groups.items():
            distinct_values = {_value_tuple(r) for r in racers}
            chosen = racers[0]
            if len(distinct_values) > 1:
                msg = (
                    f"registration_number={registration_number} observed_date={observed_date}: "
                    f"{len(distinct_values)} distinct snapshot values within {source_file!r}; "
                    "using the first occurrence"
                )
                warnings.append(msg)
                logger.warning(msg)

            racer_id = get_or_create_racer(cur, registration_number, chosen.name)
            racer_ids.add(racer_id)
            period_key = period_key_for(observed_date)

            cur.execute(
                _UPSERT_SQL,
                (
                    racer_id,
                    observed_date,
                    period_key,
                    chosen.racer_class,
                    chosen.branch,
                    chosen.weight,
                    chosen.national_win_rate,
                    chosen.national_win_rate_2,
                    chosen.local_win_rate,
                    chosen.local_win_rate_2,
                    source_file,
                ),
            )
            upserted += 1

    conn.commit()
    return UpsertResult(upserted=upserted, racer_ids=racer_ids, warnings=warnings)
