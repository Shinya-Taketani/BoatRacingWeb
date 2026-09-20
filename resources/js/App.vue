<script setup>
</script>

<template>
    <div class="min-h-screen bg-slate-50 text-slate-900">
        <header class="border-b border-slate-200 bg-white">
            <div class="mx-auto flex max-w-5xl items-center gap-6 px-4 py-3">
                <span class="text-lg font-semibold">BoatRacingWeb</span>
                <nav class="flex gap-4 text-sm">
                    <router-link
                        to="/"
                        class="text-slate-600 hover:text-slate-900"
                        exact-active-class="font-semibold text-slate-900"
                    >
                        今日のレース
                    </router-link>
                    <router-link
                        to="/performance"
                        class="text-slate-600 hover:text-slate-900"
                        exact-active-class="font-semibold text-slate-900"
                    >
                        実績
                    </router-link>
                </nav>
            </div>
        </header>

        <main class="mx-auto max-w-5xl px-4 py-6">
            <router-view />
        </main>
    </div>
</template>
