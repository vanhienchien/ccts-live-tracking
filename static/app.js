// ==========================================
// CCTS Live Map - frontend logic
// ==========================================

const CURRENT_USERNAME = (document.body.dataset.username || '').trim();
const CURRENT_ROLE = (document.body.dataset.role || '').trim().toLowerCase();

const map = L.map('map').setView([12.25, 108.5], 6.3);
// Nền bản đồ CartoDB Voyager — tile chính thức (không cần API key), phong
// cách tối giản khớp giao diện dashboard tối màu, thay cho endpoint Google
// Maps không chính thức trước đây (không SLA, có thể bị chặn bất kỳ lúc
// nào). Muốn đổi sang phong cách khác của CARTO, chỉ cần đổi "voyager" ở
// URL thành "light_all" (tối giản hơn) hoặc "dark_all" (nền tối, hợp với
// header) — https://github.com/CartoDB/basemap-styles
L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
    subdomains: 'abcd',
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    maxZoom: 20,
}).addTo(map);

// CSS popup trạm v2 — card hiện đại, bỏ style mặc định Leaflet
(function injectStationPopupStyles() {
    if (document.getElementById('station-popup-v2-css')) return;
    const style = document.createElement('style');
    style.id = 'station-popup-v2-css';
    style.textContent = `
.station-popup-v2 .leaflet-popup-content-wrapper {
    background: #ffffff;
    border-radius: 16px;
    box-shadow: 0 12px 40px rgba(15,23,42,.14), 0 4px 12px rgba(15,23,42,.06);
    border: 1px solid rgba(226,232,240,.9);
    padding: 0;
    overflow: hidden;
}
.station-popup-v2 .leaflet-popup-content {
    margin: 14px 16px 12px;
    min-width: 0;
    width: auto !important;
}
.station-popup-v2 .leaflet-popup-tip {
    background: #ffffff;
    box-shadow: 0 2px 6px rgba(15,23,42,.08);
}
.station-popup-v2 a.leaflet-popup-close-button {
    display: none !important;
}
`;
    document.head.appendChild(style);
})();

// Gom cụm marker khi zoom xa - trước đây dùng L.layerGroup() nên hàng ngàn
// trạm chồng lên nhau ở các khu đô thị đông trạm (HCM, Cần Thơ...), rối mắt.
// disableClusteringAtZoom=16 khớp đúng mức zoom flyToStation() dùng, để tới
// đó luôn thấy marker riêng lẻ như cũ.
const stationLayer = L.markerClusterGroup({
    maxClusterRadius: 50,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    disableClusteringAtZoom: 16,
}).addTo(map);
const staffMarkers = {}; // username -> { marker, wrenchMarker }

let allStations = [];          // cache toàn bộ trạm nhận được gần nhất (đã lọc theo quyền ở server)
let selectedTechs = new Set(); // tên kỹ thuật viên đang được TICK CHỌN trong tag lọc (rỗng = hiện tất cả)
let onlineUsernames = new Set(); // username (chữ thường) đang online

// Cờ ưu tiên tím — không vòng tròn, chỉ lá cờ + cán để dễ nhận trên bản đồ
const FLAG_SVG = `<svg width="14" height="16" viewBox="0 0 24 28" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <path d="M5 2v24" stroke="#4c1d95" stroke-width="2.4" stroke-linecap="round"/>
  <path d="M6.2 3.2h11.5c.7 0 1.1.8.7 1.35L15.8 9.2l2.6 4.65c.4.55 0 1.35-.7 1.35H6.2V3.2z" fill="#7c3aed"/>
  <path d="M6.2 3.2h11.5c.7 0 1.1.8.7 1.35L15.8 9.2l2.6 4.65c.4.55 0 1.35-.7 1.35H6.2V3.2z" fill="none" stroke="#5b21b6" stroke-width="0.8"/>
</svg>`;

// ---------- Icon trạm sạc: ghim giọt nước cổ điển ----------
function stationIcon(color, hasNearOverdue, hasNoInfoCritical) {
    // purple_critical (cache cũ) map về đỏ như overdue thường
    const colorMap = { red: '#dc2626', orange: '#ea580c', green: '#16a34a', purple_critical: '#dc2626' };
    const fill = colorMap[color] || '#3b82f6';

    // Ticket Open-overdue CHƯA có thông tin — chỉ cờ tím, KHÔNG vòng tròn bao quanh
    const criticalBadge = hasNoInfoCritical ? `
        <div style="position:absolute; top:-10px; right:-12px; width:14px; height:16px;
                    display:flex; align-items:center; justify-content:center;
                    filter: drop-shadow(0 1px 2px rgba(0,0,0,.4));
                    animation:unassigned-pulse 1.4s infinite; z-index:11;">${FLAG_SVG}</div>
    ` : '';

    const warningBadge = (!hasNoInfoCritical && hasNearOverdue) ? `
        <div style="position:absolute; top:-9px; right:-11px; width:20px; height:20px; border-radius:50%;
                    background:#1e293b; color:#fbbf24; display:flex; align-items:center; justify-content:center;
                    font-size:11px; border:2px solid #fff; box-shadow:0 2px 6px rgba(0,0,0,.45);
                    animation:unassigned-pulse 1.4s infinite; z-index:10;">⏰</div>
    ` : '';

    const htmlContent = `
        <div style="position:relative; width:16px; height:22px;">
            <svg width="16" height="22" viewBox="0 0 30 42" xmlns="http://www.w3.org/2000/svg">
                <path d="M15 0C6.7 0 0 6.7 0 15c0 11.25 15 27 15 27s15-15.75 15-27C30 6.7 23.3 0 15 0z"
                      fill="${fill}" stroke="rgba(0,0,0,.3)" stroke-width="1.2"/>
                <circle cx="15" cy="15" r="9" fill="#ffffff"/>
                <text x="15" y="19" font-size="12" font-weight="700" text-anchor="middle"
                      font-family="Inter, system-ui, sans-serif" fill="${fill}">i</text>
            </svg>
            ${criticalBadge}
            ${warningBadge}
        </div>`;

    return L.divIcon({
        className: '',
        html: htmlContent,
        iconSize: [16, 22],
        iconAnchor: [8, 22],
        popupAnchor: [0, -20],
    });
}

function unassignedStationIcon(hasNearOverdue) {
    const warningBadge = hasNearOverdue ? `
        <div style="position:absolute;top:-4px;right:-6px;width:16px;height:16px;border-radius:50%;
                    background:#1e293b;color:#fbbf24;display:flex;align-items:center;justify-content:center;
                    font-size:9px;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.45);">⏰</div>
    ` : '';
    const svg = `
        <div style="width:28px;height:28px;display:flex;align-items:center;justify-content:center;
                    color:#dc2626;font-size:28px;font-weight:900;line-height:1;
                    filter: drop-shadow(0 2px 5px rgba(0,0,0,0.45));
                    animation: unassigned-pulse 1.5s infinite;">
            !
            ${warningBadge}
        </div>`;
    return L.divIcon({
        className: '',
        html: svg,
        iconSize: [20, 20],
        iconAnchor: [10, 10],
        popupAnchor: [0, -14],
    });
}

async function triggerManualRefresh() {
    const btn = document.getElementById('btn-manual-refresh');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '⏳';
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

function applyRefreshPausedUI(paused) {
    const btn = document.getElementById('btn-toggle-refresh');
    if (!btn) return;
    btn.dataset.paused = paused ? '1' : '0';
    btn.classList.toggle('is-paused', !!paused);
    btn.textContent = paused ? '▶' : '⏸';
    btn.title = paused
        ? 'Đang tạm dừng cào tự động — bấm để BẬT LẠI'
        : 'Đang cào tự động — bấm để TẠM DỪNG';
}

async function loadRefreshStatus() {
    const btn = document.getElementById('btn-toggle-refresh');
    if (!btn) return;
    try {
        const res = await fetch('/api/admin/refresh-status');
        if (!res.ok) return;
        const data = await res.json();
        applyRefreshPausedUI(!!data.paused);
    } catch (_) { /* ignore */ }
}

async function toggleAutoRefresh() {
    const btn = document.getElementById('btn-toggle-refresh');
    if (btn) btn.disabled = true;
    try {
        const res = await fetch('/api/admin/toggle-refresh', { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            applyRefreshPausedUI(!!data.paused);
            alert((data.paused ? '⏸ ' : '▶ ') + data.message);
        } else {
            alert('❌ ' + (data.error || 'Không đổi được trạng thái.'));
        }
    } catch (err) {
        alert('❌ Không thể kết nối tới server.');
    } finally {
        if (btn) btn.disabled = false;
    }
}

// Đồng bộ icon pause khi trang load (chỉ admin có nút)
if (document.getElementById('btn-toggle-refresh')) {
    loadRefreshStatus();
}

// ---------- Popup trạm: dựng HTML ở CLIENT từ dữ liệu thô (station.tickets) ----------
// Trước đây backend (ccts_data.py) tự dựng sẵn popup_html cho từng trạm rồi gửi qua
// WebSocket - chuỗi CSS/HTML lặp lại cho mỗi ticket khiến payload phình to rất nhiều
// lần (thủ phạm chính gây tốn băng thông). Giờ backend chỉ gửi DỮ LIỆU THÔ
// (station.tickets = mảng ticket gọn nhẹ), và HTML được dựng ngay tại đây, chỉ khi
// người dùng thực sự mở popup đó (Leaflet gọi hàm này lười - lazy).
const STATUS_COLORS = {
    'open': '#e74c3c',
    'appointment': '#3498db',
    'pending for asp close': '#9b59b6',
    'pending for spare parts': '#e67e22',
    'pending for local team close': '#16a085',
    'pending for voms confirm': '#16a085',
};

function statusColor(status) {
    return STATUS_COLORS[(status || '').trim().toLowerCase()] || '#7f8c8d';
}

// Thang màu theo số giờ tồn đọng - PHẢI khớp với _severity_color() bên ccts_data.py
// (backend chỉ gửi kèm khoá "severity": "red"/"orange"/"green" cho mỗi ticket).
// "purple_critical" là trường hợp ĐẶC BIỆT: ticket Open, overdue (>48h), trụ EV,
// mà CHƯA có bất kỳ thông tin xử lý nào - PHẢI khớp NO_INFO_SEVERITY_KEY bên
// ccts_data.py.
// Khớp _severity_color() trong ccts_data.py — dùng tô nền từng ticket card trong popup
const SEVERITY_STYLES = {
    red:    { bg: '#ff9f94', border: '#b32a1b', text: '#b32a1b' },
    orange: { bg: '#ffca9c', border: '#ce6b15', text: '#ce6b15' },
    green:  { bg: '#93ffab', border: '#26ac43', text: '#26ac43' },
    // Cache cũ còn purple_critical → dùng palette đỏ (cờ tím chỉ là icon)
    purple_critical: { bg: '#ff9f94', border: '#b32a1b', text: '#b32a1b' },
};

const STATION_HEADER_COLORS = { red: '#b32a1b', orange: '#ce6b15', green: '#26ac43', purple_critical: '#b32a1b' };
const STATION_ACCENT = {
    red:    { bar: '#ef4444', soft: '#fef2f2', text: '#b91c1c', chip: '#fee2e2' },
    orange: { bar: '#f97316', soft: '#fff7ed', text: '#c2410c', chip: '#ffedd5' },
    green:  { bar: '#22c55e', soft: '#f0fdf4', text: '#15803d', chip: '#dcfce7' },
    purple_critical: { bar: '#ef4444', soft: '#fef2f2', text: '#b91c1c', chip: '#fee2e2' },
};

/** Người tạo ticket cần loại bỏ khi hiển thị tài khoản phụ trách */
const CREATOR_BLOCKLIST = new Set(['thailong', 'quangle']);

function parseAccountTokens(str) {
    if (!str) return [];
    return String(str).split(/[;,]/).map((s) => s.trim()).filter(Boolean);
}

function isEsOrItsAccount(name) {
    const n = (name || '').trim().toLowerCase();
    return n.startsWith('es') || n.startsWith('its');
}

/**
 * Gộp cctsTicketOwnerUserName + assistantName, lọc creator (thailong/quangle),
 * owner chỉ giữ ES/ITS, assistant giữ nguyên (trừ blocklist).
 * Thu gọn: Esmanager_Mtay_1; _2; _3; _4 → Esmanager_Mtay_1 (2, 3, 4)
 */
function compactEsAccounts(ownerStr, assistantStr) {
    const fromOwner = parseAccountTokens(ownerStr).filter((n) => {
        if (CREATOR_BLOCKLIST.has(n.toLowerCase())) return false;
        return isEsOrItsAccount(n);
    });
    const fromAsst = parseAccountTokens(assistantStr).filter((n) => {
        if (CREATOR_BLOCKLIST.has(n.toLowerCase())) return false;
        return true;
    });

    const seen = new Set();
    const unique = [];
    for (const n of [...fromOwner, ...fromAsst]) {
        const key = n.toLowerCase();
        if (!seen.has(key)) {
            seen.add(key);
            unique.push(n);
        }
    }
    if (unique.length === 0) return '';

    const groups = new Map();
    for (const name of unique) {
        const m = name.match(/^(.*?)([_\s]+)(\d+)$/);
        if (m) {
            const baseDisplay = m[1];
            const baseKey = baseDisplay.toLowerCase();
            if (!groups.has(baseKey)) groups.set(baseKey, { baseDisplay, items: [] });
            groups.get(baseKey).items.push({ num: m[3], full: name });
        } else {
            const baseKey = name.toLowerCase();
            if (!groups.has(baseKey)) {
                groups.set(baseKey, { baseDisplay: name, items: [{ num: null, full: name }] });
            }
        }
    }

    const parts = [];
    for (const g of groups.values()) {
        const withNum = g.items.filter((it) => it.num != null);
        if (withNum.length === 0) {
            parts.push(g.items[0].full);
            continue;
        }
        withNum.sort((a, b) => Number(a.num) - Number(b.num));
        const first = withNum[0];
        const rest = withNum.slice(1).map((it) => it.num);
        parts.push(rest.length === 0 ? first.full : `${first.full} (${rest.join(', ')})`);
    }
    return parts.join('; ');
}

function pickStationAddress(tickets) {
    if (!tickets || !tickets.length) return '';
    for (const t of tickets) {
        const addr = (t.address || t.Address || '').trim();
        if (addr) return addr;
    }
    return '';
}

function escapeHtmlAttr(str) {
    return String(str ?? '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

// Nút "📋 copy mã trạm" trong header popup dùng data-attribute + 1 listener
// dùng chung (thay vì onclick inline) để không phải lo escape ký tự đặc biệt.
document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-copy-code]');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const code = btn.dataset.copyCode || '';
    if (!code || !navigator.clipboard) return;
    navigator.clipboard.writeText(code).then(() => {
        const prevHtml = btn.innerHTML;
        btn.innerHTML = '✓';
        setTimeout(() => { btn.innerHTML = prevHtml; }, 1200);
    }).catch(() => {});
});

function pickStationAccounts(tickets) {
    if (!tickets || !tickets.length) return '';
    for (const t of tickets) {
        // Backend mới đã compact sẵn trong t.owners
        if (t.owners) return t.owners;
        const owner = t.cctsTicketOwnerUserName || t.owner || t.user || t.TicketOwner || '';
        const asst = t.assistantName || t.assistant || t.Collaborators || '';
        if (owner || asst) return compactEsAccounts(owner, asst);
    }
    return '';
}

function buildStationPopup(s) {
    const gmapUrl = `https://www.google.com/maps?q=${s.lat},${s.lng}`;
    // Cache cũ có thể còn color=purple_critical → map về red
    const stationColorKey = (s.color === 'purple_critical' ? 'red' : s.color);
    const accent = STATION_ACCENT[stationColorKey] || STATION_ACCENT.green;
    const tickets = s.tickets || [];
    const ticketCount = tickets.length;
    const stationAddress = (s.address || '').trim() || pickStationAddress(tickets);
    const isUnassigned = !!s.is_unassigned;

    const rowsHtml = tickets.map((t) => {
        const sColor = statusColor(t.status);
        const rawSev = t.severity || 'green';
        // Nền card đỏ theo giờ tồn — không tô tím; cờ tím chỉ dùng làm icon cảnh báo
        const styleKey = (t.is_no_info_critical || rawSev === 'purple_critical')
            ? 'red'
            : rawSev;
        const sevAccent = STATION_ACCENT[styleKey] || STATION_ACCENT.green;
        const sevStyle = SEVERITY_STYLES[styleKey] || SEVERITY_STYLES.green;
        const hours = Number(t.hours) || 0;
        const statusLabel = t.status_display || t.status || '';

        let nearOverdueHtml = '';
        if (t.is_no_info_critical) {
            nearOverdueHtml = `
            <div style="margin-top:8px;display:inline-flex;align-items:center;gap:7px;
                        padding:5px 11px;background:#fef2f2;color:#b91c1c;
                        border:1px solid #fecaca;border-radius:999px;font-size:11px;font-weight:600;">
                <span style="display:inline-flex;width:14px;height:16px;align-items:center;justify-content:center;flex-shrink:0;">${FLAG_SVG}</span>
                Overdue · CHƯA có thông tin xử lý
            </div>`;
        } else if (t.is_near_overdue) {
            const remaining = Math.max(0, 48 - hours);
            nearOverdueHtml = `
            <div style="margin-top:8px;display:inline-flex;align-items:center;gap:5px;
                        padding:4px 10px;background:#1e293b;color:#fbbf24;
                        border-radius:999px;font-size:11px;font-weight:600;
                        animation:unassigned-pulse 1.5s infinite;">
                <span style="font-size:12px;">⏰</span> Sắp quá hạn · còn ~${remaining.toFixed(1)}h
            </div>`;
        } else if (hours > 48) {
            nearOverdueHtml = `
            <div style="margin-top:8px;display:inline-flex;align-items:center;gap:5px;
                        padding:4px 10px;background:#fef2f2;color:#dc2626;
                        border-radius:999px;font-size:11px;font-weight:600;">
                ⚠ Overdue
            </div>`;
        }

        return `
        <div style="position:relative;background:${sevStyle.bg};border:1px solid ${sevStyle.border};
                    border-radius:12px;padding:12px 12px 12px 14px;margin-bottom:10px;
                    box-shadow:0 1px 2px rgba(15,23,42,.04);overflow:hidden;">
            <div style="position:absolute;left:0;top:0;bottom:0;width:4px;background:${sevAccent.bar};"></div>
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
                <div style="font-weight:700;color:#0f172a;font-size:13px;letter-spacing:-0.01em;
                            min-width:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                    ${t.cp_id || '—'}
                </div>
                <span style="flex-shrink:0;background:${sColor};color:#fff;font-size:10px;
                            padding:3px 9px;border-radius:999px;font-weight:600;white-space:nowrap;
                            letter-spacing:0.01em;">
                    ${statusLabel}
                </span>
            </div>
            <div style="color:#334155;font-size:11px;margin-top:4px;line-height:1.4;word-break:break-word;">
                ${t.model_name || ''} · #${t.ticket_id || ''}
                ${t.creator ? ` · ${t.creator}` : ''}
            </div>
            <div style="margin-top:10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <span style="display:inline-flex;align-items:center;gap:4px;
                             background:${sevAccent.chip};color:${sevAccent.text};
                             font-size:12px;font-weight:700;padding:4px 10px;border-radius:8px;">
                    <span style="opacity:.85;">⏱</span> ${t.duration || '—'}
                </span>
            </div>
            <div style="margin-top:8px;color:#334155;font-size:12.5px;line-height:1.5;
                        display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;">
                ${t.description || ''}
            </div>
            ${t.owners ? `
            <div style="margin-top:8px;display:flex;gap:6px;align-items:flex-start;">
                <span style="flex-shrink:0;font-size:12px;line-height:1.4;">👥</span>
                <div style="color:#475569;font-size:11px;line-height:1.4;word-break:break-word;">
                    ${t.owners}
                </div>
            </div>` : ''}
            ${nearOverdueHtml}
        </div>`;
    }).join('');

    const addressBlock = stationAddress
        ? `<div style="display:flex;gap:8px;align-items:flex-start;margin-top:10px;">
               <span style="flex-shrink:0;width:22px;height:22px;border-radius:6px;background:#f1f5f9;
                            display:flex;align-items:center;justify-content:center;font-size:11px;">📌</span>
               <div style="color:#64748b;font-size:12px;line-height:1.45;
                           display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;">
                   ${stationAddress}
               </div>
           </div>`
        : '';

    return `
    <div class="sp-card" style="font-family:Inter,system-ui,-apple-system,'Segoe UI',sans-serif;
                width:300px;max-width:88vw;box-sizing:border-box;margin:-2px;">
        <!-- Header tô màu đặc theo mức độ nghiêm trọng của trạm
             (đỏ/cam/xanh theo giờ tồn đọng, TÍM ĐẬM nếu có ticket Open-overdue
             EV chưa có bất kỳ thông tin xử lý nào) -->
        <div style="background:${accent.bar};border-radius:14px 14px 0 0;margin:-14px -20px 0 -20px;
                    padding:14px 18px 12px;">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
                <div style="display:flex;align-items:center;gap:6px;min-width:0;">
                    <a href="${gmapUrl}" target="_blank" rel="noopener noreferrer" title="Mở Google Maps"
                       style="color:#fff;text-decoration:none;font-size:16px;font-weight:700;
                              letter-spacing:-0.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                        ${s.station_code}
                    </a>
                    <button type="button" data-copy-code="${escapeHtmlAttr(s.station_code)}" title="Sao chép mã trạm"
                            style="flex-shrink:0;border:none;background:rgba(255,255,255,.22);color:#fff;
                                   width:20px;height:20px;border-radius:6px;font-size:10px;cursor:pointer;
                                   display:flex;align-items:center;justify-content:center;padding:0;">📋</button>
                </div>
                <span style="flex-shrink:0;background:#fff;color:${accent.text};font-size:11px;font-weight:700;
                             padding:4px 10px;border-radius:999px;white-space:nowrap;">
                    ${ticketCount} ticket${ticketCount !== 1 ? 's' : ''}
                </span>
            </div>
            <div style="margin-top:6px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                <span style="color:rgba(255,255,255,.92);font-size:12px;">🧑‍🔧 ${s.tech_name || 'Unassigned'}</span>
                ${isUnassigned
                    ? `<span style="background:rgba(255,255,255,.25);color:#fff;font-size:10.5px;font-weight:600;
                                   padding:2px 8px;border-radius:999px;">Chưa gán</span>`
                    : ''}
            </div>
        </div>

        <!-- Body -->
        <div style="padding:12px 0 0;">
            ${addressBlock}
        </div>

        <!-- Divider -->
        <div style="height:1px;background:#f1f5f9;margin:0 -4px 12px;"></div>

        <!-- Ticket list -->
        <div style="max-height:260px;overflow-y:auto;padding-right:2px;margin-right:-2px;
                    scrollbar-width:thin;scrollbar-color:#cbd5e1 transparent;">
            ${rowsHtml || `<div style="text-align:center;padding:20px 8px;color:#94a3b8;font-size:13px;">
                Không có ticket mở
            </div>`}
        </div>
    </div>`;
}

// Vẽ marker trạm dựa trên allStations + bộ lọc kỹ thuật viên hiện tại (selectedTechs)
// + bộ lọc loại trạm (showChargers/showBss)
// Mặc định CHỈ hiện trụ sạc EV — BSS tắt để giảm lag lúc vào map
let showChargerStations = true;
let showBssStations = true;

function applyStationFilter() {
    stationLayer.clearLayers();
    let stations = selectedTechs.size === 0
        ? allStations
        : allStations.filter((s) => selectedTechs.has(s.tech_name || 'Unassigned'));

    stations = stations.filter((s) => (s.is_bss_station ? showBssStations : showChargerStations));

    // Dựng hết marker vào mảng rồi thêm 1 lần bằng addLayers() - nhanh hơn
    // hẳn addTo() từng marker khi có hàng ngàn trạm (markerClusterGroup phải
    // tính lại cụm mỗi lần addLayer đơn lẻ).
    const markers = stations.map((s) => {
        const icon = s.is_unassigned
            ? unassignedStationIcon(s.has_near_overdue)
            : stationIcon(s.color, s.has_near_overdue, s.has_no_info_critical);
        const marker = L.marker([s.lat, s.lng], { icon });
        // Hàm callback: Leaflet chỉ gọi buildStationPopup(s) khi popup thực sự
        // được mở, không tốn CPU dựng HTML sẵn cho toàn bộ trạm mỗi lần vẽ lại.
        marker.bindPopup(() => buildStationPopup(s), {
            maxWidth: 340,
            maxHeight: 420,
            className: 'station-popup station-popup-v2',
            autoPanPadding: [24, 24],
            closeButton: false,
        });
        marker._stationCode = s.station_code;
        return marker;
    });
    stationLayer.addLayers(markers);
}

const chargerTypeCheckbox = document.getElementById('filter-charger');
const bssTypeCheckbox = document.getElementById('filter-bss');
if (chargerTypeCheckbox) {
    chargerTypeCheckbox.checked = showChargerStations;
    chargerTypeCheckbox.addEventListener('change', () => {
        showChargerStations = chargerTypeCheckbox.checked;
        applyStationFilter();
    });
}
if (bssTypeCheckbox) {
    bssTypeCheckbox.checked = showBssStations; // false mặc định
    bssTypeCheckbox.addEventListener('change', () => {
        showBssStations = bssTypeCheckbox.checked;
        applyStationFilter();
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
    'kỹ thuật': '#2563eb',
    'điều phối khu vực': '#16a34a',
    'điều hành': '#ea580c',
    'giám đốc': '#7c3aed',
    'admin': '#1e293b',
};

function staffIcon(name, role) {
    const color = ROLE_COLORS[(role || '').trim().toLowerCase()] || '#64748b';
    return L.divIcon({
        className: '',
        html: `<div class="staff-avatar" style="width:32px;height:32px;background:${color};font-size:12px;">${initials(name)}</div>`,
        iconSize: [32, 32],
        iconAnchor: [16, 16],
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
    const color = ROLE_COLORS[(loc.role || '').trim().toLowerCase()] || '#64748b';

    let workingNote = '';
    if (loc.nearby_station) {
        const durationText = loc.nearby_since ? formatDuration(loc.nearby_since) : '';
        workingNote = `
        <div style="margin-top:10px;padding:10px 12px;background:#fff7ed;border-radius:8px;
                    border-left:3px solid #ea580c;font-size:12.5px;line-height:1.45;">
            <div style="font-weight:600;color:#c2410c;margin-bottom:2px;">🔧 Đang sửa chữa</div>
            Trạm <b>${loc.nearby_station}</b>
            ${durationText ? `<div style="color:#9a3412;margin-top:3px;font-size:12px;">Đã ${durationText}</div>` : ''}
        </div>`;
    }

    let statsHtml = '';
    if ((loc.role || '').trim().toLowerCase() === 'kỹ thuật') {
        statsHtml = `
        <div style="margin-top:12px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;">
            <div style="background:#f1f5f9;border-radius:8px;padding:8px 4px;text-align:center;">
                <div style="font-size:16px;font-weight:700;color:#1e293b;line-height:1.2;">${loc.open_count ?? 0}</div>
                <div style="font-size:10px;color:#64748b;margin-top:2px;">🔧 Đang tồn</div>
            </div>
            <div style="background:#f0fdf4;border-radius:8px;padding:8px 4px;text-align:center;">
                <div style="font-size:16px;font-weight:700;color:#16a34a;line-height:1.2;">${loc.closed_today ?? 0}</div>
                <div style="font-size:10px;color:#64748b;margin-top:2px;">✅ Hôm nay</div>
            </div>
            <div style="background:#f8fafc;border-radius:8px;padding:8px 4px;text-align:center;">
                <div style="font-size:16px;font-weight:700;color:#64748b;line-height:1.2;">${loc.closed_yesterday ?? 0}</div>
                <div style="font-size:10px;color:#94a3b8;margin-top:2px;">✅ Hôm qua</div>
            </div>
        </div>`;
    }

    return `
    <div style="font-family:Inter,system-ui,-apple-system,sans-serif;width:240px;max-width:85vw;box-sizing:border-box;padding:2px;">
        <div style="background:${color};margin:-14px -14px 12px -14px;padding:12px 14px;border-radius:6px 6px 0 0;">
            <div style="color:#fff;font-weight:600;font-size:14.5px;letter-spacing:-0.01em;">${loc.full_name}</div>
            <div style="color:rgba(255,255,255,.85);font-size:12px;margin-top:3px;">
                ${loc.role || ''}${loc.region ? ' · ' + loc.region : ''}
            </div>
        </div>
        <a href="${gmapUrl}" target="_blank" rel="noopener noreferrer"
           style="display:inline-flex;align-items:center;gap:4px;font-size:12.5px;color:#2563eb;
                  text-decoration:none;font-weight:500;padding:4px 0;">
            🗺️ Xem trên Google Maps
        </a>
        ${statsHtml}
        ${workingNote}
    </div>`;
}

// Di chuyển marker mượt bằng nội suy tuyến tính
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

    entry.marker.bindPopup(buildStaffPopup(loc), { maxWidth: 280, className: 'staff-popup' });

    if (loc.nearby_station) {
        const wrenchLatLng = L.latLng(loc.lat + 0.00015, loc.lng + 0.00015);
        if (!entry.wrenchMarker) {
            entry.wrenchMarker = L.marker(wrenchLatLng, { icon: wrenchIcon(), zIndexOffset: 600 }).addTo(map);
        } else {
            animateMarkerTo(entry.wrenchMarker, wrenchLatLng, 1200);
        }
        const durationText = loc.nearby_since ? ` (đã ${formatDuration(loc.nearby_since)})` : '';
        entry.wrenchMarker.bindTooltip(`🔧 ${loc.full_name} đang sửa trạm ${loc.nearby_station}${durationText}`, {
            className: 'wrench-tooltip',
            direction: 'top',
            offset: [0, -8],
        });
    } else if (entry.wrenchMarker) {
        map.removeLayer(entry.wrenchMarker);
        entry.wrenchMarker = null;
    }
}

// ---------- Tag lọc theo kỹ thuật viên (accordion theo khu vực) ----------
const techPanel = document.getElementById('tech-filter-panel');
const techFilterHandle = document.getElementById('tech-filter-handle');

if (techPanel) {
    L.DomEvent.disableScrollPropagation(techPanel);
    L.DomEvent.disableClickPropagation(techPanel);
}

function techDotHtml(online) {
    if (online === true) return '<span class="conn-dot ok" title="Đang online"></span>';
    if (online === false) return '<span class="conn-dot" title="Đang offline"></span>';
    return '<span class="conn-dot" style="background:#94a3b8;" title="Không xác định"></span>';
}

function isTechFilterOpen() {
    return techPanel && techPanel.classList.contains('open');
}

function setTechFilterOpen(open) {
    if (!techPanel) return;
    techPanel.classList.toggle('open', open);
    techPanel.style.display = open ? 'flex' : 'none';
    if (techFilterHandle) techFilterHandle.classList.toggle('open', open);
}

function renderTechPanel(data) {
    const regions = data.regions || {};
    let bodyHtml = '';

    Object.keys(regions).sort().forEach((region) => {
        const techs = regions[region];
        const techNames = techs.map((item) => item.tech_name);
        bodyHtml += `
        <div class="region-group" data-region="${region}">
            <div class="region-header">
                <input type="checkbox" class="region-check region-checkbox" data-region="${region}" onclick="event.stopPropagation()">
                <span>📍 ${region}</span>
                <span class="region-count">${techs.length}</span>
                <span class="chevron">▶</span>
            </div>
            <div class="region-techs">`;
        techs.forEach((item) => {
            const uname = (item.username || '').toLowerCase();
            bodyHtml += `
                <label data-tech="${item.tech_name}" ${uname ? `data-username="${uname}"` : ''}>
                    <input type="checkbox" class="tech-checkbox" data-region="${region}" value="${item.tech_name}">
                    ${techDotHtml(item.online)} ${item.tech_name}
                </label>`;
        });
        bodyHtml += `</div></div>`;
    });

    if (data.unassigned) {
        bodyHtml += `
        <div class="region-group" data-region="__unassigned__">
            <div class="region-header">
                <input type="checkbox" class="region-check region-checkbox" data-region="__unassigned__" onclick="event.stopPropagation()">
                <span>🆕 Khác</span>
                <span class="chevron">▶</span>
            </div>
            <div class="region-techs">
                <label data-tech="Unassigned">
                    <input type="checkbox" class="tech-checkbox" data-region="__unassigned__" value="Unassigned">
                    ${techDotHtml(null)} Unassigned
                </label>
            </div>
        </div>`;
    }

    techPanel.innerHTML = `
        <div class="panel-head-text" style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
            <h3 style="margin:0;flex:1;">🧑‍🔧 Lọc kỹ thuật viên</h3>
            <button type="button" id="tech-filter-close" title="Đóng"
                    style="flex-shrink:0;width:32px;height:32px;border:none;border-radius:8px;
                           background:#f1f5f9;color:#64748b;font-size:18px;line-height:1;
                           cursor:pointer;display:flex;align-items:center;justify-content:center;
                           padding:0;-webkit-tap-highlight-color:transparent;"
                    aria-label="Đóng bộ lọc">
                ✕
            </button>
        </div>
        <div class="panel-body">${bodyHtml}</div>
        <div id="tech-filter-actions">
            <button type="button" id="tech-select-all">Chọn tất cả</button>
            <button type="button" id="tech-clear-all">Bỏ chọn</button>
        </div>`;

    const closeBtn = document.getElementById('tech-filter-close');
    if (closeBtn) {
        closeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            setTechFilterOpen(false);
        });
    }

    // Accordion: click region header (except checkbox) to expand/collapse
    techPanel.querySelectorAll('.region-header').forEach((header) => {
        header.addEventListener('click', (e) => {
            if (e.target.classList.contains('region-check') || e.target.classList.contains('region-checkbox')) return;
            header.parentElement.classList.toggle('expanded');
        });
    });

    techPanel.querySelectorAll('.tech-checkbox').forEach((cb) => {
        cb.checked = selectedTechs.has(cb.value);
        cb.addEventListener('change', () => {
            if (cb.checked) selectedTechs.add(cb.value);
            else selectedTechs.delete(cb.value);
            syncRegionCheckboxState(cb.dataset.region);
            applyStationFilter();
        });
    });

    techPanel.querySelectorAll('.region-checkbox').forEach((regionCb) => {
        syncRegionCheckboxState(regionCb.dataset.region);
        regionCb.addEventListener('change', () => {
            const region = regionCb.dataset.region;
            techPanel.querySelectorAll(`.tech-checkbox[data-region="${region}"]`).forEach((cb) => {
                cb.checked = regionCb.checked;
                if (regionCb.checked) selectedTechs.add(cb.value);
                else selectedTechs.delete(cb.value);
            });
            // Auto-expand when selecting whole region
            if (regionCb.checked) {
                const group = regionCb.closest('.region-group');
                if (group) group.classList.add('expanded');
            }
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
            techPanel.querySelectorAll('.region-checkbox').forEach((cb) => { cb.checked = true; });
            techPanel.querySelectorAll('.region-group').forEach((g) => g.classList.add('expanded'));
            applyStationFilter();
        });
    }
    if (clearAllBtn) {
        clearAllBtn.addEventListener('click', () => {
            techPanel.querySelectorAll('.tech-checkbox').forEach((cb) => { cb.checked = false; });
            techPanel.querySelectorAll('.region-checkbox').forEach((cb) => { cb.checked = false; cb.indeterminate = false; });
            selectedTechs.clear();
            applyStationFilter();
        });
    }

    updateTechPanelPresence();
}

function syncRegionCheckboxState(region) {
    if (!region || !techPanel) return;
    const regionCb = techPanel.querySelector(`.region-checkbox[data-region="${region}"]`);
    if (!regionCb) return;
    const techCbs = [...techPanel.querySelectorAll(`.tech-checkbox[data-region="${region}"]`)];
    const checkedCount = techCbs.filter((cb) => cb.checked).length;
    regionCb.checked = techCbs.length > 0 && checkedCount === techCbs.length;
    regionCb.indeterminate = checkedCount > 0 && checkedCount < techCbs.length;
}

function updateTechPanelPresence() {
    if (!techPanel) return;
    techPanel.querySelectorAll('label[data-username]').forEach((label) => {
        const uname = label.dataset.username;
        const dot = label.querySelector('.conn-dot');
        if (!dot) return;
        const online = onlineUsernames.has(uname);
        dot.classList.toggle('ok', online);
        dot.style.background = online ? '' : '#ef4444';
        dot.title = online ? 'Đang online' : 'Đang offline';
    });
}

function loadTechnicianPanel() {
    fetch('/api/technicians')
        .then((r) => r.json())
        .then(renderTechPanel)
        .catch(() => {
            techPanel.innerHTML = '<div style="padding:20px;font-size:13.5px;color:#94a3b8;text-align:center;">Không tải được danh sách kỹ thuật viên.</div>';
        });
}

if (techFilterHandle) {
    techFilterHandle.addEventListener('click', (e) => {
        e.stopPropagation();
        const willOpen = !isTechFilterOpen();
        setTechFilterOpen(willOpen);
        if (willOpen && !techPanel.dataset.loaded) {
            techPanel.dataset.loaded = '1';
            loadTechnicianPanel();
        }
    });
}

// Click outside closes panel (but not when clicking handle)
document.addEventListener('click', (e) => {
    if (!isTechFilterOpen()) return;
    if (techPanel.contains(e.target)) return;
    if (techFilterHandle && techFilterHandle.contains(e.target)) return;
    setTechFilterOpen(false);
});

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
    let target = null;
    stationLayer.eachLayer((layer) => {
        if (layer._stationCode === stationCode) target = layer;
    });
    if (!target) return;
    // zoomToShowLayer: nếu marker đang bị gộp trong 1 cụm (chưa đủ zoom để
    // tách), hàm này tự zoom/pan tới mức marker hiện ra riêng lẻ rồi mới gọi
    // callback - mở thẳng openPopup() như flyTo() cũ sẽ không thấy gì nếu
    // marker còn đang ẩn trong cụm.
    stationLayer.zoomToShowLayer(target, () => target.openPopup());
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
                    setTechFilterOpen(true);
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

// ---------- Panel "Danh sách Ticket" ----------
const ticketPanel = document.getElementById('ticket-panel');
const ticketPanelHandle = document.getElementById('ticket-panel-handle');
const ticketPanelClose = document.getElementById('ticket-panel-close');
const ticketPanelTableWrap = document.getElementById('ticket-panel-table-wrap');
const ticketTechSearch = document.getElementById('ticket-tech-search');
const ticketTechDropdown = document.getElementById('ticket-tech-dropdown');
const ticketTechSelectWrap = document.getElementById('ticket-tech-select-wrap');
const ticketTechClear = document.getElementById('ticket-tech-clear');
const ticketSelectedInfo = document.getElementById('ticket-selected-info');
const ticketSelName = document.getElementById('ticket-sel-name');
const ticketSelCount = document.getElementById('ticket-sel-count');

let ticketTechData = null;   // raw data from /api/technicians
let ticketSelectedTech = null;
let ticketPanelLoaded = false;

if (ticketPanel) {
    L.DomEvent.disableScrollPropagation(ticketPanel);
    L.DomEvent.disableClickPropagation(ticketPanel);
}

function toggleTicketPanel(forceOpen) {
    const shouldOpen = forceOpen !== undefined ? forceOpen : !ticketPanel.classList.contains('open');
    ticketPanel.classList.toggle('open', shouldOpen);
    ticketPanelHandle.style.display = shouldOpen ? 'none' : 'block';
    if (shouldOpen && !ticketPanelLoaded) {
        ticketPanelLoaded = true;
        loadTicketPanelTechList();
    }
}

if (ticketPanelHandle) ticketPanelHandle.addEventListener('click', () => toggleTicketPanel(true));
if (ticketPanelClose) ticketPanelClose.addEventListener('click', () => toggleTicketPanel(false));

function durationCellHtml(t) {
    const hours = Number(t.hours) || 0;
    let color = '#16a34a';      // < 24h green
    let bg = '#f0fdf4';
    let label = '';
    if (hours > 48 || t.is_no_info_critical) {
        color = '#dc2626';
        bg = '#fef2f2';
        label = t.is_no_info_critical ? 'Overdue · Chưa có thông tin' : 'Overdue';
    } else if (hours >= 24) {
        color = '#ea580c';
        bg = '#fff7ed';
        label = 'Nguy hiểm';
    }
    const warnBadge = t.is_near_overdue
        ? `<div style="margin-top:3px;color:#c2410c;font-weight:600;font-size:10.5px;">⏰ Sắp quá hạn (~${(48 - hours).toFixed(1)}h)</div>`
        : (label && (hours > 48 || t.is_no_info_critical)
            ? `<div style="margin-top:3px;color:${color};font-weight:600;font-size:10.5px;display:inline-flex;align-items:center;gap:4px;">${t.is_no_info_critical ? `<span style="display:inline-flex;width:12px;height:14px;">${FLAG_SVG}</span>` : ''}${label}</div>`
            : '');
    return `<td style="white-space:nowrap;background:${bg} !important;">
        <span style="color:${color};font-weight:700;">${t.duration ?? ''}</span>
        ${warnBadge}
    </td>`;
}

function renderTicketTable(tickets, techName) {
    if (!tickets || tickets.length === 0) {
        ticketPanelTableWrap.innerHTML = `
            <div id="ticket-panel-empty">
                <div class="empty-icon">✅</div>
                Không có ticket nào cho "<b>${techName}</b>".
            </div>`;
        return;
    }
    const rowsHtml = tickets.map((t) => {
        const hours = Number(t.hours) || 0;
        const rowBg = (hours > 48 || t.is_no_info_critical)
            ? 'background:#fef2f2 !important;'
            : (hours >= 24 ? 'background:#fffbeb !important;' : '');
        const statusLabel = t.status_display || t.status || '';
        // Mô tả lỗi/Địa chỉ giới hạn 2 dòng (line-clamp) thay vì wrap vô hạn -
        // trước đây 1 dòng mô tả dài làm CẢ HÀNG cao vọt (HTML table: các ô
        // cùng hàng luôn cao bằng ô cao nhất), lãng phí không gian hiển thị dù
        // các cột khác (Mã Ticket, Trạng thái...) chỉ có 1 dòng chữ ngắn.
        // title="" giữ nguyên văn đầy đủ, xem khi rê chuột vào phần bị cắt.
        const clampCell = (text, maxWidth) => `
            <td style="max-width:${maxWidth}px;line-height:1.45;overflow:hidden;
                display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;"
                title="${escapeHtmlAttr(text ?? '')}">${text ?? ''}</td>`;
        return `
        <tr style="${rowBg}">
            <td style="font-weight:500;white-space:nowrap;position:sticky;left:0;${rowBg || 'background:#fff;'}">${t.ticket_id ?? ''}</td>
            ${durationCellHtml(t)}
            <td style="white-space:nowrap;">${t.station_code ?? ''}</td>
            <td style="white-space:nowrap;">${t.cp_id ?? ''}</td>
            <td style="white-space:nowrap;${t.is_no_info_critical ? 'color:#dc2626;font-weight:700;' : ''}">${statusLabel}</td>
            ${clampCell(t.description, 320)}
            ${clampCell(t.address, 280)}
        </tr>
    `;
    }).join('');

    ticketPanelTableWrap.innerHTML = `
        <table>
            <thead>
                <tr>
                    <th>Mã Ticket</th>
                    <th>Thời gian tồn</th>
                    <th>Mã Trạm</th>
                    <th>SN Trụ</th>
                    <th>Trạng thái</th>
                    <th>Mô tả lỗi</th>
                    <th>Địa chỉ</th>
                </tr>
            </thead>
            <tbody>${rowsHtml}</tbody>
        </table>
    `;
}

function selectTicketTech(techName, openCount) {
    ticketSelectedTech = techName;
    if (ticketTechSearch) ticketTechSearch.value = techName;
    if (ticketTechSelectWrap) ticketTechSelectWrap.classList.add('has-value');
    closeTicketTechDropdown();

    if (ticketSelectedInfo) {
        ticketSelectedInfo.style.display = 'flex';
        if (ticketSelName) ticketSelName.textContent = techName;
        if (ticketSelCount) ticketSelCount.textContent = `🔧 ${openCount ?? 0}`;
    }

    ticketPanelTableWrap.innerHTML = '<div id="ticket-panel-empty"><div class="empty-icon">⏳</div>Đang tải...</div>';
    fetch(`/api/tech-tickets/${encodeURIComponent(techName)}`)
        .then((r) => r.json())
        .then((data) => {
            renderTicketTable(data.tickets, techName);
            // update count from actual tickets if available
            if (ticketSelCount && data.tickets) {
                ticketSelCount.textContent = `🔧 ${data.tickets.length}`;
            }
        })
        .catch(() => {
            ticketPanelTableWrap.innerHTML = '<div id="ticket-panel-empty"><div class="empty-icon">❌</div>Không tải được danh sách ticket.</div>';
        });
}

function closeTicketTechDropdown() {
    if (ticketTechDropdown) ticketTechDropdown.classList.remove('open');
}

function renderTicketTechDropdown(filterText) {
    if (!ticketTechDropdown || !ticketTechData) return;
    const q = (filterText || '').trim().toLowerCase();
    const regions = ticketTechData.regions || {};
    let html = '';
    let totalShown = 0;

    Object.keys(regions).sort().forEach((region) => {
        const items = regions[region].filter((item) => {
            if (!q) return true;
            return (item.tech_name || '').toLowerCase().includes(q);
        });
        if (items.length === 0) return;
        html += `<div class="region-group-label">📍 ${region}</div>`;
        items.forEach((item) => {
            const selected = item.tech_name === ticketSelectedTech ? ' selected' : '';
            html += `
            <div class="tech-option${selected}" data-tech="${item.tech_name}" data-count="${item.open_count ?? 0}">
                <span>${techDotHtml(item.online)} ${item.tech_name}</span>
                <span class="opt-stats">🔧 ${item.open_count ?? 0}</span>
            </div>`;
            totalShown++;
        });
    });

    if (ticketTechData.unassigned) {
        const name = 'Unassigned';
        if (!q || name.toLowerCase().includes(q)) {
            const selected = name === ticketSelectedTech ? ' selected' : '';
            html += `<div class="region-group-label">🆕 Khác</div>
                <div class="tech-option${selected}" data-tech="${name}" data-count="${ticketTechData.unassigned.open_count ?? 0}">
                    <span>${name}</span>
                    <span class="opt-stats">🔧 ${ticketTechData.unassigned.open_count ?? 0}</span>
                </div>`;
            totalShown++;
        }
    }

    if (totalShown === 0) {
        html = '<div class="empty-msg">Không tìm thấy kỹ thuật viên</div>';
    }
    ticketTechDropdown.innerHTML = html;
    ticketTechDropdown.classList.add('open');

    ticketTechDropdown.querySelectorAll('.tech-option').forEach((opt) => {
        opt.addEventListener('click', () => {
            selectTicketTech(opt.dataset.tech, parseInt(opt.dataset.count, 10) || 0);
        });
    });
}

function loadTicketPanelTechList() {
    fetch('/api/technicians')
        .then((r) => r.json())
        .then((data) => {
            ticketTechData = data;
            // Auto-select if only one tech
            const regions = data.regions || {};
            const allItems = Object.values(regions).flat();
            if (allItems.length === 1) {
                selectTicketTech(allItems[0].tech_name, allItems[0].open_count);
            }
        })
        .catch(() => {
            ticketPanelTableWrap.innerHTML = '<div id="ticket-panel-empty"><div class="empty-icon">❌</div>Không tải được danh sách kỹ thuật viên.</div>';
        });
}

// Tech search input interactions
if (ticketTechSearch) {
    ticketTechSearch.addEventListener('focus', () => {
        if (ticketTechData) renderTicketTechDropdown(ticketTechSearch.value);
        else loadTicketPanelTechList();
    });
    ticketTechSearch.addEventListener('input', () => {
        if (ticketTechSelectWrap) {
            ticketTechSelectWrap.classList.toggle('has-value', !!ticketTechSearch.value);
        }
        renderTicketTechDropdown(ticketTechSearch.value);
    });
    ticketTechSearch.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeTicketTechDropdown();
            ticketTechSearch.blur();
        }
    });
}
if (ticketTechClear) {
    ticketTechClear.addEventListener('click', () => {
        ticketSelectedTech = null;
        if (ticketTechSearch) ticketTechSearch.value = '';
        if (ticketTechSelectWrap) ticketTechSelectWrap.classList.remove('has-value');
        if (ticketSelectedInfo) ticketSelectedInfo.style.display = 'none';
        closeTicketTechDropdown();
        ticketPanelTableWrap.innerHTML = `
            <div id="ticket-panel-empty">
                <div class="empty-icon">🧑‍🔧</div>
                Chọn kỹ thuật viên ở trên để xem danh sách ticket.
            </div>`;
        if (ticketTechSearch) ticketTechSearch.focus();
    });
}
document.addEventListener('click', (e) => {
    if (ticketTechSelectWrap && !ticketTechSelectWrap.contains(e.target)) {
        closeTicketTechDropdown();
    }
});

// ---------- Badge "Sự cố đang mở" + panel trạm thiếu toạ độ ----------
let missingCoordTickets = [];

function formatUpdatedAt(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return iso;
        // Luôn hiển thị theo giờ Việt Nam (UTC+7), tránh lệch múi giờ server/UTC
        const parts = new Intl.DateTimeFormat('en-GB', {
            timeZone: 'Asia/Ho_Chi_Minh',
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit', second: '2-digit',
            hour12: false,
        }).formatToParts(d);
        const get = (type) => (parts.find((p) => p.type === type) || {}).value || '';
        return `${get('day')}/${get('month')}/${get('year')} ${get('hour')}:${get('minute')}:${get('second')}`;
    } catch (_) {
        return iso;
    }
}

function updateTicketBadge(data) {
    const el = document.getElementById('ticket-count');
    if (el) {
        const total = data.total_tickets ?? 0;
        const withCoords = data.with_coords_count ?? total;
        el.textContent = withCoords === total ? `${total}` : `${withCoords}/${total}`;
    }
    missingCoordTickets = data.missing_coord_tickets || [];

    const formatted = data.updated_at ? formatUpdatedAt(data.updated_at) : null;
    const ticketUpdatedEl = document.getElementById('ticket-updated-at');
    if (ticketUpdatedEl && formatted) {
        ticketUpdatedEl.textContent = formatted;
    }
    const headerUpdatedEl = document.getElementById('header-updated-at');
    if (headerUpdatedEl && formatted) {
        headerUpdatedEl.textContent = '🕒 ' + formatted;
    }
}

const missingCoordPanel = document.getElementById('missing-coord-panel');
if (missingCoordPanel) {
    L.DomEvent.disableClickPropagation(missingCoordPanel);
}

function renderMissingCoordPanel() {
    if (!missingCoordPanel) return;
    if (missingCoordTickets.length === 0) {
        missingCoordPanel.innerHTML = `<div class="header">✅ Trạm thiếu toạ độ</div>
            <div class="empty">Không có ticket nào bị thiếu toạ độ trạm.</div>`;
        return;
    }
    const itemsHtml = missingCoordTickets.map((t) => `
        <div class="item">
            <span class="tid">${t.ticket_id ?? ''}</span>
            <span class="meta">Mã trạm: ${t.station_code ?? '—'}  ·  SN trụ: ${t.cp_id ?? '—'}</span>
        </div>
    `).join('');
    missingCoordPanel.innerHTML = `
        <div class="header">⚠️ ${missingCoordTickets.length} ticket thuộc trạm thiếu toạ độ</div>
        ${itemsHtml}
    `;
}

const ticketCountBadge = document.getElementById('ticket-count');
if (ticketCountBadge) {
    ticketCountBadge.addEventListener('click', (e) => {
        e.stopPropagation();
        const isOpen = missingCoordPanel.style.display === 'block';
        if (!isOpen) renderMissingCoordPanel();
        missingCoordPanel.style.display = isOpen ? 'none' : 'block';
    });
}
document.addEventListener('click', (e) => {
    if (missingCoordPanel && missingCoordPanel.style.display === 'block'
        && !missingCoordPanel.contains(e.target) && e.target !== ticketCountBadge) {
        missingCoordPanel.style.display = 'none';
    }
});

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
            updateTicketBadge(msg);
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

// Tải dữ liệu trạm ban đầu qua REST
fetch('/api/stations')
    .then((r) => r.json())
    .then((data) => {
        renderStations(data.stations);
        updateTicketBadge(data);
    })
    .catch(() => {});

// ---------- Theo dõi vị trí ----------
// ĐÃ BỎ: trình duyệt không còn tự lấy GPS (navigator.geolocation.watchPosition)
// và gửi lên server nữa, để tránh tốn pin/dung lượng khi mở web. Vị trí trên
// bản đồ giờ CHỈ đến từ app Traccar Client (server nhận qua GET /api/traccar
// rồi broadcast xuống qua WebSocket message "location_update" như cũ - phần
// đó ở trên không đổi).