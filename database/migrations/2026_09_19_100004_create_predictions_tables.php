<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // 1回のモデル実行・1レース分の予測をまとめる単位。
        // stage=1/2は将来の「締切前の早い段階の予測」「締切直前の最終予測」等の
        // 2段階publishを想定した区分（現時点では書き込みロジックは実装しない）。
        Schema::create('predictions', function (Blueprint $table) {
            $table->id();
            $table->foreignId('race_id')->constrained('races')->restrictOnDelete();
            $table->string('model_version');
            $table->unsignedTinyInteger('stage'); // 1 or 2
            $table->timestampTz('published_at');
            $table->timestampTz('cutoff_at');
            $table->timestamps();

            $table->unique(['race_id', 'model_version', 'stage']);
        });

        DB::statement(
            'ALTER TABLE predictions ADD CONSTRAINT predictions_stage_range CHECK (stage IN (1, 2))'
        );

        // 艇(lane)ごとの予測確率。1着・2連対・3連対の確率を持つ。
        Schema::create('prediction_entries', function (Blueprint $table) {
            $table->foreignId('prediction_id')->constrained('predictions')->cascadeOnDelete();
            $table->unsignedTinyInteger('lane');
            $table->decimal('p_first', 6, 5)->nullable();
            $table->decimal('p_top2', 6, 5)->nullable();
            $table->decimal('p_top3', 6, 5)->nullable();

            $table->primary(['prediction_id', 'lane']);
        });

        DB::statement(
            'ALTER TABLE prediction_entries ADD CONSTRAINT prediction_entries_lane_range '
            .'CHECK (lane BETWEEN 1 AND 6)'
        );

        // 推奨する舟券（式別・組番）の一覧。rankは推奨順（1が最優先）。
        Schema::create('prediction_tickets', function (Blueprint $table) {
            $table->id();
            $table->foreignId('prediction_id')->constrained('predictions')->cascadeOnDelete();
            $table->string('bet_type'); // 例: 3連単/2連単/拡連複
            $table->string('combination'); // 例: "1-2-3"
            $table->decimal('est_prob', 6, 5)->nullable();
            $table->smallInteger('rank')->nullable(); // 推奨順（1が最優先）
        });

        DB::statement(
            'ALTER TABLE prediction_tickets ADD CONSTRAINT prediction_tickets_rank_positive '
            .'CHECK (rank IS NULL OR rank >= 1)'
        );

        // 舟券ごとの的中結果。predictions/prediction_entries/prediction_tickets と違い、
        // レース確定後に書き込まれるものなのでイミュータブル化トリガーの対象外。
        Schema::create('prediction_results', function (Blueprint $table) {
            $table->foreignId('prediction_id')->constrained('predictions')->cascadeOnDelete();
            $table->foreignId('ticket_id')->constrained('prediction_tickets')->cascadeOnDelete();
            $table->boolean('hit');
            $table->integer('payout')->nullable();
            $table->timestampTz('judged_at');

            $table->primary('ticket_id');
        });

        DB::statement(
            'ALTER TABLE prediction_results ADD CONSTRAINT prediction_results_payout_non_negative '
            .'CHECK (payout IS NULL OR payout >= 0)'
        );
    }

    public function down(): void
    {
        Schema::dropIfExists('prediction_results');
        Schema::dropIfExists('prediction_tickets');
        Schema::dropIfExists('prediction_entries');
        Schema::dropIfExists('predictions');
    }
};
