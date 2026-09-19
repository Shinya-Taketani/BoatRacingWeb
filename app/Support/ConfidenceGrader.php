<?php

namespace App\Support;

/**
 * p_first(6艇, 合計1)のエントロピーから confidence_grade を算出する。
 * 閾値は config('ml.confidence_thresholds')（検証期間の正規化エントロピー
 * 分布の分位から決めた初期案。CLAUDE.md「回収率の検証結果」参照）。
 */
class ConfidenceGrader
{
    /**
     * @param  array<int, float|null>  $pFirstByLane
     */
    public static function normalizedEntropy(array $pFirstByLane): ?float
    {
        $probs = array_filter(
            $pFirstByLane,
            fn (?float $p): bool => $p !== null && $p > 0
        );

        if ($probs === []) {
            return null;
        }

        $entropy = 0.0;
        foreach ($probs as $p) {
            $entropy -= $p * log($p);
        }

        return $entropy / log(6);
    }

    public static function grade(?float $normalizedEntropy): ?string
    {
        if ($normalizedEntropy === null) {
            return null;
        }

        $thresholds = config('ml.confidence_thresholds');

        if ($normalizedEntropy <= $thresholds['S']) {
            return 'S';
        }
        if ($normalizedEntropy <= $thresholds['A']) {
            return 'A';
        }
        if ($normalizedEntropy <= $thresholds['B']) {
            return 'B';
        }

        return '見送り';
    }

    /**
     * @param  array<int, float|null>  $pFirstByLane
     */
    public static function lane1Risk(array $pFirstByLane): ?bool
    {
        if (! array_key_exists(1, $pFirstByLane) || $pFirstByLane[1] === null) {
            return null;
        }

        return $pFirstByLane[1] < config('ml.lane1_risk_threshold');
    }
}
