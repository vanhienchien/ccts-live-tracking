/**
 * stats_core.js — "khung" dùng chung cho trang /stats.
 *
 * Chịu trách nhiệm:
 *  - Đăng ký các loại biểu đồ (chart module) dưới dạng tab + panel, tự tạo
 *    nút tab trong #chart-type-tabs và khung <div class="card"> trong
 *    #panels-mount — module KHÔNG cần tự viết HTML cho tab/panel khung.
 *  - Quản lý ô KPI dùng chung (4 ô trong #kpi-row) qua StatsCore.setKPIs().
 *  - Quản lý bộ lọc EV/BSS/Tổng dùng chung (#cp-filter) — module nào cần
 *    biết khi người dùng đổi bộ lọc thì đăng ký qua onCpTypeChange callback
 *    trong lúc registerChart().
 *  - Cung cấp hằng số & tiện ích dùng chung: màu khu vực, bảng màu kỹ thuật
 *    viên, định dạng ngày, escape HTML, heatmap màu theo cường độ, nội suy
 *    màu (lerp/rgb).
 *
 * ĐỂ THÊM 1 LOẠI BIỂU ĐỒ MỚI (vd "Thống kê phụ tùng"):
 *   1. Viết 1 module Python xử lý dữ liệu (như stats_charts_volume.py) +
 *      1 API endpoint trả JSON.
 *   2. Viết 1 file JS mới (như stats_chart_volume.js) tự IIFE gọi
 *      `StatsCore.registerChart({ key, label, mount, onShow, onCpTypeChange })`.
 *   3. Thêm 1 dòng <script src="..."> vào stats.html, SAU stats_core.js.
 *   KHÔNG cần sửa gì trong stats_core.js hay HTML khung của trang.
 */
(function (global) {
  "use strict";

  // ================== Hằng số dùng chung ==================
  const REGION_ORDER = ["DNA-QNA", "LDO-BTH", "Mtay", "DNI-BPH", "EC"];
  const REGION_COLORS = {
    "DNA-QNA": { border: "#3b82f6", bg: "rgba(59,130,246,.12)" },
    "DNI-BPH": { border: "#10b981", bg: "rgba(16,185,129,.12)" },
    "EC": { border: "#f59e0b", bg: "rgba(245,158,11,.12)" },
    "LDO-BTH": { border: "#ef4444", bg: "rgba(239,68,68,.12)" },
    "Mtay": { border: "#8b5cf6", bg: "rgba(139,92,246,.12)" },
  };
  const TECH_PALETTE = [
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#0ea5e9",
    "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16", "#a855f7",
  ];

  // ================== Tiện ích dùng chung ==================
  function colorFor(name, idx) {
    if (REGION_COLORS[name]) return REGION_COLORS[name];
    const c = TECH_PALETTE[idx % TECH_PALETTE.length];
    return { border: c, bg: c + "22" };
  }

  function formatDateLabel(iso) {
    const p = String(iso).split("-");
    return p.length === 3 ? (p[2] + "/" + p[1]) : iso;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function lerp(a, b, t) { return Math.round(a + (b - a) * t); }
  function rgb(r, g, b) { return "rgb(" + r + "," + g + "," + b + ")"; }

  /** Màu ô heatmap theo cường độ v/max (thang hổ phách nhạt → đỏ đậm). */
  function heatColor(v, max) {
    if (!v) return { bg: "#f1f5f9", fg: "#cbd5e1" };
    const t = Math.pow(Math.min(1, v / (max || 1)), 0.65);
    const r = lerp(254, 153, t);
    const g = lerp(243, 27, t);
    const b = lerp(199, 30, t);
    const fg = t > 0.55 ? "#fff" : "#78350f";
    return { bg: rgb(r, g, b), fg };
  }

  /** Chèn 1 khối <style> vào <head>, chỉ 1 lần cho mỗi id (dùng để mỗi
   * module JS tự mang theo CSS riêng của nó mà không cần sửa stats.html). */
  function injectStyleOnce(id, css) {
    if (document.getElementById(id)) return;
    const tag = document.createElement("style");
    tag.id = id;
    tag.textContent = css;
    document.head.appendChild(tag);
  }

  // ================== KPI row dùng chung (4 ô) ==================
  const kpiRow = document.getElementById("kpi-row");

  /** items: mảng tối đa 4 phần tử {label, value, sub} — set vào 4 ô KPI
   * theo thứ tự. Mỗi module tự quyết định 4 ô đó hiển thị gì. */
  function setKPIs(items) {
    if (!kpiRow) return;
    const boxes = kpiRow.querySelectorAll(".kpi");
    (items || []).slice(0, boxes.length).forEach((it, i) => {
      const box = boxes[i];
      if (!box || !it) return;
      const label = box.querySelector(".label");
      const value = box.querySelector(".value");
      const sub = box.querySelector(".sub");
      if (label) label.textContent = it.label ?? "";
      if (value) value.textContent = it.value ?? "—";
      if (sub) sub.textContent = it.sub ?? "";
    });
  }

  // ================== Top bar dùng chung ==================
  function setSourceBadge(text) {
    const el = document.getElementById("source-badge");
    if (el) el.textContent = text;
  }

  function setGeneratedAt(iso) {
    const el = document.getElementById("gen-at");
    if (!el) return;
    el.textContent = iso ? "Cập nhật: " + String(iso).replace("T", " ").slice(0, 19) : "";
  }

  // ================== Bộ lọc EV / BSS / Tổng dùng chung ==================
  let cpType = "all";
  const cpListeners = [];

  function onCpTypeChange(fn) {
    if (typeof fn === "function") cpListeners.push(fn);
  }

  const cpFilterEl = document.getElementById("cp-filter");
  if (cpFilterEl) {
    cpFilterEl.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-cp]");
      if (!btn) return;
      cpType = btn.dataset.cp;
      cpFilterEl.querySelectorAll("button").forEach((b) => {
        b.classList.toggle("active", b.dataset.cp === cpType);
      });
      cpListeners.forEach((fn) => {
        try { fn(cpType); } catch (err) { console.error("[stats] onCpTypeChange lỗi:", err); }
      });
    });
  }

  // ================== Đăng ký tab + panel biểu đồ ==================
  const tabsEl = document.getElementById("chart-type-tabs");
  const mountEl = document.getElementById("panels-mount");
  const modules = [];
  let activeKey = null;

  function renderActiveTabs() {
    modules.forEach((m) => {
      const isActive = m.key === activeKey;
      m.panel.style.display = isActive ? "" : "none";
      m.btn.classList.toggle("active", isActive);
    });
  }

  function showChart(key) {
    if (key === activeKey) return;
    if (!modules.some((m) => m.key === key)) return;
    activeKey = key;
    renderActiveTabs();
    const m = modules.find((x) => x.key === key);
    if (m && typeof m.onShow === "function") m.onShow();
  }

  /**
   * mod: {
   *   key: string (duy nhất),
   *   label: string (chữ trên tab),
   *   mount(panelEl): dựng HTML/nội bộ panel, gọi 1 lần lúc đăng ký,
   *   onShow(): gọi mỗi khi tab này được chọn hiển thị (kể cả lần đầu),
   *   onCpTypeChange(cp): (tuỳ chọn) gọi khi người dùng đổi bộ lọc EV/BSS,
   * }
   */
  function registerChart(mod) {
    if (!mod || !mod.key) throw new Error("registerChart cần { key, label, mount, onShow }");

    const panel = document.createElement("div");
    panel.className = "card";
    panel.id = "panel-" + mod.key;
    panel.style.display = "none";
    mountEl.appendChild(panel);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.chart = mod.key;
    btn.textContent = mod.label || mod.key;
    btn.addEventListener("click", () => showChart(mod.key));
    tabsEl.appendChild(btn);

    modules.push({ key: mod.key, panel, btn, onShow: mod.onShow });

    if (typeof mod.mount === "function") mod.mount(panel);
    if (typeof mod.onCpTypeChange === "function") onCpTypeChange(mod.onCpTypeChange);

    const isFirst = activeKey === null;
    if (isFirst) activeKey = mod.key;
    renderActiveTabs();
    if (isFirst && typeof mod.onShow === "function") mod.onShow();
  }

  global.StatsCore = {
    REGION_ORDER,
    REGION_COLORS,
    TECH_PALETTE,
    colorFor,
    formatDateLabel,
    escapeHtml,
    lerp,
    rgb,
    heatColor,
    injectStyleOnce,
    setKPIs,
    setSourceBadge,
    setGeneratedAt,
    onCpTypeChange,
    get cpType() { return cpType; },
    registerChart,
    showChart,
  };
})(window);