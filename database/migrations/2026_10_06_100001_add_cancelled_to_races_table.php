<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // 荒天等によるレース中止（Kファイルの[払戻金]概況表に「中止」と
        // 記録される）を表すフラグ。中止レースは race_results/payouts が
        // 恒久的に存在しないため、DataCoverage::refreshCoverage()の
        // has_results/has_payouts判定でこのレースを分母から除外するために
        // 使う（CLAUDE.md「中止レースを考慮したhas_results/has_payoutsの
        // 修正」参照）。
        Schema::table('races', function (Blueprint $table) {
            $table->boolean('cancelled')->default(false)->after('title');
        });
    }

    public function down(): void
    {
        Schema::table('races', function (Blueprint $table) {
            $table->dropColumn('cancelled');
        });
    }
};
