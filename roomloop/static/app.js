/* ==========================================================================
   RoomLoop console
   A tiny, dependency-free client for the booking API. Everything on screen is
   rendered from a live response — there is no mock layer.
   ========================================================================== */

const API = '';
const DAY_START = 8 * 60;   // 08:00 assumed office hours
const DAY_END = 19 * 60;    // 19:00
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  rooms: [],
  office: 'all',
  day: todayISO(),
  dayCache: new Map(),
  series: [],
  audit: [],
  view: 'calendar',
  api: { index: 0, lastResponse: '' },
};

/* ---------------------------------------------------------------- helpers */
function todayISO() { return isoDate(new Date()); }

function isoDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function parseISO(iso) { return new Date(`${iso}T12:00:00`); }

function addDays(iso, n) {
  const d = parseISO(iso);
  d.setDate(d.getDate() + n);
  return isoDate(d);
}

function startOfWeek(iso) {
  const d = parseISO(iso);
  const shift = (d.getDay() + 6) % 7; // Monday = 0
  d.setDate(d.getDate() - shift);
  return isoDate(d);
}

function weekdayShort(iso) {
  return new Intl.DateTimeFormat('en-GB', { weekday: 'short' }).format(parseISO(iso));
}

function dayNum(iso) { return parseISO(iso).getDate(); }

function monthShort(iso) {
  return new Intl.DateTimeFormat('en-GB', { month: 'short' }).format(parseISO(iso));
}

function longDate(iso) {
  return new Intl.DateTimeFormat('en-GB', {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
  }).format(parseISO(iso));
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function hhmm(iso) { return (iso || '').slice(11, 16); }

function minutesOf(iso) {
  const [h, m] = hhmm(iso).split(':').map(Number);
  return (h || 0) * 60 + (m || 0);
}

function fmtDuration(minutes) {
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

function clamp(value, min, max) { return Math.min(max, Math.max(min, value)); }

/** Build a query string with proper encoding instead of string concatenation. */
function qs(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, value);
  });
  return `?${search.toString()}`;
}

function initials(name) {
  const clean = String(name || '?').split('@')[0].replace(/[._-]/g, ' ');
  return clean.split(' ').filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join('');
}

function toneFor(text) {
  let hash = 0;
  for (const ch of String(text)) hash = (hash * 31 + ch.charCodeAt(0)) % 997;
  return hash % 5;
}

const TONES = ['#67e8f9', '#a78bfa', '#4ade80', '#fbbf24', '#f472b6'];

function tzOffsetMinutes(tz, when = new Date()) {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, timeZoneName: 'longOffset',
    }).formatToParts(when);
    const raw = parts.find((p) => p.type === 'timeZoneName')?.value || 'GMT';
    const match = raw.match(/GMT([+-])(\d{1,2})(?::?(\d{2}))?/);
    if (!match) return 0;
    const sign = match[1] === '-' ? -1 : 1;
    return sign * (Number(match[2]) * 60 + Number(match[3] || 0));
  } catch { return 0; }
}

function offsetLabel(tz, when = new Date()) {
  const total = tzOffsetMinutes(tz, when);
  const sign = total < 0 ? '-' : '+';
  const abs = Math.abs(total);
  return `${sign}${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`;
}

function nowInOffice(tz) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(new Date());
  const get = (t) => parts.find((p) => p.type === t)?.value;
  return { hour: Number(get('hour')), minute: Number(get('minute')) };
}

function officeDate(tz) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date());
  return parts;
}

/* ------------------------------------------------------------------- api */
class ApiError extends Error {
  constructor(status, body) {
    const err = body?.error || {};
    super(err.message || `Request failed (${status})`);
    this.status = status;
    this.code = err.code || 'UNKNOWN';
    this.details = err.details || {};
  }
}

async function api(path, { method = 'GET', body, headers = {} } = {}) {
  const init = { method, headers: { ...headers } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`${API}${path}`, init);
  const text = await response.text();
  let parsed = null;
  try { parsed = text ? JSON.parse(text) : null; } catch { parsed = text; }
  if (!response.ok) throw new ApiError(response.status, parsed);
  return parsed;
}

/* ------------------------------------------------------------------ toast */
function toast(title, message = '', kind = 'info', ttl = 4200) {
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.innerHTML = `<span class="t-ico"></span><div><strong>${esc(title)}</strong>${
    message ? `<span>${esc(message)}</span>` : ''}</div>`;
  $('#toasts').append(node);
  setTimeout(() => {
    node.classList.add('out');
    setTimeout(() => node.remove(), 320);
  }, ttl);
}

/* --------------------------------------------------------------- statuses */
function setStatus(kind, label) {
  const pill = $('#api-status');
  pill.dataset.state = kind;
  $('#api-status-label').textContent = label;
}

/* ------------------------------------------------------------------ clocks */
function tickClocks() {
  $$('.clock').forEach((clock) => {
    const tz = clock.dataset.zone;
    const time = new Intl.DateTimeFormat('en-GB', {
      timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(new Date());
    $('[data-clock]', clock).textContent = time;
    $('[data-offset]', clock).textContent = `UTC${offsetLabel(tz)}`;
  });
  const pulse = $('#stat-free-sub');
  if (pulse && pulse.dataset.live) {
    const tz = 'Europe/Berlin';
    const { hour, minute } = nowInOffice(tz);
    pulse.textContent = `Berlin ${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')} · live`;
  }
}

/* ------------------------------------------------------------------ views */
function switchView(view) {
  state.view = view;
  $$('.nav-item').forEach((btn) => btn.classList.toggle('is-active', btn.dataset.view === view));
  $$('.view').forEach((section) => section.classList.toggle('is-active', section.id === `view-${view}`));
  if (view === 'series') loadSeries();
  if (view === 'design') renderDesign();
  $(`.main`).scrollTop = 0;
}

/* ==================================================================== data */
async function loadRooms() {
  state.rooms = await api('/rooms?detail=true');
  $('#room-count').textContent = state.rooms.length;
  renderRoomList();
  renderOfficeChips();
}

function visibleRooms() {
  return state.rooms.filter((room) => state.office === 'all' || room.office === state.office);
}

async function loadDay(day, { force = false } = {}) {
  if (!force && state.dayCache.has(day)) return state.dayCache.get(day);
  const query = qs({ day, day_start: '08:00', day_end: '19:00', slot_minutes: 30 });
  const payload = await api(`/availability/day${query}`);
  state.dayCache.set(day, payload);
  return payload;
}

async function loadSeries() {
  const payload = await api('/series');
  state.series = payload.series;
  renderSeries();
  updateStatsFromSeries();
}

async function loadAudit() {
  try {
    const payload = await api('/audit?limit=8');
    state.audit = payload.entries;
    renderActivity();
  } catch { /* non-critical */ }
}

/* ============================================================ week strip */
function renderWeekstrip() {
  const monday = startOfWeek(state.day);
  const strip = $('#weekstrip');
  strip.innerHTML = '';
  for (let i = 0; i < 7; i += 1) {
    const iso = addDays(monday, i);
    const btn = document.createElement('button');
    btn.className = 'day-btn';
    btn.dataset.day = iso;
    btn.style.animationDelay = `${i * 26}ms`;
    if (iso === todayISO()) btn.classList.add('is-today');
    if (iso === state.day) btn.classList.add('is-active');
    btn.innerHTML = `
      <span class="dow">${weekdayShort(iso)}</span>
      <span class="dnum">${dayNum(iso)}</span>
      <span class="dmonth">${monthShort(iso)}</span>
      <span class="dots" data-dots></span>`;
    btn.addEventListener('click', () => selectDay(iso));
    strip.append(btn);
  }
  paintWeekDensity();
}

async function paintWeekDensity() {
  const monday = startOfWeek(state.day);
  await Promise.all([...Array(7)].map(async (_, i) => {
    const iso = addDays(monday, i);
    try {
      const payload = await loadDay(iso);
      const count = payload.rooms
        .filter((room) => state.office === 'all' || room.office === state.office)
        .reduce((sum, room) => sum + room.busy.length, 0);
      const dots = $(`.day-btn[data-day="${iso}"] [data-dots]`);
      if (!dots) return;
      dots.innerHTML = Array.from({ length: Math.min(4, count) }, () => '<i></i>').join('');
    } catch { /* ignore */ }
  }));
}

function selectDay(iso) {
  state.day = iso;
  $$('.day-btn').forEach((btn) => btn.classList.toggle('is-active', btn.dataset.day === iso));
  renderCalendar();
}

/* ============================================================ room rail */
function renderRoomList() {
  const list = $('#room-list');
  list.innerHTML = '';
  const rooms = visibleRooms();
  rooms.forEach((room) => {
    const now = nowInOffice(room.timezone);
    const minutes = now.hour * 60 + now.minute;
    const cached = state.dayCache.get(todayISO());
    let busy = false;
    if (cached) {
      const entry = cached.rooms.find((item) => item.room_id === room.id);
      if (entry && officeDate(room.timezone) === todayISO()) {
        busy = entry.busy.some((b) => minutesOf(b.start) <= minutes && minutesOf(b.end) > minutes);
      }
    }
    const li = document.createElement('li');
    li.innerHTML = `
      <span class="room-badge">${room.id}</span>
      <span class="room-meta">
        <span class="room-name">${esc(room.name)}</span>
        <span class="room-sub">${esc(room.office)} · ${room.capacity} seats · ${offsetLabel(room.timezone)}</span>
      </span>
      <span class="free-dot ${busy ? 'busy' : ''}" title="${busy ? 'Busy now' : 'Free now'}"></span>`;
    li.addEventListener('click', () => openNewBooking({ room_id: room.id }));
    list.append(li);
  });
}

function renderOfficeChips() {
  $$('#office-chips .chip').forEach((chip) => {
    chip.classList.toggle('is-active', chip.dataset.office === state.office);
  });
}

/* ============================================================ calendar */
async function renderCalendar() {
  $('#day-label').textContent = longDate(state.day);
  const timeline = $('#timeline');
  if (!state.rooms.length) {
    timeline.innerHTML = '<div class="timeline-skeleton"><div class="sk-row"></div><div class="sk-row"></div><div class="sk-row"></div></div>';
  }

  let payload;
  try {
    payload = await loadDay(state.day);
  } catch (error) {
    timeline.innerHTML = `<div class="tl-empty">Could not load ${esc(state.day)}: ${esc(error.message)}</div>`;
    return;
  }

  const rooms = payload.rooms.filter((room) => state.office === 'all' || room.office === state.office);
  const offices = [...new Set(rooms.map((room) => room.office))];
  const now = new Date();

  const parts = [];
  parts.push(`<div class="timeline-inner">`);
  parts.push(`<div class="tl-hours"><span></span>${Array.from({ length: 11 }, (_, i) => {
    const hour = 8 + i;
    return `<span>${String(hour).padStart(2, '0')}:00</span>`;
  }).join('')}</div>`);

  offices.forEach((office) => {
    const officeRooms = rooms.filter((room) => room.office === office);
    const sample = state.rooms.find((room) => room.office === office);
    const tz = sample?.timezone || 'UTC';
    const { hour, minute } = nowInOffice(tz);
    parts.push(`
      <div class="office-divider">
        <span>${esc(office)}</span>
        <span class="flag">${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')} local · UTC${offsetLabel(tz)}</span>
      </div>`);

    officeRooms.forEach((room, index) => {
      const span = DAY_END - DAY_START;
      const lanes = [];
      const items = room.busy
        .map((busy) => ({
          ...busy,
          startMin: clamp(minutesOf(busy.start), DAY_START, DAY_END),
          endMin: clamp(minutesOf(busy.end), DAY_START, DAY_END),
        }))
        .filter((busy) => busy.endMin > busy.startMin)
        .sort((a, b) => a.startMin - b.startMin || b.endMin - a.endMin);

      items.forEach((item) => {
        let lane = 0;
        while (lanes[lane] !== undefined && lanes[lane] > item.startMin) lane += 1;
        lanes[lane] = item.endMin;
        item.lane = lane;
      });

      const laneCount = Math.max(1, lanes.length);
      const rowHeight = Math.max(62, laneCount * 54 + 8);
      const nowMin = hour * 60 + minute;
      const showNow = officeDate(tz) === state.day && nowMin >= DAY_START && nowMin <= DAY_END;

      const pills = items.map((item) => {
        const width = Math.max(1.4, ((item.endMin - item.startMin) / span) * 100);
        const left = ((item.startMin - DAY_START) / span) * 100;
        const tone = toneFor(item.user);
        const series = item.series_id ? 'is-series' : '';
        const label = item.user.split('@')[0];
        const compact = item.endMin - item.startMin < 45;
        return `<button class="tl-pill ${series} tone-${tone}"
            style="left:${left}%;width:${width}%;top:${8 + item.lane * 54}px;height:46px;animation-delay:${index * 30 + item.lane * 40}ms"
            data-booking="${item.id}" title="${esc(item.user)} · ${hhmm(item.start)}–${hhmm(item.end)}${item.series_id ? ' · recurring' : ''}">
            <span class="p-title">${esc(compact ? label.slice(0, 8) : label)}</span>
            <span class="p-time">${hhmm(item.start)}–${hhmm(item.end)}${item.series_id ? ' ↻' : ''}</span>
          </button>`;
      }).join('');

      const empty = items.length === 0
        ? `<span class="tl-empty">free all day</span>`
        : '';

      parts.push(`
        <div class="tl-row" style="animation-delay:${index * 45}ms">
          <div class="tl-label" data-room="${room.room_id}" title="Book ${esc(room.name)}">
            <span class="room-badge">${room.room_id}</span>
            <span class="room-meta">
              <span class="room-name">${esc(room.name)}</span>
              <span class="room-sub">${room.capacity} seats · ${retainedPercent(room)}% used</span>
            </span>
          </div>
          <div class="tl-track" style="height:${rowHeight}px">
            ${pills}
            ${empty}
            ${showNow ? `<span class="now-line" style="left:${((nowMin - DAY_START) / span) * 100}%"></span>` : ''}
          </div>
        </div>`);
    });
  });
  parts.push('</div>');
  timeline.innerHTML = parts.join('');

  $$('.tl-pill').forEach((pill) => {
    pill.addEventListener('click', () => openBookingDetail(Number(pill.dataset.booking)));
  });
  $$('.tl-label').forEach((label) => {
    label.addEventListener('click', () => openNewBooking({ room_id: Number(label.dataset.room) }));
  });

  const totalBusy = rooms.reduce((sum, room) => sum + room.busy.length, 0);
  const totalUtil = rooms.reduce((sum, room) => sum + room.utilised_minutes, 0);
  const capacity = rooms.length * (DAY_END - DAY_START);
  updateStats({ rooms, totalBusy, totalUtil, capacity });
  $('#tz-note').textContent = `${rooms.length} rooms · office hours 08:00–19:00 · ${now.toTimeString().slice(0, 8)} browser time`;
  renderRoomList();
}

function retainedPercent(room) {
  const span = DAY_END - DAY_START;
  return Math.round((room.utilised_minutes / span) * 100);
}

function updateStats({ rooms, totalBusy, totalUtil, capacity }) {
  const nowMinutesByRoom = rooms.map((room) => {
    const now = nowInOffice(room.timezone);
    return {
      room,
      minutes: now.hour * 60 + now.minute,
      today: officeDate(room.timezone) === todayISO(),
    };
  });
  const freeNow = rooms.filter((room) => {
    const info = nowMinutesByRoom.find((i) => i.room.room_id === room.room_id);
    if (!info || !info.today) return false;
    if (info.minutes < DAY_START || info.minutes > DAY_END) return false;
    return !room.busy.some((b) => minutesOf(b.start) <= info.minutes && minutesOf(b.end) > info.minutes);
  }).length;

  const sub = $('#stat-free-sub');
  if (state.day === todayISO()) {
    countTo($('#stat-free'), freeNow);
    sub.dataset.live = '1';
  } else {
    $('#stat-free').textContent = '—';
    sub.dataset.live = '';
    sub.textContent = 'shown for today';
  }
  countTo($('#stat-booked'), totalBusy);
  countTo($('#stat-util'), capacity ? Math.round((totalUtil / capacity) * 100) : 0, '%');
}

function countTo(node, value, suffix = '') {
  if (!node) return;
  const start = Number(node.dataset.value || 0);
  const end = Number(value) || 0;
  if (start === end) { node.textContent = `${end}${suffix}`; return; }
  const t0 = performance.now();
  const duration = 520;
  const tick = (t) => {
    const p = Math.min(1, (t - t0) / duration);
    const eased = 1 - Math.pow(1 - p, 3);
    node.textContent = `${Math.round(start + (end - start) * eased)}${suffix}`;
    if (p < 1) requestAnimationFrame(tick);
    else node.dataset.value = String(end);
  };
  node.dataset.value = String(start);
  requestAnimationFrame(tick);
}

function updateStatsFromSeries() {
  countTo($('#stat-series'), state.series.length);
  const instances = state.series.reduce((sum, s) => sum + s.total_instances, 0);
  const cancelled = state.series.reduce((sum, s) => sum + s.cancelled_instances, 0);
  $('#stat-series-sub').textContent = `${instances} instances · ${cancelled} cancelled`;
}

/* ============================================================ activity */
function renderActivity() {
  const list = $('#activity-list');
  if (!state.audit.length) {
    list.innerHTML = '<li class="muted small">No events yet</li>';
    return;
  }
  const labels = {
    'booking.created': (e) => `Booked room ${e.payload?.room_id ?? '?'} · ${esc(e.payload?.user || '')}`,
    'series.created': (e) => `Series #${e.series_id} · ${e.payload?.created ?? '?'} created, ${e.payload?.skipped ?? 0} skipped`,
    'booking.cancelled': () => 'Cancelled a single booking',
    'series.cancelled': (e) => `Cancelled series #${e.series_id} · ${e.payload?.cancelled_count ?? 0} instances`,
  };
  list.innerHTML = state.audit.slice(0, 5).map((entry, index) => {
    const fn = labels[entry.action] || (() => entry.action);
    const time = (entry.at || '').slice(11, 16);
    return `<li style="animation-delay:${index * 40}ms">${fn(entry)} <time>${time}Z</time></li>`;
  }).join('');
}

/* ============================================================ series view */
function renderSeries() {
  const grid = $('#series-grid');
  if (!state.series.length) {
    grid.innerHTML = `<div class="notice notice-soft">No recurring series yet. Create one from
      <b>New series</b> — the calendar and this page update together.</div>`;
    return;
  }
  grid.innerHTML = state.series.map((s, index) => {
    const remaining = s.total_instances - s.cancelled_instances;
    const percent = s.total_instances ? (remaining / s.total_instances) * 100 : 0;
    const room = state.rooms.find((r) => r.id === s.room_id);
    const tone = `tone-${toneFor(s.user)}`;
    return `
      <article class="series-card" data-series="${s.series_id}" style="animation-delay:${index * 55}ms">
        <div class="sc-top">
          <div>
            <div class="sc-title">${esc(s.user.split('@')[0])} <span class="muted small">· weekly</span></div>
            <div class="sc-sub">${esc(s.room_name)} · ${esc(s.office)} · #${s.series_id}</div>
          </div>
          <span class="badge ${remaining > 0 ? 'badge-info' : 'badge-muted'}">${remaining > 0 ? 'active' : 'fully cancelled'}</span>
        </div>
        <div class="sc-when">
          <svg viewBox="0 0 24 24" class="ico" style="width:14px;height:14px;opacity:.7"><circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M12 8v4.5l3 1.8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
          ${hhmm(s.local_start)}–${hhmm(s.local_end)} · ${new Intl.DateTimeFormat('en-GB', { weekday: 'long' }).format(parseISO(s.local_start.slice(0, 10)))}
          <span class="badge badge-muted">until ${esc(s.repeat_until)}</span>
        </div>
        <div class="inst-strip">
          ${Array.from({ length: Math.min(s.total_instances, 18) }, (_, i) => (
            `<span class="inst ${i >= remaining ? 'cancelled' : tone}" title="instance ${i + 1}"></span>`
          )).join('')}
          ${s.total_instances > 18 ? `<span class="badge badge-muted">+${s.total_instances - 18}</span>` : ''}
        </div>
        <div class="sc-foot">
          <div class="sc-progress"><span style="width:${percent}%"></span></div>
          <span class="small muted">${remaining}/${s.total_instances} live</span>
          <span class="badge ${s.office === 'Berlin' ? 'badge-info' : 'badge-warn'}">${esc(s.office)}</span>
        </div>
      </article>`;
  }).join('');

  $$('.series-card').forEach((card) => {
    card.addEventListener('click', () => openSeriesDetail(Number(card.dataset.series)));
  });
}

/* ============================================================ drawer */
function openSheet(html) {
  $('#sheet-inner').innerHTML = html;
  $('#sheet').classList.add('is-open');
  $('#sheet').setAttribute('aria-hidden', 'false');
  $('#scrim').classList.add('is-open');
  document.body.style.overflow = 'hidden';
  const first = $('#sheet-inner input, #sheet-inner select, #sheet-inner button');
  setTimeout(() => first?.focus({ preventScroll: true }), 240);
}

function closeSheet() {
  $('#sheet').classList.remove('is-open');
  $('#sheet').setAttribute('aria-hidden', 'true');
  $('#scrim').classList.remove('is-open');
  document.body.style.overflow = '';
}

$('#scrim').addEventListener('click', closeSheet);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeSheet();
  if (event.target.matches('input, textarea, select')) return;
  if (event.key === '1') switchView('calendar');
  if (event.key === '2') switchView('series');
  if (event.key === '3') switchView('api');
  if (event.key === '4') switchView('design');
  if (event.key === 'n' || event.key === 'N') openNewBooking();
  if (event.key === 'ArrowLeft' && state.view === 'calendar') selectDay(addDays(state.day, -1));
  if (event.key === 'ArrowRight' && state.view === 'calendar') selectDay(addDays(state.day, 1));
});

/* ---------------------------------------------------------- new booking */
function bookingDefaults(roomId) {
  const room = state.rooms.find((r) => r.id === (roomId || state.rooms[0]?.id));
  const now = nowInOffice(room?.timezone || 'Europe/Berlin');
  let startMinutes = Math.ceil(((now.hour * 60 + now.minute) + 5) / 15) * 15;
  if (startMinutes < DAY_START) startMinutes = DAY_START;
  if (startMinutes > DAY_END - 30) startMinutes = DAY_START;
  return {
    date: state.day,
    time: `${String(Math.floor(startMinutes / 60)).padStart(2, '0')}:${String(startMinutes % 60).padStart(2, '0')}`,
  };
}

function openNewBooking({ room_id, mode = 'single' } = {}) {
  const defaults = bookingDefaults(room_id);
  const selectedRoom = room_id || state.rooms[0]?.id;
  const officeOptions = ['Berlin', 'Denver'].map((office) => `
    <optgroup label="${office}">
      ${state.rooms.filter((room) => room.office === office).map((room) => `
        <option value="${room.id}" ${room.id === selectedRoom ? 'selected' : ''}>
          ${esc(room.name)} · ${room.capacity} seats
        </option>`).join('')}
    </optgroup>`).join('');

  openSheet(`
    <div class="sheet-head">
      <div>
        <h2 id="sheet-title">New booking</h2>
        <p>Times are wall clock at the room's office.</p>
      </div>
      <button class="sheet-close" data-close aria-label="Close">
        <svg viewBox="0 0 24 24" class="ico" style="width:16px;height:16px"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
      </button>
    </div>

    <div class="segmented" id="mode-switch">
      <button data-mode="single" class="${mode === 'single' ? 'is-active' : ''}">One-off booking</button>
      <button data-mode="series" class="${mode === 'series' ? 'is-active' : ''}">Weekly series</button>
    </div>

    <form id="booking-form" novalidate>
      <div class="field">
        <span class="field-label">Room</span>
        <select name="room_id" id="f-room">${officeOptions}</select>
        <span class="field-hint" id="room-hint"></span>
      </div>

      <div class="field">
        <span class="field-label">Booked for</span>
        <input name="user" id="f-user" value="mira@example.com" spellcheck="false"
               placeholder="name@company.com" autocomplete="off" />
      </div>

      <div class="grid-2">
        <label class="field">
          <span class="field-label">Date</span>
          <input type="date" name="date" id="f-date" value="${defaults.date}" />
        </label>
        <label class="field">
          <span class="field-label">Start</span>
          <input type="time" name="start" id="f-start" value="${defaults.time}" step="900" />
        </label>
      </div>

      <div class="field">
        <span class="field-label">Duration</span>
        <div class="dur-chips" id="dur-chips">
          ${[30, 45, 60, 90, 120].map((m) => `<button type="button" data-dur="${m}" class="${m === 60 ? 'is-active' : ''}">${fmtDuration(m)}</button>`).join('')}
        </div>
      </div>

      <div class="field" id="series-fields" ${mode === 'series' ? '' : 'hidden'}>
        <label class="field" style="margin-bottom:10px">
          <span class="field-label">Repeat weekly until</span>
          <input type="date" name="repeat_until" id="f-until" value="${addDays(startOfWeek(state.day), 63)}" />
        </label>
        <span class="field-label">If an occurrence clashes</span>
        <div class="segmented" id="policy-switch">
          <button type="button" data-policy="skip" class="is-active">Skip it, save the rest</button>
          <button type="button" data-policy="fail">All-or-nothing</button>
        </div>
        <span class="field-hint" id="policy-hint">R2: only the conflicting weeks are skipped.</span>
      </div>

      <div class="preview" id="preview"></div>
      <div class="form-error hidden" id="form-error"></div>

      <div class="sheet-actions">
        <button type="button" class="btn btn-ghost" data-close>Cancel</button>
        <button type="submit" class="btn btn-primary" id="submit-booking">
          <span>Create booking</span>
        </button>
      </div>
    </form>
  `);

  let activeMode = mode;
  let duration = 60;
  let policy = 'skip';

  const form = $('#booking-form');
  const updatePreview = () => {
    const room = state.rooms.find((r) => r.id === Number($('#f-room').value));
    if (!room) return;
    const date = $('#f-date').value;
    const start = $('#f-start').value || '09:00';
    const startDate = new Date(`${date}T${start}:00Z`);
    const offset = tzOffsetMinutes(room.timezone, startDate);
    const [sh, sm] = start.split(':').map(Number);
    const endTotal = sh * 60 + sm + duration;
    const dayShift = Math.floor(endTotal / (24 * 60));
    const endDate = addDays(date, dayShift);
    const endTime = `${String(Math.floor(endTotal / 60) % 24).padStart(2, '0')}:${String(endTotal % 60).padStart(2, '0')}`;
    const utcStart = new Date(startDate.getTime() - offset * 60000).toISOString().slice(11, 16);
    const utcEnd = new Date(startDate.getTime() - offset * 60000 + duration * 60000).toISOString().slice(11, 16);
    $('#room-hint').textContent = `${room.office} · ${room.timezone} · UTC${offsetLabel(room.timezone, startDate)}`;
    $('#preview').innerHTML = `
      <div class="preview-row"><span>Local (${esc(room.timezone)})</span><b>${date} ${start} → ${endDate} ${endTime}</b></div>
      <div class="preview-row"><span>Stored / returned as</span><b>${date}T${start}:00</b></div>
      <div class="preview-row"><span>Exact instant (UTC)</span><b>${date}T${utcStart}:00Z</b></div>
      ${activeMode === 'series' ? `<div class="preview-row"><span>Repeats</span><b>weekly until ${$('#f-until').value}</b></div>` : ''}`;
    return { room, date, start, endDate, endTime, utcStart, utcEnd };
  };

  const syncMode = (next) => {
    activeMode = next;
    $$('#mode-switch button').forEach((b) => b.classList.toggle('is-active', b.dataset.mode === next));
    $('#series-fields').hidden = next !== 'series';
    $('#submit-booking span').textContent = next === 'series' ? 'Create series' : 'Create booking';
    $('#sheet-title').textContent = next === 'series' ? 'New recurring booking' : 'New booking';
    updatePreview();
  };

  $$('#mode-switch button').forEach((b) => b.addEventListener('click', () => syncMode(b.dataset.mode)));
  $$('#dur-chips button').forEach((b) => b.addEventListener('click', () => {
    duration = Number(b.dataset.dur);
    $$('#dur-chips button').forEach((x) => x.classList.toggle('is-active', x === b));
    updatePreview();
  }));
  $$('#policy-switch button').forEach((b) => b.addEventListener('click', () => {
    policy = b.dataset.policy;
    $$('#policy-switch button').forEach((x) => x.classList.toggle('is-active', x === b));
    $('#policy-hint').textContent = policy === 'skip'
      ? 'R2: only the conflicting weeks are skipped.'
      : 'R1: any clash cancels the whole series — nothing is saved.';
  }));
  ['#f-room', '#f-date', '#f-start', '#f-until'].forEach((sel) => {
    $(sel).addEventListener('input', updatePreview);
    $(sel).addEventListener('change', updatePreview);
  });
  $$('#sheet [data-close]').forEach((b) => b.addEventListener('click', closeSheet));

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const submit = $('#submit-booking');
    const errorBox = $('#form-error');
    errorBox.classList.add('hidden');
    submit.disabled = true;
    submit.querySelector('span').textContent = 'Saving…';
    const preview = updatePreview();
    const body = {
      room_id: Number($('#f-room').value),
      user: $('#f-user').value.trim(),
      start: `${preview.date}T${preview.start}:00`,
      end: `${preview.endDate}T${preview.endTime}:00`,
    };
    try {
      if (activeMode === 'series') {
        body.repeat_until = $('#f-until').value;
        body.conflict_policy = policy;
        const result = await api('/bookings/recurring', { method: 'POST', body });
        showSeriesResult(result, body);
        toast(
          `Series #${result.series_id} created`,
          `${result.created_count} instances · ${result.skipped_count} skipped`,
          result.skipped_count ? 'warn' : 'ok',
        );
      } else {
        const booking = await api('/bookings', { method: 'POST', body });
        closeSheet();
        toast('Booking confirmed', `${booking.room_name} · ${booking.start.slice(11, 16)}–${booking.end.slice(11, 16)}`, 'ok');
      }
      refreshAll();
    } catch (error) {
      errorBox.innerHTML = `
        <strong>${esc(error.code.replaceAll('_', ' ').toLowerCase())}</strong><br>${esc(error.message)}
        ${renderErrorDetails(error)}`;
      errorBox.classList.remove('hidden');
      submit.disabled = false;
      submit.querySelector('span').textContent = activeMode === 'series' ? 'Create series' : 'Create booking';
      toast('Booking rejected', `${error.code} · ${error.message}`, 'bad', 5200);
    }
  });

  syncMode(mode);
}

function renderErrorDetails(error) {
  const details = error.details || {};
  if (Array.isArray(details.skipped) && details.skipped.length) {
    return `<div class="conflict-list" style="margin-top:8px">${details.skipped.slice(0, 4).map((item) => `
      <div class="conflict-item"><b>${esc(item.start)}</b><span>${esc(item.reason.replaceAll('_', ' ').toLowerCase())}</span></div>`).join('')}</div>`;
  }
  if (Array.isArray(details.conflicts) && details.conflicts.length) {
    return `<div class="conflict-list" style="margin-top:8px">${details.conflicts.map((c) => `
      <div class="conflict-item"><b>${esc(c.start)}–${esc((c.end || '').slice(11, 16))}</b><span>blocked by ${esc(c.user)}</span></div>`).join('')}</div>`;
  }
  return '';
}

function showSeriesResult(result, body) {
  const skipped = result.skipped || [];
  openSheet(`
    <div class="sheet-head">
      <div>
        <h2 id="sheet-title">Series #${result.series_id} created</h2>
        <p>${esc(result.room_name)} · ${esc(result.office)} · weekly at ${hhmm(result.recurrence.local_start)}</p>
      </div>
      <button class="sheet-close" data-close aria-label="Close">
        <svg viewBox="0 0 24 24" class="ico" style="width:16px;height:16px"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
      </button>
    </div>
    <div class="result-hero ${skipped.length ? 'warn' : ''}">
      <h3>${skipped.length ? 'Created, with exceptions' : 'Every occurrence was saved'}</h3>
      <p>${skipped.length
        ? 'The clashing weeks were skipped so the rest of the series could go ahead (R2).'
        : 'All occurrences were available and written atomically (R1).'}</p>
      <div class="result-nums">
        <div class="result-num"><b>${result.created_count}</b><span>created</span></div>
        <div class="result-num"><b>${result.skipped_count}</b><span>skipped</span></div>
        <div class="result-num"><b>${result.requested_count}</b><span>requested</span></div>
      </div>
    </div>
    ${skipped.length ? `
      <div class="field">
        <span class="field-label">Skipped occurrences</span>
        <div class="conflict-list">${skipped.map((item, i) => `
          <div class="conflict-item" style="animation-delay:${i * 50}ms">
            <b>${esc(item.start)} → ${esc((item.end || '').slice(11, 16))}</b>
            <span>${esc(item.reason.replaceAll('_', ' ').toLowerCase())}${
              item.conflicts_with?.[0] ? ` · blocked by ${esc(item.conflicts_with[0].user)}` : ''}</span>
          </div>`).join('')}</div>
      </div>` : ''}
    ${(result.warnings || []).length ? `
      <div class="conflict-item"><b>${esc(result.warnings[0].code)}</b><span>${esc(result.warnings[0].message)}</span></div>` : ''}
    <div class="field">
      <span class="field-label">Instances</span>
      <div class="instance-list">
        ${result.created.map((item, i) => `
          <div class="instance-row" style="animation-delay:${i * 22}ms">
            <span class="status-dot-sm"></span>
            <span class="idx">${String(item.occurrence_index + 1).padStart(2, '0')}</span>
            <span>${esc(item.start.slice(0, 10))}</span>
            <span>${hhmm(item.start)}–${hhmm(item.end)}</span>
            <span class="off">UTC${esc(item.utc_offset)}</span>
          </div>`).join('')}
      </div>
    </div>
    <div class="sheet-actions">
      <button class="btn btn-ghost" data-close>Done</button>
      <button class="btn btn-primary" id="view-series-btn">Open series timeline</button>
    </div>
  `);
  $$('#sheet [data-close]').forEach((b) => b.addEventListener('click', closeSheet));
  $('#view-series-btn').addEventListener('click', () => {
    closeSheet();
    switchView('series');
    setTimeout(() => openSeriesDetail(result.series_id), 260);
  });
}

/* ------------------------------------------------------- booking detail */
async function openBookingDetail(id) {
  openSheet(`<div class="sheet-head"><div><h2 id="sheet-title">Loading booking…</h2></div></div>`);
  try {
    const booking = await api(`/bookings/${id}`);
    const room = state.rooms.find((r) => r.id === booking.room_id);
    const tz = booking.timezone || room?.timezone || 'UTC';
    const isSeries = booking.series_id != null;
    openSheet(`
      <div class="sheet-head">
        <div>
          <h2 id="sheet-title">${esc(booking.user.split('@')[0])}</h2>
          <p>${esc(booking.room_name || room?.name || '')} · ${esc(booking.office || room?.office || '')} · booking #${booking.id}</p>
        </div>
        <button class="sheet-close" data-close aria-label="Close">
          <svg viewBox="0 0 24 24" class="ico" style="width:16px;height:16px"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
        </button>
      </div>
      <div class="row">
        <span class="badge ${booking.status === 'cancelled' ? 'badge-danger' : 'badge-ok'}">${esc(booking.status)}</span>
        ${isSeries ? `<span class="badge badge-info">↻ series #${booking.series_id}</span>` : '<span class="badge badge-muted">one-off</span>'}
        <span class="badge badge-muted">${esc(booking.user)}</span>
      </div>
      <dl class="kv">
        <div class="kv-row"><dt>Local wall clock</dt><dd>${esc(booking.start)} → ${esc(booking.end)}</dd></div>
        <div class="kv-row"><dt>Office timezone</dt><dd>${esc(tz)} · UTC${esc(booking.utc_offset || offsetLabel(tz))}</dd></div>
        <div class="kv-row"><dt>Exact instant</dt><dd>${esc(booking.start_utc || '')} → ${esc(booking.end_utc || '')}</dd></div>
        <div class="kv-row"><dt>Duration</dt><dd>${fmtDuration(Math.round((new Date(booking.end_utc) - new Date(booking.start_utc)) / 60000))}</dd></div>
      </dl>
      ${isSeries ? `<div class="notice notice-soft small">Cancelling the series removes this and every future occurrence. Past instances stay in the calendar and in reporting.</div>` : ''}
      <div class="sheet-actions">
        <button class="btn btn-ghost" data-close>Close</button>
        ${booking.status === 'cancelled'
          ? '<button class="btn" disabled>Already cancelled</button>'
          : `<button class="btn ${isSeries ? 'btn-ghost' : 'btn-danger'}" id="cancel-single">${isSeries ? 'Cancel just this one' : 'Cancel booking'}</button>
             ${isSeries ? '<button class="btn btn-danger" id="cancel-series">Cancel series</button>' : ''}`}
      </div>
    `);
    $$('#sheet [data-close]').forEach((b) => b.addEventListener('click', closeSheet));
    $('#cancel-single')?.addEventListener('click', () => cancelBooking(id, 'single'));
    $('#cancel-series')?.addEventListener('click', () => cancelBooking(id, 'series'));
  } catch (error) {
    openSheet(`
      <div class="sheet-head"><div><h2 id="sheet-title">Could not load booking</h2><p>${esc(error.message)}</p></div>
      <button class="sheet-close" data-close>✕</button></div>`);
    $$('#sheet [data-close]').forEach((b) => b.addEventListener('click', closeSheet));
  }
}

async function cancelBooking(id, scope, seriesId) {
  const target = scope === 'series' && seriesId ? `/series/${seriesId}` : `/bookings/${id}?scope=${scope}`;
  const method = 'DELETE';
  try {
    const result = await api(target, { method });
    closeSheet();
    if (result.already_cancelled) {
      toast('Nothing to do', 'That booking was already cancelled.', 'info');
    } else if (scope === 'series') {
      toast('Series cancelled', `${result.cancelled_count} future instances removed · ${result.kept_instances} past kept`, 'ok');
    } else {
      toast('Occurrence cancelled', result.kept_instances
        ? `${result.kept_instances} other instances still active`
        : 'The booking was removed', 'ok');
    }
    refreshAll();
  } catch (error) {
    toast('Cancel failed', `${error.code} · ${error.message}`, 'bad');
  }
}

/* -------------------------------------------------------- series detail */
async function openSeriesDetail(seriesId) {
  openSheet(`<div class="sheet-head"><div><h2 id="sheet-title">Loading series…</h2></div></div>`);
  try {
    const detail = await api(`/series/${seriesId}`);
    const live = detail.instances.filter((i) => i.status === 'confirmed').length;
    const cancelled = detail.instances.length - live;
    openSheet(`
      <div class="sheet-head">
        <div>
          <h2 id="sheet-title">${esc(detail.user.split('@')[0])} · series #${detail.series_id}</h2>
          <p>${esc(detail.room_name)} · ${esc(detail.office)} · weekly ${hhmm(detail.recurrence.local_start)}–${hhmm(detail.recurrence.local_end)}</p>
        </div>
        <button class="sheet-close" data-close aria-label="Close">
          <svg viewBox="0 0 24 24" class="ico" style="width:16px;height:16px"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
        </button>
      </div>
      <div class="row">
        <span class="badge badge-info">${live} live</span>
        <span class="badge badge-muted">${cancelled} cancelled</span>
        <span class="badge badge-muted">until ${esc(detail.recurrence.repeat_until)}</span>
        <span class="badge badge-warn">${esc(detail.timezone)}</span>
      </div>
      <div class="notice notice-soft small">
        Wall clock stays at ${hhmm(detail.recurrence.local_start)}. The UTC offset moves when the
        office changes DST, which is exactly what the nightly report needs to resolve.
      </div>
      <div class="field">
        <span class="field-label">Every instance</span>
        <div class="instance-list">
          ${detail.instances.map((item, i) => `
            <div class="instance-row ${item.status === 'cancelled' ? 'cancelled' : ''}" style="animation-delay:${i * 18}ms">
              <span class="status-dot-sm"></span>
              <span class="idx">${String(i + 1).padStart(2, '0')}</span>
              <span>${esc(item.start.slice(0, 10))}</span>
              <span>${hhmm(item.start)}–${hhmm(item.end)}</span>
              <span class="off">UTC${esc(item.utc_offset || '')}</span>
            </div>`).join('')}
        </div>
      </div>
      <div class="sheet-actions">
        <button class="btn btn-ghost" data-close>Close</button>
        <button class="btn btn-danger" id="cancel-whole">Cancel future instances</button>
      </div>
    `);
    $$('#sheet [data-close]').forEach((b) => b.addEventListener('click', closeSheet));
    $('#cancel-whole').addEventListener('click', () => cancelBooking(null, 'series', seriesId));
  } catch (error) {
    toast('Could not load series', error.message, 'bad');
    closeSheet();
  }
}

/* ============================================================ API explorer */
function apiEndpoints() {
  const day = state.day;
  const next = addDays(startOfWeek(day), 7);
  return [
    {
      verb: 'GET', path: '/rooms',
      desc: 'The legacy room payload the facilities dashboard expects — exactly id, name and capacity.',
      request: '/rooms',
    },
    {
      verb: 'GET', path: '/rooms?detail=true',
      desc: 'Additive detail: office, timezone and capacity. Nothing removed from the legacy shape.',
      request: '/rooms?detail=true',
    },
    {
      verb: 'GET', path: `/availability/day?day=${day}`,
      desc: 'Free/busy grid per room for one local day, plus 30-minute bookable slots.',
      request: `/availability/day?day=${day}`,
    },
    {
      verb: 'GET', path: '/series',
      desc: 'Every recurring series with live vs cancelled instance counts.',
      request: '/series',
    },
    {
      verb: 'GET', path: '/bookings?window=week',
      desc: 'Active bookings overlapping a UTC window. Naive bounds are read as UTC here.',
      request: `/bookings?start=${day}T00:00:00Z&end=${addDays(day, 7)}T00:00:00Z`,
    },
    {
      verb: 'POST', path: '/bookings',
      desc: 'Create one booking. Naive timestamps mean wall clock at the room office.',
      request: '/bookings',
      body: {
        room_id: 1,
        user: 'mira@example.com',
        start: `${day}T15:00:00`,
        end: `${day}T16:00:00`,
      },
    },
    {
      verb: 'POST', path: '/bookings/recurring',
      desc: 'Create a weekly series. One occurrence per week at the same wall clock, generated up front.',
      request: '/bookings/recurring',
      body: {
        room_id: 1,
        user: 'platform@example.com',
        start: `${next}T09:00:00`,
        end: `${next}T10:00:00`,
        repeat_until: addDays(next, 42),
        conflict_policy: 'skip',
      },
    },
    {
      verb: 'DELETE', path: '/bookings/{id}?scope=series',
      desc: 'Cancel a series from now on. Past occurrences are deliberately untouched.',
      request: '/bookings/1?scope=series',
      noSend: false,
    },
    {
      verb: 'GET', path: '/meta',
      desc: 'Policy constants and the timestamp contract clients can rely on.',
      request: '/meta',
    },
    {
      verb: 'GET', path: '/health',
      desc: 'Liveness plus room count.',
      request: '/health',
    },
  ];
}

function renderApiList() {
  const endpoints = apiEndpoints();
  $('#api-list').innerHTML = endpoints.map((endpoint, index) => `
    <button data-index="${index}" class="${index === state.api.index ? 'is-active' : ''}">
      <span class="verb ${endpoint.verb.toLowerCase()}">${endpoint.verb}</span>
      <span class="p">${esc(endpoint.path)}</span>
    </button>`).join('');
  $$('#api-list button').forEach((btn) => btn.addEventListener('click', () => selectEndpoint(Number(btn.dataset.index))));
  selectEndpoint(state.api.index);
}

function selectEndpoint(index) {
  const endpoints = apiEndpoints();
  state.api.index = index;
  const endpoint = endpoints[index];
  $$('#api-list button').forEach((btn) => btn.classList.toggle('is-active', Number(btn.dataset.index) === index));
  $('#api-verb').textContent = endpoint.verb;
  $('#api-verb').className = `verb ${endpoint.verb.toLowerCase()}`;
  $('#api-path').textContent = endpoint.request;
  $('#api-desc').textContent = endpoint.desc;
  const bodyField = $('#api-body-field');
  bodyField.hidden = !endpoint.body;
  if (endpoint.body) {
    $('#api-body').value = JSON.stringify(endpoint.body, null, 2);
  }
  $('#api-response').textContent = '// press Send to call the live service';
  $('#api-status-code').textContent = '—';
  $('#api-status-code').className = 'pill';
  $('#api-meta').textContent = 'not sent yet';
}

async function sendApiRequest() {
  const endpoints = apiEndpoints();
  const endpoint = endpoints[state.api.index];
  const button = $('#api-send');
  button.disabled = true;
  const t0 = performance.now();
  try {
    let body;
    if (endpoint.body) {
      try { body = JSON.parse($('#api-body').value); } catch {
        throw new ApiError(400, { error: { code: 'CLIENT_JSON_ERROR', message: 'Request body is not valid JSON' } });
      }
    }
    const response = await fetch(`${API}${endpoint.request}`, {
      method: endpoint.verb,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const payload = await response.json();
    const ms = Math.round(performance.now() - t0);
    state.api.lastResponse = JSON.stringify(payload, null, 2);
    $('#api-status-code').textContent = `${response.status} ${response.statusText || ''}`.trim();
    $('#api-status-code').className = `pill ${response.ok ? 'ok' : 'err'}`;
    $('#api-meta').textContent = `${ms} ms · ${endpoint.verb} ${endpoint.request}`;
    $('#api-response').innerHTML = highlightJson(state.api.lastResponse);
    if (endpoint.verb !== 'GET') refreshAll();
  } catch (error) {
    const ms = Math.round(performance.now() - t0);
    $('#api-status-code').textContent = 'network error';
    $('#api-status-code').className = 'pill err';
    $('#api-meta').textContent = `${ms} ms`;
    $('#api-response').textContent = String(error.message || error);
  } finally {
    button.disabled = false;
  }
}

function highlightJson(text) {
  return esc(text).replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      let cls = 'json-num';
      if (/^"/.test(match)) cls = /:$/.test(match) ? 'json-key' : 'json-str';
      else if (/true|false/.test(match)) cls = 'json-bool';
      else if (/null/.test(match)) cls = 'json-null';
      return `<span class="${cls}">${match}</span>`;
    },
  );
}

/* ============================================================ design notes */
const RULES = [
  {
    tag: 'R1', title: 'All-or-nothing, honestly',
    body: 'A recurring create runs inside one BEGIN IMMEDIATE transaction. Validation, conflict checks and every insert commit together or not at all — so a failed series can never leave half a booking behind.',
  },
  {
    tag: 'R2', title: 'Clashes skip, not block',
    body: 'Occurrences that clash are collected, reported with the blocking booking, and the rest are written. Skipping is a first-class outcome, not an error: the response carries created, skipped and requested counts.',
  },
  {
    tag: 'R3', title: 'Wall clock beats offsets',
    body: 'Expansion steps naive local datetimes by 7 days, then resolves each one in the office timezone. Berlin 09:00 stays 09:00 when the offset moves +02:00 → +01:00; the same applies to Denver on its own, different DST date.',
  },
  {
    tag: 'R4', title: 'Half-open intervals',
    body: 'Overlap is start_a < end_b AND start_b < end_a. Back-to-back bookings share an instant but occupy no shared time, so 10:00–11:00 followed by 11:00–12:00 is allowed. Tested both directions.',
  },
  {
    tag: 'R5', title: 'Rooms are a sequence',
    body: 'Availability iterates room ids 1..N rather than trusting a list, so a removed room cannot silently shift the search. The seed keeps ids sequential, which the legacy sample did not.',
  },
];

const CONSTRAINTS = [
  {
    tag: 'C1', title: 'Timestamps left exactly as they were',
    body: 'start and end are still naive local ISO with second precision and no offset. The meaning is pinned: wall clock at the room’s office. start_utc, end_utc, utc_offset and timezone are additive fields for new clients.',
  },
  {
    tag: 'C2', title: 'The dashboard payload did not grow',
    body: 'GET /rooms returns only id, name and capacity by default. Richer data is opt-in with ?detail=true, so a dashboard that "renders whatever comes back" cannot break.',
  },
  {
    tag: 'DST', title: 'The Denver hour-off bug, explained',
    body: 'Storing naive local times without an offset is what caused it: 09:00 in Denver is 15:00 UTC in summer and 16:00 UTC in winter. Storing epoch seconds fixes the data; the legacy rendering is now derived from a real instant.',
  },
  {
    tag: 'OPS', title: 'Cancellation that actually frees the room',
    body: 'Cancelling a series marks every future instance cancelled in one statement and keeps past ones. There is also a scope=single escape hatch, plus idempotent cancels so a double-click is harmless.',
  },
];

const NEXT = [
  {
    tag: 'NEXT', title: 'Auth and room ownership',
    body: 'There is no authentication layer yet: user is a free string and anyone can cancel anything. Next step is SSO identity, plus permission rules for who may cancel a series someone else owns.',
  },
  {
    tag: 'NEXT', title: 'A real database, same semantics',
    body: 'SQLite with BEGIN IMMEDIATE serialises writers correctly, but a Postgres deployment should use a range type or an exclusion constraint for overlap so the invariant is enforced by the database, not just by the service.',
  },
  {
    tag: 'NEXT', title: 'Editing a series',
    body: 'Today a series can be created or cancelled, not edited. "This and future occurrences" edits are the natural next feature, and they need the same atomicity guarantees as creation.',
  },
  {
    tag: 'NEXT', title: 'Notifications and timezone display',
    body: 'No email or calendar invites. Also worth asking whether the dashboard should render wall clock or the viewer’s local time — right now it renders the office’s wall clock.',
  },
];

function renderDesign() {
  const card = (item, index) => `
    <article class="note-card" style="animation-delay:${index * 40}ms">
      <h3><span class="note-tag">${esc(item.tag)}</span>${esc(item.title)}</h3>
      <p>${esc(item.body)}</p>
    </article>`;

  if (!$('#rule-grid').children.length) {
    $('#rule-grid').innerHTML = RULES.map(card).join('');
    $('#constraint-grid').innerHTML = CONSTRAINTS.map(card).join('');
    $('#next-grid').innerHTML = NEXT.map(card).join('');
  }
}

/* ============================================================ actions */
async function refreshAll() {
  state.dayCache.clear();
  try {
    await loadRooms();
    await Promise.all([loadSeries(), loadAudit(), renderCalendar(), loadDay(todayISO())]);
    renderWeekstrip();
    renderRoomList();
  } catch (error) {
    toast('Refresh failed', error.message, 'bad');
  }
}

async function resetDemo() {
  const button = $('#reset-demo');
  button.disabled = true;
  try {
    await api('/admin/reset-demo', { method: 'POST' });
    state.dayCache.clear();
    state.day = todayISO();
    await refreshAll();
    toast('Demo data restored', 'Bookings, series and audit trail re-seeded.', 'ok');
  } catch (error) {
    toast('Reset failed', `${error.code} · ${error.message}`, 'bad');
  } finally {
    button.disabled = false;
  }
}

/* ============================================================ bootstrap */
function bindUi() {
  $$('.nav-item').forEach((btn) => btn.addEventListener('click', () => {
    switchView(btn.dataset.view);
    if (btn.dataset.view === 'api') renderApiList();
  }));
  $$('#office-chips .chip').forEach((chip) => chip.addEventListener('click', () => {
    state.office = chip.dataset.office;
    renderOfficeChips();
    renderWeekstrip();
    renderCalendar();
  }));
  $('#day-prev').addEventListener('click', () => selectDay(addDays(state.day, -1)));
  $('#day-next').addEventListener('click', () => selectDay(addDays(state.day, 1)));
  $('#day-today').addEventListener('click', () => selectDay(todayISO()));
  $('#new-booking').addEventListener('click', () => openNewBooking());
  $('#new-series').addEventListener('click', () => openNewBooking({ mode: 'series' }));
  $('#reset-demo').addEventListener('click', resetDemo);
  $('#api-send').addEventListener('click', sendApiRequest);
  $('#api-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(state.api.lastResponse || '');
      toast('Copied', 'Response JSON is on your clipboard.', 'ok', 2200);
    } catch { toast('Copy failed', 'Clipboard permission was denied.', 'warn'); }
  });
}

async function bootstrap() {
  bindUi();
  tickClocks();
  setInterval(tickClocks, 15000);
  renderDesign();

  try {
    const health = await api('/health');
    setStatus('ok', `API live · ${health.rooms} rooms`);
  } catch (error) {
    setStatus('down', 'API unreachable');
    toast('API unreachable', 'Start the server with uvicorn roomloop.api:app', 'bad', 9000);
    return;
  }

  try {
    await Promise.all([loadRooms(), loadSeries(), loadAudit()]);
    renderWeekstrip();
    await renderCalendar();
    renderApiList();
  } catch (error) {
    toast('Could not load data', error.message, 'bad');
  }

  setTimeout(() => {
    const pill = $('#stat-free-sub');
    if (pill) pill.dataset.live = state.day === todayISO() ? '1' : '';
    tickClocks();
  }, 400);
}

bootstrap();
