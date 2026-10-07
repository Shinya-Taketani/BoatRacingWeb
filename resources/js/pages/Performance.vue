<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { fetchConfidentTop3Performance, fetchPerformance } from '../api';
import { formatPercent } from '../format';

const summary = ref(null);
const loading = ref(true);
const error = ref(null);

const confident = ref(null);
const confidentThreshold = ref(0.84);
const confidentLoading = ref(true);
const confidentError = ref(null);

onMounted(async () => {
    try {
        summary.value = await fetchPerformance();
    } catch (e) {
        error.value = e.message;
    } finally {
        loading.value = false;
    }
    await loadConfident();
});

async function loadConfident() {
    confidentLoading.value = true;
    confidentError.value = null;
    try {
        confident.value = await fetchConfidentTop3Performance(confidentThreshold.value);
    } catch (e) {
        confidentError.value = e.message;
    } finally {
        confidentLoading.value = false;
    }
}

watch(confidentThreshold, loadConfident);

function isBelow90(hitRate) {
    return hitRate !== null && hitRate !== undefined && hitRate < 0.9;
}

// daily は日付降順(JST基準、サーバ側のRaceDate::todayで算出)で返るため、
// 先頭が当日・2番目が前日になる。ブラウザのローカルタイムゾーンでの日付
// 計算はJSTとずれる可能性があるため使わない。
const yesterday = computed(() => summary.value?.daily?.[1] ?? null);
</script>

<template>
    <div>
        <h1 class="mb-4 text-xl font-semibold">実績サマリ</h1>

        <p v-if="loading" class="text-slate-500">読み込み中...</p>
        <p v-else-if="error" class="text-red-600">読み込みに失敗しました: {{ error }}</p>

        <div v-else-if="summary">
            <p class="mb-4 text-sm text-slate-500">モデル: {{ summary.model_version }}（3連単・全戦全勝ではなく買い目ベース）</p>

            <div class="mb-8 grid grid-cols-2 gap-4 sm:grid-cols-4">
                <div class="rounded-lg border border-slate-200 bg-white p-4">
                    <p class="text-xs text-slate-500">対象レース数</p>
                    <p class="text-2xl font-semibold">{{ summary.overall.races.toLocaleString() }}</p>
                </div>
                <div class="rounded-lg border border-slate-200 bg-white p-4">
                    <p class="text-xs text-slate-500">的中率</p>
                    <p class="text-2xl font-semibold">{{ formatPercent(summary.overall.hit_rate) }}</p>
                </div>
                <div class="rounded-lg border border-slate-200 bg-white p-4">
                    <p class="text-xs text-slate-500">回収率</p>
                    <p class="text-2xl font-semibold">{{ formatPercent(summary.overall.recovery_rate) }}</p>
                </div>
                <div class="rounded-lg border border-slate-200 bg-white p-4">
                    <p class="text-xs text-slate-500">購入額 / 払戻額</p>
                    <p class="text-lg font-semibold">
                        {{ summary.overall.stake.toLocaleString() }}円 / {{ summary.overall.payout.toLocaleString() }}円
                    </p>
                </div>
                <div class="rounded-lg border border-slate-200 bg-white p-4">
                    <p class="text-xs text-slate-500">
                        昨日の的中率<span v-if="yesterday" class="ml-1 text-slate-400">({{ yesterday.date }})</span>
                    </p>
                    <p class="text-2xl font-semibold">{{ formatPercent(yesterday?.hit_rate) }}</p>
                    <p class="text-xs text-slate-400">対象{{ (yesterday?.races ?? 0).toLocaleString() }}レース</p>
                </div>
            </div>

            <p class="mb-8 text-sm text-slate-500">
                1レースあたり平均: 買い目{{ summary.overall.avg_tickets_per_race }}点
                / 購入{{ summary.overall.avg_stake_per_race?.toLocaleString() }}円
                / 払戻{{ summary.overall.avg_payout_per_race?.toLocaleString() }}円
            </p>

            <div class="mb-10 rounded-lg border border-slate-200 bg-white p-4">
                <div class="mb-1 flex flex-wrap items-center justify-between gap-2">
                    <h2 class="text-lg font-semibold">3着内確実な艇（軸艇）の実績</h2>
                    <label class="flex items-center gap-2 text-sm text-slate-600">
                        閾値:
                        <select v-model.number="confidentThreshold" class="rounded border-slate-300 text-sm">
                            <option
                                v-for="opt in confident?.threshold_options ?? [0.80, 0.84, 0.88, 0.92, 0.96]"
                                :key="opt"
                                :value="opt"
                            >
                                {{ (opt * 100).toFixed(0) }}%以上
                            </option>
                        </select>
                    </label>
                </div>
                <p class="mb-4 text-xs text-slate-500">
                    p_top3(3着以内に入る確率)が閾値以上の艇を1レース1艇選んだ場合の、実際の3着以内率。90%を下回っていないか継続的に確認する。
                </p>

                <p v-if="confidentLoading" class="text-slate-500">読み込み中...</p>
                <p v-else-if="confidentError" class="text-red-600">読み込みに失敗しました: {{ confidentError }}</p>

                <template v-else-if="confident">
                    <div
                        class="mb-6 inline-flex items-center gap-3 rounded-lg border p-4"
                        :class="isBelow90(confident.overall.hit_rate) ? 'border-rose-300 bg-rose-50' : 'border-slate-200'"
                    >
                        <div>
                            <p class="text-xs text-slate-500">全体実績</p>
                            <p
                                class="text-2xl font-semibold"
                                :class="isBelow90(confident.overall.hit_rate) ? 'text-rose-700' : ''"
                            >
                                {{ formatPercent(confident.overall.hit_rate) }}
                            </p>
                            <p class="text-xs text-slate-400">
                                {{ confident.overall.hit.toLocaleString() }}/{{ confident.overall.confident_races.toLocaleString() }}件的中
                            </p>
                        </div>
                        <span
                            v-if="isBelow90(confident.overall.hit_rate)"
                            class="rounded-full bg-rose-600 px-2 py-0.5 text-xs font-semibold text-white"
                        >
                            90%割れ
                        </span>
                    </div>

                    <h3 class="mb-2 text-sm font-semibold text-slate-500">日別（直近30日）</h3>
                    <div class="mb-6 overflow-x-auto rounded-lg border border-slate-200">
                        <table class="w-full min-w-[420px] text-sm">
                            <thead class="bg-slate-50 text-left text-xs text-slate-500">
                                <tr>
                                    <th class="px-3 py-2">日付</th>
                                    <th class="px-3 py-2">該当艇数</th>
                                    <th class="px-3 py-2">的中数</th>
                                    <th class="px-3 py-2">実際3着以内率</th>
                                </tr>
                            </thead>
                            <tbody>
                                <tr
                                    v-for="row in confident.daily"
                                    :key="row.date"
                                    class="border-t border-slate-100"
                                    :class="
                                        row.confident_races === 0
                                            ? 'bg-slate-50 text-slate-400'
                                            : isBelow90(row.hit_rate)
                                              ? 'bg-rose-50'
                                              : ''
                                    "
                                >
                                    <td class="px-3 py-2 font-medium">{{ row.date }}</td>
                                    <td class="px-3 py-2">{{ row.confident_races.toLocaleString() }}</td>
                                    <td class="px-3 py-2">{{ row.hit.toLocaleString() }}</td>
                                    <td class="px-3 py-2" :class="isBelow90(row.hit_rate) ? 'font-semibold text-rose-700' : ''">
                                        {{ row.confident_races === 0 ? '-' : formatPercent(row.hit_rate) }}
                                    </td>
                                </tr>
                            </tbody>
                        </table>
                    </div>

                    <h3 class="mb-2 text-sm font-semibold text-slate-500">月別</h3>
                    <div class="overflow-x-auto rounded-lg border border-slate-200">
                        <table class="w-full min-w-[420px] text-sm">
                            <thead class="bg-slate-50 text-left text-xs text-slate-500">
                                <tr>
                                    <th class="px-3 py-2">月</th>
                                    <th class="px-3 py-2">該当艇数</th>
                                    <th class="px-3 py-2">的中数</th>
                                    <th class="px-3 py-2">実際3着以内率</th>
                                </tr>
                            </thead>
                            <tbody>
                                <tr
                                    v-for="row in confident.monthly"
                                    :key="row.month"
                                    class="border-t border-slate-100"
                                    :class="row.confident_races > 0 && isBelow90(row.hit_rate) ? 'bg-rose-50' : ''"
                                >
                                    <td class="px-3 py-2 font-medium">{{ row.month }}</td>
                                    <td class="px-3 py-2">{{ row.confident_races.toLocaleString() }}</td>
                                    <td class="px-3 py-2">{{ row.hit.toLocaleString() }}</td>
                                    <td class="px-3 py-2" :class="isBelow90(row.hit_rate) ? 'font-semibold text-rose-700' : ''">
                                        {{ row.confident_races === 0 ? '-' : formatPercent(row.hit_rate) }}
                                    </td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </template>
            </div>

            <h2 class="mb-2 text-sm font-semibold text-slate-500">日別（直近30日）</h2>
            <div class="mb-8 overflow-x-auto rounded-lg border border-slate-200 bg-white">
                <table class="w-full min-w-[560px] text-sm">
                    <thead class="bg-slate-50 text-left text-xs text-slate-500">
                        <tr>
                            <th class="px-3 py-2">日付</th>
                            <th class="px-3 py-2">レース数</th>
                            <th class="px-3 py-2">的中率</th>
                            <th class="px-3 py-2">購入額</th>
                            <th class="px-3 py-2">払戻額</th>
                            <th class="px-3 py-2">回収率</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr
                            v-for="row in summary.daily"
                            :key="row.date"
                            class="border-t border-slate-100"
                            :class="row.races === 0 ? 'bg-slate-50 text-slate-400' : ''"
                        >
                            <td class="px-3 py-2 font-medium">{{ row.date }}</td>
                            <td class="px-3 py-2">{{ row.races.toLocaleString() }}</td>
                            <td class="px-3 py-2">{{ formatPercent(row.hit_rate) }}</td>
                            <td class="px-3 py-2">{{ row.stake > 0 ? `${row.stake.toLocaleString()}円` : '-' }}</td>
                            <template v-if="row.races === 0">
                                <td class="px-3 py-2">集計中</td>
                                <td class="px-3 py-2">集計中</td>
                            </template>
                            <template v-else>
                                <td class="px-3 py-2">{{ row.payout.toLocaleString() }}円</td>
                                <td class="px-3 py-2">{{ formatPercent(row.recovery_rate) }}</td>
                            </template>
                        </tr>
                    </tbody>
                </table>
            </div>

            <h2 class="mb-2 text-sm font-semibold text-slate-500">月別</h2>
            <div class="overflow-x-auto rounded-lg border border-slate-200 bg-white">
                <table class="w-full min-w-[560px] text-sm">
                    <thead class="bg-slate-50 text-left text-xs text-slate-500">
                        <tr>
                            <th class="px-3 py-2">月</th>
                            <th class="px-3 py-2">レース数</th>
                            <th class="px-3 py-2">的中率</th>
                            <th class="px-3 py-2">購入額</th>
                            <th class="px-3 py-2">払戻額</th>
                            <th class="px-3 py-2">回収率</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-for="row in summary.monthly" :key="row.month" class="border-t border-slate-100">
                            <td class="px-3 py-2 font-medium">{{ row.month }}</td>
                            <td class="px-3 py-2">{{ row.races.toLocaleString() }}</td>
                            <td class="px-3 py-2">{{ formatPercent(row.hit_rate) }}</td>
                            <td class="px-3 py-2">{{ row.stake.toLocaleString() }}円</td>
                            <td class="px-3 py-2">{{ row.payout.toLocaleString() }}円</td>
                            <td class="px-3 py-2">{{ formatPercent(row.recovery_rate) }}</td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <p class="mt-4 text-xs leading-relaxed text-slate-400">
                回収率は公営競技の控除率（約25%）とほぼ同水準です。買い続けた場合、長期的には購入額の約25%が減少します。本サービスは的中率を高めるための情報提供であり、利益を保証するものではありません。
            </p>
        </div>
    </div>
</template>
