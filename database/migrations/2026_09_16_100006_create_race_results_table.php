<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('race_results', function (Blueprint $table) {
            $table->id();
            $table->foreignId('race_entry_id')->unique()->constrained('race_entries')->cascadeOnDelete();
            $table->unsignedTinyInteger('start_course')->nullable(); // 進入コース（lane とは別物）
            $table->decimal('st', 4, 2)->nullable(); // 符号付き。フライングは負値
            $table->unsignedTinyInteger('finish_pos')->nullable(); // 失格・欠場等は null
            $table->string('abnormal_code')->nullable(); // F/L0/L1/K/S 等の事由コード
            $table->decimal('race_time', 6, 2)->nullable(); // 走破タイム
            $table->timestamps();
        });

        DB::statement(
            'ALTER TABLE race_results ADD CONSTRAINT race_results_start_course_range '
            .'CHECK (start_course IS NULL OR start_course BETWEEN 1 AND 6)'
        );
        DB::statement(
            'ALTER TABLE race_results ADD CONSTRAINT race_results_finish_pos_range '
            .'CHECK (finish_pos IS NULL OR finish_pos BETWEEN 1 AND 6)'
        );
    }

    public function down(): void
    {
        Schema::dropIfExists('race_results');
    }
};
