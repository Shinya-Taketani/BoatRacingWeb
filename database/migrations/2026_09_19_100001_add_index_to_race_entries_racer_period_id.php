<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // racer_period_id 列に索引が無く、racer_periods 側の DELETE(ON DELETE
        // RESTRICT)がこの外部キーを検証するたびに race_entries の全件シーケン
        // シャルスキャンになっていた。racer_periods の一括再構築時に致命的な
        // 遅さの原因となったため追加する。
        Schema::table('race_entries', function (Blueprint $table) {
            $table->index('racer_period_id');
        });
    }

    public function down(): void
    {
        Schema::table('race_entries', function (Blueprint $table) {
            $table->dropIndex(['racer_period_id']);
        });
    }
};
