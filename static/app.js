/* ============================================================
   Gretta CRM — app.js
   Vanilla JS SPA · polls the FastAPI backend · zero build step
   ============================================================ */
"use strict";

/* ---------- constants ---------- */
const STAGES = ["New", "Contacted", "Meeting Booked", "Converted", "Lost"];
const SCORES = ["HIGH", "MEDIUM", "LOW", "UNKNOWN"];
const SDOT = { "New": "#94a3b8", "Contacted": "#38bdf8", "Meeting Booked": "#a78bfa", "Converted": "#34d399", "Lost": "#f87171" };
const SCOL = { HIGH: "#34d399", MEDIUM: "#fbbf24", LOW: "#f87171", UNKNOWN: "#64748b" };
const SKEY = { "New": "New", "Contacted": "Contacted", "Meeting Booked": "MB", "Converted": "Converted", "Lost": "Lost" };

/* ---------- state ---------- */
let LEADS = [], STATS = {}, OWNERS = [];
let FILTER = { stage: "", score: "", owner: "", q: "" };
let SORT = { key: "updated", dir: -1 };
let VIEW = "overview";
let TIMER = null, PAUSED = false, LAST_ERR_TOAST = 0;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- api ---------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

/* ---------- small ui helpers ---------- */
function toast(msg, kind = "ok") {
  const box = document.createElement("div");
  box.className = `toast ${kind}`;
  box.innerHTML = `<span>${kind === "ok" ? "✅" : "⚠️"}</span><span>${esc(msg)}</span>`;
  $("toasts").appendChild(box);
  setTimeout(() => { box.classList.add("out"); setTimeout(() => box.remove(), 260); }, 3000);
}

function initials(name) {
  const s = String(name || "?").replace(/[@^_\-.0-9]/g, "").trim();
  return (s ? s.slice(0, 2) : "?").toUpperCase();
}
function avColor(name) {
  let h = 0;
  for (const ch of String(name || "")) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `hsl(${h}, 62%, 62%)`;
}
function ts(row) { return Date.parse((row.updated || "").replace(" ", "T") + "Z") || 0; }
function timeAgo(s) {
  const d = Date.now() - ts({ updated: s });
  if (!(d > 0)) return "—";
  const m = Math.floor(d / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const days = Math.floor(h / 24);
  if (days < 30) return `${days}d ago`;
  return `${Math.floor(days / 30)}mo ago`;
}
function animateNum(el, to) {
  if (!el) return;
  const t0 = performance.now(), dur = 650;
  const step = (t) => {
    const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(to * e);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* ---------- chips & pills ---------- */
function scoreChip(sc) { return `<span class="chip score-${esc(sc)}">${esc(sc)}</span>`; }
function stageChip(st) { return `<span class="chip stage-${SKEY[st] || "New"}">${esc(st)}</span>`; }
function ownerPill(owner) {
  if (!owner) return `<span class="unclaimed-pill">◌ Unclaimed</span>`;
  return `<span class="owner-pill"><span class="avatar" style="background:${avColor(owner)}">${esc(initials(owner))}</span>${esc(owner)}</span>`;
}
function cardAccent(score) { return score === "HIGH" ? "hot" : score === "MEDIUM" ? "warm" : "cold"; }

/* ---------- filtering / sorting ---------- */
function applyFilters(rows) {
  const q = FILTER.q.trim().toLowerCase();
  return rows.filter(r =>
    (!FILTER.stage || r.status === FILTER.stage) &&
    (!FILTER.score || r.score === FILTER.score) &&
    (!FILTER.owner || (r.claimed_by || "") === FILTER.owner) &&
    (!q || (r.username + " " + (r.claimed_by || "") + " " + r.summary + " " + r.next_steps).toLowerCase().includes(q))
  );
}
function sortRows(rows) {
  const k = SORT.key;
  return [...rows].sort((a, b) => {
    let va, vb;
    if (k === "updated") { va = ts(a); vb = ts(b); }
    else if (k === "username" || k === "claimed_by" || k === "platform") { va = a[k] || ""; vb = b[k] || ""; }
    else { va = a[k] || ""; vb = b[k] || ""; }
    if (k === "score") { va = SCORES.indexOf(a.score); vb = SCORES.indexOf(b.score); }
    if (typeof va === "string") return va.localeCompare(vb) * SORT.dir;
    return (va - vb) * SORT.dir;
  });
}
function collectOwners() {
  OWNERS = [...new Set(LEADS.map(l => l.claimed_by).filter(Boolean))].sort();
  return OWNERS;
}
/* ============================================================
   RENDER — overview (KPIs + pipeline funnel)
   ============================================================ */
function renderOverview() {
  const el = $("view-overview");
  if (!LEADS.length) {
    el.innerHTML = `<div class="glass empty-wrap fade-in">
      <span class="empty-ico">🪄</span>
      <h3 style="margin:0 0 6px;font-size:17px;">No leads yet</h3>
      <p style="color:var(--txt-3);font-size:13px;max-width:340px;margin:0;">
        Send a screenshot to <b>@GrittaAi_bot</b> in Telegram and watch leads appear here live.</p>
    </div>`;
    $("healthBar").style.width = "0%";
    $("healthPct").textContent = "0%";
    return;
  }

  const s = STATS;
  const kpi = (id, label, icon, tint, glow) =>
    `<div class="glass kpi" style="--kpi-tint:${tint};--kpi-glow:${glow};padding:16px 18px;">
       <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
         <span style="font-size:11px;font-weight:800;color:var(--txt-3);letter-spacing:.09em;text-transform:uppercase;">${label}</span>
         <span style="font-size:18px;">${icon}</span>
       </div>
       <div id="${id}" class="mono" style="font-size:29px;font-weight:700;line-height:1;">0</div>
     </div>`;

  el.innerHTML = `
  <div class="stagger" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:13px;margin-bottom:14px;">
    ${kpi("kTotal", "Total Leads", "👥", "rgba(52,211,153,.14)", "rgba(52,211,153,.35)")}
    ${kpi("kHot", "Hot Leads", "🔥", "rgba(251,191,36,.14)", "rgba(251,191,36,.35)")}
    ${kpi("kConv", "Converted", "🏆", "rgba(167,139,250,.14)", "rgba(167,139,250,.35)")}
    ${kpi("kMeet", "Meetings Booked", "📅", "rgba(56,189,248,.14)", "rgba(56,189,248,.35)")}
    ${kpi("kUncl", "Unclaimed", "◌", "rgba(248,113,113,.13)", "rgba(248,113,113,.3)")}
  </div>

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px;">
    <!-- FUNNEL -->
    <div class="glass fade-in" style="padding:18px;min-width:0;">
      <div style="font-weight:750;font-size:15px;margin-bottom:4px;">Pipeline Funnel</div>
      <div style="font-size:11.5px;color:var(--txt-3);margin-bottom:14px;">Click a stage to filter everywhere · click again to clear</div>
      ${STAGES.map(st => {
        const counts = STAGES.map(x => LEADS.filter(l => l.status === x).length);
        const n = counts[STAGES.indexOf(st)];
        const w = Math.max(n ? (n / Math.max(...counts, 1)) * 100 : 2, 2);
        return `<div class="funnel-row ${FILTER.stage === st ? "sel" : ""}" data-fstage="${esc(st)}">
          <span style="font-size:12.5px;font-weight:650;color:${SDOT[st]};display:flex;align-items:center;gap:7px;">
            <span style="width:8px;height:8px;border-radius:99px;background:${SDOT[st]};"></span>${esc(st)}</span>
          <div class="funnel-track"><div class="funnel-fill" style="width:${w}%;background:linear-gradient(90deg,${SDOT[st]}55,${SDOT[st]});"></div></div>
          <b class="mono" style="text-align:right;color:var(--txt-2);">${n}</b>
        </div>`;
      }).join("")}
    </div>

    <!-- CHARTS -->
    <div style="display:flex;flex-direction:column;gap:14px;min-width:0;">
      <div class="glass fade-in" style="padding:18px;">
        <div style="font-weight:750;font-size:15px;margin-bottom:11px;">Intent Score Mix</div>
        <div class="chart-box" style="height:172px;"><canvas id="donutCv"></canvas></div>
      </div>
      <div class="glass fade-in" style="padding:18px;">
        <div style="font-weight:750;font-size:15px;margin-bottom:11px;">Lead Activity — last 14 days</div>
        <div class="chart-box" style="height:150px;"><canvas id="lineCv"></canvas></div>
      </div>
    </div>

    <!-- LEADERBOARD -->
    <div class="glass fade-in" style="padding:18px;">
      <div style="font-weight:750;font-size:15px;margin-bottom:4px;">Team Leaderboard</div>
      <div style="font-size:11.5px;color:var(--txt-3);margin-bottom:13px;">Ranked by closed-won, then total owned</div>
      <div id="lbBody"></div>
    </div>
  </div>`;

  animateNum($("kTotal"), s.total);
  animateNum($("kHot"), s.hot);
  animateNum($("kConv"), s.converted);
  animateNum($("kMeet"), s.meetings);
  animateNum($("kUncl"), s.unclaimed);

  const engaged = LEADS.filter(l => l.status !== "New").length;
  const pct = LEADS.length ? Math.round((engaged / LEADS.length) * 100) : 0;
  $("healthBar").style.width = pct + "%";
  $("healthPct").textContent = pct + "%";

  renderLeaderboard();
  drawCharts();
}
/* ============================================================
   RENDER — charts + team leaderboard
   ============================================================ */
let CHARTS = {};
function chartTheme() {
  const light = document.documentElement.dataset.theme === "light";
  return {
    border: light ? "#ffffff" : "#0a0f16",
    label: light ? "#45516b" : "#93a3b5",
    tick: light ? "#7c8aa5" : "#5d6c7e",
    grid: light ? "rgba(19,26,38,.08)" : "rgba(148,163,184,.06)",
  };
}
function drawCharts() {
  if (!window.Chart) return;
  const T = chartTheme();

  /* donut: intent score mix */
  const scoreCounts = SCORES.map(sc => LEADS.filter(l => l.score === sc).length);
  const dEl = $("donutCv");
  if (CHARTS.donut) { CHARTS.donut.destroy(); delete CHARTS.donut; }
  if (dEl) {
    if (!scoreCounts.some(n => n > 0)) {
      dEl.replaceWith(Object.assign(dEl.cloneNode(false), { outerHTML: `<div class="chart-empty">No scored leads yet</div>` }));
    } else {
      CHARTS.donut = new Chart(dEl.getContext("2d"), {
        type: "doughnut",
        data: {
          labels: ["High", "Medium", "Low", "Unknown"],
          datasets: [{ data: scoreCounts, backgroundColor: [SCOL.HIGH, SCOL.MEDIUM, SCOL.LOW, SCOL.UNKNOWN], borderColor: T.border, borderWidth: 3, hoverOffset: 8 }],
        },
        options: {
          maintainAspectRatio: false, cutout: "68%",
          plugins: { legend: { position: "right", labels: { color: T.label, boxWidth: 10, boxHeight: 10, borderRadius: 3, useBorderRadius: true, padding: 12, font: { family: "Inter", size: 11.5, weight: 600 } } } },
        },
      });
    }
  }

  /* line: last-14-day activity from summary timestamps */
  const days = [];
  for (let i = 13; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000);
    days.push({ key: d.toISOString().slice(0, 10), label: `${d.getDate()}/${d.getMonth() + 1}`, n: 0 });
  }
  for (const l of LEADS) for (const t of extractTimes(l.summary)) {
    const k = new Date(t).toISOString().slice(0, 10);
    const day = days.find(x => x.key === k);
    if (day) day.n++;
  }
  const lEl = $("lineCv");
  if (CHARTS.line) { CHARTS.line.destroy(); delete CHARTS.line; }
  if (lEl) {
    const grad = lEl.getContext("2d").createLinearGradient(0, 0, 0, 150);
    grad.addColorStop(0, "rgba(52,211,153,.32)");
    grad.addColorStop(1, "rgba(52,211,153,0)");
    CHARTS.line = new Chart(lEl.getContext("2d"), {
      type: "line",
      data: {
        labels: days.map(d => d.label),
        datasets: [{
          data: days.map(d => d.n), borderColor: "#34d399", borderWidth: 2.2,
          fill: true, backgroundColor: grad, tension: .38,
          pointRadius: 2.5, pointBackgroundColor: "#34d399",
        }],
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false }, ticks: { color: T.tick, font: { size: 10 } } },
          y: { beginAtZero: true, grid: { color: T.grid }, ticks: { color: T.tick, font: { size: 10 }, precision: 0 } },
        },
      },
    });
  }
}

function renderLeaderboard() {
  const byOwner = {};
  for (const l of LEADS) {
    if (!l.claimed_by) continue;
    byOwner[l.claimed_by] = byOwner[l.claimed_by] || { total: 0, won: 0, hot: 0 };
    const o = byOwner[l.claimed_by];
    o.total++;
    if (l.status === "Converted") o.won++;
    if (l.score === "HIGH") o.hot++;
  }
  const rows = Object.entries(byOwner).map(([name, v]) => ({ name, ...v }))
    .sort((a, b) => b.won - a.won || b.total - a.total);

  const box = $("lbBody");
  if (!rows.length) { box.innerHTML = `<div style="color:var(--txt-3);font-size:12.5px;padding:18px 0;text-align:center;">No claimed leads yet — claim one from the bot!</div>`; return; }

  const max = Math.max(...rows.map(r => r.total), 1);
  box.innerHTML = rows.map((r, i) => `
    <div class="funnel-row" data-fowner="${esc(r.name)}" style="grid-template-columns:34px 1fr 118px 44px;gap:10px;padding:7px 8px;">
      <span class="lb-rank r${i + 1}">${i + 1}</span>
      <div style="min-width:0;">
        <div style="font-weight:700;font-size:13px;display:flex;align-items:center;gap:8px;"><span class="avatar" style="background:${avColor(r.name)}">${esc(initials(r.name))}</span>${esc(r.name)}</div>
        <div class="bar-mini" style="margin-top:6px;"><div style="width:${Math.round((r.total / max) * 100)}%"></div></div>
      </div>
      <div style="text-align:right;font-size:11.5px;color:var(--txt-3);line-height:1.5;">
        <b style="color:#6ee7b7;">${r.won} won</b> · ${r.total} owned<br/><span style="color:#fcd34d;">${r.hot} hot</span>
      </div>
      <button class="mini-btn" data-fowner-btn="${esc(r.name)}" title="Filter by this owner">→</button>
    </div>`).join("");
}
/* ============================================================
   RENDER — kanban board (drag & drop)
   ============================================================ */
function renderBoard() {
  const el = $("view-board");
  const rows = applyFilters(LEADS);
  el.innerHTML = `<div class="board" style="height:100%;">` + STAGES.map(st => {
    const cards = sortRows(rows.filter(r => r.status === st));
    return `
    <div class="kanban-col glass" data-stage="${esc(st)}">
      <div class="col-head">
        <span class="col-dot" style="background:${SDOT[st]};box-shadow:0 0 12px ${SDOT[st]}66;"></span>
        <span style="font-weight:750;font-size:13px;color:${SDOT[st]};">${esc(st)}</span>
        <span class="col-count mono">${cards.length}</span>
      </div>
      <div class="col-cards" data-drop="${esc(st)}">
        ${cards.map(cardHtml).join("")}
        ${cards.length ? "" : `<div style="border:1.5px dashed rgba(148,163,184,.13);border-radius:13px;padding:22px 10px;text-align:center;color:var(--txt-3);font-size:11.5px;">Drop leads here</div>`}
      </div>
    </div>`;
  }).join("") + `</div>`;
}

function cardHtml(l) {
  return `
  <article class="lead-card fade-in ${cardAccent(l.score)}" draggable="true" data-user="${esc(l.username)}">
    <div class="card-user">
      <span class="avatar" style="width:28px;height:28px;font-size:11px;background:${avColor(l.username)}">${esc(initials(l.username))}</span>
      <div style="min-width:0;">
        <div class="card-name">${esc(l.username)}</div>
        <div class="card-sub">on ${esc(l.platform)}</div>
      </div>
    </div>
    <div style="display:flex;gap:6px;margin-top:9px;align-items:center;">${scoreChip(l.score)}${ownerPill(l.claimed_by)}</div>
    ${l.next_steps && l.next_steps !== "Review lead details" ? `<div class="card-next"><span>🎯</span><span>${esc(l.next_steps)}</span></div>` : ""}
    <div class="card-foot">
      <span style="font-size:10.5px;color:var(--txt-3);font-weight:600;">🕒 ${timeAgo(l.updated)}</span>
      <button class="mini-btn open-lead" data-open="${esc(l.username)}" style="padding:4px 9px;font-size:11.5px;">Open →</button>
    </div>
  </article>`;
}
/* ============================================================
   RENDER — leads table (sortable)
   ============================================================ */
function renderLeads() {
  const el = $("view-leads");
  const owners = collectOwners();
  const rows = sortRows(applyFilters(LEADS));
  const arrow = (k) => SORT.key === k ? `<span class="arr">${SORT.dir > 0 ? "▲" : "▼"}</span>` : "";

  el.innerHTML = `
  <div class="glass fade-in" style="padding:14px;margin-bottom:12px;display:flex;flex-wrap:wrap;gap:9px;align-items:center;">
    <select id="fStage" class="fsel">
      <option value="">All stages</option>
      ${STAGES.map(s => `<option ${FILTER.stage === s ? "selected" : ""}>${s}</option>`).join("")}
    </select>
    <select id="fScore" class="fsel">
      <option value="">All scores</option>
      ${SCORES.map(s => `<option ${FILTER.score === s ? "selected" : ""}>${s}</option>`).join("")}
    </select>
    <select id="fOwner" class="fsel">
      <option value="">All owners</option>
      ${owners.map(o => `<option ${FILTER.owner === o ? "selected" : ""}>${esc(o)}</option>`).join("")}
    </select>
    ${(FILTER.stage || FILTER.score || FILTER.owner || FILTER.q) ? `<button id="clearBtn" class="mini-btn">✕ Clear filters</button>` : ""}
    <span class="result-count" style="margin-left:auto;"><b>${rows.length}</b> of ${LEADS.length} leads</span>
  </div>

  <div class="glass" style="overflow:hidden;">
    <div style="overflow-x:auto;max-height:calc(100vh - 250px);overflow-y:auto;">
      <table class="tbl">
        <thead><tr>
          <th data-sort="username">Lead ${arrow("username")}</th>
          <th data-sort="status">Stage ${arrow("status")}</th>
          <th data-sort="score">Intent ${arrow("score")}</th>
          <th data-sort="claimed_by">Owner ${arrow("claimed_by")}</th>
          <th data-sort="platform">Source ${arrow("platform")}</th>
          <th>Next step</th>
          <th>Summary</th>
          <th data-sort="updated">Updated ${arrow("updated")}</th>
        </tr></thead>
        <tbody>
          ${rows.length ? rows.map(rowHtml).join("") : `
          <tr><td colspan="8"><div class="empty-wrap" style="padding:44px 16px;">
            <span class="empty-ico" style="font-size:34px;">🔍</span>
            <div style="font-weight:700;">No leads match your filters</div>
            <div style="color:var(--txt-3);font-size:12.5px;margin-top:4px;">Try clearing a filter or changing your search.</div>
          </div></td></tr>`}
        </tbody>
      </table>
    </div>
  </div>`;
}

function rowHtml(l) {
  return `
  <tr data-user="${esc(l.username)}">
    <td><div class="cell-user">
      <span class="avatar" style="background:${avColor(l.username)}">${esc(initials(l.username))}</span>
      <div><div>${esc(l.username)}</div><div style="font-size:10.5px;color:var(--txt-3);font-weight:600;">on ${esc(l.platform)}</div></div>
    </div></td>
    <td>${stageChip(l.status)}</td>
    <td>${scoreChip(l.score)}</td>
    <td>${ownerPill(l.claimed_by)}</td>
    <td style="color:#a5b4fc;font-size:12px;font-weight:650;">${esc(l.platform)}</td>
    <td style="max-width:190px;"><div class="cell-sum" style="max-width:190px;color:#d8cfa2;" title="${esc(l.next_steps)}">${esc(l.next_steps)}</div></td>
    <td><div class="cell-sum" title="${esc(l.summary)}">${esc(l.summary) || "—"}</div></td>
    <td style="white-space:nowrap;color:var(--txt-3);font-size:11.5px;" title="${esc(l.updated)}">${timeAgo(l.updated)}</td>
  </tr>`;
}
/* ============================================================
   LEAD DRAWER — detail panel
   ============================================================ */
let DRAWER_USER = null;

function openDrawer(username) {
  const l = LEADS.find(x => x.username === username);
  if (!l) { toast("Lead no longer exists", "err"); return; }
  DRAWER_USER = username;
  renderDrawer(l);
  $("drawer").classList.add("open");
  $("backdrop").classList.add("show");
}

function closeDrawer() {
  $("drawer").classList.remove("open");
  $("backdrop").classList.remove("show");
  DRAWER_USER = null;
}

function renderDrawer(l) {
  const times = extractTimes(l.summary);
  const entries = times.map(t => ({ t, text: textBefore(l.summary, t) }));
  entries.reverse(); // newest first
  const tintByScore = { HIGH: "rgba(52,211,153,.16)", MEDIUM: "rgba(251,191,36,.15)", LOW: "rgba(248,113,113,.14)", UNKNOWN: "rgba(148,163,184,.12)" };
  const handle = l.username.replace(/^@/, "");
  const isIG = /insta/i.test(l.platform);
  const profileUrl = isIG ? `https://instagram.com/${handle}` : `https://x.com/${handle}`;

  $("drawerBody").innerHTML = `
    <div class="drawer-hero" style="--hero-tint:${tintByScore[l.score] || tintByScore.UNKNOWN};">
      <button class="icon-btn" id="closeDrawerBtn" style="position:absolute;top:16px;right:16px;padding:0;width:34px;">✕</button>
      <div style="display:flex;gap:14px;align-items:center;">
        <div class="drawer-big-avatar" style="background:linear-gradient(135deg,${avColor(l.username)},${avColor(handle)});">${esc(initials(l.username))}</div>
        <div style="min-width:0;">
          <h2 style="margin:0;font-size:19px;letter-spacing:-.01em;">${esc(l.username)}</h2>
          <div style="display:flex;gap:7px;margin-top:7px;">${stageChip(l.status)}${scoreChip(l.score)}</div>
        </div>
      </div>
      <div style="display:flex;gap:8px;margin-top:15px;">
        <a class="mini-btn" href="${profileUrl}" target="_blank" rel="noopener">${isIG ? "📸 Instagram" : "𝕏 Profile"} ↗</a>
        <a class="mini-btn" href="https://t.me/${handle}" target="_blank" rel="noopener">✈️ Telegram ↗</a>
        <button class="mini-btn" id="copyHandleBtn">⧉ Copy</button>
      </div>
    </div>

    <div class="drawer-section">
      <div class="drawer-label">Move to stage</div>
      <div class="stage-jump">
        ${STAGES.map(st => `<button data-stage-jump="${esc(st)}" class="${l.status === st ? "cur" : ""}">${esc(st)}</button>`).join("")}
      </div>
    </div>

    <div class="drawer-section">
      <div class="drawer-label">Owner</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        ${ownerPill(l.claimed_by)}
        <select id="reassignSel" class="fsel">
          <option value="">— Reassign to —</option>
          ${collectOwners().filter(o => o !== l.claimed_by).map(o => `<option>${esc(o)}</option>`).join("")}
          <option value="__new__">New teammate…</option>
        </select>
      </div>
      <div id="newOwnerRow" style="display:none;margin-top:9px;gap:8px;">
        <input id="newOwnerInput" class="inline-edit" placeholder="e.g. @gretta_sales" style="flex:1;"/>
        <button id="saveOwnerBtn" class="mini-btn" style="border-color:rgba(52,211,153,.4);color:var(--brand);">Save</button>
      </div>
    </div>

    <div class="drawer-section">
      <div class="drawer-label">Next step</div>
      <input id="nextStepInput" class="inline-edit" value="${esc(l.next_steps)}"/>
      <div style="display:flex;justify-content:flex-end;margin-top:9px;">
        <button id="saveNextBtn" class="mini-btn" style="border-color:rgba(52,211,153,.4);color:var(--brand);">💾 Save next step</button>
      </div>
    </div>

    <div class="drawer-section">
      <div class="drawer-label">Add a note (appended to AI summary)</div>
      <textarea id="noteText" class="note-input" rows="2" placeholder="Called them, wants pricing by Friday…"></textarea>
      <div style="display:flex;justify-content:flex-end;margin-top:9px;">
        <button id="saveNoteBtn" class="icon-btn primary" style="height:32px;">＋ Add note</button>
      </div>
    </div>

    <div class="drawer-section">
      <div class="drawer-label">Edit lead</div>
      <div class="edit-grid">
        <label>Stage
          <select id="editStatus">${STAGES.map(st => `<option ${l.status === st ? "selected" : ""}>${esc(st)}</option>`).join("")}</select>
        </label>
        <label>Intent score
          <select id="editScore">${["HIGH", "MEDIUM", "LOW", "UNKNOWN"].map(sc => `<option ${l.score === sc ? "selected" : ""}>${sc}</option>`).join("")}</select>
        </label>
        <label>Platform
          <input id="editPlatform" value="${esc(l.platform)}"/>
        </label>
        <label>Owner
          <input id="editOwner" value="${esc(l.claimed_by || "")}" placeholder="empty = unclaimed"/>
        </label>
      </div>
      <label style="margin-top:10px;">Next step
        <input id="editNextSteps" value="${esc(l.next_steps)}"/>
      </label>
      <label style="margin-top:10px;">Conversation summary
        <textarea id="editSummary" rows="4">${esc(l.summary)}</textarea>
      </label>
      <div style="display:flex;justify-content:flex-end;margin-top:11px;">
        <button id="saveEditBtn" class="mini-btn" style="border-color:rgba(52,211,153,.4);color:var(--brand);padding:7px 14px;">💾 Save changes</button>
      </div>
    </div>

    <div class="drawer-section" style="border-bottom:none;">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <span style="font-size:11px;color:var(--txt-3);font-weight:600;">Deletes the row and all its history.</span>
        <button class="danger-btn" id="deleteLeadBtn">🗑 Delete this lead permanently</button>
      </div>
    </div>


    <div class="drawer-section" style="border-bottom:none;">
      <div class="drawer-label">Activity timeline · ${times.length} update${times.length === 1 ? "" : "s"}</div>
      <div class="tl">
        ${entries.length ? entries.map((e, i) => `
          <div class="tl-item ${i > 0 ? "old" : ""}">
            <span class="tl-dot"></span>
            <div class="tl-time mono">${new Date(e.t).toLocaleString()}</div>
            <div class="tl-body">${esc(e.text) || "<i>update</i>"}</div>
          </div>`).join("")
      : `<div style="color:var(--txt-3);font-size:12.5px;">No timestamped history yet.</div>`}
      </div>
    </div>`;
}
/* ============================================================
   CSV EXPORT
   ============================================================ */
function exportCsv() {
  const rows = sortRows(applyFilters(LEADS));
  if (!rows.length) { toast("Nothing to export with current filters", "err"); return; }
  const cols = ["username", "status", "score", "claimed_by", "platform", "next_steps", "summary", "updated"];
  const q = (v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
  const csv = [cols.join(",")].concat(rows.map(r => cols.map(c => q(r[c])).join(","))).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = `gretta-leads-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast(`Exported ${rows.length} leads`);
}

/* ============================================================
   DATA LOAD + LIVE POLLING
   ============================================================ */
async function load({ silent = false } = {}) {
  try {
    const data = await api("/api/leads");
    LEADS = data.leads;
    STATS = data.stats;
    collectOwners();
    render();
    $("botStatus").textContent = "● Bot live";
    $("botStatus").style.color = "var(--brand)";
  } catch (e) {
    console.error(e);
    $("botStatus").textContent = "● Offline";
    $("botStatus").style.color = "#f87171";
    if (!silent && Date.now() - LAST_ERR_TOAST > 15000) {
      toast("Couldn't reach the CRM backend", "err");
      LAST_ERR_TOAST = Date.now();
    }
  }
}

function startPolling() {
  clearInterval(TIMER);
  TIMER = setInterval(() => load({ silent: true }), 15000);
}

/* ============================================================
   THEME (light / dark)
   ============================================================ */
function applyTheme(mode) {
  document.documentElement.dataset.theme = mode;
  $("themeBtn").textContent = mode === "light" ? "🌙 Dark" : "☀️ Light";
  $("themeBtn").title = mode === "light" ? "Switch to dark mode" : "Switch to light mode";
  try { localStorage.setItem("gretta_theme", mode); } catch {}
  drawCharts(); // re-render charts with theme-aware colors
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
}

function togglePause() {
  PAUSED = !PAUSED;
  if (PAUSED) { clearInterval(TIMER); }
  else startPolling();
  $("liveDot").classList.toggle("paused", PAUSED);
  $("liveLbl").textContent = PAUSED ? "Paused" : "Live";
  $("pauseBtn").innerHTML = PAUSED ? "▶ Resume" : "⏸ Pause";
}

/* ============================================================
   RENDER DISPATCH + VIEW SWITCHING
   ============================================================ */
function render() {
  if (VIEW === "overview") renderOverview();
  else if (VIEW === "board") renderBoard();
  else if (VIEW === "leads") renderLeads();

  document.querySelectorAll(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.view === VIEW));
}

function switchView(v) {
  VIEW = v;
  if (location.hash !== `#/${v}`) { location.hash = `#/${v}`; return; } // hashchange re-enters with VIEW set
  for (const id of ["overview", "board", "leads"]) $(`view-${id}`).style.display = id === v ? "" : "none";
  render();
  $("sidebar").classList.remove("open"); // close mobile drawer after nav
}

function viewFromHash() {
  const h = location.hash.replace(/^#\/?/, "");
  return ["overview", "board", "leads"].includes(h) ? h : "overview";
}
/* ============================================================
   COMMAND PALETTE (⌘/Ctrl+K)
   ============================================================ */
let CMDK_ITEMS = [];
function openCmdk() {
  $("cmdk").classList.add("show");
  const inp = $("cmdkInput");
  inp.value = "";
  buildCmdk("");
  inp.focus();
}
function closeCmdk() { $("cmdk").classList.remove("show"); }

function buildCmdk(q) {
  q = q.trim().toLowerCase();
  CMDK_ITEMS = [
    ...["overview", "board", "leads"].map(v => ({ icon: v === "overview" ? "📊" : v === "board" ? "🗂" : "📋", label: `Go to ${v === "board" ? "Pipeline Board" : v === "leads" ? "All Leads" : "Overview"}`, act: () => switchView(v) })),
    ...LEADS.filter(l => !q || l.username.toLowerCase().includes(q)).slice(0, 8).map(l => ({
      icon: "🎯", label: `${l.username} — ${l.status}`, act: () => openDrawer(l.username),
    })),
  ].filter(it => it.icon !== "🎯" || q); // lead items only when searching
  $("cmdkList").innerHTML = CMDK_ITEMS.length
    ? CMDK_ITEMS.map((it, i) => `<button class="cmdk-item ${i === 0 ? "hl" : ""}" data-idx="${i}"><span>${it.icon}</span><span>${esc(it.label)}</span></button>`).join("")
    : `<div class="cmdk-empty">No matches</div>`;
}

/* ============================================================
   ACTION HELPERS (move, reassign)
   ============================================================ */
async function moveLead(username, stage) {
  const l = LEADS.find(x => x.username === username);
  if (!l || l.status === stage) return;
  try {
    await api("/api/lead/stage", { method: "POST", body: { username, stage } });
    l.status = stage;
    toast(`${username} → ${stage}`);
    render();
    if ($("drawer").classList.contains("open")) openDrawer(username);
  } catch { toast("Move failed — is the backend up?", "err"); }
}

async function doReassign(name) {
  try {
    await api("/api/lead/owner", { method: "POST", body: { username: DRAWER_USER, owner: name } });
    toast(`Owner set to ${name}`);
    await load({ silent: true });
    closeDrawer();
  } catch { toast("Reassign failed", "err"); }
}

async function saveEdits() {
  const l = LEADS.find(x => x.username === DRAWER_USER);
  if (!l) return;
  try {
    await api("/api/lead/update", {
      method: "POST",
      body: {
        username: l.username,
        status: $("editStatus").value,
        score: $("editScore").value,
        platform: $("editPlatform").value.trim(),
        owner: $("editOwner").value.trim(),
        next_steps: $("editNextSteps").value.trim(),
        summary: $("editSummary").value,   // full overwrite of the history text
      },
    });
    toast(`${l.username} updated`);
    await load({ silent: true });
    openDrawer(l.username);
  } catch { toast("Save failed — check stage/score values", "err"); }
}
/* ============================================================
   EVENT WIRING
   ============================================================ */
function wireEvents() {
  /* sidebar + topbar buttons */
  document.querySelectorAll(".nav-btn").forEach(b => b.addEventListener("click", () => switchView(b.dataset.view)));
  $("refreshBtn").addEventListener("click", () => load());
  $("pauseBtn").addEventListener("click", togglePause);
  $("csvBtn").addEventListener("click", exportCsv);
  $("themeBtn").addEventListener("click", toggleTheme);
  $("q").addEventListener("input", (e) => {
    FILTER.q = e.target.value;
    if (VIEW === "leads") renderLeads();
    else if (VIEW === "board") renderBoard();
  });
  $("hamburger").addEventListener("click", () => $("sidebar").classList.toggle("open"));
  if (window.matchMedia("(max-width: 900px)").matches) $("hamburger").style.display = "";

  /* global click delegation */
  document.addEventListener("click", async (e) => {
    const jump = e.target.closest("[data-stage-jump]");
    if (jump && DRAWER_USER) { await moveLead(DRAWER_USER, jump.dataset.stageJump); return; }
    const fstage = e.target.closest("[data-fstage]");
    if (fstage) { FILTER.stage = FILTER.stage === fstage.dataset.fstage ? "" : fstage.dataset.fstage; render(); return; }
    const fownerBtn = e.target.closest("[data-fowner-btn]");
    if (fownerBtn) { FILTER.owner = fownerBtn.dataset.fownerBtn; switchView("leads"); return; }
    const fowner = e.target.closest("[data-fowner]");
    if (fowner) { FILTER.owner = FILTER.owner === fowner.dataset.fowner ? "" : fowner.dataset.fowner; render(); return; }
    const open = e.target.closest("[data-open]");
    if (open) { openDrawer(open.dataset.open); return; }
    const rowCard = e.target.closest("tr[data-user], .lead-card");
    if (rowCard && !e.target.closest("button, a, select, input")) { openDrawer(rowCard.dataset.user); return; }
    const cmdItem = e.target.closest(".cmdk-item");
    if (cmdItem) { closeCmdk(); CMDK_ITEMS[+cmdItem.dataset.idx].act(); return; }
  });

  /* backdrop / keyboard shortcuts */
  $("backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openCmdk(); return; }
    if (e.key === "Escape") { closeCmdk(); closeDrawer(); }
    if (e.key === "/" && !/input|textarea|select/i.test(document.activeElement.tagName)) { e.preventDefault(); $("q").focus(); }
    if (e.key.toLowerCase() === "r" && !/input|textarea|select/i.test(document.activeElement.tagName) && !(e.metaKey || e.ctrlKey)) load();
  });
  $("cmdkInput").addEventListener("input", (e) => buildCmdk(e.target.value));
  $("cmdk").addEventListener("click", (e) => { if (e.target.id === "cmdk") closeCmdk(); });
}
/* drawer inner actions (delegated because drawerBody re-renders) */
function wireDrawerEvents() {
  $("drawerBody").addEventListener("click", async (e) => {
    if (e.target.closest("#closeDrawerBtn")) { closeDrawer(); return; }
    if (e.target.id === "copyHandleBtn") {
      try { await navigator.clipboard.writeText(DRAWER_USER); toast("Handle copied"); } catch { toast("Copy failed", "err"); }
      return;
    }
    if (e.target.id === "saveNextBtn") {
      const l = LEADS.find(x => x.username === DRAWER_USER);
      try {
        await api("/api/lead/next_steps", { method: "POST", body: { username: DRAWER_USER, next_steps: $("nextStepInput").value } });
        if (l) l.next_steps = $("nextStepInput").value.trim() || l.next_steps;
        toast("Next step saved"); render();
      } catch { toast("Save failed — is the backend up?", "err"); }
      return;
    }
    if (e.target.id === "saveNoteBtn") {
      const note = $("noteText").value.trim();
      if (!note) { toast("Write something first", "err"); return; }
      try {
        await api("/api/lead/note", { method: "POST", body: { username: DRAWER_USER, note } });
        toast("Note added"); await load({ silent: true }); openDrawer(DRAWER_USER);
      } catch { toast("Save failed — is the backend up?", "err"); }
      return;
    }
    if (e.target.id === "saveOwnerBtn") {
      const name = $("newOwnerInput").value.trim();
      if (!name) { toast("Enter a name", "err"); return; }
      await doReassign(name);
      return;
    }
    if (e.target.id === "saveEditBtn") {
      await saveEdits();
      return;
    }
    if (e.target.id === "deleteLeadBtn") {
      const btn = e.target;
      if (!btn.dataset.armed) {           // two-click guard against accidents
        btn.dataset.armed = "1";
        btn.textContent = "⚠️ Really delete? Click again to confirm";
        setTimeout(() => { btn.dataset.armed = ""; btn.textContent = "🗑 Delete this lead permanently"; }, 4000);
        return;
      }
      try {
        const victim = DRAWER_USER;
        await api("/api/lead/delete", { method: "POST", body: { username: victim } });
        toast(`${victim} deleted`);
        closeDrawer();
        await load({ silent: true });
      } catch { toast("Delete failed", "err"); }
      return;
    }
  });

  $("drawerBody").addEventListener("change", async (e) => {
    if (e.target.id !== "reassignSel") return;
    if (e.target.value === "__new__") { $("newOwnerRow").style.display = "flex"; $("newOwnerInput").focus(); }
    else if (e.target.value) await doReassign(e.target.value);
  });
}

/* table filter selects + sortable headers (delegated, views re-render) */
function wireTableEvents() {
  document.addEventListener("change", (e) => {
    if (e.target.id === "fStage") { FILTER.stage = e.target.value; renderLeads(); }
    else if (e.target.id === "fScore") { FILTER.score = e.target.value; renderLeads(); }
    else if (e.target.id === "fOwner") { FILTER.owner = e.target.value; renderLeads(); }
  });
  document.addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]");
    if (!th) return;
    if (SORT.key === th.dataset.sort) SORT.dir *= -1;
    else { SORT.key = th.dataset.sort; SORT.dir = -1; }
    renderLeads();
  });
}

/* ============================================================
   DRAG & DROP
   ============================================================ */
let DND_USER = null;

function wireDragAndDrop() {
  document.addEventListener("dragstart", (e) => {
    const card = e.target.closest(".lead-card");
    if (!card) return;
    DND_USER = card.dataset.user;
    card.classList.add("dragging");
    document.body.classList.add("dragging-active");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", DND_USER); } catch {}
  });

  document.addEventListener("dragend", () => {
    document.querySelectorAll(".lead-card.dragging").forEach(c => c.classList.remove("dragging"));
    document.querySelectorAll(".kanban-col.dragover").forEach(c => c.classList.remove("dragover"));
    document.body.classList.remove("dragging-active");
    DND_USER = null;
  });

  document.addEventListener("dragover", (e) => {
    const col = e.target.closest(".kanban-col");
    if (!col || !DND_USER) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    document.querySelectorAll(".kanban-col.dragover").forEach(c => c.classList.remove("dragover"));
    col.classList.add("dragover");
  });

  document.addEventListener("drop", (e) => {
    const col = e.target.closest(".kanban-col");
    if (!col || !DND_USER) return;
    e.preventDefault();
    moveLead(DND_USER, col.dataset.stage); // moveLead re-renders the board
  });
}

/* ============================================================
   BOOT
   ============================================================ */
document.addEventListener("DOMContentLoaded", () => {
  let saved = null;
  try { saved = localStorage.getItem("gretta_theme"); } catch {}
  applyTheme(saved === "light" ? "light" : "dark");

  window.addEventListener("hashchange", () => switchView(viewFromHash()));
  VIEW = viewFromHash();

  wireEvents();
  wireDrawerEvents();
  wireTableEvents();
  wireDragAndDrop();
  load().then(startPolling);
});
