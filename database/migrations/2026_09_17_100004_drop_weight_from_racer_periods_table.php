<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // weight(体重)はレースごとに変動しうる値で、級別・勝率のような
        // 前期/後期でしか変わらない「期間スナップショット」の性質と異なるため
        // racer_periods の変化検出対象から外す。観測値としては
        // racer_daily_snapshots に残り、当日値は race_entries.weight を使う。
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION racer_periods_prevent_snapshot_update()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.racer_id IS DISTINCT FROM OLD.racer_id
                    OR NEW.valid_from IS DISTINCT FROM OLD.valid_from
                    OR NEW.racer_class IS DISTINCT FROM OLD.racer_class
                    OR NEW.branch IS DISTINCT FROM OLD.branch
                    OR NEW.national_win_rate IS DISTINCT FROM OLD.national_win_rate
                    OR NEW.national_win_rate_2 IS DISTINCT FROM OLD.national_win_rate_2
                    OR NEW.local_win_rate IS DISTINCT FROM OLD.local_win_rate
                    OR NEW.local_win_rate_2 IS DISTINCT FROM OLD.local_win_rate_2
                    OR NEW.created_at IS DISTINCT FROM OLD.created_at
                THEN
                    RAISE EXCEPTION
                        'racer_periods: snapshot columns are immutable except valid_to (racer_id=%, valid_from=%). Close this period and INSERT a new row instead of UPDATE.',
                        OLD.racer_id, OLD.valid_from;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        SQL);

        Schema::table('racer_periods', function (Blueprint $table) {
            $table->dropColumn('weight');
        });
    }

    public function down(): void
    {
        Schema::table('racer_periods', function (Blueprint $table) {
            $table->decimal('weight', 5, 2)->nullable();
        });

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION racer_periods_prevent_snapshot_update()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.racer_id IS DISTINCT FROM OLD.racer_id
                    OR NEW.valid_from IS DISTINCT FROM OLD.valid_from
                    OR NEW.racer_class IS DISTINCT FROM OLD.racer_class
                    OR NEW.branch IS DISTINCT FROM OLD.branch
                    OR NEW.national_win_rate IS DISTINCT FROM OLD.national_win_rate
                    OR NEW.national_win_rate_2 IS DISTINCT FROM OLD.national_win_rate_2
                    OR NEW.local_win_rate IS DISTINCT FROM OLD.local_win_rate
                    OR NEW.local_win_rate_2 IS DISTINCT FROM OLD.local_win_rate_2
                    OR NEW.weight IS DISTINCT FROM OLD.weight
                    OR NEW.created_at IS DISTINCT FROM OLD.created_at
                THEN
                    RAISE EXCEPTION
                        'racer_periods: snapshot columns are immutable except valid_to (racer_id=%, valid_from=%). Close this period and INSERT a new row instead of UPDATE.',
                        OLD.racer_id, OLD.valid_from;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        SQL);
    }
};
