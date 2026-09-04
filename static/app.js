/* ============================================================
   Gritta CRM — app.js
   Vanilla JS SPA · mirrors the team Google Sheet columns
   (Lead Number … Closed Won/Lost) · polls FastAPI · no build step
   ============================================================ */
"use strict";

/* ---------- constants : the 14 outreach statuses of the sheet ---------- */
const STATUSES = ["Message Sent", "Seen Not Replied", "Replied",
  "Follow up 1", "Follow up 2", "Follow up 3", "Follow up 4",
  "Replied-No yet booked", "Closing Call", "Number received",
  "Discovery Call booked", "Not Interested", "Lost", "Won"];
const EMOJI = { "Message Sent": "📨", "Seen Not Replied": "👀", "Replied": "💬",
  "Follow up 1": "1️⃣", "Follow up 2": "2️⃣", "Follow up 3": "3️⃣", "Follow up 4": "4️⃣",
  "Replied-No yet booked": "🤔", "Closing Call": "📞", "Number received": "☎️",
  "Discovery Call booked": "📅", "Not Interested": "🚫", "Lost": "❌", "Won": "🏆" };
const SCOL = { "Message Sent": "#94a3b8", "Seen Not Replied": "#64748b",
  "Replied": "#38bdf8", "Follow up 1": "#818cf8", "Follow up 2": "#a78bfa",
  "Follow up 3": "#c084fc", "Follow up 4": "#e879f9", "Replied-No yet booked": "#fbbf24",
  "Closing Call": "#fb923c", "Number received": "#2dd4bf",
  "Discovery Call booked": "#60a5fa", "Not Interested": "#f87171",
  "Lost": "#ef4444", "Won": "#34d399" };
const CLOSER = new Set(["Replied", "Replied-No yet booked", "Number received",
  "Closing Call", "Discovery Call booked", "Won"]);
const CLOSING_OPTS = ["Interested", "Not Interested", "No Response", "Scheduled",
  "Completed", "Rescheduled", "No Show"];
const disp = (s) => `${EMOJI[s] || "▫️"} ${s}`;

/* ---------- state ---------- */
let LEADS = [], STATS = {}, USERS = [];
let AUTH_REQUIRED = false;
let FILTER = { status: "", setter: "", q: "" };
let SORT = { key: "updated", dir: -1 };
let VIEW = "overview";
let TEAM_TAB = "";  // Team view: "" = leaderboard tab, otherwise a setter name
let DATA_READY = false;
let DATA_SIGNATURE = "";
let LOADING = false;
let POLL_HANDLE = null;
let LAST_ERR_TOAST = 0;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- api ---------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && !path.startsWith("/api/auth/")) showLogin();
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); if (j.error) msg = j.error; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

/* ---------- small ui helpers ---------- */
function toast(msg, kind = "ok") {
  const box = document.createElement("div");
  box.className = `toast ${kind === "ok" ? "" : kind}`;
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
  return `${Math.floor(h / 24)}d ago`;
}
function animateNum(el, to) {
  if (!el) return;
  const from = parseInt(el.textContent.replace(/\D/g, "")) || 0;
  if (from === to) { el.textContent = to; return; }
  const t0 = performance.now(), dur = 500;
  requestAnimationFrame(function step(t) {
    const p = Math.min((t - t0) / dur, 1);
    el.textContent = Math.round(from + (to - from) * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(step);
  });
}
function statusChip(st) {
  return `<span class="chip" style="background:${SCOL[st] || "#64748b"}22;color:${SCOL[st] || "#94a3b8"};border:1px solid ${SCOL[st] || "#94a3b8"}55;">${esc(disp(st))}</span>`;
}
function setterPill(name) {
  return `<span class="chip owner"><span class="avatar" style="width:16px;height:16px;font-size:8.5px;background:${avColor(name)}">${esc(initials(name))}</span>${esc(name || "Unassigned")}</span>`;
}
function fuProgress(l) {
  let done = 0;
  for (const n of [1, 2, 3, 4]) if (l[`follow_up_${n}`] === "Yes") done++;
  return `<span class="mono" style="font-size:11px;color:var(--txt-3);">🔁 ${done}/4</span>`;
}

/* ---------- filtering / sorting ---------- */
function applyFilters(rows) {
  const q = FILTER.q.trim().toLowerCase();
  return rows.filter(l => {
    if (FILTER.status && l.status !== FILTER.status) return false;
    if (FILTER.setter && (l.sender_name || "Unassigned") !== FILTER.setter) return false;
    if (q) {
      const hay = `${l.user_name} ${l.full_name} ${l.note} ${l.number} ${l.sender_name}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}
function sortRows(rows) {
  const k = SORT.key, d = SORT.dir;
  return [...rows].sort((a, b) => {
    let va, vb;
    if (k === "updated") { return (ts(a) - ts(b)) * d; }
    if (k === "lead_number") { va = a.sender_name + String(a.lead_number).padStart(5, "0"); vb = b.sender_name + String(b.lead_number).padStart(5, "0"); return va.localeCompare(vb) * d; }
    va = String(a[k] ?? "").toLowerCase(); vb = String(b[k] ?? "").toLowerCase();
    return va.localeCompare(vb) * d;
  });
}
function collectSetters() {
  const set = new Set();
  for (const l of LEADS) set.add(l.sender_name || "Unassigned");
  return [...set].sort();
}

/* ============================================================
   RENDER — overview (KPIs · funnel · charts · leaderboard)
   ============================================================ */
function renderOverview() {
  const el = $("view-overview");
  if (!DATA_READY) {
    el.innerHTML = `
      <div class="skeleton-page" aria-label="Loading dashboard" aria-busy="true">
        <div class="skeleton-kpis">
          ${Array.from({ length: 5 }, () => `<div class="glass skeleton-card"><span class="skeleton-line short"></span><span class="skeleton-line number"></span></div>`).join("")}
        </div>
        <div class="skeleton-columns">
          <div class="glass skeleton-panel"><span class="skeleton-line title"></span><span class="skeleton-line wide"></span><span class="skeleton-line wide"></span><span class="skeleton-line medium"></span><span class="skeleton-line wide"></span></div>
          <div class="glass skeleton-panel"><span class="skeleton-line title"></span><span class="skeleton-chart"></span></div>
        </div>
        <div class="glass skeleton-panel leaderboard-skeleton"><span class="skeleton-line title"></span><span class="skeleton-line wide"></span><span class="skeleton-line wide"></span><span class="skeleton-line medium"></span></div>
      </div>`;
    return;
  }
  if (!LEADS.length) {
    el.innerHTML = `
    <div class="glass empty-wrap fade-in">
      <span class="empty-ico">🪄</span>
      <h3 style="margin:0 0 6px;font-size:17px;">No leads yet</h3>
      <p style="color:var(--txt-3);font-size:13px;max-width:340px;margin:0;">
        Use <b>/addlead</b> in Telegram or <b>/importsheet</b> to pull your Google Sheet — leads appear here live.</p>
    </div>`;
    $("healthBar").style.width = "0%";
    $("healthPct").textContent = "0%";
    return;
  }

  const byStatus = STATS.by_status || {};
  const sum = (keys) => keys.reduce((acc, k) => acc + (byStatus[k] || 0), 0);
  const numbersIn = LEADS.filter(l => l.number_received === "Yes").length;
  const repliedN = LEADS.filter(l => l.replied === "Yes").length;

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
    ${kpi("kWarm", "Warm Leads", "🔥", "rgba(251,191,36,.14)", "rgba(251,191,36,.35)")}
    ${kpi("kWon", "Closed Won", "🏆", "rgba(52,211,153,.18)", "rgba(52,211,153,.45)")}
    ${kpi("kNum", "Numbers In", "☎️", "rgba(45,212,191,.14)", "rgba(45,212,191,.35)")}
    ${kpi("kRep", "Replied", "💬", "rgba(56,189,248,.14)", "rgba(56,189,248,.35)")}
    ${kpi("kDead", "NI / Lost", "🚫", "rgba(248,113,113,.13)", "rgba(248,113,113,.3)")}
  </div>

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px;">
    <!-- FUNNEL -->
    <div class="glass fade-in" style="padding:18px;min-width:0;">
      <div style="font-weight:750;font-size:15px;margin-bottom:4px;">Pipeline Funnel</div>
      <div style="font-size:11.5px;color:var(--txt-3);margin-bottom:14px;">Click a status to filter everywhere · click again to clear</div>
      ${STATUSES.map(st => {
        const n = byStatus[st] || 0;
        const max = Math.max(...STATUSES.map(x => byStatus[x] || 0), 1);
        const w = Math.max(n ? (n / max) * 100 : 2, 2);
        const c = SCOL[st];
        return `<div class="funnel-row ${FILTER.status === st ? "sel" : ""}" data-fstatus="${esc(st)}">
          <span style="font-size:12.5px;font-weight:650;color:${c};display:flex;align-items:center;gap:7px;white-space:nowrap;">
            <span style="width:8px;height:8px;border-radius:99px;background:${c};"></span>${esc(disp(st))}</span>
          <div class="funnel-track"><div class="funnel-fill" style="width:${w}%;background:linear-gradient(90deg,${c}55,${c});"></div></div>
          <b class="mono" style="text-align:right;color:var(--txt-2);">${n}</b>
        </div>`;
      }).join("")}
    </div>

    <!-- CHARTS -->
    <div style="display:flex;flex-direction:column;gap:14px;min-width:0;">
      <div class="glass fade-in" style="padding:18px;">
        <div style="font-weight:750;font-size:15px;margin-bottom:11px;">Pipeline Mix</div>
        <div class="chart-box" style="height:172px;"><canvas id="donutCv"></canvas></div>
      </div>
      <div class="glass fade-in" style="padding:18px;">
        <div style="font-weight:750;font-size:15px;margin-bottom:11px;">Lead Activity — last 14 days</div>
        <div class="chart-box" style="height:150px;"><canvas id="lineCv"></canvas></div>
      </div>
    </div>

    <!-- LEADERBOARD -->
    <div class="glass fade-in" style="padding:18px;">
      <div style="font-weight:750;font-size:15px;margin-bottom:4px;">Setter Leaderboard</div>
      <div style="font-size:11.5px;color:var(--txt-3);margin-bottom:13px;">Ranked by closed-won, then total logged</div>
      <div id="lbBody"></div>
    </div>
  </div>`;

  animateNum($("kTotal"), STATS.total || 0);
  animateNum($("kWarm"), STATS.warm || sum([...CLOSER].filter(s => s !== "Won")));
  animateNum($("kWon"), byStatus["Won"] || 0);
  animateNum($("kNum"), numbersIn);
  animateNum($("kRep"), repliedN);
  animateNum($("kDead"), (byStatus["Not Interested"] || 0) + (byStatus["Lost"] || 0));

  const engaged = LEADS.filter(l => l.status !== "Message Sent").length;
  const pct = LEADS.length ? Math.round((engaged / LEADS.length) * 100) : 0;
  $("healthBar").style.width = pct + "%";
  $("healthPct").textContent = pct + "%";

  renderLeaderboard();
  drawCharts(byStatus);
}
/* ============================================================
   RENDER — charts + setter leaderboard
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
function drawCharts(byStatus) {
  if (!window.Chart) return;
  const T = chartTheme();
  byStatus = byStatus || STATS.by_status || {};

  /* donut: pipeline mix grouped into funnel phases */
  const groups = [
    { label: "Outreach", keys: ["Message Sent", "Seen Not Replied", "Follow up 1", "Follow up 2", "Follow up 3", "Follow up 4"], color: "#94a3b8" },
    { label: "Replied", keys: ["Replied", "Replied-No yet booked"], color: "#38bdf8" },
    { label: "Closing", keys: ["Number received", "Closing Call"], color: "#fb923c" },
    { label: "Discovery", keys: ["Discovery Call booked"], color: "#60a5fa" },
    { label: "Won", keys: ["Won"], color: "#34d399" },
    { label: "Dead", keys: ["Not Interested", "Lost"], color: "#f87171" },
  ];
  const counts = groups.map(g => g.keys.reduce((a, k) => a + (byStatus[k] || 0), 0));
  const dEl = $("donutCv");
  if (CHARTS.donut) { CHARTS.donut.destroy(); delete CHARTS.donut; }
  if (dEl) {
    if (!counts.some(n => n > 0)) {
      dEl.replaceWith(Object.assign(dEl.cloneNode(false), { outerHTML: `<div class="chart-empty">No leads yet</div>` }));
    } else {
      CHARTS.donut = new Chart(dEl.getContext("2d"), {
        type: "doughnut",
        data: {
          labels: groups.map(g => g.label),
          datasets: [{ data: counts, backgroundColor: groups.map(g => g.color), borderColor: T.border, borderWidth: 3, hoverOffset: 8 }],
        },
        options: {
          maintainAspectRatio: false, cutout: "68%",
          plugins: { legend: { position: "right", labels: { color: T.label, boxWidth: 10, boxHeight: 10, borderRadius: 3, useBorderRadius: true, padding: 12, font: { family: "Inter", size: 11.5, weight: 600 } } } },
        },
      });
    }
  }

  /* line: last-14-day activity from Last Touchpoint */
  const days = [];
  for (let i = 13; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000);
    days.push({ key: d.toISOString().slice(0, 10), label: `${d.getDate()}/${d.getMonth() + 1}`, n: 0 });
  }
  for (const l of LEADS) {
    if (!l.last_touchpoint) continue;
    const k = String(l.last_touchpoint);
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
          fill: true, backgroundColor: grad, tension: .35,
          pointRadius: 2.5, pointBackgroundColor: "#34d399",
        }],
      },
      options: {
        maintainAspectRatio: false,
        scales: {
          x: { ticks: { color: T.tick, font: { family: "Inter", size: 10 } }, grid: { display: false } },
          y: { beginAtZero: true, ticks: { color: T.tick, precision: 0, font: { family: "Inter", size: 10 } }, grid: { color: T.grid } },
        },
        plugins: { legend: { display: false } },
      },
    });
  }
}


/* ============================================================
   RENDER — team view (leaderboard tabs + per-setter lead cards)
   ============================================================ */
function renderTeam() {
  const el = $("view-team");
  const setters = collectSetters().filter(s => s !== "Unassigned");
  if (!setters.length) {
    el.innerHTML = `
    <div class="glass empty-wrap fade-in">
      <span class="empty-ico">👥</span>
      <h3 style="margin:0 0 6px;font-size:17px;">No setters yet</h3>
      <p style="color:var(--txt-3);font-size:13px;max-width:340px;margin:0;">
        Add leads with <b>/addlead</b> in Telegram — setters appear here automatically.</p>
    </div>`;
    return;
  }

  const bySetter = {};
  for (const l of LEADS) {
    const s = l.sender_name || "Unassigned";
    if (!bySetter[s]) bySetter[s] = [];
    bySetter[s].push(l);
  }

  // Build tabs
  const tabHtml = setters.map(name => {
    const leads = bySetter[name] || [];
    const won = leads.filter(x => x.status === "Won").length;
    const warm = leads.filter(x => CLOSER.has(x.status) && x.status !== "Won").length;
    const active = TEAM_TAB === name ? "active" : "";
    return `<button class="team-tab ${active}" data-team-tab="${esc(name)}">
      ${esc(name)} <span class="t-count">${leads.length}</span>
      <span class="t-count" style="background:rgba(52,211,153,.12);color:#34d399;">🏆${won}</span>
      <span class="t-count" style="background:rgba(251,191,36,.12);color:#fbbf24;">🔥${warm}</span>
    </button>`;
  }).join("");
  const allTab = `<button class="team-tab ${!TEAM_TAB ? "active" : ""}" data-team-tab="">All <span class="t-count">${LEADS.length}</span></button>`;

  if (!TEAM_TAB) {
    // Leaderboard tab
    el.innerHTML = `
    <div class="glass fade-in" style="padding:18px;">
      <div style="font-weight:750;font-size:15px;margin-bottom:4px;">Team Overview</div>
      <div style="font-size:11.5px;color:var(--txt-3);margin-bottom:13px;">Click a setter to see their leads</div>
      <div class="team-tabs">${allTab}${tabHtml}</div>
      <div class="member-grid">
        ${setters.map(name => {
          const leads = bySetter[name] || [];
          const won = leads.filter(x => x.status === "Won").length;
          const warm = leads.filter(x => CLOSER.has(x.status) && x.status !== "Won").length;
          const numbers = leads.filter(x => x.number_received === "Yes").length;
          const replied = leads.filter(x => x.replied === "Yes").length;
          const nextTps = leads.filter(x => x.next_touchpoint).length;
          return `
          <button class="member-card" data-goto="team" data-team-tab="${esc(name)}">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
              <span class="avatar" style="width:36px;height:36px;font-size:13px;background:${avColor(name)}">${esc(initials(name))}</span>
              <div>
                <div style="font-weight:700;font-size:14px;">${esc(name)}</div>
                <div style="font-size:11px;color:var(--txt-3);">${leads.length} leads</div>
              </div>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:6px;">
              <span class="m-stat">🏆 <b>${won}</b></span>
              <span class="m-stat">🔥 <b>${warm}</b></span>
              <span class="m-stat">☎️ <b>${numbers}</b></span>
              <span class="m-stat">💬 <b>${replied}</b></span>
              <span class="m-stat">📅 <b>${nextTps}</b></span>
            </div>
          </button>`;
        }).join("")}
      </div>
    </div>`;
  } else {
    // Single setter's leads
    const leads = sortRows(bySetter[TEAM_TAB] || []);
    const backBtn = `<button class="team-tab" data-team-tab="" style="margin-bottom:14px;">← Back to leaderboard</button>`;
    el.innerHTML = `
    <div class="glass fade-in" style="padding:18px;">
      ${backBtn}
      <div class="member-grid">
        ${leads.length ? leads.map(l => `
        <button class="member-card lead-row" data-user="${esc(l.user_name)}" style="text-align:left;">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
            <span class="avatar" style="width:32px;height:32px;font-size:11px;background:${avColor(l.user_name)}">${esc(initials(l.full_name || l.user_name))}</span>
            <div style="flex:1;min-width:0;">
              <div style="font-weight:650;font-size:13px;">${esc(l.full_name || l.user_name)} <span class="mono" style="opacity:.5;font-size:11px;">#${l.lead_number}</span></div>
              <a class="mono" href="${esc(l.profile_link || "#")}" target="_blank" rel="noopener" style="font-size:11px;color:var(--brand);text-decoration:none;">${esc(l.user_name)}</a>
            </div>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
            ${statusChip(l.status)}
            ${setterPill(l.sender_name)}
            ${fuProgress(l)}
            ${l.number_received === "Yes" ? `<span class="m-stat">☎️</span>` : ""}
            ${l.replied === "Yes" ? `<span class="m-stat">💬</span>` : ""}
            ${l.next_touchpoint ? `<span class="m-stat mono" style="font-size:10.5px;background:rgba(56,189,248,.12);color:var(--brand);">📅 ${esc(l.next_touchpoint)}</span>` : ""}
          </div>
        </button>`).join("") : `
        <div class="chart-empty" style="grid-column:1/-1;">No leads for ${esc(TEAM_TAB)} yet</div>`}
      </div>
    </div>`;
  }
}

function renderLeaderboard() {
  const el = $("lbBody");
  if (!el) return;
  const setters = Object.entries(STATS.setters || {});
  if (!setters.length) { el.innerHTML = `<div class="chart-empty">No setters yet</div>`; return; }
  setters.sort((a, b) => ((b[1].by_status?.["Won"] || 0) - (a[1].by_status?.["Won"] || 0)) || (b[1].total - a[1].total));
  const max = Math.max(...setters.map(([, s]) => s.total), 1);
  el.innerHTML = setters.map(([name, s], i) => {
    const won = s.by_status?.["Won"] || 0;
    const warm = [...CLOSER].reduce((a, k) => a + (s.by_status?.[k] || 0), 0);
    const medal = ["🥇", "🥈", "🥉"][i] || `#${i + 1}`;
    return `
    <div style="display:flex;align-items:center;gap:9px;padding:7px 4px;border-bottom:1px solid var(--line);">
      <span style="font-size:13px;width:26px;">${medal}</span>
      <span class="avatar" style="width:26px;height:26px;font-size:10px;background:${avColor(name)}">${esc(initials(name))}</span>
      <div style="flex:1;min-width:0;">
        <div style="display:flex;justify-content:space-between;font-size:12.5px;font-weight:650;">
          <span>${esc(name)}</span>
          <span class="mono" style="color:var(--txt-2);">${s.total} · 🏆${won} · 🔥${warm}</span>
        </div>
        <div class="funnel-track" style="margin-top:5px;">
          <div class="funnel-fill" style="width:${Math.max((s.total / max) * 100, 3)}%;background:linear-gradient(90deg,#34d39955,#34d399);"></div>
        </div>
      </div>
    </div>`;
  }).join("");
}

/* ============================================================
   RENDER — kanban board (one column per status)
   ============================================================ */
function renderBoard() {
  const el = $("view-board");
  const rows = applyFilters(LEADS);
  el.innerHTML = `<div class="board" style="height:100%;">` + STATUSES.map(st => {
    const cards = sortRows(rows.filter(r => r.status === st));
    return `
    <div class="board-col" data-stage="${esc(st)}">
      <div class="board-head" style="color:${SCOL[st]};">
        ${esc(disp(st))}
        <span class="mono board-count">${cards.length}</span>
      </div>
      <div class="board-cards" data-stage-drop="${esc(st)}">
        ${cards.map(cardHtml).join("") || `<div class="board-empty">—</div>`}
      </div>
    </div>`;
  }).join("") + `</div>`;
  wireDragAndDrop();
}

function cardHtml(l) {
  const name = l.full_name || l.user_name;
  return `
  <article class="lead-card fade-in" draggable="true" data-user="${esc(l.user_name)}">
    <div class="card-user">
      <span class="avatar" style="width:28px;height:28px;font-size:11px;background:${avColor(l.user_name)}">${esc(initials(name))}</span>
      <div style="min-width:0;">
        <div class="card-name"><span class="mono" style="opacity:.6;font-size:10.5px;">#${l.lead_number}</span> ${esc(name)}</div>
        <div class="card-sub mono">${esc(l.user_name)}${l.followers_count ? ` · 👥 ${esc(l.followers_count)}` : ""}</div>
      </div>
    </div>
    <div style="display:flex;gap:6px;margin-top:9px;align-items:center;flex-wrap:wrap;">
      ${setterPill(l.sender_name)}${fuProgress(l)}
      ${l.number_received === "Yes" ? `<span title="number received">☎️</span>` : ""}
      ${l.next_touchpoint ? `<span class="mono" style="font-size:10.5px;color:var(--txt-3);">📅 ${esc(l.next_touchpoint)}</span>` : ""}
    </div>
    <div class="card-foot">
      <span style="font-size:10.5px;color:var(--txt-3);font-weight:600;">🕒 ${timeAgo(l.updated)}</span>
      <button class="mini-btn open-lead" data-open="${esc(l.user_name)}" style="padding:4px 9px;font-size:11.5px;">Open →</button>
    </div>
  </article>`;
}

/* ============================================================
   RENDER — leads table (sortable)
   ============================================================ */
function renderLeads() {
  const el = $("view-leads");
  const owners = collectSetters();
  const rows = sortRows(applyFilters(LEADS));
  const arrow = (k) => SORT.key === k ? `<span class="arr">${SORT.dir > 0 ? "▲" : "▼"}</span>` : "";

  el.innerHTML = `
  <div class="glass fade-in" style="padding:14px;margin-bottom:12px;display:flex;flex-wrap:wrap;gap:9px;align-items:center;">
    <select id="fStage" class="fsel">
      <option value="">All statuses</option>
      ${STATUSES.map(s => `<option value="${esc(s)}" ${FILTER.status === s ? "selected" : ""}>${esc(disp(s))}</option>`).join("")}
    </select>
    <select id="fOwner" class="fsel">
      <option value="">All setters</option>
      ${owners.map(o => `<option ${FILTER.setter === o ? "selected" : ""}>${esc(o)}</option>`).join("")}
    </select>
    ${(FILTER.status || FILTER.setter || FILTER.q) ? `<button id="clearBtn" class="mini-btn">✕ Clear filters</button>` : ""}
    <span class="result-count" style="margin-left:auto;"><b>${rows.length}</b> of ${LEADS.length} leads</span>
  </div>

  <div class="glass" style="overflow:hidden;">
    <div style="overflow-x:auto;max-height:calc(100vh - 250px);overflow-y:auto;">
      <table class="tbl">
        <thead><tr>
          <th style="width:44px;">#</th>
          <th data-sort="full_name">Lead ${arrow("full_name")}</th>
          <th data-sort="status">Status ${arrow("status")}</th>
          <th data-sort="sender_name">Setter ${arrow("sender_name")}</th>
          <th data-sort="followers_count">Followers</th>
          <th>Phone</th>
          <th>FU</th>
          <th data-sort="next_touchpoint">Next TP ${arrow("next_touchpoint")}</th>
          <th data-sort="updated">Updated ${arrow("updated")}</th>
        </tr></thead>
        <tbody>
          ${rows.length ? rows.map(rowHtml).join("") : `
          <tr><td colspan="9"><div class="empty-wrap" style="padding:44px 16px;">
            <span class="empty-ico" style="font-size:34px;">🔍</span>
            <div style="font-weight:700;">No leads match your filters</div>
            <div style="color:var(--txt-3);font-size:12.5px;margin-top:4px;">Try clearing a filter or changing your search.</div>
          </div></td></tr>`}
        </tbody>
      </table>
    </div>
  </div>`;
  wireTableEvents();
}

function rowHtml(l) {
  const name = l.full_name || l.user_name;
  return `
  <tr class="lead-row" data-user="${esc(l.user_name)}">
    <td class="mono" style="color:var(--txt-3);">${l.lead_number}</td>
    <td>
      <div style="display:flex;align-items:center;gap:8px;min-width:170px;">
        <span class="avatar" style="width:24px;height:24px;font-size:9.5px;background:${avColor(l.user_name)}">${esc(initials(name))}</span>
        <div style="min-width:0;">
          <div style="font-weight:650;font-size:13px;">${esc(name)}</div>
          <a class="mono" href="${esc(l.profile_link || "#")}" target="_blank" rel="noopener" style="font-size:11px;color:var(--brand);text-decoration:none;">${esc(l.user_name)}</a>
        </div>
      </div>
    </td>
    <td>${statusChip(l.status)}</td>
    <td>${setterPill(l.sender_name)}</td>
    <td class="mono" style="color:var(--txt-2);">${esc(l.followers_count || "—")}</td>
    <td class="mono" style="font-size:12px;">${l.number ? esc(l.number) : `<span style="opacity:.35;">—</span>`}${l.replied === "Yes" ? ` <span title="replied">💬</span>` : ""}</td>
    <td>${fuProgress(l)}</td>
    <td class="mono" style="font-size:12px;color:${l.next_touchpoint ? "var(--brand)" : "var(--txt-3)"};">${esc(l.next_touchpoint || "—")}</td>
    <td style="font-size:11.5px;color:var(--txt-3);white-space:nowrap;">${timeAgo(l.updated)}</td>
  </tr>`;
}
/* ============================================================
   DRAWER — full lead record (all sheet columns)
   ============================================================ */
let DRAWER_USER = null;

function openDrawer(username) {
  const l = LEADS.find(x => x.user_name === username || x.username === username);
  if (!l) { toast("Lead not found", "err"); return; }
  DRAWER_USER = l.user_name;
  renderDrawer(l);
  $("drawer").classList.add("open");
  $("backdrop").classList.add("show");
}
function closeDrawer() {
  $("drawer").classList.remove("open");
  $("backdrop").classList.remove("show");
  DRAWER_USER = null;
}

function flagBtn(l, field, label) {
  const on = l[field] === "Yes";
  return `<button class="mini-btn ${on ? "flag-on" : ""}" data-flag="${field}"
    style="${on ? "border-color:rgba(52,211,153,.55);color:var(--brand);background:rgba(52,211,153,.08);" : ""}padding:5px 10px;font-size:11.5px;">${on ? "✓" : "○"} ${label}</button>`;
}

function renderDrawer(l) {
  const fu = [1, 2, 3, 4].map(n => flagBtn(l, `follow_up_${n}`, `FU${n}`)).join("");
  $("drawerBody").innerHTML = `
  <div style="display:flex;align-items:center;gap:11px;margin-bottom:14px;">
    <span class="avatar" style="width:44px;height:44px;font-size:15px;background:${avColor(l.user_name)}">${esc(initials(l.full_name || l.user_name))}</span>
    <div style="min-width:0;flex:1;">
      <div style="font-weight:750;font-size:16px;display:flex;gap:8px;align-items:center;">
        <span class="mono" style="opacity:.55;font-size:12px;">#${l.lead_number}</span>${esc(l.full_name || l.user_name)}
      </div>
      <button class="copy-handle mono" data-copy="${esc(l.user_name)}" style="background:none;border:none;color:var(--brand);font-size:12.5px;cursor:pointer;padding:0;">${esc(l.user_name)} ⧉</button>
    </div>
    <button id="drawerClose" class="icon-btn">✕</button>
  </div>

  <div class="drawer-section">
    ${statusChip(l.status)}
    <select id="statusSelect" class="fsel" style="margin-top:9px;width:100%;">
      ${STATUSES.map(s => `<option value="${esc(s)}" ${l.status === s ? "selected" : ""}>${esc(disp(s))}</option>`).join("")}
    </select>
  </div>

  <div class="drawer-section">
    <div class="drawer-label">Contact</div>
    <div style="display:flex;flex-direction:column;gap:6px;font-size:12.5px;color:var(--txt-2);">
      ${l.profile_link ? `<a href="${esc(l.profile_link)}" target="_blank" rel="noopener" style="color:var(--brand);text-decoration:none;">🔗 ${esc(l.profile_link)}</a>` : ""}
      <span>👥 Followers: <b>${esc(l.followers_count || "—")}</b></span>
      ${l.email ? `<span>✉️ Email: <a href="mailto:${esc(l.email)}" style="color:var(--brand);">${esc(l.email)}</a></span>` : ""}
      <span>🧑‍💼 Setter: ${setterPill(l.sender_name)}</span>
      <span>🕒 1st: <b>${esc(l.first_touchpoint || "—")}</b> · last: <b>${esc(l.last_touchpoint || "—")}</b></span>
    </div>
  </div>

  <div class="drawer-section">
    <div class="drawer-label">Details</div>
    <label style="font-size:12px;color:var(--txt-3);display:block;margin-bottom:4px;">Full name</label>
    <input id="fullNameInput" class="inline-edit" value="${esc(l.full_name || "")}" placeholder="Prospect's real name"/>
    <label style="font-size:12px;color:var(--txt-3);display:block;margin:9px 0 4px;">Email</label>
    <input id="emailInput" type="email" class="inline-edit" value="${esc(l.email || "")}" placeholder="lead@example.com"/>
    <label style="font-size:12px;color:var(--txt-3);display:block;margin:9px 0 4px;">Followers count</label>
    <input id="followersInput" class="inline-edit mono" value="${esc(l.followers_count || "")}" placeholder="e.g. 12.5k"/>
    <div style="display:flex;justify-content:flex-end;margin-top:8px;">
      <button id="saveDetailsBtn" class="mini-btn" style="border-color:rgba(52,211,153,.4);color:var(--brand);">💾 Save details</button>
    </div>
  </div>

  <div class="drawer-section">
    <div class="drawer-label">Progress flags</div>
    <div style="display:flex;flex-wrap:wrap;gap:7px;">
      ${flagBtn(l, "replied", "Replied")}
      ${flagBtn(l, "number_received", "Number ✓")}
      ${fu}
      ${flagBtn(l, "discovery_call", "Discovery")}
    </div>
  </div>

  <div class="drawer-section">
    <div class="drawer-label">Phone number</div>
    <input id="numInput" class="inline-edit mono" value="${esc(l.number || "")}" placeholder="e.g. +91 98765 43210"/>
    <div style="display:flex;justify-content:flex-end;margin-top:8px;">
      <button id="saveNumBtn" class="mini-btn" style="border-color:rgba(52,211,153,.4);color:var(--brand);">💾 Save number</button>
    </div>
  </div>

  <div class="drawer-section">
    <div class="drawer-label">Dates</div>
    <label style="font-size:12px;color:var(--txt-3);display:block;margin-bottom:4px;">Next touchpoint</label>
    <input id="nextTpInput" type="date" class="inline-edit mono" value="${esc(l.next_touchpoint || "")}"/>
    <label style="font-size:12px;color:var(--txt-3);display:block;margin:9px 0 4px;">Discovery date</label>
    <input id="discDateInput" type="date" class="inline-edit mono" value="${esc(l.discovery_date || "")}"/>
    <div style="display:flex;justify-content:flex-end;margin-top:8px;">
      <button id="saveDatesBtn" class="mini-btn" style="border-color:rgba(52,211,153,.4);color:var(--brand);">💾 Save dates</button>
    </div>
  </div>

  <div class="drawer-section">
    <div class="drawer-label">Closing</div>
    <label style="font-size:12px;color:var(--txt-3);display:block;margin-bottom:4px;">Closing call status</label>
    <select id="closingSelect" class="fsel" style="width:100%;">
      <option value="" ${!l.closing_call_status ? "selected" : ""}>—</option>
      ${CLOSING_OPTS.map(o => `<option ${l.closing_call_status === o ? "selected" : ""}>${o}</option>`).join("")}
    </select>
    <label style="font-size:12px;color:var(--txt-3);display:block;margin:9px 0 4px;">Closed (Won/Lost)</label>
    <select id="closedSelect" class="fsel" style="width:100%;">
      <option value="" ${!l.closed_result ? "selected" : ""}>—</option>
      <option ${l.closed_result === "Won" ? "selected" : ""}>Won</option>
      <option ${l.closed_result === "Lost" ? "selected" : ""}>Lost</option>
    </select>
  </div>

  <div class="drawer-section">
    <div class="drawer-label">Note</div>
    <textarea id="noteArea" class="inline-edit" rows="4" placeholder="Add context about this prospect…">${esc(l.note || "")}</textarea>
    <div style="display:flex;justify-content:flex-end;margin-top:8px;">
      <button id="saveNoteBtn" class="mini-btn" style="border-color:rgba(52,211,153,.4);color:var(--brand);">💾 Save note</button>
    </div>
  </div>

  <div class="drawer-section">
    <div class="drawer-label">Reassign setter</div>
    <select id="ownerSelect" class="fsel" style="width:100%;">
      <option value="Unassigned" ${(l.sender_name || "Unassigned") === "Unassigned" ? "selected" : ""}>Unassigned</option>
      ${collectSetters().filter(o => o !== "Unassigned" && o !== l.sender_name).map(o => `<option>${esc(o)}</option>`).join("")}
      <option value="__new__">New setter…</option>
    </select>
    <input id="newOwnerInput" class="inline-edit" placeholder="@new_setter_handle" style="display:none;margin-top:7px;"/>
  </div>

  <button id="deleteLeadBtn" class="mini-btn" style="width:100%;margin-top:14px;border-color:rgba(248,113,113,.45);color:#f87171;">🗑 Delete this lead</button>`;
  wireDrawerEvents();
}

/* ---------- CSV export : exact Google Sheet column order ---------- */
const SHEET_COLS = [["lead_number", "Lead Number"], ["full_name", "Full Name (Lead)"],
  ["email", "Email"], ["user_name", "User name (Lead)"], ["profile_link", "Profile Link"],
  ["followers_count", "Followers Count"], ["sender_name", "Sender Name"],
  ["sender_profile", "Sender Profile"], ["first_touchpoint", "First Touchpoint (Date)"],
  ["note", "Note"], ["status", "Status"], ["last_touchpoint", "Last Touchpoint (Date)"],
  ["next_touchpoint", "Next Touchpoint (Date)"], ["replied", "Replied"],
  ["number_received", "Number Received"], ["number", "Number"],
  ["follow_up_1", "Follow up 1"], ["follow_up_1_date", "Follow up 1 (Date)"],
  ["follow_up_2", "Follow up 2"], ["follow_up_2_date", "Follow up 2 (Date)"],
  ["follow_up_3", "Follow up 3"], ["follow_up_3_date", "Follow up 3 (Date)"],
  ["follow_up_4", "Follow up 4"], ["follow_up_4_date", "Follow up 4 (Date)"],
  ["discovery_call", "Discovery Call"], ["discovery_date", "Discovery Date"],
  ["closing_call_status", "Closing Call Status"], ["closed_result", "Closed (Won/Lost)"]];

function exportCsv() {
  const rows = sortRows(applyFilters(LEADS));
  if (!rows.length) { toast("Nothing to export with current filters", "err"); return; }
  const q = (v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
  const csv = [SHEET_COLS.map(c => q(c[1])).join(",")]
    .concat(rows.map(r => SHEET_COLS.map(c => q(r[c[0]])).join(","))).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  a.download = "gretta-crm.csv";
  a.click(); URL.revokeObjectURL(a.href);
  toast(`Exported ${rows.length} leads`);
}

/* ---------- bot access tab ---------- */
async function loadUsers() {
  try { USERS = (await api("/api/users")).users || []; }
  catch { USERS = []; }
  if (VIEW === "access") renderAccess();
  updateBotStatus();
}
function updateBotStatus() {
  const dot = $("liveDot"), lbl = $("liveLbl"), bs = $("botStatus");
  if (!lbl) return;
  lbl.textContent = "Live";
  if (dot) dot.style.background = "#34d399";
  if (bs) bs.title = `${USERS.length} Telegram users seen`;
}
function renderAccess() {
  const el = $("view-access");
  el.innerHTML = `
  <div class="glass fade-in" style="padding:18px;max-width:760px;">
    <div style="font-weight:750;font-size:15px;margin-bottom:4px;">Bot Access</div>
    <div style="font-size:11.5px;color:var(--txt-3);margin-bottom:13px;">
      Every Telegram account that ever messaged the bot. Only whitelisted accounts can use it.</div>
    ${USERS.length ? USERS.map(u => `
      <div style="display:flex;align-items:center;gap:10px;padding:9px 2px;border-bottom:1px solid var(--line);">
        <span class="avatar" style="width:30px;height:30px;font-size:11px;background:${avColor(u.username || u.telegram_id)}">${esc(initials(u.first_name || u.username))}</span>
        <div style="flex:1;min-width:0;">
          <div style="font-size:13px;font-weight:650;">${esc(u.first_name || u.username || u.telegram_id)}</div>
          <div class="mono" style="font-size:11px;color:var(--txt-3);">${esc(u.username || u.telegram_id)} · ${u.msg_count} msgs · last ${timeAgo(u.last_seen)}</div>
        </div>
        <button class="mini-btn acc-toggle" data-id="${esc(u.telegram_id)}" data-val="${u.authorized ? "0" : "1"}"
          style="${u.authorized ? "border-color:rgba(52,211,153,.5);color:var(--brand);" : "color:var(--txt-3);"}">${u.authorized ? "✓ Allowed" : "Blocked"}</button>
      </div>`).join("") : `<div class="chart-empty">No Telegram users yet</div>`}
  </div>`;
}

/* ---------- auth gate ---------- */
let CURRENT_USER = null;

function showLogin(defaultTab = "signin") {
  const g = $("loginGate");
  if (g) {
    g.style.display = "flex";
    switchAuthTab(defaultTab);
  }
}

function hideLogin() {
  const g = $("loginGate");
  if (g) g.style.display = "none";
}

function switchAuthTab(tabName) {
  const tabs = document.querySelectorAll(".auth-tab");
  tabs.forEach(t => {
    t.classList.toggle("active", t.dataset.authTab === tabName);
  });
  
  const forms = ["signin", "setup", "reset"];
  forms.forEach(name => {
    const el = $(`auth-panel-${name}`);
    if (el) el.style.display = (name === tabName) ? "block" : "none";
  });

  setAuthFeedback("", "none");
}

function setAuthFeedback(msg, type = "err", panelId = null) {
  const feedbackEls = document.querySelectorAll(".auth-feedback");
  feedbackEls.forEach(el => {
    if (!panelId || el.id === panelId) {
      el.className = `auth-feedback ${type}`;
      el.textContent = msg;
      el.style.display = msg ? "block" : "none";
    }
  });
}

function updateSidebarUser(user) {
  CURRENT_USER = user;
  const userProfile = $("sidebarUserProfile");
  const userEmail = $("sidebarUserEmail");
  const userAvatar = $("sidebarUserAvatar");
  const logoutBtn = $("logoutBtn");

  if (user && user.email) {
    if (userProfile) userProfile.style.display = "flex";
    if (userEmail) userEmail.textContent = user.name || user.email;
    if (userAvatar) {
      userAvatar.textContent = initials(user.name || user.email);
      userAvatar.style.background = avColor(user.email);
    }
    if (logoutBtn) logoutBtn.style.display = "flex";
  } else {
    if (userProfile) userProfile.style.display = "none";
    if (logoutBtn) logoutBtn.style.display = "none";
  }
}

async function initAuth() {
  const urlParams = new URLSearchParams(window.location.search);
  const hashParams = new URLSearchParams(window.location.hash.replace(/^#/, "?"));
  const resetToken = urlParams.get("token") || hashParams.get("token") || urlParams.get("reset_token");

  try {
    const st = await api("/api/auth/status");
    AUTH_REQUIRED = st.auth_required;
    if (st.authenticated && st.user) {
      updateSidebarUser(st.user);
      hideLogin();
      return true;
    }

    if (resetToken) {
      showLogin("reset");
      const tokenInput = $("resetTokenInput");
      if (tokenInput) tokenInput.value = resetToken;
      const step2 = $("resetStep2");
      if (step2) step2.style.display = "block";
      const step1 = $("resetStep1");
      if (step1) step1.style.display = "none";
      setAuthFeedback("Reset token detected from email link. Enter your new password below.", "success");
      return false;
    }

    if (st.auth_required && !st.authenticated) {
      showLogin("signin");
      return false;
    }
    hideLogin();
    return true;
  } catch (e) {
    if (resetToken) {
      showLogin("reset");
      const tokenInput = $("resetTokenInput");
      if (tokenInput) tokenInput.value = resetToken;
      const step2 = $("resetStep2");
      if (step2) step2.style.display = "block";
      const step1 = $("resetStep1");
      if (step1) step1.style.display = "none";
      return false;
    }
    showLogin("signin");
    return false;
  }
}
function wireLogin() {
  $("loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("loginErr").textContent = "";
    try {
      await api("/api/auth/login", { method: "POST",
        body: { email: $("loginUser").value, password: $("loginPass").value } });
      hideLogin(); await load();
    } catch (err) { $("loginErr").textContent = err.message; }
  });
  $("logoutBtn").addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST" }); } catch {}
    LEADS = []; STATS = {}; render(); showLogin();
  });
}

/* ---------- data loading / polling ---------- */
async function load({ silent = false } = {}) {
  if (LOADING) return;
  LOADING = true;
  try {
    const data = await api("/api/leads");
    const nextLeads = data.leads || [];
    const nextStats = data.stats || {};
    const nextSignature = JSON.stringify([nextLeads, nextStats]);
    const changed = nextSignature !== DATA_SIGNATURE;
    LEADS = nextLeads;
    STATS = nextStats;
    DATA_SIGNATURE = nextSignature;
    DATA_READY = true;
    LAST_ERR_TOAST = 0;
    // A silent poll should be invisible when nothing changed. Rebuilding the
    // whole view here was the source of the visible blink every five seconds.
    if (changed || !silent) render();
  } catch (err) {
    if (!silent && Date.now() - LAST_ERR_TOAST > 30000) {
      toast(`Load failed: ${err.message}`, "err");
      LAST_ERR_TOAST = Date.now();
    }
  } finally {
    LOADING = false;
  }
}
function startPolling() {
  clearTimeout(POLL_HANDLE);
  const poll = async () => {
    if (!DRAWER_USER) await load({ silent: true });
    POLL_HANDLE = setTimeout(poll, 5000);
  };
  POLL_HANDLE = setTimeout(poll, 5000);
}

/* ---------- theme / render root ---------- */
function applyTheme(mode) {
  document.documentElement.dataset.theme = mode;
  localStorage.setItem("gretta-theme", mode);
  $("themeBtn").textContent = mode === "light" ? "🌙" : "☀️";
  document.querySelectorAll("[data-light-logo][data-dark-logo]").forEach((logo) => {
    logo.src = mode === "light" ? logo.dataset.lightLogo : logo.dataset.darkLogo;
  });
}
function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  if (VIEW === "overview") renderOverview();
}
function render() {
  if (VIEW === "overview") renderOverview();
  else if (VIEW === "team") renderTeam();
  else if (VIEW === "leads") renderLeads();
  else if (VIEW === "board") renderBoard();
  else if (VIEW === "access") renderAccess();
}
function switchView(v) {
  VIEW = v;
  document.querySelectorAll(".view").forEach(x => {
    x.classList.remove("active");
    x.style.display = "none";       // inline fallback so stale styles can't win
  });
  const target = $(`view-${v}`);
  if (target) { target.classList.add("active"); target.style.display = "block"; }
  document.querySelectorAll(".nav-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.view === v));
  history.replaceState(null, "", `#${v}`);
  render();
  if (v === "access") loadUsers();
}
function viewFromHash() {
  const v = location.hash.replace("#", "");
  return ["overview", "team", "leads", "board", "access"].includes(v) ? v : "overview";
}

/* ---------- command palette ---------- */
function openCmdk() { $("cmdk").classList.add("show"); $("cmdkInput").value = ""; buildCmdk(""); $("cmdkInput").focus(); }
function closeCmdk() { $("cmdk").classList.remove("show"); }
function buildCmdk(q) {
  q = q.trim().toLowerCase();
  const views = [["overview", "📊 Overview"], ["team", "👥 Team"], ["leads", "🗂 Leads table"],
                 ["board", "📌 Board"], ["access", "🔐 Bot access"]]
    .filter(([v]) => !q || v.includes(q) );
  const leads = LEADS.filter(l => !q || `${l.user_name} ${l.full_name}`.toLowerCase().includes(q))
    .slice(0, 8);
  $("cmdkList").innerHTML =
    views.map(([v, label]) => `<button class="cmdk-item" data-goto="${v}">${label}</button>`).join("") +
    leads.map(l => `<button class="cmdk-item" data-open-user="${esc(l.user_name)}">👤 ${esc(l.user_name)} <span style="opacity:.5;">· ${esc(disp(l.status))}</span></button>`).join("");
}

/* ---------- lead actions ---------- */
async function moveLead(username, status) {
  try {
    await api("/api/lead/stage", { method: "POST", body: { username, stage: status } });
    toast(`→ ${disp(status)}`);
    await load({ silent: true });
    if (DRAWER_USER) openDrawer(DRAWER_USER); else if (VIEW !== "overview") render();
  } catch (err) { toast(err.message, "err"); }
}
async function doReassign(name) {
  try {
    await api("/api/lead/owner", { method: "POST", body: { username: DRAWER_USER, owner: name } });
    toast(`Setter → ${name}`);
    await load({ silent: true }); openDrawer(DRAWER_USER);
  } catch (err) { toast(err.message, "err"); }
}

/* ---------- event wiring ---------- */
function wireDrawerEvents() {
  const body = $("drawerBody");
  body.querySelector("#drawerClose").addEventListener("click", closeDrawer);
  body.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast("Handle copied"); }
    catch { toast("Copy failed", "err"); }
  }));
  body.querySelector("#statusSelect").addEventListener("change", async (e) => {
    await moveLead(DRAWER_USER, e.target.value);
  });
  body.querySelectorAll("[data-flag]").forEach(btn => btn.addEventListener("click", async () => {
    const l = LEADS.find(x => x.user_name === DRAWER_USER);
    const field = btn.dataset.flag;
    const next = l[field] === "Yes" ? "" : "Yes";
    try {
      await api("/api/lead/update", { method: "POST", body: { username: DRAWER_USER, [field]: next } });
      await load({ silent: true }); openDrawer(DRAWER_USER);
    } catch (err) { toast(err.message, "err"); }
  }));
  body.querySelector("#saveDetailsBtn").addEventListener("click", async () => {
    try {
      await api("/api/lead/update", { method: "POST", body: {
        username: DRAWER_USER,
        full_name: $("fullNameInput").value.trim(),
        email: $("emailInput").value.trim(),
        followers_count: $("followersInput").value.trim(),
      } });
      toast("Details saved"); await load({ silent: true }); openDrawer(DRAWER_USER);
    } catch (err) { toast(err.message, "err"); }
  });
  body.querySelector("#saveNumBtn").addEventListener("click", async () => {
    try {
      await api("/api/lead/update", { method: "POST", body: { username: DRAWER_USER, number: $("numInput").value.trim(), number_received: $("numInput").value.trim() ? "Yes" : "" } });
      toast("Number saved"); await load({ silent: true }); openDrawer(DRAWER_USER);
    } catch (err) { toast(err.message, "err"); }
  });
  body.querySelector("#saveDatesBtn").addEventListener("click", async () => {
    try {
      await api("/api/lead/update", { method: "POST", body: { username: DRAWER_USER, next_touchpoint: $("nextTpInput").value, discovery_date: $("discDateInput").value } });
      toast("Dates saved"); await load({ silent: true }); openDrawer(DRAWER_USER);
    } catch (err) { toast(err.message, "err"); }
  });
  body.querySelector("#closingSelect").addEventListener("change", async (e) => {
    try {
      await api("/api/lead/update", { method: "POST", body: { username: DRAWER_USER, closing_call_status: e.target.value } });
      toast("Closing status saved"); await load({ silent: true });
    } catch (err) { toast(err.message, "err"); }
  });
  body.querySelector("#closedSelect").addEventListener("change", async (e) => {
    try {
      await api("/api/lead/update", { method: "POST", body: { username: DRAWER_USER, closed_result: e.target.value } });
      toast(`Closed: ${e.target.value || "reopened"}`); await load({ silent: true });
    } catch (err) { toast(err.message, "err"); }
  });
  body.querySelector("#saveNoteBtn").addEventListener("click", async () => {
    try {
      await api("/api/lead/update", { method: "POST", body: { username: DRAWER_USER, note: $("noteArea").value } });
      toast("Note saved"); await load({ silent: true }); openDrawer(DRAWER_USER);
    } catch (err) { toast(err.message, "err"); }
  });
  body.querySelector("#ownerSelect").addEventListener("change", async (e) => {
    if (e.target.value === "__new__") { $("newOwnerInput").style.display = "block"; $("newOwnerInput").focus(); return; }
    await doReassign(e.target.value);
  });
  body.querySelector("#newOwnerInput").addEventListener("keydown", async (e) => {
    if (e.key === "Enter" && e.target.value.trim()) await doReassign(e.target.value.trim());
  });
  body.querySelector("#deleteLeadBtn").addEventListener("click", async () => {
    if (!confirm("Delete this lead from the CRM? The Google Sheet mirror updates on the next sync.")) return;
    const victim = DRAWER_USER;
    closeDrawer();
    try {
      await api("/api/lead/delete", { method: "POST", body: { username: victim } });
      toast("Lead deleted"); await load({ silent: true });
    } catch (err) { toast(err.message, "err"); }
  });
}

function wireEvents() {
  document.querySelectorAll(".nav-btn").forEach(b => b.addEventListener("click", () => switchView(b.dataset.view)));
  $("csvBtn").addEventListener("click", exportCsv);
  $("themeBtn").addEventListener("click", toggleTheme);
  $("q").addEventListener("input", (e) => {
    FILTER.q = e.target.value;
    clearTimeout(window.__qT);
    window.__qT = setTimeout(render, 180);
  });
  $("hamburger").addEventListener("click", () => $("sidebar").classList.toggle("open"));
  // Auto-close the mobile sidebar whenever a nav destination is picked.
  // NOTE: #sidebar is an ID, not a class — the old ".sidebar .nav-btn"
  // selector matched nothing, which is why the menu never closed itself.
  document.querySelectorAll("#sidebar .nav-btn").forEach(el =>
    el.addEventListener("click", () => { if (window.innerWidth <= 900) $("sidebar").classList.remove("open"); }));

  document.addEventListener("click", async (e) => {
    const ttab = e.target.closest("[data-team-tab]");
    if (ttab) {
      TEAM_TAB = ttab.dataset.teamTab || "";
      renderTeam();
      if (window.innerWidth <= 900) $("sidebar").classList.remove("open");
      return;
    }
    const open = e.target.closest(".open-lead, .lead-row");
    if (open) { openDrawer(open.dataset.open || open.dataset.user); return; }
    const frow = e.target.closest(".funnel-row");
    if (frow) {
      FILTER.status = FILTER.status === frow.dataset.fstatus ? "" : frow.dataset.fstatus;
      render(); return;
    }
    const goto = e.target.closest("[data-goto]");
    if (goto) { closeCmdk(); switchView(goto.dataset.goto); return; }
    const ou = e.target.closest("[data-open-user]");
    if (ou) { closeCmdk(); openDrawer(ou.dataset.openUser); return; }
    const acc = e.target.closest(".acc-toggle");
    if (acc) {
      try {
        await api("/api/user/access", { method: "POST", body: { user_id: acc.dataset.id, authorized: acc.dataset.val === "1" } });
        toast("Access updated"); await loadUsers();
      } catch (err) { toast(err.message, "err"); }
    }
  });

  $("backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "k") { e.preventDefault(); openCmdk(); }
    if (e.key === "Escape") { closeCmdk(); closeDrawer(); }
  });
  $("cmdkInput").addEventListener("input", (e) => buildCmdk(e.target.value));
  $("cmdk").addEventListener("click", (e) => { if (e.target.id === "cmdk") closeCmdk(); });
}

function wireTableEvents() {
  document.querySelectorAll("th[data-sort]").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.sort;
    if (SORT.key === k) SORT.dir *= -1; else { SORT.key = k; SORT.dir = 1; }
    renderLeads();
  }));
  const fs = $("fStage"), fo = $("fOwner"), cb = $("clearBtn");
  if (fs) fs.addEventListener("change", (e) => { FILTER.status = e.target.value; renderLeads(); });
  if (fo) fo.addEventListener("change", (e) => { FILTER.setter = e.target.value; renderLeads(); });
  if (cb) cb.addEventListener("click", () => { FILTER = { status: "", setter: "", q: "" }; $("q").value = ""; renderLeads(); });
}

function wireDragAndDrop() {
  let dragged = null;
  document.querySelectorAll(".lead-card").forEach(card => {
    card.addEventListener("dragstart", () => { dragged = card; card.classList.add("dragging"); });
    card.addEventListener("dragend", () => { card.classList.remove("dragging"); });
  });
  document.querySelectorAll("[data-stage-drop]").forEach(zone => {
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drop-hover"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drop-hover"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault(); zone.classList.remove("drop-hover");
      if (dragged) moveLead(dragged.dataset.user, zone.dataset.stageDrop);
      dragged = null;
    });
  });
}

/* ---------- boot ---------- */
(async function main() {
  applyTheme(localStorage.getItem("gretta-theme") || "dark");
  wireEvents();
  wireLogin();
  switchView(viewFromHash());
  if (await initAuth()) {
    await load();
    await loadUsers();
    startPolling();
  }
})();
