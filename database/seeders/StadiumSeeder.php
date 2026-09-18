<?php

namespace Database\Seeders;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;

class StadiumSeeder extends Seeder
{
    /**
     * 公式24場のコード・場名。
     *
     * 出典: 番組表(B)ファイルの場ヘッダ（"{code}BBGN" 直後の
     * "ボートレース{場名}" 行）から実データを抽出して確定した値
     * （2026-09-10〜2026-09-17分のBファイル、全24場分を実際に確認済み）。
     */
    private const STADIUMS = [
        1 => '桐生',
        2 => '戸田',
        3 => '江戸川',
        4 => '平和島',
        5 => '多摩川',
        6 => '浜名湖',
        7 => '蒲郡',
        8 => '常滑',
        9 => '津',
        10 => '三国',
        11 => 'びわこ',
        12 => '住之江',
        13 => '尼崎',
        14 => '鳴門',
        15 => '丸亀',
        16 => '児島',
        17 => '宮島',
        18 => '徳山',
        19 => '下関',
        20 => '若松',
        21 => '芦屋',
        22 => '福岡',
        23 => '唐津',
        24 => '大村',
    ];

    public function run(): void
    {
        $now = now();

        $rows = collect(self::STADIUMS)->map(fn (string $name, int $code) => [
            'code' => $code,
            'name' => $name,
            'created_at' => $now,
            'updated_at' => $now,
        ])->values()->all();

        DB::table('stadiums')->upsert(
            $rows,
            ['code'],
            ['name', 'updated_at'],
        );
    }
}
