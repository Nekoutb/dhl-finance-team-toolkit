/* Finance Team Toolkit — shell interactions: theme, command palette,
   keyboard navigation, sortable tables, floating bulk-select bar. */
(function () {
  "use strict";

  /* ---- Theme -------------------------------------------------------------- */
  const root = document.documentElement;
  const SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  const MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 13A8.5 8.5 0 1 1 11 3a7 7 0 0 0 10 10Z"/></svg>';
  function paintToggle(t) {
    const btn = document.getElementById("themeToggle");
    if (btn) btn.innerHTML = t === "dark" ? SUN : MOON;
  }
  function setTheme(t) {
    // Suspend transitions while the theme vars flip — otherwise every
    // surface cross-fades at its own pace (and some engines leave stale
    // backgrounds behind on var-driven transitions).
    root.classList.add("no-anim");
    root.setAttribute("data-theme", t);
    try { localStorage.setItem("ft-theme", t); } catch (e) { /* private mode */ }
    paintToggle(t);
    setTimeout(function () { root.classList.remove("no-anim"); }, 80);
  }
  window.ftToggleTheme = function () {
    setTheme(root.getAttribute("data-theme") === "dark" ? "light" : "dark");
  };

  /* ---- Mobile navigation (off-canvas sidebar) ------------------------------ */
  function paintNav(open) {
    // Inline transform mirrors the CSS class so the drawer moves even on
    // engines that lag class-driven transitions (see theme-toggle fix).
    var sb = document.querySelector(".sidebar");
    if (sb && window.matchMedia("(max-width: 820px)").matches)
      sb.style.transform = open ? "none" : "";
  }
  window.ftToggleNav = function () {
    paintNav(root.classList.toggle("nav-open"));
  };
  window.ftCloseNav = function () {
    root.classList.remove("nav-open");
    paintNav(false);
  };
  document.addEventListener("click", function (e) {
    // Following a nav link closes the drawer so the page behind is visible.
    if (e.target.closest(".sidebar .nav-item")) window.ftCloseNav();
  });
  document.addEventListener("DOMContentLoaded", function () {
    paintToggle(root.getAttribute("data-theme"));
  });

  /* ---- Command palette + go-to shortcuts ----------------------------------- */
  const DESTS = [
    { ico: "🏠", label: "Dashboard", href: "/", keys: "g d" },
    { ico: "🧭", label: "Receivables — CtP monitoring", href: "/tools/ongoing-ctp-monitoring", keys: "g c" },
    { ico: "🔗", label: "Remittance & allocation portal", href: "/tools/remittance-portal", keys: "g r" },
    { ico: "🏦", label: "Bank statements — open AR appearing on the bank statements", href: "/tools/bank-statements", keys: "g b" },
    { ico: "📱", label: "Mobile Money (Orange & MTN) → PDF", href: "/tools/momo", keys: "g m" },
    { ico: "🧮", label: "Vendor invoice allocation (MTN / Orange / ENEO)", href: "/tools/vendor-invoice-allocation", keys: "g i" },
    { ico: "📄", label: "Orange Cameroun → PDF", href: "/tools/orange-cameroun", keys: "g o" },
    { ico: "🛡️", label: "Vendor NIU verification", href: "/tools/vendor-niu", keys: "g v" },
    { ico: "📊", label: "Variance analysis — CY vs PY", href: "/tools/variance-analysis", keys: "g a" },
    { ico: "⚖️", label: "Vendor invoice compliance engine", href: "/tools/invoice-compliance", keys: "g e" },
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
    if (e.key === "Escape") window.ftCloseNav();
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

  /* ---- Downloads open in a new tab (don't interrupt the working page) ------- */
  // Any link to a generated document (Excel exports, PDF receipts, invoices,
  // certificates, reconciliations) opens in a separate tab so the current
  // page — and any unsaved work on it — is never navigated away from.
  var DL_RE = /\/(download|export|invoice|certificate)(\/|$)|\/[0-9a-f]+\/pdf\//i;
  function markDownloads(scope) {
    (scope || document).querySelectorAll('a[href]').forEach(function (a) {
      var href = a.getAttribute("href") || "";
      if (DL_RE.test(href) && !a.target) {
        a.target = "_blank";
        a.rel = "noopener";
      }
    });
  }
  document.addEventListener("DOMContentLoaded", function () { markDownloads(); });

  /* ---- Busy state on every form submit (double-click guard) ----------------- */
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;          // a validator cancelled it
    const form = e.target;
    const btn = e.submitter ||
      form.querySelector('button[type="submit"], input[type="submit"]');
    if (!btn || btn.disabled) return;
    // Defer so the button's own name/value still serialize into the POST.
    setTimeout(function () {
      btn.disabled = true;
      if (btn.classList.contains("btn-primary") && !btn.dataset.busyKeep) {
        btn.dataset.orig = btn.textContent;
        btn.textContent = "⏳ Working…";
      }
    }, 0);
  });
  // Back/forward cache restores the page with buttons still disabled — undo.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("button[disabled][data-orig]").forEach(function (b) {
      b.disabled = false;
      b.textContent = b.dataset.orig;
      delete b.dataset.orig;
    });
    document.querySelectorAll('button[type="submit"][disabled]').forEach(function (b) {
      b.disabled = false;
    });
  });

  /* ---- Sortable data tables ------------------------------------------------- */
  function cellValue(row, idx) {
    const cell = row.children[idx];
    if (!cell) return "";
    const t = cell.textContent.trim();
    const n = parseFloat(t.replace(/[\s,%]/g, "").replace(/[—–-]$/, ""));
    return isNaN(n) || !/[\d]/.test(t) ? t.toLowerCase() : n;
  }
  function rowComparator(idx, asc) {
    return (a, b) => {
      const va = cellValue(a, idx), vb = cellValue(b, idx);
      if (typeof va === "number" && typeof vb === "number")
        return asc ? va - vb : vb - va;
      return asc ? String(va).localeCompare(String(vb))
                 : String(vb).localeCompare(String(va));
    };
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
    const cap = table._ftCap;
    if (cap) {                       // capped table: sort the FULL row set
      cap.rows.sort(rowComparator(idx, asc));
      cap.render();
      return;
    }
    Array.from(tbody.rows)
      .sort(rowComparator(idx, asc))
      .forEach(r => tbody.appendChild(r));
  });

  /* ---- Large-table cap: filter + "show more" so 10k-row files stay snappy --- */
  const CAP_STEP = 200;
  function enhanceTable(table) {
    const tbody = table.tBodies[0];
    if (!tbody || tbody.rows.length <= CAP_STEP || table._ftCap) return;
    const inForm = !!table.closest("form");   // form rows must stay in the DOM
    const cap = table._ftCap = {
      rows: Array.from(tbody.rows),
      shown: CAP_STEP,
      query: "",
      render: null,
    };
    const wrap = table.closest(".table-wrap") || table;
    const foot = document.createElement("div");
    foot.className = "table-foot";
    foot.innerHTML =
      '<input type="text" class="table-filter" placeholder="Filter rows…">' +
      '<span class="table-count"></span>' +
      '<button type="button" class="btn btn-small tf-more">Show ' + CAP_STEP + " more</button>" +
      '<button type="button" class="btn btn-small tf-all">Show all</button>';
    wrap.insertAdjacentElement("afterend", foot);
    const countEl = foot.querySelector(".table-count");

    cap.render = function () {
      const q = cap.query;
      const match = q
        ? cap.rows.filter(r => r.textContent.toLowerCase().indexOf(q) !== -1)
        : cap.rows;
      const visible = match.slice(0, cap.shown);
      if (inForm) {
        const show = new Set(visible);
        cap.rows.forEach(r => { r.style.display = show.has(r) ? "" : "none"; });
        // keep DOM order in sync with sorted order
        cap.rows.forEach(r => tbody.appendChild(r));
      } else {
        tbody.replaceChildren(...visible);
      }
      countEl.textContent = "Showing " + visible.length.toLocaleString() +
        " of " + match.length.toLocaleString() +
        (q ? " matching" : "") + " rows";
      foot.querySelector(".tf-more").style.display =
        visible.length < match.length ? "" : "none";
      foot.querySelector(".tf-all").style.display =
        visible.length < match.length ? "" : "none";
    };
    foot.querySelector(".tf-more").addEventListener("click", function () {
      cap.shown += CAP_STEP; cap.render();
    });
    foot.querySelector(".tf-all").addEventListener("click", function () {
      cap.shown = Infinity; cap.render();
    });
    let t = null;
    foot.querySelector(".table-filter").addEventListener("input", function (e) {
      clearTimeout(t);
      t = setTimeout(function () {
        cap.query = e.target.value.trim().toLowerCase();
        cap.shown = CAP_STEP;
        cap.render();
      }, 150);
    });
    cap.render();
  }
  window.ftEnhanceTables = function (root_) {
    (root_ || document).querySelectorAll(".tx-table").forEach(enhanceTable);
  };
  document.addEventListener("DOMContentLoaded", function () {
    window.ftEnhanceTables();
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
