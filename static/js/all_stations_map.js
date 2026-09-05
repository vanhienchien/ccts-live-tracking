// ==========================================
// Bản đồ tổng hợp TOÀN BỘ trạm sạc (EV + BSS) - /all-stations
// Khác với app.js (bản đồ live sự cố): không có khái niệm severity/overdue,
// chỉ hiện TOÀN BỘ trụ sạc đang có trong total_charges.xlsx, gom cụm theo
// khu vực khi zoom xa, tách marker riêng lẻ khi zoom gần (hoặc hiện tất cả
// không gom cụm nếu bật "chế độ hiện tất cả").
// ==========================================

// preferCanvas: chế độ "Hiện tất cả" vẽ hàng nghìn điểm cùng lúc bằng
// L.circleMarker (xem applyFilters) - cần bật canvas thì mới nhẹ, mặc định
// Leaflet vẽ circleMarker bằng SVG (1 phần tử DOM/điểm, rất chậm ở số lượng
// lớn). Marker dạng divIcon (chế độ gom cụm) không bị ảnh hưởng bởi cờ này.
const map = L.map('map', { preferCanvas: true }).setView([16.0, 107.5], 6);
L.tileLayer('https://{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', {
    subdomains: ['mt0', 'mt1', 'mt2', 'mt3'],
    attribution: '&copy; Google Maps',
    maxZoom: 20,
}).addTo(map);

// EV = đỏ, BSS = xanh dương - bão hoà cao + viền trắng dày để nổi bật trên
// mọi nền bản đồ (không trùng cam của lõi cụm).
const EV_COLOR = '#dc2626';
const BSS_COLOR = '#2563eb';
const CLUSTER_CORE_COLOR = '#f97316';

function escapeHtml(str) {
    return String(str ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

// ---------- Icon trạm: 2 HÌNH DẠNG khác nhau (không chỉ khác màu) ----------
function chargeStationIcon(type) {
    const isEv = type === 'ev';
    const color = isEv ? EV_COLOR : BSS_COLOR;
    const glyph = isEv
        ? `<path transform="translate(7,6) scale(0.7)"
                 d="M11.983 1.907a.75.75 0 00-1.292-.657l-8.5 9.5A.75.75 0 002.75 12h6.572l-1.305 6.093a.75.75 0 001.292.657l8.5-9.5A.75.75 0 0017.25 8h-6.572l1.305-6.093z"
                 fill="#ffffff"/>`
        : `<rect x="9" y="9" width="13" height="9" rx="2" fill="none" stroke="#ffffff" stroke-width="1.8"/>
           <rect x="22" y="11.5" width="2.4" height="4" rx="1" fill="#ffffff"/>
           <rect x="10.5" y="10.5" width="4.5" height="6" rx="1" fill="#ffffff"/>`;

    const shape = isEv
        ? `<path d="M15 0C6.7 0 0 6.7 0 15c0 11.25 15 27 15 27s15-15.75 15-27C30 6.7 23.3 0 15 0z"
                 fill="${color}" stroke="#ffffff" stroke-width="2"/>`
        : `<path d="M15 2 L24 2 Q28 2 28 6 L28 19 Q28 22.5 25 24.5 L15 40 L5 24.5 Q2 22.5 2 19 L2 6 Q2 2 6 2 Z"
                 fill="${color}" stroke="#ffffff" stroke-width="2"/>`;

    const html = `
        <div style="position:relative;width:14px;height:18px;
                    filter:drop-shadow(0 1px 3px rgba(0,0,0,.55));">
            <svg width="14" height="18" viewBox="0 0 30 42" xmlns="http://www.w3.org/2000/svg">
                ${shape}
                ${glyph}
            </svg>
        </div>`;
    return L.divIcon({
        className: '',
        html,
        iconSize: [14, 18],
        iconAnchor: [7, 18],
        popupAnchor: [0, -16],
    });
}

// ---------- Popup trạm: mã trạm lên đầu, đủ địa chỉ/KT/danh sách trụ ----------
// Bỏ hẳn nhãn chữ "Trạm sạc"/"Trụ đổi pin" - icon trên bản đồ đã thể hiện
// loại trụ rồi, nhắc lại bằng chữ là thừa. Header màu theo đúng màu icon
// (đỏ=EV ưu tiên hiển thị, xanh=BSS) để nhất quán giữa ghim và popup.
function buildChargeStationPopup(s) {
    const headerColor = s.type === 'ev' ? EV_COLOR : BSS_COLOR;
    const codeTitle = (s.station_codes || []).join(' / ') || 'Chưa có mã';
    const addrParts = [s.name, s.district, s.province].filter(Boolean);
    const techsText = (s.techs || []).join(', ') || 'Chưa phân công';
    const poles = s.poles || [];

    const polesHtml = poles.map((p) => {
        const isBss = p.is_bss;
        return `<div class="cp-pole ${isBss ? 'bss' : 'ev'}">${isBss ? '🔋' : '⚡'} <span class="sn">${escapeHtml(p.sn)}</span></div>`;
    }).join('');

    return `
        <div class="charge-popup">
            <div class="cp-header" style="background:${headerColor};">${escapeHtml(codeTitle)}</div>
            <div class="cp-body">
                <div class="cp-addr">📍 ${escapeHtml(addrParts.join(' · ') || '—')}</div>
                <div class="cp-tech">🧑‍🔧 ${escapeHtml(techsText)}</div>
                <div class="cp-divider"></div>
                <div class="cp-poles-label">Trụ (${poles.length})</div>
                <div class="cp-poles-scroll">${polesHtml || '<div style="color:#94a3b8;font-size:12px;">Không có dữ liệu.</div>'}</div>
            </div>
        </div>`;
}

// ---------- Icon cụm: vòng tỉ lệ EV/BSS + lõi cam ghi tổng số trụ ----------
function buildChargeClusterIcon(cluster) {
    let evCount = 0, bssCount = 0;
    cluster.getAllChildMarkers().forEach((m) => {
        evCount += m._evCount || 0;
        bssCount += m._bssCount || 0;
    });
    const total = evCount + bssCount || cluster.getChildCount();

    const evDeg = total > 0 ? (evCount / total) * 360 : 0;
    const ring = total > 0
        ? `conic-gradient(${EV_COLOR} 0deg ${evDeg}deg, ${BSS_COLOR} ${evDeg}deg 360deg)`
        : EV_COLOR;

    const size = total < 50 ? 26 : total < 300 ? 32 : 38;
    const fontSize = total < 50 ? 10.5 : total < 300 ? 11.5 : 13;
    const holeSize = size - 9;

    return L.divIcon({
        html: `
            <div style="width:${size}px;height:${size}px;border-radius:50%;background:${ring};
                        box-shadow:0 2px 6px rgba(15,23,42,.4);border:2px solid #fff;
                        display:flex;align-items:center;justify-content:center;">
              <div style="width:${holeSize}px;height:${holeSize}px;border-radius:50%;background:${CLUSTER_CORE_COLOR};
                          display:flex;align-items:center;justify-content:center;
                          color:#fff;font-weight:800;font-size:${fontSize}px;
                          font-family:'Inter',system-ui,-apple-system,sans-serif;">${total}</div>
            </div>`,
        className: 'charge-cluster-icon',
        iconSize: [size, size],
    });
}

// Layer gom cụm (mặc định) + layer phẳng cho "chế độ hiện tất cả" (không gom
// cụm) - chỉ 1 trong 2 được add vào map tại 1 thời điểm.
const stationLayer = L.markerClusterGroup({
    maxClusterRadius: 60,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    disableClusteringAtZoom: 17,
    iconCreateFunction: buildChargeClusterIcon,
}).addTo(map);
const plainStationLayer = L.layerGroup();

let clusterMode = true;
let allChargeStations = [];    // cache toàn bộ trạm nhận từ API
let allTechSummary = {};       // {region: [{tech_name,...}]}
let selectedTechs = new Set(); // kỹ thuật viên đang tick chọn (rỗng = không lọc theo KT)
let showEv = true;
let showBss = true;

function bindStationPopup(layer, s) {
    layer.bindPopup(() => buildChargeStationPopup(s), {
        maxWidth: 280,
        className: 'charge-station-popup',
        closeButton: false,
    });
}

// Chế độ gom cụm: marker divIcon (giọt nước/khiên, đẹp + rõ) - số lượng
// hiện trên màn hình luôn ít (đã gom cụm) nên không lo hiệu năng.
function buildFancyMarker(s) {
    const marker = L.marker([s.lat, s.lng], { icon: chargeStationIcon(s.type) });
    bindStationPopup(marker, s);
    marker._evCount = s.ev_count || 0;
    marker._bssCount = s.bss_count || 0;
    return marker;
}

// Chế độ "Hiện tất cả": có thể vẽ CÙNG LÚC hàng nghìn điểm không gom cụm -
// mỗi divIcon là 1 <div><svg> riêng (rất nặng ở số lượng lớn). circleMarker
// vẽ thẳng lên Canvas (map preferCanvas:true ở trên) - hàng nghìn điểm vẫn
// mượt vì trình duyệt chỉ tốn 1 lần vẽ canvas thay vì hàng nghìn phần tử DOM.
function buildLightMarker(s) {
    const color = s.type === 'ev' ? EV_COLOR : BSS_COLOR;
    const marker = L.circleMarker([s.lat, s.lng], {
        radius: 4,
        weight: 1,
        color: '#ffffff',
        fillColor: color,
        fillOpacity: 0.9,
    });
    bindStationPopup(marker, s);
    return marker;
}

function applyFilters() {
    const stations = allChargeStations.filter((s) => {
        if (selectedTechs.size > 0 && !(s.techs || []).some((t) => selectedTechs.has(t))) return false;
        return (showEv && s.ev_count > 0) || (showBss && s.bss_count > 0);
    });

    stationLayer.clearLayers();
    plainStationLayer.clearLayers();

    if (clusterMode) {
        stationLayer.addLayers(stations.map(buildFancyMarker));
    } else {
        stations.forEach((s) => plainStationLayer.addLayer(buildLightMarker(s)));
    }
}

// ---------- Chip lọc EV/BSS (giống bản đồ sự cố) ----------
function syncChip(chipId, checkboxId, onChange) {
    const chip = document.getElementById(chipId);
    const cb = document.getElementById(checkboxId);
    if (!chip || !cb) return;
    cb.addEventListener('change', () => {
        chip.classList.toggle('active', cb.checked);
        onChange(cb.checked);
        applyFilters();
    });
}
syncChip('chip-ev', 'filter-ev', (checked) => { showEv = checked; });
syncChip('chip-bss', 'filter-bss', (checked) => { showBss = checked; });

// ---------- Chế độ gom nhóm / hiện tất cả (không gom cụm) ----------
const modeToggleBtn = document.getElementById('mode-toggle-btn');
if (modeToggleBtn) {
    modeToggleBtn.addEventListener('click', () => {
        clusterMode = !clusterMode;
        modeToggleBtn.textContent = clusterMode ? '🔗 Gom nhóm' : '📍 Hiện tất cả';
        modeToggleBtn.classList.toggle('no-cluster', !clusterMode);
        if (clusterMode) {
            if (map.hasLayer(plainStationLayer)) map.removeLayer(plainStationLayer);
            if (!map.hasLayer(stationLayer)) stationLayer.addTo(map);
        } else {
            if (map.hasLayer(stationLayer)) map.removeLayer(stationLayer);
            if (!map.hasLayer(plainStationLayer)) plainStationLayer.addTo(map);
        }
        applyFilters();
    });
}

// ---------- Bảng lọc kỹ thuật viên (theo khu vực, thu gọn được) ----------
const techPanel = document.getElementById('tech-panel');
const techPanelHandle = document.getElementById('tech-panel-handle');
const techPanelClose = document.getElementById('tech-panel-close');
const techPanelBody = document.getElementById('tech-panel-body');
const techSelectedCount = document.getElementById('tech-selected-count');
const techSelectAllBtn = document.getElementById('tech-select-all');
const techClearAllBtn = document.getElementById('tech-clear-all');

function renderTechPanel(techSummary) {
    allTechSummary = techSummary || {};
    if (!techPanelBody) return;
    const regions = Object.keys(allTechSummary).sort();
    if (regions.length === 0) {
        techPanelBody.innerHTML = '<div class="tech-panel-empty">Không có dữ liệu.</div>';
        return;
    }
    techPanelBody.innerHTML = regions.map((region) => {
        const rows = allTechSummary[region] || [];
        const names = rows.map((r) => r.tech_name);
        const allSelected = names.length > 0 && names.every((n) => selectedTechs.has(n));
        const rowsHtml = rows.map((r) => {
            const checked = selectedTechs.has(r.tech_name) ? 'checked' : '';
            return `
                <label class="tech-row ${checked ? 'selected' : ''}">
                    <div class="tech-row-top">
                        <input type="checkbox" class="tech-check" value="${escapeHtml(r.tech_name)}" ${checked}>
                        <span class="tech-name">${escapeHtml(r.tech_name)}</span>
                    </div>
                    <div class="tech-counts">
                        <span class="tc ev">⚡${r.ev_count}</span>
                        <span class="tc bss">🔋${r.bss_count}</span>
                        <span class="tc total">Σ${r.total_count}</span>
                    </div>
                </label>`;
        }).join('');
        return `
            <div class="region-group" data-region="${escapeHtml(region)}">
                <div class="region-header">
                    <input type="checkbox" class="region-check" ${allSelected ? 'checked' : ''}
                           title="Chọn/bỏ chọn cả khu vực này">
                    <span class="region-name">${escapeHtml(region)}</span>
                    <span class="region-count">${rows.length}</span>
                    <span class="chevron">▸</span>
                </div>
                <div class="region-techs">${rowsHtml}</div>
            </div>`;
    }).join('');

    techPanelBody.querySelectorAll('.region-header').forEach((header) => {
        header.addEventListener('click', (e) => {
            if (e.target.classList.contains('region-check')) return;
            header.closest('.region-group').classList.toggle('expanded');
        });
    });

    techPanelBody.querySelectorAll('.region-check').forEach((cb) => {
        cb.addEventListener('change', () => {
            const group = cb.closest('.region-group');
            const region = group.dataset.region;
            (allTechSummary[region] || []).forEach((r) => {
                if (cb.checked) selectedTechs.add(r.tech_name);
                else selectedTechs.delete(r.tech_name);
            });
            renderTechPanel(allTechSummary);
            updateSelectedCount();
            applyFilters();
        });
    });

    techPanelBody.querySelectorAll('.tech-check').forEach((cb) => {
        cb.addEventListener('change', () => {
            if (cb.checked) selectedTechs.add(cb.value);
            else selectedTechs.delete(cb.value);
            cb.closest('.tech-row').classList.toggle('selected', cb.checked);
            const region = cb.closest('.region-group').dataset.region;
            const regionCb = cb.closest('.region-group').querySelector('.region-check');
            const names = (allTechSummary[region] || []).map((r) => r.tech_name);
            regionCb.checked = names.length > 0 && names.every((n) => selectedTechs.has(n));
            updateSelectedCount();
            applyFilters();
        });
    });
}

function updateSelectedCount() {
    if (!techSelectedCount) return;
    techSelectedCount.textContent = selectedTechs.size > 0
        ? `Đang lọc ${selectedTechs.size} kỹ thuật viên`
        : 'Chưa lọc — hiện tất cả';
}

if (techPanelHandle) {
    techPanelHandle.addEventListener('click', () => {
        techPanel.classList.add('open');
        techPanelHandle.classList.add('hidden');
    });
}
if (techPanelClose) {
    techPanelClose.addEventListener('click', () => {
        techPanel.classList.remove('open');
        techPanelHandle.classList.remove('hidden');
    });
}

if (techSelectAllBtn) {
    techSelectAllBtn.addEventListener('click', () => {
        Object.values(allTechSummary).forEach((rows) => {
            rows.forEach((r) => selectedTechs.add(r.tech_name));
        });
        renderTechPanel(allTechSummary);
        updateSelectedCount();
        applyFilters();
    });
}
if (techClearAllBtn) {
    techClearAllBtn.addEventListener('click', () => {
        selectedTechs.clear();
        renderTechPanel(allTechSummary);
        updateSelectedCount();
        applyFilters();
    });
}

// ---------- Thống kê trụ thiếu toạ độ ----------
const missingCoordBadge = document.getElementById('missing-coord-badge');
const missingCoordPanel = document.getElementById('missing-coord-panel');

function renderMissingCoordPanel(items) {
    if (!missingCoordBadge || !missingCoordPanel) return;
    if (!items || items.length === 0) {
        missingCoordBadge.style.display = 'none';
        return;
    }
    missingCoordBadge.style.display = 'inline-flex';
    missingCoordBadge.textContent = `⚠️ ${items.length} trụ thiếu toạ độ`;
    missingCoordPanel.innerHTML = items.slice(0, 200).map((it) => `
        <div class="mc-item">
            <div class="mc-sn">${escapeHtml(it.sn || '—')}</div>
            <div class="mc-meta">${escapeHtml(it.station_code || '—')} · ${escapeHtml(it.name || '—')}</div>
        </div>`).join('') + (items.length > 200
            ? `<div class="mc-more">… và ${items.length - 200} trụ khác.</div>` : '');
}

if (missingCoordBadge) {
    missingCoordBadge.addEventListener('click', () => {
        missingCoordPanel.classList.toggle('open');
    });
    document.addEventListener('click', (e) => {
        if (!missingCoordPanel.classList.contains('open')) return;
        if (missingCoordPanel.contains(e.target) || missingCoordBadge.contains(e.target)) return;
        missingCoordPanel.classList.remove('open');
    });
}

async function loadAllStations() {
    const summaryBadge = document.getElementById('summary-badge');
    try {
        const res = await fetch('/api/all-charging-stations');
        const data = await res.json();
        if (!res.ok) {
            summaryBadge.textContent = data.error || 'Lỗi tải dữ liệu';
            return;
        }
        allChargeStations = data.stations || [];
        applyFilters();
        renderTechPanel(data.tech_summary || {});
        renderMissingCoordPanel(data.missing_coord_items || []);
        updateSelectedCount();

        summaryBadge.textContent =
            `${data.total_stations} trạm · ${data.total_poles} trụ sạc`;
    } catch (err) {
        summaryBadge.textContent = 'Không thể kết nối tới server.';
    }
}

loadAllStations();
