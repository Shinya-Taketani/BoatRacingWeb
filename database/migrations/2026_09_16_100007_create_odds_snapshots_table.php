<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('odds_snapshots', function (Blueprint $table) {
            $table->id();
            $table->foreignId('race_id')->constrained('races')->cascadeOnDelete();
            $table->string('bet_type'); // 単勝/複勝/2連単/2連複/拡連複/3連単/3連複
            $table->string('combination'); // 例: "1-2-3"
            $table->decimal('odds', 7, 1);
            $table->timestampTz('captured_at'); // 取得時刻（特徴量に使えるのは deadline_at-10分以前のみ、は生成側で担保）
            $table->timestamps();

            $table->unique(['race_id', 'bet_type', 'combination', 'captured_at']);
            $table->index(['race_id', 'captured_at']);
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('odds_snapshots');
    }
};
