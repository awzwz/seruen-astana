/* Seruen web client — static, no build step.
   Talks to the orchestrator: POST /api/recommend (SSE), GET /api/landmarks,
   POST /api/feedback, DELETE /api/session/{id}. Contract: docs/CONTRACTS.md.
   Privacy: the exact point lives only in memory here; the server receives a geohash-7 cell. */
(function () {
  'use strict';

  const L = window.SERUEN_I18N;

  /* ------------------------------------------------------------ constants */
  const ASTANA = {lat: 51.128, lon: 71.430};
  const ASTANA_BBOX = {s: 50.82, n: 51.42, w: 70.95, e: 71.95};   // Astana agglomeration
  const WALK_FACTOR = 1.3;          // straight line → walking distance
  const WALK_M_PER_MIN = 75;        // 4.5 km/h
  const STYLE_LIGHT = 'https://tiles.openfreemap.org/styles/liberty';
  const STYLE_DARK = 'https://tiles.openfreemap.org/styles/dark';
  const KEYS = {lang: 'seruen.lang', chips: 'seruen.chips', session: 'seruen.session'};
  const MOCK_FILE = 'mock/sample_stream.txt';
  const MOCK_DELAY = {context: 420, constraints: 380, cards: 950, text: 820, trace: 420, done: 260, error: 200};
  // Exact start of the recorded example (kept in the browser, like any exact point).
  const SAMPLE_ORIGIN = {lat: 51.13095, lon: 71.40310};

  // Built-in landmarks, used only when GET /api/landmarks fails.
  // Coordinates are approximate (приблизительно) — good enough for a start point.
  const BUILTIN_LANDMARKS = [
    {id: 'baiterek', lat: 51.1283, lon: 71.4305, names: {ru: 'Байтерек', kk: 'Бәйтерек', en: 'Baiterek'}},
    {id: 'khan-shatyr', lat: 51.1324, lon: 71.4039, names: {ru: 'Хан Шатыр', kk: 'Хан Шатыр', en: 'Khan Shatyr'}},
    {id: 'mega-silk-way', lat: 51.0890, lon: 71.4080, names: {ru: 'Mega Silk Way', kk: 'Mega Silk Way', en: 'Mega Silk Way'}},
    {id: 'expo', lat: 51.0890, lon: 71.4160, names: {ru: 'ЭКСПО', kk: 'ЭКСПО', en: 'EXPO'}},
    {id: 'aitu', lat: 51.0907, lon: 71.4183, names: {ru: 'Astana IT University', kk: 'Astana IT University', en: 'Astana IT University'}},
    {id: 'hazrat-sultan', lat: 51.1250, lon: 71.4720, names: {ru: 'Мечеть Хазрет Султан', kk: 'Әзірет Сұлтан мешіті', en: 'Hazrat Sultan Mosque'}},
    {id: 'astana-opera', lat: 51.1366, lon: 71.4078, names: {ru: 'Астана Опера', kk: 'Астана Опера', en: 'Astana Opera'}},
    {id: 'nurly-zhol', lat: 51.1056, lon: 71.5325, names: {ru: 'Вокзал Нурлы Жол', kk: 'Нұрлы Жол вокзалы', en: 'Nurly Zhol station'}},
    {id: 'airport-nqz', lat: 51.0222, lon: 71.4669, names: {ru: 'Аэропорт NQZ', kk: 'NQZ әуежайы', en: 'NQZ Airport'}},
    {id: 'central-park', lat: 51.1550, lon: 71.4180, names: {ru: 'Центральный парк', kk: 'Орталық саябақ', en: 'Central Park'}}
  ];

  const TIME_OPTIONS = [30, 60, 90, 120];
  const GROUP_OPTIONS = [1, 2, 3, 4, 5];
  // values follow the backend vocabulary (seruen/engine/intents.py WANT_MAP)
  const INTERESTS = [
    {id: 'coffee', key: 'int_coffee', tags: ['coffee']},
    {id: 'food', key: 'int_food', tags: ['food']},
    {id: 'museum', key: 'int_museum', tags: ['museum']},
    {id: 'park', key: 'int_park', tags: ['park']},
    {id: 'view', key: 'int_view', tags: ['view']},
    {id: 'night', key: 'int_night', tags: ['night']},
    {id: 'unusual', key: 'int_unusual', tags: ['unusual']},
    {id: 'quiet', key: 'int_quiet', tags: ['quiet', 'power']},
    {id: 'events', key: 'int_events', tags: ['events']}
  ];
  const FEEDBACK = [
    {kind: 'closed', key: 'fb_closed', icon: 'xCircle'},
    {kind: 'wrong', key: 'fb_wrong', icon: 'flag'},
    {kind: 'useful', key: 'fb_useful', icon: 'thumb'}
  ];

  /* ------------------------------------------------------------ tiny helpers */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const ESC = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC[c]);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const reduceMotionMQ = window.matchMedia('(prefers-reduced-motion: reduce)');
  const darkMQ = window.matchMedia('(prefers-color-scheme: dark)');
  const mobileMQ = window.matchMedia('(max-width: 959px)');
  const motionOK = () => !reduceMotionMQ.matches;

  const LS = {
    get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } },
    del(k) { try { window.localStorage.removeItem(k); } catch (e) { /* storage unavailable */ } }
  };

  /* ------------------------------------------------------------ icons (inline SVG) */
  const ICONS = {
    walk: '<circle cx="13" cy="4.6" r="1.9"/><path d="M12.6 7.8L11.6 14M11.6 14L9.4 21M11.6 14l2.5 3v4M12.3 9.6L9 12.2v2.4M12.3 9.6l3.3 2.6 2.4.6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/>',
    shieldCheck: '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6l8-3z"/><path d="M8.6 12.2l2.4 2.4 4.4-4.8"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.2"/>',
    alert: '<path d="M12 3.6L2.8 19.6h18.4L12 3.6z"/><path d="M12 10v4.2M12 16.9v.2"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.6 9.6a2.5 2.5 0 1 1 3.6 2.3c-.7.3-1.2.9-1.2 1.6v.5M12 16.9v.2"/>',
    tree: '<path d="M12 21.5v-5"/><path d="M12 2.5l5 7h-3l4 6.5H6l4-6.5H7l5-7z"/>',
    lock: '<rect x="5" y="10.5" width="14" height="10" rx="2.2"/><path d="M8.2 10.5V8a3.8 3.8 0 0 1 7.6 0v2.5"/>',
    locate: '<circle cx="12" cy="12" r="6.8"/><circle cx="12" cy="12" r="2.4"/><path d="M12 2.2v3M12 18.8v3M2.2 12h3M18.8 12h3"/>',
    pin: '<path d="M12 21.2s-7-6.1-7-11.6a7 7 0 0 1 14 0c0 5.5-7 11.6-7 11.6z"/><circle cx="12" cy="9.6" r="2.5"/>',
    landmark: '<circle cx="12" cy="6.4" r="3.4"/><path d="M12 9.8v11M8 21h8M8.6 12.6L12 15.2l3.4-2.6"/>',
    sparkle: '<path d="M12 3.2l1.8 5 5 1.8-5 1.8-1.8 5-1.8-5-5-1.8 5-1.8 1.8-5z"/><path d="M18.6 15.2l.7 1.9 1.9.7-1.9.7-.7 1.9-.7-1.9-1.9-.7 1.9-.7.7-1.9z"/>',
    database: '<ellipse cx="12" cy="5.6" rx="7" ry="2.6"/><path d="M5 5.6v12.8c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V5.6M5 12c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6"/>',
    wheelchair: '<circle cx="11.5" cy="4.2" r="1.8"/><path d="M11.5 7.4v5.8h5.3l2 4.8"/><path d="M11.5 10.2h4.6"/><path d="M8.3 10.8a5.2 5.2 0 1 0 7 6.9"/>',
    calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2.2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    external: '<path d="M14 4h6v6M20 4l-8.5 8.5"/><path d="M18.5 13.5V19a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 19V7A1.5 1.5 0 0 1 5 5.5h5.5"/>',
    thumb: '<path d="M7 10.5v10H4v-10h3z"/><path d="M7 10.5l4-7.2c1.7 0 2.6 1.1 2.3 2.7l-.6 3.5H19a2 2 0 0 1 2 2.3l-1.2 6.9a2 2 0 0 1-2 1.8H7"/>',
    xCircle: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    flag: '<path d="M5.5 21V3.8M5.5 4h11.2l-2.2 4.2 2.2 4.3H5.5"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6L6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/>',
    moon: '<path d="M20 14.6A8.2 8.2 0 1 1 9.4 4a6.6 6.6 0 0 0 10.6 10.6z"/>',
    sunset: '<path d="M3 17.5h18M6.5 14a5.5 5.5 0 0 1 11 0M12 3.5v4M5.4 7.4l1.5 1.5M18.6 7.4l-1.5 1.5M8 21h8"/>',
    wind: '<path d="M3 8.5h10.5a2.8 2.8 0 1 0-2.8-2.8M3 12.5h15.5a2.8 2.8 0 1 1-2.8 2.8M3 16.5h8"/>',
    thermo: '<path d="M14 14.6V5.2a2 2 0 0 0-4 0v9.4a4.1 4.1 0 1 0 4 0z"/><path d="M12 9v7.6"/>',
    home: '<path d="M3.5 11L12 4l8.5 7"/><path d="M5.5 9.6V20h13V9.6"/><path d="M10 20v-5.2h4V20"/>',
    route: '<circle cx="6" cy="18.5" r="2.2"/><circle cx="18" cy="5.5" r="2.2"/><path d="M8.2 18.5h7.6a3.4 3.4 0 0 0 0-6.8H8.2a3.4 3.4 0 0 1 0-6.8h7.6"/>',
    flame: '<path d="M12 21.2c-3.9 0-6.6-2.6-6.6-6.1 0-3 2-5 3.6-6.6.3 1.6 1.2 2.6 2.3 3 0-3.4 1-6.1 3.4-8.3.5 3 4 5.5 4 10.6 0 4.4-2.8 7.4-6.7 7.4z"/>',
    arrow: '<path d="M5 12h13.5M13 6.5l5.5 5.5-5.5 5.5"/>',
    chevron: '<path d="M6.5 9.5l5.5 5.5 5.5-5.5"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    cpu: '<rect x="6" y="6" width="12" height="12" rx="2.2"/><rect x="9.6" y="9.6" width="4.8" height="4.8" rx=".6"/><path d="M9.5 2.5V6M14.5 2.5V6M9.5 18v3.5M14.5 18v3.5M2.5 9.5H6M2.5 14.5H6M18 9.5h3.5M18 14.5h3.5"/>',
    eyeOff: '<path d="M3.5 3.5l17 17"/><path d="M10.6 6.1c.5-.1.9-.1 1.4-.1 5 0 9 6 9 6a16.6 16.6 0 0 1-2.8 3.3M6.6 7.6C4.3 9.2 3 12 3 12s4 6 9 6c1.6 0 3-.4 4.3-1.1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    send: '<path d="M20.5 3.5L10.2 13.8M20.5 3.5l-6.6 17-3.7-6.7-6.7-3.7 17-6.6z"/>',
    check: '<path d="M5 12.6l4.4 4.4L19 7.4"/>',
    book: '<path d="M4.5 5.2A2.2 2.2 0 0 1 6.7 3h12.8v15.6H6.7a2.2 2.2 0 0 0-2.2 2.2V5.2z"/><path d="M4.5 20.8A2.2 2.2 0 0 1 6.7 18.6h12.8"/>',
    sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    users: '<circle cx="9" cy="8.2" r="3.2"/><path d="M2.8 20.2c0-3.4 2.8-6.2 6.2-6.2s6.2 2.8 6.2 6.2"/><circle cx="17.2" cy="9.2" r="2.5"/><path d="M16.4 14.2c2.7.2 4.8 2.5 4.8 5.2"/>',
    child: '<circle cx="12" cy="5.4" r="2.4"/><path d="M7.5 11.2l4.5-2.2 4.5 2.2M12 9v6.4M9.4 21l2.6-5.6 2.6 5.6"/>',
    close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    edit: '<path d="M4 20h4.2L19.4 8.8a2.1 2.1 0 0 0-3-3L5.2 17v3z"/><path d="M14.4 7.8l3 3"/>',
    swap: '<path d="M4 8h13.5M14 4.5L17.5 8 14 11.5M20 16H6.5M10 12.5L6.5 16l3.5 3.5"/>',
    plane: '<path d="M10.5 13.5L3 11l1.5-1.5 7.5 1L16 6c1-1 2.6-1.4 3.4-.6.8.8.4 2.4-.6 3.4l-4.5 4.5 1 7.5L13.8 22l-2.5-7.5-3.8 3.8.3 2.2-1.5 1.5-1.2-3.3-3.3-1.2 1.5-1.5 2.2.3 3.8-3.8z"/>',
    bus: '<rect x="5" y="3.5" width="14" height="14" rx="2.5"/><path d="M5 10.5h14M8 17.5v2.5M16 17.5v2.5"/><circle cx="8.5" cy="14" r=".9"/><circle cx="15.5" cy="14" r=".9"/>',
    car: '<path d="M4 15.5V12l2-5h12l2 5v3.5"/><path d="M3.5 15.5h17v3h-17z"/><circle cx="7.5" cy="18.5" r="1.3"/><circle cx="16.5" cy="18.5" r="1.3"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r=".8"/>',
    spark: '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/>'
  };
  function icon(name, cls) {
    const body = ICONS[name] || ICONS.info;
    return '<svg class="ic' + (cls ? ' ' + cls : '') + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + body + '</svg>';
  }

  /* ------------------------------------------------------------ state */
  const params = new URLSearchParams(window.location.search);
  const state = {
    lang: 'ru',
    origin: null,          // {lat, lon, source: geo|map|landmark|sample, acc?, lm?} — memory only
    cell: null,            // geohash-7 — the only location the server ever gets
    chips: {time: null, kids: false, wheel: false, group: 1, interests: []},
    noAi: false,
    sim: {on: false, dt: '', temp: -5, wind: 15, precip: false},
    mock: params.get('mock') === '1',
    landmarks: BUILTIN_LANDMARKS.slice(),
    busy: false,
    ctrl: null,
    run: 0,
    res: null,
    feedback: {},
    activeIdx: -1,
    sessionId: null,
    traceOpen: false,
    deleteMsgKey: null,
    originMsgKey: null,
    formMsgKey: null,
    collapsed: false
  };

  /* ------------------------------------------------------------ i18n */
  const LOCALES = {ru: 'ru-RU', kk: 'ru-RU', en: 'en-GB'};   // Kazakh uses the same number format as ru (1 234,5)
  const UNITS = {ru: ['м', 'км'], kk: ['м', 'км'], en: ['m', 'km']};

  function t(key, vars) {
    const dict = L[state.lang] || L.ru;
    let s = dict[key];
    if (s === undefined) s = L.ru[key];
    if (s === undefined) return key;
    if (typeof s === 'string' && vars) {
      s = s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] !== undefined && vars[k] !== null ? vars[k] : m));
    }
    return s;
  }
  function pick3(arr) {
    if (!Array.isArray(arr)) return arr;
    const i = {ru: 0, kk: 1, en: 2}[state.lang] || 0;
    return arr[i] != null ? arr[i] : arr[0];
  }
  function fmtNum(n, frac) {
    const f = frac || 0;
    try {
      return new Intl.NumberFormat(LOCALES[state.lang], {minimumFractionDigits: f, maximumFractionDigits: f}).format(n);
    } catch (e) { return Number(n).toFixed(f); }
  }
  function fmtTemp(v) {
    if (v === null || v === undefined || isNaN(v)) return '';
    const r = Math.round(Number(v));
    return (r > 0 ? '+' : r < 0 ? '−' : '') + Math.abs(r) + '°';
  }
  function fmtDist(m) {
    const u = UNITS[state.lang] || UNITS.ru;
    if (m < 1000) return Math.max(10, Math.round(m / 10) * 10) + ' ' + u[0];
    return fmtNum(m / 1000, 1) + ' ' + u[1];
  }
  function fmtDur(ms) {
    const v = Number(ms) || 0;
    if (v < 1000) return Math.round(v) + ' ' + t('ms');
    return fmtNum(v / 1000, 1) + ' ' + t('sec');
  }
  function plural(n, forms) {
    if (!Array.isArray(forms)) return forms;
    if (state.lang !== 'ru') return n === 1 ? forms[0] : forms[1];
    const n10 = n % 10, n100 = n % 100;
    if (n10 === 1 && n100 !== 11) return forms[0];
    if (n10 >= 2 && n10 <= 4 && (n100 < 10 || n100 >= 20)) return forms[1];
    return forms[2];
  }
  function hhmm(s) {
    if (!s) return null;
    const m = /T(\d{2}):(\d{2})/.exec(s) || /^(\d{1,2}):(\d{2})/.exec(String(s).trim());
    return m ? m[1].padStart(2, '0') + ':' + m[2] : null;
  }
  function weekdayIdx(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || '');
    if (!m) return null;
    const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])).getUTCDay();
    return (d + 6) % 7; // Monday = 0
  }
  function fmtDay(s) { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ''); return m ? m[3] + '.' + m[2] : String(s || ''); }
  function fmtMonth(s) { const m = /^(\d{4})-(\d{2})/.exec(s || ''); return m ? m[2] + '.' + m[1] : String(s || ''); }
  function fmtYear(s) { const m = /^(\d{4})/.exec(s || ''); return m ? m[1] : String(s || ''); }
  function srcName(k) {
    if (!k) return '';
    const v = L.SOURCES[String(k).trim().toLowerCase()];
    return v ? pick3(v) : String(k).trim();
  }
  function srcList(s) {
    if (!s) return '';
    const parts = Array.isArray(s) ? s : String(s).split(/[+,;]/);
    return parts.map(srcName).filter(Boolean).join(', ');
  }
  function catLabel(key) { const c = L.CATEGORIES[key]; return c ? pick3(c) : null; }
  function tagLabel(key) { const tg = L.TAGS[key]; return tg ? pick3(tg) : (catLabel(key) || key); }
  function safeUrl(u) { return /^https?:\/\//i.test(String(u || '')) ? String(u) : null; }

  /* ------------------------------------------------------------ geo: geohash + distance */
  const B32 = '0123456789bcdefghjkmnpqrstuvwxyz';
  function geohashEncode(lat, lon, precision) {
    const p = precision || 7;
    const la = [-90, 90], lo = [-180, 180];
    let hash = '', bit = 0, ch = 0, even = true;
    while (hash.length < p) {
      const r = even ? lo : la, v = even ? lon : lat;
      const mid = (r[0] + r[1]) / 2;
      if (v >= mid) { ch = (ch << 1) | 1; r[0] = mid; } else { ch = ch << 1; r[1] = mid; }
      even = !even;
      if (++bit === 5) { hash += B32[ch]; bit = 0; ch = 0; }
    }
    return hash;
  }
  function geohashBounds(hash) {
    const la = [-90, 90], lo = [-180, 180];
    let even = true;
    for (const c of hash) {
      const n = B32.indexOf(c);
      for (let i = 4; i >= 0; i--) {
        const r = even ? lo : la;
        const mid = (r[0] + r[1]) / 2;
        if ((n >> i) & 1) r[0] = mid; else r[1] = mid;
        even = !even;
      }
    }
    return {s: la[0], n: la[1], w: lo[0], e: lo[1]};
  }
  function haversine(a, b) {
    const R = 6371008.8, rad = Math.PI / 180;
    const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
    const x = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(x)));
  }
  function inAstana(lat, lon) {
    return lat >= ASTANA_BBOX.s && lat <= ASTANA_BBOX.n && lon >= ASTANA_BBOX.w && lon <= ASTANA_BBOX.e;
  }
  function localWalk(card) {
    if (!state.origin || !isFinite(card.lat) || !isFinite(card.lon)) {
      return {m: card.distance_m || 0, min: card.walk_min || 0};
    }
    const m = haversine(state.origin, {lat: card.lat, lon: card.lon}) * WALK_FACTOR;
    return {m: m, min: Math.max(1, Math.round(m / WALK_M_PER_MIN))};
  }

  /* ------------------------------------------------------------ storage */
  function initialLang() {
    const saved = LS.get(KEYS.lang);
    if (saved && L[saved]) return saved;
    // Russian by default (the city's lingua franca); Kazakh if the browser asks for it; English via the switch
    const nav = (navigator.language || 'ru').toLowerCase();
    return nav.startsWith('kk') ? 'kk' : 'ru';
  }
  function loadChips() {
    try {
      const raw = LS.get(KEYS.chips);
      if (!raw) return;
      const c = JSON.parse(raw);
      state.chips.time = TIME_OPTIONS.includes(c.time) ? c.time : null;
      state.chips.kids = !!c.kids;
      state.chips.wheel = !!c.wheel;
      state.chips.group = GROUP_OPTIONS.includes(c.group) ? c.group : 1;
      state.chips.interests = Array.isArray(c.interests) ? c.interests.filter((id) => INTERESTS.some((x) => x.id === id)) : [];
      state.noAi = !!c.noAi;
    } catch (e) { /* ignore broken storage */ }
  }
  function saveChips() {
    const c = state.chips;
    LS.set(KEYS.chips, JSON.stringify({time: c.time, kids: c.kids, wheel: c.wheel, group: c.group, interests: c.interests, noAi: state.noAi}));
  }
  function newId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    const b = new Uint8Array(16);
    window.crypto.getRandomValues(b);
    return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  }
  function ensureSession() {
    if (!state.sessionId) state.sessionId = LS.get(KEYS.session) || newId();
    LS.set(KEYS.session, state.sessionId);
    return state.sessionId;
  }

  /* ------------------------------------------------------------ static UI */
  function buildStaticUI() {
    $$('[data-icon]').forEach((el) => {
      el.innerHTML = icon(el.getAttribute('data-icon'));
      el.classList.add('icw');
    });
    $('#time-chips').innerHTML = TIME_OPTIONS.map((m) =>
      '<button type="button" class="chip" data-time="' + m + '" aria-pressed="false"></button>').join('');
    $('#group-seg').innerHTML = GROUP_OPTIONS.map((n) =>
      '<button type="button" class="seg-btn" data-group="' + n + '" aria-pressed="false">' + n + '</button>').join('');
    $('#interest-chips').innerHTML = INTERESTS.map((x) =>
      '<button type="button" class="chip" data-interest="' + x.id + '" aria-pressed="false"></button>').join('');
    // collapsed request summary (shown after "Подобрать")
    const sum = document.createElement('div');
    sum.className = 'req-summary';
    sum.id = 'req-summary';
    sum.hidden = true;
    sum.innerHTML = '<div class="rs-text" id="rs-text"></div>' +
      '<button type="button" class="btn btn-soft" id="btn-edit">' + icon('edit') + '<span id="btn-edit-text"></span></button>';
    $('#req-form').before(sum);
  }

  function applyLang() {
    document.documentElement.lang = state.lang;
    $$('[data-i18n]').forEach((el) => {
      const v = t(el.getAttribute('data-i18n'));
      if (typeof v === 'string') el.textContent = v;
    });
    $$('[data-i18n-attr]').forEach((el) => {
      el.getAttribute('data-i18n-attr').split(';').forEach((pair) => {
        const [attr, key] = pair.split(':');
        if (attr && key) el.setAttribute(attr.trim(), t(key.trim()));
      });
    });
    $$('.lang-btn').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.lang === state.lang)));
    $$('#time-chips .chip').forEach((b) => { b.textContent = b.dataset.time + ' ' + t('min'); });
    $$('#interest-chips .chip').forEach((b) => {
      const def = INTERESTS.find((x) => x.id === b.dataset.interest);
      b.textContent = t(def.key);
    });
    $('#examples').innerHTML = '<span class="ex-label">' + esc(t('examples_label')) + '</span>' +
      t('examples').map((ex) => '<button type="button" class="ex-btn">' + esc(ex) + '</button>').join('');
    $('#btn-edit-text').textContent = t('edit_request');
    $('#mock-lang').hidden = state.lang === 'ru';
    $('#sim-note').textContent = state.mock ? t('sim_note_mock') : t('sim_note');
    placeholderTick(true);
    renderLandmarkOptions();
    renderOrigin();
    syncChipsUI();
    syncSimUI();
    renderAbout();
    renderMsgs();
    setBusy(state.busy);
    renderSummary();
    if (state.res) renderAll(false);
  }

  /* Rotating placeholder with examples in the current language. */
  let phIdx = 0;
  function placeholderTick(reset) {
    const q = $('#q');
    const list = t('placeholders');
    if (reset) phIdx = 0;
    else if (document.activeElement === q || q.value) return;
    else phIdx = (phIdx + 1) % list.length;
    q.placeholder = '«' + list[phIdx] + '»';
  }

  function renderLandmarkOptions() {
    const sel = $('#landmark');
    const cur = state.origin && state.origin.source === 'landmark' ? String(state.origin.lm) : '';
    sel.innerHTML = '<option value="">' + esc(t('landmark_placeholder')) + '</option>' +
      state.landmarks.map((l, i) => '<option value="' + i + '">' + esc(lmName(l)) + '</option>').join('');
    sel.value = cur;
  }
  function lmName(l) {
    const n = l.names || {};
    return n[state.lang] || n.ru || n.en || n.kk || l.id || '?';
  }

  function syncChipsUI() {
    const c = state.chips;
    $$('#time-chips .chip').forEach((b) => b.setAttribute('aria-pressed', String(+b.dataset.time === c.time)));
    $$('#group-seg .seg-btn').forEach((b) => b.setAttribute('aria-pressed', String(+b.dataset.group === c.group)));
    $$('#interest-chips .chip').forEach((b) => b.setAttribute('aria-pressed', String(c.interests.includes(b.dataset.interest))));
    $('#chip-kids').setAttribute('aria-pressed', String(c.kids));
    $('#chip-wheel').setAttribute('aria-pressed', String(c.wheel));
    $('#noai').checked = state.noAi;
  }

  function syncSimUI() {
    const s = state.sim;
    $('#sim-on').checked = s.on;
    $('#sim-dt').value = s.dt;
    $('#sim-temp').value = s.temp;
    $('#sim-wind').value = s.wind;
    $('#sim-precip').checked = s.precip;
    $('#sim-temp-out').textContent = fmtTemp(s.temp);
    $('#sim-wind-out').textContent = s.wind + ' ' + t('kmh');
    $('#sim').classList.toggle('is-on', s.on);
  }

  function renderMsgs() {
    setMsg('#origin-msg', state.originMsgKey);
    setMsg('#form-msg', state.formMsgKey);
    setMsg('#delete-msg', state.deleteMsgKey);
  }
  function setMsg(sel, entry) {
    const el = $(sel);
    if (!entry) { el.textContent = ''; el.className = 'form-msg' + (sel === '#delete-msg' ? ' ok' : ''); return; }
    el.className = 'form-msg ' + (entry.tone || '');
    el.innerHTML = icon(entry.tone === 'ok' ? 'check' : entry.tone === 'warn' ? 'alert' : 'info') + '<span>' + esc(t(entry.key)) +
      (entry.help && entry.help.length ? '<ol class="geo-help">' + entry.help.map((h) => '<li>' + esc(h) + '</li>').join('') + '</ol>' : '') + '</span>';
  }

  /* ------------------------------------------------------------ about dialog */
  function renderAbout() {
    const secs = t('about_sections');
    const rows = t('src_rows');
    $('#about-body').innerHTML = secs.map((s) =>
      '<section class="about-sec"><h3>' + esc(s.h) + '</h3><ul>' + s.items.map((i) => '<li>' + esc(i) + '</li>').join('') + '</ul></section>').join('') +
      '<section class="about-sec"><h3>' + esc(t('about_sources')) + '</h3><div class="src-table" role="table">' +
      rows.map((r) => '<div class="src-row" role="row"><span role="cell" class="src-name">' + esc(r[0]) + '</span><span role="cell" class="src-lic">' + esc(r[1]) + '</span><span role="cell" class="src-use">' + esc(r[2]) + '</span></div>').join('') +
      '</div><p class="hint">' + esc(t('about_map')) + '</p></section>';
  }

  /* ------------------------------------------------------------ origin */
  function setOrigin(o, opts) {
    const options = opts || {};
    state.origin = o;
    state.cell = geohashEncode(o.lat, o.lon, 7);
    state.originMsgKey = null;
    if (state.formMsgKey && state.formMsgKey.key === 'need_origin') state.formMsgKey = null;
    renderMsgs();
    state.where = null;
    renderOrigin();
    fetchWhere();
    updateOriginOnMap(options.fly !== false);
    // distances are recomputed locally from the new exact point
    if (state.res && state.res.cards) renderCards(false);
    renderSummary();
  }

  // «Вы здесь»: 2GIS address of the privacy cell (the exact point never leaves the browser)
  let whereSeq = 0;
  async function fetchWhere() {
    if (state.mock || !state.cell) return;
    const seq = ++whereSeq, cell = state.cell;
    try {
      const r = await fetch('/api/where?cell=' + encodeURIComponent(cell), {headers: {Accept: 'application/json'}});
      const j = r.ok ? await r.json() : null;
      if (seq !== whereSeq || cell !== state.cell) return;
      state.where = j && j.address ? j : null;
      renderOrigin();
    } catch (e) { /* no address — fine */ }
  }

  // «Откуда?»: 2GIS suggestions for a typed place or address
  let sugTimer = null, sugSeq = 0;
  function initFromSearch() {
    const inp = $('#from-q'), list = $('#from-list');
    if (!inp || !list) return;
    const close = () => { list.hidden = true; list.innerHTML = ''; inp.setAttribute('aria-expanded', 'false'); };
    inp.addEventListener('input', () => {
      clearTimeout(sugTimer);
      const q = inp.value.trim();
      if (q.length < 2 || state.mock) { close(); return; }
      sugTimer = setTimeout(async () => {
        const seq = ++sugSeq;
        try {
          const r = await fetch('/api/suggest?q=' + encodeURIComponent(q) + '&cell=' + encodeURIComponent(state.cell || ''), {headers: {Accept: 'application/json'}});
          const j = r.ok ? await r.json() : {items: []};
          if (seq !== sugSeq) return;
          const items = (j && j.items) || [];
          if (!items.length) { list.innerHTML = '<li class="from-empty">' + esc(t('from_none')) + '</li>'; list.hidden = false; return; }
          list.innerHTML = items.map((it, i) => '<li role="option" tabindex="-1" data-i="' + i + '"><b>' + esc(it.name) + '</b><span>' + esc(it.subtitle || '') + '</span></li>').join('') +
            '<li class="from-src" aria-hidden="true">2GIS</li>';
          list.hidden = false;
          inp.setAttribute('aria-expanded', 'true');
          list.querySelectorAll('li[data-i]').forEach((li) => li.addEventListener('mousedown', (ev) => {
            ev.preventDefault();
            const it = items[+li.dataset.i];
            inp.value = it.name;
            close();
            setOrigin({lat: it.lat, lon: it.lon, source: 'search', label: it.name});
          }));
        } catch (e) { close(); }
      }, 250);
    });
    inp.addEventListener('blur', () => setTimeout(close, 150));
    inp.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') close(); });
  }

  function originText() {
    const o = state.origin;
    if (!o) return t('origin_none');
    if (o.source === 'search') return t('origin_search', {name: o.label || ''});
    if (o.source === 'geo') return t('origin_geo', {acc: Math.max(1, Math.round(o.acc || 0))});
    if (o.source === 'landmark') return t('origin_landmark', {name: lmName(state.landmarks[o.lm] || {})});
    if (o.source === 'sample') return t('origin_sample');
    return t('origin_map');
  }
  function originShort() {
    const o = state.origin;
    if (!o) return '';
    if (o.source === 'landmark') return lmName(state.landmarks[o.lm] || {});
    if (o.source === 'search') return o.label || '';
    if (o.source === 'sample') return t('origin_sample');
    if (o.source === 'geo') return t('btn_locate');
    return t('origin_map');
  }

  function renderOrigin() {
    const o = state.origin;
    const st = $('#origin-status');
    const ic = !o ? 'target' : o.source === 'geo' ? 'locate' : o.source === 'landmark' ? 'landmark' : 'pin';
    st.className = 'origin-status' + (o ? ' is-set' : '');
    const w = state.where;
    st.innerHTML = icon(ic) + '<span>' + esc(originText()) +
      (w && w.address ? '<span class="where">' + esc(t('where_prefix')) + ' ' + esc([w.address, w.building].filter(Boolean).join(' · ')) +
        '<small> · 2GIS</small></span>' : '') + '</span>';
    $('#cell-code').innerHTML = state.cell ? ' · ' + esc(t('cell_label')) + ' <code>' + esc(state.cell) + '</code>' : '';
    const sel = $('#landmark');
    sel.value = o && o.source === 'landmark' ? String(o.lm) : '';
    renderMapChip();
  }

  function locate() {
    const btn = $('#btn-locate');
    if (!window.isSecureContext || !navigator.geolocation) {
      state.originMsgKey = {key: 'geo_insecure', tone: 'warn'};
      renderMsgs();
      return;
    }
    state.originMsgKey = {key: 'geo_locating', tone: 'info'};
    renderMsgs();
    btn.setAttribute('aria-busy', 'true');
    btn.classList.add('is-loading');
    // remember the permission state before asking: "denied" here = the site is blocked in the browser;
    // a refusal while the state is "prompt"/"granted" = location is switched off for the browser in the OS
    let permBefore = 'unknown';
    let retried = false;
    const ask = (opts) => navigator.geolocation.getCurrentPosition(onPos, onErr, opts);
    const onPos = (pos) => {
      btn.removeAttribute('aria-busy');
      btn.classList.remove('is-loading');
      const c = pos.coords;
      setOrigin({lat: c.latitude, lon: c.longitude, source: 'geo', acc: c.accuracy});
      if (!inAstana(c.latitude, c.longitude)) {   // never block: the search widens step by step and 2GIS covers the outskirts
        state.originMsgKey = {key: 'geo_outside', tone: 'warn'};
        renderMsgs();
      }
    };
    const onErr = (err) => {
      const code = err && err.code;
      if ((code === 2 || code === 3) && !retried) {   // no fix / too slow: retry once with the fast Wi-Fi based position
        retried = true;
        ask({enableHighAccuracy: false, timeout: 10000, maximumAge: 300000});
        return;
      }
      btn.removeAttribute('aria-busy');
      btn.classList.remove('is-loading');
      if (code === 1) {
        const where = permBefore === 'denied' ? 'site' : 'os';
        state.originMsgKey = {key: where === 'site' ? 'geo_blocked_site' : 'geo_blocked_os', tone: 'warn', help: geoHelp(where)};
      } else {
        state.originMsgKey = {key: code === 3 ? 'geo_timeout' : 'geo_unavailable', tone: 'warn', help: [t('help_type_instead')]};
      }
      renderMsgs();
    };
    (async () => {
      try {
        if (navigator.permissions && navigator.permissions.query) permBefore = (await navigator.permissions.query({name: 'geolocation'})).state;
      } catch (e) { /* Safari < 16 has no permissions API for geolocation */ }
      ask({enableHighAccuracy: true, timeout: 12000, maximumAge: 30000});
    })();
  }

  // step-by-step help for the browser / OS the person actually uses
  function geoHelp(where) {
    const ua = navigator.userAgent || '';
    const ios = /iPhone|iPad|iPod/.test(ua);
    const android = /Android/.test(ua);
    const safari = /^((?!chrome|crios|android|edg).)*safari/i.test(ua);
    const firefox = /Firefox|FxiOS/.test(ua);
    const mac = /Macintosh|Mac OS X/.test(ua) && !ios;
    const win = /Windows/.test(ua);
    const browser = safari ? 'Safari' : firefox ? 'Firefox' : /Edg\//.test(ua) ? 'Edge' : 'Chrome';
    const steps = [];
    if (ios) steps.push(t('help_ios'));
    else if (android) steps.push(t('help_android'));
    else if (where === 'site') steps.push(t(safari ? 'help_safari_site' : 'help_chrome_site', {browser: browser}));
    else {
      if (mac) steps.push(t('help_mac_os', {browser: browser}));
      else if (win) steps.push(t('help_win_os'));
      steps.push(t(safari ? 'help_safari_site' : 'help_chrome_site', {browser: browser}));
    }
    steps.push(t('help_type_instead'));
    return steps;
  }

  async function loadLandmarks() {
    if (state.mock) return; // the demo never touches the network API
    try {
      const r = await fetch('/api/landmarks', {headers: {Accept: 'application/json'}});
      if (!r.ok) return;
      const j = await r.json();
      const arr = Array.isArray(j) ? j : (j.items || j.landmarks || []);
      const list = arr.map(normLandmark).filter(Boolean);
      if (!list.length) return;
      const keep = state.origin && state.origin.source === 'landmark' ? state.landmarks[state.origin.lm] : null;
      state.landmarks = list;
      if (keep) {
        const idx = list.findIndex((l) => l.id === keep.id);
        if (idx >= 0) state.origin.lm = idx; else state.origin.source = 'map';
      }
      renderLandmarkOptions();
      renderOrigin();
    } catch (e) { /* keep the built-in list */ }
  }
  function normLandmark(x) {
    if (!x) return null;
    const lat = Number(x.lat), lon = Number(x.lon !== undefined ? x.lon : x.lng);
    if (!isFinite(lat) || !isFinite(lon)) return null;
    let names;
    if (x.name && typeof x.name === 'object') names = x.name;
    else if (x.names && typeof x.names === 'object') names = x.names;
    else names = {ru: x.name_ru || x.name || x.label, kk: x.name_kk, en: x.name_en};
    return {id: String(x.id || x.slug || names.ru || names.en || lat + ',' + lon), lat: lat, lon: lon, names: names};
  }

  /* ------------------------------------------------------------ map */
  let map = null;
  let originMarker = null;
  let poiMarkers = [];
  const EMPTY_FC = {type: 'FeatureCollection', features: []};

  function initMap() {
    if (!window.maplibregl) { mapUnavailable(); return; }
    try {
      map = new window.maplibregl.Map({
        container: 'map',
        style: darkMQ.matches ? STYLE_DARK : STYLE_LIGHT,
        center: [ASTANA.lon, ASTANA.lat],
        zoom: mobileMQ.matches ? 11.2 : 11.8,
        minZoom: 9,
        maxZoom: 18,
        attributionControl: false,   // attribution is rendered in the page (always visible)
        dragRotate: false,
        pitchWithRotate: false,
        maxBounds: [[70.85, 50.82], [72.05, 51.45]]
      });
    } catch (e) { mapUnavailable(); return; }
    map.addControl(new window.maplibregl.NavigationControl({showCompass: false}), 'top-right');
    map.touchZoomRotate.disableRotation();
    map.on('style.load', () => { ensureLayers(); syncMapData(); });
    map.on('click', onMapClick);
    map.on('error', () => { /* tile hiccups are not user-facing; keep the console clean */ });
    // the dark OpenFreeMap style references a sprite pattern it does not ship; give it a transparent pixel
    map.on('styleimagemissing', (e) => {
      if (e && e.id && !map.hasImage(e.id)) map.addImage(e.id, {width: 1, height: 1, data: new Uint8Array(4)});
    });
    const onScheme = () => { if (map) map.setStyle(darkMQ.matches ? STYLE_DARK : STYLE_LIGHT, {diff: false}); };
    if (darkMQ.addEventListener) darkMQ.addEventListener('change', onScheme);
  }
  function mapUnavailable() {
    $('#mapwrap').classList.add('no-map');
    showMapNote('map_unavailable', 0);
  }

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  function ensureLayers() {
    if (!map) return;
    const route = cssVar('--route', '#0B6BA0');
    const casing = cssVar('--route-casing', '#ffffff');
    const cell = cssVar('--cell', route);
    // draw above every road/building layer but below the labels that come after them
    const layers = (map.getStyle() && map.getStyle().layers) || [];
    let lastGeom = -1;
    layers.forEach((l, k) => { if (l.type !== 'symbol') lastGeom = k; });
    const sym = layers.slice(lastGeom + 1).find((l) => l.type === 'symbol');
    const before = sym ? sym.id : undefined;
    if (!map.getSource('cell')) map.addSource('cell', {type: 'geojson', data: EMPTY_FC});
    if (!map.getSource('route')) map.addSource('route', {type: 'geojson', data: EMPTY_FC});
    const add = (layer) => { if (!map.getLayer(layer.id)) map.addLayer(layer, before); };
    add({id: 'cell-fill', type: 'fill', source: 'cell', paint: {'fill-color': cell, 'fill-opacity': 0.14}});
    add({id: 'cell-line', type: 'line', source: 'cell', paint: {'line-color': cell, 'line-width': 1.6, 'line-dasharray': [2, 1.5]}});
    add({id: 'route-casing', type: 'line', source: 'route', layout: {'line-cap': 'round', 'line-join': 'round'},
      paint: {'line-color': casing, 'line-width': 9, 'line-opacity': 0.92}});
    add({id: 'route-solid', type: 'line', source: 'route', filter: ['!=', ['get', 'approx'], true],
      layout: {'line-cap': 'round', 'line-join': 'round'}, paint: {'line-color': route, 'line-width': 4.5}});
    add({id: 'route-dashed', type: 'line', source: 'route', filter: ['==', ['get', 'approx'], true],
      layout: {'line-cap': 'round', 'line-join': 'round'}, paint: {'line-color': route, 'line-width': 4, 'line-dasharray': [0.8, 1.8]}});
  }
  function setSource(id, data) {
    if (!map) return;
    const src = map.getSource(id);
    if (src && src.setData) src.setData(data);
  }
  function cellFC() {
    if (!state.cell) return EMPTY_FC;
    const b = geohashBounds(state.cell);
    return {type: 'FeatureCollection', features: [{type: 'Feature', properties: {},
      geometry: {type: 'Polygon', coordinates: [[[b.w, b.s], [b.e, b.s], [b.e, b.n], [b.w, b.n], [b.w, b.s]]]}}]};
  }
  function routeFC() {
    const r = state.res;
    if (!r || !r.route || !Array.isArray(r.route.legs)) return EMPTY_FC;
    const byId = {};
    (r.cards || []).forEach((c) => { byId[c.poi_id] = c; });
    const start = state.origin || r.origin;
    const pt = (id) => (byId[id] ? [byId[id].lon, byId[id].lat] : start ? [start.lon, start.lat] : null);
    const feats = r.route.legs.map((leg, i) => {
      const hasGeom = Array.isArray(leg.geometry) && leg.geometry.length >= 2;
      let coords = hasGeom ? leg.geometry.map((p) => [Number(p[0]), Number(p[1])]) : [pt(leg.from_id), pt(leg.to_id)].filter(Boolean);
      // the first leg starts at the exact local point, which never left the device
      if (i === 0 && !byId[leg.from_id] && state.origin) coords = [[state.origin.lon, state.origin.lat]].concat(coords);
      return {type: 'Feature', properties: {approx: !!leg.approx || !hasGeom}, geometry: {type: 'LineString', coordinates: coords}};
    }).filter((f) => f.geometry.coordinates.length >= 2);
    return {type: 'FeatureCollection', features: feats};
  }
  function syncMapData() {
    if (!map || !map.getSource('route')) return;
    setSource('cell', cellFC());
    setSource('route', routeFC());
  }

  function onMapClick(e) {
    const target = e.originalEvent && e.originalEvent.target;
    if (target && target.closest && target.closest('.poi-marker, .origin-marker')) return;
    const lat = e.lngLat.lat, lon = e.lngLat.lng;
    if (!inAstana(lat, lon)) { showMapNote('map_outside', 3500); return; }
    hideMapNote();
    setOrigin({lat: lat, lon: lon, source: 'map'}, {fly: false});
  }

  function updateOriginOnMap(fly) {
    if (!map || !state.origin) {
      if (originMarker && !state.origin) { originMarker.remove(); originMarker = null; }
      syncMapData();
      return;
    }
    const ll = [state.origin.lon, state.origin.lat];
    if (!originMarker) {
      const el = document.createElement('div');
      el.className = 'origin-marker';
      el.setAttribute('role', 'img');
      el.innerHTML = '<span class="om-pulse"></span><span class="om-dot"></span>';
      originMarker = new window.maplibregl.Marker({element: el, draggable: true}).setLngLat(ll).addTo(map);
      originMarker.on('dragend', () => {
        const p = originMarker.getLngLat();
        if (!inAstana(p.lat, p.lng)) {
          showMapNote('map_outside', 3500);
          originMarker.setLngLat([state.origin.lon, state.origin.lat]);
          return;
        }
        setOrigin({lat: p.lat, lon: p.lng, source: 'map'}, {fly: false});
      });
    } else {
      originMarker.setLngLat(ll);
    }
    originMarker.getElement().setAttribute('aria-label', t('start') + ': ' + (originShort() || originText()));
    syncMapData();
    if (fly) map.easeTo({center: ll, zoom: Math.max(map.getZoom(), 14), duration: motionOK() ? 700 : 0});
  }

  function renderMapChip() {
    const chip = $('#map-chip');
    if (!chip) return;
    if (!state.origin) {
      chip.className = 'map-chip hint';
      chip.innerHTML = icon('pin') + '<span>' + esc(t('map_pick_hint')) + '</span>';
    } else {
      chip.className = 'map-chip';
      chip.innerHTML = icon('lock') + '<span>' + esc(t('map_cell_chip')) + ' · <code>' + esc(state.cell) + '</code></span>';
    }
  }

  let noteTimer = null;
  function showMapNote(key, ms) {
    const n = $('#map-note');
    n.innerHTML = icon('info') + '<span>' + esc(t(key)) + '</span>';
    n.hidden = false;
    clearTimeout(noteTimer);
    if (ms) noteTimer = setTimeout(hideMapNote, ms);
  }
  function hideMapNote() { $('#map-note').hidden = true; }

  /* Display order: route stops in route order, then single picks/events, then backups.
     Route stops and picks get numbers; alternatives get a "backup" badge and are not numbered. */
  function cardView() {
    const r = state.res;
    if (!r || !r.cards) return {main: [], alts: [], byIdx: {}};
    if (r._view) return r._view;
    const stops = (r.route && Array.isArray(r.route.stops)) ? r.route.stops : [];
    const all = r.cards.map((c, i) => ({c: c, i: i, alt: c.role === 'alternative', num: null}));
    const main = all.filter((x) => !x.alt);
    const pos = (x) => { const k = stops.indexOf(x.c.poi_id); return k >= 0 ? k : 1000 + x.i; };
    main.sort((a, b) => pos(a) - pos(b));
    main.forEach((x, k) => { x.num = k + 1; });
    const alts = all.filter((x) => x.alt);
    const byIdx = {};
    all.forEach((x) => { byIdx[x.i] = x; });
    r._view = {main: main, alts: alts, byIdx: byIdx};
    return r._view;
  }
  function badgeHTML(v, cls) {
    return v.num ? '<span class="num' + (cls ? ' ' + cls : '') + '" aria-hidden="true">' + v.num + '</span>'
      : '<span class="num num-alt' + (cls ? ' ' + cls : '') + '" aria-hidden="true">' + icon('swap') + '</span>';
  }

  function renderMarkers() {
    poiMarkers.forEach((m) => m.remove());
    poiMarkers = [];
    const r = state.res;
    if (!map || !r || !r.cards) return;
    const view = cardView();
    r.cards.forEach((c, i) => {
      if (!isFinite(c.lat) || !isFinite(c.lon)) { poiMarkers.push(null); return; }
      const v = view.byIdx[i];
      const el = document.createElement('button');
      el.type = 'button';
      el.className = 'poi-marker' + (c.kind === 'event' ? ' is-event' : '') + (v.alt ? ' is-alt' : '') + (i === state.activeIdx ? ' is-active' : '');
      if (v.num) el.textContent = String(v.num); else el.innerHTML = icon('swap');
      el.setAttribute('aria-label', (v.num ? v.num + '. ' : t('alt_title') + ': ') + c.name);
      el.addEventListener('click', (ev) => { ev.stopPropagation(); selectCard(i, {scrollCard: true}); });
      poiMarkers.push(new window.maplibregl.Marker({element: el}).setLngLat([c.lon, c.lat]).addTo(map));
    });
  }

  function fitToResults() {
    const r = state.res;
    if (!map || !r || !r.cards || !r.cards.length) return;
    const pts = r.cards.filter((c) => isFinite(c.lat) && isFinite(c.lon)).map((c) => [c.lon, c.lat]);
    if (state.origin) pts.push([state.origin.lon, state.origin.lat]);
    if (!pts.length) return;
    const b = new window.maplibregl.LngLatBounds(pts[0], pts[0]);
    pts.forEach((p) => b.extend(p));
    const small = mobileMQ.matches;
    map.fitBounds(b, {padding: small ? {top: 60, bottom: 86, left: 34, right: 60} : {top: 80, bottom: 60, left: 70, right: 90},
      maxZoom: 15.5, duration: motionOK() ? 900 : 0});
  }

  function renderMapResults(fit) {
    renderMarkers();
    syncMapData();
    if (fit) fitToResults();
  }

  function selectCard(i, opts) {
    const o = opts || {};
    state.activeIdx = i;
    $$('#cards .card').forEach((el) => el.classList.toggle('is-active', +el.dataset.idx === i));
    poiMarkers.forEach((m, j) => { if (m) m.getElement().classList.toggle('is-active', j === i); });
    const card = state.res && state.res.cards && state.res.cards[i];
    if (o.fly && map && card) {
      map.easeTo({center: [card.lon, card.lat], zoom: Math.max(map.getZoom(), 14.5), duration: motionOK() ? 700 : 0});
    }
    if (o.showMap && mobileMQ.matches) window.scrollTo({top: 0, behavior: motionOK() ? 'smooth' : 'auto'});
    if (o.scrollCard) {
      const el = $('#card-' + i);
      if (el) el.scrollIntoView({block: 'nearest', behavior: motionOK() ? 'smooth' : 'auto'});
    }
  }

  /* ------------------------------------------------------------ request */
  function buildRequest() {
    const c = state.chips;
    const interests = [];
    c.interests.forEach((id) => {
      const def = INTERESTS.find((x) => x.id === id);
      if (def) def.tags.forEach((tg) => { if (!interests.includes(tg)) interests.push(tg); });
    });
    const body = {
      cell: state.cell,
      text: $('#q').value.trim(),
      chips: {time_budget_min: c.time, with_kids: c.kids, wheelchair: c.wheel, group_size: c.group, interests: interests},
      lang: state.lang,
      no_ai: state.noAi,
      session_id: ensureSession()
    };
    if (state.origin && state.origin.source === 'landmark') body.origin_label = lmName(state.landmarks[state.origin.lm] || {});
    if (state.sim.on) {
      body.sim = {
        datetime: state.sim.dt ? state.sim.dt + ':00' : null,
        temp_c: Number(state.sim.temp),
        wind_kmh: Number(state.sim.wind),
        precip: !!state.sim.precip
      };
    }
    return body;
  }

  async function submit(opts) {
    const o = opts || {};
    if (!state.origin) {
      state.formMsgKey = {key: 'need_origin', tone: 'warn'};
      renderMsgs();
      expandForm();
      $('#btn-locate').focus();
      return;
    }
    state.formMsgKey = null;
    state.deleteMsgKey = null;
    renderMsgs();
    const useMock = state.mock || !!o.mock;
    if (state.ctrl) state.ctrl.abort();
    const run = ++state.run;
    const ctrl = new AbortController();
    state.ctrl = ctrl;
    startResults(useMock);
    setBusy(true);
    try {
      if (useMock) await replayMock(run);
      else await streamRecommend(buildRequest(), ctrl.signal, run);
      if (run === state.run) finishIfIncomplete();
    } catch (err) {
      if (run !== state.run || (err && err.name === 'AbortError')) return;
      showError(err);
    } finally {
      if (run === state.run) { setBusy(false); state.ctrl = null; }
    }
  }

  async function streamRecommend(body, signal, run) {
    let res;
    try {
      res = await fetch('/api/recommend', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', Accept: 'text/event-stream'},
        body: JSON.stringify(body),
        signal: signal
      });
    } catch (e) {
      if (e && e.name === 'AbortError') throw e;
      throw Object.assign(new Error('network'), {kind: 'network'});
    }
    if (!res.ok || !res.body) {
      let detail = '';
      try {
        const j = await res.json();
        detail = typeof j.detail === 'string' ? j.detail : (j.message || '');
      } catch (e) { /* not JSON */ }
      throw Object.assign(new Error(detail), {kind: 'http', code: res.status});
    }
    try {
      for await (const ev of readSSE(res.body)) {
        if (run !== state.run) return;
        handleEvent(ev.event, ev.data);
      }
    } catch (e) {
      if (e && e.name === 'AbortError') throw e;
      throw Object.assign(new Error('cut'), {kind: state.res && state.res.cards ? 'cut' : 'network'});
    }
  }

  /* Minimal text/event-stream reader over a fetch() ReadableStream. */
  async function* readSSE(stream) {
    const reader = stream.getReader();
    const dec = new TextDecoder();
    let buf = '';
    try {
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, {stream: true});
        let m;
        while ((m = /\r\n\r\n|\n\n|\r\r/.exec(buf))) {
          const block = buf.slice(0, m.index);
          buf = buf.slice(m.index + m[0].length);
          const ev = parseSSEBlock(block);
          if (ev) yield ev;
        }
      }
      buf += dec.decode();
      if (buf.trim()) {
        const ev = parseSSEBlock(buf);
        if (ev) yield ev;
      }
    } finally {
      try { reader.releaseLock(); } catch (e) { /* already released */ }
    }
  }
  function parseSSEBlock(block) {
    let event = 'message';
    const data = [];
    block.split(/\r\n|\n|\r/).forEach((line) => {
      if (!line || line[0] === ':') return;
      const i = line.indexOf(':');
      const field = i === -1 ? line : line.slice(0, i);
      let val = i === -1 ? '' : line.slice(i + 1);
      if (val[0] === ' ') val = val.slice(1);
      if (field === 'event') event = val;
      else if (field === 'data') data.push(val);
    });
    if (!data.length) return null;
    const raw = data.join('\n');
    let parsed;
    try { parsed = JSON.parse(raw); } catch (e) { parsed = raw; }
    return {event: event, data: parsed};
  }

  /* Replays web/mock/sample_stream.txt through the same event handler. */
  async function replayMock(run) {
    let txt;
    try {
      const r = await fetch(MOCK_FILE, {cache: 'no-cache'});
      if (!r.ok) throw new Error(String(r.status));
      txt = await r.text();
    } catch (e) {
      throw Object.assign(new Error('mock'), {kind: 'mock'});
    }
    const blocks = txt.split(/\r?\n\r?\n/);
    for (const b of blocks) {
      const ev = parseSSEBlock(b);
      if (!ev) continue;
      await sleep(MOCK_DELAY[ev.event] || 300);
      if (run !== state.run) return;
      handleEvent(ev.event, ev.data);
    }
  }

  function handleEvent(type, data) {
    const r = state.res;
    if (!r) return;
    switch (type) {
      case 'context':
        r.context = data; renderContext(); break;
      case 'constraints':
        r.constraints = data; renderConstraints(); break;
      case 'cards':
        r.cards = data && Array.isArray(data.items) ? data.items : [];
        r.route = (data && data.route) || null;
        r.origin = (data && data.origin) || null;
        r.toDest = data && data.to_destination_min != null ? Number(data.to_destination_min) : null;
        r.toDestObj = (data && data.to_destination) || null;
        if (r.origin && r.origin.address && !state.where) { state.where = r.origin; renderOrigin(); }
        r._view = null;
        renderCards(true);
        renderRoute();
        renderMapResults(true);
        setStatus(r.cards.length ? t(r.noAi ? 'status_cards_tpl' : 'status_cards', {n: r.cards.length}) : '');
        break;
      case 'text':
        if (data && data.poi_id) { r.texts[data.poi_id] = data; applyText(data, true); }
        break;
      case 'trace':
        r.trace = data; renderTrace(); break;
      case 'done':
        r.done = data || {};
        r.elapsed = performance.now() - r.t0;
        renderDone();
        break;
      case 'error':
        r.errorView = {msg: (data && data.message) || String(data || ''), offer: !r.cards && !r.mock};
        renderErrorBox();
        if (!r.cards) $('#cards').innerHTML = '';
        break;
      default: break;
    }
  }

  function finishIfIncomplete() {
    const r = state.res;
    if (!r || r.done || r.errorView) return;
    if (!r.cards) { showError({kind: 'cut'}); return; }
    r.elapsed = performance.now() - r.t0;
    renderDone();
  }

  function showError(err) {
    const r = state.res;
    if (!r) return;
    const e = err || {};
    let msg;
    let offer = false;
    if (e.kind === 'network') { msg = t('error_network'); offer = true; }
    else if (e.kind === 'http') { msg = t('error_http', {code: e.code}) + (e.message ? ' ' + e.message : ''); offer = true; }
    else if (e.kind === 'mock') { msg = t('error_mock'); }
    else if (e.kind === 'cut') { msg = t('error_cut'); offer = !r.cards; }
    else { msg = (e.message || '') || t('error_cut'); }
    if (r.mock) offer = false;
    r.errorView = {msg: msg, offer: offer};
    if (!r.cards) $('#cards').innerHTML = '';
    setStatus('');
    renderErrorBox();
  }

  function setBusy(b) {
    state.busy = b;
    const btn = $('#btn-go');
    btn.disabled = b;
    btn.classList.toggle('is-busy', b);
    btn.setAttribute('aria-busy', String(b));
    $('#btn-go-text').textContent = b ? t('submitting') : t('submit');
    $('#results').setAttribute('aria-busy', String(b));
  }
  function setStatus(text) { $('#status').textContent = text || ''; }

  /* ------------------------------------------------------------ collapse / summary */
  function collapseForm() {
    state.collapsed = true;
    $('#req-form').hidden = true;
    $('#req-summary').hidden = false;
    renderSummary();
  }
  function expandForm(focus) {
    state.collapsed = false;
    $('#req-form').hidden = false;
    $('#req-summary').hidden = true;
    if (focus) $('#q').focus();
  }
  function renderSummary() {
    const el = $('#rs-text');
    if (!el) return;
    const c = state.chips;
    const bits = [];
    if (c.time) bits.push(c.time + ' ' + t('min'));
    if (c.kids) bits.push(t('chip_kids').toLowerCase());
    if (c.wheel) bits.push(t('chip_wheelchair').toLowerCase());
    if (c.group > 1) bits.push(c.group + ' ' + icon('users', 'ic-inline'));
    c.interests.forEach((id) => { const d = INTERESTS.find((x) => x.id === id); if (d) bits.push(t(d.key).toLowerCase()); });
    if (state.noAi) bits.push(t('noai_label'));
    if (state.sim.on) bits.push(t('simulated_tag'));
    const q = $('#q').value.trim();
    el.innerHTML =
      (state.origin ? '<span class="rs-origin">' + icon('pin') + esc(originShort()) + '</span>' : '') +
      (q ? '<span class="rs-q">«' + esc(q) + '»</span>' : '') +
      (bits.length ? '<span class="rs-chips">' + bits.map((b) => (b.indexOf('<svg') >= 0 ? b : esc(b))).join(' · ') + '</span>' : '');
  }

  /* ------------------------------------------------------------ results rendering */
  function startResults(isMock) {
    state.res = {mock: isMock, noAi: state.noAi, t0: performance.now(), context: null, constraints: null, cards: null,
      route: null, origin: null, texts: {}, trace: null, done: null, errorView: null, elapsed: null};
    state.feedback = {};
    state.activeIdx = -1;
    $('#mock-banner').hidden = !isMock;
    $('#empty').hidden = true;
    ['#ctx', '#constraints', '#route', '#error', '#trace', '#done'].forEach((s) => { $(s).innerHTML = ''; });
    $('#cards').innerHTML = '<ol class="cards">' + [0, 1, 2].map(skeletonCard).join('') + '</ol>';
    setStatus(t('status_waiting'));
    renderMapResults(false);
    collapseForm();
    // bring the results into view: on phones the map stays on top, results right below it
    const head = $('#results-title');
    head.setAttribute('tabindex', '-1');
    head.focus({preventScroll: true});
    if (mobileMQ.matches) window.scrollTo({top: 0, behavior: motionOK() ? 'smooth' : 'auto'});
    else $('#panel').scrollTo({top: 0, behavior: motionOK() ? 'smooth' : 'auto'});
  }

  function skeletonCard(i) {
    return '<li class="card skeleton" aria-hidden="true" style="--d:' + i + '">' +
      '<div class="sk-row"><span class="sk sk-num"></span><span class="sk sk-title"></span></div>' +
      '<span class="sk sk-line w80"></span><span class="sk sk-line w60"></span><span class="sk sk-line w90"></span></li>';
  }

  function renderAll(animate) {
    const r = state.res;
    if (!r) return;
    $('#mock-banner').hidden = !r.mock;
    $('#empty').hidden = true;
    renderContext();
    renderConstraints();
    renderRoute();
    if (r.cards) renderCards(animate);
    renderTrace();
    renderDone();
    renderErrorBox();
    if (r.cards) renderMarkers();
    if (r.done && r.elapsed != null) setStatus(t('status_done', {s: fmtDur(r.elapsed)}));
  }

  function renderContext() {
    const r = state.res;
    const c = r && r.context;
    const box = $('#ctx');
    if (!c) { box.innerHTML = ''; return; }
    const w = c.weather || {};
    const sim = !!(c.simulated || w.simulated);
    const time = hhmm(c.local_time);
    const wd = weekdayIdx(c.local_time);
    const sunset = hhmm(c.sunset);
    const mode = ['walk', 'short', 'indoor'].includes(c.mode) ? c.mode : 'walk';
    const modeIcon = {walk: 'walk', short: 'route', indoor: 'home'}[mode];
    const items = [];
    if (time) items.push('<span class="ctx-item ctx-time">' + icon('clock') + '<b>' + esc(time) + '</b>' + (wd != null ? '<span>' + esc(t('weekdays')[wd]) + '</span>' : '') + '</span>');
    if (w.temp_c != null) {
      items.push('<span class="ctx-item">' + icon('thermo') + '<b>' + fmtTemp(w.temp_c) + '</b>' +
        (w.feels_c != null ? '<span>' + esc(t('feels', {t: fmtTemp(w.feels_c)})) + '</span>' : '') + '</span>');
    }
    if (w.wind_kmh != null) items.push('<span class="ctx-item">' + icon('wind') + '<span>' + esc(t('wind', {v: Math.round(w.wind_kmh)})) + '</span></span>');
    if (w.precip_prob != null && (w.precip_prob > 1 ? w.precip_prob : w.precip_prob * 100) >= 30) {
      const p = Math.round(w.precip_prob > 1 ? w.precip_prob : w.precip_prob * 100);
      items.push('<span class="ctx-item">' + icon('alert') + '<span>' + esc(t('precip', {p: p})) + '</span></span>');
    }
    if (c.is_dark) items.push('<span class="ctx-item">' + icon('moon') + '<span>' + esc(t('dark')) + '</span></span>');
    else if (sunset) {
      const soon = c.minutes_to_sunset != null && c.minutes_to_sunset >= 0 && c.minutes_to_sunset <= 180;
      items.push('<span class="ctx-item">' + icon('sunset') + '<span>' + esc(t('sunset_at', {t: sunset})) +
        (soon ? ' · ' + esc(t('sunset_in', {m: c.minutes_to_sunset})) : '') + '</span></span>');
    }
    if (c.is_holiday) items.push('<span class="ctx-item ctx-holiday">' + icon('calendar') + '<span>' + esc(c.holiday_name || t('holiday')) + '</span></span>');
    box.innerHTML =
      '<section class="ctx' + (sim ? ' is-sim' : '') + '" aria-label="' + esc(t('weather_src')) + '">' +
        (sim ? '<span class="tag tag-sim">' + icon('sliders') + esc(t('simulated_tag')) + '</span>' : '') +
        '<div class="ctx-row">' + items.join('') + '</div>' +
        '<div class="mode mode-' + mode + '"><span class="mode-chip">' + icon(modeIcon) + esc(t('mode_' + mode)) + '</span>' +
          (c.mode_reason ? '<p class="mode-reason">' + esc(c.mode_reason) + '</p>' : '') + '</div>' +
        '<p class="ctx-src">' + esc(t('weather_src')) + (w.desc ? ' · ' + esc(w.desc) : '') + '</p>' +
      '</section>';
  }

  function renderConstraints() {
    const r = state.res;
    const c = r && r.constraints;
    const box = $('#constraints');
    if (!c) { box.innerHTML = ''; return; }
    const bits = [];
    if (c.time_budget_min) bits.push(c.time_budget_min + ' ' + t('min'));
    if (c.adults) bits.push(t('c_adults', {n: c.adults}));
    if (c.kids) bits.push(t('c_kids', {n: c.kids}));
    if (c.mobility) bits.push(t('mob_' + c.mobility));
    if (c.reserve_min) bits.push(t('c_reserve', {n: c.reserve_min}));
    if (c.must_return) bits.push(t('c_return'));
    if (c.destination) bits.push(t('c_dest_' + c.destination));
    if (c.night) bits.push(t('c_night'));
    const wants = (c.wants || []).map(tagLabel);
    const avoid = (c.avoid || []).map(tagLabel);
    box.innerHTML = '<div class="understood"><span class="u-label">' + esc(t('understood')) + ':</span> ' +
      bits.map((b) => '<span class="u-chip">' + esc(b) + '</span>').join('') +
      (wants.length ? '<span class="u-chip u-want">' + esc(t('c_wants')) + ': ' + esc(wants.join(', ')) + '</span>' : '') +
      (avoid.length ? '<span class="u-chip u-avoid">' + esc(t('c_avoid')) + ': ' + esc(avoid.join(', ')) + '</span>' : '') +
      '<span class="u-by">' + (c.parsed_by === 'llm' ? icon('sparkle') + esc(t('parsed_llm')) : icon('cpu') + esc(t('parsed_rules'))) + '</span>' +
      '</div>';
  }

  function renderRoute() {
    const r = state.res;
    const box = $('#route');
    const route = r && r.route;
    const destKey = r && r.constraints && r.constraints.destination ? 'dest_' + r.constraints.destination : 'dest_other';
    const td = r && r.toDestObj;
    const destLine = td && (td.taxi || td.transit)
      ? '<div class="dest-block"><p class="dest-line">' + icon(destKey === 'dest_airport' ? 'plane' : 'arrow') + '<span>' +
          esc(t('dest_then', {name: td.name || ''})) + (td.from ? ' · ' + esc(t('dest_from', {name: td.from})) : '') + '</span></p>' + movesHTML(td) + '</div>'
      : r && r.toDest != null && isFinite(r.toDest)
        ? '<p class="dest-line">' + icon(destKey === 'dest_airport' ? 'plane' : 'arrow') + '<span>' + esc(t(destKey, {n: Math.round(r.toDest)})) + '</span></p>' : '';
    if (!route || !Array.isArray(route.stops) || !route.stops.length) {
      box.innerHTML = destLine ? '<section class="route">' + destLine + '</section>' : '';
      return;
    }
    const view = cardView();
    const idxById = {};
    (r.cards || []).forEach((c, i) => { idxById[c.poi_id] = i; });
    const tl = {};
    (route.timeline || []).forEach((x) => { if (x && x.poi_id) tl[x.poi_id] = x; });
    const legs = route.legs || [];
    const startT = hhmm(r.context && r.context.local_time);
    const parts = [];
    parts.push('<li class="tl-step tl-start"><span class="tl-dot" aria-hidden="true"></span>' +
      (startT ? '<time>' + esc(startT) + '</time>' : '') + '<span class="tl-name">' + esc(t('start')) + '</span></li>');
    let outdoor = 0;
    route.stops.forEach((id, k) => {
      const leg = legs[k];
      if (leg) outdoor += Number(leg.outdoor_min) || 0;
      parts.push('<li class="tl-leg' + (leg && leg.approx ? ' is-approx' : '') + '" aria-label="' + esc(leg ? t('leg_walk', {n: leg.minutes}) : '') + '">' +
        icon('arrow') + (leg ? '<span>' + esc(leg.minutes) + ' ' + esc(t('min')) + '</span>' : '') + '</li>');
      const i = idxById[id];
      const card = i != null ? r.cards[i] : null;
      const x = tl[id] || {};
      const arrive = hhmm(x.arrive), leave = hhmm(x.leave);
      parts.push('<li class="tl-step"><button type="button" class="tl-btn" data-idx="' + (i != null ? i : '') + '">' +
        (arrive ? '<time>' + esc(arrive) + '</time>' : '') +
        (i != null ? badgeHTML(view.byIdx[i], 'num-sm') : '<span class="num num-sm" aria-hidden="true">•</span>') +
        '<span class="tl-name">' + esc(card ? card.name : id) + '</span>' +
        (leave ? '<span class="tl-until">' + esc(t('until', {t: leave})) + '</span>' : '') +
        '</button></li>');
    });
    const last = route.timeline && route.timeline.length ? hhmm(route.timeline[route.timeline.length - 1].leave) : null;
    if (last) parts.push('<li class="tl-leg tl-leg-end" aria-hidden="true">' + icon('arrow') + '</li><li class="tl-step tl-end"><time>' + esc(last) + '</time><span class="tl-name">' + esc(t('finish')) + '</span></li>');
    const anyApprox = legs.some((l) => l && l.approx);
    box.innerHTML = '<section class="route" aria-labelledby="route-h">' +
      '<div class="route-head"><h3 id="route-h">' + icon('route') + esc(t('route_title', {n: route.total_min})) + '</h3>' +
      (outdoor ? '<span class="muted">' + esc(t('outdoor_total', {n: outdoor})) + '</span>' : '') +
      (route.warm_stops ? '<span class="tag tag-warm">' + icon('flame') + esc(t('warm_stops')) + '</span>' : '') + '</div>' +
      '<ol class="timeline">' + parts.join('') + '</ol>' +
      destLine +
      (anyApprox ? '<p class="hint route-hint"><span class="dash-sample" aria-hidden="true"></span>' + esc(t('approx_leg')) + '</p>' : '') +
      '</section>';
  }

  function hoursHTML(h0) {
    const h = h0 || {status: 'unknown', level: 'unknown', label: ''};
    let level = h.level || 'unknown';
    if (h.status === 'open_area') level = 'open_area';
    if (!['high', 'medium', 'low', 'unknown', 'open_area'].includes(level)) level = 'unknown';
    const ic = {high: 'shieldCheck', medium: 'info', low: 'alert', unknown: 'help', open_area: 'tree'}[level] || 'help';
    const label = String(h.label || '').trim();
    let main = '', note = '';
    if (level === 'open_area') {
      main = label || t('hours_open_area');
    } else if (level === 'unknown') {
      main = label || t('hours_unknown');
    } else {
      const cut = label.indexOf(' · ');
      if (cut > 0) {            // the server already wrote "… · по данным X (MM.YYYY) — уточните"
        main = label.slice(0, cut);
        note = label.slice(cut + 3);
      } else {
        main = label || t('hours_unknown');
        const src = srcList(h.src);
        if (level === 'high') note = h.as_of ? t('hours_checked', {d: fmtDay(h.as_of)}) : '';
        else if (level === 'medium') note = src ? (h.as_of ? t('hours_medium', {src: src, d: fmtMonth(h.as_of)}) : t('hours_medium_nodate', {src: src})) : t('hours_low_nodate');
        else if (h.status !== 'unknown') note = h.as_of ? t('hours_low', {d: fmtYear(h.as_of)}) : t('hours_low_nodate');
      }
    }
    let status = '';
    if (h.status === 'closes_soon') status = '<span class="tag tag-soon">' + icon('clock') + esc(t('closes_soon')) + '</span>';
    else if (h.status === 'closed') status = '<span class="tag tag-closed">' + icon('xCircle') + esc(t('closed_now')) + '</span>';
    const lvl = ['high', 'medium', 'low'].includes(level) ? '<span class="lvl-chip">' + esc(t('lvl_' + level)) + '</span>' : '';
    return '<div class="hours lvl-' + level + '">' + '<span class="hours-ic">' + icon(ic) + '</span>' +
      '<p><span class="hours-main">' + esc(main) + '</span>' + (note ? '<span class="hours-sep"> · </span><span class="hours-note">' + esc(note) + '</span>' : '') +
      ' ' + lvl + status + '</p></div>';
  }

  // taxi + public transport options from 2GIS (for far places and the way to the airport / station)
  function movesHTML(m) {
    if (!m || (!m.taxi && !m.transit)) return '';
    const rows = [];
    if (m.taxi) {
      rows.push('<li>' + icon('car') + '<span>' + esc(t('taxi_line', {n: m.taxi.minutes, km: fmtNum(m.taxi.km || 0, 1)})) + '</span></li>');
    }
    if (m.transit) {
      const lines = (m.transit.lines || []).map((l) => (l.kind || '') + ' ' + (l.names || []).join(', ')).join(' → ');
      rows.push('<li>' + icon('bus') + '<span>' + esc(t('transit_line', {lines: lines || t('transit_generic'), n: m.transit.minutes})) +
        (m.transit.transfers ? ' · ' + esc(t('transfers', {n: m.transit.transfers})) : '') +
        (m.transit.walk ? ' · ' + esc(m.transit.walk) : '') + '</span></li>');
    }
    const link = (m.taxi && m.taxi.link) || (m.transit && m.transit.link);
    return '<ul class="moves">' + rows.join('') + '</ul>' +
      (link ? '<p class="moves-link"><a href="' + esc(link) + '" target="_blank" rel="noopener">' + icon('external') + esc(t('open_2gis')) + '</a></p>' : '');
  }

  function accessHTML(card) {
    const a = card.accessibility;
    const wants = state.chips.wheel || (state.res && state.res.constraints && ['wheelchair', 'stroller'].includes(state.res.constraints.mobility));
    if (!a || a.value === null || a.value === undefined || a.value === '') {
      return wants ? '<p class="access access-unknown">' + icon('wheelchair') + '<span>' + esc(t('access_unknown')) + '</span></p>' : '';
    }
    const v = String(a.value).toLowerCase();
    const key = (v === 'yes' || v === 'true' || v === 'designated') ? 'access_yes' : v === 'limited' ? 'access_limited' : (v === 'no' || v === 'false') ? 'access_no' : 'access_unknown';
    return '<p class="access ' + key.replace('_', '-') + '">' + icon('wheelchair') + '<span>' + esc(t(key)) +
      (a.src ? '<span class="muted"> · ' + esc(t('per_src', {src: srcList(a.src)})) + '</span>' : '') + '</span></p>';
  }

  function fmtEventTime(start, end) {
    const s = hhmm(start), e = hhmm(end);
    const d = /^\d{4}-\d{2}-\d{2}/.test(start || '') ? fmtDay(start) : '';
    return [d, s ? s + (e ? '–' + e : '') : ''].filter(Boolean).join(', ');
  }
  function fmtPrice(p) {
    const s = String(p).trim();
    if (/^(0|free|бесплатно|тегін)$/i.test(s)) return t('free');
    if (/^\d+([.,]\d+)?$/.test(s)) return fmtNum(Number(s.replace(',', '.'))) + ' ₸';
    return /₸|тг|tg|kzt/i.test(s) ? s : s + ' ₸';
  }
  function eventHTML(card) {
    if (card.kind !== 'event' && !card.event) return '';
    const e = card.event || {};
    const title = e.title && e.title !== card.name ? e.title : '';
    const when = fmtEventTime(e.start, e.end);
    const rawPrice = e.price_kzt != null ? e.price_kzt : e.price;
    const price = rawPrice !== undefined && rawPrice !== null && rawPrice !== '' ? fmtPrice(rawPrice) : '';
    const venue = e.venue_name || e.venue || '';
    const url = safeUrl(e.url || card.url);
    return '<div class="event">' + icon('calendar') + '<div class="event-body">' +
      (title ? '<p class="ev-title">' + esc(title) + '</p>' : '') +
      (when || venue ? '<p>' + esc([when, venue].filter(Boolean).join(' · ')) + '</p>' : '') +
      (price ? '<p>' + esc(t('price')) + ': ' + esc(price) + '</p>' : '') +
      (url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + esc(t('event_more')) + icon('external') + '</a>' : '') +
      '</div></div>';
  }

  function scoreHTML(card) {
    const parts = card.score_parts || {};
    const keys = Object.keys(parts);
    if (!card.score && !keys.length) return '';
    const names = t('score_parts') || {};
    const total = keys.reduce((s, k) => s + Math.max(0, Number(parts[k]) || 0), 0) || 1;
    return '<details class="score"><summary>' + esc(t('score')) + ' ' + esc(fmtNum(card.score || 0, 2)) + '</summary>' +
      (keys.length ? '<div class="score-bar" aria-hidden="true">' + keys.map((k, i) =>
        '<i class="sb' + (i % 4) + '" style="width:' + (Math.max(0, Number(parts[k]) || 0) / total * 100).toFixed(1) + '%"></i>').join('') + '</div>' +
        '<ul>' + keys.map((k, i) => '<li><span class="sw sb' + (i % 4) + '" aria-hidden="true"></span>' + esc(names[k] || k) + ' ' + esc(fmtNum(Number(parts[k]) || 0, 2)) + '</li>').join('') + '</ul>' : '') +
      '</details>';
  }

  function fbHTML(card) {
    const f = state.feedback[card.poi_id];
    const locked = f && (f.st === 'ok' || f.st === 'mock' || f.st === 'sending');
    const btns = FEEDBACK.map((x) =>
      '<button type="button" class="fb-btn" data-fb="' + x.kind + '"' + (locked ? ' disabled' : '') +
      ' aria-pressed="' + String(!!(f && f.kind === x.kind)) + '">' + icon(x.icon) + '<span>' + esc(t(x.key)) + '</span></button>').join('');
    let msg = '';
    if (f && f.st === 'ok') msg = t('fb_thanks');
    else if (f && f.st === 'mock') msg = t('fb_thanks_mock');
    else if (f && f.st === 'error') msg = t('fb_error');
    return '<div class="fb" role="group" aria-label="' + esc(t('fb_label')) + '">' + btns + '</div>' +
      '<p class="fb-msg' + (f && f.st === 'error' ? ' warn' : '') + '" role="status">' + (msg ? icon(f.st === 'error' ? 'alert' : 'check') + esc(msg) : '') + '</p>';
  }

  function expPending() {
    return '<div class="exp-pending"><span class="sk sk-line w90"></span><span class="sk sk-line w70"></span>' +
      '<span class="writing">' + icon('sparkle') + esc(t('writing')) + '</span></div>';
  }
  function expMissing() {
    return '<p class="exp-missing">' + icon('info') + esc(t('no_text')) + '</p>';
  }
  function wordsHTML(text, animate, offset) {
    if (!animate || !motionOK()) return esc(text);
    let n = offset || 0;
    return String(text).split(/(\s+)/).map((w) => (/^\s+$/.test(w) || !w ? w : '<span class="w" style="--i:' + (n++) + '">' + esc(w) + '</span>')).join('');
  }
  function expHTML(exp, animate) {
    const idea = exp.source === 'ai_idea';
    const tpl = !!exp.template;
    const why = String(exp.why || '');
    const whyWords = why.split(/\s+/).filter(Boolean).length;
    const src = idea
      ? '<span class="exp-src idea" title="' + esc(t('exp_ai_idea_title')) + '">' + icon('sparkle') + esc(t('exp_ai_idea')) + '</span>'
      : '<span class="exp-src poi" title="' + esc(t('exp_poi_title')) + '">' + icon('database') + esc(t('exp_poi')) + '</span>';
    const badge = tpl
      ? '<span class="ai-badge tpl"><b>' + icon('cpu') + esc(t('tpl_badge')) + '</b><span>' + esc(t('tpl_badge_full')) + '</span></span>'
      : '<span class="ai-badge"><b>' + icon('sparkle') + esc(t('ai_badge')) + '</b><span>' + esc(t('ai_badge_full')) + '</span></span>';
    return '<div class="exp-head">' + src + badge + '</div>' +
      (why ? '<p class="exp-why"><span class="exp-k">' + esc(t('why')) + '.</span> ' + wordsHTML(why, animate, 0) + '</p>' : '') +
      (exp.what_to_do ? '<p class="exp-do"><span class="exp-k">' + esc(t('what_to_do')) + '.</span> ' + wordsHTML(exp.what_to_do, animate, whyWords + 2) + '</p>' : '');
  }

  function cardHTML(card, i, animate) {
    const r = state.res;
    const v = cardView().byIdx[i] || {num: i + 1, alt: false};
    const walk = localWalk(card);
    const exp = r.texts[card.poi_id];
    // local label keeps the chrome in the current UI language after a language switch; events keep the server label
    const catText = (card.kind !== 'event' && catLabel(card.category)) || card.category_label || '';
    const tags = [];
    (card.tags || []).forEach((x) => {
      const lbl = x === 'event' ? '' : tagLabel(x);
      if (lbl && lbl.toLowerCase() !== catText.toLowerCase() && !tags.includes(lbl) && tags.length < 3) tags.push(lbl);
    });
    const statusWarn = card.status === 'possibly_closed' ? '<p class="status-warn">' + icon('alert') + esc(t('status_possibly_closed')) + '</p>'
      : card.status === 'unconfirmed' ? '<p class="status-warn soft">' + icon('help') + esc(t('status_unconfirmed')) + '</p>' : '';
    const sources = card.source_label || (card.sources || []).map(srcName).join(', ');
    let expBody;
    if (exp) expBody = expHTML(exp, animate);
    else if (r.done || r.errorView) expBody = expMissing();
    else expBody = expPending();
    return '<li class="card' + (animate ? ' card-in' : '') + (i === state.activeIdx ? ' is-active' : '') + (card.kind === 'event' ? ' is-event' : '') +
      (v.alt ? ' is-alt' : '') + '" id="card-' + i + '" data-idx="' + i + '" data-poi="' + esc(card.poi_id) + '" style="--d:' + (v.num || i) + '">' +
      '<article aria-labelledby="card-' + i + '-t">' +
      '<header class="card-head">' + badgeHTML(v) +
        '<div class="card-tw"><h3 class="card-title" id="card-' + i + '-t">' + esc(card.name) + '</h3>' +
        '<p class="card-cat">' + (v.alt ? '<span class="tag tag-alt">' + icon('swap') + esc(t('alt_title')) + '</span>' : '') +
        (card.kind === 'event' ? '<span class="tag tag-event">' + icon('calendar') + esc(t('event')) + '</span>' : '') +
        '<span class="card-cat-text">' + esc([catText].concat(tags).filter(Boolean).join(' · ')) + '</span></p></div>' +
        '<button type="button" class="icon-btn card-map" data-act="map" aria-label="' + esc(t('show_on_map') + ': ' + card.name) + '" title="' + esc(t('show_on_map')) + '">' + icon('pin') + '</button>' +
        '</header>' +
      '<p class="facts"><span class="fact" title="' + esc(t('card_walk_title')) + '">' + icon('walk') +
        esc(t('card_walk', {min: walk.min, dist: fmtDist(walk.m)})) + '</span>' +
        (card.visit_min ? '<span class="fact">' + icon('clock') + esc(t('visit', {n: card.visit_min})) + '</span>' : '') +
        (card.rating ? '<span class="fact fact-rating" title="2GIS">★ ' + esc(fmtNum(card.rating, 1)) + ' · ' +
          esc(fmtNum(card.reviews || 0, 0)) + ' ' + esc({ru: 'отзывов в 2GIS', kk: '2GIS пікірі', en: 'reviews on 2GIS'}[state.lang] || 'отзывов в 2GIS') + '</span>' : '') + '</p>' +
      (card.kind === 'event' ? '' : hoursHTML(card.hours)) + statusWarn + accessHTML(card) + eventHTML(card) +
      ((card.taxi || card.transit) ? '<div class="card-moves"><p class="moves-title">' + esc(t('far_how')) + '</p>' + movesHTML(card) + '</div>' : '') +
      (card.description ? '<p class="desc">' + esc(card.description) + (card.description_ai ? ' <span class="tag tag-ai-sm">' + icon('sparkle') + esc(t('desc_ai')) + '</span>' : '') + '</p>' : '') +
      '<div class="exp' + (exp && animate ? ' exp-in' : '') + '" id="exp-' + i + '" aria-live="polite">' + expBody + '</div>' +
      '<footer class="card-foot">' +
        '<p class="src">' + icon('database') + '<span>' + esc(t('sources', {list: sources})) + '</span></p>' +
        '<div class="card-actions">' + fbHTML(card) + '</div>' + scoreHTML(card) +
      '</footer></article></li>';
  }

  function renderCards(animate) {
    const r = state.res;
    const list = $('#cards');
    if (!r || !r.cards) return;
    if (!r.cards.length) {
      list.innerHTML = '<ol class="cards"><li class="card nothing">' + icon('info') + '<p>' + esc(t('nothing_found')) + '</p></li></ol>';
      return;
    }
    const view = cardView();
    list.innerHTML = '<ol class="cards">' + view.main.map((x) => cardHTML(x.c, x.i, animate)).join('') + '</ol>' +
      (view.alts.length ? '<section class="alts" aria-labelledby="alts-h"><h3 class="alt-title" id="alts-h">' + icon('swap') +
        esc(t(view.alts.length > 1 ? 'alt_title_many' : 'alt_title')) + '</h3><p class="hint">' + esc(t('alt_hint')) + '</p>' +
        '<ul class="cards cards-alt">' + view.alts.map((x) => cardHTML(x.c, x.i, animate)).join('') + '</ul></section>' : '');
  }

  function applyText(exp, animate) {
    const r = state.res;
    const i = (r.cards || []).findIndex((c) => c.poi_id === exp.poi_id);
    if (i < 0) return; // arrives before cards: kept in r.texts, applied when cards render
    const box = $('#exp-' + i);
    if (!box) return;
    box.innerHTML = expHTML(exp, animate);
    if (animate) { box.classList.remove('exp-in'); void box.offsetWidth; box.classList.add('exp-in'); }
  }

  function renderTrace() {
    const r = state.res;
    const tr = r && r.trace;
    const box = $('#trace');
    if (!tr) { box.innerHTML = ''; return; }
    const steps = Array.isArray(tr.steps) ? tr.steps : [];
    const totalStep = steps.find((x) => x.service === 'orchestrator');
    const parts = steps.filter((x) => x !== totalStep);
    const total = totalStep ? Number(totalStep.ms) || 0 : parts.reduce((s, x) => s + (Number(x.ms) || 0), 0);
    const max = Math.max(1, ...parts.map((s) => Number(s.ms) || 0));
    const mode = String(tr.mode || 'template');
    const modeTag = mode === 'ai' ? ['tag-ai', t('mode_ai')] : mode.indexOf('ai') === 0 ? ['tag-ai', t('mode_mixed')] : ['tag-tpl', t('mode_template')];
    const li = (arr) => (arr && arr.length ? arr.map((x) => '<li>' + esc(x) + '</li>').join('') : '<li>—</li>');
    box.innerHTML = '<details class="trace"' + (state.traceOpen ? ' open' : '') + '>' +
      '<summary>' + icon('cpu') + '<span class="tr-title">' + esc(t('trace_title')) + '</span>' +
      '<span class="muted">' + esc(t('trace_meta', {n: parts.length + ' ' + plural(parts.length, t('step_forms')), s: fmtDur(total)})) + '</span>' +
      '<span class="sum-chev">' + icon('chevron') + '</span></summary>' +
      '<div class="trace-body"><ol class="steps">' + steps.map((s) =>
        '<li><div class="st-head"><b>' + esc(s.name) + '</b><code>' + esc(s.service) + '</code><span class="st-ms">' + esc(fmtDur(s.ms)) + '</span></div>' +
        '<div class="st-bar" aria-hidden="true"><i style="width:' + (s === totalStep ? 100 : Math.max(2, (Number(s.ms) || 0) / max * 100)).toFixed(1) + '%"></i></div>' +
        (s.detail ? '<p>' + esc(s.detail) + '</p>' : '') + '</li>').join('') + '</ol>' +
      '<div class="llm-io"><div class="io sent"><h4>' + icon('send') + esc(t('sent_llm')) + '</h4><ul>' + li(tr.sent_to_llm) + '</ul></div>' +
      '<div class="io kept"><h4>' + icon('eyeOff') + esc(t('not_sent')) + '</h4><ul>' + li(tr.not_sent) + '</ul></div></div>' +
      '<p class="model-line">' + icon('cpu') + esc(t('model')) + ': <b>' + esc(tr.model || '—') + '</b> · ' + esc(t('mode_label')) + ': ' +
      '<span class="tag ' + modeTag[0] + '">' + esc(modeTag[1]) + '</span></p>' +
      '</div></details>';
    $('details', box).addEventListener('toggle', (e) => { state.traceOpen = e.target.open; });
  }

  function renderDone() {
    const r = state.res;
    const box = $('#done');
    if (!r || !r.done) { box.innerHTML = ''; return; }
    (r.cards || []).forEach((c, i) => {
      if (!r.texts[c.poi_id]) { const b = $('#exp-' + i); if (b) b.innerHTML = expMissing(); }
    });
    const d = r.done;
    let key = d.ai_generated ? 'done_ai' : 'done_tpl';
    if (r.noAi) key = 'done_noai';
    else if (d.fallback) key = d.ai_generated ? 'done_partial' : 'done_fallback';
    const warn = key === 'done_fallback' || key === 'done_partial';
    box.innerHTML = '<p class="done-line' + (warn ? ' warn' : '') + '">' + icon(warn ? 'alert' : d.ai_generated ? 'sparkle' : 'check') +
      '<span>' + esc(t(key)) + '</span>' +
      (d.request_id ? '<span class="muted"> · ' + esc(t('request_id')) + ' <code>' + esc(d.request_id) + '</code></span>' : '') + '</p>';
    if (r.elapsed != null) setStatus(t('status_done', {s: fmtDur(r.elapsed)}));
  }

  function renderErrorBox() {
    const r = state.res;
    const box = $('#error');
    if (!r || !r.errorView) { box.innerHTML = ''; return; }
    const v = r.errorView;
    box.innerHTML = '<div class="alert" role="alert">' + icon('alert') + '<div>' +
      '<p class="alert-title">' + esc(t('error_title')) + '</p><p>' + esc(v.msg) + '</p>' +
      '<div class="alert-actions">' + (v.offer ? '<button type="button" class="btn btn-soft" data-act="example">' + icon('sparkle') + '<span>' + esc(t('show_example')) + '</span></button>' : '') +
      '<button type="button" class="btn btn-ghost" data-act="retry">' + esc(t('retry')) + '</button></div></div></div>';
  }

  /* ------------------------------------------------------------ feedback */
  async function sendFeedback(poiId, kind) {
    const r = state.res;
    if (!r) return;
    state.feedback[poiId] = {kind: kind, st: 'sending'};
    refreshFeedback(poiId);
    if (r.mock) {
      state.feedback[poiId].st = 'mock';
    } else {
      try {
        const res = await fetch('/api/feedback', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({poi_id: poiId, kind: kind, cell: state.cell})
        });
        let ok = res.ok;
        try { const j = await res.json(); if (j && j.ok === false) ok = false; } catch (e) { /* empty body */ }
        state.feedback[poiId].st = ok ? 'ok' : 'error';
      } catch (e) {
        state.feedback[poiId].st = 'error';
      }
    }
    refreshFeedback(poiId);
  }
  function refreshFeedback(poiId) {
    const r = state.res;
    const i = (r.cards || []).findIndex((c) => c.poi_id === poiId);
    const li = $('#card-' + i);
    if (!li) return;
    $('.card-actions', li).innerHTML = fbHTML(r.cards[i]);
  }

  /* ------------------------------------------------------------ delete my data */
  async function deleteMyData() {
    const sid = state.sessionId || LS.get(KEYS.session);
    Object.keys(KEYS).forEach((k) => LS.del(KEYS[k]));
    state.sessionId = null;
    if (state.ctrl) state.ctrl.abort();
    state.run++;
    let key = 'deleted_ok';
    if (state.mock) key = 'deleted_mock';
    else if (sid) {
      try {
        const r = await fetch('/api/session/' + encodeURIComponent(sid), {method: 'DELETE'});
        if (!r.ok && r.status !== 404) key = 'deleted_local';
      } catch (e) { key = 'deleted_local'; }
    }
    // forget everything personal held in memory as well
    state.origin = null;
    state.cell = null;
    state.chips = {time: null, kids: false, wheel: false, group: 1, interests: []};
    state.noAi = false;
    state.sim.on = false;
    state.res = null;
    state.feedback = {};
    $('#q').value = '';
    ['#ctx', '#constraints', '#route', '#cards', '#error', '#trace', '#done'].forEach((s) => { $(s).innerHTML = ''; });
    $('#empty').hidden = false;
    $('#mock-banner').hidden = !state.mock;
    setStatus('');
    setBusy(false);
    renderMapResults(false);
    updateOriginOnMap(false);
    renderOrigin();
    syncChipsUI();
    syncSimUI();
    expandForm(false);
    state.formMsgKey = null;
    state.originMsgKey = null;
    state.deleteMsgKey = {key: key, tone: 'ok'};
    renderMsgs();
  }

  /* ------------------------------------------------------------ simulator presets */
  function ymd(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }
  function nowLocalInput() {
    const d = new Date();
    return ymd(d) + 'T' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }
  function applyPreset(name) {
    const now = new Date();
    const today = ymd(now);
    if (name === 'frost') Object.assign(state.sim, {dt: today + 'T18:30', temp: -25, wind: 40, precip: false});
    if (name === 'warm') Object.assign(state.sim, {dt: today + 'T19:00', temp: 18, wind: 8, precip: false});
    if (name === 'night') {
      const d = new Date(now);
      d.setDate(d.getDate() + ((5 - d.getDay() + 7) % 7));
      Object.assign(state.sim, {dt: ymd(d) + 'T23:30', temp: -2, wind: 12, precip: false});
    }
    state.sim.on = true;
    syncSimUI();
    renderSummary();
  }

  /* ------------------------------------------------------------ events */
  function bindEvents() {
    $$('.lang-btn').forEach((b) => b.addEventListener('click', () => {
      state.lang = b.dataset.lang;
      LS.set(KEYS.lang, state.lang);
      applyLang();
    }));

    $('#btn-locate').addEventListener('click', locate);
    initFromSearch();
    $('#btn-mappick').addEventListener('click', () => {
      showMapNote('map_pick_hint', 5000);
      if (mobileMQ.matches) window.scrollTo({top: 0, behavior: motionOK() ? 'smooth' : 'auto'});
    });
    $('#landmark').addEventListener('change', (e) => {
      const v = e.target.value;
      if (v === '') return;
      const l = state.landmarks[+v];
      if (l) setOrigin({lat: l.lat, lon: l.lon, source: 'landmark', lm: +v});
    });

    $('#time-chips').addEventListener('click', (e) => {
      const b = e.target.closest('[data-time]');
      if (!b) return;
      const m = +b.dataset.time;
      state.chips.time = state.chips.time === m ? null : m;
      syncChipsUI(); saveChips(); renderSummary();
    });
    $('#group-seg').addEventListener('click', (e) => {
      const b = e.target.closest('[data-group]');
      if (!b) return;
      state.chips.group = +b.dataset.group;
      syncChipsUI(); saveChips(); renderSummary();
    });
    $('#interest-chips').addEventListener('click', (e) => {
      const b = e.target.closest('[data-interest]');
      if (!b) return;
      const id = b.dataset.interest;
      const list = state.chips.interests;
      state.chips.interests = list.includes(id) ? list.filter((x) => x !== id) : list.concat(id);
      syncChipsUI(); saveChips(); renderSummary();
    });
    $('#chip-kids').addEventListener('click', () => { state.chips.kids = !state.chips.kids; syncChipsUI(); saveChips(); renderSummary(); });
    $('#chip-wheel').addEventListener('click', () => { state.chips.wheel = !state.chips.wheel; syncChipsUI(); saveChips(); renderSummary(); });
    $('#noai').addEventListener('change', (e) => { state.noAi = e.target.checked; saveChips(); renderSummary(); });
    $('#noai-info-btn').addEventListener('click', (e) => {
      const pop = $('#noai-info');
      pop.hidden = !pop.hidden;
      e.currentTarget.setAttribute('aria-expanded', String(!pop.hidden));
    });

    $('#examples').addEventListener('click', (e) => {
      const b = e.target.closest('.ex-btn');
      if (!b) return;
      $('#q').value = b.textContent;
      $('#q').focus();
      renderSummary();
    });
    $('#q').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
    });
    $('#q').addEventListener('input', renderSummary);

    // simulator
    $('#sim-on').addEventListener('change', (e) => {
      state.sim.on = e.target.checked;
      if (state.sim.on && !state.sim.dt) state.sim.dt = nowLocalInput();
      syncSimUI(); renderSummary();
    });
    const simInput = (fn) => (e) => { fn(e.target); state.sim.on = true; if (!state.sim.dt) state.sim.dt = nowLocalInput(); syncSimUI(); renderSummary(); };
    $('#sim-dt').addEventListener('change', simInput((el) => { state.sim.dt = el.value; }));
    $('#sim-temp').addEventListener('input', simInput((el) => { state.sim.temp = +el.value; }));
    $('#sim-wind').addEventListener('input', simInput((el) => { state.sim.wind = +el.value; }));
    $('#sim-precip').addEventListener('change', simInput((el) => { state.sim.precip = el.checked; }));
    $$('[data-preset]').forEach((b) => b.addEventListener('click', () => applyPreset(b.dataset.preset)));

    $('#req-form').addEventListener('submit', (e) => { e.preventDefault(); submit(); });
    $('#panel').addEventListener('click', (e) => {
      if (e.target.closest('#btn-edit')) { expandForm(true); return; }
      const act = e.target.closest('[data-act]');
      if (act && act.dataset.act === 'retry') { submit(); return; }
      if (act && act.dataset.act === 'example') {
        setOrigin({lat: SAMPLE_ORIGIN.lat, lon: SAMPLE_ORIGIN.lon, source: 'sample'}, {fly: false});
        submit({mock: true});
        return;
      }
    });

    // cards: feedback, "on map", timeline buttons
    $('#cards').addEventListener('click', (e) => {
      const li = e.target.closest('.card');
      if (!li || li.classList.contains('skeleton')) return;
      const i = +li.dataset.idx;
      const fb = e.target.closest('[data-fb]');
      if (fb) { sendFeedback(li.dataset.poi, fb.dataset.fb); return; }
      if (e.target.closest('[data-act="map"]')) { selectCard(i, {fly: true, showMap: true}); return; }
      if (e.target.closest('.card-title')) selectCard(i, {fly: true});
    });
    $('#cards').addEventListener('mouseover', (e) => {
      const li = e.target.closest('.card');
      poiMarkers.forEach((m, j) => { if (m) m.getElement().classList.toggle('is-hover', !!li && +li.dataset.idx === j); });
    });
    $('#cards').addEventListener('mouseleave', () => {
      poiMarkers.forEach((m) => { if (m) m.getElement().classList.remove('is-hover'); });
    });
    $('#route').addEventListener('click', (e) => {
      const b = e.target.closest('.tl-btn');
      if (!b || b.dataset.idx === '') return;
      selectCard(+b.dataset.idx, {fly: true, scrollCard: true});
    });

    // footer
    $('#btn-delete').addEventListener('click', deleteMyData);
    const dlg = $('#about-dialog');
    $('#btn-about').addEventListener('click', () => {
      if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    });
    $('#about-close').addEventListener('click', () => dlg.close ? dlg.close() : dlg.removeAttribute('open'));
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });

    setInterval(() => placeholderTick(false), 3800);
  }

  /* ------------------------------------------------------------ init */
  function setupMock() {
    $('#mock-banner').hidden = false;
    setOrigin({lat: SAMPLE_ORIGIN.lat, lon: SAMPLE_ORIGIN.lon, source: 'sample'}, {fly: false});
    // pre-fill the request that matches the recorded answer (not saved to storage)
    $('#q').value = t('sample_text');
    state.chips.time = 120;
    state.chips.kids = true;
    state.chips.group = 2;
    syncChipsUI();
    renderSummary();
  }

  function init() {
    state.lang = initialLang();
    loadChips();
    state.sessionId = LS.get(KEYS.session);
    state.sim.dt = nowLocalInput();
    buildStaticUI();
    applyLang();
    bindEvents();
    initMap();
    if (state.mock) setupMock();
    else loadLandmarks();
    if (map && state.origin) {
      map.once('load', () => {
        updateOriginOnMap(false);
        map.jumpTo({center: [state.origin.lon, state.origin.lat], zoom: mobileMQ.matches ? 13.4 : 14});
      });
    }
  }

  init();
})();
