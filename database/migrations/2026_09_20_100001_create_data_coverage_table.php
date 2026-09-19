<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // data:catch-up（起動時の欠損自動検出・補完）が読み書きする、
        // 日付ごとのデータ取得状況。races/race_results/payouts/predictions は
        // 「その日の全レース数と一致して初めて揃った」とみなす。
        // オッズは締切後には取得し直せないため、埋めようとはせず
        // odds_race_count（取得できたレース数。0なら完全欠損）として
        // 記録するだけにとどめる。
        Schema::create('data_coverage', function (Blueprint $table) {
            $table->date('race_date')->primary();
            $table->boolean('has_races')->default(false);
            $table->boolean('has_results')->default(false);
            $table->boolean('has_payouts')->default(false);
            $table->boolean('has_predictions')->default(false);
            $table->unsignedSmallInteger('odds_race_count')->default(0);
            $table->timestamps();
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('data_coverage');
    }
};
