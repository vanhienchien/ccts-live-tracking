/**
 * stats_chart_overdue.js — module biểu đồ "Tỷ lệ Overdue khi đóng".
 * Nguồn dữ liệu: GET /api/stats/overdue-rate.
 * Tự đăng ký vào StatsCore — xem stats_core.js để biết cách thêm module mới.
 */
(function () {
  "use strict";
  const Core = window.StatsCore;
  const { REGION_ORDER, lerp, rgb, setKPIs } = Core;

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
    const showTick = currentView === "region" || currentView === "tech" || currentView === "top_od";

    let values;
    if (isVolume) values = pack.closed_counts || [];
    else if (isEff) values = pack.efficiency_pct || pack.values_pct || [];
    else values = pack.rates_pct || pack.values_pct || [];

    const ticksPct = pack.rates_tick_pct || [];
    const closed = pack.closed_counts || [];
    const overdue = pack.overdue_counts || [];
    const overdueSubj = pack.overdue_subjective_counts || [];
    const overdueTick = pack.overdue_tick_counts || [];
    const labels = pack.labels || [];

    const colors = isVolume
      ? values.map(() => "rgba(56, 189, 248, 0.9)")
      : isEff
        ? values.map((v) => efficiencyToColor(Number(v || 0)))
        : values.map((v) => rateToColor(Number(v || 0)));

    const tickData = showTick ? ticksPct.map((v, i) => ({ x: Number(v) || 0, y: i })) : [];

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
    if (showTick) {
      datasets.push({
        type: "scatter",
        label: "Vạch (OD − OD chủ quan)",
        data: tickData,
        parsing: false,
        pointStyle: "line",
        rotation: 90,
        radius: 12,
        borderWidth: 3,
        borderColor: "#1e293b",
        backgroundColor: "#1e293b",
        order: 1,
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
                return [
                  "Đã đóng: " + (closed[i] ?? "—"),
                  "Overdue: " + (overdue[i] ?? "—"),
                  "OD chủ quan (VT/hẹn): " + (overdueSubj[i] ?? "—"),
                ];
              },
            },
          },
          datalabels: {
            display: (c) => c.dataset.type !== "scatter",
            anchor: "end",
            align: "right",
            offset: 6,
            clamp: true,
            clip: false,
            color: "#0f172a",
            font: { size: 12, weight: "700" },
            formatter: (value) => {
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
        refs.title.textContent = "% Overdue theo KT · " + r;
        if (currentPayload) { renderKPIs(currentPayload); drawChart(); }
      });
      el.appendChild(btn);
    });
  }

  function applyView(view) {
    currentView = view;
    refs.viewToggle.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === view);
    });
    if (view === "region") {
      refs.title.textContent = "Tỷ lệ Overdue theo khu vực";
      refs.desc.innerHTML = "Thanh = % OD · vạch = (OD − OD chủ quan VT/hẹn) / đóng · 30 ngày";
    } else if (view === "tech") {
      refs.title.textContent = "% Overdue theo KT · " + selectedRegion;
      refs.desc.innerHTML = "Kỹ thuật viên khu vực <strong>" + selectedRegion + "</strong> · 30 ngày";
    } else if (view === "top_od") {
      refs.title.textContent = "Top 10 KT — tỷ lệ Overdue cao nhất";
      refs.desc.innerHTML = "Toàn công ty · tối thiểu 3 ticket đã đóng · 30 ngày";
    } else if (view === "top_eff") {
      refs.title.textContent = "Top 10 KT — hiệu quả cao nhất";
      refs.desc.innerHTML = "Hiệu quả = 100% − % Overdue · tối thiểu 3 ticket đóng";
    } else if (view === "top_vol") {
      refs.title.textContent = "Top 10 KT — xử lý nhiều sự cố nhất";
      refs.desc.innerHTML = "Theo số ticket đã đóng trong 30 ngày";
    }
    renderRegionTabs();
    if (currentPayload) { renderKPIs(currentPayload); drawChart(); }
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
        <button type="button" data-view="top_od">Top 10 OD cao</button>
        <button type="button" data-view="top_eff">Top 10 hiệu quả</button>
        <button type="button" data-view="top_vol">Top 10 khối lượng</button>
      </div>
      <div class="region-tabs" style="display:none;"></div>
      <div class="card-header">
        <div>
          <h2 class="chart-title">Tỷ lệ ticket Overdue khi đóng</h2>
          <div class="desc chart-desc">
            Ticket đã đóng (Events → <strong>Pending for local team close</strong>) · <strong>30 ngày</strong> ·
            màu theo % OD (đỏ nhạt→đỏ tươi@10%→đỏ sẫm) · <strong>vạch</strong> = (OD − OD chủ quan VT/hẹn) / đóng
          </div>
        </div>
      </div>
      <div class="loading chart-loading" style="display:none;">⏳ Đang tải tỷ lệ Overdue…</div>
      <div class="chart-wrap" style="display:none;"><canvas></canvas></div>
      <div class="source-note"></div>
    `;
    refs = {
      viewToggle: panel.querySelector(".view-toggle"),
      regionTabs: panel.querySelector(".region-tabs"),
      title: panel.querySelector(".chart-title"),
      desc: panel.querySelector(".chart-desc"),
      loading: panel.querySelector(".chart-loading"),
      chartWrap: panel.querySelector(".chart-wrap"),
      canvas: panel.querySelector("canvas"),
      sourceNote: panel.querySelector(".source-note"),
    };
    refs.viewToggle.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-view]");
      if (btn) applyView(btn.dataset.view);
    });
  }

  Core.registerChart({
    key: "overdue",
    label: "Tỷ lệ Overdue khi đóng",
    mount,
    onShow,
    onCpTypeChange,
  });
})();