/* Apple Photos library dashboard — front end.
 *
 * No build step, no dependencies, no network beyond this server. Charts are
 * hand-rolled SVG; the backend already downsamples, so a 30-day view arrives
 * as a few hundred points with min/max bands preserved.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
};

/* ---------------------------------------------------------------- format */
const fmt = {
  int: n => (n === null || n === undefined || Number.isNaN(n)) ? '–'
          : Math.round(n).toLocaleString('en-US'),
  num: (n, d = 1) => (n === null || n === undefined || Number.isNaN(n)) ? '–'
          : n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }),
  pct: n => (n === null || n === undefined) ? '–' : n.toFixed(1) + '%',
  bytes(n) {
    if (n === null || n === undefined) return '–';
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let i = 0, v = Number(n);
    while (Math.abs(v) >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + ' ' + u[i];
  },
  dur(s) {
    if (s === null || s === undefined) return '–';
    s = Math.abs(s);
    if (s < 60) return Math.round(s) + 's';
    if (s < 3600) return Math.round(s / 60) + 'm';
    if (s < 86400) return (s / 3600).toFixed(1) + 'h';
    return (s / 86400).toFixed(1) + 'd';
  },
  ago: ts => ts ? fmt.dur(Date.now() / 1000 - ts) + ' ago' : '–',
  clock: ts => ts ? new Date(ts * 1000).toLocaleTimeString('en-GB') : '–',
  date: ts => ts ? new Date(ts * 1000).toLocaleString('en-GB',
      { day: '2-digit', month: 'short', year: 'numeric' }) : '–',
  stamp: ts => ts ? new Date(ts * 1000).toLocaleString('en-GB',
      { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '–',
  signed: n => (n === null || n === undefined) ? '–'
          : (n > 0 ? '+' : '') + fmt.num(n, 1),
};

/* ------------------------------------------------------------- SVG chart */
const NS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs) {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}

/* series: [{ key, label, color, points: [[ts, v, vmin, vmax], ...], area }] */
function drawChart(host, series, opts = {}) {
  const W = host.clientWidth || 600;
  const H = opts.height || 150;
  const P = { t: 10, r: 52, b: 20, l: 8 };
  host.innerHTML = '';
  const all = series.flatMap(s => s.points || []);
  if (all.length < 2) {
    host.appendChild(el('div', 'empty', opts.emptyText || 'collecting…'));
    return;
  }
  const svg = svgEl('svg', { class: 'chart', width: '100%', height: H,
                             viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none' });

  const now = Date.now() / 1000;
  const x0 = opts.window ? now - opts.window : Math.min(...all.map(p => p[0]));
  const x1 = now;
  let y0 = 0, y1 = Math.max(...all.map(p => Math.max(p[1], p[3] ?? p[1])));
  if (opts.zeroFloor === false) y0 = Math.min(...all.map(p => Math.min(p[1], p[2] ?? p[1])));
  if (y1 === y0) y1 = y0 + 1;
  const pad = (y1 - y0) * 0.12;
  y1 += pad;
  if (opts.zeroFloor === false) y0 -= pad;

  const X = t => P.l + (t - x0) / (x1 - x0) * (W - P.l - P.r);
  const Y = v => H - P.b - (v - y0) / (y1 - y0) * (H - P.t - P.b);

  /* horizontal guides + right-hand value axis */
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = y0 + (y1 - y0) * i / ticks;
    const y = Y(v);
    svg.appendChild(svgEl('line', { x1: P.l, x2: W - P.r, y1: y, y2: y,
                                    stroke: '#1e232a', 'stroke-width': 1 }));
    const lab = svgEl('text', { x: W - P.r + 6, y: y + 3.5, fill: '#5d6674',
                                'font-size': 10, 'font-family': 'var(--mono)' });
    lab.textContent = opts.yFormat ? opts.yFormat(v) : fmt.int(v);
    svg.appendChild(lab);
  }

  /* time axis */
  const span = x1 - x0;
  const tickCount = Math.min(6, Math.max(2, Math.floor(W / 120)));
  for (let i = 0; i <= tickCount; i++) {
    const t = x0 + span * i / tickCount;
    const d = new Date(t * 1000);
    const label = span <= 6 * 3600
      ? d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
      : span <= 3 * 86400
        ? d.toLocaleString('en-GB', { weekday: 'short', hour: '2-digit' })
        : d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
    const tx = svgEl('text', { x: X(t), y: H - 5, fill: '#5d6674', 'font-size': 10,
                               'font-family': 'var(--mono)',
                               'text-anchor': i === 0 ? 'start'
                                            : i === tickCount ? 'end' : 'middle' });
    tx.textContent = label;
    svg.appendChild(tx);
  }

  series.forEach((s, si) => {
    const pts = (s.points || []).filter(p => p[0] >= x0);
    if (pts.length < 2) return;
    const uid = `g${si}-${Math.random().toString(36).slice(2, 7)}`;

    /* min/max band, only meaningful once rows have been rolled up */
    const banded = pts.some(p => p[2] !== null && p[3] !== null && p[3] > p[2]);
    if (banded) {
      const up = pts.map(p => `${X(p[0])},${Y(p[3])}`).join(' L');
      const dn = pts.slice().reverse().map(p => `${X(p[0])},${Y(p[2])}`).join(' L');
      svg.appendChild(svgEl('path', { d: `M${up} L${dn} Z`, fill: s.color,
                                      opacity: .13, stroke: 'none' }));
    }
    if (s.area) {
      const grad = svgEl('linearGradient', { id: uid, x1: 0, y1: 0, x2: 0, y2: 1 });
      grad.appendChild(svgEl('stop', { offset: '0%', 'stop-color': s.color, 'stop-opacity': .30 }));
      grad.appendChild(svgEl('stop', { offset: '100%', 'stop-color': s.color, 'stop-opacity': 0 }));
      const defs = svgEl('defs', {});
      defs.appendChild(grad);
      svg.appendChild(defs);
      const d = `M${X(pts[0][0])},${Y(y0)} L` +
                pts.map(p => `${X(p[0])},${Y(p[1])}`).join(' L') +
                ` L${X(pts[pts.length - 1][0])},${Y(y0)} Z`;
      svg.appendChild(svgEl('path', { d, fill: `url(#${uid})`, stroke: 'none' }));
    }
    svg.appendChild(svgEl('path', {
      d: 'M' + pts.map(p => `${X(p[0])},${Y(p[1])}`).join(' L'),
      fill: 'none', stroke: s.color, 'stroke-width': 1.6,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));
    const last = pts[pts.length - 1];
    svg.appendChild(svgEl('circle', { cx: X(last[0]), cy: Y(last[1]), r: 2.6,
                                      fill: s.color }));
  });

  host.appendChild(svg);
}

function windowPicker(host, options, current, onPick) {
  host.innerHTML = '';
  options.forEach(([label, secs]) => {
    const b = el('button', secs === current ? 'on' : '', label);
    b.onclick = () => onPick(secs);
    host.appendChild(b);
  });
}

/* --------------------------------------------------------------- charts registry
 *
 * Each chart declares its own default window, matched to how fast the
 * underlying quantity actually moves: the queue is watched in minutes, the
 * search index in hours, the library census in weeks.
 */
const CHARTS = {
  queue: {
    windows: [['15m', 900], ['1h', 3600], ['6h', 21600], ['24h', 86400], ['7d', 604800], ['30d', 2592000]],
    window: 3600,
    series: [{ key: 'queue.pending', label: 'pending jobs', color: '#4cc4e0', area: true }],
  },
  throughput: {
    windows: [['15m', 900], ['1h', 3600], ['6h', 21600], ['24h', 86400], ['7d', 604800]],
    window: 3600,
    zeroFloor: false,
    series: [
      { key: 'queue.arrivals_per_min', label: 'enqueued /min', color: '#e8b451' },
      { key: 'queue.departures_per_min', label: 'completed /min (derived)', color: '#4fd18b' },
      { key: 'queue.net_drain_per_min', label: 'net drain /min', color: '#7d8cff' },
    ],
  },
  writes: {
    windows: [['15m', 900], ['1h', 3600], ['6h', 21600], ['24h', 86400], ['7d', 604800]],
    window: 3600,
    series: [
      { key: 'writes.transactions_60s', label: 'saves /min', color: '#7d8cff', area: true },
    ],
  },
  index: {
    windows: [['1h', 3600], ['6h', 21600], ['24h', 86400], ['7d', 604800], ['30d', 2592000]],
    window: 86400,
    yFormat: v => fmt.bytes(v),
    series: [{ key: 'search.bytes', label: 'psi.sqlite size', color: '#4cc4e0', area: true }],
  },
  indexed: {
    windows: [['6h', 21600], ['24h', 86400], ['7d', 604800], ['30d', 2592000]],
    window: 604800,
    zeroFloor: false,
    series: [
      { key: 'search.assets_indexed', label: 'assets in the index', color: '#4fd18b' },
      { key: 'search.keywords_indexed', label: 'keywords searchable', color: '#e8b451' },
    ],
  },
  content: {
    windows: [['24h', 86400], ['7d', 604800], ['30d', 2592000], ['1y', 31536000]],
    window: 2592000,
    zeroFloor: false,
    series: [
      { key: 'assets.active', label: 'assets', color: '#4cc4e0' },
      { key: 'people.faces_named', label: 'named faces', color: '#7d8cff' },
      { key: 'keywords.keyword_links', label: 'keyword links', color: '#e8b451' },
    ],
  },
  scheduler: {
    windows: [['1h', 3600], ['6h', 21600], ['24h', 86400], ['7d', 604800]],
    window: 21600,
    series: [
      { key: 'log.decision_CP', label: 'Can Proceed', color: '#4fd18b' },
      { key: 'log.decision_MNP', label: 'May Not Proceed', color: '#e8b451' },
      { key: 'log.decision_AMNP', label: 'Absolutely May Not', color: '#f2685f' },
    ],
  },
  storage: {
    windows: [['24h', 86400], ['7d', 604800], ['30d', 2592000], ['1y', 31536000]],
    window: 604800,
    zeroFloor: false,
    yFormat: v => fmt.bytes(v),
    series: [
      { key: 'db.bytes', label: 'Photos.sqlite', color: '#4cc4e0' },
      { key: 'db.wal_bytes', label: 'WAL', color: '#e8b451' },
      { key: 'fs.volume_free_bytes', label: 'volume free', color: '#4fd18b' },
    ],
  },
};

async function refreshChart(name) {
  const cfg = CHARTS[name];
  const host = $(`#chart-${name}`);
  if (!host) return;
  const keys = cfg.series.map(s => s.key).join(',');
  let data;
  try {
    const r = await fetch(`/api/series?metrics=${encodeURIComponent(keys)}&window=${cfg.window}`);
    data = await r.json();
  } catch {
    return;                    // server restarting; tick() reports the outage
  }
  const series = cfg.series.map(s => ({ ...s, points: data.series[s.key] || [] }));
  drawChart(host, series, { window: cfg.window, height: cfg.height || 150,
                            zeroFloor: cfg.zeroFloor, yFormat: cfg.yFormat });
  const legend = $(`#legend-${name}`);
  if (legend) {
    legend.innerHTML = '';
    series.forEach(s => {
      const span = el('span');
      const i = el('i');
      i.style.background = s.color;
      span.appendChild(i);
      const last = s.points.length ? s.points[s.points.length - 1][1] : null;
      span.appendChild(document.createTextNode(
        s.label + (last !== null ? ` · ${cfg.yFormat ? cfg.yFormat(last) : fmt.num(last, 1)}` : '')));
      legend.appendChild(span);
    });
  }
}

function initCharts() {
  for (const name in CHARTS) {
    const picker = $(`#win-${name}`);
    if (!picker) continue;
    const render = () => {
      windowPicker(picker, CHARTS[name].windows, CHARTS[name].window, secs => {
        CHARTS[name].window = secs;
        render();
        refreshChart(name);
      });
    };
    render();
  }
}

function refreshAllCharts() { Object.keys(CHARTS).forEach(refreshChart); }

/* ------------------------------------------------------------ renderers */
function statList(host, pairs) {
  host.innerHTML = '';
  pairs.forEach(([k, v, cls]) => {
    if (v === undefined) return;
    const row = el('div', 'stat');
    row.appendChild(el('span', 'k', k));
    const val = el('span', 'v', v);
    if (cls) val.style.color = `var(--${cls})`;
    row.appendChild(val);
    host.appendChild(row);
  });
}

function table(host, cols, rows) {
  host.innerHTML = '';
  if (!rows.length) { host.appendChild(el('div', 'empty', 'nothing to show')); return; }
  // Every table gets its own horizontal scroller. Wide tables in a narrow
  // column must scroll inside their panel, never push the page sideways.
  const wrap = el('div', 'tblwrap');
  const t = el('table');
  const thead = el('thead'), tr = el('tr');
  cols.forEach(c => {
    const th = el('th', c.num ? 'num' : '', c.label);
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  t.appendChild(thead);
  const tb = el('tbody');
  rows.forEach(r => {
    const row = el('tr');
    cols.forEach(c => {
      const td = el('td', [c.num ? 'num' : '', c.cls || ''].filter(Boolean).join(' '));
      const v = c.render(r);
      if (v instanceof Node) td.appendChild(v); else td.textContent = v ?? '';
      row.appendChild(td);
    });
    tb.appendChild(row);
  });
  t.appendChild(tb);
  wrap.appendChild(t);
  host.appendChild(wrap);
}

function bars(host, rows) {
  host.innerHTML = '';
  const wrap = el('div', 'bars');
  rows.forEach(r => {
    const row = el('div', 'bar-row');
    row.appendChild(el('div', 'bl', r.label));
    const track = el('div', 'bar-track');
    const fill = el('div', 'bar-fill');
    fill.style.width = Math.max(0, Math.min(100, r.pct)) + '%';
    fill.style.background = r.color || (r.pct >= 99.5 ? 'var(--ok)'
                                      : r.pct >= 50 ? 'var(--accent)'
                                      : r.pct > 0 ? 'var(--warn)' : 'var(--err)');
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el('div', 'bv', r.value));
    wrap.appendChild(row);
  });
  host.appendChild(wrap);
}

const AGE_COLORS = { lt_1h: '#4fd18b', lt_6h: '#4cc4e0', lt_24h: '#7d8cff',
                     lt_7d: '#e8b451', older: '#f2685f' };
const AGE_LABELS = { lt_1h: '<1h', lt_6h: '1–6h', lt_24h: '6–24h',
                     lt_7d: '1–7d', older: '>7d' };

function ageStack(age, total) {
  const wrap = el('div', 'stack');
  wrap.title = Object.entries(age)
    .map(([k, v]) => `${AGE_LABELS[k]}: ${fmt.int(v)}`).join('   ');
  Object.entries(age).forEach(([k, v]) => {
    if (!v) return;
    const s = el('span');
    s.style.width = (100 * v / Math.max(total, 1)) + '%';
    s.style.background = AGE_COLORS[k];
    wrap.appendChild(s);
  });
  return wrap;
}

/* ------------------------------------------------------------- snapshot */
function render(s) {
  const id = s.identity || {};
  $('#lib-name').textContent = id.name || '–';
  $('#lib-path').textContent = id.path || '';
  document.title = (id.name || 'Photos') + ' — library dashboard';

  const stale = s.updated_at ? (Date.now() / 1000 - s.updated_at) : 999;
  const dot = $('#status-dot');
  dot.className = 'dot live ' + (s.status !== 'ok' ? 'err' : stale > 60 ? 'warn' : 'ok');
  $('#status-text').textContent = s.status === 'ok'
    ? `live · sampled ${fmt.ago(s.updated_at)}`
    : (s.status + (s.status_detail ? ' — ' + s.status_detail : ''));

  const acc = s.access || {};
  const badge = $('#access-mode');
  // textContent wipes the injected hint button, so re-attach it afterwards.
  badge.textContent = acc.mode === 'snapshot'
    ? (acc.apfs_clone ? 'snapshot (clone) · library never opened'
                      : 'snapshot · library never opened')
    : 'read-only';
  badge.className = 'pill ' + (acc.mode === 'snapshot' ? 'ok' : 'dim');
  attachHelp(badge.parentElement);

  $('#fact-schema').textContent = id.schema_version ?? '–';
  $('#fact-db').textContent = fmt.bytes(id.db_bytes);
  $('#fact-wal').textContent = fmt.bytes(id.wal_bytes);
  $('#fact-inode').textContent = id.inode ?? '–';
  $('#fact-uptime').textContent = fmt.dur(s.sampler?.uptime_s);

  renderBanners(s);
  renderKpis(s);
  renderQueue(s);
  renderAnalysis(s);
  renderScheduler(s);
  renderWrites(s);
  renderContent(s);
  renderDatabase(s);
  renderStorage(s);
  renderDiag(s);
}

/* Doc pointers, fetched once from /api/docs so the wording lives in one place
 * (photodash/docs.py) and cannot drift between the terminal and the browser. */
let DOCS = {};
async function loadDocs() {
  try { DOCS = (await (await fetch('/api/docs')).json()).topics || {}; } catch {}
}

function docLink(key) {
  const t = DOCS[key];
  if (!t) return null;
  const wrap = el('div', 'doc-ref');
  wrap.appendChild(el('span', '', 'Read more: '));
  wrap.appendChild(el('b', '', `${t.doc} → ${t.anchor.replace(/\s*>\s*/g, ' → ')}`));
  if (t.internals) {
    wrap.appendChild(document.createTextNode('  ·  background: '));
    wrap.appendChild(el('b', '', t.internals));
  }
  return wrap;
}

function banner(host, kind, html, docKey) {
  const b = el('div', 'banner ' + kind);
  const body = el('div');
  body.innerHTML = html;
  const link = docKey && docLink(docKey);
  if (link) body.appendChild(link);
  b.appendChild(body);
  host.appendChild(b);
}

function renderBanners(s) {
  const host = $('#banners');
  host.innerHTML = '';

  if (s.status && s.status !== 'ok') {
    banner(host, 'err',
      `<b>${s.status}.</b> ${s.status_detail ? `<code>${s.status_detail}</code>` : ''} ` +
      `The dashboard keeps showing the last successful sample until it can read again.`,
      'unavailable');
  }
  const pk = s.pk_health;
  if (pk && pk.faults && pk.faults.length) {
    const names = pk.faults.map(f => `${f.table} (MAX(Z_PK)=${f.max_pk} > Z_MAX=${f.z_max})`).join(', ');
    banner(host, 'err',
      `<b>Primary-key counter behind.</b> Photos cannot save, and background analysis writes ` +
      `will fail silently: ${names}. <code>PRAGMA integrity_check</code> returns <code>ok</code> ` +
      `in this state, so it cannot rule the problem out.`, 'pk-fault');
  }
  if (s.log && s.log.constraint_violations > 0) {
    banner(host, 'err',
      `<b>${s.log.constraint_violations} persistent-history constraint violations</b> in the ` +
      `last ${s.log.window_minutes} minutes of the unified log — the signature of a counter ` +
      `that has fallen behind its table.`, 'pk-fault');
  }
  if (s.processes && s.processes.unattended) {
    banner(host, 'warn',
      `<b>Nothing holds this library.</b> No daemon has it open, so no background analysis or ` +
      `indexing is happening. A secondary library only advances while Photos.app is open on it; ` +
      `a promoted System Photo Library is held permanently.`, 'unattended');
  }
  if (s.log && s.log.throttle_state === 'fully throttled') {
    banner(host, 'warn',
      `<b>Analysis is fully throttled.</b> Every scheduler verdict in the last ` +
      `${s.log.window_minutes} minutes was <i>May Not Proceed</i>. The heavy policy is ` +
      `<i>Device Activity</i>: it will not run while the Mac is in use. Leave the machine idle, ` +
      `on mains power, and prevent sleep.`, 'throttled');
  }
  if (s.access && s.access.error) {
    banner(host, 'err',
      `<b>Snapshot copy failed.</b> <code>${s.access.error}</code>`, 'snapshot');
  }
  const errs = s.sampler && s.sampler.errors;
  if (errs && errs.length) {
    banner(host, 'warn',
      `<b>${errs.length} collector error${errs.length > 1 ? 's' : ''}.</b> ` +
      `Some panels may be stale — the most recent is <code>${errs[errs.length - 1]}</code>.`,
      'diagnostics');
  }
}

function kpi(host, { label, value, unit, foot, cls }) {
  const k = el('div', 'kpi' + (cls ? ' ' + cls : ''));
  k.appendChild(el('div', 'label', label));
  const v = el('div', 'value');
  v.textContent = value;
  if (unit) { const u = el('span', 'unit', ' ' + unit); v.appendChild(u); }
  k.appendChild(v);
  if (foot) k.appendChild(el('div', 'foot', foot));
  host.appendChild(k);
}

function renderKpis(s) {
  const host = $('#kpis');
  host.innerHTML = '';
  const q = s.queue || {}, d = q.derived || {}, log = s.log || {},
        p = s.processes || {}, srch = s.search || {}, pk = s.pk_health || {};

  kpi(host, {
    label: 'Pending jobs', value: fmt.int(q.pending), cls: 'hero',
    foot: d.trend ? `${d.trend} · ${fmt.signed(d.delta)} last sample` : 'baseline…',
  });
  kpi(host, {
    label: 'Completing (derived)', value: fmt.num(d.departures_per_min, 1), unit: '/min',
    foot: `enqueued ${fmt.num(d.arrivals_per_min, 1)}/min`,
    cls: d.departures_per_min > 0 ? 'ok' : '',
  });
  kpi(host, {
    label: 'Drain ETA',
    value: d.eta_hours ? (d.eta_hours > 48 ? fmt.num(d.eta_hours / 24, 1) : fmt.num(d.eta_hours, 1)) : '–',
    unit: d.eta_hours ? (d.eta_hours > 48 ? 'days' : 'hours') : '',
    foot: d.trend === 'growing' ? 'queue is growing, not draining' : 'at the current pace',
    cls: d.trend === 'growing' ? 'warn' : '',
  });
  kpi(host, {
    label: 'Scheduler', value: log.throttle_state || '–',
    foot: log.decisions_total
      ? Object.entries(log.decisions).map(([k, v]) => `${k}=${v}`).join(' ')
      : `no verdicts in ${log.window_minutes || 5}m`,
    cls: log.throttle_state === 'running' ? 'ok'
       : log.throttle_state === 'fully throttled' ? 'err'
       : log.throttle_state === 'mostly throttled' ? 'warn' : '',
  });
  kpi(host, {
    label: 'Library held by', value: fmt.int(p.holder_count), unit: 'proc',
    foot: p.photos_running ? 'Photos.app is open' : 'Photos.app not running',
    cls: p.unattended ? 'err' : 'ok',
  });
  kpi(host, {
    label: 'Search index',
    value: srch.index_exceeds_library ? fmt.int(srch.assets_indexed)
         : fmt.pct(srch.assets_total ? 100 * srch.assets_indexed / srch.assets_total : null),
    unit: srch.index_exceeds_library ? 'rows' : '',
    foot: srch.index_exceeds_library
      ? `more than the ${fmt.int(srch.assets_total)} assets in the library`
      : `${fmt.int(srch.assets_indexed)} of ${fmt.int(srch.assets_total)} assets`,
    cls: srch.index_exceeds_library ? '' : '',
  });
  kpi(host, {
    label: 'Keywords searchable',
    value: srch.keywords_total ? `${fmt.int(srch.keywords_indexed)}` : '–',
    unit: srch.keywords_total ? `/ ${fmt.int(srch.keywords_total)}` : '',
    foot: srch.keywords_missing_count ? `${srch.keywords_missing_count} not yet indexed` : 'all indexed',
    cls: srch.keywords_missing_count ? 'warn' : 'ok',
  });
  kpi(host, {
    label: 'Save integrity', value: pk.ok === true ? 'OK' : pk.ok === false ? 'FAULT' : '–',
    foot: pk.checked ? `${pk.checked} counters checked` : '',
    cls: pk.ok === true ? 'ok' : pk.ok === false ? 'err' : '',
  });
}

/* ---- queue ---- */
function renderQueue(s) {
  const q = s.queue || {}, mix = s.queue_mix || {};
  statList($('#queue-stats'), [
    ['pending', fmt.int(q.pending)],
    ['distinct identifiers', fmt.int(q.distinct_identifiers)],
    ['delayed (future date)', fmt.int(q.delayed)],
    ['enqueued last 1h', fmt.int(q.arrivals_1h)],
    ['enqueued last 24h', fmt.int(q.arrivals_24h)],
    ['oldest item', fmt.dur(q.oldest_age_s)],
    ['newest item', fmt.ago(q.newest_ts)],
  ]);

  const buckets = mix.buckets || [];
  const total = buckets.reduce((a, b) => a + b.count, 0) || 1;
  table($('#queue-mix'), [
    { label: 'type', render: r => r.jobtype, num: true },
    { label: 'flags', render: r => {
        const w = el('span');
        w.appendChild(el('span', 'pill dim', String(r.flags)));
        if (r.flag_bits.length > 1) {
          w.appendChild(document.createTextNode(' '));
          w.appendChild(el('span', 'ref', '= ' + r.flag_bits.join('|')));
        }
        return w;
      } },
    { label: 'queued', render: r => fmt.int(r.count), num: true },
    { label: 'share', render: r => fmt.pct(100 * r.count / total), num: true },
    { label: 'age profile', render: r => ageStack(r.age, r.count) },
    { label: 'attributable', render: r => {
        const pct = 100 * r.attributable / Math.max(r.count, 1);
        return el('span', 'pill ' + (pct > 99 ? 'ok' : pct > 0 ? 'warn' : 'dim'),
                  pct > 99 ? 'all' : pct === 0 ? 'none' : fmt.pct(pct));
      } },
    { label: 'Δ/min', render: r => r.delta_per_min === undefined ? '–'
        : fmt.signed(r.delta_per_min), num: true },
    { label: 'oldest', render: r => fmt.stamp(r.oldest_ts), num: true },
  ], buckets);

  const legend = $('#age-legend');
  legend.innerHTML = '';
  Object.entries(AGE_LABELS).forEach(([k, label]) => {
    const sp = el('span');
    const i = el('i');
    i.style.background = AGE_COLORS[k];
    sp.appendChild(i);
    sp.appendChild(document.createTextNode(label));
    legend.appendChild(sp);
  });

  table($('#queue-hot'), [
    { label: 'asset', cls: 'mono trunc', render: r => r.filename || r.uuid },
    { label: 'kind', render: r => r.kind ? el('span', 'pill dim', r.kind)
        : el('span', 'pill', 'not an asset') },
    { label: 'jobs', render: r => fmt.int(r.count), num: true },
    { label: 'types', cls: 'mono', render: r => r.jobtypes },
    { label: 'waiting', render: r => fmt.dur(Date.now() / 1000 - r.oldest_ts), num: true },
  ], s.hot_assets || []);

  table($('#queue-named'), [
    { label: 'identifier', cls: 'mono trunc', render: r => r.identifier },
    { label: 'queued', render: r => fmt.int(r.count), num: true },
  ], mix.named_identifiers || []);
}

/* ---- analysis ---- */
function renderAnalysis(s) {
  const cov = s.coverage || {};
  bars($('#coverage-bars'), (cov.workers || []).map(w => ({
    label: w.label,
    pct: w.pct,
    value: `${fmt.int(w.done)} / ${fmt.int(w.total)}`,
  })));

  const side = s.side_tables || {}, srch = s.search || {};
  statList($('#analysis-stats'), [
    ['scene classifications', fmt.int(side.scene_classifications)],
    ['scene prints', fmt.int(side.scene_prints)],
    ['computed attributes', fmt.int(side.computed_attributes)],
    ['visual search rows', fmt.int(side.visual_search)],
    ['ZASSETANALYSISSTATE', fmt.int(side.asset_analysis_state_rows)],
  ]);

  statList($('#index-stats'), [
    ['psi.sqlite', fmt.bytes(srch.bytes)],
    ['psi WAL', fmt.bytes(srch.wal_bytes)],
    ['last written', fmt.ago(srch.mtime)],
    ['rows in assets', fmt.int(srch.assets_indexed),
      srch.index_exceeds_library ? 'warn' : null],
    ['assets in library', fmt.int(srch.assets_total)],
    ['search terms', fmt.int(srch.groups_total)],
    ['Spotlight index', srch.spotlight_present ? 'present' : 'absent'],
  ]);
  const note = $('#index-note');
  note.innerHTML = srch.index_exceeds_library
    ? `The index holds <b>${fmt.int(srch.assets_indexed)}</b> rows against ` +
      `<b>${fmt.int(srch.assets_total)}</b> untrashed assets. Why is not established: the byte ` +
      `order of the split UUID (<code>uuid_0</code>, <code>uuid_1</code>) was never verified, so ` +
      `these rows cannot be joined back to <code>ZASSET</code> to tell whether they are stale or ` +
      `cover objects other than assets (§22). Shown as a count rather than a &gt;100% coverage figure.`
    : '';

  table($('#index-categories'), [
    { label: 'category', render: r => r.category, num: true },
    { label: 'meaning', render: r => r.meaning
        ? r.meaning : el('span', 'ref', 'not established (§22)') },
    { label: 'terms', render: r => fmt.int(r.count), num: true },
  ], srch.categories || []);

  const missing = $('#index-missing');
  missing.innerHTML = '';
  if (srch.keywords_missing_count) {
    missing.appendChild(el('div', 'footnote',
      `${srch.keywords_missing_count} of ${srch.keywords_total} keyword titles have no ` +
      `category-1200 entry yet:`));
    const w = el('div');
    w.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px;margin-top:6px';
    (srch.keywords_missing || []).forEach(k => w.appendChild(el('span', 'pill warn', k)));
    missing.appendChild(w);
  } else if (srch.keywords_total) {
    missing.appendChild(el('div', 'footnote',
      `All ${srch.keywords_total} keyword titles are present in the index at category 1200. ` +
      `Keyword indexing runs independently of the analysis queue, so a large pending queue ` +
      `does not mean keywords are unsearchable (§19).`));
  }
}

/* ---- scheduler & processes ---- */
function renderScheduler(s) {
  const log = s.log || {};
  statList($('#sched-stats'), [
    ['window', (log.window_minutes || 5) + ' min'],
    ['collected', fmt.ago(log.collected_at)],
    ['decisions', fmt.int(log.decisions_total)],
    ['can proceed', log.can_proceed_pct === null || log.can_proceed_pct === undefined
      ? '–' : fmt.pct(log.can_proceed_pct)],
    ['PhotoKit ok', fmt.int(log.photokit_ok)],
    ['PhotoKit failed', fmt.int(log.photokit_fail),
      log.photokit_fail > (log.photokit_ok || 0) ? 'err' : null],
    ['scene cancelled', fmt.int(log.scene_cancelled)],
    ['face cancelled', fmt.int(log.face_cancelled)],
    ['userCanceledErr', fmt.int(log.user_cancelled)],
    ['Spotlight donations', fmt.int(log.spotlight_donations)],
    ['search-index jobs', fmt.int(log.search_index_jobs)],
    ['log lines scanned', fmt.int(log.log_lines)],
  ]);

  table($('#sched-policies'), [
    { label: 'policy evaluated', cls: 'mono trunc', render: r => r[0] },
    { label: 'times', render: r => fmt.int(r[1]), num: true },
  ], log.policies || []);

  const p = s.processes || {};
  table($('#proc-table'), [
    { label: 'process', cls: 'mono', render: r => r.process },
    { label: 'pid', render: r => r.pid, num: true },
    { label: 'open handles', render: r => fmt.int(r.handles), num: true },
  ], p.holders || []);

  const notRunning = Object.entries(p.running || {})
    .filter(([, n]) => !n).map(([k]) => k);
  const notes = [];
  if (notRunning.length) notes.push('not running: ' + notRunning.join(', '));
  if (p.unreadable_processes) {
    notes.push(`${p.unreadable_processes} running process(es) could not be inspected — ` +
               `"no holders" cannot be concluded from this`);
  }
  $('#proc-note').textContent = notes.join(' · ');

  const lines = $('#log-lines');
  lines.innerHTML = '';
  if ((log.notable_lines || []).length) {
    (log.notable_lines || []).slice().reverse().forEach(l => {
      const d = el('div', 'mono');
      d.style.cssText = 'font-size:11px;color:var(--fg-dim);padding:2px 0;' +
                        'border-bottom:1px solid var(--line-soft);word-break:break-all';
      d.textContent = l;
      lines.appendChild(d);
    });
  } else {
    lines.appendChild(el('div', 'empty', 'no save-path or repair messages in this window'));
  }
}

/* ---- writes ---- */
function renderWrites(s) {
  const w = s.writes || {};
  statList($('#write-stats'), [
    ['saves last 60s', fmt.int(w.transactions_60s)],
    ['saves last 5m', fmt.int(w.transactions_5m)],
    ['saves last 1h', fmt.int(w.transactions_1h)],
    ['saves last 24h', fmt.int(w.transactions_24h)],
    ['last write', w.seconds_since_last_write === null ? '–'
      : fmt.dur(w.seconds_since_last_write) + ' ago'],
    ['ATRANSACTION max pk', fmt.int(w.transaction_max_pk)],
    ['ACHANGE max pk', fmt.int(w.change_max_pk)],
    ['string pool', fmt.int(w.string_pool)],
  ]);

  table($('#write-authors'), [
    { label: 'writing subsystem (last hour)', cls: 'mono trunc', render: r => r.name },
    { label: 'saves', render: r => fmt.int(r.count), num: true },
    { label: 'last', render: r => fmt.ago(r.last_ts), num: true },
  ], s.authors || []);

  table($('#write-changes'), [
    { label: 'entity', render: r => r.entity },
    { label: 'Z_ENT', cls: 'mono', render: r => r.z_ent, num: true },
    { label: 'change', render: r => el('span', 'pill dim', r.change) },
    { label: 'rows', render: r => fmt.int(r.count), num: true },
  ], s.changes || []);
}

/* ---- content ---- */
function renderContent(s) {
  const a = s.assets || {}, p = s.people || {}, al = s.albums || {},
        k = s.keywords || {}, r = s.resources || {};

  statList($('#asset-stats'), [
    ['total rows', fmt.int(a.total)],
    ['own, not trashed', fmt.int(a.active)],
    ['photos', fmt.int(a.photos)],
    ['videos', fmt.int(a.videos)],
    ['shared-album', fmt.int(a.shared_album)],
    ['trashed', fmt.int(a.trashed)],
    ['hidden', fmt.int(a.hidden)],
    ['excluded from grid', fmt.int(a.excluded)],
    ['favourites', fmt.int(a.favorites)],
    ['edited', fmt.int(a.edited)],
    ['with GPS', fmt.int(a.with_location)],
    ['iCloud synced', fmt.int(a.cloud_synced)],
  ]);
  $('#asset-range').textContent = a.oldest_capture_ts
    ? `captures span ${fmt.date(a.oldest_capture_ts)} → ${fmt.date(a.newest_capture_ts)}` +
      (a.newest_import_ts ? ` · last import ${fmt.ago(a.newest_import_ts)}` : '')
    : '';

  statList($('#people-stats'), [
    ['detected faces', fmt.int(p.faces_total)],
    ['visible faces', fmt.int(p.faces_visible)],
    ['named faces', fmt.int(p.faces_named)],
    ['manual faces', fmt.int(p.faces_manual)],
    ['person clusters', fmt.int(p.persons_total)],
    ['named people', fmt.int(p.persons_named)],
    ['face prints', fmt.int(p.faceprints)],
    ['face crops', fmt.int(p.facecrops)],
    ['deferred rebuilds', fmt.int(p.deferred_rebuild)],
  ]);
  table($('#top-people'), [
    { label: 'person', render: r => r.name },
    { label: 'faces', render: r => fmt.int(r.faces), num: true },
  ], p.top || []);

  statList($('#album-stats'), [
    ['user albums', fmt.int(al.user_albums)],
    ['folders', fmt.int(al.folders)],
    ['smart albums', fmt.int(al.smart_albums)],
    ['shared albums', fmt.int(al.shared_albums)],
    ['import sessions', fmt.int(al.import_sessions)],
    ['album memberships', fmt.int(al.album_memberships)],
    ['moments', fmt.int(al.moments)],
    ['memories', fmt.int(al.memories)],
  ]);

  statList($('#keyword-stats'), [
    ['distinct keywords', fmt.int(k.keywords_total)],
    ['keyword links', fmt.int(k.keyword_links)],
    ['assets tagged', fmt.int(k.assets_tagged)],
    ['join table', k.keyword_join_table || '–'],
  ]);
  table($('#top-keywords'), [
    { label: 'keyword', render: r => r.keyword },
    { label: 'assets', render: r => fmt.int(r.assets), num: true },
  ], k.top || []);

  statList($('#resource-stats'), [
    ['resource rows', fmt.int(r.resources_total)],
    ['on this Mac', fmt.int(r.local_present)],
    ['not local', fmt.int(r.local_absent)],
    ['in iCloud', fmt.int(r.remote_present)],
    ['local bytes', fmt.bytes(r.bytes_local)],
    ['cloud masters', fmt.int(r.cloud_masters)],
  ]);
  table($('#resource-types'), [
    { label: 'ZRESOURCETYPE', render: r => r.type, num: true },
    { label: 'rows', render: r => fmt.int(r.count), num: true },
    { label: 'bytes', render: r => fmt.bytes(r.bytes), num: true },
  ], r.by_type || []);
}

/* ---- database ---- */
function renderDatabase(s) {
  table($('#census'), [
    { label: 'table', cls: 'mono', render: r => r.table },
    { label: 'rows', render: r => fmt.int(r.rows), num: true },
    { label: 'Z_MAX', render: r => r.z_max === null || r.z_max === undefined
        ? el('span', 'ref', 'no counter') : fmt.int(r.z_max), num: true },
    { label: 'state', render: r => {
        if (r.z_max === null || r.z_max === undefined) return '';
        return el('span', 'pill ok', 'ok');
      } },
  ], s.census || []);

  const pk = s.pk_health || {};
  $('#pk-summary').textContent = pk.checked
    ? `${pk.checked} entity counters compared against MAX(Z_PK)` +
      (pk.faults?.length ? ` — ${pk.faults.length} behind` : ' — none behind')
    : '';
  table($('#pk-faults'), [
    { label: 'table', cls: 'mono', render: r => r.table },
    { label: 'MAX(Z_PK)', render: r => fmt.int(r.max_pk), num: true },
    { label: 'Z_MAX', render: r => fmt.int(r.z_max), num: true },
    { label: 'behind by', render: r => fmt.int(r.behind_by), num: true },
  ], pk.faults || []);
}

/* ---- storage ---- */
function renderStorage(s) {
  const fs = s.filesystem, id = s.identity || {};
  const host = $('#fs-stats');
  if (!fs) {
    host.innerHTML = '';
    host.appendChild(el('div', 'empty',
      'filesystem inventory runs every 6 hours (or is disabled with --no-fs-walk)'));
  } else {
    const pairs = [];
    Object.entries(fs.directories || {}).forEach(([k, v]) => {
      pairs.push([k, `${fmt.bytes(v.bytes)} · ${fmt.int(v.files)} files`]);
    });
    pairs.push(['originals: assets', fmt.int(fs.originals_assets)]);
    pairs.push(['originals: companions', fmt.int(fs.originals_companions)]);
    if (fs.dtrash) pairs.push(['originals/.dtrash', `${fmt.bytes(fs.dtrash.bytes)} · ${fmt.int(fs.dtrash.files)} files`, 'warn']);
    pairs.push(['volume free', fmt.bytes(fs.volume_free_bytes)]);
    pairs.push(['volume total', fmt.bytes(fs.volume_total_bytes)]);
    pairs.push(['walked', fmt.ago(fs.collected_at)]);
    statList(host, pairs);
  }
  statList($('#db-files'), [
    ['Photos.sqlite', fmt.bytes(id.db_bytes)],
    ['-wal', fmt.bytes(id.wal_bytes)],
    ['-shm', fmt.bytes(id.shm_bytes)],
    ['last written', fmt.ago(id.db_mtime)],
    ['inode', String(id.inode ?? '–')],
    ['schema version', String(id.schema_version ?? '–')],
    ['meta schema', String(id.meta_schema_version ?? '–')],
  ]);
}

/* ---- diagnostics ---- */
function renderDiag(s) {
  const sm = s.sampler || {}, h = s.history || {};
  table($('#tiers'), [
    { label: 'tier', render: r => r[0] },
    { label: 'every', render: r => fmt.dur(r[1]), num: true },
    { label: 'passes', render: r => fmt.int(r[2]), num: true },
    { label: 'last', render: r => r[3] ? fmt.ago(r[3]) : 'pending', num: true },
  ], Object.keys(sm.intervals || {}).map(t =>
      [t, sm.intervals[t], sm.passes?.[t] ?? 0, sm.last_pass?.[t]]));

  const durs = Object.entries(sm.durations || {}).sort((a, b) => b[1] - a[1]);
  table($('#collector-times'), [
    { label: 'collector', cls: 'mono', render: r => r[0] },
    { label: 'last run', render: r => fmt.num(r[1] * 1000, 0) + ' ms', num: true },
  ], durs);

  statList($('#hist-stats'), [
    ['store', h.file ? h.file.split('/').pop() : '–'],
    ['size', fmt.bytes(h.bytes)],
    ['metrics', fmt.int(h.metrics)],
    ['events', fmt.int(h.events)],
    ['history since', h.first_ts ? fmt.stamp(h.first_ts) : '–'],
    ...Object.entries(h.rows_by_res || {}).map(([res, n]) =>
      [res === '0' ? 'raw samples' : `${fmt.dur(Number(res))} buckets`, fmt.int(n)]),
  ]);

  const errs = $('#sampler-errors');
  errs.innerHTML = '';
  if ((sm.errors || []).length) {
    sm.errors.slice().reverse().forEach(e => {
      const d = el('div', 'mono');
      d.style.cssText = 'font-size:11px;color:var(--err);padding:2px 0';
      d.textContent = e;
      errs.appendChild(d);
    });
  } else {
    errs.appendChild(el('div', 'footnote', 'no collector errors'));
  }
}

/* ---- events ---- */
async function refreshEvents() {
  let events;
  try {
    const r = await fetch('/api/events?limit=120');
    ({ events } = await r.json());
  } catch {
    return;
  }
  const host = $('#events');
  host.innerHTML = '';
  if (!events.length) {
    host.appendChild(el('div', 'empty', 'no transitions recorded yet'));
    return;
  }
  events.forEach(e => {
    const row = el('div', 'ev ' + (e.severity === 'error' ? 'error' : e.severity));
    row.appendChild(el('div', 't', fmt.stamp(e.ts)));
    const kind = el('div');
    kind.appendChild(el('span', 'pill ' + (e.severity === 'error' ? 'err'
      : e.severity === 'warn' ? 'warn' : 'dim'), e.kind));
    row.appendChild(kind);
    row.appendChild(el('div', 'm', e.message));
    host.appendChild(row);
  });
}

/* ------------------------------------------------------- library picker */
async function refreshLibraries() {
  let data;
  try {
    data = await (await fetch('/api/libraries')).json();
  } catch { return; }
  const menu = $('#libpick-menu');
  menu.innerHTML = '';
  (data.libraries || []).forEach(l => {
    const b = el('button');
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    const isCurrent = l.path === data.current;
    const name = el('span', isCurrent ? 'cur' : '',
      (isCurrent ? '● ' : '') + l.name +
      (l.readable ? '' : '  — blocked, needs Full Disk Access'));
    b.appendChild(name);
    b.appendChild(el('span', 'p', l.path));
    b.disabled = !l.readable || isCurrent;
    b.onclick = async e => {
      e.stopPropagation();
      $('#libpick').classList.remove('open');
      $('#status-text').textContent = 'switching library…';
      try {
        const r = await fetch('/api/select-library', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: l.path }),
        });
        const res = await r.json();
        if (!res.ok) { $('#status-text').textContent = res.error || 'switch failed'; return; }
        await tick();
        refreshAllCharts();
        refreshEvents();
        refreshLibraries();
      } catch {
        $('#status-text').textContent = 'switch failed';
      }
    };
    menu.appendChild(b);
  });
  if (!menu.children.length) menu.appendChild(el('div', 'empty', 'no libraries found'));
}

function initLibraryPicker() {
  const pick = $('#libpick'), btn = $('#libpick-btn');
  btn.onclick = e => {
    e.stopPropagation();
    const open = pick.classList.toggle('open');
    btn.setAttribute('aria-expanded', String(open));
    if (open) refreshLibraries();
  };
  document.addEventListener('click', () => {
    pick.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
  });
}

/* A tooltip near the right edge would overflow the viewport; open it leftwards. */
function flipEdgeTooltips() {
  document.querySelectorAll('.hint').forEach(h => {
    const room = window.innerWidth - h.getBoundingClientRect().left;
    h.classList.toggle('flip', room < 440);
  });
}

function renderWorkerReference() {
  const host = $('#worker-ref');
  if (!host || host.children.length) return;
  WORKERS.forEach(([name, what]) => {
    host.appendChild(el('dt', '', name));
    host.appendChild(el('dd', '', what));
  });
}

/* ------------------------------------------------------------------ loop */
async function tick() {
  try {
    const r = await fetch('/api/snapshot');
    render(await r.json());
  } catch (err) {
    $('#status-dot').className = 'dot err';
    $('#status-text').textContent = 'dashboard server unreachable';
  }
}

let paused = false;
document.addEventListener('visibilitychange', () => { paused = document.hidden; });

async function boot() {
  await loadDocs();
  initCharts();
  initLibraryPicker();
  renderWorkerReference();
  attachHelp();
  flipEdgeTooltips();
  await tick();
  refreshAllCharts();
  refreshEvents();
  refreshLibraries();
  setInterval(() => { if (!paused) tick(); }, 5000);
  setInterval(() => { if (!paused) refreshAllCharts(); }, 15000);
  setInterval(() => { if (!paused) refreshEvents(); }, 30000);
  let rt;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => { refreshAllCharts(); flipEdgeTooltips(); }, 250);
  });
}

boot();
