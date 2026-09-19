import { createRouter, createWebHistory } from 'vue-router';
import RacesToday from './pages/RacesToday.vue';
import RaceDetail from './pages/RaceDetail.vue';
import Performance from './pages/Performance.vue';

export default createRouter({
    history: createWebHistory(),
    routes: [
        { path: '/', name: 'races.today', component: RacesToday },
        { path: '/races/:id', name: 'races.show', component: RaceDetail, props: true },
        { path: '/performance', name: 'performance', component: Performance },
    ],
});
