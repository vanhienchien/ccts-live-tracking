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

// Màu cố ý CHỌN LỆCH hẳn bảng màu bản đồ Google (xanh lá đất liền, xanh
// dương nhạt mặt nước, vàng đường lớn) để icon luôn nổi bật bất kể nền:
// chàm (indigo) cho EV, hồng tím (magenta) cho BSS - không trùng đỏ/cam/
// xanh lá vốn đã mang nghĩa "mức độ nghiêm trọng" ở bản đồ sự cố.
const EV_COLOR = '#4338ca';
const BSS_COLOR = '#c026d3';
const CLUSTER_CORE_COLOR = '#f97316'; // cam - theo yêu cầu, thay cho màu đen/xám cũ

function escapeHtml(str) {
    return String(str ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

// ---------- Icon trạm: 2 HÌNH DẠNG khác nhau (không chỉ khác màu) ----------
// EV: ghim giọt nước cổ điển + tia sét trắng.
// BSS: khiên bo góc (dáng hoàn toàn khác giọt nước) + biểu tượng pin trắng.
// Cả 2 đều viền trắng dày + đổ bóng đậm để nổi bật trên mọi nền bản đồ.
function chargeStationIcon(type) {
    const isEv = type === 'ev';
    const color = isEv ? EV_COLOR : BSS_COLOR;
    const glyph = isEv
        // Tia sét (dạng path, không dùng emoji để hiển thị nhất quán mọi máy).
        ? `<path transform="translate(7,6) scale(0.7)"
                 d="M11.983 1.907a.75.75 0 00-1.292-.657l-8.5 9.5A.75.75 0 002.75 12h6.572l-1.305 6.093a.75.75 0 001.292.657l8.5-9.5A.75.75 0 0017.25 8h-6.572l1.305-6.093z"
                 fill="#ffffff"/>`
        // Biểu tượng pin: khung bo góc + 1 cọc nhỏ + 1 vạch đầy trắng.
        : `<rect x="9" y="9" width="13" height="9" rx="2" fill="none" stroke="#ffffff" stroke-width="1.8"/>
           <rect x="22" y="11.5" width="2.4" height="4" rx="1" fill="#ffffff"/>
           <rect x="10.5" y="10.5" width="4.5" height="6" rx="1" fill="#ffffff"/>`;

    const shape = isEv
        // Giọt nước (pin cổ điển).
        ? `<path d="M15 0C6.7 0 0 6.7 0 15c0 11.25 15 27 15 27s15-15.75 15-27C30 6.7 23.3 0 15 0z"
                 fill="${color}" stroke="#ffffff" stroke-width="2"/>`
        // Khiên bo góc + đuôi nhọn - silhouette khác hẳn giọt nước.
        : `<path d="M15 2 L24 2 Q28 2 28 6 L28 19 Q28 22.5 25 24.5 L15 40 L5 24.5 Q2 22.5 2 19 L2 6 Q2 2 6 2 Z"
                 fill="${color}" stroke="#ffffff" stroke-width="2"/>`;

    const html = `
        <div style="position:relative;width:20px;height:26px;
                    filter:drop-shadow(0 2px 4px rgba(0,0,0,.55));">
            <svg width="20" height="26" viewBox="0 0 30 42" xmlns="http://www.w3.org/2000/svg">
                ${shape}
                ${glyph}
            </svg>
        </div>`;
    return L.divIcon({
        className: '',
        html,
        iconSize: [20, 26],
        iconAnchor: [10, 26],
        popupAnchor: [0, -24],
    });
}

function buildChargeStationPopup(s) {
    const addrParts = [s.district, s.province].filter(Boolean);
    const codesText = (s.station_codes || []).join(', ') || '—';
    const techsText = (s.techs || []).join(', ') || 'Chưa phân công';
    return `
        <div class="charge-popup">
            <div class="cp-name">${escapeHtml(s.name || 'Trạm sạc')}</div>
            <div class="cp-addr">${escapeHtml(addrParts.join(' · ') || '—')}</div>
            <div class="cp-counts">
                ${s.ev_count > 0 ? `<span class="cp-count-pill ev">⚡ ${s.ev_count} EV</span>` : ''}
                ${s.bss_count > 0 ? `<span class="cp-count-pill bss">🔋 ${s.bss_count} BSS</span>` : ''}
            </div>
            <div class="cp-codes">Mã trạm: ${escapeHtml(codesText)}</div>
            <div class="cp-codes">Kỹ thuật viên: ${escapeHtml(techsText)}</div>
        </div>`;
}

// ---------- Icon cụm: vòng tỉ lệ EV/BSS + lõi cam ghi tổng số trụ ----------
// Trước đây là 1 chấm đen/xám trơn chỉ ghi số - giờ vòng ngoài tô theo tỉ lệ
// conic-gradient (chàm=EV, hồng tím=BSS) để biết ngay tỉ trọng loại trụ
// trong khu vực mà không cần bấm vào; lõi trong đổi sang cam theo yêu cầu.
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

    const size = total < 50 ? 42 : total < 300 ? 50 : 58;
    const fontSize = total < 50 ? 13 : total < 300 ? 14.5 : 16;
    const holeSize = size - 12;

    return L.divIcon({
        html: `
            <div style="width:${size}px;height:${size}px;border-radius:50%;background:${ring};
                        box-shadow:0 3px 10px rgba(15,23,42,.4);border:3px solid #fff;
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

const stationLayer = L.markerClusterGroup({
    maxClusterRadius: 60,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    disableClusteringAtZoom: 17,
    iconCreateFunction: buildChargeClusterIcon,
}).addTo(map);

let allChargeStations = [];   // cache toàn bộ trạm nhận từ API
let selectedTechs = new Set(); // kỹ thuật viên đang tick chọn trong bảng lọc (rỗng = hiện tất cả)

function applyTechFilter() {
    const stations = selectedTechs.size === 0
        ? allChargeStations
        : allChargeStations.filter((s) => (s.techs || []).some((t) => selectedTechs.has(t)));

    const markers = stations.map((s) => {
        const marker = L.marker([s.lat, s.lng], { icon: chargeStationIcon(s.type) });
        marker.bindPopup(() => buildChargeStationPopup(s), { maxWidth: 300 });
        marker._evCount = s.ev_count || 0;
        marker._bssCount = s.bss_count || 0;
        return marker;
    });
    stationLayer.clearLayers();
    stationLayer.addLayers(markers);
}

// ---------- Bảng lọc kỹ thuật viên (theo khu vực) ----------
// Hiện số trụ (EV/BSS/Tổng) NGAY TRONG danh sách - không cần chọn mới thấy,
// nên tự nhiên "so sánh" được nhiều kỹ thuật viên cùng lúc bằng mắt; tick
// chọn thêm để lọc bản đồ chỉ còn trạm của (các) kỹ thuật viên đó.
const techPanel = document.getElementById('tech-panel');
const techPanelHandle = document.getElementById('tech-panel-handle');
const techPanelBody = document.getElementById('tech-panel-body');
const techSelectedCount = document.getElementById('tech-selected-count');

function renderTechPanel(techSummary) {
    if (!techPanelBody) return;
    const regions = Object.keys(techSummary || {}).sort();
    if (regions.length === 0) {
        techPanelBody.innerHTML = '<div class="tech-panel-empty">Không có dữ liệu.</div>';
        return;
    }
    techPanelBody.innerHTML = regions.map((region) => {
        const rows = techSummary[region] || [];
        const rowsHtml = rows.map((r) => {
            const checked = selectedTechs.has(r.tech_name) ? 'checked' : '';
            return `
                <label class="tech-row">
                    <input type="checkbox" class="tech-check" value="${escapeHtml(r.tech_name)}" ${checked}>
                    <span class="tech-name">${escapeHtml(r.tech_name)}</span>
                    <span class="tech-counts">
                        <span class="tc ev">⚡${r.ev_count}</span>
                        <span class="tc bss">🔋${r.bss_count}</span>
                        <span class="tc total">Σ${r.total_count}</span>
                    </span>
                </label>`;
        }).join('');
        return `
            <div class="region-group">
                <div class="region-header">${escapeHtml(region)}</div>
                ${rowsHtml}
            </div>`;
    }).join('');

    techPanelBody.querySelectorAll('.tech-check').forEach((cb) => {
        cb.addEventListener('change', () => {
            if (cb.checked) selectedTechs.add(cb.value);
            else selectedTechs.delete(cb.value);
            cb.closest('.tech-row').classList.toggle('selected', cb.checked);
            updateSelectedCount();
            applyTechFilter();
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
        techPanel.classList.toggle('open');
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
        applyTechFilter();
        renderTechPanel(data.tech_summary || {});
        updateSelectedCount();

        summaryBadge.textContent =
            `${data.total_stations} trạm · ${data.total_poles} trụ sạc`;
    } catch (err) {
        summaryBadge.textContent = 'Không thể kết nối tới server.';
    }
}

loadAllStations();
