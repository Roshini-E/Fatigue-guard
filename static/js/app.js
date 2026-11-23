/* FatigueGuard front-end — same screens, classes and behaviour as the original Next.js UI.
   All detection (OpenCV + MediaPipe), e-mail alerts and database writes happen in Python;
   this file only renders the UI and plays the siren. */
'use strict'

/* ------------------------------------------------------------------ helpers */
const $ = (id) => document.getElementById(id)
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
const ic = (name, cls = '') => `<i data-lucide="${name}" class="${cls}" aria-hidden="true"></i>`
const icons = () => { try { window.lucide && lucide.createIcons() } catch (e) {} }

async function api(path, opts = {}) {
  const o = { method: opts.method || 'GET', headers: {}, credentials: 'same-origin' }
  if (opts.body !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(opts.body) }
  try {
    const res = await fetch(path, o)
    let data = null
    try { data = await res.json() } catch (e) { data = {} }
    return { ok: res.ok, status: res.status, data }
  } catch (e) {
    return { ok: false, status: 0, data: { error: 'Network error — please try again' } }
  }
}

const LEVELS = ['ALERT', 'CAUTION', 'WARNING', 'CRITICAL']
const SEGMENT_WIDTH = { ALERT: 28, CAUTION: 24, WARNING: 26, CRITICAL: 22 }
const LEVEL_STYLES = {
  ALERT:    { text: 'text-emerald-400', bg: 'bg-emerald-500/10', border: 'border-emerald-500/40', dot: 'bg-emerald-400', hex: '#34d399' },
  CAUTION:  { text: 'text-amber-400',   bg: 'bg-amber-500/10',   border: 'border-amber-500/40',   dot: 'bg-amber-400',   hex: '#fbbf24' },
  WARNING:  { text: 'text-orange-500',  bg: 'bg-orange-500/10',  border: 'border-orange-500/40',  dot: 'bg-orange-500',  hex: '#f97316' },
  CRITICAL: { text: 'text-red-500',     bg: 'bg-red-500/10',     border: 'border-red-500/50',     dot: 'bg-red-500',     hex: '#ef4444' },
}
const eventTypeLabel = (t) => t.split('_').map((w) => w.charAt(0) + w.slice(1).toLowerCase()).join(' ')
const IST_OPTS = { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }
const formatIST = (iso) => new Date(iso).toLocaleString('en-IN', IST_OPTS)
function formatDuration(s) {
  const sec = Math.max(0, Math.round(s)), h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), r = sec % 60
  const p = (n) => String(n).padStart(2, '0')
  return h > 0 ? `${h}h ${p(m)}m ${p(r)}s` : `${m}m ${p(r)}s`
}
const num = (v) => (typeof v === 'number' && isFinite(v) ? v : 0)

/* ------------------------------------------------------------------ toasts (limit 1, like the original) */
function toast({ title, description, variant }) {
  const box = $('toasts')
  box.innerHTML = ''
  const el = document.createElement('div')
  el.className = 'toast' + (variant === 'destructive' ? ' destructive' : '')
  el.innerHTML = `<div class="grid gap-1"><div class="t-title">${esc(title)}</div>${description ? `<div class="t-desc">${esc(description)}</div>` : ''}</div>
    <button class="t-close" aria-label="Close">${ic('x', 'h-4 w-4')}</button>`
  el.querySelector('.t-close').onclick = () => el.remove()
  box.appendChild(el)
  icons()
  setTimeout(() => { el.classList.add('leaving'); setTimeout(() => el.remove(), 220) }, 5000)
}

/* ------------------------------------------------------------------ siren (Web Audio, as in alarm.ts) */
function audioCtx() {
  if (!window.__fgCtx) { const C = window.AudioContext || window.webkitAudioContext; window.__fgCtx = new C() }
  return window.__fgCtx
}
const siren = {
  osc: null, lfo: null, gain: null,
  get playing() { return this.osc !== null },
  start() {
    if (this.osc) return
    try {
      const ctx = audioCtx(); ctx.resume().catch(() => {})
      const osc = ctx.createOscillator(); osc.type = 'sawtooth'; osc.frequency.value = 720
      const gain = ctx.createGain(); gain.gain.setValueAtTime(0, ctx.currentTime)
      gain.gain.linearRampToValueAtTime(0.18, ctx.currentTime + 0.3)
      const lfo = ctx.createOscillator(); lfo.type = 'square'; lfo.frequency.value = 2.4   // two-tone wail
      const lfoGain = ctx.createGain(); lfoGain.gain.value = 160
      lfo.connect(lfoGain); lfoGain.connect(osc.frequency)
      osc.connect(gain); gain.connect(ctx.destination)
      osc.start(); lfo.start()
      this.osc = osc; this.lfo = lfo; this.gain = gain
    } catch (e) { /* audio unavailable — visual alarm still works */ }
  },
  stop() {
    try { this.osc && this.osc.stop(); this.lfo && this.lfo.stop(); this.gain && this.gain.disconnect() } catch (e) {}
    this.osc = this.lfo = this.gain = null
  },
}
function startAlarm() { siren.start() }
function stopAlarm() { siren.stop() }

/* ------------------------------------------------------------------ app state */
const app = { driver: null, vehicle: null, tab: 'monitor' }
const EMPTY_METRICS = { risk: 0, level: 'ALERT', perclos: 0, ear: 0, baselineEar: 0, thresholdEar: 0, eyesClosedS: 0, blinks: 0, slowBlinks: 0,
  microsleeps: 0, yawns: 0, headNods: 0, distractions: 0, absences: 0, headPitchDeg: 0, headYawDeg: 0, monitoredS: 0, faceDetected: false }
const mv = { phase: 'idle', mode: null, metrics: { ...EMPTY_METRICS }, events: [], cameraError: '', muted: false, sirenActive: false,
  alarmActive: false, stateSeq: -1, sessionId: null, sessionGeneration: 0, apiLatencyMs: 0, renderCostMs: 0, backendResponseAt: null, frontendUpdatedAt: null,
  alertInfo: null, alertSeq: 0, toastedSeq: 0, contacts: [], poll: null, eventsSig: '' }

const root = () => $('root')

/* ------------------------------------------------------------------ boot */
async function boot() {
  root().innerHTML = `<div class="flex min-h-screen items-center justify-center bg-zinc-950 text-zinc-500">
    <div class="flex flex-col items-center gap-3">${ic('shield-check', 'h-8 w-8 animate-pulse text-amber-400')}
    <p class="text-xs uppercase tracking-[0.3em]">FatigueGuard</p></div></div>`
  icons()
  const r = await api('/api/auth/me')
  if (r.ok && r.data.driver) { app.driver = r.data.driver; app.vehicle = r.data.vehicle; showDashboard() } else showLogin()
}

/* ================================================================== LOGIN */
const FEATURES = [
  ['eye', 'Computer-vision monitoring', 'EAR calibration, PERCLOS, microsleep & head-posture tracking — all on-device.'],
  ['activity', '4-level risk escalation', 'ALERT → CAUTION → WARNING → CRITICAL, scored 0–100 in real time.'],
  ['bell-ring', 'Instant wake-up siren', 'The moment drowsiness is detected, an in-cabin alarm rings to wake the driver.'],
  ['mail', 'Automatic emergency email', 'Emergency contacts are emailed in the background at the same time.'],
  ['line-chart', 'Every trip, saved', 'Blinks, yawns, nods, risk curve and level time — stored per session as reports.'],
]

function showLogin() {
  root().innerHTML = `
  <div class="relative min-h-screen overflow-hidden bg-zinc-950 text-zinc-100">
    <div aria-hidden class="pointer-events-none absolute inset-0" style="background: radial-gradient(900px 500px at 15% 0%, rgba(251,191,36,0.07), transparent 60%), radial-gradient(700px 500px at 90% 100%, rgba(239,68,68,0.06), transparent 60%)"></div>
    <div class="relative mx-auto grid min-h-screen max-w-6xl items-center gap-10 px-4 py-10 lg:grid-cols-2 lg:gap-16 lg:py-0">
      <section class="order-2 lg:order-1">
        <div class="flex items-center gap-3">
          <div class="flex h-11 w-11 items-center justify-center rounded-xl border border-amber-500/40 bg-amber-500/10">${ic('shield-check', 'h-6 w-6 text-amber-400')}</div>
          <div>
            <h1 class="text-2xl font-bold tracking-tight">Fatigue<span class="text-amber-400">Guard</span></h1>
            <p class="text-xs uppercase tracking-[0.25em] text-zinc-500">Driver Drowsiness Monitoring</p>
          </div>
        </div>
        <p class="mt-6 max-w-lg text-3xl font-bold leading-snug sm:text-4xl">An AI co-pilot that <span class="text-amber-400">never blinks</span>, so you safely can.</p>
        <p class="mt-3 max-w-md text-sm leading-relaxed text-zinc-400">FatigueGuard watches the driver through the camera, scores fatigue in real time, rings a wake-up siren on drowsiness and emails your emergency contacts — all in the same second.</p>
        <ul class="mt-8 space-y-4">
          ${FEATURES.map(([i, t, d]) => `<li class="flex gap-3">
            <div class="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-zinc-800 bg-zinc-900">${ic(i, 'h-4 w-4 text-amber-400')}</div>
            <div><p class="text-sm font-semibold">${t}</p><p class="text-xs text-zinc-500">${d}</p></div></li>`).join('')}
        </ul>
      </section>

      <section class="order-1 lg:order-2">
        <div class="mx-auto w-full max-w-md rounded-2xl border border-zinc-800 bg-zinc-900/80 p-6 shadow-2xl shadow-black/40 backdrop-blur">
          <div class="tabs-default flex flex-col gap-2">
            <div class="tabs-list grid grid-cols-2 bg-zinc-950" id="auth-tabs">
              <button class="tab-trigger active" data-t="login">Sign in</button>
              <button class="tab-trigger" data-t="register">Register</button>
            </div>

            <div id="auth-login" class="mt-5 space-y-4">
              <form id="login-form" class="space-y-4">
                <div class="space-y-1.5"><label class="fg-label" for="login-username">Username</label>
                  <input class="fg-input" id="login-username" autocomplete="username" placeholder="driver1" required></div>
                <div class="space-y-1.5"><label class="fg-label" for="login-password">Password</label>
                  <input class="fg-input" id="login-password" type="password" autocomplete="current-password" placeholder="••••••••" required></div>
                <p class="text-xs text-red-400 auth-error"></p>
                <button type="submit" class="btn w-full bg-amber-500 text-zinc-950 hover:bg-amber-400" id="login-btn">Sign in</button>
              </form>
              <div class="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3 text-xs text-zinc-500"><span class="font-semibold text-zinc-400">Demo account:</span> driver1 · demo1234</div>
            </div>

            <div id="auth-register" class="mt-5 space-y-4 hidden-view">
              <form id="reg-form" class="space-y-4">
                <div class="space-y-1.5"><label class="fg-label" for="reg-username">Username</label>
                  <input class="fg-input" id="reg-username" placeholder="e.g. rahul" required minlength="3"></div>
                <div class="space-y-1.5"><label class="fg-label" for="reg-email">Email <span class="text-zinc-500">(optional)</span></label>
                  <input class="fg-input" id="reg-email" type="email" placeholder="you@example.com"></div>
                <div class="space-y-1.5"><label class="fg-label" for="reg-pass">Password</label>
                  <input class="fg-input" id="reg-pass" type="password" placeholder="min. 6 characters" required minlength="6"></div>
                <div class="space-y-1.5"><label class="fg-label" for="reg-vehicle">Vehicle registration <span class="text-zinc-500">(optional)</span></label>
                  <input class="fg-input" id="reg-vehicle" placeholder="KA-01-AB-1234"></div>
                <p class="text-xs text-red-400 auth-error"></p>
                <button type="submit" class="btn w-full bg-amber-500 text-zinc-950 hover:bg-amber-400" id="reg-btn">Create driver account</button>
              </form>
              <div class="flex items-start gap-2 text-xs text-zinc-500">${ic('user', 'mt-0.5 h-3.5 w-3.5 shrink-0')}
                <p>Each driver gets a private dashboard: live monitoring, fatigue reports and emergency contacts.</p></div>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>`
  icons()

  document.querySelectorAll('#auth-tabs .tab-trigger').forEach((b) => (b.onclick = () => {
    document.querySelectorAll('#auth-tabs .tab-trigger').forEach((x) => x.classList.toggle('active', x === b))
    $('auth-login').classList.toggle('hidden-view', b.dataset.t !== 'login')
    $('auth-register').classList.toggle('hidden-view', b.dataset.t !== 'register')
    document.querySelectorAll('.auth-error').forEach((e) => (e.textContent = ''))
  }))

  async function submit(path, payload, btn, busyText, idleText) {
    document.querySelectorAll('.auth-error').forEach((e) => (e.textContent = ''))
    btn.disabled = true; btn.textContent = busyText
    const r = await api(`/api/auth/${path}`, { method: 'POST', body: payload })
    btn.disabled = false; btn.textContent = idleText
    if (!r.ok) { document.querySelectorAll('.auth-error').forEach((e) => (e.textContent = r.data.error || 'Something went wrong')); return }
    app.driver = r.data.driver; app.vehicle = null
    const me = await api('/api/auth/me')               // pick up vehicle registered at signup
    if (me.ok && me.data.driver) { app.driver = me.data.driver; app.vehicle = me.data.vehicle }
    showDashboard()
  }
  $('login-form').onsubmit = (e) => { e.preventDefault()
    submit('login', { username: $('login-username').value, password: $('login-password').value }, $('login-btn'), 'Signing in…', 'Sign in') }
  $('reg-form').onsubmit = (e) => { e.preventDefault()
    submit('register', { username: $('reg-username').value, password: $('reg-pass').value, email: $('reg-email').value,
      registrationNumber: $('reg-vehicle').value }, $('reg-btn'), 'Creating account…', 'Create driver account') }
}

/* ================================================================== DASHBOARD */
let refreshKey = 0
const TABS = [['monitor', 'activity', 'Monitor'], ['reports', 'file-bar-chart', 'Reports'], ['contacts', 'users', 'Contacts'], ['alerts', 'mail-warning', 'Alert Log']]

function displayName() { return /^[0-9a-f]{32}$/i.test(app.driver.name || '') ? app.driver.username : (app.driver.name || app.driver.username) }

function showDashboard() {
  const d = app.driver, v = app.vehicle
  root().innerHTML = `
  <div class="flex min-h-screen flex-col bg-zinc-950 text-zinc-100">
    <header class="sticky top-0 z-20 border-b border-zinc-800 bg-zinc-950/90 backdrop-blur">
      <div class="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3">
        <div class="flex items-center gap-2.5">
          <div class="flex h-9 w-9 items-center justify-center rounded-lg border border-amber-500/40 bg-amber-500/10">${ic('shield-check', 'h-5 w-5 text-amber-400')}</div>
          <div><p class="text-sm font-bold leading-tight">Fatigue<span class="text-amber-400">Guard</span></p>
            <p class="text-[10px] uppercase tracking-[0.2em] text-zinc-500">Driver Safety Copilot</p></div>
        </div>
        <div class="flex items-center gap-2">
          ${v ? `<span class="hidden items-center gap-1.5 rounded-full border border-zinc-800 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-400 sm:inline-flex">${ic('car', 'h-3.5 w-3.5 text-zinc-500')}<span class="font-mono">${esc(v.registrationNumber)}</span></span>` : ''}
          <span class="inline-flex items-center gap-1.5 rounded-full border border-zinc-800 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-300">${ic('activity', 'h-3.5 w-3.5 text-amber-400')}${esc(displayName())}</span>
          <button class="btn btn-sm btn-outline border-zinc-700 text-zinc-400 hover:text-zinc-200" id="logout-btn">${ic('log-out', 'mr-1 h-3.5 w-3.5')} Logout</button>
        </div>
      </div>
    </header>
    <main class="mx-auto w-full max-w-6xl flex-1 px-4 py-4">
      <div class="tabs-amber">
        <div class="tabs-list mb-4 grid w-full max-w-xl grid-cols-4 bg-zinc-900" id="dash-tabs">
          ${TABS.map(([k, i, l]) => `<button class="tab-trigger ${k === app.tab ? 'active' : ''}" data-tab="${k}">${ic(i, 'h-3.5 w-3.5')}<span class="hidden sm:inline">${l}</span></button>`).join('')}
        </div>
      </div>
      <div id="view-monitor"></div>
      <div id="view-reports" class="hidden-view"></div>
      <div id="view-contacts" class="hidden-view"></div>
      <div id="view-alerts" class="hidden-view"></div>
    </main>
    <footer class="mt-auto border-t border-zinc-900 pb-[env(safe-area-inset-bottom)]">
      <div class="mx-auto flex max-w-6xl items-center justify-between px-4 py-3 text-[11px] text-zinc-600">
        <p>FatigueGuard — webcam drowsiness detection · events &amp; reports stored per driver</p>
        <p class="font-mono">driver: ${esc(d.id.slice(0, 12))}…</p>
      </div>
    </footer>
  </div>`
  icons()
  $('logout-btn').onclick = logout
  document.querySelectorAll('#dash-tabs .tab-trigger').forEach((b) => (b.onclick = () => setTab(b.dataset.tab)))

  mountMonitor(); mountReports(); mountContacts(); mountAlerts()
  setTab(app.tab)
}

function setTab(t) {
  app.tab = t
  document.querySelectorAll('#dash-tabs .tab-trigger').forEach((b) => b.classList.toggle('active', b.dataset.tab === t))
  for (const [k] of TABS) $('view-' + k).classList.toggle('hidden-view', k !== t)
}

async function logout() {
  mv.sessionGeneration++
  stopPolling(); stopAlarm()
  await api('/api/auth/logout', { method: 'POST' })
  app.driver = null; app.vehicle = null
  mv.phase = 'idle'; mv.mode = null; mv.metrics = { ...EMPTY_METRICS }; mv.events = []; mv.alertInfo = null; mv.sirenActive = false; mv.muted = false
  showLogin()
}

function sessionEnded() {
  refreshKey++
  loadReports(); loadAlerts()
  toast({ title: 'Reports & alert log updated' })
}

/* ================================================================== MONITOR VIEW */
const EYE_L = [33, 160, 158, 133, 153, 144]   // (landmark overlay is drawn by OpenCV on the server)

function mountMonitor() {
  $('view-monitor').innerHTML = `
  <div class="space-y-4">
    <div id="mv-banner"></div>
    <div class="grid gap-4 lg:grid-cols-5">
      <div id="mv-cam" class="rounded-2xl border bg-zinc-900/60 p-4 lg:col-span-3 border-zinc-800">
        <div class="mb-3 flex items-center justify-between">
          <h2 class="text-sm font-semibold text-zinc-300">Live Monitoring</h2>
          <div class="flex items-center gap-2 text-xs" id="mv-head"></div>
        </div>
        <div class="relative aspect-[4/3] w-full overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950">
          <img id="mv-img" alt="" class="absolute inset-0 h-full w-full object-cover invisible">
          <div id="mv-overlays"></div>
        </div>
        <div id="mv-error"></div>
        <div class="mt-3 flex flex-wrap items-center gap-2" id="mv-controls"></div>
        <div class="mt-4">
          <p class="mb-1.5 text-[11px] uppercase tracking-widest text-zinc-500">Risk escalation</p>
          <div class="flex gap-1" id="mv-strip"></div>
        </div>
      </div>
      <div class="space-y-4 lg:col-span-2">
        <div class="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4">
          <h2 class="mb-1 text-sm font-semibold text-zinc-300">Fatigue Risk</h2>
          <div id="mv-gauge"></div>
          <div id="mv-calib"></div>
        </div>
        <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-2" id="mv-metrics"></div>
        <div class="flex flex-wrap gap-2" id="mv-chips"></div>
        <div id="mv-contacts-note"></div>
      </div>
    </div>
    <div class="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4">
      <h2 class="mb-2 text-sm font-semibold text-zinc-300">Event feed</h2>
      <div id="mv-events"></div>
    </div>
  </div>`
  loadMonitorContacts()
  renderMonitor()
  api('/api/monitor/state').then((r) => { if (r.ok && r.data.phase !== 'idle') { applyState(r.data); startPolling() } })   // resume a running session
}

function loadMonitorContacts() {
  api('/api/contacts/list').then((r) => { mv.contacts = r.ok ? r.data.contacts || [] : []; renderContactsNote() })
}

/* ---- risk gauge (port of risk-gauge.tsx) */
function polar(cx, cy, r, deg) { const rad = ((deg - 180) * Math.PI) / 180; return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) } }
function arcPath(cx, cy, r, from, to) {
  const s = polar(cx, cy, r, from), e = polar(cx, cy, r, to), large = to - from > 180 ? 1 : 0
  return `M ${s.x.toFixed(2)} ${s.y.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${e.x.toFixed(2)} ${e.y.toFixed(2)}`
}
const GAUGE_SEGMENTS = [[0, 28, 'ALERT'], [28, 52, 'CAUTION'], [52, 78, 'WARNING'], [78, 100, 'CRITICAL']]
function gaugeHtml(risk, level, live) {
  const cx = 110, cy = 105, r = 84
  const needleDeg = 180 + (Math.max(0, Math.min(100, risk)) / 100) * 180
  const end = polar(cx, cy, r - 18, needleDeg), active = LEVEL_STYLES[level]
  return `<div class="flex flex-col items-center" aria-label="Fatigue risk ${Math.round(risk)} percent, level ${level}">
    <svg viewBox="0 0 220 130" class="w-full max-w-[260px]" role="img">
      <path d="${arcPath(cx, cy, r, 0, 180)}" fill="none" stroke="#27272a" stroke-width="13" stroke-linecap="round"/>
      ${GAUGE_SEGMENTS.map(([f, t, lv]) => `<path d="${arcPath(cx, cy, r, f + 1.5, t - 1.5)}" fill="none" stroke="${LEVEL_STYLES[lv].hex}"
        stroke-width="${level === lv ? 13 : 6}" stroke-linecap="round" opacity="${level === lv ? 1 : 0.28}"/>`).join('')}
      <line x1="${cx}" y1="${cy}" x2="${end.x}" y2="${end.y}" stroke="${active.hex}" stroke-width="3" stroke-linecap="round"/>
      <circle cx="${cx}" cy="${cy}" r="6" fill="#18181b" stroke="${active.hex}" stroke-width="2.5"/>
      <text x="${cx}" y="${cy - 28}" text-anchor="middle" class="font-mono" fill="#f4f4f5" font-size="34" font-weight="700">${Math.round(risk)}<tspan font-size="15" fill="#71717a">%</tspan></text>
    </svg>
    <div class="-mt-3 inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-semibold tracking-widest uppercase${live ? '' : ' opacity-50'} ${active.bg} ${active.border} ${active.text}">
      <span class="h-1.5 w-1.5 rounded-full ${active.dot} ${live && level === 'CRITICAL' ? 'animate-ping' : ''}"></span>${level}</div></div>`
}

const metricCard = (label, value, unit, warn) => `<div class="rounded-xl border p-3 ${warn ? 'border-amber-500/40 bg-amber-500/5' : 'border-zinc-800 bg-zinc-900/60'}">
  <p class="text-[10px] uppercase tracking-widest text-zinc-500">${label}</p>
  <p class="font-mono text-xl font-bold ${warn ? 'text-amber-300' : 'text-zinc-100'}">${value}${unit ? `<span class="ml-0.5 text-xs text-zinc-500">${unit}</span>` : ''}</p></div>`
const chip = (label, value, danger) => `<div class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${value > 0 && danger ? 'border-red-500/40 bg-red-500/10 text-red-400' : 'border-zinc-800 bg-zinc-900/60 text-zinc-400'}">
  <span class="font-mono font-bold">${value}</span><span>${label}</span></div>`

function renderContactsNote() {
  const c = mv.contacts
  $('mv-contacts-note').innerHTML = `<div class="rounded-xl border p-3 text-xs ${c.length === 0 ? 'border-amber-500/40 bg-amber-500/5 text-amber-300' : 'border-zinc-800 bg-zinc-900/60 text-zinc-400'}">
    <p class="flex items-start gap-2">${ic('mail-warning', 'mt-0.5 h-4 w-4 shrink-0')}${c.length === 0
      ? `<span><strong class="font-semibold">No emergency contact yet.</strong> On drowsiness the siren will ring, but no email can be sent — add one in the Contacts tab.</span>`
      : `<span>On drowsiness: siren rings immediately + alert email to <strong class="text-zinc-200">${esc(c.map((x) => x.name).join(', '))}</strong>.</span>`}</p></div>`
  icons()
}

function renderMonitor() {
  const renderStarted = performance.now()
  const m = mv.metrics, live = mv.phase === 'live', ls = LEVEL_STYLES[m.level]
  const criticalNow = live && m.level === 'CRITICAL'

  // alert banner
  const a = mv.alertInfo
  $('mv-banner').innerHTML = (mv.sirenActive || (a && live)) ? `
    <div class="rounded-xl border-2 p-4 ${mv.sirenActive ? 'animate-pulse border-red-500 bg-red-950/60' : 'border-red-500/50 bg-red-950/30'}" role="alert">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex items-start gap-3">${ic('siren', 'mt-0.5 h-5 w-5 shrink-0 text-red-500')}
          <div>
            <p class="font-bold text-red-400">DROWSINESS DETECTED — WAKE UP!</p>
            <p class="text-xs text-red-300/90">${esc(a ? a.trigger : 'Fatigue event')} · alarm ${mv.sirenActive ? 'ON' : 'OFF'} · notification handled in background</p>
            ${a && a.notified.length ? `<ul class="mt-1.5 space-y-0.5">${a.notified.map((n) => `<li class="text-[11px] text-zinc-400">${ic('mail-warning', 'mr-1 inline h-3 w-3 text-amber-400')}
              <span class="text-zinc-300">${esc(n.contactName ?? 'Contact')}</span> &lt;${esc(n.recipient)}&gt; —
              <span class="${n.status === 'delivered' ? 'text-emerald-400' : n.status === 'simulated' ? 'text-amber-400' : 'text-red-400'}">${esc(n.status)}</span></li>`).join('')}</ul>` : ''}
          </div></div>
        ${mv.sirenActive ? `<button class="btn btn-sm btn-outline border-red-500/60 text-red-300 hover:bg-red-500/10" id="stop-siren-btn">${ic('volume-x', 'mr-1.5 h-4 w-4')} Stop siren</button>` : ''}
      </div></div>` : ''

  // camera panel
  $('mv-cam').className = `rounded-2xl border bg-zinc-900/60 p-4 lg:col-span-3 ${criticalNow ? 'border-red-500/70 shadow-[0_0_35px_rgba(239,68,68,0.25)]' : 'border-zinc-800'}`
  $('mv-head').innerHTML = (live
    ? `<span class="inline-flex items-center gap-1.5 rounded-full border border-red-500/40 bg-red-500/10 px-2 py-0.5 font-semibold text-red-400">
        <span class="relative flex h-1.5 w-1.5"><span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-red-500 opacity-75"></span><span class="relative inline-flex h-1.5 w-1.5 rounded-full bg-red-500"></span></span>
        LIVE ${mv.mode === 'simulated' ? '· SIMULATED' : ''}</span>`
    : `<span class="rounded-full border border-zinc-700 bg-zinc-800/60 px-2 py-0.5 text-zinc-400">STANDBY</span>`)
    + `<span class="font-mono text-zinc-400">${formatDuration(m.monitoredS)}</span>`

  const img = $('mv-img'), showCam = mv.mode === 'camera' && live
  img.classList.toggle('invisible', !showCam)
  if (showCam && !img.getAttribute('src')) img.src = '/video_feed?t=' + Date.now()
  if (!showCam && img.getAttribute('src')) img.removeAttribute('src')

  $('mv-overlays').innerHTML = `
    ${mv.mode === 'simulated' && live ? `<div class="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-zinc-950">
      ${ic('flask-conical', 'h-8 w-8 text-amber-400/70')}<p class="text-sm font-medium text-zinc-400">Simulated driver feed</p>
      <p class="max-w-xs text-center text-xs text-zinc-600">Scripted scenario: alert → head nod → microsleep → CRITICAL. Same pipeline as the live camera.</p>
      <div class="mt-1 rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-widest ${ls.bg} ${ls.border} ${ls.text}">${m.level}</div></div>` : ''}
    ${!live ? `<div class="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-zinc-950">
      ${ic('camera', `h-9 w-9 ${mv.phase === 'starting' ? 'animate-pulse text-amber-400' : 'text-zinc-700'}`)}
      <p class="text-sm text-zinc-500">${mv.phase === 'starting' ? 'Starting engine &amp; calibrating…' : 'Camera standby — start monitoring to begin'}</p></div>` : ''}
    ${live && mv.mode === 'camera' && !m.faceDetected && m.monitoredS > 3 ? `<div class="absolute left-1/2 top-3 -translate-x-1/2 rounded-full border border-amber-500/50 bg-zinc-950/90 px-3 py-1 text-xs font-medium text-amber-400">${ic('eye-off', 'mr-1 inline h-3.5 w-3.5')} No face detected</div>` : ''}
    ${live ? `<div class="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border px-4 py-1 text-xs font-bold uppercase tracking-widest ${ls.bg} ${ls.border} ${ls.text} backdrop-blur">${m.level}</div>` : ''}
    ${live && mv.mode === 'camera' ? `<div aria-hidden class="pointer-events-none absolute inset-0 overflow-hidden opacity-30"><div class="scanline absolute left-0 h-px w-full bg-gradient-to-r from-transparent via-amber-400/70 to-transparent"></div></div>` : ''}`

  $('mv-error').innerHTML = mv.cameraError ? `<div class="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
    <p class="flex items-start gap-2 text-xs text-amber-300">${ic('alert-triangle', 'mt-0.5 h-4 w-4 shrink-0')}${esc(mv.cameraError)}</p>
    <div class="mt-2 flex gap-2">
      <button class="btn btn-sm btn-outline h-8 border-zinc-700 text-xs" id="retry-cam-btn">Retry camera</button>
      <button class="btn btn-sm btn-outline h-8 border-amber-500/50 text-xs text-amber-300 hover:bg-amber-500/10" id="use-sim-btn">Use demo simulation</button>
    </div></div>` : ''

  $('mv-controls').innerHTML = mv.phase !== 'live' ? `
    <button class="btn bg-emerald-600 text-white hover:bg-emerald-500" id="start-btn" ${mv.phase === 'starting' ? 'disabled' : ''}>${ic('play', 'mr-1.5 h-4 w-4')} Start Monitoring</button>
    <button class="btn btn-outline border-amber-500/50 text-amber-300 hover:bg-amber-500/10" id="sim-btn" ${mv.phase === 'starting' ? 'disabled' : ''}>${ic('flask-conical', 'mr-1.5 h-4 w-4')} Demo simulation</button>` : `
    <button class="btn btn-destructive bg-red-600 hover:bg-red-500" id="stop-btn">${ic('square', 'mr-1.5 h-4 w-4')} Stop &amp; save report</button>
    <button class="btn btn-outline btn-icon border-zinc-700" id="mute-btn" aria-label="${mv.muted ? 'Unmute siren' : 'Mute siren'}">${mv.muted ? ic('volume-x', 'h-4 w-4 text-red-400') : ic('volume-2', 'h-4 w-4 text-emerald-400')}</button>
    <span class="text-xs text-zinc-500">${mv.muted ? 'Siren muted' : 'Siren armed'}</span>`

  $('mv-strip').innerHTML = LEVELS.map((lv) => { const s = LEVEL_STYLES[lv], on = m.level === lv
    return `<div style="width:${SEGMENT_WIDTH[lv]}%" class="rounded-md border px-2 py-1.5 text-center text-[10px] font-bold uppercase tracking-wider transition-all duration-300 sm:text-xs ${on ? `${s.bg} ${s.border} ${s.text} shadow-[0_0_18px_rgba(251,191,36,0.15)]` : 'border-zinc-800 bg-zinc-950/60 text-zinc-600'}">${lv}</div>` }).join('')

  // gauge + metrics
  $('mv-gauge').innerHTML = gaugeHtml(m.risk, m.level, live)
  $('mv-calib').innerHTML = live ? `<div class="mt-2 grid grid-cols-3 gap-2 text-center text-[11px] text-zinc-500">
    <div><p class="font-mono text-sm text-zinc-200">${m.baselineEar > 0 ? m.baselineEar.toFixed(3) : '—'}</p><p>baseline EAR</p></div>
    <div><p class="font-mono text-sm text-zinc-200">${m.thresholdEar > 0 ? m.thresholdEar.toFixed(3) : '—'}</p><p>threshold</p></div>
    <div><p class="font-mono text-sm text-zinc-200">${m.faceDetected ? 'yes' : 'no'}</p><p>face tracked</p></div></div>` : ''
  $('mv-metrics').innerHTML =
    metricCard('PERCLOS', m.perclos.toFixed(1), '%', m.perclos > 15) +
    metricCard('EAR (eyes)', m.ear > 0 ? m.ear.toFixed(3) : '—', '', false) +
    metricCard('Blinks', String(m.blinks), '', false) +
    metricCard('Mouth / MAR', m.mar > 0 ? m.mar.toFixed(2) : '—', m.yawnStatus || 'CALIBRATING', m.yawnStatus === 'YAWNING') +
    metricCard('Yawns', String(m.yawns), m.lastYawnAt ? formatIST(m.lastYawnAt) : 'no event', m.yawns > 0) +
    metricCard('Head posture', live && m.headPitchDeg !== 0 ? `${m.headPitchDeg.toFixed(0)}°` : '—', m.headPitchDeg < -15 ? 'nod' : '', m.headPitchDeg < -15) +
    metricCard('Eyes closed', m.eyesClosedS.toFixed(1), 's', m.eyesClosedS >= 1)
  $('mv-chips').innerHTML = chip('Microsleeps', m.microsleeps, true) + chip('Head nods', m.headNods, true) +
    chip('Distractions', m.distractions, false) + chip('Slow blinks', m.slowBlinks, false) + chip('Absences', m.absences, false) +
    `<span class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${mv.alarmActive ? 'border-red-500/40 bg-red-500/10 text-red-400' : 'border-zinc-800 bg-zinc-900/60 text-zinc-400'}"><span class="font-mono font-bold">${mv.alarmActive ? 'ON' : 'OFF'}</span><span>Alarm</span></span>` +
    `<span class="rounded-full border border-zinc-800 px-2.5 py-1 text-[10px] text-zinc-500">frame ${Number(m.processingMs || 0).toFixed(0)} ms · API ${mv.apiLatencyMs.toFixed(0)} ms · UI ${mv.renderCostMs.toFixed(1)} ms</span>`

  // event feed (only rebuilt when it changes, to keep the scroll position)
  const sig = mv.events.length + ':' + (mv.events[0] ? mv.events[0].key : '') + ':' + live
  if (sig !== mv.eventsSig) {
    mv.eventsSig = sig
    $('mv-events').innerHTML = mv.events.length === 0
      ? `<p class="py-6 text-center text-xs text-zinc-600">${live ? 'Monitoring — fatigue events will appear here in real time.' : 'Start monitoring to see live fatigue events.'}</p>`
      : `<ul class="scrollbar-slim max-h-64 space-y-1.5 overflow-y-auto pr-1">${mv.events.map((e) => { const s = LEVEL_STYLES[e.riskLevel] || LEVEL_STYLES.ALERT
        return `<li class="flex items-center gap-2.5 rounded-lg border border-zinc-800/70 bg-zinc-950/50 px-3 py-1.5">
          <span class="font-mono text-[11px] text-zinc-500">${formatDuration(e.elapsedS)}</span>
          <span class="rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${s.bg} ${s.border} ${s.text}">${eventTypeLabel(e.eventType)}</span>
          <span class="truncate text-xs text-zinc-400">${esc(e.description)}</span></li>` }).join('')}</ul>`
  }

  bindMonitorButtons()
  icons()
  mv.renderCostMs = performance.now() - renderStarted
}

function bindMonitorButtons() {
  const on = (id, fn) => { const el = $(id); if (el) el.onclick = fn }
  on('start-btn', () => startMonitoring('camera'))
  on('sim-btn', () => startMonitoring('simulated'))
  on('retry-cam-btn', () => startMonitoring('camera'))
  on('use-sim-btn', () => startMonitoring('simulated'))
  on('stop-btn', stopMonitoring)
  on('mute-btn', toggleMute)
  on('stop-siren-btn', async () => { stopAlarm(); mv.sirenActive = false; mv.muted = true; await api('/api/monitor/stop-siren', { method: 'POST' }); renderMonitor() })
}

async function toggleMute() {
  mv.muted = !mv.muted
  if (mv.muted) { stopAlarm(); mv.sirenActive = false }
  await api('/api/monitor/mute', { method: 'POST', body: { muted: mv.muted } })
  renderMonitor()
}

/* ---- start / poll / stop */
async function startMonitoring(mode) {
  try { audioCtx().resume() } catch (e) {}              // unlock audio on the click so the siren can ring later
  mv.sessionGeneration++
  mv.stateSeq = -1; mv.sessionId = null
  mv.cameraError = ''; mv.phase = 'starting'; mv.mode = mode; mv.metrics = { ...EMPTY_METRICS }; mv.events = []; mv.alertInfo = null
  mv.eventsSig = ''; renderMonitor()
  await api('/api/monitor/mute', { method: 'POST', body: { muted: mv.muted } })
  const r = await api('/api/monitor/start', { method: 'POST', body: { mode } })
  if (r.ok && (r.data.phase === 'live' || r.data.phase === 'starting')) {
    applyState(r.data); startPolling()
  } else {
    mv.cameraError = (r.data && r.data.error) || (mode === 'camera' ? 'Could not start the camera.' : 'Could not start the simulation.')
    mv.phase = 'idle'; mv.mode = null; renderMonitor()
  }
}

function startPolling() { stopPolling(); mv.poll = setInterval(pollState, 250) }
function stopPolling() { if (mv.poll) clearInterval(mv.poll); mv.poll = null }

let polling = false
async function pollState() {
  if (polling) return
  polling = true
  const requestGeneration = mv.sessionGeneration
  const requestStarted = performance.now()
  try {
    const r = await api('/api/monitor/state')
    if (r.ok && requestGeneration === mv.sessionGeneration) { mv.apiLatencyMs = performance.now() - requestStarted; applyState(r.data) }
  } finally { polling = false }
}

function applyState(s) {
  const incomingSession = s.sessionId || null
  if (mv.sessionId && incomingSession && incomingSession !== mv.sessionId) return
  if (mv.sessionId && !incomingSession && s.phase === 'idle' && mv.phase === 'live') return
  if (incomingSession && incomingSession !== mv.sessionId) { mv.sessionId = incomingSession; mv.stateSeq = -1 }
  if (Number.isFinite(s.stateSeq) && s.stateSeq < mv.stateSeq) return
  if (Number.isFinite(s.stateSeq)) mv.stateSeq = s.stateSeq
  mv.frontendUpdatedAt = new Date().toISOString()
  mv.backendResponseAt = s.backendResponseAt || null
  if (s.phase === 'idle' && (mv.phase === 'live' || mv.phase === 'starting')) stopPolling()
  mv.cameraError = s.phase === 'idle' && s.error ? s.error : ''
  mv.phase = s.phase; mv.mode = s.mode; mv.metrics = s.metrics; mv.events = s.events
  mv.muted = s.muted; mv.alarmActive = !!s.alarmActive; mv.sirenActive = !!s.sirenActive; mv.alertInfo = s.alertInfo
  if (mv.sirenActive && !mv.muted) startAlarm(); else stopAlarm()
  // one toast per dispatched alert (mirrors the original handleDrowsiness toasts)
  const ai = s.alertInfo
  if (ai && ai.done && s.alertSeq !== mv.toastedSeq) {
    mv.toastedSeq = s.alertSeq
    const rows = ai.notified || []
    const noContacts = rows.length === 1 && rows[0].recipient === '—'
    if (ai.throttled) { /* alert batch already sent in the last 30 s */ }
    else if (rows.length === 0 || noContacts) toast({ title: 'No emergency contacts configured', description: 'Add an emergency contact so alerts can be emailed.', variant: 'destructive' })
    else {
      const delivered = rows.filter((r) => r.status === 'delivered').length, simulated = rows.filter((r) => r.status === 'simulated').length
      toast({ title: `Emergency alert dispatched (${rows.length})`,
        description: delivered > 0 ? `Email delivered to ${delivered} contact(s).` : simulated > 0 ? 'Alert recorded (SMTP not configured) — see Alert Log for the full email content.' : 'Alert delivery failed — see Alert Log.' })
    }
  }
  renderMonitor()
}

async function stopMonitoring() {
  mv.sessionGeneration++
  stopPolling(); stopAlarm()
  mv.sirenActive = false
  const btn = $('stop-btn'); if (btn) btn.disabled = true
  const r = await api('/api/monitor/stop', { method: 'POST' })
  toast(r.ok ? { title: 'Session report saved', description: 'Open the Reports tab to view your fatigue summary.' }
             : { title: 'Could not save session report', variant: 'destructive' })
  mv.phase = 'idle'; mv.mode = null; mv.metrics = { ...EMPTY_METRICS }; mv.alertInfo = null; mv.eventsSig = ''
  mv.sessionId = null; mv.stateSeq = -1
  renderMonitor()
  sessionEnded()
}

/* ================================================================== REPORTS VIEW */
const rp = { sessions: [], selected: null, loading: false }

function mountReports() {
  $('view-reports').innerHTML = `<div class="grid gap-4 lg:grid-cols-5">
    <div class="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4 lg:col-span-2">
      <div class="mb-3 flex items-center justify-between">
        <h2 class="text-sm font-semibold text-zinc-300">Session history</h2>
        <button class="btn btn-sm btn-ghost h-7 text-xs text-zinc-400" id="rp-refresh">Refresh</button>
      </div>
      <div id="rp-list"></div>
    </div>
    <div class="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4 lg:col-span-3" id="rp-detail"></div></div>`
  $('rp-refresh').onclick = loadReports
  renderReportList(); renderReportDetail(); loadReports()
}

async function loadReports() {
  const r = await api('/api/sessions/list')
  rp.sessions = r.ok ? r.data.sessions || [] : []
  renderReportList()
}

async function openSession(id) {
  rp.loading = true; renderReportDetail()
  const r = await api(`/api/sessions/detail?id=${encodeURIComponent(id)}`)
  rp.loading = false
  if (r.ok) rp.selected = r.data
  renderReportDetail()
}

function renderReportList() {
  const el = $('rp-list')
  if (rp.sessions.length === 0) { el.innerHTML = `<p class="py-8 text-center text-xs text-zinc-600">No sessions yet — start monitoring first.</p>`; return }
  el.innerHTML = `<ul class="scrollbar-slim max-h-[560px] space-y-2 overflow-y-auto pr-1">${rp.sessions.map((row) => {
    const peak = row.summary && typeof row.summary.peak_level === 'string' ? row.summary.peak_level : 'ALERT', ps = LEVEL_STYLES[peak] || LEVEL_STYLES.ALERT
    return `<li><button data-id="${esc(row.id)}" class="rp-open group w-full rounded-xl border border-zinc-800 bg-zinc-950/60 p-3 text-left transition-colors hover:border-amber-500/40 hover:bg-zinc-900">
      <div class="flex items-center justify-between gap-2"><p class="font-mono text-xs text-zinc-300">${formatIST(row.startTime)}</p>${ic('chevron-right', 'h-3.5 w-3.5 text-zinc-600 transition-transform group-hover:translate-x-0.5')}</div>
      <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
        <span class="rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${row.status === 'failed' ? 'border-amber-500/40 bg-amber-500/10 text-amber-400' : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400'}">${esc(row.status)}</span>
        ${row.status !== 'failed' ? `<span class="rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase ${ps.bg} ${ps.border} ${ps.text}">peak ${esc(peak)}</span>` : ''}
        ${row.summary ? `<span class="rounded border border-zinc-800 bg-zinc-900 px-1.5 py-0.5 text-[10px] text-zinc-400">${num(row.summary.microsleeps)} microsleeps</span>` : ''}
        <span class="rounded border border-zinc-800 bg-zinc-900 px-1.5 py-0.5 text-[10px] text-zinc-500">${row.eventCount} events</span>
      </div></button></li>` }).join('')}</ul>`
  el.querySelectorAll('.rp-open').forEach((b) => (b.onclick = () => openSession(b.dataset.id)))
  icons()
}

function renderReportDetail() {
  const el = $('rp-detail')
  if (rp.loading) { el.innerHTML = `<p class="py-16 text-center text-xs text-zinc-500">Loading report…</p>`; return }
  if (!rp.selected) {
    el.innerHTML = `<div class="flex h-full min-h-[300px] flex-col items-center justify-center gap-2 text-center">${ic('gauge', 'h-8 w-8 text-zinc-700')}
      <p class="text-sm text-zinc-500">Select a session to view its fatigue report</p>
      <p class="max-w-xs text-xs text-zinc-600">Reports include blinks, microsleeps, PERCLOS, head posture, level time and the full event timeline.</p></div>`
    icons(); return
  }
  const { session, events } = rp.selected, s = session.summary
  const sil = (s && s.seconds_in_level) || {}
  const totalLevelTime = LEVELS.reduce((a, lv) => a + num(sil[lv]), 0)
  const peak = LEVELS.find((lv) => s && s.peak_level === lv) || 'ALERT'

  const points = events.map((e) => ({ x: Math.max(0, e.elapsedS), y: Math.max(0, Math.min(100, e.riskScore)) }))
  const maxT = Math.max(1, ...points.map((p) => p.x), num(s && s.monitored_s))
  const path = points.sort((a, b) => a.x - b.x).map((p, i) => `${i === 0 ? 'M' : 'L'} ${((p.x / maxT) * 300).toFixed(1)} ${(40 - (p.y / 100) * 36).toFixed(1)}`).join(' ')

  const hasSummary = !!s, countEv = (t) => events.filter((e) => e.eventType === t).length
  const statValue = (key, type) => (hasSummary ? String(num(s[key])) : String(countEv(type)))
  const dur = num(s && s.monitored_s) || (session.endTime ? (new Date(session.endTime) - new Date(session.startTime)) / 1000 : 0)
  const stats = [
    ['Duration', formatDuration(dur)], ['Blinks', hasSummary ? String(num(s.blinks)) : '—'],
    ['Slow blinks', statValue('slow_blinks', 'SLOW_BLINK')], ['Microsleeps', statValue('microsleeps', 'MICROSLEEP')],
    ['Yawns', statValue('yawns', 'YAWN')], ['Head nods', statValue('head_nods', 'HEAD_NOD')],
    ['Distractions', statValue('distractions', 'DISTRACTION')], ['Longest closure', s ? `${num(s.longest_closure_s).toFixed(1)}s` : '—'],
    ['Avg risk', s ? `${num(s.avg_risk).toFixed(1)}%` : '—'], ['Max risk', s ? `${num(s.max_risk).toFixed(0)}%` : '—'],
    ['Max PERCLOS', s ? `${num(s.max_perclos).toFixed(1)}%` : '—'], ['% time caution+', s ? `${num(s.pct_caution_or_worse).toFixed(0)}%` : '—'],
  ]

  el.innerHTML = `<div class="space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div><h3 class="font-mono text-sm text-zinc-200">${formatIST(session.startTime)} IST</h3>
        <p class="text-xs text-zinc-500">Session ${esc(session.id.slice(0, 18))}… · ${esc(session.status)}</p></div>
      <span class="rounded-full border px-3 py-1 text-xs font-bold uppercase tracking-widest ${LEVEL_STYLES[peak].bg} ${LEVEL_STYLES[peak].border} ${LEVEL_STYLES[peak].text}">peak ${peak}</span>
    </div>
    ${session.status === 'failed' ? `<div class="rounded-xl border border-amber-500/40 bg-amber-500/5 p-3"><p class="flex items-start gap-2 text-xs text-amber-300">${ic('file-warning', 'mt-0.5 h-4 w-4 shrink-0')}${esc(session.errorMessage || 'Session failed.')}</p></div>` : ''}
    ${s ? `
      <div class="grid grid-cols-3 gap-2 sm:grid-cols-4">${stats.map(([l, v]) => `<div class="rounded-lg border border-zinc-800 bg-zinc-950/60 p-2.5">
        <p class="text-[9px] uppercase tracking-widest text-zinc-500">${l}</p><p class="font-mono text-sm font-bold text-zinc-100">${v}</p></div>`).join('')}</div>
      <div><p class="mb-1.5 text-[11px] uppercase tracking-widest text-zinc-500">Time in each level</p>
        <div class="flex h-5 w-full overflow-hidden rounded-md border border-zinc-800">${LEVELS.map((lv) => { const v = num(sil[lv]), pct = totalLevelTime > 0 ? (v / totalLevelTime) * 100 : 0
          return `<div style="width:${pct}%;background-color:${LEVEL_STYLES[lv].hex};opacity:${pct > 0 ? 0.85 : 0}" title="${lv}: ${v.toFixed(1)}s"></div>` }).join('')}</div>
        <div class="mt-1 flex justify-between text-[10px] text-zinc-500">${LEVELS.map((lv) => `<span class="inline-flex items-center gap-1"><span class="h-1.5 w-1.5 rounded-full" style="background-color:${LEVEL_STYLES[lv].hex}"></span>${lv} ${num(sil[lv]).toFixed(0)}s</span>`).join('')}</div></div>
      ${points.length > 1 ? `<div><p class="mb-1.5 text-[11px] uppercase tracking-widest text-zinc-500">Risk over time (at events)</p>
        <svg viewBox="0 0 300 44" class="w-full rounded-lg border border-zinc-800 bg-zinc-950/60 p-1">
          ${[28, 52, 78].map((t) => `<line x1="0" x2="300" y1="${40 - (t / 100) * 36}" y2="${40 - (t / 100) * 36}" stroke="#27272a" stroke-dasharray="3 3" stroke-width="0.5"/>`).join('')}
          ${path ? `<path d="${path}" fill="none" stroke="#fbbf24" stroke-width="1.5" stroke-linejoin="round"/>` : ''}</svg></div>` : ''}` : ''}
    <div><p class="mb-1.5 text-[11px] uppercase tracking-widest text-zinc-500">Event timeline (${events.length})</p>
      ${events.length === 0 ? `<p class="py-4 text-center text-xs text-zinc-600">No events recorded in this session.</p>`
        : `<ul class="scrollbar-slim max-h-72 space-y-1.5 overflow-y-auto pr-1">${events.map((e) => { const st = LEVEL_STYLES[e.riskLevel] || LEVEL_STYLES.ALERT
          return `<li class="flex items-center gap-2.5 rounded-lg border border-zinc-800/70 bg-zinc-950/50 px-3 py-1.5">
            <span class="font-mono text-[11px] text-zinc-500">${formatDuration(e.elapsedS)}</span>
            <span class="rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${st.bg} ${st.border} ${st.text}">${eventTypeLabel(e.eventType)}</span>
            <span class="truncate text-xs text-zinc-400">${esc(e.description)}</span></li>` }).join('')}</ul>`}</div></div>`
  icons()
}

/* ================================================================== CONTACTS VIEW */
const ct = { contacts: [], confirmingId: null }

function mountContacts() {
  $('view-contacts').innerHTML = `<div class="grid gap-4 lg:grid-cols-5">
    <div class="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4 lg:col-span-2">
      <h2 class="mb-1 flex items-center gap-2 text-sm font-semibold text-zinc-300">${ic('user-plus', 'h-4 w-4 text-amber-400')} Add emergency contact</h2>
      <p class="mb-4 text-xs text-zinc-500">When drowsiness is detected during monitoring, an alert email is sent to every contact here — instantly, in the background.</p>
      <form class="space-y-3" id="ct-form">
        <div class="space-y-1.5"><label class="fg-label" for="c-name">Full name</label><input class="fg-input" id="c-name" placeholder="e.g. Roshini" required></div>
        <div class="space-y-1.5"><label class="fg-label" for="c-email">Email (alerts are sent here)</label><input class="fg-input" id="c-email" type="email" placeholder="name@example.com" required></div>
        <div class="space-y-1.5"><label class="fg-label" for="c-phone">Phone <span class="text-zinc-500">(optional)</span></label><input class="fg-input" id="c-phone" type="tel" placeholder="+91 98XXXXXX21"></div>
        <div class="grid grid-cols-2 gap-3">
          <div class="space-y-1.5"><label class="fg-label">Relationship</label>
            <select class="fg-select" id="c-rel">${['Family', 'Friend', 'Colleague', 'Other'].map((r) => `<option>${r}</option>`).join('')}</select></div>
          <div class="space-y-1.5"><label class="fg-label">Priority</label>
            <select class="fg-select" id="c-pri"><option>Primary</option><option>Secondary</option></select></div>
        </div>
        <button type="submit" class="btn w-full bg-amber-500 text-zinc-950 hover:bg-amber-400" id="c-add">Add contact</button>
      </form>
    </div>
    <div class="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4 lg:col-span-3" id="ct-list"></div></div>`
  $('ct-form').onsubmit = (e) => { e.preventDefault(); addContact() }
  renderContacts(); loadContacts()
}

async function loadContacts() {
  const r = await api('/api/contacts/list')
  ct.contacts = r.ok ? r.data.contacts || [] : []
  renderContacts()
}

async function addContact() {
  const btn = $('c-add'); btn.disabled = true; btn.textContent = 'Adding…'
  const name = $('c-name').value
  const r = await api('/api/contacts/add', { method: 'POST', body: { name, email: $('c-email').value, phone: $('c-phone').value, relationship: $('c-rel').value, priority: $('c-pri').value } })
  btn.disabled = false; btn.textContent = 'Add contact'
  if (!r.ok) { toast({ title: r.data.error || 'Could not add contact', variant: 'destructive' }); return }
  toast({ title: 'Emergency contact added', description: `${name} will be emailed when drowsiness is detected.` })
  $('c-name').value = ''; $('c-email').value = ''; $('c-phone').value = ''
  loadContacts(); loadMonitorContacts()
}

async function removeContact(id) {
  if (ct.confirmingId !== id) {            // first click arms, second click (within 3 s) confirms
    ct.confirmingId = id; renderContacts()
    setTimeout(() => { if (ct.confirmingId === id) { ct.confirmingId = null; renderContacts() } }, 3000)
    return
  }
  ct.confirmingId = null
  const r = await api('/api/contacts/delete', { method: 'POST', body: { id } })
  if (r.ok) { toast({ title: 'Contact removed' }); loadContacts(); loadMonitorContacts() }
}

function renderContacts() {
  const el = $('ct-list')
  el.innerHTML = `<h2 class="mb-3 flex items-center gap-2 text-sm font-semibold text-zinc-300">${ic('users', 'h-4 w-4 text-amber-400')} Your emergency contacts (${ct.contacts.length})</h2>` +
    (ct.contacts.length === 0
      ? `<p class="py-10 text-center text-xs text-zinc-600">No contacts yet. Without one, the siren will still ring — but no alert email can be delivered.</p>`
      : `<ul class="scrollbar-slim max-h-[480px] space-y-2 overflow-y-auto pr-1">${ct.contacts.map((c) => `
        <li class="flex items-center justify-between gap-3 rounded-xl border border-zinc-800 bg-zinc-950/60 p-3">
          <div class="min-w-0"><div class="flex flex-wrap items-center gap-2">
            <p class="text-sm font-semibold text-zinc-200">${esc(c.name)}</p>
            <span class="rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${c.priority === 'Primary' ? 'border-amber-500/40 bg-amber-500/10 text-amber-400' : 'border-zinc-700 bg-zinc-900 text-zinc-400'}">${esc(c.priority)}</span>
            ${c.relationship ? `<span class="rounded border border-zinc-800 bg-zinc-900 px-1.5 py-0.5 text-[10px] text-zinc-400">${esc(c.relationship)}</span>` : ''}</div>
            <p class="mt-1 truncate text-xs text-zinc-500">${ic('mail', 'mr-1 inline h-3 w-3')}<span class="font-mono">${esc(c.email)}</span>${c.phone ? ` · ${ic('phone', 'mr-1 inline h-3 w-3')}${esc(c.phone)}` : ''}</p></div>
          <div class="flex shrink-0 gap-1"><button data-id="${esc(c.id)}" aria-label="Edit ${esc(c.name)}" class="ct-edit btn btn-ghost btn-icon text-zinc-500 hover:text-amber-400">${ic('pencil', 'h-4 w-4')}</button>
          <button data-id="${esc(c.id)}" aria-label="Remove ${esc(c.name)}" class="ct-del btn btn-ghost btn-icon ${ct.confirmingId === c.id ? 'bg-red-500/20 text-red-400' : 'text-zinc-500 hover:text-red-400'}">${ic('trash-2', 'h-4 w-4')}</button></div></li>`).join('')}</ul>`)
  el.querySelectorAll('.ct-edit').forEach((b) => (b.onclick = () => editContact(b.dataset.id)))
  el.querySelectorAll('.ct-del').forEach((b) => (b.onclick = () => removeContact(b.dataset.id)))
  icons()
}

async function editContact(id) {
  const c = ct.contacts.find((x) => x.id === id); if (!c) return
  const name = prompt('Contact name', c.name); if (name === null) return
  const email = prompt('Contact email', c.email); if (email === null) return
  const phone = prompt('Contact phone', c.phone || ''); if (phone === null) return
  const relationship = prompt('Relationship', c.relationship || 'Other'); if (relationship === null) return
  const priority = prompt('Priority: Primary or Secondary', c.priority); if (priority === null) return
  const r = await api('/api/contacts/update', { method: 'POST', body: { id, name, email, phone, relationship, priority } })
  if (!r.ok) { toast({ title: r.data.error || 'Could not update contact', variant: 'destructive' }); return }
  toast({ title: 'Emergency contact updated' }); loadContacts(); loadMonitorContacts()
}

/* ================================================================== ALERT LOG VIEW */
const al = { rows: [], loaded: false }

function mountAlerts() {
  $('view-alerts').innerHTML = `<div class="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4">
    <div class="mb-1 flex items-center justify-between"><h2 class="text-sm font-semibold text-zinc-300">Emergency alert log</h2>
      <button class="btn btn-sm btn-ghost h-7 text-xs text-zinc-400" id="al-refresh">${ic('refresh-cw', 'mr-1 h-3 w-3')} Refresh</button></div>
    <p class="mb-3 text-xs text-zinc-500">Drowsiness alerts are sent to your emergency contacts. Delivered means the SMTP server accepted the message; Failed or Simulated means it was not sent. Configure SMTP_HOST, SMTP_PORT (587 or 465), SMTP_USERNAME, SMTP_PASSWORD, and SMTP_FROM_EMAIL in the project-root .env file.</p>
    <div class="mb-3 flex flex-wrap items-center gap-2"><button class="btn btn-sm btn-outline border-zinc-700 text-zinc-300" id="al-test-email">Send test email to my account</button><span class="text-xs text-zinc-500" id="al-email-status"></span></div>
    <div id="al-body"></div></div>`
  $('al-refresh').onclick = loadAlerts
  $('al-test-email').onclick = testEmail
  api('/api/email/status').then((r) => { if (r.ok) $('al-email-status').textContent = r.data.configured ? 'SMTP configured' : `SMTP not configured: ${r.data.missing.join(', ')}` })
  loadAlerts()
}

async function testEmail() {
  const btn = $('al-test-email'); btn.disabled = true; btn.textContent = 'Sending test…'
  const r = await api('/api/email/test', { method: 'POST', body: {} })
  btn.disabled = false; btn.textContent = 'Send test email to my account'
  toast(r.ok ? { title: 'SMTP test delivered', description: r.data.detail } : { title: r.data.status === 'simulated' ? 'SMTP is not configured' : 'SMTP test failed', description: r.data.error || r.data.detail, variant: 'destructive' })
}

async function loadAlerts() {
  const r = await api('/api/notifications')
  al.rows = r.ok ? r.data.notifications || [] : []; al.loaded = true
  renderAlerts()
}

function statusBadge(status) {
  const cls = status === 'delivered' ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400' : status === 'simulated' ? 'border-amber-500/40 bg-amber-500/10 text-amber-400' : 'border-red-500/40 bg-red-500/10 text-red-400'
  const i = status === 'delivered' ? 'mail-check' : status === 'failed' ? 'circle-alert' : 'flask-conical'
  return `<span class="inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${cls}">${ic(i, 'h-3 w-3')}${esc(status)}</span>`
}

function renderAlerts() {
  const el = $('al-body')
  if (al.loaded && al.rows.length === 0) { el.innerHTML = `<p class="py-12 text-center text-xs text-zinc-600">No alerts yet — they appear here automatically whenever drowsiness is detected during monitoring.</p>`; return }
  el.innerHTML = `<div class="scrollbar-slim max-h-[520px] overflow-y-auto"><table class="w-full text-left text-xs">
    <thead class="sticky top-0 bg-zinc-900/95 text-[10px] uppercase tracking-widest text-zinc-500 backdrop-blur"><tr>
      <th class="px-2 py-2 font-medium">Time (IST)</th><th class="px-2 py-2 font-medium">Contact</th><th class="px-2 py-2 font-medium">Trigger</th>
      <th class="px-2 py-2 font-medium">Status</th><th class="hidden px-2 py-2 font-medium md:table-cell">Detail</th></tr></thead>
    <tbody>${al.rows.map((n) => `<tr class="border-t border-zinc-800/70">
      <td class="whitespace-nowrap px-2 py-2 font-mono text-zinc-400">${formatIST(n.createdAt)}</td>
      <td class="px-2 py-2"><p class="text-zinc-200">${esc(n.contactName ?? '—')}</p><p class="font-mono text-[10px] text-zinc-500">${esc(n.recipient)}</p></td>
      <td class="max-w-[220px] px-2 py-2 text-zinc-400"><p class="truncate">${esc(n.trigger)}</p>${n.sessionId ? `<p class="font-mono text-[10px] text-zinc-600">${esc(n.sessionId.slice(0, 22))}…</p>` : ''}</td>
      <td class="px-2 py-2">${statusBadge(n.status)}</td>
      <td class="hidden max-w-[260px] px-2 py-2 md:table-cell"><p class="truncate text-zinc-500" title="${esc(n.detail ?? '')}">${esc(n.detail)}</p></td></tr>`).join('')}</tbody></table></div>`
  icons()
}

boot()
