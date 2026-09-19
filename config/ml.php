<?php

return [

    /*
    |--------------------------------------------------------------------------
    | 本番運用で使う予測モデルのバージョン
    |--------------------------------------------------------------------------
    |
    | ml/models/{prediction_model_version}.pkl を使う（ml/src/ml/models/train.py
    | が保存するファイル名 = model_version と一致させること）。新しいモデルを
    | 学習・保存したら .env の PREDICTION_MODEL_VERSION を更新する。
    |
    */

    'prediction_model_version' => env('PREDICTION_MODEL_VERSION'),

];
