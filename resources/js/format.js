export function formatTime(iso) {
    if (!iso) return '-';
    return new Intl.DateTimeFormat('ja-JP', {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
        timeZone: 'Asia/Tokyo',
    }).format(new Date(iso));
}

export function formatPercent(value, digits = 1) {
    if (value === null || value === undefined) return '-';
    return `${(value * 100).toFixed(digits)}%`;
}
