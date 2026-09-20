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

    /**
     * config('ml.lane1_risk_levels')（閾値の小さい順）に照らして
     * 「高」「中」等のラベルを返す。lane1_risk=falseの艇はnull。
     *
     * @param  array<int, float|null>  $pFirstByLane
     */
    public static function lane1RiskLevel(array $pFirstByLane): ?string
    {
        if (! array_key_exists(1, $pFirstByLane) || $pFirstByLane[1] === null) {
            return null;
        }

        $p1 = $pFirstByLane[1];

        foreach (config('ml.lane1_risk_levels') as $label => $threshold) {
            if ($p1 < $threshold) {
                return $label;
            }
        }

        return null;
    }

    /**
     * p_firstが最大の艇番。
     *
     * @param  array<int, float|null>  $pFirstByLane
     */
    public static function topLane(array $pFirstByLane): ?int
    {
        $probs = array_filter($pFirstByLane, fn (?float $p): bool => $p !== null);

        if ($probs === []) {
            return null;
        }

        return array_search(max($probs), $probs, true);
    }

    /**
     * 本命が1号艇以外、かつ本命の確率がconfig('ml.upset_pick_threshold')以上の
     * レース（「妙味のあるレース」＝モデルの判断が際立つレース）かどうか。
     *
     * @param  array<int, float|null>  $pFirstByLane
     */
    public static function isUpsetPick(array $pFirstByLane): bool
    {
        $topLane = self::topLane($pFirstByLane);

        if ($topLane === null || $topLane === 1) {
            return false;
        }

        return $pFirstByLane[$topLane] >= config('ml.upset_pick_threshold');
    }
}
