<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('race_entries', function (Blueprint $table) {
            $table->id();
            $table->foreignId('race_id')->constrained('races')->cascadeOnDelete();
            $table->foreignId('racer_id')->constrained('racers')->restrictOnDelete();
            // 配信時刻時点で参照した racer_periods を固定し、後からの更新で
            // 学習時の特徴量が変わらないようにする
            $table->foreignId('racer_period_id')->nullable()->constrained('racer_periods')->restrictOnDelete();
            $table->unsignedTinyInteger('lane'); // 枠番 1-6
            $table->unsignedTinyInteger('motor_no')->nullable();
            $table->decimal('motor_win_rate_2', 5, 2)->nullable();
            $table->unsignedTinyInteger('boat_no')->nullable();
            $table->decimal('boat_win_rate_2', 5, 2)->nullable();
            $table->decimal('exhibition_time', 4, 2)->nullable(); // 展示タイム
            $table->decimal('weight', 5, 2)->nullable(); // 当日体重
            $table->timestamps();

            $table->unique(['race_id', 'lane']);
            $table->unique(['race_id', 'racer_id']);
        });

        DB::statement(
            'ALTER TABLE race_entries ADD CONSTRAINT race_entries_lane_range CHECK (lane BETWEEN 1 AND 6)'
        );
    }

    public function down(): void
    {
        Schema::dropIfExists('race_entries');
    }
};
