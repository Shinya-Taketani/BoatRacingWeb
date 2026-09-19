const BASE_URL = '/api';

async function request(path) {
    const response = await fetch(`${BASE_URL}${path}`, {
        headers: { Accept: 'application/json' },
    });

    if (!response.ok) {
        throw new Error(`${path} failed: HTTP ${response.status}`);
    }

    return response.json();
}

export function fetchRacesToday() {
    return request('/races/today').then((body) => body.data);
}

export function fetchRace(id) {
    return request(`/races/${id}`).then((body) => body.data);
}

export function fetchPerformance() {
    return request('/performance');
}
