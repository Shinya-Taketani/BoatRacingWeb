<script setup>
defineProps({
    grade: {
        type: String,
        default: null,
    },
});

const colors = {
    S: 'bg-emerald-100 text-emerald-800',
    A: 'bg-sky-100 text-sky-800',
    B: 'bg-amber-100 text-amber-800',
    見送り: 'bg-slate-200 text-slate-600',
};
</script>

<template>
    <span
        v-if="grade"
        class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium"
        :class="colors[grade] ?? 'bg-slate-200 text-slate-600'"
    >
        {{ grade }}
    </span>
    <span v-else class="text-xs text-slate-400">未予測</span>
</template>
