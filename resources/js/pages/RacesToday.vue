<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { fetchRacesToday } from '../api';
import { formatPercent, formatTime } from '../format';
import ConfidenceBadge from '../components/ConfidenceBadge.vue';
import BeforeInfoBadge from '../components/BeforeInfoBadge.vue';

const races = ref([]);
const loading = ref(true);
const error = ref(null);
const now = ref(new Date());
const showAll = ref(false);

let timer = null;

onMounted(async () => {
    timer = setInterval(() => {
        now.value = new Date();
    }, 30000);

    try {
        races.value = await fetchRacesToday();
    } catch (e) {
        error.value = e.message;
    } finally {
        loading.value = false;
    }
});

onUnmounted(() => {
    if (timer) clearInterval(timer);
});

function isPast(race) {
    return new Date(race.deadline_at) <= now.value;
}

function topLaneProb(race) {
    if (race.top_lane == null || !race.predicted_probabilities) return null;
    return race.predicted_probabilities[String(race.top_lane)];
}

const sortedByDeadline = computed(() =>
    [...races.value].sort((a, b) => new Date(a.deadline_at) - new Date(b.deadline_at))
);

// 3着内確実な艇: p_top3がconfig('ml.top3_confident_threshold')(現在84%、
// is_top3直接学習モデル由来。2026-10-04切替)以上の艇。
// 締切が近い順に最大10件。
const confidentTop3Races = computed(() =>
    sortedByDeadline.value.filter((r) => r.confident_top3_lane !== null && !isPast(r)).slice(0, 10)
);

// A. 堅いレース: 1号艇本命 かつ confidence_grade=S（手堅く当てたい人向け）
const safeRaces = computed(() =>
    sortedByDeadline.value
        .filter((r) => r.confidence_grade === 'S' && r.top_lane === 1 && !isPast(r))
        .slice(0, 10)
);

// B. 妙味のあるレース: 1号艇以外が本命、かつその確率が高い（モデルの判断が際立つレース）
const upsetRaces = computed(() =>
    sortedByDeadline.value.filter((r) => r.is_upset_pick === true && !isPast(r)).slice(0, 10)
);

// 妙味のあるレース(is_upset_pick)と重複する場合はそちらを優先して表示するため、
// このセクションからは除外する（1号艇が弱く、かつ明確な対抗本命も無いレースのみ）。
const lane1RiskRaces = computed(() =>
    sortedByDeadline.value
        .filter((r) => r.lane1_risk === true && r.is_upset_pick !== true && !isPast(r))
        // 1号艇の確率が低い(=危険度が高い)順。件数が多い場合に備え最大10件まで。
        .sort((a, b) => (a.predicted_probabilities?.['1'] ?? 0) - (b.predicted_probabilities?.['1'] ?? 0))
        .slice(0, 10)
);

const listRaces = computed(() =>
    showAll.value ? sortedByDeadline.value : sortedByDeadline.value.filter((r) => !isPast(r))
);

const groupedListByStadium = computed(() => {
    const groups = new Map();
    for (const race of listRaces.value) {
        if (!groups.has(race.stadium_name)) {
            groups.set(race.stadium_name, []);
        }
        groups.get(race.stadium_name).push(race);
    }
    return groups;
});
</script>

<template>
    <div>
        <p v-if="loading" class="text-slate-500">読み込み中...</p>
        <p v-else-if="error" class="text-red-600">読み込みに失敗しました: {{ error }}</p>

        <div v-else class="space-y-10">
            <!-- 0. 3着内確実な艇 -->
            <section v-if="confidentTop3Races.length > 0">
                <h2 class="mb-1 text-lg font-semibold">3着内確実な艇</h2>
                <p class="mb-3 text-xs text-slate-500">
                    3着以内に入る確率が84%以上の艇。検証期間で実績90.4%（22,303艇中20,154艇的中）。
                </p>
                <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                    <div
                        v-for="race in confidentTop3Races"
                        :key="race.id"
                        class="cursor-pointer rounded-xl border-2 border-sky-200 bg-sky-50 p-4 transition hover:border-sky-400"
                        @click="$router.push(`/races/${race.id}`)"
                    >
                        <div class="flex items-center justify-between">
                            <span class="font-semibold text-slate-800">{{ race.stadium_name }} {{ race.race_no }}R</span>
                            <div class="flex items-center gap-1.5">
                                <BeforeInfoBadge v-if="race.uses_before_info" compact />
                                <span class="text-xs text-slate-500">締切 {{ formatTime(race.deadline_at) }}</span>
                            </div>
                        </div>
                        <p class="mt-3 text-2xl font-bold text-sky-700">
                            {{ race.confident_top3_lane }}号艇
                            <span class="text-base font-normal text-sky-600">{{ formatPercent(race.confident_top3_prob) }}</span>
                        </p>
                        <p class="text-xs text-slate-500">3着以内</p>
                    </div>
                </div>
            </section>

            <!-- 1B. 妙味のあるレース（モデルの判断が際立つレース） -->
            <section v-if="upsetRaces.length > 0">
                <h2 class="mb-1 text-lg font-semibold">妙味のあるレース</h2>
                <p class="mb-3 text-xs text-slate-500">
                    モデルの判断が際立つレース（1号艇以外が本命、かつ確率40%以上）。このレース群では1号艇の的中率が21.8%まで下がる一方、モデルの本命は49.3%的中しています（検証期間1,864レース実測）。
                </p>
                <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                    <div
                        v-for="race in upsetRaces"
                        :key="race.id"
                        class="cursor-pointer rounded-xl border-2 border-amber-200 bg-amber-50 p-4 transition hover:border-amber-400"
                        @click="$router.push(`/races/${race.id}`)"
                    >
                        <div class="flex items-center justify-between">
                            <span class="font-semibold text-slate-800">{{ race.stadium_name }} {{ race.race_no }}R</span>
                            <div class="flex items-center gap-1.5">
                                <BeforeInfoBadge v-if="race.uses_before_info" compact />
                                <ConfidenceBadge :grade="race.confidence_grade" />
                            </div>
                        </div>
                        <p class="mt-1 text-sm text-slate-500">締切 {{ formatTime(race.deadline_at) }}</p>
                        <p class="mt-3 text-2xl font-bold text-amber-700">
                            {{ race.top_lane }}号艇
                            <span class="text-base font-normal text-amber-600">{{ formatPercent(topLaneProb(race)) }}</span>
                        </p>
                    </div>
                </div>
            </section>

            <!-- 1A. 堅いレース（手堅く当てたい人向け） -->
            <section v-if="safeRaces.length > 0">
                <h2 class="mb-1 text-lg font-semibold">堅いレース</h2>
                <p class="mb-3 text-xs text-slate-500">
                    手堅く当てたい人向け（1号艇本命、confidence S）。このレース群ではモデルの本命が76.1%的中しています（検証期間10,096レース実測）。
                </p>
                <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                    <div
                        v-for="race in safeRaces"
                        :key="race.id"
                        class="cursor-pointer rounded-xl border-2 border-emerald-200 bg-emerald-50 p-4 transition hover:border-emerald-400"
                        @click="$router.push(`/races/${race.id}`)"
                    >
                        <div class="flex items-center justify-between">
                            <span class="font-semibold text-slate-800">{{ race.stadium_name }} {{ race.race_no }}R</span>
                            <div class="flex items-center gap-1.5">
                                <BeforeInfoBadge v-if="race.uses_before_info" compact />
                                <ConfidenceBadge :grade="race.confidence_grade" />
                            </div>
                        </div>
                        <p class="mt-1 text-sm text-slate-500">締切 {{ formatTime(race.deadline_at) }}</p>
                        <p class="mt-3 text-2xl font-bold text-emerald-700">
                            {{ race.top_lane }}号艇
                            <span class="text-base font-normal text-emerald-600">{{ formatPercent(topLaneProb(race)) }}</span>
                        </p>
                    </div>
                </div>
            </section>

            <!-- 2. 1号艇が危険なレース -->
            <section v-if="lane1RiskRaces.length > 0">
                <h2 class="mb-1 text-lg font-semibold">1号艇が危険なレース</h2>
                <p class="mb-3 text-xs text-slate-500">
                    モデルが1号艇以外を推奨したレースでは、1号艇の的中率が実測54.95%→22.35%まで低下することを検証済みです。
                </p>
                <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                    <div
                        v-for="race in lane1RiskRaces"
                        :key="race.id"
                        class="cursor-pointer rounded-xl border-2 border-rose-200 bg-rose-50 p-4 transition hover:border-rose-400"
                        @click="$router.push(`/races/${race.id}`)"
                    >
                        <div class="flex items-center justify-between">
                            <span class="font-semibold text-slate-800">{{ race.stadium_name }} {{ race.race_no }}R</span>
                            <div class="flex items-center gap-1.5">
                                <BeforeInfoBadge v-if="race.uses_before_info" compact />
                                <span class="text-xs text-slate-500">締切 {{ formatTime(race.deadline_at) }}</span>
                            </div>
                        </div>
                        <div class="mt-3 flex items-baseline justify-between">
                            <div>
                                <div class="flex items-center gap-1.5">
                                    <p class="text-xs text-slate-500">1号艇</p>
                                    <span
                                        v-if="race.lane1_risk_level"
                                        class="inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold"
                                        :class="race.lane1_risk_level === '高' ? 'bg-rose-600 text-white' : 'bg-rose-200 text-rose-800'"
                                    >
                                        危険度{{ race.lane1_risk_level }}
                                    </span>
                                </div>
                                <p class="text-xl font-bold text-rose-700">
                                    {{ formatPercent(race.predicted_probabilities?.['1']) }}
                                </p>
                            </div>
                            <div class="text-right">
                                <p class="text-xs text-slate-500">モデルの本命</p>
                                <p class="text-xl font-bold text-slate-800">
                                    {{ race.top_lane }}号艇
                                    <span class="text-sm font-normal text-slate-500">{{ formatPercent(topLaneProb(race)) }}</span>
                                </p>
                            </div>
                        </div>
                    </div>
                </div>
            </section>

            <!-- 3. 全レース一覧 -->
            <section>
                <div class="mb-3 flex items-center justify-between">
                    <h2 class="text-lg font-semibold">全レース一覧</h2>
                    <label class="flex items-center gap-2 text-sm text-slate-600">
                        <input v-model="showAll" type="checkbox" class="rounded border-slate-300" />
                        締切を過ぎたレースも表示
                    </label>
                </div>

                <p v-if="listRaces.length === 0" class="text-slate-500">該当するレースはありません。</p>

                <div v-else class="space-y-6">
                    <section v-for="[stadiumName, stadiumRaces] in groupedListByStadium" :key="stadiumName">
                        <h3 class="mb-2 text-sm font-semibold text-slate-500">{{ stadiumName }}</h3>
                        <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
                            <table class="w-full text-sm">
                                <thead class="bg-slate-50 text-left text-xs text-slate-500">
                                    <tr>
                                        <th class="px-3 py-2">R</th>
                                        <th class="px-3 py-2">締切</th>
                                        <th class="px-3 py-2">本命</th>
                                        <th class="px-3 py-2">確信度</th>
                                        <th class="px-3 py-2">1号艇</th>
                                        <th class="px-3 py-2">結果</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr
                                        v-for="race in stadiumRaces"
                                        :key="race.id"
                                        class="cursor-pointer border-t border-slate-100 hover:bg-slate-50"
                                        :class="isPast(race) ? 'bg-slate-50/60 text-slate-400' : ''"
                                        @click="$router.push(`/races/${race.id}`)"
                                    >
                                        <td class="px-3 py-2 font-medium">
                                            {{ race.race_no }}R
                                            <BeforeInfoBadge v-if="race.uses_before_info" compact class="ml-1 align-middle" />
                                        </td>
                                        <td class="px-3 py-2">{{ formatTime(race.deadline_at) }}</td>
                                        <td class="px-3 py-2">
                                            <span v-if="race.top_lane">
                                                {{ race.top_lane }}号艇
                                                <span :class="isPast(race) ? '' : 'text-slate-400'">
                                                    ({{ formatPercent(topLaneProb(race)) }})
                                                </span>
                                            </span>
                                            <span v-else>-</span>
                                        </td>
                                        <td class="px-3 py-2"><ConfidenceBadge :grade="race.confidence_grade" /></td>
                                        <td class="px-3 py-2">
                                            <span
                                                v-if="race.lane1_risk === true"
                                                class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium"
                                                :class="race.lane1_risk_level === '高' ? 'bg-rose-600 text-white' : 'bg-rose-100 text-rose-700'"
                                            >
                                                危険{{ race.lane1_risk_level ? `(${race.lane1_risk_level})` : '' }}
                                            </span>
                                            <span v-else>-</span>
                                        </td>
                                        <td class="px-3 py-2">
                                            <template v-if="race.result_available">
                                                <span v-if="race.actual_winner_lane">{{ race.actual_winner_lane }}号艇 1着</span>
                                                <span v-else>(1着なし)</span>
                                                <span v-if="race.predicted_hit === true" class="ml-1 text-emerald-600">✓的中</span>
                                                <span v-else-if="race.predicted_hit === false" class="ml-1">✕</span>
                                            </template>
                                            <span v-else>-</span>
                                        </td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </section>
                </div>
            </section>
        </div>
    </div>
</template>
