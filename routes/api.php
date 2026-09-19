<?php

use App\Http\Controllers\Api\PerformanceController;
use App\Http\Controllers\Api\RaceController;
use Illuminate\Support\Facades\Route;

// 認証は未実装（読み取り専用）。書き込み系エンドポイントを追加する際は
// 別途ミドルウェアを検討すること。
Route::get('/races/today', [RaceController::class, 'today']);
Route::get('/races/{race}', [RaceController::class, 'show']);
Route::get('/performance', [PerformanceController::class, 'index']);
