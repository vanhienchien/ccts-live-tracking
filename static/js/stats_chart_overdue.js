/**
 * stats_chart_overdue.js — module biểu đồ "Tỷ lệ Overdue khi đóng".
 * Nguồn dữ liệu: GET /api/stats/overdue-rate.
 * Tự đăng ký vào StatsCore — xem stats_core.js để biết cách thêm module mới.
 */
(function () {
  "use strict";
  const Core = window.StatsCore;
  const { REGION_ORDER, lerp, rgb, setKPIs, colorFor } = Core;

  Core.injectStyleOnce("stats-style-overdue-region-chips", `
    .card-header {
      display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: space-between;
      gap: 12px 16px;
    }
    .card-header .header-main { flex: 1 1 220px; min-width: 0; }
    .card-header .region-tabs,
    .card-header .header-region-filters {
      display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
      justify-content: flex-end; margin: 0; flex: 0 1 auto;
      max-width: 100%;
    }
    .region-tabs .legend-chip,
    .header-region-filters .legend-chip {
      display: inline-flex; align-items: center; justify-content: center;
      padding: 6px 14px; border-radius: 999px; font-size: 12.5px; font-weight: 600;
      border: 1.5px solid #cbd5e1; background: #f1f5f9; color: #64748b;
      cursor: pointer; user-select: none;
      transition: background .15s, color .15s, border-color .15s, box-shadow .15s;
    }
    .region-tabs .legend-chip:hover,
    .header-region-filters .legend-chip:hover { filter: brightness(0.97); }
    .region-tabs .legend-chip.active,
    .header-region-filters .legend-chip.active {
      color: #fff; border-color: transparent;
      box-shadow: 0 2px 6px rgba(15, 23, 42, 0.12);
    }
  `);


  Core.injectStyleOnce("stats-style-resolution-tip", `
    .res-tip {
      position: fixed; z-index: 9999; pointer-events: none;
      background: #0f172a; color: #f8fafc; border-radius: 10px;
      padding: 10px 12px; font-size: 12.5px; line-height: 1.45;
      box-shadow: 0 8px 24px rgba(15,23,42,.28);
      max-width: 320px; border: 1px solid rgba(148,163,184,.25);
      display: none;
    }
    .res-tip .rt-title { font-weight: 700; font-size: 13px; margin-bottom: 6px; color: #fff; }
    .res-tip .rt-row { display: flex; justify-content: space-between; gap: 16px; padding: 1px 0; }
    .res-tip .rt-row .k { color: #94a3b8; }
    .res-tip .rt-row .v { font-weight: 600; color: #f1f5f9; text-align: right; }
    .res-tip .rt-sep { height: 1px; background: rgba(148,163,184,.25); margin: 6px 0; }
    .res-tip .rt-badge {
      display: inline-block; font-size: 10.5px; font-weight: 700;
      padding: 1px 6px; border-radius: 999px; margin-left: 6px; vertical-align: middle;
    }
    .res-tip .rt-badge.far { background: #fef2f2; color: #b91c1c; }
    .res-tip .rt-badge.ok { background: #ecfdf5; color: #047857; }
  `);


  Core.injectStyleOnce("stats-style-overdue-matrix", `
    .quad-legend { display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 14px; }
    .quad-legend .qitem {
      flex: 1 1 200px; border-radius: 10px; padding: 10px 12px; border: 1px solid var(--border);
      background: #f8fafc; font-size: 12px; line-height: 1.45;
    }
    .quad-legend .qitem .qtitle { font-weight: 700; font-size: 12.5px; margin-bottom: 2px; }
    .quad-legend .qitem .qcount { font-weight: 600; color: var(--text-muted); font-size: 11px; }
    .quad-legend .qitem.star { border-left: 4px solid #16a34a; }
    .quad-legend .qitem.overload { border-left: 4px solid #ea580c; }
    .quad-legend .qitem.improve { border-left: 4px solid #dc2626; }
    .quad-legend .qitem.idle { border-left: 4px solid #2563eb; }
    .matrix-wrap { position: relative; height: 480px; width: 100%; }
  `);


  let chart = null;
  let fullPayload = null;
  let currentPayload = null;
  let currentView = "region";
  let selectedRegion = "LDO-BTH";
  let panelEl = null;
  let refs = null;

  function slicePayload(full, cp) {
    if (!full) return null;
    const by = full.by_cp_type || {};
    const sub = by[cp] || by.all || full;
    return Object.assign({}, full, sub, {
      by_cp_type: full.by_cp_type,
      counts: full.counts,
      chart_days: full.chart_days,
      source: full.source,
      generated_at: full.generated_at,
      meta: full.meta,
      cp_type: cp,
      // Ma trận & boxplot theo đúng filter EV/BSS/all (từ sub)
      performance_matrix: (sub && sub.performance_matrix) || full.performance_matrix,
      resolution_boxplot: (sub && sub.resolution_boxplot) || full.resolution_boxplot,
      resolution_boxplot_by_cp: full.resolution_boxplot_by_cp || {
        all: full.resolution_boxplot,
        ev: (full.by_cp_type && full.by_cp_type.ev && full.by_cp_type.ev.resolution_boxplot),
        bss: (full.by_cp_type && full.by_cp_type.bss && full.by_cp_type.bss.resolution_boxplot),
      },
    });
  }

  function efficiencyToColor(pct) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    if (p <= 70) {
      const t = p / 70;
      return rgb(lerp(248, 251, t), lerp(113, 191, t), lerp(113, 36, t));
    }
    const t = (p - 70) / 30;
    return rgb(lerp(251, 74, t), lerp(191, 222, t), lerp(36, 128, t));
  }

  function rateToColor(pct) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    if (p <= 10) {
      const t = p / 10;
      return rgb(lerp(254, 239, t), lerp(226, 68, t), lerp(226, 68, t));
    }
    const t = (p - 10) / 90;
    return rgb(lerp(239, 127, t), lerp(68, 29, t), lerp(68, 29, t));
  }

  function getOdPackForView() {
    if (!currentPayload) return null;
    if (currentView === "region") return currentPayload.by_region_rates || null;
    if (currentView === "tech") return (currentPayload.by_tech_rates || {})[selectedRegion] || null;
    if (currentView === "top_od") return currentPayload.top10_overdue || null;
    if (currentView === "top_eff") return currentPayload.top10_efficiency || null;
    if (currentView === "top_vol") return currentPayload.top10_volume || null;
    return null;
  }


  const QUAD_META = {
    star: {
      key: "star",
      label: "Xuất sắc",
      emoji: "🟢",
      color: "#16a34a",
      bg: "rgba(22,163,74,.75)",
      action: "Kỹ thuật tốt / Cần cù siêng năng",
    },
    overload: {
      key: "overload",
      label: "Quá tải",
      emoji: "🟠",
      color: "#ea580c",
      bg: "rgba(234,88,12,.75)",
      action: "Bổ sung kỹ thuật hoặc chia bớt trạm",
    },
    improve: {
      key: "improve",
      label: "Cần cải thiện",
      emoji: "🔴",
      color: "#dc2626",
      bg: "rgba(220,38,38,.75)",
      action: "Rà soát tay nghề / thái độ làm việc",
    },
    idle: {
      key: "idle",
      label: "Ít việc",
      emoji: "🔵",
      color: "#2563eb",
      bg: "rgba(37,99,235,.75)",
      action: "Hỗ trợ vùng quá tải / Phân thêm trạm",
    },
  };

  function renderQuadLegend(matrix) {
    const el = refs.quadLegend;
    if (!el) return;
    const counts = (matrix && matrix.quadrant_counts) || {};
    const order = ["star", "overload", "improve", "idle"];
    el.innerHTML = order.map((k) => {
      const m = QUAD_META[k];
      const n = counts[k] || 0;
      return (
        '<div class="qitem ' + k + '">' +
        '<div class="qtitle">' + m.emoji + " " + m.label +
        ' <span class="qcount">(' + n + ' KT)</span></div>' +
        '<div>' + m.action + "</div></div>"
      );
    }).join("");
  }

  function drawMatrix() {
    const matrix = (currentPayload && currentPayload.performance_matrix) || {};
    const points = matrix.points || [];
    const medVol = Number(matrix.median_volume) || 0;
    const medRate = Number(matrix.median_rate_pct) || 0;
    const xLabel = matrix.x_axis_label || "Ticket Tại trạm";

    if (!points.length) {
      refs.loading.style.display = "block";
      refs.loading.textContent = "Không đủ KT (≥ " + (matrix.min_closed || 3) + " ticket đóng) để vẽ ma trận.";
      refs.chartWrap.style.display = "none";
      refs.matrixWrap.style.display = "none";
      refs.quadLegend.style.display = "none";
      if (chart) { chart.destroy(); chart = null; }
      return;
    }

    refs.loading.style.display = "none";
    refs.chartWrap.style.display = "none";
    refs.matrixWrap.style.display = "block";
    refs.quadLegend.style.display = "flex";
    renderQuadLegend(matrix);

    // Kích thước điểm theo khối lượng trục X
    const maxX = Math.max(1, ...points.map((p) => p.x || 0));
    const minR = 6;
    const maxR = 22;
    function radiusFor(x) {
      return minR + (maxR - minR) * Math.sqrt(Math.max(0, x) / maxX);
    }

    // Nhóm điểm trùng toạ độ (cùng x,y) để tooltip liệt kê hết tên
    const clusterKey = (p) => Math.round(p.x * 10) + "|" + Math.round(p.y * 10);
    const clusters = {};
    points.forEach((p) => {
      const k = clusterKey(p);
      (clusters[k] = clusters[k] || []).push(p);
    });

    const byQ = { star: [], overload: [], improve: [], idle: [] };
    points.forEach((p) => {
      const q = p.quadrant || "idle";
      if (!byQ[q]) byQ[q] = [];
      byQ[q].push(p);
    });

    const datasets = ["star", "overload", "improve", "idle"].map((k) => {
      const m = QUAD_META[k];
      return {
        label: m.emoji + " " + m.label,
        data: (byQ[k] || []).map((p) => ({
          x: p.x,
          y: p.y,
          r: radiusFor(p.x),
          _p: p,
          _cluster: clusters[clusterKey(p)] || [p],
        })),
        backgroundColor: m.bg,
        borderColor: m.color,
        borderWidth: 1.5,
        // bubble dùng r; scatter dùng pointRadius callback
        pointRadius: (ctx) => {
          const raw = ctx.raw;
          return raw && raw.r != null ? raw.r : 8;
        },
        pointHoverRadius: (ctx) => {
          const raw = ctx.raw;
          const base = raw && raw.r != null ? raw.r : 8;
          return base + 3;
        },
      };
    });

    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    const xMax = Math.max(medVol * 1.15, Math.max(...xs, 1) * 1.15, 5);
    const yMax = Math.max(medRate * 1.2, Math.max(...ys, 1) * 1.15, 15);

    // Đường median (khớp phân loại 4 góc phía backend)
    datasets.push({
      label: "Median volume",
      type: "line",
      data: [{ x: medVol, y: 0 }, { x: medVol, y: yMax }],
      borderColor: "rgba(71,85,105,.75)",
      borderWidth: 1.5,
      borderDash: [6, 4],
      pointRadius: 0,
      fill: false,
      order: 0,
    });
    datasets.push({
      label: "Median OD %",
      type: "line",
      data: [{ x: 0, y: medRate }, { x: xMax, y: medRate }],
      borderColor: "rgba(71,85,105,.75)",
      borderWidth: 1.5,
      borderDash: [6, 4],
      pointRadius: 0,
      fill: false,
      order: 0,
    });

    // Plugin tô nền 4 góc + nhãn điểm bất thường
    const bgPlugin = {
      id: "quadBackground",
      beforeDraw(chartInstance) {
        const { ctx, chartArea, scales } = chartInstance;
        if (!chartArea) return;
        const xScale = scales.x;
        const yScale = scales.y;
        const xMed = xScale.getPixelForValue(medVol);
        const yMed = yScale.getPixelForValue(medRate);
        const regions = [
          // dưới-phải: ngôi sao (high vol, low OD)
          { x0: xMed, x1: chartArea.right, y0: yMed, y1: chartArea.bottom, color: "rgba(22,163,74,.06)" },
          // trên-phải: quá tải
          { x0: xMed, x1: chartArea.right, y0: chartArea.top, y1: yMed, color: "rgba(234,88,12,.07)" },
          // trên-trái: cần cải thiện
          { x0: chartArea.left, x1: xMed, y0: chartArea.top, y1: yMed, color: "rgba(220,38,38,.07)" },
          // dưới-trái: ít việc
          { x0: chartArea.left, x1: xMed, y0: yMed, y1: chartArea.bottom, color: "rgba(37,99,235,.06)" },
        ];
        regions.forEach((r) => {
          ctx.save();
          ctx.fillStyle = r.color;
          ctx.fillRect(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0);
          ctx.restore();
        });
      },
    };

    // Nhãn tên cho điểm bất thường:
    // - OD cao bất thường (> median + 15pp hoặc top theo OD)
    // - Volume rất cao (> median * 1.8)
    // - Góc improve với OD cao
    // - Mỗi cluster chỉ nhãn 1 lần (tên ghép nếu trùng)
    // Chỉ gắn tên khi: % OD > 30 VÀ khối lượng (trục X / Tại trạm) > 300
    const outlierKeys = new Set();
    const LABEL_OD_MIN = 30;
    const LABEL_VOL_MIN = 300;
    points.forEach((p) => {
      if ((p.y || 0) > LABEL_OD_MIN && (p.x || 0) > LABEL_VOL_MIN) {
        outlierKeys.add(clusterKey(p));
      }
    });

    const labelPlugin = {
      id: "outlierLabels",
      afterDatasetsDraw(chartInstance) {
        const { ctx } = chartInstance;
        const seen = new Set();
        chartInstance.data.datasets.forEach((ds, di) => {
          if (ds.type === "line" || String(ds.label || "").startsWith("Median")) return;
          const meta = chartInstance.getDatasetMeta(di);
          meta.data.forEach((el, i) => {
            const raw = ds.data[i];
            if (!raw || !raw._p) return;
            const k = clusterKey(raw._p);
            if (!outlierKeys.has(k) || seen.has(k)) return;
            seen.add(k);
            const cluster = raw._cluster || [raw._p];
            // Tối đa 3 tên, còn lại +N
            let text;
            if (cluster.length === 1) {
              text = cluster[0].tech;
            } else if (cluster.length <= 3) {
              text = cluster.map((c) => c.tech).join(", ");
            } else {
              text = cluster.slice(0, 2).map((c) => c.tech).join(", ") + " +" + (cluster.length - 2);
            }
            const pos = el.getProps(["x", "y"], true);
            ctx.save();
            ctx.font = "600 11px system-ui, sans-serif";
            ctx.fillStyle = "#0f172a";
            ctx.strokeStyle = "rgba(255,255,255,.9)";
            ctx.lineWidth = 3;
            ctx.textAlign = "center";
            ctx.textBaseline = "bottom";
            const tx = pos.x;
            const ty = pos.y - (raw.r || 8) - 4;
            ctx.strokeText(text, tx, ty);
            ctx.fillText(text, tx, ty);
            ctx.restore();
          });
        });
      },
    };


    // Nhãn số trên đường median (volume / OD %)
    const refLineLabelPlugin = {
      id: "refLineLabels",
      afterDatasetsDraw(chartInstance) {
        const { ctx, chartArea, scales } = chartInstance;
        if (!chartArea) return;
        const xScale = scales.x;
        const yScale = scales.y;
        const xMedPx = xScale.getPixelForValue(medVol);
        const yMedPx = yScale.getPixelForValue(medRate);
        const volText = "Median " + (Math.round(medVol * 10) / 10);
        const rateText = "Median " + (Math.round(medRate * 10) / 10) + "%";

        ctx.save();
        ctx.font = "600 11px system-ui, sans-serif";
        ctx.textBaseline = "middle";

        // Vertical median — label near top of chart
        ctx.textAlign = "left";
        const vt = volText;
        const vw = ctx.measureText(vt).width;
        let vx = xMedPx + 6;
        if (vx + vw + 8 > chartArea.right) vx = xMedPx - vw - 8;
        const vy = chartArea.top + 12;
        ctx.fillStyle = "rgba(255,255,255,.92)";
        ctx.strokeStyle = "rgba(71,85,105,.35)";
        ctx.lineWidth = 1;
        const pad = 4;
        ctx.beginPath();
        ctx.roundRect
          ? ctx.roundRect(vx - pad, vy - 8, vw + pad * 2, 16, 4)
          : ctx.rect(vx - pad, vy - 8, vw + pad * 2, 16);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "#334155";
        ctx.fillText(vt, vx, vy);

        // Horizontal median — label near right edge
        ctx.textAlign = "right";
        const rt = rateText;
        const rw = ctx.measureText(rt).width;
        const rx = chartArea.right - 6;
        let ry = yMedPx - 12;
        if (ry < chartArea.top + 10) ry = yMedPx + 14;
        ctx.fillStyle = "rgba(255,255,255,.92)";
        ctx.beginPath();
        ctx.roundRect
          ? ctx.roundRect(rx - rw - pad, ry - 8, rw + pad * 2, 16, 4)
          : ctx.rect(rx - rw - pad, ry - 8, rw + pad * 2, 16);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "#334155";
        ctx.fillText(rt, rx, ry);

        ctx.restore();
      },
    };

    if (chart) chart.destroy();
    chart = new Chart(refs.matrixCanvas.getContext("2d"), {
      type: "scatter",
      data: { datasets },
      plugins: [bgPlugin, labelPlugin, refLineLabelPlugin],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            display: true,
            position: "top",
            labels: {
              filter: (item) => !String(item.text).startsWith("Median"),
              boxWidth: 12,
              font: { size: 11.5 },
              color: "#64748b",
            },
          },
          tooltip: {
            backgroundColor: "#0f172a",
            titleFont: { size: 13, weight: "600" },
            bodyFont: { size: 12.5 },
            padding: 12,
            cornerRadius: 8,
            filter: (item) => item.dataset.type !== "line" && !String(item.dataset.label || "").startsWith("Median"),
            callbacks: {
              title: (items) => {
                const raw = items[0].raw;
                if (!raw) return "";
                const cluster = raw._cluster || (raw._p ? [raw._p] : []);
                if (cluster.length <= 1) {
                  const p = cluster[0] || raw._p;
                  const m = QUAD_META[p.quadrant] || {};
                  return (m.emoji || "") + " " + p.tech + (p.region ? " · " + p.region : "");
                }
                return "📍 " + cluster.length + " KT trùng vị trí";
              },
              label: (item) => {
                const raw = item.raw;
                if (!raw) return "";
                const cluster = raw._cluster || (raw._p ? [raw._p] : []);
                if (cluster.length <= 1) {
                  const p = cluster[0] || raw._p;
                  const m = QUAD_META[p.quadrant] || {};
                  return [
                    " " + xLabel + ": " + p.x,
                    " Tổng đóng: " + p.closed + " (tại trạm " + (p.onsite ?? "—") + " · từ xa " + (p.remote ?? "—") + ")",
                    " Overdue: " + p.overdue + " (" + p.rate_pct + "%)",
                    " Góc: " + (m.label || p.quadrant),
                  ];
                }
                // Liệt kê từng KT trong cluster
                const lines = [];
                cluster.forEach((p) => {
                  const m = QUAD_META[p.quadrant] || {};
                  lines.push(
                    " " + (m.emoji || "") + " " + p.tech +
                    (p.region ? " (" + p.region + ")" : "") +
                    " — đóng " + p.closed + ", OD " + p.rate_pct + "%"
                  );
                });
                return lines;
              },
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: 0,
            suggestedMax: xMax,
            title: {
              display: true,
              text: xLabel + " (khối lượng) →",
              color: "#94a3b8",
              font: { size: 12, weight: "500" },
            },
            grid: { color: "rgba(148,163,184,.2)" },
            ticks: { color: "#64748b", precision: 0 },
          },
          y: {
            type: "linear",
            min: 0,
            suggestedMax: yMax,
            title: {
              display: true,
              text: "% Overdue →",
              color: "#94a3b8",
              font: { size: 12, weight: "500" },
            },
            grid: { color: "rgba(148,163,184,.2)" },
            ticks: {
              color: "#64748b",
              callback: (v) => v + "%",
            },
          },
        },
      },
    });
  }

  function drawChart() {
    const pack = getOdPackForView();
    if (!pack || !(pack.labels || []).length) {
      refs.loading.style.display = "block";
      refs.loading.textContent = "Không có dữ liệu cho view này.";
      refs.chartWrap.style.display = "none";
      if (chart) { chart.destroy(); chart = null; }
      return;
    }
    refs.loading.style.display = "none";
    refs.chartWrap.style.display = "block";
    refs.chartWrap.style.height = Math.max(280, (pack.labels || []).length * 44 + 90) + "px";

    const isVolume = currentView === "top_vol";
    const isEff = currentView === "top_eff";
    // Vạch OD trên region/tech/top_od; vạch "Tại trạm" trên top_vol
    const showOdTick = currentView === "region" || currentView === "tech" || currentView === "top_od";
    const showOnsiteTick = isVolume;

    let values;
    if (isVolume) values = pack.closed_counts || [];
    else if (isEff) values = pack.efficiency_pct || pack.values_pct || [];
    else values = pack.rates_pct || pack.values_pct || [];

    const ticksPct = pack.rates_tick_pct || [];
    const closed = pack.closed_counts || [];
    const overdue = pack.overdue_counts || [];
    const overdueSubj = pack.overdue_subjective_counts || [];
    const overdueTick = pack.overdue_tick_counts || [];
    const onsiteCounts = pack.onsite_counts || [];
    const remoteCounts = pack.remote_counts || [];
    const labels = pack.labels || [];

    const colors = isVolume
      ? values.map(() => "rgba(56, 189, 248, 0.9)")
      : isEff
        ? values.map((v) => efficiencyToColor(Number(v || 0)))
        : values.map((v) => rateToColor(Number(v || 0)));

    const odTickData = showOdTick ? ticksPct.map((v, i) => ({ x: Number(v) || 0, y: i })) : [];
    const onsiteTickData = showOnsiteTick
      ? onsiteCounts.map((v, i) => ({ x: Number(v) || 0, y: i }))
      : [];

    const datasets = [
      {
        type: "bar",
        label: isVolume ? "Số đóng" : (isEff ? "% Hiệu quả" : "% Overdue"),
        data: values.map((v) => Number(v) || 0),
        backgroundColor: colors,
        borderWidth: 0,
        borderRadius: 5,
        barThickness: 18,
        order: 2,
      },
    ];
    if (showOdTick) {
      datasets.push({
        type: "scatter",
        label: "Vạch (OD − OD chủ quan)",
        data: odTickData,
        parsing: false,
        pointStyle: "line",
        rotation: 90,
        radius: 14,
        borderWidth: 3,
        borderColor: "#1e293b",
        backgroundColor: "#1e293b",
        order: 1,
        datalabels: {
          display: true,
          // Số ticket OD thực (đã trừ OD chủ quan VT/hẹn) — khớp ý nghĩa vạch
          formatter: (val, ctx) => {
            const i = ctx.dataIndex;
            const n = overdueTick[i];
            return n != null ? String(n) : "";
          },
          align: "top",
          anchor: "center",
          offset: 2,
          color: "#0f172a",
          font: { size: 11, weight: "700" },
          backgroundColor: "rgba(255,255,255,.9)",
          borderRadius: 3,
          padding: { top: 1, bottom: 1, left: 4, right: 4 },
        },
      });
    }
    if (showOnsiteTick) {
      datasets.push({
        type: "scatter",
        label: "Tại trạm",
        data: onsiteTickData,
        parsing: false,
        pointStyle: "line",
        rotation: 90,
        radius: 14,
        borderWidth: 3,
        borderColor: "#b45309",
        backgroundColor: "#b45309",
        order: 1,
        datalabels: {
          display: true,
          formatter: (val) => {
            const n = val && val.x != null ? val.x : val;
            return n != null ? String(n) : "";
          },
          align: "top",
          anchor: "center",
          offset: 2,
          color: "#92400e",
          font: { size: 11, weight: "700" },
          backgroundColor: "rgba(255,255,255,.85)",
          borderRadius: 3,
          padding: { top: 1, bottom: 1, left: 4, right: 4 },
        },
      });
    }

    const maxX = isVolume
      ? Math.max(5, Math.ceil(Math.max(0, ...values.map(Number)) * 1.2) || 5)
      : 100;

    if (chart) chart.destroy();
    chart = new Chart(refs.canvas.getContext("2d"), {
      type: "bar",
      plugins: typeof ChartDataLabels !== "undefined" ? [ChartDataLabels] : [],
      data: { labels, datasets },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        layout: { padding: { right: 56 } },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (item) => {
                if (item.dataset.type === "scatter") {
                  const i = item.dataIndex;
                  if (item.dataset.label === "Tại trạm") {
                    return [
                      " Tại trạm: " + (onsiteCounts[i] ?? "—"),
                      " Từ xa: " + (remoteCounts[i] ?? "—"),
                    ];
                  }
                  return [
                    " Vạch: " + (ticksPct[i] ?? "—") + "%",
                    " OD − OD chủ quan: " + (overdueTick[i] ?? "—"),
                  ];
                }
                if (isVolume) return " Đã đóng: " + item.raw;
                if (isEff) return " Hiệu quả: " + item.raw + "%";
                return " % Overdue: " + item.raw + "%";
              },
              afterBody: (items) => {
                const i = items[0].dataIndex;
                const lines = [
                  "Đã đóng: " + (closed[i] ?? "—"),
                  "Overdue: " + (overdue[i] ?? "—"),
                  "OD chủ quan (VT/hẹn): " + (overdueSubj[i] ?? "—"),
                ];
                if (isVolume) {
                  lines.push("Tại trạm: " + (onsiteCounts[i] ?? "—"));
                  lines.push("Từ xa: " + (remoteCounts[i] ?? "—"));
                }
                return lines;
              },
            },
          },
          datalabels: {
            display: (c) => {
              if (c.dataset.type !== "scatter") return true;
              // Hiện số trên vạch OD và vạch Tại trạm
              return c.dataset.label === "Tại trạm" || c.dataset.label === "Vạch (OD − OD chủ quan)";
            },
            anchor: "end",
            align: "right",
            offset: 6,
            clamp: true,
            clip: false,
            color: "#0f172a",
            font: { size: 12, weight: "700" },
            formatter: (value, ctx) => {
              if (ctx.dataset.type === "scatter") {
                if (ctx.dataset.label === "Tại trạm") {
                  const n = value && value.x != null ? value.x : value;
                  return n != null ? String(n) : "";
                }
                if (ctx.dataset.label === "Vạch (OD − OD chủ quan)") {
                  const i = ctx.dataIndex;
                  const n = overdueTick[i];
                  return n != null ? String(n) : "";
                }
                return "";
              }
              if (value == null || value !== value) return "—";
              if (isVolume) return String(value);
              return value + "%";
            },
          },
        },
        scales: {
          x: {
            beginAtZero: true,
            max: maxX,
            grace: isVolume ? "10%" : undefined,
            ticks: { color: "#64748b", callback: (v) => (isVolume ? v : v + "%") },
            grid: { color: "rgba(148,163,184,.25)" },
          },
          y: {
            ticks: { color: "#0f172a", font: { size: 12, weight: "500" } },
            grid: { display: false },
          },
        },
      },
    });
  }

  function renderKPIs(payload) {
    const totalC = payload.total_closed || 0;
    const totalO = payload.total_overdue || 0;
    const rate = payload.overall_rate_pct != null ? payload.overall_rate_pct : 0;
    setKPIs([
      { label: "Đã đóng", value: totalC.toLocaleString("vi-VN"), sub: "30 ngày (Close Time)" },
      { label: "Overdue", value: totalO.toLocaleString("vi-VN"), sub: "trong ticket đã đóng" },
      { label: "Tỷ lệ OD", value: rate + "%", sub: "toàn bộ filter" },
      { label: "Closed (filter)", value: (payload.counts && payload.counts[Core.cpType]) || totalC, sub: Core.cpType.toUpperCase() },
    ]);
  }


  function makeRegionChip(label, active, idx, onClick) {
    const col = (typeof colorFor === "function")
      ? colorFor(label, idx)
      : { border: ["#0ea5e9","#22c55e","#ef4444","#f97316","#a855f7"][idx % 5] };
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "legend-chip" + (active ? " active" : "");
    chip.textContent = label;
    if (active) {
      chip.style.background = col.border;
      chip.style.borderColor = col.border;
      chip.style.color = "#fff";
    }
    chip.addEventListener("click", onClick);
    return chip;
  }

  function renderRegionTabs() {
    const el = refs.regionTabs;
    if (currentView !== "tech" && currentView !== "resolution") { el.style.display = "none"; return; }
    if (currentView === "resolution") { renderResolutionRegionTabs(); return; }
    el.style.display = "flex";
    el.className = "region-tabs";
    el.innerHTML = "";
    REGION_ORDER.forEach((r, idx) => {
      el.appendChild(makeRegionChip(r, r === selectedRegion, idx, () => {
        selectedRegion = r;
        renderRegionTabs();
        refs.title.textContent = "% Overdue theo KT · " + r;
        if (currentPayload) { renderKPIs(currentPayload); drawChart(); }
      }));
    });
  }



  let resolutionRegion = REGION_ORDER[0] || "LDO-BTH";

  function getResolutionPack() {
    if (!currentPayload) return null;
    const byCp = currentPayload.resolution_boxplot_by_cp || {};
    const cp = Core.cpType || "all";
    const root = byCp[cp] || currentPayload.resolution_boxplot || {};
    // Cấu trúc mới: by_region
    if (root.by_region) return root;
    // tương thích cũ (phẳng) → bọc 1 region
    return root;
  }

  function getResolutionRegionPack() {
    const root = getResolutionPack();
    if (!root) return null;
    if (root.by_region) {
      return root.by_region[resolutionRegion] || { labels: [], boxes: [], total_tickets: 0, axis_max_days: 7 };
    }
    // legacy flat
    return root;
  }

  function renderResolutionRegionTabs() {
    const el = refs.regionTabs;
    if (currentView !== "resolution") return;
    el.style.display = "flex";
    el.className = "region-tabs";
    el.innerHTML = "";
    REGION_ORDER.forEach((r, idx) => {
      el.appendChild(makeRegionChip(r, r === resolutionRegion, idx, () => {
        resolutionRegion = r;
        renderResolutionRegionTabs();
        refs.title.textContent = "Thời gian xử lý · " + resolutionRegion;
        const pack = getResolutionRegionPack();
        renderKPIsResolution(pack);
        drawResolutionBoxplot();
      }));
    });
  }

  let resTipPinned = false;
  let resTipPinnedData = null; // { outlier, tech }

  function ensureResTip() {
    let el = document.getElementById("stats-res-tip");
    if (!el) {
      el = document.createElement("div");
      el.id = "stats-res-tip";
      el.className = "res-tip";
      document.body.appendChild(el);
      el.addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-act]");
        if (!btn || !resTipPinnedData) return;
        const act = btn.getAttribute("data-act");
        if (act === "close") {
          unpinResTip();
          return;
        }
        const o = resTipPinnedData.outlier;
        const tech = resTipPinnedData.tech;
        let text = "";
        if (act === "copy-id") text = o.ticket_id || "";
        else text = formatOutlierPlainGlobal(o, tech);
        if (text && navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(() => {
            btn.textContent = act === "copy-id" ? "Đã copy ID" : "Đã copy";
            setTimeout(() => {
              btn.textContent = act === "copy-id" ? "Copy Ticket ID" : "Copy toàn bộ";
            }, 1200);
          }).catch(() => {
            window.prompt("Copy thủ công:", text);
          });
        } else {
          window.prompt("Copy thủ công:", text);
        }
      });
    }
    return el;
  }

  // formatOutlierPlain dùng trong handler pin (định nghĩa lại nhẹ nếu boxplot chưa tạo)
  function formatOutlierPlainGlobal(o, tech) {
    const days = (o && o.duration_days != null) ? o.duration_days : "—";
    return [
      "Ticket ID: " + ((o && o.ticket_id) || "—"),
      "Kỹ thuật: " + (tech || "—"),
      "Mã trạm: " + ((o && o.station) || "—"),
      "Mã trụ: " + ((o && o.cp_id) || "—"),
      "Thời gian xử lý: " + days + (typeof days === "number" ? " ngày" : ""),
      "Mô tả lỗi: " + (((o && o.problem) || "").trim() || "—"),
    ].join("\n");
  }

  function showResTip(html, clientX, clientY, pinned) {
    if (resTipPinned && !pinned) return; // đang ghim — bỏ qua hover
    const el = ensureResTip();
    el.innerHTML = html;
    el.style.display = "block";
    el.classList.toggle("pinned", !!pinned);
    const pad = 14;
    let left = clientX + pad;
    let top = clientY + pad;
    // đo sau khi render
    const rect = el.getBoundingClientRect();
    if (left + rect.width > window.innerWidth - 8) left = clientX - rect.width - pad;
    if (top + rect.height > window.innerHeight - 8) top = clientY - rect.height - pad;
    el.style.left = Math.max(8, left) + "px";
    el.style.top = Math.max(8, top) + "px";
  }

  function hideResTip() {
    if (resTipPinned) return;
    const el = document.getElementById("stats-res-tip");
    if (el) {
      el.style.display = "none";
      el.classList.remove("pinned");
    }
  }

  function unpinResTip() {
    resTipPinned = false;
    resTipPinnedData = null;
    const el = document.getElementById("stats-res-tip");
    if (el) {
      el.style.display = "none";
      el.classList.remove("pinned");
      el.innerHTML = "";
    }
  }

  function pinResTip(outlier, tech, clientX, clientY) {
    resTipPinned = true;
    resTipPinnedData = { outlier: outlier, tech: tech };
    // tipHtmlOutlier được định nghĩa trong drawResolutionBoxplot — gọi bản pin qua event
    showResTip(
      (window._tipHtmlOutlierPinned
        ? window._tipHtmlOutlierPinned(outlier, tech)
        : ""),
      clientX,
      clientY,
      true
    );
  }

  function fmtDays(v) {
    if (v == null || v !== v) return "—";
    const n = Number(v);
    return (Math.abs(n - Math.round(n)) < 0.05 ? String(Math.round(n)) : n.toFixed(2)) + " ngày";
  }

  function drawResolutionBoxplot() {
    const pack = getResolutionRegionPack();
    const boxes = (pack && pack.boxes) || [];
    const labels = (pack && pack.labels) || [];
    const axisMax = (pack && pack.axis_max_days) || 30;

    if (!boxes.length) {
      if (refs.resolutionWrap) refs.resolutionWrap.style.display = "block";
      if (refs.resolutionNote) {
        refs.resolutionNote.textContent =
          "Không có ticket đóng + duration trong khu vực " + resolutionRegion + ".";
      }
      hideResTip();
      if (chart) { chart.destroy(); chart = null; }
      return;
    }

    refs.loading.style.display = "none";
    refs.chartWrap.style.display = "none";
    if (refs.matrixWrap) refs.matrixWrap.style.display = "none";
    if (refs.quadLegend) refs.quadLegend.style.display = "none";
    if (refs.resolutionWrap) refs.resolutionWrap.style.display = "block";

    const h = Math.max(280, labels.length * 36 + 80);
    if (refs.resolutionCanvas && refs.resolutionCanvas.parentElement) {
      refs.resolutionCanvas.parentElement.style.height = h + "px";
    }

    // hit zones: boxes + outliers
    const boxHits = [];     // {y0,y1, box}
    const outlierHits = []; // {x,y,r, outlier, tech}

    const boxPlugin = {
      id: "techBoxplotH",
      afterDatasetsDraw(ch) {
        const { ctx, chartArea, scales } = ch;
        if (!chartArea) return;
        const xScale = scales.x, yScale = scales.y;
        boxHits.length = 0;
        outlierHits.length = 0;
        const slot = Math.abs(yScale.getPixelForValue(1) - yScale.getPixelForValue(0));
        const half = Math.min(12, slot * 0.32);

        boxes.forEach((b, i) => {
          const y = yScale.getPixelForValue(i);
          const clamp = (v) => Math.min(Math.max(0, Number(v) || 0), axisMax);
          const xWLow = xScale.getPixelForValue(clamp(b.whisker_low));
          const xQ1 = xScale.getPixelForValue(clamp(b.q1));
          const xMed = xScale.getPixelForValue(clamp(b.median));
          const xQ3 = xScale.getPixelForValue(clamp(b.q3));
          const xWHigh = xScale.getPixelForValue(clamp(b.whisker_high));

          let border = "#0ea5e9";
          if (typeof colorFor === "function") {
            const col = colorFor(b.region || resolutionRegion, REGION_ORDER.indexOf(b.region || resolutionRegion));
            border = (col && col.border) || border;
          }

          ctx.save();
          ctx.strokeStyle = border;
          ctx.lineWidth = 1.5;
          ctx.beginPath();
          ctx.moveTo(xWLow, y); ctx.lineTo(xQ1, y);
          ctx.moveTo(xQ3, y); ctx.lineTo(xWHigh, y);
          ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(xWLow, y - half * 0.55); ctx.lineTo(xWLow, y + half * 0.55);
          ctx.moveTo(xWHigh, y - half * 0.55); ctx.lineTo(xWHigh, y + half * 0.55);
          ctx.stroke();
          const left = Math.min(xQ1, xQ3);
          const w = Math.abs(xQ3 - xQ1) || 2;
          ctx.fillStyle = border + "33";
          ctx.fillRect(left, y - half, w, half * 2);
          ctx.strokeRect(left, y - half, w, half * 2);
          ctx.strokeStyle = "#0f172a";
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.moveTo(xMed, y - half);
          ctx.lineTo(xMed, y + half);
          ctx.stroke();

          boxHits.push({ y0: y - half - 4, y1: y + half + 4, box: b });

          (b.outliers || []).forEach((ov) => {
            const days = ov.duration_days != null ? Number(ov.duration_days) : Number(ov);
            const far = !!(ov.far || days > axisMax);
            const xv = far ? axisMax : days;
            const px = xScale.getPixelForValue(xv);
            ctx.beginPath();
            ctx.fillStyle = far ? "#b91c1c" : border;
            ctx.strokeStyle = "#fff";
            ctx.lineWidth = 1.5;
            ctx.arc(px, y, far ? 5.5 : 4, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();
            if (far) {
              ctx.fillStyle = "#b91c1c";
              ctx.font = "700 12px system-ui,sans-serif";
              ctx.textAlign = "left";
              ctx.textBaseline = "middle";
              ctx.fillText("…", px + 8, y);
            }
            outlierHits.push({ x: px, y, r: far ? 9 : 7, outlier: ov, tech: b.tech });
          });
          ctx.restore();
        });
      },
    };

    if (chart) chart.destroy();
    const canvas = refs.resolutionCanvas;
    chart = new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels,
        datasets: [{
          label: "median (ngày)",
          data: boxes.map((b) => Math.min(b.median, axisMax)),
          backgroundColor: "rgba(0,0,0,0)",
          borderWidth: 0,
          barThickness: 1,
        }],
      },
      plugins: [boxPlugin],
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { enabled: false }, // dùng custom tip
        },
        scales: {
          x: {
            min: 0,
            max: axisMax,
            title: {
              display: true,
              text: "Thời gian xử lý (ngày)  ·  >" + axisMax + " ngày → chấm đỏ + …",
              color: "#94a3b8",
              font: { size: 12, weight: "500" },
            },
            ticks: {
              color: "#64748b",
              stepSize: 1,
              autoSkip: false,
              maxRotation: 0,
              callback: (v) => {
                const n = Number(v);
                if (n !== n) return "";
                if (n === axisMax) return axisMax + "+";
                if (n >= 0 && n <= axisMax && Math.abs(n - Math.round(n)) < 1e-6) return String(Math.round(n));
                return "";
              },
            },
            grid: { color: "rgba(148,163,184,.2)" },
          },
          y: {
            ticks: {
              color: "#0f172a",
              font: { size: 11.5, weight: "500" },
            },
            grid: { display: false },
          },
        },
      },
    });

    function tipHtmlBox(b) {
      const under = b.under_2_days != null ? b.under_2_days : "—";
      const over = b.over_2_days != null ? b.over_2_days : "—";
      const pct = b.pct_under_2 != null ? b.pct_under_2 + "%" : "—";
      return (
        '<div class="rt-title">' + (b.tech || "") + '</div>' +
        '<div class="rt-row"><span class="k">Số ticket</span><span class="v">' + b.count + '</span></div>' +
        '<div class="rt-row"><span class="k">≤ 2 ngày</span><span class="v">' + under + ' <span style="color:#94a3b8;font-weight:500">(' + pct + ')</span></span></div>' +
        '<div class="rt-row"><span class="k">> 2 ngày</span><span class="v">' + over + '</span></div>' +
        '<div class="rt-sep"></div>' +
        '<div class="rt-row"><span class="k">Trung bình</span><span class="v">' + fmtDays(b.mean) + '</span></div>' +
        '<div class="rt-row"><span class="k">Median</span><span class="v">' + fmtDays(b.median) + '</span></div>' +
        '<div class="rt-row"><span class="k">Q1 – Q3</span><span class="v">' + fmtDays(b.q1) + ' – ' + fmtDays(b.q3) + '</span></div>' +
        '<div class="rt-row"><span class="k">Whisker</span><span class="v">' + fmtDays(b.whisker_low) + ' – ' + fmtDays(b.whisker_high) + '</span></div>' +
        (b.outliers && b.outliers.length
          ? '<div class="rt-row"><span class="k">Outliers</span><span class="v">' + b.outliers.length + '</span></div>'
          : '')
      );
    }

    function tipHtmlOutlier(o, tech) {
      const far = !!(o.far || (o.duration_days != null && o.duration_days > axisMax));
      const badge = far
        ? '<span class="rt-badge far">ngoài ' + axisMax + ' ngày</span>'
        : '<span class="rt-badge ok">outlier</span>';
      return (
        '<div class="rt-title">Ngoại lệ · ' + (tech || "") + badge + '</div>' +
        '<div class="rt-row"><span class="k">Ticket ID</span><span class="v">' + (o.ticket_id || "—") + '</span></div>' +
        '<div class="rt-row"><span class="k">Mã trạm</span><span class="v">' + (o.station || "—") + '</span></div>' +
        '<div class="rt-row"><span class="k">Mã trụ</span><span class="v">' + (o.cp_id || "—") + '</span></div>' +
        '<div class="rt-sep"></div>' +
        '<div class="rt-row"><span class="k">Thời gian xử lý</span><span class="v">' + fmtDays(o.duration_days) + '</span></div>'
      );
    }

    // Cho pinResTip gọi đúng HTML có nút Copy/Đóng
    window._tipHtmlOutlierPinned = function (o, tech) {
      return tipHtmlOutlier(o, tech, true);
    };

    function hitTestOutlier(evt) {
      const rect = canvas.getBoundingClientRect();
      const mx = evt.clientX - rect.left;
      const my = evt.clientY - rect.top;
      const dpr = chart.currentDevicePixelRatio || window.devicePixelRatio || 1;
      const cx = mx * (canvas.width / rect.width);
      const cy = my * (canvas.height / rect.height);
      for (let i = outlierHits.length - 1; i >= 0; i--) {
        const h = outlierHits[i];
        const dx = cx - h.x, dy = cy - h.y;
        if (dx * dx + dy * dy <= (h.r * dpr) * (h.r * dpr)) return h;
      }
      for (let i = outlierHits.length - 1; i >= 0; i--) {
        const h = outlierHits[i];
        const hx = h.x / dpr, hy = h.y / dpr;
        const dx = mx - hx, dy = my - hy;
        if (dx * dx + dy * dy <= h.r * h.r) return h;
      }
      return null;
    }

    canvas.onmousemove = function (evt) {
      if (resTipPinned) {
        canvas.style.cursor = hitTestOutlier(evt) ? "pointer" : "default";
        return;
      }
      const hitOut = hitTestOutlier(evt);
      if (hitOut) {
        showResTip(tipHtmlOutlier(hitOut.outlier, hitOut.tech, false), evt.clientX, evt.clientY, false);
        canvas.style.cursor = "pointer";
        return;
      }
      const rect = canvas.getBoundingClientRect();
      const mx = evt.clientX - rect.left;
      const my = evt.clientY - rect.top;
      const dpr = chart.currentDevicePixelRatio || window.devicePixelRatio || 1;
      let hitBox = null;
      for (const h of boxHits) {
        const y0 = h.y0 / dpr, y1 = h.y1 / dpr;
        if (my >= y0 && my <= y1) { hitBox = h.box; break; }
      }
      if (hitBox) {
        showResTip(tipHtmlBox(hitBox), evt.clientX, evt.clientY, false);
        canvas.style.cursor = "default";
      } else {
        hideResTip();
        canvas.style.cursor = "default";
      }
    };
    canvas.onclick = function (evt) {
      const hitOut = hitTestOutlier(evt);
      if (hitOut) {
        pinResTip(hitOut.outlier, hitOut.tech, evt.clientX, evt.clientY);
        // refresh pinned HTML (pinResTip gọi window helper)
        showResTip(tipHtmlOutlier(hitOut.outlier, hitOut.tech, true), evt.clientX, evt.clientY, true);
        resTipPinned = true;
        resTipPinnedData = { outlier: hitOut.outlier, tech: hitOut.tech };
        return;
      }
      // click chỗ trống → bỏ ghim
      if (resTipPinned) unpinResTip();
    };
    canvas.onmouseleave = function () {
      if (!resTipPinned) hideResTip();
    };

    if (refs.resolutionNote) {
      refs.resolutionNote.textContent =
        resolutionRegion + " · boxplot ngang · X = ngày (0–" + axisMax +
        ") · n = " + (pack.total_tickets || 0) +
        " ticket · hover box = thống kê (≤2 ngày / >2 ngày / TB…) · hover chấm = chi tiết ticket · click chấm = ghim + copy.";
    }
  }

  function renderKPIsResolution(pack) {
    const boxes = (pack && pack.boxes) || [];
    if (!boxes.length) {
      setKPIs([
        { label: "KT", value: "0", sub: resolutionRegion },
        { label: "Ticket", value: "0", sub: "có duration" },
        { label: "Median TB", value: "—", sub: "ngày" },
        { label: "Chậm nhất", value: "—", sub: "—" },
      ]);
      return;
    }
    const meds = boxes.map((b) => b.median);
    const avgMed = (meds.reduce((a, b) => a + b, 0) / meds.length).toFixed(2);
    const slowest = boxes.reduce((a, b) => (a.median >= b.median ? a : b));
    setKPIs([
      { label: "Kỹ thuật viên", value: boxes.length, sub: resolutionRegion },
      { label: "Ticket", value: (pack.total_tickets || 0).toLocaleString("vi-VN"), sub: "có thời gian xử lý" },
      { label: "Median TB", value: avgMed + " ngày", sub: "TB các median KT" },
      { label: "Chậm nhất", value: slowest.median + " ngày", sub: slowest.tech },
    ]);
  }

  function applyView(view) {
    currentView = view;
    refs.viewToggle.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === view);
    });
    if (view === "region") {
      refs.title.textContent = "Tỷ lệ Overdue theo khu vực";
      refs.desc.innerHTML = "Thanh = % OD · <strong>vạch + số</strong> = số ticket OD thực (đã trừ OD chủ quan VT/hẹn) · 30 ngày";
    } else if (view === "tech") {
      refs.title.textContent = "% Overdue theo KT · " + selectedRegion;
      refs.desc.innerHTML = "Kỹ thuật viên khu vực <strong>" + selectedRegion + "</strong> · vạch + số = OD thực (trừ VT/hẹn) · 30 ngày";
    } else if (view === "top_od") {
      refs.title.textContent = "Top 10 KT — tỷ lệ Overdue cao nhất";
      refs.desc.innerHTML = "Toàn công ty · tối thiểu 3 ticket đóng · vạch + số = OD thực (trừ VT/hẹn) · 30 ngày";
    } else if (view === "top_eff") {
      refs.title.textContent = "Top 10 KT — hiệu quả cao nhất";
      refs.desc.innerHTML = "Hiệu quả = 100% − % Overdue · tối thiểu 3 ticket đóng";
    } else if (view === "top_vol") {
      refs.title.textContent = "Top 10 KT — xử lý nhiều sự cố nhất";
      refs.desc.innerHTML = "Cột = tổng đã đóng · <strong>vạch nâu + số</strong> = ticket <strong>Tại trạm</strong> · 30 ngày";
    } else if (view === "resolution") {
      refs.title.textContent = "Thời gian xử lý · " + resolutionRegion;
      refs.desc.innerHTML = "Boxplot ngang · <strong>Y = KT</strong> · <strong>X = ngày</strong> (0–30, outlier xa hơn = chấm đỏ + …) · Close − Create · closed 30 ngày";
    } else if (view === "matrix") {
      refs.title.textContent = "Ma trận hiệu suất KT (Tại trạm × Overdue)";
      const mx = (currentPayload && currentPayload.performance_matrix) || {};
      refs.desc.innerHTML =
        "Mỗi điểm = 1 KT · kích thước ∝ khối lượng · X = <strong>" + (mx.x_axis_label || "Ticket Tại trạm") +
        "</strong> · Y = % OD · đường đứt = median (vol " + (mx.median_volume ?? "—") +
        " · OD " + (mx.median_rate_pct ?? "—") + "%) · tên chỉ hiện khi OD&gt;30% và volume&gt;300 · tối thiểu " +
        (mx.min_closed || 3) + " ticket đóng";
    }
    renderRegionTabs();
    if (!currentPayload) return;
    renderKPIs(currentPayload);
    if (view === "matrix") {
      refs.chartWrap.style.display = "none";
      if (refs.resolutionWrap) refs.resolutionWrap.style.display = "none";
      drawMatrix();
    } else if (view === "resolution") {
      refs.chartWrap.style.display = "none";
      if (refs.matrixWrap) refs.matrixWrap.style.display = "none";
      if (refs.quadLegend) refs.quadLegend.style.display = "none";
      renderResolutionRegionTabs();
      const pack = getResolutionRegionPack();
      renderKPIsResolution(pack);
      drawResolutionBoxplot();
    } else {
      if (refs.matrixWrap) refs.matrixWrap.style.display = "none";
      if (refs.quadLegend) refs.quadLegend.style.display = "none";
      if (refs.resolutionWrap) refs.resolutionWrap.style.display = "none";
      drawChart();
    }
  }

  function renderSourceNote(payload) {
    Core.setSourceBadge(payload.source === "sample" ? "Dữ liệu mẫu" : "Cache");
    Core.setGeneratedAt(payload.generated_at);
    refs.sourceNote.textContent =
      "Closed 30d · Events: Pending for local team close · " + (payload.generated_at || "");
  }

  async function load() {
    refs.loading.style.display = "block";
    refs.loading.textContent = "⏳ Đang tải tỷ lệ Overdue…";
    try {
      const res = await fetch("/api/stats/overdue-rate");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const payload = await res.json();
      if (payload.error) throw new Error(payload.error);
      fullPayload = payload;
      currentPayload = slicePayload(payload, Core.cpType);
      renderSourceNote(payload);
      applyView(currentView);
    } catch (e) {
      refs.loading.textContent = "Chưa có closed_tickets trong cache — restart để cào lại (cần Events Record).";
      console.warn(e);
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
    if (panelEl.style.display !== "none") applyView(currentView);
  }

  function mount(panel) {
    panelEl = panel;
    panel.innerHTML = `
      <div class="view-toggle" style="margin-bottom:12px;">
        <button type="button" class="active" data-view="region">Theo khu vực</button>
        <button type="button" data-view="tech">Theo kỹ thuật viên</button>
        <button type="button" data-view="top_od">Top 10 Overdue</button>
        <button type="button" data-view="top_eff">Top 10 hiệu quả</button>
        <button type="button" data-view="top_vol">Top 10 khối lượng</button>
        <button type="button" data-view="matrix">Ma trận hiệu suất</button>
        <button type="button" data-view="resolution">Thời gian xử lý</button>
      </div>
      <div class="card-header">
        <div class="header-main">
          <h2 class="chart-title">Tỷ lệ Overdue</h2>
          <div class="desc chart-desc">
            Ticket đã đóng (Events → <strong>Pending for local team close</strong>) · <strong>30 ngày</strong> ·
            màu theo % OD · <strong>vạch + số</strong> = số ticket OD thực (đã trừ OD chủ quan VT/hẹn)
          </div>
        </div>
        <div class="region-tabs header-region-filters" style="display:none;"></div>
      </div>
      <div class="loading chart-loading" style="display:none;">⏳ Đang tải tỷ lệ Overdue…</div>
      <div class="quad-legend" style="display:none;"></div>
      <div class="chart-wrap" style="display:none;"><canvas></canvas></div>
      <div class="matrix-wrap" style="display:none;"><canvas class="matrix-canvas"></canvas></div>
      <div class="resolution-wrap" style="display:none;">
        <div style="position:relative;height:420px;width:100%;"><canvas class="resolution-canvas"></canvas></div>
        <div class="resolution-note" style="font-size:11.5px;color:var(--text-muted);margin-top:8px;"></div>
      </div>
      <div class="source-note"></div>
    `;
    refs = {
      viewToggle: panel.querySelector(".view-toggle"),
      regionTabs: panel.querySelector(".region-tabs"),
      title: panel.querySelector(".chart-title"),
      desc: panel.querySelector(".chart-desc"),
      loading: panel.querySelector(".chart-loading"),
      chartWrap: panel.querySelector(".chart-wrap"),
      canvas: panel.querySelector(".chart-wrap canvas"),
      matrixWrap: panel.querySelector(".matrix-wrap"),
      matrixCanvas: panel.querySelector(".matrix-canvas"),
      resolutionWrap: panel.querySelector(".resolution-wrap"),
      resolutionCanvas: panel.querySelector(".resolution-canvas"),
      resolutionNote: panel.querySelector(".resolution-note"),
      quadLegend: panel.querySelector(".quad-legend"),
      sourceNote: panel.querySelector(".source-note"),
    };
    refs.viewToggle.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-view]");
      if (btn) applyView(btn.dataset.view);
    });
  }

  Core.registerChart({
    key: "overdue",
    label: "Hiệu quả công việc",
    mount,
    onShow,
    onCpTypeChange,
  });
})();