/**
 * stats_chart_heatmap.js — module "Bản đồ nhiệt": mật độ ticket & ticket
 * Đã tối giản UI: Ẩn panel bên phải, đưa Control lên map, ẩn marker trạm.
 */
(function () {
  "use strict";
  const Core = window.StatsCore;
  const { setKPIs, escapeHtml } = Core;

  Core.injectStyleOnce("stats-style-heatmap", `
    .heatmap-body {
      display: block; width: 100%;
    }
    .heatmap-map-wrap {
      position: relative; height: 600px; width: 100%;
      border-radius: 10px; overflow: hidden; border: 1px solid var(--border);
    }
    /* Đổi nền thành màu sáng nhẹ để nổi bật màu nhiệt */
    .heatmap-map { height: 100%; width: 100%; background: #f8fafc; }
    
    /* Box điều khiển nổi trên bản đồ */
    .heatmap-map-overlay {
      position: absolute;
      bottom: 24px;
      right: 16px;
      z-index: 1000; /* Hiển thị đè lên Leaflet */
      background: rgba(255, 255, 255, 0.95);
      backdrop-filter: blur(4px);
      padding: 14px 18px;
      border-radius: 10px;
      box-shadow: 0 4px 15px rgba(0,0,0,0.15);
      border: 1px solid rgba(0,0,0,0.05);
      pointer-events: auto; /* Cho phép click */
    }
    .heatmap-legend {
      display: flex; align-items: center; gap: 8px;
      font-size: 11.5px; font-weight: 500; color: #334155; flex-wrap: wrap;
    }
    .heatmap-gradient-bar { display: inline-block; width: 140px; height: 10px; border-radius: 6px; vertical-align: middle; }
    .heatmap-gradient-bar.vol { background: linear-gradient(90deg,#fffbeb,#fde68a,#fbbf24,#f59e0b,#f97316,#ef4444,#b91c1c); }
    .heatmap-gradient-bar.od { background: linear-gradient(90deg,#fff7ed,#fed7aa,#fca5a5,#ef4444,#dc2626,#b91c1c,#7f1d1d); }
    .heatmap-layer-toggle {
      display: flex; align-items: center; gap: 8px; margin-top: 12px;
      font-size: 12.5px; font-weight: 600; color: #1e293b; cursor: pointer; user-select: none;
    }
    .heatmap-layer-toggle input { cursor: pointer; width: 16px; height: 16px; accent-color: #3b82f6; }
    
    .heatmap-loadfail { padding: 40px 20px; text-align: center; color: #b91c1c; font-size: 13px; }
    .leaflet-tooltip { font-family: inherit; font-size: 12px; font-weight: 600; padding: 4px 8px; border-radius: 6px; }
    .leaflet-popup-content { font-family: inherit; font-size: 13px; min-width: 180px; }
    .eng-popup-title { font-weight: 700; font-size: 14px; margin-bottom: 6px; color: #1e293b; }
    .station-popup-row { margin: 4px 0; color: #475569; }
    .eng-pin { display: block; filter: drop-shadow(0 3px 4px rgba(0,0,0,.3)); }
  `);

  const REGIONS = Core.REGION_ORDER;
  const REGION_LABELS = {
    "DNA-QNA": "Đà Nẵng - Quảng Nam",
    "DNI-BPH": "Đồng Nai - Bình Phước",
    "LDO-BTH": "Lâm Đồng - Bình Thuận",
    "Tây Nguyên": "Tây Nguyên",
    "Mtay": "Miền Tây",
  };

  let fullPayload = null;
  let currentPayload = null;
  let currentView = "volume";
  let selectedRegion = REGIONS[0];
  let panelEl = null;
  let refs = null;

  let map = null;
  let heatLayer = null;
  let maskLayer = null;
  let boundaryLayer = null;
  let engineerLayer = null;
  let showEngineers = true;
  let leafletWarned = false;
  let turfWarned = false;

  function slicePayload(full, cp) {
    if (!full) return null;
    const by = full.by_cp_type || {};
    const sub = by[cp] || by.all || full;
    return Object.assign({}, full, sub, { cp_type: cp });
  }

  function currentRegionLayer() {
    if (!currentPayload) return null;
    const regionData = (currentPayload.regions || {})[selectedRegion];
    if (!regionData) return null;
    return { regionData, layer: regionData[currentView] || null };
  }

  function currentRegionBoundaryGeometry() {
    const map_ = currentPayload && currentPayload.region_boundaries;
    return (map_ && map_[selectedRegion]) || null;
  }

  // Icon Kỹ thuật viên siêu nhỏ (18x18px), phân loại 3 cấp độ màu sắc
  function engineerIcon(role) {
    const isLead = role === "lead";
    const isMember = role === "member";

    let fill = "#ef4444";
    let stroke = "#fff";
    let inner = "";

    if (isLead) {
      fill = "#7c3aed";
      stroke = "#f59e0b";
      inner = '<circle cx="17" cy="13" r="2.5" fill="#f59e0b"/>';
    } else if (isMember) {
      fill = "#0284c7";
      inner = '<circle cx="17" cy="13" r="2" fill="#fff"/>';
    }

    const svg = `
      <svg class="eng-pin" width="24" height="30" viewBox="0 0 34 42" xmlns="http://www.w3.org/2000/svg">
        <ellipse cx="17" cy="39" rx="7" ry="2" fill="rgba(0,0,0,.2)"/>
        <path d="M17 2C8.7 2 2 8.7 2 17c0 10.5 15 23 15 23s15-12.5 15-23C32 8.7 25.3 2 17 2z"
          fill="${fill}" stroke="${stroke}" stroke-width="2"/>
        <circle cx="17" cy="13" r="4" fill="#fff"/>
        <path d="M9.5 25c0-4.2 3.3-6.5 7.5-6.5s7.5 2.3 7.5 6.5z" fill="#fff"/>
        ${inner}
      </svg>`;

    return L.divIcon({
      className: "",
      html: svg,
      iconSize: [24, 30],
      iconAnchor: [12, 29],
      popupAnchor: [0, -26]
    });
  }
  // Hàm render marker kỹ thuật viên với ghi chú rõ ràng khi click/hover
  function renderEngineerMarkers() {
    if (!map || !engineerLayer) return;
    engineerLayer.clearLayers();
    if (!showEngineers) return;

    const engineers = currentPayload && currentPayload.engineers;
    const list = (engineers && engineers.by_region && engineers.by_region[selectedRegion]) || [];

    list.forEach((eng) => {
      let roleLabel = "Kỹ thuật viên";
      if (eng.role === "lead") {
        roleLabel = "⭐ <b>Trưởng nhóm</b>";
      } else if (eng.role === "member") {
        roleLabel = "🔹 <b>Thành viên nhóm</b> (TN: " + escapeHtml(eng.team_lead || "Nguyễn Hải Nguyên") + ")";
      }

      const approxNote = eng.approx
        ? '<div class="station-popup-row" style="color:#d97706;font-size:11px;margin-top:4px;">⚠ Đang tham chiếu từ khu vực lân cận</div>'
        : "";

      const popupHtml = (
        '<div class="eng-popup-title" style="font-size:13px;font-weight:700;">' + escapeHtml(eng.name) + '</div>' +
        '<div class="station-popup-row" style="font-size:12px;">' + roleLabel + '</div>' +
        approxNote
      );

      L.marker([eng.lat, eng.lng], { icon: engineerIcon(eng.role) })
        .bindTooltip(eng.name, { direction: "top", offset: [0, -10] })
        .bindPopup(popupHtml)
        .addTo(engineerLayer);
    });
  }

  function applyRegionMask(geometry) {
    if (maskLayer) { map.removeLayer(maskLayer); maskLayer = null; }
    if (boundaryLayer) { map.removeLayer(boundaryLayer); boundaryLayer = null; }
    if (!geometry) return null;

    if (!window.turf) {
      if (!turfWarned) {
        turfWarned = true;
        console.warn("[stats] turf.js chưa nạp — không cắt được bản đồ nhiệt theo ranh giới khu vực.");
      }
      return null;
    }

    const regionFeature = { type: "Feature", properties: {}, geometry };
    try {
      const masked = turf.mask(regionFeature);
      maskLayer = L.geoJSON(masked, {
        interactive: false,
        style: { color: "transparent", weight: 0, fillColor: "#f8fafc", fillOpacity: 0.96 },
      }).addTo(map);
      boundaryLayer = L.geoJSON(regionFeature, {
        interactive: false,
        style: { color: "#475569", weight: 1.5, opacity: 0.6, fill: false },
      }).addTo(map);
      if (maskLayer.bringToFront) maskLayer.bringToFront();
      if (boundaryLayer.bringToFront) boundaryLayer.bringToFront();
      return regionFeature;
    } catch (err) {
      console.warn("[stats] Lỗi tính mask ranh giới khu vực:", err);
      return null;
    }
  }

  function gradientFor(view) {
    return view === "overdue"
      ? { 0.0: "#fff7ed", 0.15: "#fed7aa", 0.32: "#fca5a5", 0.5: "#ef4444", 0.68: "#dc2626", 0.84: "#b91c1c", 1.0: "#7f1d1d" }
      : { 0.0: "#fffbeb", 0.15: "#fde68a", 0.32: "#fbbf24", 0.5: "#f59e0b", 0.68: "#f97316", 0.84: "#ef4444", 1.0: "#b91c1c" };
  }

  const HEAT_MAX_FACTOR = { volume: 0.6, overdue: 0.45 };
  const HEAT_MIN_OPACITY = { volume: 0.45, overdue: 0.5 }; // Tăng opacity để dễ nhìn hơn

  function radiusForZoom(zoom) {
    const r = Math.round(Math.min(50, Math.max(32, zoom * 4.4)));
    return { radius: r, blur: Math.round(r * 0.9) }; // Giảm blur nhẹ để gom nhiệt tốt hơn
  }

  function ensureMap() {
    if (map || !refs.mapEl) return;
    if (!window.L) {
      if (!leafletWarned) {
        leafletWarned = true;
        console.warn("[stats] Leaflet chưa tải được.");
      }
      return;
    }
    map = L.map(refs.mapEl, { center: [16.0, 106.0], zoom: 5, zoomControl: true, attributionControl: true });
    
    // Đã chuyển sang sử dụng CartoDB Positron (Nền đơn sắc) để màu nhiệt nổi lên tuyệt đối
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: "&copy; OpenStreetMap",
    }).addTo(map);
    
    engineerLayer = L.layerGroup().addTo(map);
    map.on("zoomend", () => {
      if (!heatLayer) return;
      const { radius, blur } = radiusForZoom(map.getZoom());
      heatLayer.setOptions({ radius, blur });
    });
  }

  function drawMap() {
    ensureMap();
    if (!map) {
      if (refs.mapEl) refs.mapEl.innerHTML = '<div class="heatmap-loadfail">Không tải được thư viện bản đồ.</div>';
      return;
    }

    const found = currentRegionLayer();
    const layer = found ? found.layer : null;
    const bounds = layer && layer.bounds;
    const boundaryGeom = currentRegionBoundaryGeometry();

    if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }

    let fitted = false;
    if (boundaryGeom && window.turf) {
      try {
        const [minX, minY, maxX, maxY] = turf.bbox({ type: "Feature", properties: {}, geometry: boundaryGeom });
        map.fitBounds([[minY, minX], [maxY, maxX]], { animate: false, padding: [10, 10] });
        fitted = true;
      } catch (err) {}
    }
    if (!fitted && bounds) {
      map.fitBounds([[bounds.min_lat, bounds.min_lng], [bounds.max_lat, bounds.max_lng]], { animate: false });
    }

    const rawPts = (layer && layer.heat_points) || [];
    if (rawPts.length && window.L && L.heatLayer) {
      const pts = rawPts.map((p) => [p[0], p[1], Math.sqrt(Math.max(1, p[2]))]);
      const maxCount = Math.max(1, (layer && layer.max_count) || 1);
      const heatMax = Math.max(1.1, Math.sqrt(maxCount) * HEAT_MAX_FACTOR[currentView]);
      const { radius, blur } = radiusForZoom(map.getZoom());

      heatLayer = L.heatLayer(pts, {
        radius, blur, maxZoom: 15, max: heatMax, minOpacity: HEAT_MIN_OPACITY[currentView], gradient: gradientFor(currentView),
      }).addTo(map);
    }

    applyRegionMask(boundaryGeom);
    renderEngineerMarkers();

    setTimeout(() => { if (map) map.invalidateSize(); }, 60);
  }

  function renderEngineerMarkers() {
    if (!map || !engineerLayer) return;
    engineerLayer.clearLayers();
    if (!showEngineers) return;
    
    const engineers = currentPayload && currentPayload.engineers;
    const list = (engineers && engineers.by_region && engineers.by_region[selectedRegion]) || [];
    
    list.forEach((eng) => {
      const roleLabel = eng.role === "lead" 
        ? "⭐ <b>Trưởng nhóm</b>" 
        : (eng.role === "member" ? "Thành viên (TN: " + escapeHtml(eng.team_lead || "") + ")" : "KT độc lập");
      const approxNote = eng.approx 
        ? '<div class="station-popup-row" style="color:#d97706;font-size:12px;">⚠ Đang tham chiếu từ khu vực lân cận</div>' 
        : "";
      const popupHtml = (
        '<div class="eng-popup-title">' + escapeHtml(eng.name) + '</div>' +
        '<div class="station-popup-row">' + roleLabel + '</div>' +
        approxNote
      );
      L.marker([eng.lat, eng.lng], { icon: engineerIcon(eng.role) })
        .bindTooltip(eng.name, { direction: "top", offset: [0, -28] })
        .bindPopup(popupHtml)
        .addTo(engineerLayer);
    });
  }

  function renderKPIs() {
    const regionData = currentPayload ? (currentPayload.regions || {})[selectedRegion] : null;
    const vol = regionData ? regionData.volume : null;
    const od = regionData ? regionData.overdue : null;
    const volTotal = vol ? vol.total : 0;
    const odTotal = od ? od.total : 0;
    const odPct = volTotal ? Math.round((odTotal / volTotal) * 1000) / 10 : 0;
    const top = vol && vol.top_stations && vol.top_stations[0];
    
    setKPIs([
      { label: "Tổng ticket", value: volTotal.toLocaleString("vi-VN"), sub: REGION_LABELS[selectedRegion] || selectedRegion },
      { label: "Overdue", value: odTotal.toLocaleString("vi-VN"), sub: odPct + "% tổng ticket khu vực" },
      { label: "Trạm có toạ độ", value: (regionData ? regionData.stations_with_coords : 0) + "/" + (regionData ? regionData.stations_total : 0), sub: (regionData ? regionData.coord_coverage_pct : 0) + "% phủ toạ độ" },
      { label: "Trạm nhiều ticket nhất", value: top ? top.station : "—", sub: top ? top.count + " ticket" : "—" },
    ]);
  }

  function renderRegionTabs() {
    refs.regionTabs.innerHTML = REGIONS.map((r) => (
      '<button type="button" class="region-tab' + (r === selectedRegion ? " active" : "") + '" data-region="' + r + '">' +
        (REGION_LABELS[r] || r) +
      '</button>'
    )).join("");
    refs.regionTabs.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.region === selectedRegion) return;
        selectedRegion = btn.dataset.region;
        renderRegionTabs();
        renderAll();
      });
    });
  }

  function renderAll() {
    if (!currentPayload) return;
    refs.title.textContent = currentView === "overdue"
      ? "Bản đồ nhiệt · Ticket Overdue"
      : "Bản đồ nhiệt · Số lượng ticket";
    refs.desc.innerHTML = currentView === "overdue"
      ? "Mật độ ticket có <strong>SLA Status = Overdue</strong> theo vị trí trạm · " + (currentPayload.chart_days || 30) + " ngày gần nhất"
      : "Mật độ toàn bộ ticket theo vị trí trạm · " + (currentPayload.chart_days || 30) + " ngày gần nhất";
    renderKPIs();
    drawMap();
  }

  function renderSourceNote(payload) {
    Core.setSourceBadge(payload.source === "sample" ? "Dữ liệu mẫu" : "Cache");
    Core.setGeneratedAt(payload.generated_at);
    refs.sourceNote.textContent = (payload.chart_days || 30) + " ngày gần nhất · " + (payload.generated_at || "");
  }

  async function load() {
    refs.loading.style.display = "block";
    refs.loading.textContent = "⏳ Đang tải bản đồ nhiệt…";
    try {
      const res = await fetch("/api/stats/heatmap");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const payload = await res.json();
      if (payload.error) throw new Error(payload.error);
      fullPayload = payload;
      currentPayload = slicePayload(payload, Core.cpType);
      refs.loading.style.display = "none";
      renderSourceNote(payload);
      renderRegionTabs();
      renderAll();
    } catch (e) {
      refs.loading.textContent = "Không tải được bản đồ nhiệt: " + e.message;
      console.warn(e);
    }
  }

  function onShow() {
    if (!fullPayload) { load(); return; }
    renderSourceNote(fullPayload);
    currentPayload = slicePayload(fullPayload, Core.cpType);
    renderRegionTabs();
    renderAll();
  }

  function onCpTypeChange() {
    if (!fullPayload) return;
    currentPayload = slicePayload(fullPayload, Core.cpType);
    if (panelEl.style.display !== "none") renderAll();
  }

  function mount(panel) {
    panelEl = panel;
    // Đã gỡ bỏ thẻ <div class="heatmap-side"> ra khỏi bộ khung HTML
    panel.innerHTML = `
      <div class="view-toggle" style="margin-bottom:12px;">
        <button type="button" class="active" data-view="volume">🎫 Số lượng ticket</button>
        <button type="button" data-view="overdue">⏰ Overdue</button>
      </div>
      <div class="card-header">
        <div class="header-main">
          <h2 class="chart-title">Bản đồ nhiệt · Số lượng ticket</h2>
          <div class="desc chart-desc">Mật độ ticket theo vị trí trạm · 30 ngày gần nhất</div>
        </div>
      </div>
      <div class="region-tabs"></div>
      <div class="loading chart-loading" style="display:none;">⏳ Đang tải bản đồ nhiệt…</div>
      
      <div class="heatmap-body">
        <div class="heatmap-map-wrap">
          <div class="heatmap-map"></div>
          <!-- Bảng điều khiển nổi (Legend + Toggle UI) -->
          <div class="heatmap-map-overlay">
            <div class="heatmap-legend"></div>
            <label class="heatmap-layer-toggle">
              <input type="checkbox" class="eng-toggle" checked>
              <span>Hiển thị vị trí Kỹ thuật viên</span>
            </label>
          </div>
        </div>
      </div>
      <div class="source-note"></div>
    `;
    refs = {
      viewToggle: panel.querySelector(".view-toggle"),
      title: panel.querySelector(".chart-title"),
      desc: panel.querySelector(".chart-desc"),
      regionTabs: panel.querySelector(".region-tabs"),
      loading: panel.querySelector(".chart-loading"),
      mapEl: panel.querySelector(".heatmap-map"),
      legend: panel.querySelector(".heatmap-legend"),
      engToggle: panel.querySelector(".eng-toggle"),
      sourceNote: panel.querySelector(".source-note"),
    };

    refs.legend.innerHTML = '<span>Thấp</span><span class="heatmap-gradient-bar vol"></span><span>Cao</span>';

    refs.engToggle.addEventListener("change", () => {
      showEngineers = refs.engToggle.checked;
      renderEngineerMarkers();
    });

    refs.viewToggle.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-view]");
      if (!btn || btn.dataset.view === currentView) return;
      currentView = btn.dataset.view;
      refs.viewToggle.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
      refs.legend.innerHTML = currentView === "overdue"
        ? '<span>Thấp</span><span class="heatmap-gradient-bar od"></span><span>Cao (Overdue)</span>'
        : '<span>Thấp</span><span class="heatmap-gradient-bar vol"></span><span>Cao</span>';
      renderAll();
    });
  }

  Core.registerChart({
    key: "heatmap",
    label: "Bản đồ nhiệt",
    mount,
    onShow,
    onCpTypeChange,
  });
})();