<script setup>
import { onMounted, ref, watch } from 'vue';
import { fetchRace } from '../api';
import { formatPercent, formatTime } from '../format';
import ConfidenceBadge from '../components/ConfidenceBadge.vue';

const props = defineProps({
    id: { type: [String, Number], required: true },
});

const race = ref(null);
const loading = ref(true);
const error = ref(null);

async function load() {
    loading.value = true;
    error.value = null;
    try {
        race.value = await fetchRace(props.id);
    } catch (e) {
        error.value = e.message;
    } finally {
        loading.value = false;
    }
}

onMounted(load);
watch(() => props.id, load);

function predictionFor(lane) {
    if (!race.value?.prediction) return null;
    return race.value.prediction.entries.find((e) => e.lane === lane) ?? null;
}
</script>

<template>
    <div>
        <p v-if="loading" class="text-slate-500">読み込み中...</p>
        <p v-else-if="error" class="text-red-600">読み込みに失敗しました: {{ error }}</p>

        <div v-else-if="race">
            <router-link to="/" class="text-sm text-slate-500 hover:text-slate-900">&larr; 今日のレース一覧</router-link>

            <div class="mt-2 mb-4 flex items-baseline gap-3">
                <h1 class="text-xl font-semibold">{{ race.stadium_name }} {{ race.race_no }}R</h1>
                <span class="text-sm text-slate-500">締切 {{ formatTime(race.deadline_at) }}</span>
                <ConfidenceBadge v-if="race.prediction" :grade="race.prediction.confidence_grade" />
                <span
                    v-if="race.prediction?.lane1_risk"
                    class="inline-flex items-center rounded-full bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-700"
                >
                    1号艇 危険
                </span>
            </div>
            <p v-if="race.event_name || race.title" class="mb-4 text-sm text-slate-500">
                {{ race.event_name }} {{ race.title }}
            </p>

            <h2 class="mb-2 text-sm font-semibold text-slate-500">出走表</h2>
            <div class="mb-6 overflow-x-auto rounded-lg border border-slate-200 bg-white">
                <table class="w-full min-w-[720px] text-sm">
                    <thead class="bg-slate-50 text-left text-xs text-slate-500">
                        <tr>
                            <th class="px-3 py-2">枠</th>
                            <th class="px-3 py-2">選手</th>
                            <th class="px-3 py-2">級別</th>
                            <th class="px-3 py-2">年齢</th>
                            <th class="px-3 py-2">モーター2連率</th>
                            <th class="px-3 py-2">ボート2連率</th>
                            <th class="px-3 py-2">展示</th>
                            <th class="px-3 py-2">p_first</th>
                            <th class="px-3 py-2">p_top2</th>
                            <th class="px-3 py-2">p_top3</th>
                            <th class="px-3 py-2">着順</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr v-for="entry in race.entries" :key="entry.lane" class="border-t border-slate-100">
                            <td class="px-3 py-2 font-medium">{{ entry.lane }}</td>
                            <td class="px-3 py-2">{{ entry.racer_name }}</td>
                            <td class="px-3 py-2">{{ entry.racer_class ?? '-' }}</td>
                            <td class="px-3 py-2">{{ entry.age ?? '-' }}</td>
                            <td class="px-3 py-2">{{ entry.motor_win_rate_2 ?? '-' }}</td>
                            <td class="px-3 py-2">{{ entry.boat_win_rate_2 ?? '-' }}</td>
                            <td class="px-3 py-2">{{ entry.exhibition_time ?? '-' }}</td>
                            <td class="px-3 py-2">{{ formatPercent(predictionFor(entry.lane)?.p_first) }}</td>
                            <td class="px-3 py-2">{{ formatPercent(predictionFor(entry.lane)?.p_top2) }}</td>
                            <td class="px-3 py-2">{{ formatPercent(predictionFor(entry.lane)?.p_top3) }}</td>
                            <td class="px-3 py-2">
                                <span v-if="entry.result?.finish_pos">{{ entry.result.finish_pos }}着</span>
                                <span v-else-if="entry.result?.abnormal_code">{{ entry.result.abnormal_code }}</span>
                                <span v-else class="text-slate-400">-</span>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <template v-if="race.prediction">
                <h2 class="mb-2 text-sm font-semibold text-slate-500">
                    買い目（3連単、モデル: {{ race.prediction.model_version }}）
                </h2>
                <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
                    <table class="w-full text-sm">
                        <thead class="bg-slate-50 text-left text-xs text-slate-500">
                            <tr>
                                <th class="px-3 py-2">順位</th>
                                <th class="px-3 py-2">組番</th>
                                <th class="px-3 py-2">推定確率</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr v-for="ticket in race.prediction.tickets" :key="ticket.combination" class="border-t border-slate-100">
                                <td class="px-3 py-2">{{ ticket.rank }}</td>
                                <td class="px-3 py-2 font-medium">{{ ticket.combination }}</td>
                                <td class="px-3 py-2">{{ formatPercent(ticket.est_prob, 2) }}</td>
                            </tr>
                            <tr v-if="race.prediction.tickets.length === 0">
                                <td class="px-3 py-2 text-slate-400" colspan="3">買い目は未生成です。</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </template>
            <p v-else class="text-slate-500">このレースの予測はまだ生成されていません。</p>
        </div>
    </div>
</template>
