/* STP-UI runtime: turns a resolved screen definition into a working screen.
 *
 * The runtime knows nothing about any one screen. Everything it draws comes from
 * the definition (conditions, grids, columns with labels from DB comments) and
 * everything it shows comes from read transactions (POST /api/tx/{id}).
 * Keys: Enter in a condition = 조회, ↑/↓ moves the row, Enter or double-click on
 * a row = move to the linked screen, Ctrl+Enter anywhere = 조회.
 */
(function () {
  "use strict";
  const DATA = JSON.parse(document.getElementById("stpui-data").textContent);
  const app = document.getElementById("app");
  const NUMERIC = new Set(["number", "money", "rate"]);
  const nf = new Intl.NumberFormat("ko-KR");
  const rf = new Intl.NumberFormat("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const dtf = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const STATUS = {
    MATCH: "ok", ACCEPTED: "ok", FILLED: "ok", true: "ok",
    BREAK: "bad", REJECTED: "bad", false: "bad",
    RESOLVED: "warn", UNKNOWN: "warn", CANCELED: "warn", PENDING: "warn",
  };

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "text") el.textContent = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined) el.append(kid);
    return el;
  }
  const svg = (tag, attrs) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
  };

  // ---------- motion ----------
  // Motion only where it tells something (what changed, where a row went, which screen you came to).
  // Short (≤ 0.3 s), one easing curve, never in the way of the keyboard, off with "reduce motion".
  const REDUCED = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: true };
  const EASE = "cubic-bezier(.2, .8, .2, 1)";
  const still = () => REDUCED.matches;
  const anim = (el, frames, ms) => (still() || !el.animate) ? null : el.animate(frames, { duration: ms, easing: EASE });

  // Replace a box's content: its height glides to the new size and the new content fades up.
  function morph(box, render) {
    if (still() || !box.isConnected) return render();
    const h0 = box.offsetHeight;
    render();
    const h1 = box.offsetHeight;
    if (h0 && Math.abs(h0 - h1) > 2) {
      const prev = box.style.overflow;
      box.style.overflow = "hidden";
      const a = anim(box, [{ height: h0 + "px" }, { height: h1 + "px" }], 240);
      if (a) a.onfinish = a.oncancel = () => { box.style.overflow = prev; };
      else box.style.overflow = prev;
    }
    for (const k of box.children) {
      if (!k.classList.contains("selbar")) anim(k, [{ opacity: 0, transform: "translateY(4px)" }, { opacity: 1, transform: "none" }], 200);
    }
  }

  // FLIP: rows keep their identity (data-k) and slide from where they were to where they are.
  function flip(box, mutate) {
    if (still()) return mutate();
    const before = new Map([...box.querySelectorAll("tbody tr[data-k]")].map(tr => [tr.dataset.k, tr.getBoundingClientRect().top]));
    mutate();
    const rows = [...box.querySelectorAll("tbody tr[data-k]")];
    if (rows.length > 400) return;
    for (const tr of rows) {
      const t0 = before.get(tr.dataset.k);
      if (t0 === undefined) continue;
      const d = t0 - tr.getBoundingClientRect().top;
      if (Math.abs(d) <= 1) continue;
      if (Math.abs(d) > box.clientHeight) {   // from far off screen: arrive gently instead of streaking past
        anim(tr, [{ opacity: 0, transform: `translateY(${Math.sign(d) * 12}px)` }, { opacity: 1, transform: "none" }], 240);
      } else {
        anim(tr, [{ transform: `translateY(${d}px)` }, { transform: "none" }], 280);
      }
    }
  }

  // A number that changed counts to its new value instead of jumping.
  function roll(el, from, to, format) {
    if (still() || typeof from !== "number" || typeof to !== "number" || from === to) return;
    const t0 = performance.now(), dur = 450, final = el.textContent;
    const step = now => {
      const p = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      const v = from + (to - from) * e;
      el.textContent = p < 1 ? fmt(format === "rate" ? v : Math.round(v), format) : final;
      if (p < 1) requestAnimationFrame(step);
    };
    el.textContent = fmt(from, format);
    requestAnimationFrame(step);
  }

  // ---------- formatting ----------
  function fmt(v, format) {
    if (v === null || v === undefined || v === "") return "";
    switch (format) {
      case "number": case "money": return nf.format(v);
      case "rate": return rf.format(v);
      case "date": return String(v).slice(0, 10);
      case "datetime": { const d = new Date(v); return isNaN(d) ? String(v) : dtf.format(d); }
      case "json": return typeof v === "string" ? v : JSON.stringify(v);
      default: return String(v);
    }
  }
  function tone(v, rule, row) {
    if (rule === "verdict") return row && row.ok === true ? "st-ok" : row && row.ok === false ? "st-bad" : "";
    if (v === null || v === undefined) return "";
    if (rule === "sign") return v > 0 ? "up" : v < 0 ? "down" : "";
    if (rule === "side") return v === "BUY" ? "up" : v === "SELL" ? "down" : "";
    if (rule === "status") return STATUS[v] ? "st-" + STATUS[v] : "";
    return "";
  }

  // ---------- shell ----------
  function topbar(screen) {
    const nav = h("nav", { class: "menu", "aria-label": "화면 목록" },
      DATA.menu.map(m => h("a", {
        href: "/screen/" + m.id, class: screen && m.id === screen.id ? "on" : null,
        "aria-current": screen && m.id === screen.id ? "page" : null,
      }, h("span", { class: "sid", text: m.id }), " " + m.title)));
    return h("header", { class: "top" },
      h("a", { class: "brand", href: "/" }, "MiniSTP ", h("b", { text: "STP-UI" })),
      nav,
      h("span", { class: "env", title: `DB ${DATA.db} · 읽기 전용 연결` },
        h("span", { class: "dot", id: "live-dot" }), `${DATA.label || DATA.db} · 기준일 ${DATA.today} · 조회 전용`));
  }
  // Keep the current screen's tab in view when the menu is wider than the bar.
  function revealTab() {
    const nav = document.querySelector(".menu"), on = nav && nav.querySelector(".on");
    if (on && (on.offsetLeft + on.offsetWidth > nav.scrollLeft + nav.clientWidth || on.offsetLeft < nav.scrollLeft)) {
      nav.scrollLeft = on.offsetLeft - (nav.clientWidth - on.offsetWidth) / 2;
    }
  }

  function home() {
    app.append(topbar(null), h("main", { class: "home" },
      h("h1", { text: "업무 화면" }),
      h("p", { class: "lead", text: "화면은 정의 파일(YAML)에서 만들어지고, 열 이름은 DB 주석에서 가져와요. 서버는 시작할 때 모든 화면 정의를 DB 스키마와 대조해요." }),
      h("div", { class: "tiles" }, DATA.menu.map(m => h("a", { class: "tile", href: "/screen/" + m.id },
        h("span", { class: "sid", text: m.id }), h("span", { class: "tt", text: m.title }))))));
  }

  // ---------- screen ----------
  function Screen(def) {
    const grids = {};
    const condEls = {};
    const status = h("div", { class: "status", role: "status", "aria-live": "polite" });
    const urlParams = new URLSearchParams(location.search);

    function condValues() {
      const out = {};
      for (const c of def.conditions) {
        const el = condEls[c.name];
        if (c.type === "bool") { if (el.checked) out[c.name] = true; }
        else if (el.value !== "") out[c.name] = el.value;
      }
      return out;
    }

    function condField(c) {
      const id = "c-" + c.name;
      let def0 = urlParams.get(c.name) ?? (c.default === "today" ? DATA.today : c.default);
      let el;
      if (c.type === "select") {
        el = h("select", { id }, c.required ? null : h("option", { value: "", text: "전체" }),
          c.options.map(o => h("option", { value: o, text: o, selected: o === def0 })));
      } else if (c.type === "bool") {
        el = h("input", { id, type: "checkbox", checked: def0 === "true" || def0 === true });
      } else if (c.type === "number") {
        el = h("input", { id, type: "number", min: "0", step: "1", inputmode: "numeric", value: def0 || "",
          autocomplete: "off" });
      } else {
        el = h("input", { id, type: c.type === "date" ? "date" : "text", value: def0 || "",
          autocomplete: "off", spellcheck: "false", size: c.type === "date" ? null : 10 });
      }
      el.addEventListener(c.type === "bool" || c.type === "select" || c.type === "date" ? "change" : "input", recalc);
      el.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); search(); } });
      condEls[c.name] = el;
      return h("label", { class: "cond" + (c.type === "bool" ? " bool" : ""), for: id },
        h("span", {}, c.label, c.required ? h("b", { class: "req", title: "필수", text: " *" }) : null), el);
    }

    async function call(tx, params) {
      const r = await fetch("/api/tx/" + tx, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ params }),
      });
      const body = await r.json().catch(() => ({ error: "응답을 읽지 못했어요 (HTTP " + r.status + ")" }));
      if (!r.ok || body.error) throw new Error(body.error || "HTTP " + r.status);
      return body;
    }

    // Grids that share a transaction and parameters share one call.
    function missingRequired(g) {
      const vals = condValues();
      return def.conditions.filter(c => c.required && g.tx_params.includes(c.name) && !(c.name in vals));
    }

    async function load(list, paramsOf, opts = {}) {
      const calls = new Map();
      const ready = [];
      for (const g of list) {
        const missing = g.load === "master" ? [] : missingRequired(g);
        if (missing.length) grids[g.id].clear("필수 조회조건을 넣어 주세요: " + missing.map(c => c.label).join(", "));
        else ready.push(g);
      }
      list = ready;
      if (!list.length) return;
      for (const g of list) {
        const params = paramsOf(g);
        grids[g.id].params = params;
        const key = g.tx + JSON.stringify(params);
        if (!calls.has(key)) calls.set(key, { tx: g.tx, params, grids: [] });
        calls.get(key).grids.push(g);
      }
      if (!opts.live) list.forEach(g => grids[g.id].busy(true));
      const started = performance.now();
      const done = await Promise.all([...calls.values()].map(async c => {
        try {
          const res = await call(c.tx, c.params);
          c.grids.forEach(g => grids[g.id].fill(res.datasets[g.dataset], res.meta, opts.live));
          return `${c.tx} ${res.elapsed_ms}ms`;
        } catch (e) {
          c.grids.forEach(g => grids[g.id].fail(e.message));
          return `${c.tx} 오류`;
        }
      }));
      const when = new Date().toLocaleTimeString("sv-SE");
      status.textContent = `${opts.live ? "실시간 갱신 " + when + " · " : ""}${done.join(" · ")} · 화면 ${Math.round(performance.now() - started)}ms`;
    }

    function searchParams(g) {
      const all = condValues();
      return Object.fromEntries(Object.entries(all).filter(([k]) => g.tx_params.includes(k)));
    }

    let recalcTimer = null;
    function recalc() {
      const list = def.grids.filter(g => g.load === "input");
      if (!list.length) return;
      clearTimeout(recalcTimer);
      recalcTimer = setTimeout(() => load(list, searchParams), 120);
    }

    function search() {
      const list = def.grids.filter(g => g.load !== "master");
      def.grids.filter(g => g.load === "master").forEach(g => grids[g.id].clear("위 목록에서 행을 고르세요"));
      return load(list, searchParams);
    }

    function onSelect(master, row) {
      const details = def.grids.filter(g => g.load === "master" && g.master === master.id);
      if (!details.length || !row) return;
      load(details, g => Object.fromEntries(Object.entries(g.link).map(([p, col]) => [p, row[col]])));
    }

    function drill(g, row) {
      if (!g.drill || !row) return;
      if (g.drill.screen === def.id) {
        for (const [cond, col] of Object.entries(g.drill.params || {})) if (condEls[cond]) condEls[cond].value = row[col] ?? "";
        const first = def.conditions.find(c => c.required && condValues()[c.name] === undefined);
        if (first) condEls[first.name].focus();
        recalc();
        return;
      }
      const q = new URLSearchParams();
      for (const [cond, col] of Object.entries(g.drill.params || {})) if (row[col] != null) q.set(cond, row[col]);
      location.href = "/screen/" + g.drill.screen + "?" + q.toString();
    }

    const conds = def.conditions.length ? h("form", { class: "conds", onsubmit: e => { e.preventDefault(); search(); } },
      def.conditions.map(condField),
      h("button", { type: "submit", class: "primary", title: "Ctrl+Enter" }, "조회")) : null;

    const panels = def.grids.map(g => {
      const grid = Grid(g, { onSelect: row => onSelect(g, row), onDrill: row => drill(g, row) });
      grids[g.id] = grid;
      return grid.el;
    });

    app.append(topbar(def), h("main", { class: "screen" },
      h("div", { class: "title" },
        h("h1", {}, h("span", { class: "sid", text: def.id }), " " + def.title),
        h("a", { class: "spec", href: "/spec/" + def.id, title: "정의 파일과 DB 주석에서 생성한 화면정의서" }, "화면정의서")),
      h("p", { class: "purpose", text: def.purpose }),
      conds,
      h("div", { class: "grids" }, panels),
      status));

    document.addEventListener("keydown", e => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); search(); }
    });

    const opening = def.grids.filter(g => g.load === "open" || g.load === "input");
    opening.forEach(g => grids[g.id].skeleton());
    const hasDrill = def.conditions.some(c => urlParams.has(c.name));
    def.grids.filter(g => g.load === "search").forEach(g => grids[g.id].clear("조회조건을 정하고 조회를 누르세요"));
    def.grids.filter(g => g.load === "master").forEach(g => grids[g.id].clear("위 목록에서 행을 고르세요"));
    if (hasDrill) search(); else if (opening.length) load(opening, searchParams);

    // 실시간: the server says when the DB head moves; live grids reload with their last conditions.
    const liveGrids = def.grids.filter(g => g.live);
    if (liveGrids.length && window.EventSource) {
      const dot = document.getElementById("live-dot");
      const es = new EventSource("/api/live");
      let first = true, pending = false;
      es.addEventListener("change", () => {
        if (first) { first = false; return; }          // the opening head, nothing changed yet
        if (pending) return;
        pending = true;
        const list = liveGrids.filter(g => grids[g.id].params);
        load(list, g => grids[g.id].params, { live: true }).finally(() => { pending = false; });
      });
      es.onopen = () => { dot.classList.remove("off"); dot.classList.add("on"); dot.title = "실시간 연결됨"; };
      es.onerror = () => { dot.classList.remove("on"); dot.classList.add("off"); dot.title = "실시간 연결 끊김 (자동 재연결)"; };
    }
  }

  // ---------- grid ----------
  function Grid(g, { onSelect, onDrill }) {
    let rows = [], view = [], sel = -1, sortCol = null, sortDir = 1;
    let changed = new Map();   // row key -> Set of changed columns ("*" = new row), shown once
    let previous = new Map();  // row key -> the row before this fill (numbers roll from here)
    let loaded = false;
    const keyCol = g.key || g.columns[0].name;
    const rowKey = g.view === "cards" ? () => "card" : r => String(r[keyCol]);   // cards show one row
    const count = h("span", { class: "count" });
    const metaBox = h("span", { class: "meta" });
    const body = h("div", { class: "gbody" });
    const chartBox = g.chart ? h("div", { class: "chart" }) : null;
    const el = h("section", { class: "panel" + (g.view === "cards" ? " cards-panel" : ""), "aria-label": g.title },
      h("div", { class: "phead" }, h("h2", { text: g.title }),
        g.live ? h("span", { class: "livechip", title: "DB가 바뀌면 자동으로 다시 조회해요" }, "실시간") : null,
        metaBox, count,
        h("span", { class: "txid", title: "조회 트랜잭션", text: g.tx })),
      chartBox, body);

    function clear(msg) {
      rows = []; view = []; sel = -1; loaded = false; count.textContent = ""; metaBox.replaceChildren();
      if (chartBox) chartBox.replaceChildren();
      morph(body, () => body.replaceChildren(h("div", { class: "empty", text: msg })));
    }
    function skeleton() {
      const n = g.view === "cards" ? 1 : 4;
      body.replaceChildren(h("div", { class: "skeleton", "aria-hidden": "true" },
        Array.from({ length: n }, () => h("div", { class: "sk" }))));
    }
    // Keep the old data on screen while a query runs; a thin bar shows progress (after a short delay).
    function busy(on) { el.classList.toggle("busy", on); el.setAttribute("aria-busy", on ? "true" : "false"); }
    function fail(msg) {
      busy(false); rows = []; view = []; sel = -1; loaded = false;
      morph(body, () => body.replaceChildren(h("div", { class: "err", role: "alert", text: msg })));
    }

    function fill(ds, meta, live) {
      busy(false);
      if (!ds) return fail(`데이터셋 ${g.dataset}이 없어요`);
      const fresh = ds.rows.map(r => Object.fromEntries(ds.columns.map((c, i) => [c, r[i]])));
      const keep = live && view[sel] ? rowKey(view[sel]) : null;
      changed = new Map();
      previous = new Map(rows.map(r => [rowKey(r), r]));
      const same = loaded && (live || g.load === "input");   // same rows updated in place: no cross-fade
      if (live) {
        const before = new Map(rows.map(r => [rowKey(r), r]));
        for (const r of fresh) {
          const old = before.get(rowKey(r));
          if (!old) { changed.set(rowKey(r), new Set(["*"])); continue; }
          const cols = g.columns.filter(c => JSON.stringify(old[c.name]) !== JSON.stringify(r[c.name])).map(c => c.name);
          if (cols.length) changed.set(rowKey(r), new Set(cols));
        }
      } else {
        sortCol = null;
      }
      const hadFocus = body.contains(document.activeElement);
      rows = fresh;
      renderMeta(meta || {});
      if (same) apply(); else morph(body, apply);
      loaded = true;
      sel = keep ? view.findIndex(r => rowKey(r) === keep) : -1;
      if (keep) mark(false);
      if (hadFocus) { const t = body.querySelector(".grid"); if (t) t.focus({ preventScroll: true }); }
      changed = new Map();   // highlight once; a later sort does not flash again
      previous = new Map();
      if (chartBox) renderChart();
    }

    function renderMeta(meta) {
      metaBox.replaceChildren(...Object.entries(g.meta || {}).filter(([k]) => k in meta).map(([k, label]) => {
        const v = meta[k];
        const cls = typeof v === "boolean" ? (v ? "badge ok" : "badge bad") : "badge";
        const text = typeof v === "boolean" ? (v ? "예" : "아니오") : fmt(v, "number");
        return h("span", { class: cls }, label + " ", h("b", { text }));
      }));
    }

    function apply() {
      view = rows.slice();
      if (sortCol) {
        const c = g.columns.find(c => c.name === sortCol);
        const num = NUMERIC.has(c.format);
        view.sort((a, b) => {
          const x = a[sortCol], y = b[sortCol];
          if (x == null) return 1; if (y == null) return -1;
          return (num ? x - y : String(x).localeCompare(String(y), "ko")) * sortDir;
        });
      }
      count.textContent = g.view === "cards" ? "" : `${nf.format(rows.length)}건`;
      g.view === "cards" ? renderCards() : renderTable();
    }

    function flashed(r, col) {
      const set = changed.get(rowKey(r));
      return !!set && (set.has("*") || set.has(col));
    }

    function renderCards() {
      const r = rows[0];
      if (!r) return body.replaceChildren(h("div", { class: "empty", text: "조회된 내역이 없어요" }));
      const old = previous.get("card");
      const values = g.columns.map(c => h("div", { class: ["cv", tone(r[c.name], c.color, r)].join(" ").trim(),
        text: fmt(r[c.name], c.format) || "-" }));
      body.replaceChildren(h("div", { class: "cards" }, g.columns.map((c, i) =>
        h("div", { class: "card" }, h("div", { class: "cl", text: c.label }), values[i]))));
      if (old) g.columns.forEach((c, i) => {
        if (NUMERIC.has(c.format)) roll(values[i], old[c.name], r[c.name], c.format);
        else if (old[c.name] !== r[c.name]) anim(values[i], [{ opacity: 0, transform: "translateY(3px)" }, { opacity: 1, transform: "none" }], 180);
      });
    }

    function renderTable() {
      if (!rows.length) return body.replaceChildren(h("div", { class: "empty", text: "조회된 내역이 없어요" }));
      const head = h("tr", {}, h("th", { class: "rn", scope: "col", text: "#" }), g.columns.map(c => {
        const on = sortCol === c.name;
        const th = h("th", { scope: "col", class: c.align === "right" ? "r" : null,
          "aria-sort": on ? (sortDir > 0 ? "ascending" : "descending") : null, title: c.source || "화면 계산 값" },
          h("button", { type: "button", onclick: () => sortBy(c.name) }, c.label, on ? (sortDir > 0 ? " ▲" : " ▼") : ""));
        if (c.width) th.style.width = c.width + "px";
        return th;
      }));
      const rolls = [];
      const tbody = h("tbody", {}, view.map((r, i) => h("tr", {
        "data-k": rowKey(r),
        class: [i === sel ? "sel" : "", changed.get(rowKey(r))?.has("*") ? "new" : ""].join(" ").trim() || null,
        "aria-selected": i === sel ? "true" : "false",
        onclick: () => select(i), ondblclick: () => onDrill(view[i]),
      }, h("td", { class: "rn", text: i + 1 }), g.columns.map(c => {
        const v = r[c.name];
        const text = fmt(v, c.format);
        const td = h("td", { class: [c.align === "right" ? "r" : "", tone(v, c.color, r), c.format === "json" ? "js" : "",
          flashed(r, c.name) ? "flash" : ""].join(" ").trim() || null,
          title: c.format === "json" ? text : null, text });
        if (NUMERIC.has(c.format) && flashed(r, c.name)) {
          const was = previous.get(rowKey(r));
          if (was) rolls.push([td, was[c.name], v, c.format]);
        }
        return td;
      }))));
      const foot = g.totals.length ? h("tfoot", {}, h("tr", {}, h("td", { class: "rn", text: "합계" }), g.columns.map(c =>
        g.totals.includes(c.name)
          ? h("td", { class: "r", text: fmt(rows.reduce((s, r) => s + (Number(r[c.name]) || 0), 0), c.format) })
          : h("td", {})))) : null;
      const table = h("table", { class: "grid", tabindex: "0", role: "grid", "aria-rowcount": rows.length,
        onkeydown: key }, h("thead", {}, head), tbody, foot);
      body.replaceChildren(table, h("div", { class: "selbar", "aria-hidden": "true" }));
      rolls.forEach(a => roll(...a));
      if (g.drill) el.classList.add("drillable");
      placeBar(true);
    }

    function sortBy(name) {
      if (sortCol === name) sortDir = -sortDir; else { sortCol = name; sortDir = 1; }
      const keep = view[sel];
      flip(body, () => { apply(); sel = keep ? view.indexOf(keep) : -1; mark(false); });   // the bar slides with its row
    }
    function mark(scroll = true) {
      body.querySelectorAll("tbody tr").forEach((tr, i) => {
        tr.classList.toggle("sel", i === sel); tr.setAttribute("aria-selected", i === sel ? "true" : "false");
      });
      const tr = body.querySelectorAll("tbody tr")[sel];
      if (tr && scroll) tr.scrollIntoView({ block: "nearest" });
      placeBar(false);
    }
    // One highlight that slides to the selected row instead of jumping.
    function placeBar(instant) {
      const bar = body.querySelector(".selbar");
      if (!bar) return;
      const tr = body.querySelectorAll("tbody tr")[sel];
      if (!tr) { bar.classList.remove("on"); return; }
      const table = tr.closest("table");
      bar.classList.toggle("instant", instant || still() || !bar.classList.contains("on"));
      bar.style.transform = `translateY(${table.offsetTop + tr.offsetTop}px)`;
      bar.style.height = tr.offsetHeight + "px";
      bar.classList.add("on");
      if (bar.classList.contains("instant")) { void bar.offsetHeight; bar.classList.remove("instant"); }
    }
    function select(i) {
      if (i < 0 || i >= view.length) return;
      sel = i; mark(); onSelect(view[i]);
    }
    function key(e) {
      if (e.key === "ArrowDown") { e.preventDefault(); select(Math.min(sel + 1, view.length - 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); select(Math.max(sel - 1, 0)); }
      else if (e.key === "Home") { e.preventDefault(); select(0); }
      else if (e.key === "End") { e.preventDefault(); select(view.length - 1); }
      else if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && sel >= 0) { e.preventDefault(); onDrill(view[sel]); }
    }

    function renderChart() {
      const { x, y } = g.chart;
      const pts = rows.filter(r => r[y] != null).map(r => [String(r[x]), Number(r[y])]).sort((a, b) => a[0] < b[0] ? -1 : 1);
      if (pts.length < 2) return chartBox.replaceChildren();
      const W = Math.max(320, Math.round(chartBox.clientWidth - 16)), H = 160, L = 64, R = 12, T = 12, B = 24;
      const ys = pts.map(p => p[1]);
      let lo = Math.min(...ys), hi = Math.max(...ys);
      if (hi === lo) { hi += 1; lo -= 1; }
      const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
      const px = i => L + (W - L - R) * i / (pts.length - 1);
      const py = v => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
      const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "line", role: "img",
        "aria-label": `${g.columns.find(c => c.name === y).label} 추이` });
      for (let k = 0; k <= 3; k++) {
        const v = lo + (hi - lo) * k / 3;
        s.append(svg("line", { x1: L, x2: W - R, y1: py(v), y2: py(v), class: "gl" }));
        const t = svg("text", { x: L - 8, y: py(v) + 4, class: "ax", "text-anchor": "end" }); t.textContent = rf.format(v); s.append(t);
      }
      [0, Math.floor((pts.length - 1) / 2), pts.length - 1].forEach(i => {
        const t = svg("text", { x: px(i), y: H - 6, class: "ax", "text-anchor": i === 0 ? "start" : i === pts.length - 1 ? "end" : "middle" });
        t.textContent = pts[i][0].slice(0, 10); s.append(t);
      });
      s.append(svg("path", { d: pts.map((p, i) => `${i ? "L" : "M"}${px(i).toFixed(1)},${py(p[1]).toFixed(1)}`).join(""), class: "ln" }));
      const last = pts[pts.length - 1];
      s.append(svg("circle", { cx: px(pts.length - 1), cy: py(last[1]), r: 3.5, class: "pt" }));
      const lt = svg("text", { x: px(pts.length - 1) - 6, y: py(last[1]) - 8, class: "lv", "text-anchor": "end" });
      lt.textContent = rf.format(last[1]); s.append(lt);
      chartBox.replaceChildren(s);
    }

    return { el, fill, clear, busy, fail, skeleton };
  }

  if (DATA.screen) Screen(DATA.screen); else home();
  revealTab();
})();
