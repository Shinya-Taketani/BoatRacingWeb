<?php

use Illuminate\Support\Facades\Route;

// Vue Router(history mode)のSPAを配信する。/api/* はbootstrap/app.phpの
// apiルートグループが別途処理するため、ここでは扱わない。
Route::get('/{any}', function () {
    return view('app');
})->where('any', '.*');
