<script setup>
import { computed, onMounted, ref } from 'vue';
import { fetchRacesToday } from '../api';
import { formatPercent, formatTime } from '../format';
import ConfidenceBadge from '../components/ConfidenceBadge.vue';

const races = ref([]);
const loading = ref(true);
const error = ref(null);

onMounted(async () => {
    try {
        races.value = await fetchRacesToday();
    } catch (e) {
        error.value = e.message;
    } finally {
        loading.value = false;
    }
});

const groupedByStadium = computed(() => {
    const groups = new Map();
    for (const race of races.value) {
        if (!groups.has(race.stadium_name)) {
            groups.set(race.stadium_name, []);
        }
        groups.get(race.stadium_name).push(race);
    }
    return groups;
});

function topLane(race) {
    if (!race.predicted_probabilities) return null;
    return Object.entries(race.predicted_probabilities).reduce((best, [lane, p]) =>
        !best || p > best.p ? { lane, p } : best
    , null);
}
</script>

<template>
    <div>
        <h1 class="mb-4 text-xl font-semibold">今日のレース</h1>

        <p v-if="loading" class="text-slate-500">読み込み中...</p>
        <p v-else-if="error" class="text-red-600">読み込みに失敗しました: {{ error }}</p>
        <p v-else-if="races.length === 0" class="text-slate-500">本日のレースはありません。</p>

        <div v-else class="space-y-6">
            <section v-for="[stadiumName, stadiumRaces] in groupedByStadium" :key="stadiumName">
                <h2 class="mb-2 text-sm font-semibold text-slate-500">{{ stadiumName }}</h2>
                <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
                    <table class="w-full text-sm">
                        <thead class="bg-slate-50 text-left text-xs text-slate-500">
                            <tr>
                                <th class="px-3 py-2">R</th>
                                <th class="px-3 py-2">締切</th>
                                <th class="px-3 py-2">本命</th>
                                <th class="px-3 py-2">確信度</th>
                                <th class="px-3 py-2">1号艇</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr
                                v-for="race in stadiumRaces"
                                :key="race.id"
                                class="cursor-pointer border-t border-slate-100 hover:bg-slate-50"
                                @click="$router.push(`/races/${race.id}`)"
                            >
                                <td class="px-3 py-2 font-medium">{{ race.race_no }}R</td>
                                <td class="px-3 py-2 text-slate-600">{{ formatTime(race.deadline_at) }}</td>
                                <td class="px-3 py-2">
                                    <span v-if="topLane(race)">
                                        {{ topLane(race).lane }}号艇
                                        <span class="text-slate-400">({{ formatPercent(topLane(race).p) }})</span>
                                    </span>
                                    <span v-else class="text-slate-400">-</span>
                                </td>
                                <td class="px-3 py-2"><ConfidenceBadge :grade="race.confidence_grade" /></td>
                                <td class="px-3 py-2">
                                    <span
                                        v-if="race.lane1_risk === true"
                                        class="inline-flex items-center rounded-full bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-700"
                                    >
                                        危険
                                    </span>
                                    <span v-else-if="race.lane1_risk === false" class="text-xs text-slate-400">-</span>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </section>
        </div>
    </div>
</template>
