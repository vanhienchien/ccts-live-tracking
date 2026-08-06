/**
 * stats_chart_volume.js — module biểu đồ "Thống kê sự cố theo ngày".
 * Nguồn dữ liệu: GET /api/stats/daily-volume (xem stats_charts_volume.py).
 *
 * 3 view:
 *   - region : 1 đường / khu vực (đa đường, có legend bật/tắt).
 *   - total  : 1 đường TỔNG tất cả khu vực + nhãn số liệu từng điểm, đường
 *              trung bình tham chiếu, điểm tô màu theo trên/dưới TB, và
 *              dải chỉ số nổi bật (đỉnh/đáy/so với hôm trước/so với TB).
 *   - tech   : heatmap Kỹ thuật viên (dòng) × Ngày (cột), màu theo số ticket.
 *
 * Tự đăng ký vào StatsCore — xem stats_core.js để biết cách thêm 1 module
 * biểu đồ mới tương tự file này.
 */
(function () {
  "use strict";
  const Core = window.StatsCore;
  const { REGION_ORDER, colorFor, formatDateLabel, escapeHtml, heatColor, setKPIs } = Core;

  Core.injectStyleOnce("stats-style-volume", `
    .total-stat-strip { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; }
    .total-stat-strip .tstat {
      flex: 1 1 150px; background: #f8fafc; border: 1px solid var(--border);
      border-radius: 10px; padding: 10px 14px; display: flex; align-items: center; gap: 10px;
    }
    .total-stat-strip .tstat .icon { font-size: 20px; line-height: 1; }
    .total-stat-strip .tstat .label {
      font-size: 10.5px; font-weight: 600; color: var(--text-muted);
      text-transform: uppercase; margin: 0 0 2px;
    }
    .total-stat-strip .tstat .val { font-size: 15px; font-weight: 700; color: var(--primary); }
    .total-stat-strip .tstat .val .sub { font-size: 11px; font-weight: 500; color: var(--text-muted); margin-left: 4px; }
    .total-stat-strip .tstat.up .val { color: #dc2626; }
    .total-stat-strip .tstat.down .val { color: #16a34a; }

    .heatmap-wrap { width: 100%; }
    .heatmap-scroll { overflow: auto; max-height: 560px; border: 1px solid var(--border); border-radius: 10px; }
    table.heatmap-table { border-collapse: separate; border-spacing: 0; font-size: 11.5px; width: max-content; }
    table.heatmap-table thead th {
      position: sticky; top: 0; z-index: 2; background: #f8fafc; color: var(--text-muted);
      font-weight: 600; padding: 7px 5px; border-bottom: 1px solid var(--border);
      white-space: nowrap; min-width: 34px;
    }
    table.heatmap-table thead th.corner {
      position: sticky; left: 0; top: 0; z-index: 3; background: #f8fafc;
      text-align: left; padding-left: 10px;
    }
    table.heatmap-table thead th.total-col { min-width: 42px; }
    table.heatmap-table tbody th {
      position: sticky; left: 0; z-index: 1; background: #fff; text-align: left;
      padding: 4px 10px; font-weight: 600; color: var(--text); white-space: nowrap;
      max-width: 170px; overflow: hidden; text-overflow: ellipsis;
      border-right: 1px solid var(--border); border-bottom: 1px solid #f1f5f9;
    }
    table.heatmap-table tbody tr:last-child th, table.heatmap-table tbody tr:last-child td {
      border-top: 2px solid var(--border); font-weight: 700;
    }
    td.heatmap-cell {
      width: 34px; height: 26px; min-width: 34px; text-align: center; font-weight: 600;
      font-variant-numeric: tabular-nums; border-bottom: 1px solid #f1f5f9;
      border-right: 1px solid rgba(255,255,255,.7);
    }
    td.heatmap-cell.total-cell { background: #f1f5f9 !important; color: var(--primary) !important; border-left: 1px solid var(--border); }
    .heatmap-legend { display: flex; align-items: center; gap: 8px; margin-top: 12px; font-size: 11.5px; color: var(--text-muted); }
    .heatmap-gradient-bar {
      display: inline-block; width: 160px; height: 10px; border-radius: 6px;
      background: linear-gradient(to right, #fef3c7, #f59e0b, #991b1b); border: 1px solid rgba(0,0,0,.06);
    }
    .heatmap-empty { padding: 60px 20px; text-align: center; color: var(--text-muted); font-size: 13.5px; }
  `);

  let chart = null;
  let fullPayload = null;
  let currentPayload = null;
  let currentView = "total";
  let selectedRegion = "LDO-BTH";
  let visibleSeries = {};
  let panelEl = null;
  let refs = null;
  let lastBuiltRawLabels = [];

  // ----- payload slicing theo bộ lọc EV/BSS -----
  function slicePayload(full, cp) {
    if (!full) return null;
    const by = full.by_cp_type || {};
    const sub = by[cp] || by.all || full;
    return Object.assign({}, full, sub, {
      by_cp_type: full.by_cp_type,
      counts: full.counts,
      scrape_days: full.scrape_days,
      chart_days: full.chart_days,
      source: full.source,
      generated_at: full.generated_at,
      meta: full.meta,
      cp_type: cp,
    });
  }

  // ----- view "region": nhiều đường -----
  function buildRegionDatasets() {
    if (!currentPayload) return { labels: [], datasets: [], rawLabels: [] };
    const seriesNames = currentPayload.regions || [];
    const src = currentPayload.datasets || {};
    const datasets = [];
    seriesNames.forEach((name, idx) => {
      if (!(name in src)) return;
      if (visibleSeries[name] === false) return;
      const col = colorFor(name, idx);
      datasets.push({
        label: name,
        data: src[name],
        borderColor: col.border,
        backgroundColor: col.bg,
        borderWidth: 2,
        pointRadius: 3,
        pointHoverRadius: 6,
        pointBackgroundColor: col.border,
        pointBorderColor: "#fff",
        pointBorderWidth: 2,
        tension: 0,
        fill: false,
      });
    });
    return {
      labels: (currentPayload.labels || []).map(formatDateLabel),
      datasets,
      rawLabels: currentPayload.labels || [],
    };
  }

  function renderLegend() {
    const el = refs.legend;
    el.innerHTML = "";
    const seriesNames = (currentPayload && currentPayload.regions) || [];
    const src = (currentPayload && currentPayload.datasets) || {};
    seriesNames.forEach((name, idx) => {
      if (!(name in src)) return;
      if (!(name in visibleSeries)) visibleSeries[name] = true;
      const col = colorFor(name, idx);
      const chip = document.createElement("span");
      chip.className = "legend-chip" + (visibleSeries[name] ? " active" : "");
      chip.textContent = name;
      if (visibleSeries[name]) {
        chip.style.background = col.border;
        chip.style.borderColor = col.border;
      }
      chip.addEventListener("click", () => {
        visibleSeries[name] = !visibleSeries[name];
        drawLineChart();
        renderLegend();
      });
      el.appendChild(chip);
    });
  }

  function drawLineChart() {
    const built = buildRegionDatasets();
    lastBuiltRawLabels = built.rawLabels;
    const ctx = refs.canvas.getContext("2d");
    if (chart) chart.destroy();
    chart = new Chart(ctx, {
      type: "line",
      data: { labels: built.labels, datasets: built.datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#0f172a",
            titleFont: { size: 13, weight: "600" },
            bodyFont: { size: 12.5 },
            padding: 12,
            cornerRadius: 8,
            callbacks: {
              title: (items) => lastBuiltRawLabels[items[0].dataIndex] || items[0].label,
              label: (item) => " " + item.dataset.label + ": " + item.raw + " ticket",
            },
          },
        },
        scales: {
          x: {
            grid: { color: "rgba(148,163,184,.15)", drawBorder: false },
            ticks: { font: { size: 11.5 }, color: "#64748b", maxRotation: 0 },
          },
          y: {
            beginAtZero: true,
            grace: "10%",
            grid: { color: "rgba(148,163,184,.2)", drawBorder: false },
            ticks: { font: { size: 11.5 }, color: "#64748b", precision: 0, stepSize: 1 },
            title: { display: true, text: "Số ticket", color: "#94a3b8", font: { size: 12, weight: "500" } },
          },
        },
      },
    });
  }

  // ----- view "total": 1 đường tổng, có nhãn/chỉ số từng ngày -----
  function drawTotalChart(series) {
    const rawLabels = series.labels || [];
    const labels = rawLabels.map(formatDateLabel);
    const values = series.total || [];
    const deltas = series.deltas || [];
    const avg = series.avg || 0;
    const pointColors = values.map((v) => (v > avg ? "#dc2626" : "#16a34a"));

    const ctx = refs.canvas.getContext("2d");
    if (chart) chart.destroy();
    chart = new Chart(ctx, {
      type: "line",
      plugins: typeof ChartDataLabels !== "undefined" ? [ChartDataLabels] : [],
      data: {
        labels,
        datasets: [
          {
            label: "Trung bình (" + avg + "/ngày)",
            data: labels.map(() => avg),
            borderColor: "rgba(100,116,139,.55)",
            borderDash: [6, 5],
            borderWidth: 1.5,
            pointRadius: 0,
            fill: false,
            tension: 0,
            datalabels: { display: false },
          },
          {
            label: "Tổng tất cả khu vực",
            data: values,
            borderColor: "#f97316",
            backgroundColor: (c) => {
              const g = c.chart.ctx.createLinearGradient(0, 0, 0, c.chart.height || 300);
              g.addColorStop(0, "rgba(249,115,22,.28)");
              g.addColorStop(1, "rgba(249,115,22,.02)");
              return g;
            },
            borderWidth: 2.5,
            pointRadius: 4,
            pointHoverRadius: 7,
            pointBackgroundColor: pointColors,
            pointBorderColor: "#fff",
            pointBorderWidth: 2,
            tension: 0.15,
            fill: true,
            datalabels: {
              display: true,
              align: "top",
              anchor: "end",
              offset: 6,
              color: "#0f172a",
              font: { size: 11, weight: "700" },
              formatter: (v) => v,
            },
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        layout: { padding: { top: 22 } },
        plugins: {
          legend: {
            display: true,
            position: "top",
            align: "end",
            labels: { boxWidth: 14, font: { size: 11.5 }, color: "#64748b" },
          },
          tooltip: {
            backgroundColor: "#0f172a",
            titleFont: { size: 13, weight: "600" },
            bodyFont: { size: 12.5 },
            padding: 12,
            cornerRadius: 8,
            callbacks: {
              title: (items) => rawLabels[items[0].dataIndex] || items[0].label,
              label: (item) => {
                if (item.datasetIndex === 0) return " Trung bình: " + avg + " ticket/ngày";
                const d = deltas[item.dataIndex];
                let extra = "";
                if (d != null) {
                  extra = d > 0 ? " (▲ +" + d + " so với hôm trước)"
                    : d < 0 ? " (▼ " + d + " so với hôm trước)"
                      : " (không đổi so với hôm trước)";
                }
                return " Tổng: " + item.raw + " ticket" + extra;
              },
            },
          },
        },
        scales: {
          x: {
            grid: { color: "rgba(148,163,184,.15)", drawBorder: false },
            ticks: { font: { size: 11.5 }, color: "#64748b", maxRotation: 0 },
          },
          y: {
            beginAtZero: true,
            grace: "18%",
            grid: { color: "rgba(148,163,184,.2)", drawBorder: false },
            ticks: { font: { size: 11.5 }, color: "#64748b", precision: 0, stepSize: 1 },
            title: { display: true, text: "Số ticket", color: "#94a3b8", font: { size: 12, weight: "500" } },
          },
        },
      },
    });
  }

  function renderTotalStatStrip(series) {
    const el = refs.totalStrip;
    const labels = series.labels || [];
    if (!labels.length) { el.style.display = "none"; el.innerHTML = ""; return; }
    el.style.display = "flex";

    const values = series.total || [];
    const deltas = series.deltas || [];
    const avg = series.avg || 0;
    const lastIdx = values.length - 1;
    const lastVal = values[lastIdx];
    const lastDelta = deltas[lastIdx];
    const trendUp = lastVal > avg;

    let deltaTxt = "· không đổi so với hôm trước";
    let deltaCls = "";
    if (lastDelta > 0) { deltaTxt = "▲ +" + lastDelta + " so với hôm trước"; deltaCls = "up"; }
    else if (lastDelta < 0) { deltaTxt = "▼ " + lastDelta + " so với hôm trước"; deltaCls = "down"; }

    el.innerHTML = `
      <div class="tstat">
        <span class="icon">📅</span>
        <div><div class="label">Ngày gần nhất · ${escapeHtml(formatDateLabel(labels[lastIdx]))}</div>
        <div class="val">${lastVal} <span class="sub">ticket</span></div></div>
      </div>
      <div class="tstat ${deltaCls}">
        <span class="icon">${deltaCls === "up" ? "🔺" : deltaCls === "down" ? "🟢" : "➖"}</span>
        <div><div class="label">So với hôm trước</div><div class="val">${deltaTxt}</div></div>
      </div>
      <div class="tstat">
        <span class="icon">🏔️</span>
        <div><div class="label">Đỉnh · ${escapeHtml(formatDateLabel(series.max_date))}</div>
        <div class="val">${series.max_value} <span class="sub">ticket</span></div></div>
      </div>
      <div class="tstat">
        <span class="icon">📉</span>
        <div><div class="label">Đáy · ${escapeHtml(formatDateLabel(series.min_date))}</div>
        <div class="val">${series.min_value} <span class="sub">ticket</span></div></div>
      </div>
      <div class="tstat ${trendUp ? "up" : "down"}">
        <span class="icon">${trendUp ? "⚠️" : "✅"}</span>
        <div><div class="label">So với TB (${avg}/ngày)</div>
        <div class="val">${trendUp ? "Cao hơn TB" : "Thấp hơn/bằng TB"}</div></div>
      </div>
    `;
  }

  // ----- view "tech": heatmap KT × ngày -----
  function renderHeatmap() {
    const by = (currentPayload.by_region || {})[selectedRegion] || {};
    const labels = by.labels || currentPayload.labels || [];
    const techs = by.techs || [];
    const datasets = by.datasets || {};

    if (!techs.length || !labels.length) {
      refs.heatmapScroll.innerHTML = '<div class="heatmap-empty">Không có dữ liệu kỹ thuật viên cho khu vực này.</div>';
      refs.heatmapLegendEl.innerHTML = "";
      return;
    }

    const totals = techs.map((t) => (datasets[t] || []).reduce((a, b) => a + b, 0));
    const order = techs.map((_, i) => i).sort((a, b) => totals[b] - totals[a]);
    const max = Math.max(1, ...techs.flatMap((t) => datasets[t] || [0]));
    const colTotals = labels.map((_, di) => techs.reduce((s, t) => s + ((datasets[t] || [])[di] || 0), 0));

    let html = '<table class="heatmap-table"><thead><tr><th class="corner">Kỹ thuật viên</th>';
    labels.forEach((l) => { html += `<th>${escapeHtml(formatDateLabel(l))}</th>`; });
    html += '<th class="total-col">Tổng</th></tr></thead><tbody>';

    order.forEach((i) => {
      const t = techs[i];
      const row = datasets[t] || [];
      html += `<tr><th>${escapeHtml(t)}</th>`;
      row.forEach((v, di) => {
        const c = heatColor(v, max);
        const title = `${escapeHtml(t)} · ${labels[di]}: ${v || 0} ticket`;
        html += `<td class="heatmap-cell" style="background:${c.bg};color:${c.fg}" title="${title}">${v || ""}</td>`;
      });
      html += `<td class="heatmap-cell total-cell">${totals[i]}</td></tr>`;
    });

    html += '<tr><th>Tổng theo ngày</th>';
    colTotals.forEach((v) => { html += `<td class="heatmap-cell total-cell">${v}</td>`; });
    const grand = colTotals.reduce((a, b) => a + b, 0);
    html += `<td class="heatmap-cell total-cell" style="background:#0f172a !important;color:#fff !important;">${grand}</td>`;
    html += "</tr></tbody></table>";

    refs.heatmapScroll.innerHTML = html;
    refs.heatmapLegendEl.innerHTML =
      '<span>Ít</span><span class="heatmap-gradient-bar"></span><span>Nhiều (tối đa ' + max + ' ticket/ngày)</span>';
  }

  // ----- KPI theo từng view -----
  function renderKPIsRegion(payload) {
    const total = payload.total_tickets || 0;
    const days = (payload.labels || []).length;
    const regions = (payload.regions || []).length;
    const dr = payload.date_range || {};
    setKPIs([
      { label: "Tổng ticket", value: total.toLocaleString("vi-VN"), sub: dr.from && dr.to ? formatDateLabel(dr.from) + " → " + formatDateLabel(dr.to) : "—" },
      { label: "Số ngày", value: days, sub: "trong khoảng thống kê" },
      { label: "Khu vực", value: regions, sub: "có phát sinh ticket" },
      { label: "TB / ngày", value: days > 0 ? (total / days).toFixed(1) : "—", sub: "ticket theo ngày tạo" },
    ]);
  }

  function renderKPIsTotal(series) {
    setKPIs([
      { label: "Tổng ticket", value: (series.total || []).reduce((a, b) => a + b, 0).toLocaleString("vi-VN"), sub: series.date_range && series.date_range.from ? formatDateLabel(series.date_range.from) + " → " + formatDateLabel(series.date_range.to) : "—" },
      { label: "Đỉnh", value: series.max_value ?? "—", sub: series.max_date ? formatDateLabel(series.max_date) : "—" },
      { label: "Đáy", value: series.min_value ?? "—", sub: series.min_date ? formatDateLabel(series.min_date) : "—" },
      { label: "TB / ngày", value: series.avg ?? "—", sub: "tất cả khu vực" },
    ]);
  }

  function renderKPIsTech(payload) {
    const by = (payload.by_region || {})[selectedRegion] || {};
    const ds = by.datasets || {};
    let total = 0;
    Object.values(ds).forEach((arr) => (arr || []).forEach((v) => { total += v; }));
    const days = (by.labels || payload.labels || []).length;
    const techs = (by.techs || []).length;
    setKPIs([
      { label: "Ticket khu vực", value: total.toLocaleString("vi-VN"), sub: selectedRegion },
      { label: "Số ngày", value: days, sub: "trong khoảng thống kê" },
      { label: "Kỹ thuật viên", value: techs, sub: "đang có ticket" },
      { label: "TB / ngày", value: days > 0 ? (total / days).toFixed(1) : "—", sub: "ticket theo ngày tạo" },
    ]);
  }

  // ----- điều phối view -----
  function renderRegionTabs() {
    const el = refs.regionTabs;
    if (currentView !== "tech") { el.style.display = "none"; return; }
    el.style.display = "flex";
    el.innerHTML = "";
    REGION_ORDER.forEach((r) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "region-tab" + (r === selectedRegion ? " active" : "");
      btn.textContent = r;
      btn.addEventListener("click", () => {
        selectedRegion = r;
        renderRegionTabs();
        refs.title.textContent = "Sự cố theo KT · " + selectedRegion;
        refs.desc.innerHTML = "Ticket giao KT khu vực <strong>" + selectedRegion + "</strong> · 30 ngày gần nhất · đến hết hôm qua · màu ô = số ticket";
        renderKPIsTech(currentPayload);
        renderHeatmap();
      });
      el.appendChild(btn);
    });
  }

  function applyView(view) {
    currentView = view;
    visibleSeries = {};
    refs.viewToggle.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === view);
    });
    refs.totalStrip.style.display = "none";
    refs.legend.style.display = view === "region" ? "flex" : "none";

    if (view === "region") {
      refs.title.textContent = "Số lượng sự cố theo ngày";
      refs.desc.innerHTML = "Ticket theo <strong>Create Time</strong> · <strong>30 ngày</strong> gần nhất · đến hết hôm qua · theo khu vực";
    } else if (view === "total") {
      refs.title.textContent = "Tổng số sự cố theo ngày (tất cả khu vực)";
      refs.desc.innerHTML = "Gộp toàn bộ khu vực đang quản lý · <strong>30 ngày</strong> gần nhất · đến hết hôm qua";
    } else {
      refs.title.textContent = "Sự cố theo KT · " + selectedRegion;
      refs.desc.innerHTML = "Ticket giao KT khu vực <strong>" + selectedRegion + "</strong> · 30 ngày gần nhất · đến hết hôm qua · màu ô = số ticket";
    }

    renderRegionTabs();
    if (!currentPayload) return;

    if (view === "region") {
      refs.chartWrap.style.display = "block";
      refs.heatmapWrap.style.display = "none";
      renderKPIsRegion(currentPayload);
      renderLegend();
      drawLineChart();
    } else if (view === "total") {
      refs.chartWrap.style.display = "block";
      refs.heatmapWrap.style.display = "none";
      const series = currentPayload.total_series || {};
      renderKPIsTotal(series);
      renderTotalStatStrip(series);
      drawTotalChart(series);
    } else {
      refs.chartWrap.style.display = "none";
      refs.heatmapWrap.style.display = "block";
      renderKPIsTech(currentPayload);
      renderHeatmap();
    }
  }

  // ----- nạp dữ liệu / vòng đời module -----
  function renderSourceNote(payload) {
    Core.setSourceBadge(payload.source === "sample" ? "Dữ liệu mẫu" : "Cache");
    if (payload.source === "sample") {
      refs.sourceNote.textContent = "⚠ Cache mẫu — restart app để cào live 45 ngày.";
      refs.sourceNote.className = "source-note sample";
    } else {
      const m = payload.meta || {};
      refs.sourceNote.textContent =
        "Cào " + (payload.scrape_days || 45) + " ngày · [" + (m.start_time || "?") + " → " + (m.end_time || "?") + ") · " + (payload.generated_at || "");
      refs.sourceNote.className = "source-note";
    }
    Core.setGeneratedAt(payload.generated_at);
  }

  function renderChart(payload) {
    fullPayload = payload;
    currentPayload = slicePayload(payload, Core.cpType);
    refs.loading.style.display = "none";
    renderSourceNote(payload);
    applyView(currentView);
  }

  async function load() {
    refs.loading.style.display = "block";
    refs.loading.textContent = "⏳ Đang tải dữ liệu thống kê…";
    try {
      const res = await fetch("/api/stats/daily-volume");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const payload = await res.json();
      if (payload.error && !payload.labels) throw new Error(payload.error);
      renderChart(payload);
    } catch (e) {
      console.warn(e);
      refs.loading.textContent = "Chưa có cache — đợi cào nền (restart) hoặc 0h.";
    }
  }

  function onShow() {
    if (!fullPayload) { load(); return; }
    renderSourceNote(fullPayload);
    currentPayload = slicePayload(fullPayload, Core.cpType);
    applyView(currentView);
  }

  function onCpTypeChange() {
    if (!fullPayload) return;
    currentPayload = slicePayload(fullPayload, Core.cpType);
    visibleSeries = {};
    if (panelEl.style.display !== "none") applyView(currentView);
  }

  function mount(panel) {
    panelEl = panel;
    panel.innerHTML = `
      <div class="view-toggle" style="margin-bottom:12px;">
        <button type="button" class="active" data-view="total">Total</button>
        <button type="button" data-view="region">Theo khu vực</button>
        <button type="button" data-view="tech">Theo kỹ thuật viên</button>
      </div>
      <div class="region-tabs" style="display:none;"></div>
      <div class="card-header">
        <div>
          <h2 class="chart-title">Tổng số sự cố theo ngày (tất cả khu vực)</h2>
          <div class="desc chart-desc">Gộp toàn bộ khu vực đang quản lý · <strong>30 ngày</strong> gần nhất · đến hết hôm qua</div>
        </div>
        <div class="legend-toggle"></div>
      </div>
      <div class="loading chart-loading">⏳ Đang tải dữ liệu thống kê…</div>
      <div class="total-stat-strip" style="display:none;"></div>
      <div class="chart-wrap" style="display:none;"><canvas></canvas></div>
      <div class="heatmap-wrap" style="display:none;">
        <div class="heatmap-scroll"></div>
        <div class="heatmap-legend"></div>
      </div>
      <div class="source-note"></div>
    `;
    refs = {
      viewToggle: panel.querySelector(".view-toggle"),
      regionTabs: panel.querySelector(".region-tabs"),
      title: panel.querySelector(".chart-title"),
      desc: panel.querySelector(".chart-desc"),
      legend: panel.querySelector(".legend-toggle"),
      loading: panel.querySelector(".chart-loading"),
      totalStrip: panel.querySelector(".total-stat-strip"),
      chartWrap: panel.querySelector(".chart-wrap"),
      canvas: panel.querySelector("canvas"),
      heatmapWrap: panel.querySelector(".heatmap-wrap"),
      heatmapScroll: panel.querySelector(".heatmap-scroll"),
      heatmapLegendEl: panel.querySelector(".heatmap-legend"),
      sourceNote: panel.querySelector(".source-note"),
    };
    refs.viewToggle.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-view]");
      if (btn) applyView(btn.dataset.view);
    });
  }

  Core.registerChart({
    key: "volume",
    label: "Thống kê sự cố theo ngày",
    mount,
    onShow,
    onCpTypeChange,
  });
})();