/* Smart Inventory Intelligence System — shared UI behaviour */
(function () {
  "use strict";

  // ---- theme toggle (resolved theme is applied early in <head>, see base.html) ----
  document.addEventListener("click", function (e) {
    const btn = e.target.closest("[data-theme-toggle]");
    if (!btn) return;
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    try { localStorage.setItem("sis-theme", next); } catch (err) { /* storage unavailable */ }
    document.documentElement.setAttribute("data-theme", next);
    document.documentElement.setAttribute("data-bs-theme", next);
    document.dispatchEvent(new CustomEvent("sis:themechange"));
  });

  // ---- mobile sidebar ----
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-sidebar-toggle]")) {
      document.querySelector(".sidebar").classList.toggle("open");
    } else if (!e.target.closest(".sidebar")) {
      const sb = document.querySelector(".sidebar.open");
      if (sb) sb.classList.remove("open");
    }
  });

  // ---- confirm dangerous actions ----
  document.addEventListener("submit", function (e) {
    const msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // ---- select-all checkboxes ----
  document.addEventListener("change", function (e) {
    if (!e.target.matches("[data-select-all]")) return;
    document.querySelectorAll(e.target.getAttribute("data-select-all")).forEach(function (cb) {
      cb.checked = e.target.checked;
    });
  });

  // ---- formatting helpers ----
  window.SIS = window.SIS || {};
  SIS.fmtNumber = function (v, digits) {
    return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits === undefined ? 0 : digits });
  };
  SIS.fmtMoney = function (v) {
    const cur = document.body.getAttribute("data-currency") || "RWF";
    return cur + " " + SIS.fmtNumber(v, 0);
  };
})();
