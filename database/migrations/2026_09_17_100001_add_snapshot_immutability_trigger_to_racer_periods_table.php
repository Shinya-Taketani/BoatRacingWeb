<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        // racer_periods のスナップショット列（級別・勝率系など）は確定後イミュータブル。
        // 訂正が必要な場合は valid_to を閉じて新しい期間行を INSERT すること。
        // valid_to（期間クローズ）と updated_at / id（技術的なカラム）のみ変更を許可する。
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION racer_periods_prevent_snapshot_update()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.racer_id IS DISTINCT FROM OLD.racer_id
                    OR NEW.valid_from IS DISTINCT FROM OLD.valid_from
                    OR NEW.racer_class IS DISTINCT FROM OLD.racer_class
                    OR NEW.branch IS DISTINCT FROM OLD.branch
                    OR NEW.birthplace IS DISTINCT FROM OLD.birthplace
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

        DB::statement(<<<'SQL'
            CREATE TRIGGER racer_periods_prevent_snapshot_update
                BEFORE UPDATE ON racer_periods
                FOR EACH ROW
                EXECUTE FUNCTION racer_periods_prevent_snapshot_update();
        SQL);
    }

    public function down(): void
    {
        DB::statement('DROP TRIGGER IF EXISTS racer_periods_prevent_snapshot_update ON racer_periods');
        DB::statement('DROP FUNCTION IF EXISTS racer_periods_prevent_snapshot_update()');
    }
};
