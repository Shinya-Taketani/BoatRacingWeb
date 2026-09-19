<script setup>
import { onMounted, ref } from 'vue';
import { fetchPerformance } from '../api';
import { formatPercent } from '../format';

const summary = ref(null);
const loading = ref(true);
const error = ref(null);

onMounted(async () => {
    try {
        summary.value = await fetchPerformance();
    } catch (e) {
        error.value = e.message;
    } finally {
        loading.value = false;
    }
});
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
        </div>
    </div>
</template>
