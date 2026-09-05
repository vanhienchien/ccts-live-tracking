// ==========================================
// Bản đồ tổng hợp TOÀN BỘ trạm sạc (EV + BSS) - /all-stations
// Khác với app.js (bản đồ live sự cố): không có khái niệm severity/overdue,
// chỉ hiện TOÀN BỘ trụ sạc đang có trong total_charges.xlsx, gom cụm theo
// khu vực khi zoom xa, tách marker riêng lẻ khi zoom gần.
// ==========================================

const map = L.map('map').setView([16.0, 107.5], 6);
L.tileLayer('https://{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', {
    subdomains: ['mt0', 'mt1', 'mt2', 'mt3'],
    attribution: '&copy; Google Maps',
    maxZoom: 20,
}).addTo(map);

const EV_COLOR = '#2563eb';
const BSS_COLOR = '#7c3aed';

// Icon giọt nước cùng phong cách với bản đồ sự cố (stationIcon trong
// app.js) - chỉ đổi màu + glyph để phân biệt loại trụ, không phải severity.
function chargeStationIcon(type) {
    const color = type === 'ev' ? EV_COLOR : BSS_COLOR;
    const glyph = type === 'ev' ? '⚡' : '🔋';
    const html = `
        <div style="position:relative;width:16px;height:22px;">
            <svg width="16" height="22" viewBox="0 0 30 42" xmlns="http://www.w3.org/2000/svg">
                <path d="M15 0C6.7 0 0 6.7 0 15c0 11.25 15 27 15 27s15-15.75 15-27C30 6.7 23.3 0 15 0z"
                      fill="${color}" stroke="rgba(0,0,0,.3)" stroke-width="1.2"/>
                <circle cx="15" cy="15" r="9" fill="#ffffff"/>
                <text x="15" y="19" font-size="11" text-anchor="middle"
                      font-family="Inter, system-ui, sans-serif">${glyph}</text>
            </svg>
        </div>`;
    return L.divIcon({
        className: '',
        html,
        iconSize: [16, 22],
        iconAnchor: [8, 22],
        popupAnchor: [0, -20],
    });
}

function escapeHtml(str) {
    return String(str ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function buildChargeStationPopup(s) {
    const addrParts = [s.name, s.district, s.province].filter(Boolean);
    const codesText = (s.station_codes || []).join(', ') || '—';
    return `
        <div class="charge-popup">
            <div class="cp-name">${escapeHtml(s.name || 'Trạm sạc')}</div>
            <div class="cp-addr">${escapeHtml(addrParts.slice(1).join(' · ') || '—')}</div>
            <div class="cp-counts">
                ${s.ev_count > 0 ? `<span class="cp-count-pill ev">⚡ ${s.ev_count} EV</span>` : ''}
                ${s.bss_count > 0 ? `<span class="cp-count-pill bss">🔋 ${s.bss_count} BSS</span>` : ''}
            </div>
            <div class="cp-codes">Mã trạm: ${escapeHtml(codesText)}</div>
        </div>`;
}

// Cụm marker: số hiển thị = TỔNG SỐ TRỤ SẠC (total_count cộng dồn từng
// trạm trong cụm), không phải số trạm - đúng yêu cầu "thể hiện tổng số
// lượng trụ sạc trong khu vực đó".
function buildChargeClusterIcon(cluster) {
    let total = 0;
    cluster.getAllChildMarkers().forEach((m) => {
        total += m._totalCount || 0;
    });
    if (total === 0) total = cluster.getChildCount();

    const size = total < 50 ? 40 : total < 300 ? 48 : 56;
    const fontSize = total < 50 ? 13 : total < 300 ? 14.5 : 16;
    const bg = total < 50 ? '#1e293b' : total < 300 ? '#334155' : '#0f172a';

    return L.divIcon({
        html: `
            <div style="width:${size}px;height:${size}px;border-radius:50%;background:${bg};
                        display:flex;align-items:center;justify-content:center;
                        color:#fff;font-weight:800;font-size:${fontSize}px;
                        border:3px solid #fff;box-shadow:0 3px 10px rgba(15,23,42,.4);
                        font-family:'Inter',system-ui,-apple-system,sans-serif;">${total}</div>`,
        className: 'charge-cluster-icon',
        iconSize: [size, size],
    });
}

const stationLayer = L.markerClusterGroup({
    maxClusterRadius: 60,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    disableClusteringAtZoom: 17,
    iconCreateFunction: buildChargeClusterIcon,
}).addTo(map);

async function loadAllStations() {
    const summaryBadge = document.getElementById('summary-badge');
    try {
        const res = await fetch('/api/all-charging-stations');
        const data = await res.json();
        if (!res.ok) {
            summaryBadge.textContent = data.error || 'Lỗi tải dữ liệu';
            return;
        }
        const markers = (data.stations || []).map((s) => {
            const marker = L.marker([s.lat, s.lng], { icon: chargeStationIcon(s.type) });
            marker.bindPopup(() => buildChargeStationPopup(s), { maxWidth: 300 });
            marker._totalCount = s.total_count || 0;
            return marker;
        });
        stationLayer.clearLayers();
        stationLayer.addLayers(markers);

        summaryBadge.textContent =
            `${data.total_stations} trạm · ${data.total_poles} trụ sạc`;
    } catch (err) {
        summaryBadge.textContent = 'Không thể kết nối tới server.';
    }
}

loadAllStations();
