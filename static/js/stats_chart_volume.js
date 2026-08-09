/**
 * stats_chart_volume.js — module biểu đồ "Thống kê sự cố theo ngày".
 * Nguồn dữ liệu: GET /api/stats/daily-volume (xem stats_charts_volume.py).
 *
 * 5 view:
 *   - region   : 1 đường / khu vực (đa đường, có legend bật/tắt).
 *   - total    : 1 đường TỔNG tất cả khu vực + nhãn số liệu từng điểm, đường
 *                trung bình tham chiếu, điểm tô màu theo trên/dưới TB, và
 *                dải chỉ số nổi bật (đỉnh/đáy/so với hôm trước/so với TB).
 *   - tech     : heatmap Kỹ thuật viên (dòng) × Ngày (cột), màu theo số ticket.
 *   - workload : bubble so sánh khối lượng — X = TB km/chặng, Y = số trạm,
 *                bán kính ∝ số ticket; đã lọc nhiễu toạ độ (>200 km).
 *
 * Tự đăng ký vào StatsCore — xem stats_core.js để biết cách thêm 1 module
 * biểu đồ mới tương tự file này.
 */
(function () {
  "use strict";
  const Core = window.StatsCore;
  const { REGION_ORDER, colorFor, formatDateLabel, escapeHtml, heatColor, setKPIs } = Core;

  Core.injectStyleOnce("stats-style-volume-polar", `
    .polar-wrap { width: 100%; }
    .polar-layout {
      display: grid;
      grid-template-columns: minmax(320px, 1.2fr) minmax(280px, 0.85fr);
      gap: 20px;
      align-items: stretch;
    }
    @media (max-width: 960px) {
      .polar-layout { grid-template-columns: 1fr; }
    }
    .polar-chart-box {
      position: relative;
      min-height: 440px;
      height: 440px;
      width: 100%;
      background: #fff;
      border: 1px solid var(--border, #e2e8f0);
      border-radius: 14px;
      padding: 12px 12px 8px;
      box-shadow: 0 1px 2px rgba(15,23,42,.04);
    }
    .polar-side {
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-width: 0;
    }
    .polar-kpis {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .polar-kpis .pstat {
      background: #f8fafc;
      border: 1px solid var(--border, #e2e8f0);
      border-radius: 12px;
      padding: 12px 14px;
    }
    .polar-kpis .pstat.wide { grid-column: 1 / -1; }
    .polar-kpis .plabel {
      font-size: 11px;
      font-weight: 600;
      color: #64748b;
      text-transform: uppercase;
      letter-spacing: .02em;
      margin-bottom: 4px;
    }
    .polar-kpis .pval {
      font-size: 22px;
      font-weight: 700;
      color: #0f172a;
      line-height: 1.2;
      font-variant-numeric: tabular-nums;
    }
    .polar-kpis .psub {
      font-size: 12px;
      color: #64748b;
      margin-top: 4px;
      line-height: 1.35;
    }
    .polar-list {
      background: #fff;
      border: 1px solid var(--border, #e2e8f0);
      border-radius: 12px;
      overflow: hidden;
      flex: 1;
      max-height: 320px;
      display: flex;
      flex-direction: column;
    }
    .polar-list .plist-head {
      display: grid;
      grid-template-columns: 1fr 88px 64px;
      gap: 8px;
      padding: 10px 14px;
      background: #f8fafc;
      border-bottom: 1px solid var(--border, #e2e8f0);
      font-size: 11px;
      font-weight: 600;
      color: #64748b;
      text-transform: uppercase;
      letter-spacing: .02em;
    }
    .polar-list .plist-body {
      overflow: auto;
      flex: 1;
    }
    .polar-list .prow {
      display: grid;
      grid-template-columns: 1fr 88px 64px;
      gap: 8px;
      align-items: center;
      padding: 10px 14px;
      border-bottom: 1px solid #f1f5f9;
      font-size: 13px;
    }
    .polar-list .prow:last-child { border-bottom: 0; }
    .polar-list .prow:hover { background: #f8fafc; }
    .polar-list .pname-cell {
      display: flex; align-items: center; gap: 10px; min-width: 0;
    }
    .polar-list .pdot {
      width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
      box-shadow: 0 0 0 2px rgba(255,255,255,.9);
    }
    .polar-list .pname {
      font-weight: 600; color: #0f172a;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .polar-list .prank {
      font-size: 11px; color: #94a3b8; font-weight: 600; min-width: 16px;
    }
    .polar-list .pcount {
      text-align: right;
      font-variant-numeric: tabular-nums;
      font-weight: 600;
      color: #0f172a;
    }
    .polar-list .ppct {
      text-align: right;
      font-variant-numeric: tabular-nums;
      color: #64748b;
      font-weight: 500;
    }
    .polar-list .pbar-wrap {
      grid-column: 1 / -1;
      height: 4px;
      background: #f1f5f9;
      border-radius: 999px;
      overflow: hidden;
      margin-top: -2px;
    }
    .polar-list .pbar {
      height: 100%;
      border-radius: 999px;
    }
    .polar-note {
      font-size: 12px;
      color: #64748b;
      line-height: 1.5;
      padding: 0 2px;
    }
  `);


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

    .workload-wrap { margin-top: 22px; padding-top: 20px; border-top: 1px solid var(--border); }
    .workload-header h3 { margin: 0 0 4px; font-size: 14.5px; font-weight: 600; color: var(--text); }
    .workload-header .desc { font-size: 12.5px; color: var(--text-muted); margin-bottom: 14px; }
    .workload-chart-wrap { position: relative; height: 380px; width: 100%; }
    .workload-note { font-size: 11.5px; color: var(--text-muted); margin-top: 10px; line-height: 1.5; }
  
    /* Nút khu vực — bên phải header (cùng hàng title) */
    .card-header {
      display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: space-between;
      gap: 12px 16px;
    }
    .card-header .header-main { flex: 1 1 220px; min-width: 0; }
    .card-header .legend-toggle,
    .card-header .region-tabs {
      display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
      justify-content: flex-end; margin: 0; flex: 0 1 auto;
    }
    .card-header .legend-toggle:empty { display: none !important; }
    .legend-chip {
      display: inline-flex; align-items: center; justify-content: center;
      padding: 6px 14px; border-radius: 999px; font-size: 12.5px; font-weight: 600;
      border: 1.5px solid #cbd5e1; background: #f1f5f9; color: #64748b;
      cursor: pointer; user-select: none;
      transition: background .15s, color .15s, border-color .15s, box-shadow .15s;
    }
    .legend-chip:hover { filter: brightness(0.97); }
    .legend-chip.active {
      color: #fff; border-color: transparent;
      box-shadow: 0 2px 6px rgba(15, 23, 42, 0.12);
    }
`);

  let chart = null;
  let workloadChart = null;
  let polarChart = null;
  let polarScope = "all"; // all | region code

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
      tech_workload: full.tech_workload,
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
    // Nút lọc khu vực bên PHẢI header (cùng hàng title)
    if (refs.regionTabs) {
      refs.regionTabs.style.display = "none";
      refs.regionTabs.innerHTML = "";
    }
    const el = refs.legend;
    if (!el) return;
    el.style.display = "flex";
    el.innerHTML = "";
    const seriesNames = (currentPayload && currentPayload.regions) || REGION_ORDER || [];
    const src = (currentPayload && currentPayload.datasets) || {};
    seriesNames.forEach((name, idx) => {
      if (!(name in src)) return;
      if (!(name in visibleSeries)) visibleSeries[name] = true;
      const col = colorFor(name, idx);
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "legend-chip" + (visibleSeries[name] ? " active" : "");
      chip.textContent = name;
      if (visibleSeries[name]) {
        chip.style.background = col.border;
        chip.style.borderColor = col.border;
        chip.style.color = "#fff";
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

  // ----- view "workload": bubble so sánh khối lượng KT (toàn bộ khu vực) -----
  // Trục X = TB km nhà→trạm (avg_leg_km)
  // Trục Y = số trạm unique · bubble ∝ ticket
  // Workload: loại Tây Nguyên (nhà thầu) · tooltip: has_home, trạm gần/xa nhất
  function renderWorkloadChart() {
    const data = (currentPayload && currentPayload.tech_workload) || {};
    const techs = data.techs || [];
    const cov = data.coverage || {};

    if (!techs.length) {
      refs.workloadChartWrap.style.display = "none";
      refs.workloadNote.textContent = cov.tickets_total
        ? "Không đủ toạ độ trạm (StationCoords.json) để ước tính quãng đường cho các KT hiện có."
        : "Chưa có dữ liệu để tính khối lượng công việc.";
      if (workloadChart) { workloadChart.destroy(); workloadChart = null; }
      return;
    }
    refs.workloadChartWrap.style.display = "block";

    const maxTickets = Math.max(1, ...techs.map((t) => t.ticket_count || 0));
    const minR = 8;
    const maxR = 28;
    const byRegion = {};
    techs.forEach((t) => { (byRegion[t.region] = byRegion[t.region] || []).push(t); });

    const EXCL_WL = new Set(["Tây Nguyên", "Tay Nguyen", "TÂY NGUYÊN"]);
    if (!window._workloadVisible) window._workloadVisible = {};
    const datasets = REGION_ORDER.filter((r) => byRegion[r] && !EXCL_WL.has(r) && window._workloadVisible[r] !== false).map((region, idx) => {
      const col = colorFor(region, idx);
      return {
        label: region,
        data: byRegion[region].map((t) => ({
          x: t.avg_leg_km != null ? t.avg_leg_km : 0,
          y: t.unique_stations || 0,
          r: minR + (maxR - minR) * Math.sqrt((t.ticket_count || 0) / maxTickets),
          _tech: t,
        })),
        backgroundColor: col.border + "cc",
        borderColor: col.border,
        borderWidth: 1.5,
        hoverBorderWidth: 2.5,
      };
    });

    // Đường trung bình theo dữ liệu đang hiển thị
    const shown = datasets.flatMap((ds) => ds.data || []);
    const avgX = shown.length
      ? shown.reduce((s, p) => s + (Number(p.x) || 0), 0) / shown.length
      : 0;
    const avgY = shown.length
      ? shown.reduce((s, p) => s + (Number(p.y) || 0), 0) / shown.length
      : 0;
    const xMaxLine = Math.max(avgX * 1.2, ...shown.map((p) => Number(p.x) || 0), 1) * 1.15;
    const yMaxLine = Math.max(avgY * 1.2, ...shown.map((p) => Number(p.y) || 0), 1) * 1.15;

    datasets.push({
      type: "line",
      label: "TB km nhà→trạm",
      data: [{ x: avgX, y: 0 }, { x: avgX, y: yMaxLine }],
      borderColor: "rgba(71, 85, 105, 0.75)",
      borderWidth: 1.5,
      borderDash: [6, 4],
      pointRadius: 0,
      fill: false,
      order: 0,
    });
    datasets.push({
      type: "line",
      label: "TB số trạm",
      data: [{ x: 0, y: avgY }, { x: xMaxLine, y: avgY }],
      borderColor: "rgba(71, 85, 105, 0.75)",
      borderWidth: 1.5,
      borderDash: [6, 4],
      pointRadius: 0,
      fill: false,
      order: 0,
    });

    const wlRefLabelPlugin = {
      id: "wlRefLabels",
      afterDatasetsDraw(ch) {
        const { ctx, chartArea, scales } = ch;
        if (!chartArea) return;
        const xPx = scales.x.getPixelForValue(avgX);
        const yPx = scales.y.getPixelForValue(avgY);
        const xText = "TB " + (Math.round(avgX * 10) / 10) + " km";
        const yText = "TB " + (Math.round(avgY * 10) / 10) + " trạm";
        const pad = 4;
        ctx.save();
        ctx.font = "600 11px system-ui, sans-serif";
        ctx.textBaseline = "middle";
        // vertical
        ctx.textAlign = "left";
        let vx = xPx + 6;
        const vw = ctx.measureText(xText).width;
        if (vx + vw + 8 > chartArea.right) vx = xPx - vw - 8;
        const vy = chartArea.top + 12;
        ctx.fillStyle = "rgba(255,255,255,.92)";
        ctx.strokeStyle = "rgba(71,85,105,.35)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.roundRect ? ctx.roundRect(vx - pad, vy - 8, vw + pad * 2, 16, 4) : ctx.rect(vx - pad, vy - 8, vw + pad * 2, 16);
        ctx.fill(); ctx.stroke();
        ctx.fillStyle = "#334155";
        ctx.fillText(xText, vx, vy);
        // horizontal
        ctx.textAlign = "right";
        const rw = ctx.measureText(yText).width;
        const rx = chartArea.right - 6;
        let ry = yPx - 12;
        if (ry < chartArea.top + 10) ry = yPx + 14;
        ctx.fillStyle = "rgba(255,255,255,.92)";
        ctx.beginPath();
        ctx.roundRect ? ctx.roundRect(rx - rw - pad, ry - 8, rw + pad * 2, 16, 4) : ctx.rect(rx - rw - pad, ry - 8, rw + pad * 2, 16);
        ctx.fill(); ctx.stroke();
        ctx.fillStyle = "#334155";
        ctx.fillText(yText, rx, ry);
        ctx.restore();
      },
    };

    if (workloadChart) workloadChart.destroy();
    workloadChart = new Chart(refs.workloadCanvas.getContext("2d"), {
      type: "bubble",
      data: { datasets },
      plugins: [wlRefLabelPlugin],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#0f172a",
            titleFont: { size: 13, weight: "600" },
            bodyFont: { size: 12.5 },
            padding: 12,
            cornerRadius: 8,
            callbacks: {
              filter: (item) => item.dataset.type !== "line" && item.raw && item.raw._tech,
              title: (items) => {
                const t = items[0].raw._tech;
                return t.tech + " · " + t.region;
              },
              label: (item) => {
                const t = item.raw._tech;
                const homeLine = t.has_home
                  ? " Toạ độ nhà: đã có ✓"
                  : " Toạ độ nhà: CHƯA CÓ — cần cập nhật EngineerCoords";
                const near = (t.nearest_station != null)
                  ? " Gần nhất: " + t.nearest_station + " (" + (t.nearest_km ?? "—") + " km)"
                  : null;
                const far = (t.farthest_station != null)
                  ? " Xa nhất: " + t.farthest_station + " (" + (t.farthest_km ?? "—") + " km)"
                  : null;
                return [
                  homeLine,
                  " Ticket: " + t.ticket_count + " (có toạ độ trạm: " + t.coord_ticket_count + ")",
                  " Số trạm: " + t.unique_stations,
                  " TB nhà→trạm: " + (t.avg_leg_km ?? 0) + " km",
                  " Tổng quãng đường ước tính: " + t.total_km + " km",
                  near,
                  far,
                  " Bán kính phục vụ: ~" + t.service_radius_km + " km",
                ].filter(Boolean);
              },
            },
          },
        },
        scales: {
          x: {
            beginAtZero: true,
            grace: "12%",
            title: {
              display: true,
              text: "TB km nhà → trạm",
              color: "#94a3b8",
              font: { size: 12, weight: "500" },
            },
            grid: { color: "rgba(148,163,184,.15)", drawBorder: false },
            ticks: { color: "#64748b", font: { size: 11.5 } },
          },
          y: {
            beginAtZero: true,
            grace: "12%",
            title: {
              display: true,
              text: "Số trạm khác nhau",
              color: "#94a3b8",
              font: { size: 12, weight: "500" },
            },
            grid: { color: "rgba(148,163,184,.2)", drawBorder: false },
            ticks: { color: "#64748b", font: { size: 11.5 }, precision: 0, stepSize: 1 },
          },
        },
      },
    });

    const pct = cov.pct != null ? cov.pct : 0;
    const noise = cov.noise_legs_dropped || 0;
    const maxLegit = cov.max_legit_km || 200;
    const factor = cov.road_factor != null ? cov.road_factor : 1.3;
    const missingHome = (techs || []).filter((t) => !t.has_home).length;
    refs.workloadNote.textContent =
      "Đường đứt = TB km & TB số trạm (theo điểm đang hiện) · kích thước ∝ ticket · X = TB km nhà→trạm (chim bay × " + factor + ") · " +
      "Y = số trạm · > " + maxLegit + " km khỏi nhà = nhiễu · " +
      "toạ độ trạm phủ " + pct + "% (" + (cov.tickets_with_coords ?? 0) + "/" + (cov.tickets_total ?? 0) + ")" +
      (noise ? " · loại " + noise + " trạm nhiễu" : "") +
      " · không gồm Tây Nguyên (nhà thầu)" +
      (missingHome ? " · " + missingHome + " KT chưa có toạ độ nhà" : " · đủ toạ độ nhà") + ".";
  }

  function renderKPIsWorkload(payload) {
    const data = (payload && payload.tech_workload) || {};
    const techs = data.techs || [];
    const cov = data.coverage || {};
    const withTravel = techs.filter((t) => (t.leg_count || 0) > 0);
    const avgLeg = withTravel.length
      ? (withTravel.reduce((s, t) => s + (t.avg_leg_km || 0), 0) / withTravel.length).toFixed(1)
      : "—";
    const maxLegTech = withTravel.length
      ? withTravel.reduce((a, b) => ((a.avg_leg_km || 0) >= (b.avg_leg_km || 0) ? a : b))
      : null;
    setKPIs([
      { label: "Kỹ thuật viên", value: techs.length, sub: "có ticket trong 30 ngày" },
      { label: "TB chặng / KT", value: avgLeg, sub: "km (chỉ chặng ≤ " + (cov.max_legit_km || 200) + " km)" },
      {
        label: "Chặng xa nhất",
        value: maxLegTech ? (maxLegTech.avg_leg_km + " km") : "—",
        sub: maxLegTech ? maxLegTech.tech + " · " + maxLegTech.region : "—",
      },
      {
        label: "Phủ toạ độ",
        value: (cov.pct != null ? cov.pct + "%" : "—"),
        sub: (cov.tickets_with_coords ?? 0) + "/" + (cov.tickets_total ?? 0) + " ticket",
      },
    ]);
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
    if (!el) return;
    // Tech / Workload only — polar dùng renderPolarScopeTabs
    if (currentView !== "tech" && currentView !== "workload") {
      if (currentView !== "polar") {
        el.style.display = "none";
        el.innerHTML = "";
      }
      return;
    }
    el.style.display = "flex";
    el.className = "region-tabs" + (currentView === "workload" ? " workload-region-tabs" : "");
    el.innerHTML = "";

    // Chỉ loại Tây Nguyên ở Khối lượng KT (nhà thầu); tech / polar / region giữ đủ
    const EXCL_WL = new Set(["Tây Nguyên", "Tay Nguyen", "TÂY NGUYÊN"]);
    const regions = currentView === "workload"
      ? REGION_ORDER.filter((r) => !EXCL_WL.has(r))
      : REGION_ORDER.slice();

    if (currentView === "tech") {
      regions.forEach((r, idx) => {
        const col = colorFor(r, idx);
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "legend-chip" + (r === selectedRegion ? " active" : "");
        chip.textContent = r;
        if (r === selectedRegion) {
          chip.style.background = col.border;
          chip.style.borderColor = col.border;
          chip.style.color = "#fff";
        }
        chip.addEventListener("click", () => {
          selectedRegion = r;
          renderRegionTabs();
          refs.title.textContent = "Sự cố theo KT · " + selectedRegion;
          refs.desc.innerHTML = "Ticket giao KT khu vực <strong>" + selectedRegion + "</strong> · 30 ngày gần nhất · đến hết hôm qua · màu ô = số ticket";
          renderKPIsTech(currentPayload);
          renderHeatmap();
        });
        el.appendChild(chip);
      });
      return;
    }

    // workload: toggle visibility từng khu vực (giống legend "Theo khu vực")
    if (!window._workloadVisible) window._workloadVisible = {};
    regions.forEach((r, idx) => {
      if (!(r in window._workloadVisible)) window._workloadVisible[r] = true;
      const col = colorFor(r, idx);
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "legend-chip" + (window._workloadVisible[r] ? " active" : "");
      chip.textContent = r;
      if (window._workloadVisible[r]) {
        chip.style.background = col.border;
        chip.style.borderColor = col.border;
        chip.style.color = "#fff";
      }
      chip.addEventListener("click", () => {
        window._workloadVisible[r] = !window._workloadVisible[r];
        renderRegionTabs();
        renderWorkloadChart();
      });
      el.appendChild(chip);
    });
  }



  function sumSeries(arr) {
    if (!arr || !arr.length) return 0;
    return arr.reduce((s, v) => s + (Number(v) || 0), 0);
  }

  function buildPolarSegments() {
    if (!currentPayload) return { mode: "all", total: 0, segments: [] };

    if (polarScope === "all") {
      // Mặc định gồm mọi khu vực (kể cả Tây Nguyên) — chỉ workload mới loại
      const regions = currentPayload.regions || REGION_ORDER || [];
      const ds = currentPayload.datasets || {};
      const segments = regions.map((r, idx) => {
        const count = sumSeries(ds[r]);
        const col = colorFor(r, idx);
        return { key: r, label: r, count, color: col.border || col.fill };
      }).filter((s) => s.count > 0)
        .sort((a, b) => b.count - a.count);
      const total = segments.reduce((s, x) => s + x.count, 0);
      return { mode: "all", total, segments, titleRegion: null };
    }

    // drill-down theo KT trong 1 khu vực
    const region = polarScope;
    const by = (currentPayload.by_region || {})[region] || {};
    const techs = by.techs || [];
    const tds = by.datasets || {};
    const colBase = colorFor(region, REGION_ORDER.indexOf(region));
    const segments = techs.map((t, idx) => {
      const count = sumSeries(tds[t]);
      // biến thiên màu nhẹ theo index
      const shade = 0.55 + (idx % 5) * 0.08;
      return {
        key: t,
        label: t,
        count,
        color: colBase.border || "#0ea5e9",
        _idx: idx,
      };
    }).filter((s) => s.count > 0)
      .sort((a, b) => b.count - a.count);

    // Gán màu phân biệt cho từng KT
    const palette = [
      "#0ea5e9", "#22c55e", "#f59e0b", "#ef4444", "#a855f7",
      "#14b8a6", "#f97316", "#6366f1", "#ec4899", "#84cc16",
      "#06b6d4", "#e11d48",
    ];
    segments.forEach((s, i) => { s.color = palette[i % palette.length]; });

    const total = segments.reduce((s, x) => s + x.count, 0);
    return { mode: "region", total, segments, titleRegion: region };
  }

  function renderPolarScopeTabs() {
    const el = refs.regionTabs || refs.legend;
    if (!el) return;
    if (refs.legend && refs.legend !== el) {
      refs.legend.style.display = "none";
      refs.legend.innerHTML = "";
    }
    el.style.display = "flex";
    el.className = "region-tabs polar-scope-tabs";
    el.innerHTML = "";

    const scopes = ["all"].concat(REGION_ORDER || []);

    scopes.forEach((scope, idx) => {
      const active = polarScope === scope;
      const label = scope === "all" ? "Tất cả KV" : scope;
      const col = scope === "all"
        ? { border: "#0f172a" }
        : colorFor(scope, REGION_ORDER.indexOf(scope));
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "legend-chip" + (active ? " active" : "");
      chip.textContent = label;
      if (active) {
        chip.style.background = col.border;
        chip.style.borderColor = col.border;
        chip.style.color = "#fff";
      }
      chip.addEventListener("click", () => {
        polarScope = scope;
        renderPolarScopeTabs();
        renderPolarChart();
        // title
        if (scope === "all") {
          refs.title.textContent = "Phân bổ ticket theo khu vực";
          refs.desc.innerHTML = "Polar Area · tỷ trọng ticket 30 ngày · click khu vực bên phải hoặc nút KV để xem chi tiết KT";
        } else {
          refs.title.textContent = "Phân bổ ticket · " + scope;
          refs.desc.innerHTML = "Polar Area theo <strong>kỹ thuật viên</strong> khu vực <strong>" + scope + "</strong> · 30 ngày";
        }
      });
      el.appendChild(chip);
    });
  }

  function renderPolarChart() {
    const pack = buildPolarSegments();
    const segments = pack.segments || [];
    const total = pack.total || 0;

    if (!refs.polarWrap) return;
    refs.polarWrap.style.display = "block";

    const top = segments[0];
    const topPct = (top && total) ? ((top.count / total) * 100).toFixed(1) : "0";
    const nSeg = segments.length;
    const scopeLabel = pack.mode === "all" ? "Tất cả khu vực" : (pack.titleRegion || "");
    const entityLabel = pack.mode === "all" ? "khu vực" : "kỹ thuật viên";

    if (refs.polarKpis) {
      refs.polarKpis.innerHTML =
        '<div class="pstat">' +
          '<div class="plabel">Tổng ticket</div>' +
          '<div class="pval">' + total.toLocaleString("vi-VN") + '</div>' +
          '<div class="psub">30 ngày gần nhất · ' + scopeLabel + '</div>' +
        '</div>' +
        '<div class="pstat">' +
          '<div class="plabel">' + (pack.mode === "all" ? "Số khu vực" : "Số kỹ thuật") + '</div>' +
          '<div class="pval">' + nSeg + '</div>' +
          '<div class="psub">có phát sinh ticket</div>' +
        '</div>' +
        // '<div class="pstat wide">' +
        //   '<div class="plabel">Tỷ trọng cao nhất</div>' +
        //   '<div class="pval">' + (top ? topPct + "%" : "—") + '</div>' +
        //   '<div class="psub">' + (top
        //     ? top.label + " — " + top.count.toLocaleString("vi-VN") + " ticket"
        //     : "—") + '</div>' +
        '</div>';
    }

    if (refs.polarList) {
      if (!segments.length) {
        refs.polarList.innerHTML =
          '<div style="padding:16px;color:#94a3b8;text-align:center;">Không có dữ liệu.</div>';
      } else {
        const head =
          '<div class="plist-head">' +
            '<span>' + (pack.mode === "all" ? "Khu vực" : "Kỹ thuật viên") + '</span>' +
            '<span style="text-align:right">Ticket</span>' +
            '<span style="text-align:right">Tỷ trọng</span>' +
          '</div>';
        const body = segments.map((s, i) => {
          const pct = total ? ((s.count / total) * 100) : 0;
          const pctStr = pct.toFixed(1) + "%";
          return (
            '<div class="prow">' +
              '<div class="pname-cell">' +
                '<span class="prank">' + (i + 1) + '</span>' +
                '<span class="pdot" style="background:' + s.color + '"></span>' +
                '<span class="pname" title="' + s.label + '">' + s.label + '</span>' +
              '</div>' +
              '<div class="pcount">' + s.count.toLocaleString("vi-VN") + '</div>' +
              '<div class="ppct">' + pctStr + '</div>' +
              '<div class="pbar-wrap"><div class="pbar" style="width:' + pct + '%;background:' + s.color + '"></div></div>' +
            '</div>'
          );
        }).join("");
        refs.polarList.innerHTML = head + '<div class="plist-body">' + body + '</div>';
      }
    }

    if (refs.polarNote) {
      refs.polarNote.innerHTML = pack.mode === "all"
        ? "Mỗi lát trên biểu đồ tương ứng <strong>một khu vực</strong>. Bán kính (diện tích) tỉ lệ với số ticket trong 30 ngày. Dùng nút khu vực phía trên bên phải để xem phân bổ theo kỹ thuật viên."
        : "Mỗi lát = <strong>một kỹ thuật viên</strong> tại <strong>" + pack.titleRegion + "</strong>. So sánh khối lượng ticket trong khu vực. Chọn «Tất cả KV» để quay lại tổng quan.";
    }

    if (polarChart) { polarChart.destroy(); polarChart = null; }
    if (!segments.length || !refs.polarCanvas) return;

    polarChart = new Chart(refs.polarCanvas.getContext("2d"), {
      type: "polarArea",
      data: {
        labels: segments.map((s) => s.label),
        datasets: [{
          data: segments.map((s) => s.count),
          backgroundColor: segments.map((s) => s.color + "cc"),
          borderColor: segments.map((s) => s.color),
          borderWidth: 1.5,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#0f172a",
            titleFont: { size: 13, weight: "600" },
            bodyFont: { size: 12.5 },
            padding: 12,
            cornerRadius: 8,
            callbacks: {
              label: (ctx) => {
                const v = Number(ctx.raw) || 0;
                const pct = total ? ((v / total) * 100).toFixed(1) : "0";
                return " " + v.toLocaleString("vi-VN") + " ticket (" + pct + "%)";
              },
            },
          },
        },
        scales: {
          r: {
            beginAtZero: true,
            ticks: {
              backdropColor: "rgba(255,255,255,.85)",
              color: "#94a3b8",
              font: { size: 10 },
              z: 1,
              callback: (v) => (Number(v) >= 1000 ? (v / 1000).toFixed(v % 1000 === 0 ? 0 : 1) + "k" : v),
            },
            grid: { color: "rgba(148,163,184,.22)" },
            angleLines: { color: "rgba(148,163,184,.18)" },
          },
        },
      },
    });
  }

  function applyView(view) {
    currentView = view;
    visibleSeries = {};
    refs.viewToggle.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === view);
    });
    refs.totalStrip.style.display = "none";
    /* legend / region-tabs: bật tắt trong từng nhánh view */
    if (refs.polarWrap) refs.polarWrap.style.display = "none";

    if (view === "region") {
      refs.title.textContent = "Số lượng sự cố theo ngày";
      refs.desc.innerHTML = "Ticket theo <strong>Create Time</strong> · <strong>30 ngày</strong> gần nhất · đến hết hôm qua · theo khu vực";
    } else if (view === "total") {
      refs.title.textContent = "Tổng số sự cố theo ngày (tất cả khu vực)";
      refs.desc.innerHTML = "Gộp toàn bộ khu vực đang quản lý · <strong>30 ngày</strong> gần nhất · đến hết hôm qua";
    } else if (view === "tech") {
      refs.title.textContent = "Sự cố theo KT · " + selectedRegion;
      refs.desc.innerHTML = "Ticket giao KT khu vực <strong>" + selectedRegion + "</strong> · 30 ngày gần nhất · đến hết hôm qua · màu ô = số ticket";
    } else if (view === "polar") {
      if (polarScope === "all") {
        refs.title.textContent = "Phân bổ ticket theo khu vực";
        refs.desc.innerHTML = "Polar Area · tỷ trọng ticket <strong>30 ngày</strong> · nút bên phải chọn KV để xem theo kỹ thuật viên";
      } else {
        refs.title.textContent = "Phân bổ ticket · " + polarScope;
        refs.desc.innerHTML = "Polar Area theo <strong>kỹ thuật viên</strong> · khu vực <strong>" + polarScope + "</strong> · 30 ngày";
      }
    } else if (view === "workload") {
      refs.title.textContent = "Khối lượng công việc KT (ticket × quãng đường)";
      refs.desc.innerHTML = "Mỗi điểm = 1 kỹ thuật viên · chỉ ticket <strong>Tại trạm</strong> · <strong>X</strong> = TB km <strong>nhà→trạm</strong> · <strong>Y</strong> = số trạm · kích thước ∝ số ticket · màu = khu vực · 30 ngày";
    }

    renderRegionTabs();
    if (!currentPayload) return;

    if (view === "region") {
      refs.chartWrap.style.display = "block";
      refs.heatmapWrap.style.display = "none";
      if (refs.workloadOnlyWrap) refs.workloadOnlyWrap.style.display = "none";
      if (refs.polarWrap) refs.polarWrap.style.display = "none";
      if (refs.regionTabs) { refs.regionTabs.style.display = "none"; refs.regionTabs.innerHTML = ""; }
      if (refs.legend) refs.legend.style.display = "flex";
      renderKPIsRegion(currentPayload);
      renderLegend();
      drawLineChart();
    } else if (view === "total") {
      refs.chartWrap.style.display = "block";
      refs.heatmapWrap.style.display = "none";
      if (refs.workloadOnlyWrap) refs.workloadOnlyWrap.style.display = "none";
      if (refs.polarWrap) refs.polarWrap.style.display = "none";
      if (refs.legend) { refs.legend.style.display = "none"; refs.legend.innerHTML = ""; }
      if (refs.regionTabs) { refs.regionTabs.style.display = "none"; refs.regionTabs.innerHTML = ""; }
      const series = currentPayload.total_series || {};
      renderKPIsTotal(series);
      renderTotalStatStrip(series);
      drawTotalChart(series);
    } else if (view === "tech") {
      refs.chartWrap.style.display = "none";
      refs.heatmapWrap.style.display = "block";
      if (refs.workloadOnlyWrap) refs.workloadOnlyWrap.style.display = "none";
      if (refs.polarWrap) refs.polarWrap.style.display = "none";
      if (refs.legend) { refs.legend.style.display = "none"; refs.legend.innerHTML = ""; }
      renderRegionTabs();
      renderKPIsTech(currentPayload);
      renderHeatmap();
    } else if (view === "workload") {
      refs.chartWrap.style.display = "none";
      refs.heatmapWrap.style.display = "none";
      if (refs.workloadOnlyWrap) refs.workloadOnlyWrap.style.display = "block";
      if (refs.polarWrap) refs.polarWrap.style.display = "none";
      if (refs.legend) { refs.legend.style.display = "none"; refs.legend.innerHTML = ""; }
      renderRegionTabs();
      renderKPIsWorkload(currentPayload);
      renderWorkloadChart();
    } else if (view === "polar") {
      refs.chartWrap.style.display = "none";
      refs.heatmapWrap.style.display = "none";
      if (refs.workloadOnlyWrap) refs.workloadOnlyWrap.style.display = "none";
      if (refs.legend) { refs.legend.style.display = "none"; refs.legend.innerHTML = ""; }
      if (refs.polarWrap) refs.polarWrap.style.display = "block";
      renderPolarScopeTabs();
      renderPolarChart();
      // KPI strip dùng stats bên cạnh polar
      setKPIs([
        { label: "Phạm vi", value: polarScope === "all" ? "Tất cả KV" : polarScope, sub: "30 ngày" },
        { label: "Biểu đồ", value: "Polar Area", sub: polarScope === "all" ? "theo khu vực" : "theo KT" },
      ]);
    }
  }


  // ----- nạp dữ liệu / vòng đời module -----
  function renderSourceNote(payload) {
    Core.setSourceBadge(payload.source === "sample" ? "Dữ liệu mẫu" : "Cache");
    if (payload.source === "sample") {
      refs.sourceNote.textContent = "⚠ Cache mẫu — restart app để cào live 60 ngày.";
      refs.sourceNote.className = "source-note sample";
    } else {
      const m = payload.meta || {};
      refs.sourceNote.textContent =
        "Dữ liệu " + (payload.scrape_days || 60) + " ngày · [" + (m.start_time || "?") + " → " + (m.end_time || "?") + ") · " + (payload.generated_at || "");
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
        <button type="button" data-view="polar">Phân bổ ticket</button>
        <button type="button" data-view="workload">Khối lượng công việc</button>
        <button type="button" data-view="tech">Ticket theo kỹ thuật</button>
      </div>
      <div class="card-header">
        <div class="header-main">
          <h2 class="chart-title">Tổng số sự cố theo ngày (tất cả khu vực)</h2>
          <div class="desc chart-desc">Gộp toàn bộ khu vực đang quản lý · <strong>30 ngày</strong> gần nhất · đến hết hôm qua</div>
        </div>
        <div class="legend-toggle" style="display:none;"></div>
        <div class="region-tabs" style="display:none;"></div>
      </div>
      <div class="loading chart-loading">⏳ Đang tải dữ liệu thống kê…</div>
      <div class="total-stat-strip" style="display:none;"></div>
      <div class="chart-wrap" style="display:none;"><canvas></canvas></div>
      <div class="heatmap-wrap" style="display:none;">
        <div class="heatmap-scroll"></div>
        <div class="heatmap-legend"></div>
      </div>
      <div class="workload-only-wrap" style="display:none;">
        <div class="workload-wrap" style="margin-top:0;padding-top:0;border-top:none;">
          <div class="workload-chart-wrap" style="display:none;"><canvas class="workload-canvas"></canvas></div>
          <div class="workload-note"></div>
        </div>
      </div>
      <div class="polar-wrap" style="display:none;">
        <div class="polar-layout">
          <div class="polar-chart-box"><canvas class="polar-canvas"></canvas></div>
          <div class="polar-side">
            <div class="polar-kpis"></div>
            <div class="polar-list"></div>
            <div class="polar-note"></div>
          </div>
        </div>
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
      canvas: panel.querySelector(".chart-wrap canvas"),
      heatmapWrap: panel.querySelector(".heatmap-wrap"),
      heatmapScroll: panel.querySelector(".heatmap-scroll"),
      heatmapLegendEl: panel.querySelector(".heatmap-legend"),
      workloadOnlyWrap: panel.querySelector(".workload-only-wrap"),
      workloadChartWrap: panel.querySelector(".workload-chart-wrap"),
      workloadCanvas: panel.querySelector(".workload-canvas"),
      workloadNote: panel.querySelector(".workload-note"),
      polarWrap: panel.querySelector(".polar-wrap"),
      polarCanvas: panel.querySelector(".polar-canvas"),
      polarKpis: panel.querySelector(".polar-kpis"),
      polarList: panel.querySelector(".polar-list"),
      polarNote: panel.querySelector(".polar-note"),
      sourceNote: panel.querySelector(".source-note"),
    };
    refs.viewToggle.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-view]");
      if (btn) applyView(btn.dataset.view);
    });
  }

  Core.registerChart({
    key: "volume",
    label: "Thống kê sự cố",
    mount,
    onShow,
    onCpTypeChange,
  });
})();