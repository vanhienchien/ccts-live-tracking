// ==========================================
// CCTS Live Map - frontend logic
// ==========================================

const map = L.map('map').setView([12.25, 108.5], 6.3);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
}).addTo(map);

const stationLayer = L.layerGroup().addTo(map);
const staffMarkers = {}; // username -> { marker, wrenchMarker }

// ---------- Icon trạm sạc: ghim giọt nước cổ điển (giống Folium mặc định) ----------
// Vẽ bằng SVG nội tuyến (không phụ thuộc plugin/font ngoài) để đảm bảo luôn
// hiển thị đúng, không bị "vỡ" icon nếu 1 CDN nào đó chậm/lỗi.
function stationIcon(color) {
    const colorMap = { darkred: '#8b0000', orange: '#e67e22', green: '#2ca02c' };
    const fill = colorMap[color] || '#3498db';
    const svg = `
        <svg width="30" height="42" viewBox="0 0 30 42" xmlns="http://www.w3.org/2000/svg">
            <path d="M15 0C6.7 0 0 6.7 0 15c0 11.25 15 27 15 27s15-15.75 15-27C30 6.7 23.3 0 15 0z"
                  fill="${fill}" stroke="rgba(0,0,0,.35)" stroke-width="1"/>
            <circle cx="15" cy="15" r="9.5" fill="#ffffff"/>
            <text x="15" y="19.5" font-size="13" font-weight="700" text-anchor="middle"
                  font-family="Georgia, serif" fill="${fill}">i</text>
        </svg>`;
    return L.divIcon({
        className: '',
        html: svg,
        iconSize: [30, 42],
        iconAnchor: [15, 42],
        popupAnchor: [0, -38],
    });
}

function renderStations(stations) {
    stationLayer.clearLayers();
    (stations || []).forEach((s) => {
        const marker = L.marker([s.lat, s.lng], { icon: stationIcon(s.color) });
        marker.bindPopup(s.popup_html, { maxWidth: 320, maxHeight: 340 });
        marker.addTo(stationLayer);
    });
}

// ---------- Icon nhân sự (hình tròn + chữ viết tắt) ----------
function initials(name) {
    const parts = (name || '').trim().split(/\s+/).filter(Boolean);
    if (parts.length === 0) return '?';
    if (parts.length === 1) return parts[0].substring(0, 2).toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

const ROLE_COLORS = {
    'kỹ thuật': '#1f77b4',
    'điều phối khu vực': '#2ca02c',
    'điều hành': '#e67e22',
    'giám đốc': '#8e44ad',
    'admin': '#2c3e50',
};

function staffIcon(name, role) {
    const color = ROLE_COLORS[(role || '').trim().toLowerCase()] || '#7f8c8d';
    return L.divIcon({
        className: '',
        html: `<div class="staff-avatar" style="width:30px;height:30px;background:${color};">${initials(name)}</div>`,
        iconSize: [30, 30],
        iconAnchor: [15, 15],
    });
}

function wrenchIcon() {
    return L.divIcon({
        className: '',
        html: `<div class="wrench-badge" style="width:24px;height:24px;">🔧</div>`,
        iconSize: [24, 24],
        iconAnchor: [12, 12],
    });
}

function buildStaffPopup(loc) {
    const gmapUrl = `https://www.google.com/maps?q=${loc.lat},${loc.lng}`;
    let workingNote = '';
    if (loc.nearby_station) {
        workingNote = `<div style="margin-top:4px;padding:4px 6px;background:#fff3cd;border-radius:4px;">
            🔧 Đang tại trạm <b>${loc.nearby_station}</b> (~${loc.nearby_distance}m)</div>`;
    }
    return `
    <div style="font-family:Arial;font-size:12px;min-width:200px;">
        <b>${loc.full_name}</b><br>
        ${loc.role || ''}${loc.region ? ' · ' + loc.region : ''}<br>
        <a href="${gmapUrl}" target="_blank" rel="noopener noreferrer">Xem trên Google Maps 🗺️</a>
        ${workingNote}
    </div>`;
}

// Di chuyển marker mượt bằng nội suy tuyến tính (giống hiệu ứng "lướt" của Google Maps)
function animateMarkerTo(marker, newLatLng, durationMs = 1200) {
    const start = marker.getLatLng();
    const startTime = performance.now();

    function step(now) {
        const t = Math.min(1, (now - startTime) / durationMs);
        const lat = start.lat + (newLatLng.lat - start.lat) * t;
        const lng = start.lng + (newLatLng.lng - start.lng) * t;
        marker.setLatLng([lat, lng]);
        if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
}

function upsertStaffMarker(loc) {
    if (loc.lat == null || loc.lng == null) return;
    const latlng = L.latLng(loc.lat, loc.lng);
    let entry = staffMarkers[loc.username];

    if (!entry) {
        const marker = L.marker(latlng, { icon: staffIcon(loc.full_name, loc.role), zIndexOffset: 500 }).addTo(map);
        entry = { marker, wrenchMarker: null };
        staffMarkers[loc.username] = entry;
    } else {
        animateMarkerTo(entry.marker, latlng, 1200);
    }

    entry.marker.bindPopup(buildStaffPopup(loc));

    if (loc.nearby_station) {
        const wrenchLatLng = L.latLng(loc.lat + 0.00015, loc.lng + 0.00015);
        if (!entry.wrenchMarker) {
            entry.wrenchMarker = L.marker(wrenchLatLng, { icon: wrenchIcon(), zIndexOffset: 600 }).addTo(map);
        } else {
            animateMarkerTo(entry.wrenchMarker, wrenchLatLng, 1200);
        }
        entry.wrenchMarker.bindTooltip(`🔧 ${loc.full_name} đang sửa trạm ${loc.nearby_station}`);
    } else if (entry.wrenchMarker) {
        map.removeLayer(entry.wrenchMarker);
        entry.wrenchMarker = null;
    }
}

// ---------- WebSocket ----------
let ws;
let reconnectDelay = 2000;

function setConnStatus(ok, text) {
    const dot = document.getElementById('conn-dot');
    const label = document.getElementById('conn-text');
    if (dot) dot.classList.toggle('ok', ok);
    if (label) label.textContent = text;
}

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${protocol}://${window.location.host}/ws/location`);

    ws.onopen = () => {
        setConnStatus(true, 'Đã kết nối');
        reconnectDelay = 2000;
    };

    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'snapshot') {
            (msg.locations || []).forEach(upsertStaffMarker);
        } else if (msg.type === 'location_update') {
            upsertStaffMarker(msg);
        } else if (msg.type === 'stations_update') {
            renderStations(msg.stations);
            const el = document.getElementById('ticket-count');
            if (el) el.textContent = msg.total_tickets ?? 0;
        }
    };

    ws.onclose = () => {
        setConnStatus(false, 'Mất kết nối - đang thử lại...');
        setTimeout(connectWebSocket, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.5, 15000);
    };

    ws.onerror = () => {
        ws.close();
    };
}
connectWebSocket();

// Tải dữ liệu trạm ban đầu qua REST (không cần đợi WebSocket kết nối xong)
fetch('/api/stations')
    .then((r) => r.json())
    .then((data) => {
        renderStations(data.stations);
        const el = document.getElementById('ticket-count');
        if (el) el.textContent = data.total_tickets ?? 0;
    })
    .catch(() => {});

// ---------- Theo dõi vị trí liên tục (không cần bấm nút, mượt như Google Maps) ----------
let lastSentAt = 0;
const MIN_SEND_INTERVAL_MS = 3000; // gửi tối đa 1 lần / 3 giây - đủ mượt, không dội mạng/server

if (navigator.geolocation) {
    navigator.geolocation.watchPosition(
        (pos) => {
            const now = Date.now();
            if (now - lastSentAt < MIN_SEND_INTERVAL_MS) return;
            lastSentAt = now;
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({
                    type: 'location',
                    lat: pos.coords.latitude,
                    lng: pos.coords.longitude,
                    accuracy: pos.coords.accuracy,
                }));
            }
        },
        (err) => {
            console.warn('Không lấy được vị trí:', err.message);
        },
        { enableHighAccuracy: true, maximumAge: 2000, timeout: 20000 }
    );
} else {
    console.warn('Trình duyệt không hỗ trợ định vị.');
}