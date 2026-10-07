<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;
use Illuminate\Database\Eloquent\Relations\HasOne;

class Race extends Model
{
    protected $casts = [
        'race_date' => 'date',
        'deadline_at' => 'datetime',
        'cancelled' => 'boolean',
    ];

    public function stadium(): BelongsTo
    {
        return $this->belongsTo(Stadium::class);
    }

    public function raceEntries(): HasMany
    {
        return $this->hasMany(RaceEntry::class)->orderBy('lane');
    }

    public function predictions(): HasMany
    {
        return $this->hasMany(Prediction::class);
    }

    /**
     * 本番で使う model_version/stage(config('ml.*')) に絞った予測1件。
     * まだ予測が生成されていないレースではnullになる。
     */
    public function prediction(): HasOne
    {
        return $this->hasOne(Prediction::class)
            ->where('model_version', config('ml.prediction_model_version'))
            ->where('stage', config('ml.prediction_stage'));
    }

    /**
     * 「3着以内モデル」(config('ml.prediction_top3_model_version'))に絞った
     * 予測1件。p_top3はこちらから取得する（2026-10-04、1着予測モデルと
     * 3着以内予測モデルの2本立てに変更。CLAUDE.md「p_top3の直接学習モデル」
     * 参照）。prediction()とは別のpredictionsレコード（p_first/p_top2は
     * 常にNULL、p_top3のみ入る）。
     */
    public function top3Prediction(): HasOne
    {
        return $this->hasOne(Prediction::class)
            ->where('model_version', config('ml.prediction_top3_model_version'))
            ->where('stage', config('ml.prediction_stage'));
    }

    /**
     * stage2（直前再予測、v5_exhibitionを含む）の1着予測モデルに絞った予測1件。
     * ライブ取得(beforeinfo)が間に合ったレースのみ存在する。無ければnull
     * （2026-10-08、stage2構成。CLAUDE.md「stage2構成」参照）。
     */
    public function stage2Prediction(): HasOne
    {
        return $this->hasOne(Prediction::class)
            ->where('model_version', config('ml.prediction_stage2_model_version'))
            ->where('stage', config('ml.prediction_stage2'));
    }

    /**
     * stage2の3着以内予測モデルに絞った予測1件。stage2Prediction()と同様、
     * 存在しないレースではnull。
     */
    public function stage2Top3Prediction(): HasOne
    {
        return $this->hasOne(Prediction::class)
            ->where('model_version', config('ml.prediction_stage2_top3_model_version'))
            ->where('stage', config('ml.prediction_stage2'));
    }

    /**
     * 「レースごとにstage2があればそれ、無ければstage1」という優先ルールを
     * 全経路(API/Resource/集計)で統一するための単一の参照先。呼び出し前に
     * stage2Prediction/predictionの両方をeager loadしておくこと（load済みの
     * リレーションを読むだけなので追加クエリは発生しない）。
     */
    public function effectivePrediction(): ?Prediction
    {
        return $this->stage2Prediction ?? $this->prediction;
    }

    /**
     * effectivePrediction()のtop3版。
     */
    public function effectiveTop3Prediction(): ?Prediction
    {
        return $this->stage2Top3Prediction ?? $this->top3Prediction;
    }

    /**
     * 画面表示中の予測(p_first/p_top3のどちらか)がstage2（ライブ取得した
     * 直前情報=beforeinfoを反映したもの）由来かどうか。フロントのバッジ表示用。
     */
    public function usesBeforeInfo(): bool
    {
        return $this->effectivePrediction()?->stage === 2
            || $this->effectiveTop3Prediction()?->stage === 2;
    }

    public function payouts(): HasMany
    {
        return $this->hasMany(Payout::class);
    }
}
