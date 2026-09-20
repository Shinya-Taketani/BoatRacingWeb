<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // races.title はレースごとの回次名（例: "予選" "優勝戦"）であり、
        // 大会名（例: "第71回ボートレースダービー"）とは別物。大会名は
        // グレード(SG/G1/G2/G3/一般)推定の元データになるが、これまで
        // パースされておらずどこにも保存されていなかった。推定ロジック自体は
        // 別途実装するため、ここでは生の大会名文字列の保存のみ行う。
        Schema::table('races', function (Blueprint $table) {
            $table->string('event_name')->nullable()->after('title');
        });
    }

    public function down(): void
    {
        Schema::table('races', function (Blueprint $table) {
            $table->dropColumn('event_name');
        });
    }
};
