/* Finance Team Toolkit — shell interactions: theme, command palette,
   keyboard navigation, sortable tables, floating bulk-select bar. */
(function () {
  "use strict";

  /* ---- Theme -------------------------------------------------------------- */
  const root = document.documentElement;
  function setTheme(t) {
    root.setAttribute("data-theme", t);
    try { localStorage.setItem("ft-theme", t); } catch (e) { /* private mode */ }
    const btn = document.getElementById("themeToggle");
    if (btn) btn.textContent = t === "dark" ? "☀" : "☾";
  }
  window.ftToggleTheme = function () {
    setTheme(root.getAttribute("data-theme") === "dark" ? "light" : "dark");
  };
  document.addEventListener("DOMContentLoaded", function () {
    const btn = document.getElementById("themeToggle");
    if (btn) btn.textContent =
      root.getAttribute("data-theme") === "dark" ? "☀" : "☾";
  });

  /* ---- Command palette + go-to shortcuts ----------------------------------- */
  const DESTS = [
    { ico: "🏠", label: "Dashboard", href: "/", keys: "g d" },
    { ico: "🧭", label: "Receivables — CtP monitoring", href: "/tools/ongoing-ctp-monitoring", keys: "g c" },
    { ico: "🔗", label: "Remittance & allocation portal", href: "/tools/remittance-portal", keys: "g r" },
    { ico: "🏦", label: "Bank statements — collection activity", href: "/tools/bank-statements", keys: "g b" },
    { ico: "📱", label: "Mobile Money (Orange & MTN) → PDF", href: "/tools/momo", keys: "g m" },
    { ico: "🧮", label: "Vendor invoice allocation (MTN / Orange / ENEO)", href: "/tools/vendor-invoice-allocation", keys: "g i" },
    { ico: "📄", label: "Orange Cameroun → PDF", href: "/tools/orange-cameroun", keys: "g o" },
    { ico: "🛡️", label: "Vendor NIU verification", href: "/tools/vendor-niu", keys: "g v" },
    { ico: "🗂", label: "Credit-hold register", href: "/tools/ongoing-ctp-monitoring/holds", keys: "" },
    { ico: "👥", label: "Remittance customer contacts", href: "/tools/remittance-portal/contacts", keys: "" },
    { ico: "⚙️", label: "Remittance settings", href: "/tools/remittance-portal/settings", keys: "" },
    { ico: "✉️", label: "Email (SMTP) settings", href: "/settings", keys: "g s" },
    { ico: "🌓", label: "Toggle dark / light mode", action: "theme", keys: "t" },
  ];
  let overlay = null, input = null, list = null, sel = 0, filtered = DESTS;

  function buildPalette() {
    overlay = document.createElement("div");
    overlay.className = "palette-overlay";
    overlay.innerHTML =
      '<div class="palette">' +
      '<input type="text" placeholder="Jump to… (type to filter)" />' +
      '<div class="palette-list"></div>' +
      '<div class="palette-hint"><span><kbd>↑↓</kbd> navigate</span>' +
      '<span><kbd>Enter</kbd> open</span><span><kbd>Esc</kbd> close</span></div>' +
      "</div>";
    document.body.appendChild(overlay);
    input = overlay.querySelector("input");
    list = overlay.querySelector(".palette-list");
    overlay.addEventListener("mousedown", function (e) {
      if (e.target === overlay) closePalette();
    });
    input.addEventListener("input", function () { sel = 0; render(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { sel = Math.min(sel + 1, filtered.length - 1); render(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); render(); e.preventDefault(); }
      else if (e.key === "Enter") { go(filtered[sel]); e.preventDefault(); }
      else if (e.key === "Escape") closePalette();
    });
    render();
  }
  function render() {
    const q = (input.value || "").toLowerCase();
    filtered = DESTS.filter(d => d.label.toLowerCase().indexOf(q) !== -1);
    list.innerHTML = filtered.map((d, i) =>
      '<div class="palette-item' + (i === sel ? " sel" : "") + '" data-i="' + i + '">' +
      '<span class="pi-ico">' + d.ico + "</span><span>" + d.label + "</span>" +
      (d.keys ? "<kbd>" + d.keys + "</kbd>" : "") + "</div>").join("") ||
      '<div class="palette-item">No match</div>';
    list.querySelectorAll(".palette-item[data-i]").forEach(el => {
      el.addEventListener("click", () => go(filtered[+el.dataset.i]));
    });
  }
  function go(d) {
    if (!d) return;
    closePalette();
    if (d.action === "theme") window.ftToggleTheme();
    else window.location.href = d.href;
  }
  function openPalette() {
    if (!overlay) buildPalette();
    sel = 0; overlay.classList.add("open");
    input.value = ""; render(); input.focus();
  }
  function closePalette() { if (overlay) overlay.classList.remove("open"); }
  window.ftOpenPalette = openPalette;

  let gPending = false, gTimer = null;
  document.addEventListener("keydown", function (e) {
    const tag = (e.target.tagName || "").toLowerCase();
    const typing = tag === "input" || tag === "textarea" || tag === "select";
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault(); openPalette(); return;
    }
    if (typing) return;
    if (e.key === "/") { e.preventDefault(); openPalette(); return; }
    if (e.key.toLowerCase() === "t" && !gPending) { window.ftToggleTheme(); return; }
    if (e.key.toLowerCase() === "g") {
      gPending = true;
      clearTimeout(gTimer);
      gTimer = setTimeout(() => { gPending = false; }, 900);
      return;
    }
    if (gPending) {
      const dest = DESTS.find(d => d.keys === "g " + e.key.toLowerCase());
      gPending = false;
      if (dest) window.location.href = dest.href;
    }
  });

  /* ---- Sortable data tables ------------------------------------------------- */
  function cellValue(row, idx) {
    const cell = row.children[idx];
    if (!cell) return "";
    const t = cell.textContent.trim();
    const n = parseFloat(t.replace(/[\s,%]/g, "").replace(/[—–-]$/, ""));
    return isNaN(n) || !/[\d]/.test(t) ? t.toLowerCase() : n;
  }
  document.addEventListener("click", function (e) {
    const th = e.target.closest(".tx-table thead th");
    if (!th || e.target.closest("input,button,a,form")) return;
    const table = th.closest("table");
    const tbody = table.tBodies[0];
    if (!tbody || tbody.rows.length < 2) return;
    const idx = Array.prototype.indexOf.call(th.parentNode.children, th);
    const asc = !th.classList.contains("sort-asc");
    table.querySelectorAll("thead th").forEach(h =>
      h.classList.remove("sort-asc", "sort-desc"));
    th.classList.add(asc ? "sort-asc" : "sort-desc");
    Array.from(tbody.rows)
      .sort((a, b) => {
        const va = cellValue(a, idx), vb = cellValue(b, idx);
        if (typeof va === "number" && typeof vb === "number")
          return asc ? va - vb : vb - va;
        return asc ? String(va).localeCompare(String(vb))
                   : String(vb).localeCompare(String(va));
      })
      .forEach(r => tbody.appendChild(r));
  });

  /* ---- Floating bulk-select bar --------------------------------------------- */
  document.addEventListener("DOMContentLoaded", function () {
    const picks = document.querySelectorAll("input.rowpick");
    if (!picks.length) return;
    const bar = document.createElement("div");
    bar.className = "bulkbar";
    bar.innerHTML = '<span><strong id="bbCount">0</strong> selected</span>' +
      '<button type="button" class="btn btn-small" id="bbClear">Clear</button>' +
      '<button type="button" class="btn btn-primary btn-small" id="bbGo">Continue ↵</button>';
    document.body.appendChild(bar);
    const update = () => {
      const n = Array.from(picks).filter(p => p.checked).length;
      bar.querySelector("#bbCount").textContent = n;
      bar.classList.toggle("show", n > 0);
    };
    document.addEventListener("change", e => {
      if (e.target.classList && e.target.classList.contains("rowpick")) update();
      if (e.target.id === "pickall") update();
    });
    bar.querySelector("#bbClear").addEventListener("click", () => {
      picks.forEach(p => { p.checked = false; });
      const all = document.getElementById("pickall");
      if (all) all.checked = false;
      update();
    });
    bar.querySelector("#bbGo").addEventListener("click", () => {
      const form = document.getElementById("genform");
      if (form) form.requestSubmit ? form.requestSubmit() : form.submit();
    });
    update();
  });
})();
