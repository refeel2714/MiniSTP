/* STP-UI runtime: turns a resolved screen definition into a working screen.
 *
 * The runtime knows nothing about any one screen. Everything it draws comes from
 * the definition (conditions, grids, columns with labels from DB comments) and
 * everything it shows comes from read transactions (POST /api/tx/{id}).
 * A grid's 처리 buttons (top right of the grid) open an input window and call a
 * 처리 transaction (POST /api/action/{id}); an input that takes a code opens a
 * 찾기 팝업 to pick it from a list.
 * Keys: Enter in a condition = 조회, ↑/↓ moves the row (in a calendar ←/→ a day, ↑/↓ a week), Enter or double-click on
 * a row = move to the linked screen, Ctrl+Enter anywhere = 조회, F2 in an input
 * with a 찾기 button = open the list, Ctrl+K anywhere = 바로 가기 창 (a screen, or a code and the screen to open for it).
 * The left 메뉴트리 is the screen classification (대분류 > 중분류 > 화면) the server read from menu.yaml.
 */
(function () {
  "use strict";
  const DATA = JSON.parse(document.getElementById("stpui-data").textContent);
  const app = document.getElementById("app");
  const NUMERIC = new Set(["number", "money", "rate"]);
  const LEVELS = new Set(["class", "total"]);   // emphasis: rows drawn as 분류 합계 and 총합계
  const nf = new Intl.NumberFormat("ko-KR");
  const rf = new Intl.NumberFormat("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pf = new Intl.NumberFormat("ko-KR", { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const dtf = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const STATUS = {
    MATCH: "ok", ACCEPTED: "ok", FILLED: "ok", true: "ok",
    BREAK: "bad", REJECTED: "bad", false: "bad",
    RESOLVED: "warn", UNKNOWN: "warn", CANCELED: "warn", PENDING: "warn", "적용 중": "ok", "예정": "warn",
    OPEN: "ok", SUSPENDED: "warn", CLOSED: "bad",
    "일치": "ok", "불일치(판단함)": "warn", "불일치": "bad", "대사 없음": "miss",   // miss: something owed is not there
    "결제 완료": "ok", "결제 대기": "info", "체결 대기": "info", "부분 체결": "warn",   // info: on its way, nothing to do
    "접수 거부": "bad", "결제 지연": "bad", "접수 기록 없음": "miss",
    "정상": "ok", "주의": "warn", "확인 필요": "bad", "평가 전": "info",
    "완료": "ok", "대기": "info", "누락": "miss", "점검 완료": "ok", "진행 중": "info",
  };

  // ---------- account (다계좌) ----------
  // Every screen looks at one account, picked in the top bar and remembered in this browser.
  // The server gets it with every transaction; it never comes from a screen definition.
  const ACCOUNTS = DATA.accounts || [];
  // 코드명: a column that holds a 공통코드 is shown by the code's name; the code itself stays in the tooltip
  // and in the row (colour rules, links and buttons still read the code).
  const CODES = DATA.codes || {};
  function codeName(c, v) {
    const names = c && c.codes ? CODES[c.codes] : null;
    return names && v !== null && v !== undefined && Object.hasOwn(names, v) ? names[v] : null;
  }
  const ACCOUNT_KEY = "stpui.account";
  const ACCOUNT_STATUS = { SUSPENDED: "정지", CLOSED: "해지" };
  const knownAccount = id => ACCOUNTS.some(a => a.account_id === id);
  function rememberAccount(id) { try { localStorage.setItem(ACCOUNT_KEY, id); } catch (e) { /* private mode */ } }
  const ACCOUNT = (function () {
    const url = new URL(location.href), asked = url.searchParams.get("account");
    let saved = null;
    try { saved = localStorage.getItem(ACCOUNT_KEY); } catch (e) { /* private mode */ }
    if (asked !== null) {              // a link from the account list: take it, then keep the address clean
      url.searchParams.delete("account");
      history.replaceState(null, "", url.pathname + url.search);
      if (knownAccount(asked)) rememberAccount(asked);
    }
    return [asked, saved, DATA.default_account].find(knownAccount) || (ACCOUNTS[0] || {}).account_id || DATA.default_account;
  })();
  // 실전 테두리: the shell's environment or the picked account's (trading_accounts.env), whichever is live.
  // The server could only write the shell's into the page, because the account is picked in this browser.
  // A sample stays a sample whatever its accounts say.
  const ACCOUNT_LIVE = DATA.env !== "sample" && (ACCOUNTS.find(a => a.account_id === ACCOUNT) || {}).env === "LIVE";
  if (ACCOUNT_LIVE && DATA.env !== "live") {
    DATA.env = "live";
    DATA.env_name = "실전";
    document.body.dataset.env = "live";
    document.title = "[실전] " + document.title;
  }
  function accountPicker() {
    if (ACCOUNTS.length < 2) return null;   // one account: nothing to pick, the bar stays as it was
    const el = h("select", { id: "acct", "aria-label": "조회할 계좌" }, ACCOUNTS.map(a => h("option", {
      value: a.account_id, selected: a.account_id === ACCOUNT,
      text: `${a.account_id} ${a.alias}` + (ACCOUNT_STATUS[a.status] ? ` · ${ACCOUNT_STATUS[a.status]}` : ""),
    })));
    el.addEventListener("change", () => { rememberAccount(el.value); location.reload(); });
    const me = ACCOUNTS.find(a => a.account_id === ACCOUNT) || {};
    return h("label", { class: "acct" + (me.status && me.status !== "OPEN" ? " warn" : ""), for: "acct",
      title: `고객 ${me.customer_id || ""} · 모든 화면이 이 계좌를 보여줘요` }, h("span", { text: "계좌" }), el);
  }

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

  // ---------- server ----------
  async function post(url, payload, headers) {
    const r = await fetch(url, {
      method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, headers || {}),
      body: JSON.stringify(payload),
    });
    const body = await r.json().catch(() => ({ error: "응답을 읽지 못했어요 (HTTP " + r.status + ")" }));
    if (!r.ok || body.error) throw new Error(body.error || "HTTP " + r.status);
    return body;
  }
  const call = (tx, params) => post("/api/tx/" + tx, { params, account: ACCOUNT });   // 조회
  // 처리: only a page this server sent knows the token, so no other site can press a button.
  const act = (tx, params) => post("/api/action/" + tx, { params, account: ACCOUNT }, { "X-STPUI-Token": DATA.token });

  const TOAST_KEY = "stpui.toast";   // what a 처리 said, carried over a page reload
  function toast(text) {
    let box = document.getElementById("toast");
    if (!box) { box = h("div", { id: "toast", role: "status", "aria-live": "polite" }); document.body.append(box); }
    const t = h("div", { class: "toast", text });
    box.append(t);
    setTimeout(() => t.remove(), 5000);
  }

  // ---------- formatting ----------
  function fmt(v, format) {
    if (v === null || v === undefined || v === "") return "";
    switch (format) {
      case "number": case "money": return nf.format(v);
      case "rate": return rf.format(v);
      case "date": return String(v).slice(0, 10);
      case "datetime": { const d = new Date(v); return isNaN(d) ? String(v) : dtf.format(d); }
      case "time": { const d = new Date(v); return isNaN(d) ? String(v) : dtf.format(d).slice(11); }
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

  // ---------- 셀 표시 유형 ----------
  // A column's `display` draws a small picture next to the value. The value is never replaced:
  // the number, the name and the code stay readable, the picture adds size, trend or state.
  const WEEK = ["월", "화", "수", "목", "금", "토", "일"];
  const HUES = 8;
  const ROUND = new Set(["STOCK"]);   // 주식은 원, 그 밖(ETF, ETN ...)은 둥근 사각
  function hue(code) {
    let n = 0;
    for (const ch of String(code ?? "")) n = (n * 31 + ch.codePointAt(0)) % 9973;
    return n % HUES;   // the same code is the same colour on every screen
  }
  function badge(r, c, cols) {
    const name = String(r[c.name] ?? "").trim();
    const code = c.code ? r[c.code] : null, kind = c.shape ? r[c.shape] : null;
    const kindName = codeName((cols || []).find(x => x.name === c.shape), kind) ?? kind;
    const mark = h("span", { class: `ib h${hue(code ?? name)} ${ROUND.has(kind) ? "round" : ""}`.trim(), "aria-hidden": "true",
      text: (Array.from(name)[0] || "?").toUpperCase() });
    const sub = [code, kindName].filter(v => v !== null && v !== undefined && v !== "").join(" · ");
    return h("div", { class: "ibox" }, mark,
      h("div", { class: "iname" }, h("b", { text: name || "-" }), sub ? h("span", { text: sub }) : null));
  }
  // A share of a whole: a rate as a percentage, or a quantity against the column that is its whole ("4 / 10").
  // A share of a whole (비중) is drawn against the largest share in the column (`top`), so 1~6% weights of a
  // hundred holdings still tell apart; the number stays the real share. A count against its own whole ("4 / 10") is not scaled.
  function meter(v, whole, top) {
    if (v === null || v === undefined) return h("span", { class: "none", text: "-" });
    const raw = whole === undefined ? Number(v) : (Number(whole) ? Number(v) / Number(whole) : 0);
    const share = whole === undefined && top ? raw / top : raw;
    const fill = h("i", { class: raw >= 1 ? "full" : null });
    fill.style.width = (Math.max(0, Math.min(1, share)) * 100).toFixed(1) + "%";
    return h("div", { class: "meter" }, h("span", { text: whole === undefined ? pf.format(v) : `${nf.format(v)} / ${nf.format(whole ?? 0)}` }),
      h("div", { class: "mtrack", "aria-hidden": "true" }, fill));
  }
  // 진행 단계: a dot a step. done 끝 (filled) · now 진행 중 (ring) · stop 멈춤 (×) · wait 전 (hollow).
  const STEP_TEXT = { done: "끝", now: "진행 중", stop: "멈춤", wait: "전" };
  function steps(values, names) {
    const v = Array.isArray(values) ? values : [];
    if (!v.length) return h("span", { class: "none", text: "-" });
    const said = names.map((n, i) => `${n} ${STEP_TEXT[v[i]] || "?"}`);
    return h("div", { class: "steps", role: "img", "aria-label": said.join(", "), title: said.join(" → ") },
      v.map((st, i) => h("span", { class: `sd ${STEP_TEXT[st] ? st : "wait"}${i && v[i - 1] === "done" && (st === "done" || st === "now") ? " on" : ""}`,
        text: st === "stop" ? "×" : null })));
  }
  function spark(values) {
    const pts = (Array.isArray(values) ? values : []).map(Number).filter(Number.isFinite);
    if (pts.length < 2) return h("span", { class: "none", title: "종가가 이틀 치 이상 쌓이면 그려요", text: "-" });
    const W = 72, H = 20, P = 2.5, lo = Math.min(...pts), hi = Math.max(...pts), first = pts[0], last = pts[pts.length - 1];
    const x = i => P + i * (W - 2 * P) / (pts.length - 1);
    const y = v => hi === lo ? H / 2 : H - P - (v - lo) * (H - 2 * P) / (hi - lo);
    const s = svg("svg", { class: ("spark " + (last > first ? "up" : last < first ? "down" : "")).trim(), viewBox: `0 0 ${W} ${H}`,
      width: W, height: H, role: "img",
      "aria-label": `최근 ${pts.length}일 종가 ${nf.format(first)}에서 ${nf.format(last)}, 최저 ${nf.format(lo)} 최고 ${nf.format(hi)}` });
    const tip = svg("title", {}); tip.textContent = `최근 ${pts.length}일: ${nf.format(first)} → ${nf.format(last)} (최저 ${nf.format(lo)}, 최고 ${nf.format(hi)})`;
    s.append(tip, svg("polyline", { points: pts.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ") }),
      svg("circle", { cx: x(pts.length - 1).toFixed(1), cy: y(last).toFixed(1), r: 2 }));
    return s;
  }

  // ---------- 찾기 팝업 ----------
  // An input that takes a code gets a 찾기 button (F2): a list to pick from, narrowed as you type.
  // Typing the code directly still works; the list is help, not a gate.
  function withLookup(input, lk, fixed) {
    const name = h("span", { class: "lkname" });
    const open = () => openLookup(lk, fixed || {}, input.value.trim(), row => {
      input.value = row[lk.value] ?? "";
      name.textContent = lk.shows ? (row[lk.shows] ?? "") : "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.focus();
    });
    input.addEventListener("keydown", e => {
      if (e.key === "F2" || (e.altKey && e.key === "ArrowDown")) { e.preventDefault(); open(); }
    });
    input.addEventListener("input", e => { if (e.isTrusted) name.textContent = ""; });   // typed over: that name is gone
    return h("span", { class: "lk" }, input,
      h("button", { type: "button", class: "find", title: lk.title + " (F2)", onclick: open }, "찾기"), name);
  }

  function openLookup(lk, fixed, typed, onPick) {
    let rows = [], view = [], sel = 0;
    const filter = h("input", { type: "search", class: "lkfilter", placeholder: "코드나 이름 일부를 넣으면 걸러져요",
      autocomplete: "off", spellcheck: "false", "aria-label": "걸러 보기" });
    const count = h("span", { class: "count" });
    const box = h("div", { class: "lkbody" }, h("div", { class: "empty", text: "불러오는 중이에요" }));
    const dlg = h("dialog", { class: "dlg lookup", "aria-label": lk.title },
      h("div", { class: "dhead" }, h("h2", { text: lk.title }), count,
        h("span", { class: "txid", title: "조회 트랜잭션", text: lk.tx })),
      filter, box,
      h("div", { class: "dfoot" }, h("span", { class: "hint", text: "↑↓ 이동 · Enter 선택 · Esc 닫기" }),
        h("button", { type: "button", class: "ghost", onclick: () => dlg.close() }, "닫기"),
        h("button", { type: "button", class: "primary", onclick: () => pick(sel) }, "선택")));
    function pick(i) {
      const r = view[i];
      if (!r) return;
      dlg.close();
      onPick(r);
    }
    function mark() {
      box.querySelectorAll("tbody tr").forEach((tr, i) => {
        tr.classList.toggle("sel", i === sel); tr.setAttribute("aria-selected", i === sel ? "true" : "false");
      });
      const tr = box.querySelectorAll("tbody tr")[sel];
      if (tr) tr.scrollIntoView({ block: "nearest" });
    }
    function render() {
      const q = filter.value.trim().toLowerCase();
      view = q ? rows.filter(r => lk.columns.some(c => String(r[c.name] ?? "").toLowerCase().includes(q))) : rows.slice();
      sel = Math.max(0, Math.min(sel, view.length - 1));
      count.textContent = `${nf.format(view.length)}건`;
      if (!view.length) return box.replaceChildren(h("div", { class: "empty", text: rows.length ? "맞는 항목이 없어요" : "항목이 없어요" }));
      box.replaceChildren(h("table", { class: "grid", role: "listbox", "aria-label": lk.title },
        h("thead", {}, h("tr", {}, lk.columns.map(c => h("th", { scope: "col", class: c.align === "right" ? "r" : null, text: c.label })))),
        h("tbody", {}, view.map((r, i) => h("tr", { role: "option", onclick: () => { sel = i; mark(); }, ondblclick: () => pick(i) },
          lk.columns.map(c => h("td", { class: c.align === "right" ? "r" : null, text: codeName(c, r[c.name]) ?? fmt(r[c.name], c.format) })))))));
      mark();
    }
    filter.addEventListener("input", () => { sel = 0; render(); });
    filter.addEventListener("keydown", e => {
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, view.length - 1); mark(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(sel - 1, 0); mark(); }
      else if (e.key === "Enter") { e.preventDefault(); pick(sel); }
    });
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
    filter.focus();
    call(lk.tx, fixed).then(res => {
      const ds = res.datasets.main;
      rows = ds.rows.map(r => Object.fromEntries(ds.columns.map((c, i) => [c, r[i]])));
      // What was already typed narrows the list, unless nothing matches it.
      const q = typed.toLowerCase();
      if (q && rows.some(r => lk.columns.some(c => String(r[c.name] ?? "").toLowerCase().includes(q)))) filter.value = typed;
      const exact = rows.findIndex(r => String(r[lk.value]) === typed);
      render();
      if (exact >= 0) { sel = Math.max(0, view.indexOf(rows[exact])); mark(); }
    }).catch(e => box.replaceChildren(h("div", { class: "err", role: "alert", text: e.message })));
  }

  // ---------- 바로 가기 창 (Ctrl+K) ----------
  // One box that goes anywhere: a screen by its number or name, or a code a screen can be opened for
  // (종목 and the like: whatever a 조회조건 picks from a 찾기 팝업). A code that more than one screen
  // takes asks which screen next. Nothing here knows a screen: the list comes from the definitions.
  const jumpRows = {};                       // 찾기 목록 이름 -> rows, read once per page
  const squash = v => String(v ?? "").toLowerCase().replace(/\s+/g, "");
  function rank(q, ...texts) {               // lower is closer; -1 is no match
    let best = -1;
    texts.forEach((t, i) => {
      const at = squash(t).indexOf(q);
      if (at < 0) return;
      const score = (at === 0 ? 0 : 10) + i;
      if (best < 0 || score < best) best = score;
    });
    return best;
  }
  function openJump() {
    if (document.querySelector("dialog[open]")) return;
    const here = DATA.screen ? DATA.screen.id : null;
    let items = [], sel = 0, picked = null, typed = "";   // picked: the code whose screen is being chosen
    const input = h("input", { type: "text", class: "lkfilter", autocomplete: "off", spellcheck: "false",   // text, not search: Esc closes at once
      role: "combobox", "aria-expanded": "true", "aria-controls": "jump-list", "aria-label": "바로 가기" });
    const crumb = h("div", { class: "jcrumb" });
    const box = h("div", { class: "jbody", id: "jump-list", role: "listbox", "aria-label": "바로 가기" });
    const hint = h("span", { class: "hint" });
    const dlg = h("dialog", { class: "dlg jump", "aria-label": "바로 가기" }, crumb, input, box, h("div", { class: "dfoot" }, hint));

    const screenRow = (m, note) => ({
      el: [h("span", { class: "sid", text: m.id }), h("span", { class: "jt", text: m.title }),
        note || m.id === here ? h("span", { class: "jnote", text: note || "지금 화면" }) : null],
      go: () => { location.href = "/screen/" + m.id; },
    });
    const mark = (lk, row) => badge(row, { name: lk.shows || lk.value, code: lk.value,
      shape: lk.columns.some(c => c.name === "asset_type") ? "asset_type" : null });
    const open = (lk, row, t) => { location.href = `/screen/${t.id}?${t.param}=${encodeURIComponent(row[lk.value])}`; };

    function build() {
      const q = squash(input.value);
      if (picked) {                                           // which screen to open for the code
        const { lk, row } = picked;
        crumb.replaceChildren(mark(lk, row), h("span", { text: "이 코드로 열 화면을 고르세요" }));
        input.placeholder = "화면번호나 화면명";
        hint.textContent = "↑↓ 이동 · Enter 열기 · Esc 뒤로";
        return [{ title: null, rows: lk.into.filter(t => !q || rank(q, t.id, t.id.slice(4), t.title) >= 0).map(t => ({
          el: [h("span", { class: "sid", text: t.id }), h("span", { class: "jt", text: t.title }),
            t.id === here ? h("span", { class: "jnote", text: "지금 화면" }) : null],
          go: () => open(lk, row, t), here: t.id === here })) }];
      }
      crumb.replaceChildren();
      input.placeholder = "화면번호, 화면명, 종목코드나 종목명";
      hint.textContent = "↑↓ 이동 · Enter 열기 · Esc 닫기 · Ctrl+K 어디서나";
      const groups = [];
      const screens = DATA.menu.map(m => [q ? rank(q, m.id, m.id.slice(4), m.title) : 0, m]).filter(x => x[0] >= 0);
      if (q) screens.sort((a, b) => a[0] - b[0]);
      if (screens.length) groups.push({ title: "화면", rows: screens.map(x => screenRow(x[1])) });
      for (const lk of DATA.jump) {
        const rows = jumpRows[lk.name];
        const what = lk.title.replace(/\s*찾기$/, "");
        if (!q) continue;
        if (!rows) { groups.push({ title: what, note: "불러오는 중이에요", rows: [] }); continue; }
        const hit = rows.map(r => [rank(q, r[lk.value], lk.shows ? r[lk.shows] : ""), r]).filter(x => x[0] >= 0)
          .sort((a, b) => a[0] - b[0]);
        if (!hit.length) continue;
        groups.push({ title: what, note: hit.length > 8 ? `${nf.format(hit.length)}건 중 8건` : null,
          rows: hit.slice(0, 8).map(x => ({
            el: [mark(lk, x[1]), h("span", { class: "jnote",
              text: lk.into.length === 1 ? `${lk.into[0].id} ${lk.into[0].title}` : `화면 ${lk.into.length}곳 ›` })],
            go: () => { if (lk.into.length === 1) open(lk, x[1], lk.into[0]); else pick(lk, x[1]); } })) });
      }
      return groups;
    }
    function render(keep) {
      const groups = build();
      items = groups.flatMap(g => g.rows);
      const at = items.findIndex(it => it.here);
      sel = keep ? Math.max(0, Math.min(sel, items.length - 1)) : (picked && at >= 0 ? at : 0);
      let i = 0;
      box.replaceChildren(...groups.flatMap(g => [
        g.title ? h("div", { class: "jgroup" }, g.title, g.note ? h("span", { text: g.note }) : null) : null,
        ...g.rows.map(it => { const n = i++; return h("div", { class: "jitem", role: "option", id: "jump-" + n,
          onclick: () => { sel = n; go(); } }, it.el); }),
      ]).filter(Boolean));
      if (!items.length) {
        box.append(h("div", { class: "jempty", text: squash(input.value) ? "맞는 화면이나 코드가 없어요" : "열 수 있는 화면이 없어요" }));
      } else if (!picked && !squash(input.value) && DATA.jump.length) {
        box.append(h("div", { class: "jtip", text: `코드나 이름을 넣으면 ${DATA.jump.map(lk => lk.title.replace(/\s*찾기$/, "")).join(", ")}도 찾아요` }));
      }
      show();
    }
    function show() {
      box.querySelectorAll(".jitem").forEach((el, i) => {
        el.classList.toggle("sel", i === sel); el.setAttribute("aria-selected", i === sel ? "true" : "false");
      });
      const el = box.querySelectorAll(".jitem")[sel];
      if (el) { el.scrollIntoView({ block: "nearest" }); input.setAttribute("aria-activedescendant", el.id); }
      else input.removeAttribute("aria-activedescendant");
    }
    function go() { const it = items[sel]; if (it) it.go(); }
    function pick(lk, row) { typed = input.value; picked = { lk, row }; input.value = ""; render(); input.focus(); }
    function back() { picked = null; input.value = typed; render(); input.focus(); }

    input.addEventListener("input", () => render());
    input.addEventListener("keydown", e => {
      if (e.isComposing) return;                              // 한글 조합 중의 Enter는 글자를 끝내는 키
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, items.length - 1); show(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(sel - 1, 0); show(); }
      else if (e.key === "Enter") { e.preventDefault(); go(); }
      else if (e.key === "Backspace" && picked && input.value === "") { e.preventDefault(); back(); }
    });
    dlg.addEventListener("cancel", e => { if (picked) { e.preventDefault(); back(); } });   // Esc: one step back first
    dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });               // outside the box
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
    render();
    input.focus();
    for (const lk of DATA.jump) {
      if (jumpRows[lk.name]) continue;
      call(lk.tx, {}).then(res => {
        const ds = res.datasets.main;
        jumpRows[lk.name] = ds.rows.map(r => Object.fromEntries(ds.columns.map((c, i) => [c, r[i]])));
      }).catch(() => { jumpRows[lk.name] = []; }).finally(() => { if (dlg.open && !picked) render(true); });
    }
  }
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") { e.preventDefault(); openJump(); }
  });

  // ---------- 처리 입력 창 ----------
  // A 처리 button opens this: the row it works on at the top, the inputs, and one button that
  // calls the 처리 transaction. A refusal from the server is shown here and the window stays open.
  function openForm(def, g, b, ctx, onDone) {
    const inputs = {};
    const err = h("div", { class: "ferr", role: "alert" });
    const fields = b.fields.map(f => {
      const id = "f-" + f.name;
      const start = f.column ? ctx[f.column] : f.default === "today" ? DATA.today : f.default;
      let el;
      if (f.type === "select") {
        el = h("select", { id, disabled: f.readonly }, h("option", { value: "", text: f.required ? "고르세요" : "선택 안 함" }),
          f.options.map(o => h("option", { value: o, text: o, selected: String(start ?? "") === o })));
      } else if (f.type === "bool") {
        el = h("input", { id, type: "checkbox", checked: start === true || start === "true", disabled: f.readonly });
      } else {
        el = h("input", { id, type: f.type === "date" ? "date" : f.type === "number" ? "number" : "text",
          value: start ?? "", readonly: f.readonly, autocomplete: "off", spellcheck: "false" });
      }
      inputs[f.name] = el;
      const lk = f.lookup && !f.readonly ? def.lookups[f.lookup] : null;
      return h("div", { class: "frow" },
        h("label", { class: "fl", for: id }, f.label, f.required ? h("b", { class: "req", title: "필수", text: " *" }) : null),
        lk ? withLookup(el, lk, f.lookup_params) : el);
    });
    function values() {
      const out = {};
      for (const f of b.fields) {
        const el = inputs[f.name];
        if (f.type === "bool") out[f.name] = el.checked;
        else if (el.value.trim() !== "") out[f.name] = el.value.trim();
      }
      return out;
    }
    const ok = h("button", { type: "submit", class: "primary" }, b.label);
    async function submit(e) {
      e.preventDefault();
      const missing = b.fields.filter(f => f.required && f.type !== "bool" && inputs[f.name].value.trim() === "");
      if (missing.length) {
        err.textContent = "넣어 주세요: " + missing.map(f => f.label).join(", ");
        inputs[missing[0].name].focus();
        return;
      }
      ok.disabled = true; err.textContent = "";
      try {
        const res = await act(b.tx, values());
        dlg.close();
        onDone(res);
      } catch (ex) {
        err.textContent = ex.message; ok.disabled = false;
      }
    }
    const shows = b.shows.length ? h("dl", { class: "fctx" }, b.shows.flatMap(name => {
      const c = g.columns.find(c => c.name === name);
      return [h("dt", { text: c.label }), h("dd", { class: tone(ctx[name], c.color, ctx) || null, text: (codeName(c, ctx[name]) ?? fmt(ctx[name], c.format)) || "-" })];
    })) : null;
    const dlg = h("dialog", { class: "dlg", "aria-labelledby": "dlg-title" },
      h("form", { onsubmit: submit, novalidate: true, oninput: () => { err.textContent = ""; } },
        h("div", { class: "dhead" }, h("h2", { id: "dlg-title", text: b.label }),
          h("span", { class: "txid", title: "처리 트랜잭션: " + b.title, text: b.tx })),
        DATA.env === "live" ? h("p", { class: "flive", role: "note", text: (ACCOUNT_LIVE ? "실전 계좌예요." : "실전 환경이에요.") + " 이 처리는 실전 원장에 기록돼요." }) : null,
        shows, b.note ? h("p", { class: "fnote", text: b.note }) : null,
        h("div", { class: "fields" }, fields), err,
        h("div", { class: "dfoot" }, h("button", { type: "button", class: "ghost", onclick: () => dlg.close() }, "취소"), ok)));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
    const first = b.fields.find(f => !f.readonly);
    (first ? inputs[first.name] : ok).focus();
  }

  // ---------- shell ----------
  const ENV_TITLES = {
    sample: "샘플 DB: 연습용이라 증권사 계좌와 이어져 있지 않아요",
    paper: "모의투자 환경 (MINISTP_KIS_ENV=paper)",
    live: ACCOUNT_LIVE ? `실전 계좌 (${ACCOUNT}): 이 화면의 숫자와 처리는 실전 원장의 것이에요`
      : "실전 환경 (MINISTP_KIS_ENV=live): 이 화면의 숫자와 처리는 실전 원장의 것이에요",
  };
  // What a viewer set on this browser (menu shut, a 대분류 folded). A convenience only: the page works without it.
  const prefs = {
    get(k) { try { return window.localStorage.getItem("stpui." + k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem("stpui." + k, v); } catch (e) { /* private window: not kept */ } },
  };
  function topbar() {
    const shut = () => document.body.dataset.nav === "off";
    const toggle = h("button", { type: "button", class: "navbtn", "aria-controls": "nav-tree", title: "메뉴 접기·펴기",
      onclick: () => {
        document.body.dataset.nav = shut() ? "on" : "off";
        prefs.set("nav", document.body.dataset.nav);
        toggle.setAttribute("aria-expanded", String(!shut()));
        window.dispatchEvent(new Event("resize"));      // a split screen fits itself to the room it has
      } }, h("span", { class: "bars", "aria-hidden": "true" }), "메뉴");
    toggle.setAttribute("aria-expanded", String(!shut()));
    return h("header", { class: "top" },
      toggle,
      h("a", { class: "brand", href: "/" }, "MiniSTP ", h("b", { text: "STP-UI" })),
      h("span", { class: "grow" }),
      h("button", { type: "button", class: "jumpbtn", title: "화면이나 종목으로 바로 가기 (Ctrl+K)", onclick: openJump },
        "바로 가기", h("kbd", { text: "Ctrl K" })),
      accountPicker(),
      h("span", { class: "env", title: DATA.writable
        ? `DB ${DATA.db} · 조회는 읽기 전용 연결, 처리 버튼을 누를 때만 쓰기 연결`
        : `DB ${DATA.db} · 읽기 전용 연결 (처리 버튼은 ui --write 로 열었을 때만)` },
        h("span", { class: "dot", id: "live-dot" }),
        // 실전 테두리: which environment this is, said in a word next to the DB (the frame itself is CSS on body[data-env])
        h("b", { class: "envchip " + DATA.env, title: ENV_TITLES[DATA.env], text: DATA.env_name }),
        `${DATA.env === "sample" ? "" : DATA.db + " · "}기준일 ${DATA.today} · ${DATA.writable ? "처리 가능" : "조회 전용"}`));
  }

  // 메뉴트리: 대분류 > (중분류) > 화면. A 중분류 holding one screen is not drawn as a branch of its own:
  // the screen sits right under the 대분류, and the branch appears when a second screen joins it.
  function leaf(m, screen) {
    const on = screen && m.id === screen.id;
    return h("li", {}, h("a", { href: "/screen/" + m.id, class: on ? "on" : null, "aria-current": on ? "page" : null, title: m.id + " " + m.title },
      h("span", { class: "sid", text: m.id.slice(4) }), h("span", { class: "lt", text: m.title })));
  }
  function sidebar(screen) {
    const folded = new Set((prefs.get("folded") || "").split(",").filter(Boolean));
    const here = screen ? screen.id.slice(4, 5) : null;
    const areas = DATA.tree.map(a => {
      const kids = a.groups.flatMap(g => g.screens.length > 1
        ? [h("li", { class: "grp" }, h("span", { class: "gname", text: g.name }), h("ul", {}, g.screens.map(m => leaf(m, screen))))]
        : g.screens.map(m => leaf(m, screen)));
      const d = h("details", { class: "area", open: a.id === here || !folded.has(a.id) },   // the branch you are in is never shut
        h("summary", { title: a.note }, h("span", { class: "an", text: a.id }), h("span", { class: "lt", text: a.name })),
        h("ul", {}, kids));
      d.addEventListener("toggle", () => {
        if (d.open) folded.delete(a.id); else folded.add(a.id);
        prefs.set("folded", [...folded].join(","));
      });
      return d;
    });
    return h("nav", { class: "tree", id: "nav-tree", "aria-label": "화면 메뉴" }, areas);
  }
  // header on top, then the menu tree on the left and the page on the right
  function frame(screen, main) {
    app.append(topbar(), h("div", { class: "frame" }, sidebar(screen), main));
  }
  // Keep the current screen's line in view when the tree is taller than the window.
  function revealLeaf() {
    const on = document.querySelector(".tree a.on");
    if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest" });
  }

  function home() {
    frame(null, h("main", { class: "home" },
      h("h1", { text: "업무 화면" }),
      h("p", { class: "lead", text: "화면은 거래가 흘러가는 순서로 나눠요. 화면번호의 첫 자리가 대분류, 둘째 자리까지가 중분류예요. 화면은 정의 파일(YAML)에서 만들어지고, 열 이름은 DB 주석에서 가져와요." }),
      DATA.tree.map(a => h("section", { class: "harea" },
        h("h2", {}, h("span", { class: "an", text: a.id }), " " + a.name, h("span", { class: "hnote", text: a.note })),
        h("div", { class: "tiles" }, a.groups.flatMap(g => g.screens.map(m => h("a", { class: "tile", href: "/screen/" + m.id },
          h("span", { class: "sid", text: m.id }), h("span", { class: "tt", text: m.title }),
          h("span", { class: "tg", text: g.name })))))))));
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
          c.options.map(o => h("option", { value: o, text: codeName(c, o) ? `${codeName(c, o)} (${o})` : o, selected: o === def0 })));
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
      const lk = c.lookup ? def.lookups[c.lookup] : null;
      return h("label", { class: "cond" + (c.type === "bool" ? " bool" : ""), for: id },
        h("span", {}, c.label, c.required ? h("b", { class: "req", title: "필수", text: " *" }) : null),
        lk ? withLookup(el, lk, c.lookup_params) : el);
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
      const out = Object.fromEntries(Object.entries(all).filter(([k]) => g.tx_params.includes(k)));
      // 입력 그리드: what is typed there goes along as one condition (`행키:값,…`); before it has rows, nothing
      for (const e of def.grids.filter(x => x.edit && g.tx_params.includes(x.edit.param))) {
        const v = grids[e.id] && grids[e.id].editValue();
        if (v !== undefined) out[e.edit.param] = v;
      }
      return out;
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
      def.grids.filter(g => g.load === "master").forEach(g => grids[g.id].clear("목록에서 행을 고르세요"));
      return load(list, searchParams);
    }

    function onSelect(master, row) {
      const details = def.grids.filter(g => g.load === "master" && g.master === master.id);
      if (!details.length || !row) return;
      const linked = g => Object.fromEntries(Object.entries(g.link).map(([p, col]) => [p, row[col]]));
      const said = col => codeName(master.columns.find(c => c.name === col), row[col]) ?? row[col];   // a code by its name
      details.forEach(g => grids[g.id].about([...new Set(Object.values(g.link).map(said).filter(v => v != null && v !== ""))].join(" · ")));   // whose detail this is
      load(details, linked);
    }

    function drill(g, row) {
      if (!g.drill || !row) return;
      // one screen for every row, or the one the row itself names (only a screen the definition lists)
      const target = g.drill.by ? row[g.drill.by] : g.drill.screen;
      if (g.drill.by && !(g.drill.screens || []).includes(target)) return;
      if (target === def.id) {
        for (const [cond, col] of Object.entries(g.drill.params || {})) if (condEls[cond]) condEls[cond].value = row[col] ?? "";
        const first = def.conditions.find(c => c.required && condValues()[c.name] === undefined);
        if (first) condEls[first.name].focus();
        recalc();
        return;
      }
      const q = new URLSearchParams();
      for (const [cond, col] of Object.entries(g.drill.params || {})) if (row[col] != null) q.set(cond, row[col]);
      if (g.drill.account && row[g.drill.account]) q.set("account", row[g.drill.account]);   // open it for that account
      location.href = "/screen/" + target + "?" + q.toString();
    }

    // 처리 버튼: the input window starts from the selected row (and what the grid was opened with);
    // when it is done the grids are read again in place, so the changed cells light up.
    function onAction(g, b, row) {
      const ctx = Object.assign({}, grids[g.id].params || {}, row || {});
      openForm(def, g, b, ctx, async res => {
        if (b.reload) {   // the top bar shows it too (계좌 선택): open the page again and say it there
          try { sessionStorage.setItem(TOAST_KEY, res.message); } catch (e) { /* private mode */ }
          return location.reload();
        }
        toast(res.message);
        await refresh();
        status.textContent = `${b.tx} ${res.elapsed_ms}ms · ${res.message}`;
      });
    }
    async function refresh() {
      const shown = g => grids[g.id].params !== undefined;
      const again = g => grids[g.id].params;
      await load(def.grids.filter(g => g.load !== "master" && shown(g)), again, { live: true });
      await load(def.grids.filter(g => g.load === "master" && shown(g)), again, { live: true });
    }

    const conds = def.conditions.length ? h("form", { class: "conds", onsubmit: e => { e.preventDefault(); search(); } },
      def.conditions.map(condField),
      h("button", { type: "submit", class: "primary", title: "Ctrl+Enter" }, "조회")) : null;

    const panels = def.grids.map(g => {
      const grid = Grid(g, { onSelect: row => onSelect(g, row), onDrill: row => drill(g, row),
        onAction: (b, row) => onAction(g, b, row), onEdit: () => recalc(), lookups: def.lookups,
        leads: def.grids.some(d => d.load === "master" && d.master === g.id) });
      grids[g.id] = grid;
      return grid.el;
    });

    // 좌우 구성: the list that has details on the left, its details on the right, each scrolling
    // inside its own panel. Grids that follow no row stay below, full width.
    function splitView() {
      const editParams = def.grids.filter(g => g.edit).map(g => g.edit.param);
      const follows = g => g.load === "input" && g.tx_params.some(p => editParams.includes(p));   // worked out from an 입력 그리드
      const detail = g => g.load === "master" || follows(g);
      const leads = g => !!g.edit || def.grids.some(d => d.load === "master" && d.master === g.id);
      const pick = test => def.grids.filter(test).map(g => grids[g.id].el);
      def.grids.filter(g => (leads(g) || detail(g)) && g.view !== "cards")
        .forEach(g => { grids[g.id].el.style.flexGrow = g.weight; });   // share of the height (cards take what they need)
      const split = h("div", { class: "split" },
        h("div", { class: "side" }, pick(leads)), h("div", { class: "side" }, pick(detail)));
      split.style.setProperty("--left", def.left + "fr");
      split.style.setProperty("--right", (100 - def.left) + "fr");
      return [split, ...pick(g => !leads(g) && !detail(g))];
    }
    // The two sides fill what is left of the window, so a long list scrolls in place.
    function fitSplit() {
      const split = app.querySelector(".split");
      if (!split) return;
      const room = window.innerHeight - split.getBoundingClientRect().top - window.scrollY - 56;   // the status line and the page margin stay in view
      split.style.setProperty("--fit", Math.max(380, Math.round(room)) + "px");
    }
    // 위아래 구성: the last list reaches down to the bottom of the window, so a long list shows as many rows as fit
    // and scrolls in place (the others keep their 360px). It is worked out again when something above it grows.
    const stacked = () => def.layout === "split" ? null
      : [...app.querySelectorAll(".grids > .panel")].filter(p => !p.classList.contains("cards-panel")).pop();
    function fitLast() {
      const last = stacked(), body = last && last.querySelector(".gbody");
      if (!body || window.innerWidth <= 900) { if (body) body.style.maxHeight = ""; return; }
      const below = last.getBoundingClientRect().bottom - body.getBoundingClientRect().bottom;   // a legend under the list
      const room = window.innerHeight - body.getBoundingClientRect().top - window.scrollY - below - 56;
      body.style.maxHeight = Math.max(360, Math.round(room)) + "px";
    }

    frame(def, h("main", { class: "screen" },
      DATA.path.length ? h("p", { class: "crumb", "aria-label": "화면 분류", text: DATA.path.join(" › ") }) : null,
      h("div", { class: "title" },
        h("h1", {}, h("span", { class: "sid", text: def.id }), " " + def.title),
        h("a", { class: "spec", href: "/spec/" + def.id, title: "정의 파일과 DB 주석에서 생성한 화면정의서" }, "화면정의서")),
      h("p", { class: "purpose", text: def.purpose }),
      conds,
      h("div", { class: "grids" }, def.layout === "split" ? splitView() : panels),
      status));
    fitSplit();
    window.addEventListener("resize", fitSplit);
    fitLast();
    window.addEventListener("resize", fitLast);
    if (stacked() && "ResizeObserver" in window) {   // cards above it fill in, a chart lands over the list, a condition wraps
      const moved = new ResizeObserver(() => fitLast());   // the same top gives the same height, so it settles at once
      app.querySelectorAll(".screen > .purpose, .screen > .conds, .grids > .panel").forEach(el => moved.observe(el));
    }

    document.addEventListener("keydown", e => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); search(); }
    });

    const opening = def.grids.filter(g => g.load === "open" || g.load === "input");
    opening.forEach(g => grids[g.id].skeleton());
    const hasDrill = def.conditions.some(c => urlParams.has(c.name));
    def.grids.filter(g => g.load === "search").forEach(g => grids[g.id].clear("조회조건을 정하고 조회를 누르세요"));
    def.grids.filter(g => g.load === "master").forEach(g => grids[g.id].clear("목록에서 행을 고르세요"));
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
        // a live list takes its open details along, so the right side never shows an older state than the left
        const follow = def.grids.filter(g => g.load === "master" && liveGrids.some(m => m.id === g.master));
        const list = [...liveGrids, ...follow].filter(g => grids[g.id].params);
        load(list, g => grids[g.id].params, { live: true }).finally(() => { pending = false; });
      });
      es.onopen = () => { dot.classList.remove("off"); dot.classList.add("on"); dot.title = "실시간 연결됨"; };
      es.onerror = () => { dot.classList.remove("on"); dot.classList.add("off"); dot.title = "실시간 연결 끊김 (자동 재연결)"; };
    }
  }

  // ---------- grid ----------
  function Grid(g, { onSelect, onDrill, leads, onAction, onEdit, lookups }) {
    let rows = [], view = [], sel = -1, sortCol = null, sortDir = 1;
    let changed = new Map();   // row key -> Set of changed columns ("*" = new row), shown once
    let previous = new Map();  // row key -> the row before this fill (numbers roll from here)
    let loaded = false;
    // a T계정 grid opens as 전표 (an entry's lines down the page, as a journal is written) and turns into T계정 or a
    // table; a grid with a 구성 지도 turns into a table and back (buttons next to the count)
    let mode = g.view === "taccount" ? "voucher" : g.view;
    const keyCol = g.key || g.columns[0].name;
    const rowKey = g.view === "cards" ? () => "card" : r => String(r[keyCol]);   // cards show one row
    const count = h("span", { class: "count" });
    // 목록 안 찾기: a long list (FILTER_FROM rows or more) gets a box that keeps the rows holding the typed text
    // in any column (the code under a name too). Sort, live updates and the map keep to it; Esc empties it.
    let q = "";
    const finder = h("input", { type: "search", class: "gfind", placeholder: "목록에서 찾기", "aria-label": `${g.title}에서 찾기`,
      hidden: true, oninput: () => refind(finder.value), onkeydown: e => { if (e.key === "Escape" && finder.value) { e.stopPropagation(); finder.value = ""; refind(""); } } });
    const metaBox = h("span", { class: "meta" });
    const aboutBox = h("span", { class: "about", title: "목록에서 고른 행" });
    const body = h("div", { class: "gbody" });
    const chartBox = g.chart ? h("div", { class: "chart" }) : null;
    const FILTER_FROM = 20;
    const MODES = { voucher: "전표", taccount: "T계정", map: "지도", grid: "표" };
    const offered = [...new Set([g.view === "taccount" ? "voucher" : null, g.view, g.map ? "map" : null, "grid"])].filter(m => m in MODES);
    const modes = offered.length > 1 ? offered.map(m =>
      h("button", { type: "button", "aria-pressed": m === mode ? "true" : "false", "data-m": m, onclick: () => {
        if (m === mode) return;
        mode = m;
        modes.forEach(b => b.setAttribute("aria-pressed", b.dataset.m === m ? "true" : "false"));
        if (loaded) { morph(body, apply); mark(false); }
      } }, MODES[m])) : null;
    // 처리 버튼 live at the top right of their grid.
    const acts = (g.actions || []).map(b => ({ b, btn: h("button", { type: "button", class: "act",
      onclick: () => onAction(b, b.needs_row ? view[sel] : null) }, b.label) }));
    // 입력 그리드: one column is typed in here. Nothing is saved; the values go to the grids that work them out.
    const ed = g.edit || null;
    let edits = new Map();     // row key -> typed text
    let added = [];            // rows put in with 행 추가 (kept when the grid is read again)
    let editTotal = null;      // the footer cell of the typed column
    const editBtns = ed ? [
      ed.add && lookups && lookups[ed.add] ? h("button", { type: "button", class: "act", title: "찾기 팝업에서 골라 행을 더해요",
        onclick: () => openLookup(lookups[ed.add], {}, "", addRow) }, "＋ 추가") : null,
      ed.reset ? h("button", { type: "button", class: "act", title: "고친 값을 모두 처음 값으로 되돌려요", onclick: resetEdits }, ed.reset) : null,
    ].filter(Boolean) : [];
    const el = h("section", { class: "panel" + (g.view === "cards" ? " cards-panel" : ""), "aria-label": g.title },
      h("div", { class: "phead" }, h("h2", { text: g.title }), aboutBox,
        g.live ? h("span", { class: "livechip", title: "DB가 바뀌면 자동으로 다시 조회해요" }, "실시간") : null,
        metaBox, count, finder,
        modes ? h("div", { class: "seg", role: "group", "aria-label": "보기" }, modes) : null,
        h("span", { class: "txid", title: "조회 트랜잭션", text: g.tx }),
        acts.length || editBtns.length ? h("div", { class: "acts" }, editBtns, acts.map(a => a.btn)) : null),
      chartBox, body);

    // A button is off, and says why, until it has what it works on.
    function syncActions() {
      const row = view[sel];
      for (const { b, btn } of acts) {
        const unmet = Object.entries(b.when || {}).filter(([c, vals]) => !row || !vals.includes(row[c]));
        let why = "";
        if (!DATA.writable) why = "조회 전용으로 열려 있어요. 처리하려면 ui --write 로 다시 열어 주세요.";
        else if (g.load === "master" && !loaded) why = "목록에서 행을 먼저 고르세요";
        else if (b.needs_row && !row) why = "행을 먼저 고르세요";
        else if (unmet.length) why = unmet.map(([c, vals]) => `${g.columns.find(x => x.name === c).label}: ${vals.join("·")}인 행만 처리해요`).join(", ");
        btn.disabled = !!why;
        btn.title = why || `${b.title} (${b.tx})`;
      }
    }
    syncActions();

    // A detail grid names the master row it belongs to, next to its title.
    function about(text) { aboutBox.textContent = text || ""; }
    function clear(msg) {
      rows = []; view = []; sel = -1; loaded = false; count.textContent = ""; metaBox.replaceChildren(); about("");
      if (chartBox) chartBox.replaceChildren();
      morph(body, () => body.replaceChildren(h("div", { class: "empty", text: msg })));
      syncActions();
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
      syncActions();
    }

    function fill(ds, meta, live) {
      busy(false);
      if (!ds) return fail(`데이터셋 ${g.dataset}이 없어요`);
      const fresh = ds.rows.map(r => Object.fromEntries(ds.columns.map((c, i) => [c, r[i]])));
      if (ed) {   // rows the user added stay unless the list now has them itself
        const have = new Set(fresh.map(r => rowKey(r)));
        added = added.filter(r => !have.has(rowKey(r)));
        fresh.push(...added);
      }
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
      finder.hidden = !(rows.length >= FILTER_FROM && (g.view === "grid" || g.view === "taccount"));
      if (finder.hidden && q) { finder.value = ""; q = ""; }
      renderMeta(meta || {});
      if (same) apply(); else morph(body, apply);
      loaded = true;
      sel = keep ? view.findIndex(r => rowKey(r) === keep) : -1;
      if (keep) mark(false);
      // 기초 정보 → 상세: a grid with details under it opens with a row already picked
      // (the one the transaction names in meta.selected, else the first), so the details are never empty.
      if (!live && leads && view.length) {
        const named = meta && meta.selected != null ? view.findIndex(r => rowKey(r) === String(meta.selected)) : -1;
        sel = named >= 0 ? named : 0;
        mark(false);
        onSelect(view[sel]);
      }
      if (hadFocus) { const t = body.querySelector(".grid, button.day.sel"); if (t) t.focus({ preventScroll: true }); }
      changed = new Map();   // highlight once; a later sort does not flash again
      previous = new Map();
      if (chartBox) renderChart();
      syncActions();
    }

    function editedValue(r) {
      const k = rowKey(r);
      return edits.has(k) ? edits.get(k) : r[ed.column];
    }
    function isEdited(r) {
      const v = editedValue(r);
      return Number(v) !== Number(r[ed.base] ?? 0) || String(v).trim() === "";
    }
    // What the grids on the right get: every row, typed or not. Nothing until the rows are here.
    function editValue() {
      if (!ed || !loaded) return undefined;
      return rows.map(r => `${rowKey(r)}:${String(editedValue(r) ?? 0).trim()}`).join(",");
    }
    function editSum() {
      return rows.reduce((s, r) => s + (Number(editedValue(r)) || 0), 0);
    }
    function showEditTotal() {
      if (!editTotal) return;
      const sum = Math.round(editSum() * 100) / 100;
      editTotal.textContent = fmt(sum, "rate");
      editTotal.classList.toggle("bad", Math.abs(sum - 100) > 0.004);
      editTotal.title = Math.abs(sum - 100) > 0.004 ? "합계가 100이 아니면 계산하지 않아요" : "";
    }
    function shownEdit(v) {   // a number from the server with its two decimals; what was typed as typed
      return typeof v === "number" ? v.toFixed(2) : String(v ?? "");
    }
    function editCell(r) {
      const k = rowKey(r), base = r[ed.base] ?? 0;
      const input = h("input", { type: "text", inputmode: "decimal", class: "einput", autocomplete: "off",
        "aria-label": `${k} ${g.columns.find(c => c.name === ed.column).label}`, value: shownEdit(editedValue(r)) });
      const back = h("button", { type: "button", class: "erevert", title: "이 행만 처음 값으로 되돌려요",
        text: "↺ " + fmt(base, "rate") });
      const box = h("div", { class: "ebox" }, input, back);
      const sync = () => box.classList.toggle("edited", isEdited(r));
      input.addEventListener("input", () => {
        edits.set(k, input.value);
        sync(); showEditTotal(); onEdit();
      });
      input.addEventListener("keydown", e => {
        e.stopPropagation();   // typing here does not move the row or open another screen
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
      });
      back.addEventListener("click", e => {
        e.stopPropagation();
        edits.delete(k);
        input.value = shownEdit(r[ed.column]);
        sync(); showEditTotal(); onEdit();
      });
      sync();
      return box;
    }
    function resetEdits() {
      edits = new Map();
      if (added.length) {
        const gone = new Set(added.map(r => rowKey(r)));
        rows = rows.filter(r => !gone.has(rowKey(r)));
        added = [];
      }
      if (loaded) apply();
      onEdit();
    }
    function addRow(picked) {
      const k = String(picked[keyCol] ?? "");
      if (!k || rows.some(r => rowKey(r) === k)) { toast(`${k} 이미 바스켓에 있어요`); return; }
      const r = Object.fromEntries(g.columns.map(c => [c.name, picked[c.name] ?? null]));
      r[keyCol] = k; r[ed.base] = 0; r[ed.column] = 0;
      added.push(r); rows.push(r);
      if (loaded) apply();
      const inputs = body.querySelectorAll("input.einput");
      if (inputs.length) inputs[inputs.length - 1].focus();
      onEdit();
    }

    function renderMeta(meta) {
      metaBox.replaceChildren(...Object.entries(g.meta || {}).filter(([k]) => k in meta).map(([k, label]) => {
        const v = meta[k];
        const cls = typeof v === "boolean" ? (v ? "badge ok" : "badge bad") : "badge";
        const text = typeof v === "boolean" ? (v ? "예" : "아니오") : typeof v === "string" ? v : fmt(v, "number");
        return h("span", { class: cls }, label + " ", h("b", { text }));
      }));
    }

    function apply() {
      view = q ? rows.filter(hit) : rows.slice();
      if (sortCol) {
        const c = g.columns.find(c => c.name === sortCol);
        const num = NUMERIC.has(c.format);
        view.sort((a, b) => {
          const x = a[sortCol], y = b[sortCol];
          if (x == null) return 1; if (y == null) return -1;
          return (num ? x - y : String(x).localeCompare(String(y), "ko")) * sortDir;
        });
      }
      count.textContent = g.view === "grid" || g.view === "taccount"
        ? (q ? `${nf.format(view.length)} / ${nf.format(rows.length)}건` : `${nf.format(rows.length)}건`) : "";
      mode === "cards" ? renderCards() : mode === "calendar" ? renderCalendar() : mode === "taccount" ? renderTAccounts() : mode === "voucher" ? renderVouchers()
        : mode === "map" ? renderMap() : renderTable();
    }

    // 구성 지도: a tile a row. The area is the row's share of the size column, the colour is the tone
    // column against the size (+ red, - blue, deeper the further from zero; no value = hatched).
    // Tiles are the rows: a click picks the row, a double click or Enter moves on like a table row.
    const MAP_TILES = 40, MAP_MIN = 0.005;   // at most 40 tiles, and none under 0.5% of the whole
    const mapWatch = window.ResizeObserver ? new ResizeObserver(() => placeTiles()) : null;
    function squarify(items, x, y, w, hgt) {          // items: [{ v }] largest first; gives each x, y, w, h in px
      const total = items.reduce((a, it) => a + it.v, 0);
      let rest = items.map(it => ({ it, a: it.v / total * w * hgt }));
      while (rest.length && w > 0 && hgt > 0) {
        const short = Math.min(w, hgt);
        const worst = (s, n) => Math.max(short * short * rest[0].a / (s * s), s * s / (short * short * rest[n - 1].a));
        let n = 1, sum = rest[0].a;
        while (n < rest.length && worst(sum + rest[n].a, n + 1) <= worst(sum, n)) { sum += rest[n].a; n++; }
        const thick = sum / short;
        let off = 0;
        for (const e of rest.slice(0, n)) {
          const len = e.a / thick;
          if (w >= hgt) Object.assign(e.it, { x, y: y + off, w: thick, h: len });
          else Object.assign(e.it, { x: x + off, y, w: len, h: thick });
          off += len;
        }
        if (w >= hgt) { x += thick; w -= thick; } else { y += thick; hgt -= thick; }
        rest = rest.slice(n);
      }
    }
    function placeTiles() {
      const box = body.querySelector(".tmap");
      if (!box || !box.clientWidth) return;
      const tiles = [...box.children].map(el => ({ el, v: Number(el.dataset.v) }));
      const rest = tiles.find(t => t.el.classList.contains("rest"));   // 나머지 runs down the right edge, its width its share
      const W = box.clientWidth, H = box.clientHeight;
      const rw = rest ? Math.round(W * rest.v / tiles.reduce((a, t) => a + t.v, 0)) : 0;
      if (rest) Object.assign(rest, { x: W - rw, y: 0, w: rw, h: H });
      squarify(tiles.filter(t => t !== rest), 0, 0, W - rw, H);
      for (const t of tiles) {
        t.el.style.left = t.x + "px"; t.el.style.top = t.y + "px";
        t.el.style.width = Math.max(0, t.w - 2) + "px"; t.el.style.height = Math.max(0, t.h - 2) + "px";
        t.el.classList.toggle("sm", t.w < 96 || t.h < 58);     // room for the name only
        t.el.classList.toggle("xs", t.w < 44 || t.h < 24);     // room for nothing: the hover text says it
      }
    }
    function renderMap() {
      const m = g.map, col = n => g.columns.find(c => c.name === n);
      const size = col(m.size), toneCol = m.tone ? col(m.tone) : null;
      const all = view.map((r, i) => ({ r, i, v: Number(r[m.size]) || 0 })).filter(x => x.v > 0).sort((a, b) => b.v - a.v);
      if (!all.length) return body.replaceChildren(h("div", { class: "empty", text: rows.length ? `${size.label}이 있는 행이 없어요` : "조회된 내역이 없어요" }));
      const total = all.reduce((a, x) => a + x.v, 0);
      // A hundred holdings make a tail of tiles too small for a name. The largest ones keep a tile each (up to
      // MAP_TILES, and only while a tile is at least MAP_MIN of the whole); the rest share one 나머지 tile.
      let cut = all.findIndex((x, k) => k >= MAP_TILES || x.v / total < MAP_MIN);
      if (cut < 0 || all.length - cut < 2) cut = all.length;
      const sized = all.slice(0, cut), tail = all.slice(cut);
      const box = h("div", { class: "tmap", role: "listbox", "aria-label": `${g.title} 구성 지도`, tabindex: "0",
        onkeydown: e => {
          const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
          const at = sized.findIndex(x => x.i === sel);
          if (step) { e.preventDefault(); const to = sized[Math.max(0, Math.min(sized.length - 1, at + step))]; if (to) select(to.i); }
          else if (e.key === "Enter" && view[sel]) { e.preventDefault(); onDrill(view[sel]); }
        } },
        sized.map(({ r, i, v }) => {
          const t = toneCol ? r[m.tone] : undefined;
          const known = t !== null && t !== undefined;
          const ratio = known ? Number(t) / v : 0;
          const share = pf.format(v / total);
          const lines = [`${r[m.label]}`, `${size.label} ${fmt(v, size.format)} (${share})`];
          if (toneCol) lines.push(known ? `${toneCol.label} ${Number(t) > 0 ? "+" : ""}${fmt(t, toneCol.format)} (${ratio > 0 ? "+" : ""}${pf.format(ratio)})` : `${toneCol.label} 없음`);
          const el = h("div", { class: ["mtile", !toneCol ? "" : !known ? "none" : ratio > 0 ? "up" : ratio < 0 ? "down" : ""].join(" ").trim(),
            role: "option", "aria-selected": "false", "data-i": i, "data-v": v, title: lines.join("\n"), "aria-label": lines.join(", "),
            onclick: () => select(i), ondblclick: () => onDrill(r) },
            h("b", { text: r[m.label] }), h("span", { class: "tshare", text: share }), h("span", { class: "tsize", text: fmt(v, size.format) }),
            toneCol ? h("span", { class: "ttone", text: known ? `${Number(t) > 0 ? "+" : ""}${fmt(t, toneCol.format)} (${ratio > 0 ? "+" : ""}${pf.format(ratio)})` : "평가 전" }) : null);
          el.style.setProperty("--k", (known ? 10 + 38 * Math.min(1, Math.abs(ratio) / 0.08) : 0).toFixed(1) + "%");   // full depth at 8%
          return el;
        }), tail.length ? restTile() : null);
      function restTile() {   // not a row: a double click shows them all in the table
        const v = tail.reduce((a, x) => a + x.v, 0), share = pf.format(v / total);
        const tones = toneCol ? tail.map(x => x.r[m.tone]) : [];
        const t = toneCol && tones.every(x => x !== null && x !== undefined) ? tones.reduce((a, x) => a + Number(x), 0) : null;
        const lines = [`나머지 ${nf.format(tail.length)}건`, `${size.label} ${fmt(v, size.format)} (${share})`];
        if (t !== null) lines.push(`${toneCol.label} ${t > 0 ? "+" : ""}${fmt(t, toneCol.format)}`);
        lines.push("두 번 누르면 표로 모두 보여요");
        return h("div", { class: "mtile rest", "data-v": v, title: lines.join("\n"), "aria-label": lines.join(", "),
          ondblclick: () => modes && modes.find(b => b.dataset.m === "grid")?.click() },
          h("b", { text: `나머지 ${nf.format(tail.length)}건` }), h("span", { class: "tshare", text: share }),
          h("span", { class: "tsize", text: fmt(v, size.format) }),
          t !== null ? h("span", { class: "ttone", text: `${t > 0 ? "+" : ""}${fmt(t, toneCol.format)}` }) : null);
      }
      body.replaceChildren(h("div", { class: "tmapbox" }, box,
        h("div", { class: "tlegend" }, h("span", { text: `면적 ${size.label}` }),
          toneCol ? [h("span", { text: `색 ${toneCol.label}` }), h("span", { class: "lg up", text: "이익" }), h("span", { class: "lg down", text: "손실" }),
            all.some(x => x.r[m.tone] === null || x.r[m.tone] === undefined) ? h("span", { class: "lg none", text: "값 없음" }) : null] : null,
          all.length < rows.length ? h("span", { text: `${size.label}이 없는 ${rows.length - all.length}건은 그리지 않았어요` }) : null,
          tail.length ? h("span", { text: `작은 ${nf.format(tail.length)}건은 "나머지" 한 칸으로 묶었어요` }) : null)));
      if (mapWatch) { mapWatch.disconnect(); mapWatch.observe(box); }
      placeTiles();
    }

    // T계정 보기: a journal the way it is taught. A T an account, 차변 on the left and 대변 on the right,
    // a block a 전표; under each block both sums and whether they agree. The columns that are not the
    // account or an amount are the block's heading. The amounts are the rows': nothing is worked out but the sums.
    function renderTAccounts() {
      if (!rows.length) return body.replaceChildren(h("div", { class: "empty", text: "조회된 내역이 없어요" }));
      const t = g.taccount, col = n => g.columns.find(c => c.name === n);
      const facts = g.columns.filter(c => ![t.account, t.name, t.debit, t.credit].includes(c.name));
      const blocks = new Map();
      for (const r of view) {
        const k = t.entry ? String(r[t.entry]) : "";
        if (!blocks.has(k)) blocks.set(k, []);
        blocks.get(k).push(r);
      }
      const amount = v => fmt(v, "money");
      const side = (label, values) => h("div", { class: "tside" }, h("div", { class: "tl", text: label }),
        values.map(v => h("div", { class: "tv", text: amount(v) })));
      body.replaceChildren(h("div", { class: "tview" }, [...blocks.values()].map(lines => {
        const accounts = new Map();
        for (const r of lines) {
          const k = String(r[t.account]);
          if (!accounts.has(k)) accounts.set(k, { name: t.name ? r[t.name] : null, dr: [], cr: [] });
          if (Number(r[t.debit])) accounts.get(k).dr.push(r[t.debit]);
          if (Number(r[t.credit])) accounts.get(k).cr.push(r[t.credit]);
        }
        const dr = lines.reduce((a, r) => a + (Number(r[t.debit]) || 0), 0);
        const cr = lines.reduce((a, r) => a + (Number(r[t.credit]) || 0), 0);
        const shown = facts.filter(c => lines[0][c.name] !== null && lines[0][c.name] !== undefined && lines[0][c.name] !== "");
        return h("section", { class: "tentry", "aria-label": t.entry ? `${col(t.entry).label} ${lines[0][t.entry]}` : g.title },
          shown.length ? h("div", { class: "tfacts" }, shown.map(c => h("span", {}, c.label, h("b", { text: codeName(c, lines[0][c.name]) ?? fmt(lines[0][c.name], c.format) })))) : null,
          h("div", { class: "taccs" }, [...accounts].map(([code, a]) => h("div", { class: "tacc", role: "group",
            "aria-label": `${a.name || code} 차변 ${a.dr.map(amount).join(", ") || "없음"}, 대변 ${a.cr.map(amount).join(", ") || "없음"}` },
            h("h3", {}, a.name || code, a.name ? h("span", { text: code }) : null),
            h("div", { class: "tsides" }, side("차변", a.dr), side("대변", a.cr))))),
          h("div", { class: "tsum" },
            h("span", {}, "차변 합계", h("b", { text: amount(dr) })), h("span", {}, "대변 합계", h("b", { text: amount(cr) })),
            h("span", { class: "pill " + (dr === cr ? "st-ok" : "st-bad"), text: dr === cr ? "차대 일치" : `차대 불일치 ${amount(dr - cr)}` })));
      })));
    }

    // 전표 보기: the way a 전표 is printed in an ERP. One table for the whole event: 차변계정 · 차변과목 · 차변금액 next to
    // 대변계정 · 대변과목 · 대변금액, the k-th debit line beside the k-th credit line, a dotted line between 전표.
    // The 전표's own facts (번호, 회계일, 유형, 원천) sit in a column of their own on the left, over all its lines,
    // so they are not read as one more line; the foot row carries both sums.
    function renderVouchers() {
      if (!rows.length) return body.replaceChildren(h("div", { class: "empty", text: "조회된 내역이 없어요" }));
      const t = g.taccount;
      const facts = g.columns.filter(c => ![t.entry, t.account, t.name, t.debit, t.credit].includes(c.name));
      const entryCol = g.columns.find(c => c.name === t.entry);
      const blocks = new Map();
      for (const r of view) {
        const k = t.entry ? String(r[t.entry]) : "";
        if (!blocks.has(k)) blocks.set(k, []);
        blocks.get(k).push(r);
      }
      const money = v => fmt(v, "money");
      const sum = (rs, c) => rs.reduce((a, r) => a + (Number(r[c]) || 0), 0);
      const side = (r, c) => {           // the credit half starts with a rule on its left
        const edge = c === t.credit ? " vsplit" : "";
        return r ? [h("td", { class: "vcode" + edge, text: r[t.account] }), h("td", { class: "vname", text: t.name ? r[t.name] : "" }),
          h("td", { class: "num", text: money(r[c]) })] : [h("td", { class: edge.trim() || null }), h("td", {}), h("td", {})];
      };
      let DR = 0, CR = 0;
      const groups = [...blocks.values()].map(lines => {
        const drs = lines.filter(r => Number(r[t.debit])), crs = lines.filter(r => Number(r[t.credit]));
        const dr = sum(lines, t.debit), cr = sum(lines, t.credit);
        DR += dr; CR += cr;
        const first = lines[0];
        const shown = facts.filter(c => first[c.name] !== null && first[c.name] !== undefined && first[c.name] !== "");
        const n = Math.max(drs.length, crs.length, 1);
        const card = h("td", { class: "vfacts", rowspan: String(n) },
          entryCol ? h("div", { class: "vno" }, entryCol.label, h("b", { text: fmt(first[t.entry], entryCol.format) })) : null,
          h("dl", {}, shown.flatMap(c => [h("dt", { text: c.label }), h("dd", { text: fmt(first[c.name], c.format) })])),
          dr === cr ? null : h("span", { class: "pill st-bad", text: `차대 불일치 ${money(dr - cr)}` }));
        return h("tbody", {}, Array.from({ length: n }, (_, i) =>
          h("tr", {}, i ? null : card, side(drs[i], t.debit), side(crs[i], t.credit))));
      });
      body.replaceChildren(h("div", { class: "vwrap" }, h("table", { class: "vtable" },
        h("thead", {}, h("tr", {}, ["전표", "차변계정", "차변과목", "차변금액", "대변계정", "대변과목", "대변금액"].map((x, i) =>
          h("th", { class: i === 0 ? "vfacts" : i % 3 === 0 ? "num" : i === 4 ? "vsplit" : null, text: x })))),
        groups,
        h("tfoot", {}, h("tr", {}, h("td", { colspan: "3" }, "합계", h("span", { class: "pill " + (DR === CR ? "st-ok" : "st-bad"),
          text: DR === CR ? "차대 일치" : `차대 불일치 ${money(DR - CR)}` })), h("td", { class: "num", text: money(DR) }),
          h("td", { colspan: "2", class: "vsplit" }), h("td", { class: "num", text: money(CR) }))))));
    }

    function flashed(r, col) {
      const set = changed.get(rowKey(r));
      return !!set && (set.has("*") || set.has(col));
    }

    function renderCards() {
      const r = rows[0];
      if (!r) return body.replaceChildren(h("div", { class: "empty", text: "조회된 내역이 없어요" }));
      const old = previous.get("card");
      const values = g.columns.map(c => { const named = codeName(c, r[c.name]);
        return h("div", { class: ["cv", c.format === "datetime" ? "dt" : "", tone(r[c.name], c.color, r)].filter(Boolean).join(" "),
          // a card has room for the minute, not the second: the full time is in the tooltip
          text: named ?? ((c.format === "datetime" ? fmt(r[c.name], c.format).slice(0, 16) : fmt(r[c.name], c.format)) || "-"),
          title: named ? String(r[c.name]) : (fmt(r[c.name], c.format) || null) }); });
      body.replaceChildren(h("div", { class: "cards" }, g.columns.map((c, i) =>
        h("div", { class: values[i].textContent.length > 18 ? "card long" : "card" }, h("div", { class: "cl", text: c.label }), values[i]))));
      if (old) g.columns.forEach((c, i) => {
        if (NUMERIC.has(c.format)) roll(values[i], old[c.name], r[c.name], c.format);
        else if (old[c.name] !== r[c.name]) anim(values[i], [{ opacity: 0, transform: "translateY(3px)" }, { opacity: 1, transform: "none" }], 180);
      });
    }

    // 달력 보기: a cell a day, coloured by the state column's colour rule, a block a month.
    // Weekends are drawn only when a row falls on one; a day without a row is a faint number.
    // The other columns are the cell's hover text; picking a cell picks the row, as in a table.
    const iso = v => String(v ?? "").slice(0, 10);
    const dow = d => (new Date(d + "T00:00:00Z").getUTCDay() + 6) % 7;   // Monday = 0
    const dayIndex = () => new Map(view.map((r, i) => [iso(r[g.calendar.date]), i]));
    function renderCalendar() {
      if (!rows.length) return body.replaceChildren(h("div", { class: "empty", text: "조회된 내역이 없어요" }));
      const sc = g.columns.find(c => c.name === g.calendar.state);
      const rest = g.columns.filter(c => c !== sc && c.name !== g.calendar.date);
      const at = dayIndex();
      const span = [...at.keys()].some(d => dow(d) > 4) ? 7 : 5;
      const dayCell = (d, n, i) => {
        const r = view[i];
        const lines = [`${d} (${WEEK[dow(d)]}) ${fmt(r[sc.name], sc.format)}`, ...rest
          .filter(c => r[c.name] !== null && r[c.name] !== undefined && r[c.name] !== "")
          .map(c => `${c.label} ${codeName(c, r[c.name]) ?? fmt(r[c.name], c.format)}`)];
        return h("button", { type: "button", "data-i": i, "data-k": rowKey(r), tabindex: "-1", "aria-pressed": "false",
          class: ["day", tone(r[sc.name], sc.color, r), d === DATA.today ? "today" : "", flashed(r, sc.name) ? "flash" : ""].filter(Boolean).join(" "),
          title: lines.join("\n"), "aria-label": lines.join(", "), onclick: () => select(i) }, String(n));
      };
      const months = [...new Set([...at.keys()].map(d => d.slice(0, 7)))].sort().map(m => {
        const [y, mo] = m.split("-").map(Number);
        const lead = dow(m + "-01"), cells = [];
        for (let n = 1, last = new Date(Date.UTC(y, mo, 0)).getUTCDate(); n <= last; n++) {
          const d = `${m}-${String(n).padStart(2, "0")}`, col = dow(d);
          if (col >= span) continue;
          const i = at.get(d);
          const cell = i === undefined ? h("span", { class: "day off", "aria-hidden": "true", text: n }) : dayCell(d, n, i);
          cells.push([cell, col, Math.floor((n - 1 + lead) / 7)]);
        }
        const top = Math.min(...cells.map(c => c[2]));   // a month that starts on a weekend has no first row
        for (const [cell, col, week] of cells) { cell.style.gridColumnStart = col + 1; cell.style.gridRowStart = week - top + 2; }
        return h("section", { class: "month", "aria-label": `${y}년 ${mo}월` }, h("h3", { text: `${y}년 ${mo}월` }),
          h("div", { class: "mgrid" + (span === 7 ? " w7" : "") },
            WEEK.slice(0, span).map(w => h("span", { class: "wd", "aria-hidden": "true", text: w })), cells.map(c => c[0])));
      });
      // The legend is made of what is on screen: each state once, with how many days it has.
      const seen = new Map();
      for (const r of view) {
        const v = fmt(r[sc.name], sc.format), e = seen.get(v) || { cls: tone(r[sc.name], sc.color, r), n: 0 };
        e.n += 1; seen.set(v, e);
      }
      const order = ["st-ok", "st-warn", "st-bad", "st-miss", ""];
      const legend = h("div", { class: "legend" }, [...seen].sort((a, b) => order.indexOf(a[1].cls) - order.indexOf(b[1].cls))
        .map(([v, e]) => h("span", {}, h("i", { class: ("day " + e.cls).trim(), "aria-hidden": "true" }), `${v} ${nf.format(e.n)}일`)));
      body.replaceChildren(h("div", { class: "cal", role: "group", "aria-label": g.title, onkeydown: calKey }, legend, months));
    }
    // ←/→ a day, ↑/↓ a week; days without a row (weekends, days out of range) are stepped over.
    function calKey(e) {
      const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key];
      if (!step || sel < 0) return;
      e.preventDefault();
      const at = dayIndex(), t = new Date(iso(view[sel][g.calendar.date]) + "T00:00:00Z");
      for (let tries = 0; tries < 14; tries++) {
        t.setUTCDate(t.getUTCDate() + step);
        const i = at.get(t.toISOString().slice(0, 10));
        if (i !== undefined) return select(i);
      }
    }

    function renderTable() {
      if (!rows.length) return body.replaceChildren(h("div", { class: "empty", text: "조회된 내역이 없어요" }));
      const cols = g.columns.filter(c => !c.inside && !c.hidden);   // drawn inside another cell, or only there to name the row
      // 셀 안 막대, 비중 막대: each bar is the value against the largest one in its column
      const scaled = c => c.display === "bar" || (c.display === "meter" && !c.of);
      const peak = Object.fromEntries(cols.filter(scaled).map(c =>
        [c.name, Math.max(0, ...rows.map(r => Math.abs(Number(r[c.name]) || 0)))]));
      const edges = new Set();   // 묶음 머리: a line down the table where each band after the first starts
      g.bands.reduce((at, b) => { if (at) edges.add(at); return at + b.span; }, 0);
      const head = h("tr", {}, h("th", { class: "rn", scope: "col", text: "#" }), cols.map((c, ci) => {
        const on = sortCol === c.name;
        const th = h("th", { scope: "col", class: [c.align === "right" ? "r" : "", edges.has(ci) ? "edge" : ""].join(" ").trim() || null,
          "aria-sort": on ? (sortDir > 0 ? "ascending" : "descending") : null,
          title: (c.source || "화면 계산 값") + (c.display === "meter" && !c.of && peak[c.name] ? ` · 막대 끝은 이 목록에서 가장 큰 값(${pf.format(peak[c.name])})` : "") },
          c.display === "spark" ? c.label   // a run of numbers has no order to sort by
            : c.display === "steps" ? h("div", { class: "steps head" }, c.steps.map(n => h("span", { text: n })))   // each name over its dot
            : g.bands.length ? c.label   // a banded table (시산표) is read in its own order
            : h("button", { type: "button", onclick: () => sortBy(c.name) }, c.label, on ? (sortDir > 0 ? " ▲" : " ▼") : ""));
        if (c.width) th.style.width = c.width + "px";
        return th;
      }));
      const rolls = [];
      const tbody = h("tbody", {}, view.map((r, i) => h("tr", {
        "data-k": rowKey(r),
        class: [i === sel ? "sel" : "", changed.get(rowKey(r))?.has("*") ? "new" : "",
          g.emphasis && LEVELS.has(r[g.emphasis]) ? "lv-" + r[g.emphasis] : ""].join(" ").trim() || null,
        "aria-selected": i === sel ? "true" : "false",
        onclick: () => select(i), ondblclick: () => onDrill(view[i]),
      }, h("td", { class: "rn", text: i + 1 }), cols.map((c, ci) => {
        if (ed && c.name === ed.column) return h("td", { class: "r ecell" }, editCell(r));
        const v = r[c.name];
        const named = codeName(c, v);
        const text = named ?? fmt(v, c.format);
        const drawn = c.display && c.display !== "bar";   // the cell holds an element, not just text
        const td = h("td", { class: [c.align === "right" ? "r" : "", drawn ? "" : tone(v, c.color, r), c.format === "json" ? "js" : "",
          c.display ? "d-" + c.display : "", flashed(r, c.name) ? "flash" : "", edges.has(ci) ? "edge" : ""].filter(Boolean).join(" ") || null,
          title: c.format === "json" ? text : named !== null ? String(v) : null, text: drawn ? null : text });
        if (c.sub) {   // a second, smaller line under the value: its time, its rate
          const sc = g.columns.find(x => x.name === c.sub), sv = fmt(r[c.sub], sc ? sc.format : "text");
          if (sv) td.append(h("span", { class: "csub", text: sv }));
        }
        if (c.display === "badge") td.append(badge(r, c, g.columns));
        else if (c.display === "meter") td.append(c.of ? meter(v, r[c.of]) : meter(v, undefined, peak[c.name]));
        else if (c.display === "steps") td.append(steps(v, c.steps));
        else if (c.display === "spark") td.append(spark(v));
        else if (c.display === "pill" && text) td.append(h("span", { class: ("pill " + tone(v, c.color, r)).trim(), text }));
        else if (c.display === "bar" && peak[c.name]) td.style.setProperty("--p", (Math.abs(Number(v) || 0) / peak[c.name] * 100).toFixed(1) + "%");
        if (NUMERIC.has(c.format) && !drawn && !c.sub && flashed(r, c.name)) {
          const was = previous.get(rowKey(r));
          if (was) rolls.push([td, was[c.name], v, c.format]);
        }
        return td;
      }))));
      editTotal = null;
      const foot = g.totals.length ? h("tfoot", {}, h("tr", {}, h("td", { class: "rn", text: q ? "찾은 행 합계" : "합계" }), cols.map(c =>
        ed && c.name === ed.column ? (editTotal = h("td", { class: "r" }))
        : g.totals.includes(c.name)
          ? (sum => h("td", { class: ("r " + (c.color === "sign" ? tone(sum, "sign") : "")).trim(), text: fmt(sum, c.format) }))(
              (q ? view : rows).reduce((s, r) => s + (Number(r[c.name]) || 0), 0))
          : h("td", {})))) : null;
      const table = h("table", { tabindex: "0", role: "grid", "aria-rowcount": rows.length,
        onkeydown: key, class: g.bands.length ? "grid banded" : "grid" },
        h("thead", {}, g.bands.length ? h("tr", { class: "band" }, h("th", { class: "rn", "aria-hidden": "true" }),
          g.bands.map(b => h("th", { scope: "colgroup", colspan: b.span, text: b.label }))) : null, head), tbody, foot);
      body.replaceChildren(table, h("div", { class: "selbar", "aria-hidden": "true" }));
      showEditTotal();
      rolls.forEach(a => roll(...a));
      if (g.drill) el.classList.add("drillable");
      placeBar(true);
    }

    function hit(r) {
      return g.columns.some(c => { const v = r[c.name]; return v != null && (String(v).toLowerCase().includes(q) || fmt(v, c.format).toLowerCase().includes(q)); });
    }
    function refind(text) {
      q = text.trim().toLowerCase();
      const keep = view[sel];
      apply(); sel = keep ? view.indexOf(keep) : -1; mark(false);
    }
    function sortBy(name) {
      if (sortCol === name) sortDir = -sortDir; else { sortCol = name; sortDir = 1; }
      const keep = view[sel];
      flip(body, () => { apply(); sel = keep ? view.indexOf(keep) : -1; mark(false); });   // the bar slides with its row
    }
    function mark(scroll = true) {
      if (mode === "map") {
        body.querySelectorAll(".mtile").forEach(el => {
          const hit = Number(el.dataset.i) === sel;
          el.classList.toggle("sel", hit); el.setAttribute("aria-selected", hit ? "true" : "false");
        });
        return syncActions();
      }
      if (g.view === "calendar") {
        let on = null;
        body.querySelectorAll("button.day").forEach(b => {
          const hit = Number(b.dataset.i) === sel;
          b.classList.toggle("sel", hit); b.setAttribute("aria-pressed", hit ? "true" : "false"); b.tabIndex = hit ? 0 : -1;
          if (hit) on = b;
        });
        if (on && scroll) on.scrollIntoView({ block: "nearest" });
        else if (on && (on.offsetTop < body.scrollTop || on.offsetTop + on.offsetHeight > body.scrollTop + body.clientHeight)) {
          body.scrollTop = on.offsetTop - body.clientHeight / 2;   // opened on a long range: bring the picked day into view
        }
        if (on && body.contains(document.activeElement)) on.focus({ preventScroll: true });
        return syncActions();
      }
      body.querySelectorAll("tbody tr").forEach((tr, i) => {
        tr.classList.toggle("sel", i === sel); tr.setAttribute("aria-selected", i === sel ? "true" : "false");
      });
      const tr = body.querySelectorAll("tbody tr")[sel];
      if (tr && scroll) tr.scrollIntoView({ block: "nearest" });
      placeBar(false);
      syncActions();
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
      const money = g.columns.find(c => c.name === y).format === "money";   // won has no decimals
      const yf = v => money ? fmt(Math.round(v), "money") : rf.format(v);
      const pts = rows.filter(r => r[y] != null).map(r => [String(r[x]), Number(r[y])]).sort((a, b) => a[0] < b[0] ? -1 : 1);
      if (pts.length < 2) return chartBox.replaceChildren();
      const W = Math.max(320, Math.round(chartBox.clientWidth - 16)), H = 160, L = 64, R = 12, T = 12, B = 24;
      const ys = pts.map(p => p[1]);
      let lo = Math.min(...ys), hi = Math.max(...ys);
      if (hi === lo) { hi += 1; lo -= 1; }
      const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
      // Axis ticks in 억/만 so ten-digit won fit the margin, with just enough decimals to tell ticks apart.
      const [u, uName] = !money ? [1, ""] : Math.max(Math.abs(hi), Math.abs(lo)) >= 1e8 ? [1e8, "억"]
        : Math.max(Math.abs(hi), Math.abs(lo)) >= 1e4 ? [1e4, "만"] : [1, ""];
      const dec = Math.min(4, Math.max(0, Math.ceil(-Math.log10((hi - lo) / 3 / u)) + 1));
      const af = v => u === 1 ? yf(v) : (v / u).toLocaleString("ko-KR", { minimumFractionDigits: dec, maximumFractionDigits: dec }) + uName;
      const px = i => L + (W - L - R) * i / (pts.length - 1);
      const py = v => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
      const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "line", role: "img",
        "aria-label": `${g.columns.find(c => c.name === y).label} 추이` });
      for (let k = 0; k <= 3; k++) {
        const v = lo + (hi - lo) * k / 3;
        s.append(svg("line", { x1: L, x2: W - R, y1: py(v), y2: py(v), class: "gl" }));
        const t = svg("text", { x: L - 8, y: py(v) + 4, class: "ax", "text-anchor": "end" }); t.textContent = af(v); s.append(t);
      }
      [0, Math.floor((pts.length - 1) / 2), pts.length - 1].forEach(i => {
        const t = svg("text", { x: px(i), y: H - 6, class: "ax", "text-anchor": i === 0 ? "start" : i === pts.length - 1 ? "end" : "middle" });
        t.textContent = pts[i][0].slice(0, 10); s.append(t);
      });
      s.append(svg("path", { d: pts.map((p, i) => `${i ? "L" : "M"}${px(i).toFixed(1)},${py(p[1]).toFixed(1)}`).join(""), class: "ln" }));
      const last = pts[pts.length - 1];
      s.append(svg("circle", { cx: px(pts.length - 1), cy: py(last[1]), r: 3.5, class: "pt" }));
      const lt = svg("text", { x: px(pts.length - 1) - 6, y: py(last[1]) - 8, class: "lv", "text-anchor": "end" });
      lt.textContent = yf(last[1]); s.append(lt);
      chartBox.replaceChildren(s);
    }

    return { el, fill, clear, busy, fail, skeleton, about, editValue: () => (ed ? editValue() : undefined) };
  }

  // The tree starts shut where the viewer shut it, and on a narrow window (it would push the screen down).
  const narrow = window.matchMedia && window.matchMedia("(max-width: 900px)").matches;
  if ((prefs.get("nav") || (narrow ? "off" : "on")) === "off") document.body.dataset.nav = "off";
  if (DATA.screen) Screen(DATA.screen); else home();
  try {
    const said = sessionStorage.getItem(TOAST_KEY);
    if (said) { sessionStorage.removeItem(TOAST_KEY); toast(said); }
  } catch (e) { /* private mode */ }
  revealLeaf();
})();
