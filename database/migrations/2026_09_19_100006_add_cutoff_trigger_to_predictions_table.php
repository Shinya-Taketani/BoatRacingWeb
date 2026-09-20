<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        // cutoff_at は races.deadline_at より前でなければならない（配信時刻の
        // 逆転を防止）。features テーブルの features_cutoff_before_deadline
        // トリガーと同じパターン。
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION predictions_cutoff_before_deadline()
            RETURNS trigger AS $$
            DECLARE
                v_deadline_at timestamptz;
            BEGIN
                SELECT deadline_at INTO v_deadline_at FROM races WHERE id = NEW.race_id;

                IF v_deadline_at IS NULL THEN
                    RAISE EXCEPTION 'predictions: race_id=% not found in races', NEW.race_id;
                END IF;

                IF NEW.cutoff_at >= v_deadline_at THEN
                    RAISE EXCEPTION
                        'predictions: cutoff_at (%) must be before races.deadline_at (%) for race_id=%',
                        NEW.cutoff_at, v_deadline_at, NEW.race_id;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        SQL);

        // UPDATE時にもこのトリガーは動くが、predictions_prevent_update の方が
        // 先に必ずUPDATE自体を拒否する（published_atがNOT NULLのため常に不変）。
        // それでも将来の変更に備え、BEFORE INSERT OR UPDATE の両方に掛けておく。
        DB::statement(<<<'SQL'
            CREATE TRIGGER predictions_cutoff_before_deadline
                BEFORE INSERT OR UPDATE ON predictions
                FOR EACH ROW
                EXECUTE FUNCTION predictions_cutoff_before_deadline();
        SQL);
    }

    public function down(): void
    {
        DB::statement('DROP TRIGGER IF EXISTS predictions_cutoff_before_deadline ON predictions');
        DB::statement('DROP FUNCTION IF EXISTS predictions_cutoff_before_deadline()');
    }
};
