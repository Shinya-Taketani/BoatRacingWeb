"""predictions のうち race_results が確定済みでまだ判定していないものを対象に、
prediction_entries の最大 p_first の lane が実際の1着と一致したかを判定し、
prediction_judgments に記録する。

prediction_results（ticket_id が NOT NULL・主キーで prediction_tickets への
必須FK）は「舟券(買い目)ごとの的中判定」専用のテーブルであり、買い目が
未実装の現時点では使えない（対応する ticket_id が存在しない）。そのため
予測単位の的中は新設した prediction_judgments に記録する。
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from ml.loaders.db import get_connection

# 「結果が確定している」= そのレースの全race_entriesにrace_resultsが
# 紐づいている状態。件数比較で判定する（開催なし等で欠けているレースは対象外）。
_JUDGE_INSERT_SQL = """
    INSERT INTO prediction_judgments (prediction_id, predicted_lane, actual_lane, hit, judged_at)
    SELECT
        p.id,
        top1.lane,
        winner.lane,
        COALESCE(top1.lane = winner.lane, false),
        now()
    FROM predictions p
    JOIN LATERAL (
        SELECT lane FROM prediction_entries pe
        WHERE pe.prediction_id = p.id
        ORDER BY pe.p_first DESC
        LIMIT 1
    ) top1 ON true
    LEFT JOIN LATERAL (
        SELECT re.lane
        FROM race_entries re
        JOIN race_results rr ON rr.race_entry_id = re.id
        WHERE re.race_id = p.race_id AND rr.finish_pos = 1
        LIMIT 1
    ) winner ON true
    WHERE NOT EXISTS (
        SELECT 1 FROM prediction_judgments pj WHERE pj.prediction_id = p.id
    )
    AND (
        SELECT count(*) FROM race_entries re2
        JOIN race_results rr2 ON rr2.race_entry_id = re2.id
        WHERE re2.race_id = p.race_id
    ) = (
        SELECT count(*) FROM race_entries re3 WHERE re3.race_id = p.race_id
    )
    RETURNING prediction_id, hit
"""


@dataclass(frozen=True)
class JudgeResult:
    judged_count: int
    hit_count: int

    @property
    def hit_rate(self) -> float:
        return self.hit_count / self.judged_count if self.judged_count else float("nan")


def judge_pending_predictions(conn: psycopg.Connection) -> JudgeResult:
    with conn.cursor() as cur:
        cur.execute(_JUDGE_INSERT_SQL)
        rows = cur.fetchall()
    conn.commit()

    judged_count = len(rows)
    hit_count = sum(1 for _, hit in rows if hit)
    return JudgeResult(judged_count=judged_count, hit_count=hit_count)


def main() -> int:
    conn = get_connection()
    try:
        result = judge_pending_predictions(conn)
    finally:
        conn.close()

    print(
        f"judged={result.judged_count} hit={result.hit_count} "
        f"hit_rate={result.hit_rate:.4f} ({result.hit_rate * 100:.2f}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
