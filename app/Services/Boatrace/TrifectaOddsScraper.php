<?php

namespace App\Services\Boatrace;

use Carbon\CarbonImmutable;
use DateTimeInterface;
use Illuminate\Support\Facades\Http;
use Symfony\Component\DomCrawler\Crawler;

/**
 * BOAT RACE公式サイトの3連単オッズページ(odds3t)を取得・パースする。
 *
 * URL: https://www.boatrace.jp/owpc/pc/race/odds3t?rno={race_no}&jcd={stadium_code:02d}&hd={Ymd}
 *
 * ページは1着ボート(1-6)ごとに6つの列グループを横に並べたテーブルで、
 * 各グループ内は2着ボートをrowspanでまとめて4行(3着×4通り)を表現している。
 * rowspanはHTML描画上の省略であり実データには全120通りが必要なため、
 * グループごとに「現在の2着ボート番号」と「残りrowspan行数」を追跡して
 * 展開する。
 */
class TrifectaOddsScraper
{
    private const BASE_URL = 'https://www.boatrace.jp/owpc/pc/race/odds3t';

    private const USER_AGENT = 'Mozilla/5.0 (compatible; BoatRacingWeb odds collector)';

    public function fetch(int $stadiumCode, int $raceNo, DateTimeInterface $raceDate): TrifectaOddsResult
    {
        $response = Http::withHeaders(['User-Agent' => self::USER_AGENT])
            ->timeout(15)
            ->get(self::BASE_URL, [
                'rno' => $raceNo,
                'jcd' => sprintf('%02d', $stadiumCode),
                'hd' => $raceDate->format('Ymd'),
            ]);

        // captured_at はサーバー時刻ではなく、レスポンス受信直後の時刻を使う。
        $capturedAt = CarbonImmutable::now();

        if (! $response->successful()) {
            throw new OddsFetchException(
                "odds3t request failed: HTTP {$response->status()} ".
                "(jcd={$stadiumCode}, rno={$raceNo}, hd={$raceDate->format('Ymd')})"
            );
        }

        $html = $response->body();

        if (str_contains($html, 'データがありません')) {
            throw new OddsFetchException(
                "no odds data for jcd={$stadiumCode}, rno={$raceNo}, hd={$raceDate->format('Ymd')} ".
                '(race may not exist or odds not yet published)'
            );
        }

        $odds = $this->parseTrifectaTable($html, $stadiumCode, $raceNo);

        return new TrifectaOddsResult($capturedAt, $odds);
    }

    /**
     * @return array<int, array{combination: string, odds: float}>
     */
    private function parseTrifectaTable(string $html, int $stadiumCode, int $raceNo): array
    {
        $crawler = new Crawler();
        $crawler->addHtmlContent($html, 'UTF-8');

        $targetTable = null;
        foreach ($crawler->filter('table') as $tableNode) {
            $tableCrawler = new Crawler($tableNode);
            if ($tableCrawler->filter('td.oddsPoint')->count() > 0) {
                $targetTable = $tableCrawler;
                break;
            }
        }

        if ($targetTable === null) {
            throw new OddsFetchException(
                "3連単オッズテーブルが見つかりません (jcd={$stadiumCode}, rno={$raceNo})"
            );
        }

        $rows = $targetTable->filter('tr');
        if ($rows->count() < 2) {
            throw new OddsFetchException(
                "3連単オッズテーブルの行数が不正です (jcd={$stadiumCode}, rno={$raceNo})"
            );
        }

        // グループ(0-5)ごとに「現在の2着ボート番号」と「残りrowspan行数」を保持
        $groupState = array_fill(0, 6, ['second' => null, 'remaining' => 0]);
        $combos = [];

        // 先頭行は見出し(th)のみなのでスキップ
        $rows->slice(1)->each(function (Crawler $row) use (&$groupState, &$combos) {
            $cellNodes = [];
            $row->filter('td')->each(function (Crawler $c) use (&$cellNodes) {
                $cellNodes[] = $c;
            });

            $ci = 0;
            for ($g = 0; $g < 6; $g++) {
                $firstPlace = $g + 1;

                if ($groupState[$g]['remaining'] === 0) {
                    $secondCell = $cellNodes[$ci++];
                    $rowspan = (int) ($secondCell->attr('rowspan') ?? 1);
                    $groupState[$g]['second'] = (int) trim($secondCell->text());
                    $groupState[$g]['remaining'] = $rowspan;
                }

                $second = $groupState[$g]['second'];
                $thirdCell = $cellNodes[$ci++];
                $oddsCell = $cellNodes[$ci++];
                $third = (int) trim($thirdCell->text());
                $oddsText = trim($oddsCell->text());

                $combos[] = [
                    'combination' => "{$firstPlace}-{$second}-{$third}",
                    'odds' => (float) $oddsText,
                ];

                $groupState[$g]['remaining']--;
            }
        });

        if (count($combos) !== 120) {
            throw new OddsFetchException(
                'expected 120 trifecta combinations, got '.count($combos).
                " (jcd={$stadiumCode}, rno={$raceNo})"
            );
        }

        $distinct = array_unique(array_column($combos, 'combination'));
        if (count($distinct) !== 120) {
            throw new OddsFetchException(
                "duplicate combinations detected while parsing odds table (jcd={$stadiumCode}, rno={$raceNo})"
            );
        }

        return $combos;
    }
}
