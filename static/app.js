// ==========================================
// CCTS Live Map - frontend logic
// ==========================================

const CURRENT_USERNAME = (document.body.dataset.username || '').trim();

const map = L.map('map').setView([12.25, 108.5], 6.3);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
}).addTo(map);

const stationLayer = L.layerGroup().addTo(map);
const staffMarkers = {}; // username -> { marker, wrenchMarker }

let allStations = [];          // cache toàn bộ trạm nhận được gần nhất (đã lọc theo quyền ở server)
let selectedTechs = new Set(); // tên kỹ thuật viên đang được TICK CHỌN trong tag lọc (rỗng = hiện tất cả)
let onlineUsernames = new Set(); // username (chữ thường) đang online

// ---------- Icon trạm sạc: ghim giọt nước cổ điển (giống Folium mặc định) ----------
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
async function triggerManualRefresh() {
    const btn = document.getElementById('btn-manual-refresh');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '⏳ Đang cào dữ liệu...';
    }
    try {
        const res = await fetch('/api/admin/refresh-stations', { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            alert('✅ ' + data.message);
        } else {
            alert('❌ ' + (data.error || 'Lỗi cập nhật.'));
        }
    } catch (err) {
        alert('❌ Không thể kết nối tới server.');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = '🔄';
        }
    }
}
// Vẽ marker trạm dựa trên allStations + bộ lọc kỹ thuật viên hiện tại (selectedTechs)
function applyStationFilter() {
    stationLayer.clearLayers();
    const stations = selectedTechs.size === 0
        ? allStations
        : allStations.filter((s) => selectedTechs.has(s.tech_name || 'Unassigned'));

    stations.forEach((s) => {
        const marker = L.marker([s.lat, s.lng], { icon: stationIcon(s.color) });
        marker.bindPopup(s.popup_html, { maxWidth: 320, maxHeight: 340 });
        marker._stationCode = s.station_code;
        marker.addTo(stationLayer);
    });
}

function renderStations(stations) {
    allStations = stations || [];
    applyStationFilter();
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
        html: `<div class="wrench-pure-icon">🔧</div>`,
        iconSize: [24, 24],
        iconAnchor: [12, 12],
    });
}

function formatDuration(sinceEpochSeconds) {
    const elapsed = Math.max(0, Date.now() / 1000 - sinceEpochSeconds);
    const totalMin = Math.floor(elapsed / 60);
    if (totalMin < 60) {
        const sec = Math.floor(elapsed % 60);
        return `${totalMin} phút ${sec.toString().padStart(2, '0')} giây`;
    }
    const hours = Math.floor(totalMin / 60);
    const mins = totalMin % 60;
    return `${hours} giờ ${mins} phút`;
}

function buildStaffPopup(loc) {
    const gmapUrl = `https://www.google.com/maps?q=${loc.lat},${loc.lng}`;
    let workingNote = '';
    if (loc.nearby_station) {
        const durationText = loc.nearby_since ? formatDuration(loc.nearby_since) : '';
        workingNote = `<div style="margin-top:4px;padding:4px 6px;background:#fff3cd;border-radius:4px;">
            🔧 Đang sửa trạm <b>${loc.nearby_station}</b>${durationText ? ` — đã ${durationText}` : ''}</div>`;
    }
    return `
    <div style="font-family:Arial;font-size:12px;min-width:200px;">
        <b>${loc.full_name}</b><br>
        ${loc.role || ''}${loc.region ? ' · ' + loc.region : ''}<br>
        <a href="${gmapUrl}" target="_blank" rel="noopener noreferrer">Xem trên Google Maps</a>
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
        const durationText = loc.nearby_since ? ` (đã ${formatDuration(loc.nearby_since)})` : '';
        entry.wrenchMarker.bindTooltip(`🔧 ${loc.full_name} đang sửa trạm ${loc.nearby_station}${durationText}`);
    } else if (entry.wrenchMarker) {
        map.removeLayer(entry.wrenchMarker);
        entry.wrenchMarker = null;
    }
}

// ---------- Tag lọc theo kỹ thuật viên (gom theo khu vực + chấm online/offline) ----------
const techPanel = document.getElementById('tech-filter-panel');
const techFilterBtn = document.getElementById('tech-filter-btn');

// Panel này nằm ĐÈ LÊN bản đồ Leaflet, nên mặc định cuộn chuột (wheel) bên
// trong nó sẽ bị Leaflet bắt và zoom bản đồ thay vì cuộn danh sách. Đây là
// API chính thức của Leaflet dành riêng cho việc này (control/popup nổi trên
// bản đồ cần tự cuộn nội bộ).
if (techPanel) {
    L.DomEvent.disableScrollPropagation(techPanel);
    L.DomEvent.disableClickPropagation(techPanel);
}

function techDotHtml(online) {
    if (online === true) return '<span class="conn-dot ok" title="Đang online"></span>';
    if (online === false) return '<span class="conn-dot" title="Đang offline"></span>';
    return '<span class="conn-dot" style="background:#bbb;" title="Không xác định"></span>';
}

function renderTechPanel(data) {
    const regions = data.regions || {};
    let html = '';
    Object.keys(regions).sort().forEach((region) => {
        html += `<div class="region-group"><div class="region-title">📍 ${region}</div>`;
        regions[region].forEach((item) => {
            const uname = (item.username || '').toLowerCase();
            html += `
            <label data-tech="${item.tech_name}" ${uname ? `data-username="${uname}"` : ''}>
                <input type="checkbox" class="tech-checkbox" value="${item.tech_name}">
                ${techDotHtml(item.online)} ${item.tech_name}
            </label>`;
        });
        html += `</div>`;
    });

    // "Unassigned" chỉ 1 mục duy nhất, dùng chung cho toàn bộ khu vực (không lặp lại)
    if (data.unassigned) {
        html += `<div class="region-group"><div class="region-title">🆕 Khác</div>
            <label data-tech="Unassigned">
                <input type="checkbox" class="tech-checkbox" value="Unassigned">
                ${techDotHtml(null)} Unassigned
            </label>
        </div>`;
    }

    html += `
    <div id="tech-filter-actions">
        <button id="tech-select-all">Chọn tất cả</button>
        <button id="tech-clear-all">Bỏ chọn</button>
    </div>`;
    techPanel.innerHTML = html;

    techPanel.querySelectorAll('.tech-checkbox').forEach((cb) => {
        cb.checked = selectedTechs.has(cb.value);
        cb.addEventListener('change', () => {
            if (cb.checked) selectedTechs.add(cb.value);
            else selectedTechs.delete(cb.value);
            applyStationFilter();
        });
    });

    const selectAllBtn = document.getElementById('tech-select-all');
    const clearAllBtn = document.getElementById('tech-clear-all');
    if (selectAllBtn) {
        selectAllBtn.addEventListener('click', () => {
            techPanel.querySelectorAll('.tech-checkbox').forEach((cb) => {
                cb.checked = true;
                selectedTechs.add(cb.value);
            });
            applyStationFilter();
        });
    }
    if (clearAllBtn) {
        clearAllBtn.addEventListener('click', () => {
            techPanel.querySelectorAll('.tech-checkbox').forEach((cb) => { cb.checked = false; });
            selectedTechs.clear();
            applyStationFilter();
        });
    }

    updateTechPanelPresence();
}

function updateTechPanelPresence() {
    if (!techPanel) return;
    techPanel.querySelectorAll('label[data-username]').forEach((label) => {
        const uname = label.dataset.username;
        const dot = label.querySelector('.conn-dot');
        if (!dot) return;
        const online = onlineUsernames.has(uname);
        dot.classList.toggle('ok', online);
        dot.style.background = online ? '' : '#e74c3c';
        dot.title = online ? 'Đang online' : 'Đang offline';
    });
}

function loadTechnicianPanel() {
    fetch('/api/technicians')
        .then((r) => r.json())
        .then(renderTechPanel)
        .catch(() => {
            techPanel.innerHTML = '<div style="padding:10px;font-size:13px;color:#999;">Không tải được danh sách kỹ thuật viên.</div>';
        });
}

if (techFilterBtn) {
    techFilterBtn.addEventListener('click', () => {
        const isOpen = techPanel.style.display === 'block';
        techPanel.style.display = isOpen ? 'none' : 'block';
        if (!isOpen && !techPanel.dataset.loaded) {
            techPanel.dataset.loaded = '1';
            loadTechnicianPanel();
        }
    });
}

// ---------- Tìm kiếm trạm / kỹ thuật viên ----------
const searchInput = document.getElementById('search-input');
const searchResults = document.getElementById('search-results');

function closeSearchResults() {
    searchResults.style.display = 'none';
    searchResults.innerHTML = '';
}

function flyToStation(stationCode) {
    const s = allStations.find((x) => x.station_code === stationCode);
    if (!s) return;
    map.flyTo([s.lat, s.lng], 16, { duration: 1 });
    setTimeout(() => {
        stationLayer.eachLayer((layer) => {
            if (layer._stationCode === stationCode) layer.openPopup();
        });
    }, 350);
}

function flyToStaff(username) {
    const entry = staffMarkers[username];
    if (!entry) {
        alert('Kỹ thuật viên này hiện chưa có vị trí trên bản đồ (có thể đang offline).');
        return;
    }
    map.flyTo(entry.marker.getLatLng(), 16, { duration: 1 });
    setTimeout(() => entry.marker.openPopup(), 350);
}

function runSearch(keyword) {
    const q = keyword.trim().toLowerCase();
    if (!q) { closeSearchResults(); return; }

    const stationMatches = allStations
        .filter((s) => (s.station_code || '').toLowerCase().includes(q))
        .slice(0, 8)
        .map((s) => ({ type: 'station', label: s.station_code, sub: s.tech_name || 'Unassigned', value: s.station_code }));

    const techNames = new Set(allStations.map((s) => s.tech_name).filter(Boolean));

    const techMatches = [...techNames]
        .filter((name) => name.toLowerCase().includes(q))
        .slice(0, 8)
        .map((name) => ({ type: 'tech', label: name, sub: 'Kỹ thuật viên', value: name }));

    const results = [...stationMatches, ...techMatches];

    if (results.length === 0) {
        searchResults.innerHTML = '<div class="empty">Không tìm thấy kết quả</div>';
    } else {
        searchResults.innerHTML = results.map((r) => `
            <div class="item" data-type="${r.type}" data-value="${r.value}">
                ${r.type === 'station' ? '⚡' : '🧑‍🔧'} <b>${r.label}</b>
                <div class="tag">${r.sub}</div>
            </div>
        `).join('');
    }
    searchResults.style.display = 'block';
}

if (searchInput) {
    searchInput.addEventListener('input', (e) => runSearch(e.target.value));
    searchInput.addEventListener('focus', (e) => { if (e.target.value) runSearch(e.target.value); });
}

if (searchResults) {
    searchResults.addEventListener('click', (e) => {
        const item = e.target.closest('.item');
        if (!item) return;
        const { type, value } = item.dataset;
        if (type === 'station') {
            flyToStation(value);
        } else if (type === 'tech') {
            // Tìm 1 kỹ thuật đang online có tên trùng khớp để bay tới vị trí của họ
            const match = Object.entries(staffMarkers).find(([uname, entry]) => {
                const popupContent = entry.marker.getPopup()?.getContent() || '';
                return popupContent.includes(value);
            });
            if (match) {
                flyToStaff(match[0]);
            } else {
                alert(`"${value}" hiện không có vị trí online trên bản đồ. Đang hiện các trạm họ phụ trách.`);
                selectedTechs = new Set([value]);
                applyStationFilter();
                if (techPanel) {
                    techPanel.style.display = 'block';
                    if (!techPanel.dataset.loaded) { techPanel.dataset.loaded = '1'; loadTechnicianPanel(); }
                }
            }
        }
        closeSearchResults();
        searchInput.value = '';
    });
}

document.addEventListener('click', (e) => {
    if (searchResults && !searchResults.contains(e.target) && e.target !== searchInput) {
        closeSearchResults();
    }
});

// ---------- Nút "Vị trí của bạn" ----------
const myLocationBtn = document.getElementById('my-location-btn');
if (myLocationBtn) {
    myLocationBtn.addEventListener('click', () => {
        const entry = staffMarkers[CURRENT_USERNAME];
        if (entry) {
            map.flyTo(entry.marker.getLatLng(), 17, { duration: 1 });
        } else {
            alert('Chưa xác định được vị trí của bạn. Hãy đảm bảo đã cấp quyền định vị cho trình duyệt và đợi vài giây.');
        }
    });
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
        } else if (msg.type === 'presence_update') {
            onlineUsernames = new Set((msg.online_usernames || []).map((u) => u.toLowerCase()));
            updateTechPanelPresence();
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