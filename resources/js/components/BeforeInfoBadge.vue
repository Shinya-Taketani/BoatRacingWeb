<script setup>
defineProps({
    compact: {
        type: Boolean,
        default: false,
    },
});
</script>

<template>
    <span
        class="inline-flex items-center gap-1 rounded-full bg-teal-50 px-2 py-0.5 text-[11px] font-medium text-teal-700"
        title="展示タイム・体重・気象等の直前情報を反映した最新の予測です"
    >
        <span class="h-1.5 w-1.5 rounded-full bg-teal-500"></span>
        <template v-if="!compact">直前情報反映</template>
    </span>
</template>
