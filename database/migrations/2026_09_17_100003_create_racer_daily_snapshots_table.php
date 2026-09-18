<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // 番組表(B)から取り込んだ選手プロフィールの生観測値（1選手・1日=1行）。
        // 追記専用（upsertのみ）で、racer_periods はここから導出する。
        // racer_periods と違いイミュータブル化トリガーは付けない
        // （同じ日を後から再取込みして訂正することを許容するため）。
        Schema::create('racer_daily_snapshots', function (Blueprint $table) {
            $table->foreignId('racer_id')->constrained('racers')->restrictOnDelete();
            $table->date('observed_date');
            $table->string('period_key', 6); // 例: "2026H1" = 2026年前期(1-6月)
            $table->string('racer_class'); // A1 / A2 / B1 / B2
            $table->string('branch')->nullable();
            $table->decimal('weight', 5, 2)->nullable();
            $table->decimal('national_win_rate', 5, 2)->nullable();
            $table->decimal('national_win_rate_2', 5, 2)->nullable();
            $table->decimal('local_win_rate', 5, 2)->nullable();
            $table->decimal('local_win_rate_2', 5, 2)->nullable();
            $table->string('source_file'); // 取込元ファイル名（監査用）
            $table->timestampTz('imported_at');

            $table->primary(['racer_id', 'observed_date']);
            $table->index(['racer_id', 'period_key']);
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('racer_daily_snapshots');
    }
};
