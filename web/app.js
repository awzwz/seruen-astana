/* Seruen web client — static, no build step. Design G «2GIS × iOS» (sheet with detents / floating panel, inset lists).
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
  const KEYS = {lang: 'seruen.lang', chips: 'seruen.chips', session: 'seruen.session', theme: 'seruen.theme'};
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
  const mobileMQ = window.matchMedia('(max-width: 999.98px)');
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
    spark: '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/>',
    // SF-Symbols-like glyphs for design G
    gear: '<circle cx="12" cy="12" r="5.9"/><circle cx="12" cy="12" r="2.3"/><circle cx="12" cy="12" r="8.3" stroke-width="2.7" stroke-dasharray="2.6 3.92" stroke-linecap="butt"/>',
    xfill: '<circle cx="12" cy="12" r="10" fill="currentColor" stroke="none"/><path d="M8.5 8.5l7 7M15.5 8.5l-7 7" stroke="var(--cell)" stroke-width="2.2"/>',
    chevR: '<path d="M9.5 5.5l6.5 6.5-6.5 6.5"/>',
    back: '<path d="M15 5l-7 7 7 7"/>',
    loc: '<path d="M20.5 3.5L3.5 10.6l7.2 2.7 2.7 7.2z" fill="currentColor"/>',
    pinf: '<path d="M12 22s-7-6.2-7-12a7 7 0 0 1 14 0c0 5.8-7 12-7 12z" fill="currentColor" stroke="none"/><circle cx="12" cy="10" r="2.6" fill="rgba(0,0,0,.25)" stroke="none"/>',
    map: '<path d="M9 4.5L3.5 6.5v13L9 17.5l6 2 5.5-2v-13L15 6.5zM9 4.5v13M15 6.5v13"/>',
    dirs: '<path d="M12 2.8l9.2 9.2-9.2 9.2L2.8 12z"/><path d="M9 14.5v-2.5a1.5 1.5 0 0 1 1.5-1.5H15M13 8.5l2 2-2 2"/>',
    hourglass: '<path d="M7 3.5h10M7 20.5h10M8 3.5c0 4.5 4 5.5 4 8.5s-4 4-4 8.5M16 3.5c0 4.5-4 5.5-4 8.5s4 4 4 8.5"/>',
    starf: '<path d="M12 3.2l2.6 5.4 5.9.8-4.3 4.1 1 5.9L12 16.6l-5.2 2.8 1-5.9-4.3-4.1 5.9-.8z" fill="currentColor" stroke="none"/>',
    seal: '<path d="M12 2.8l2.2 1.6 2.7-.2.8 2.6 2.2 1.6-.9 2.6.9 2.6-2.2 1.6-.8 2.6-2.7-.2L12 21.2l-2.2-1.6-2.7.2-.8-2.6-2.2-1.6.9-2.6-.9-2.6 2.2-1.6.8-2.6 2.7.2z" fill="currentColor" stroke="none"/><path d="M8.5 12.2l2.4 2.4 4.6-4.8" stroke="var(--cell)" stroke-width="2"/>',
    bulb: '<path d="M9 17.5h6M10 20.5h4M12 3.5a6 6 0 0 0-3.5 10.9c.6.5 1 1.3 1 2.1h5c0-.8.4-1.6 1-2.1A6 6 0 0 0 12 3.5z"/>',
    snow: '<path d="M12 3v18M4.2 7.5l15.6 9M4.2 16.5l15.6-9M9.5 4.5L12 7l2.5-2.5M9.5 19.5L12 17l2.5 2.5"/>',
    coffee: '<path d="M5 9h11v5a5 5 0 0 1-5 5h-1a5 5 0 0 1-5-5zM16 10.5h1.5a2.5 2.5 0 0 1 0 5H16M8 3.5c-.6.9-.6 1.9 0 2.8M11.5 3.5c-.6.9-.6 1.9 0 2.8"/>',
    museum: '<path d="M3 9.5L12 4l9 5.5M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20.5h18"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    minus: '<path d="M5 12h14"/>',
    list: '<path d="M9 6.5h11M9 12h11M9 17.5h11"/><circle cx="4.8" cy="6.5" r="1"/><circle cx="4.8" cy="12" r="1"/><circle cx="4.8" cy="17.5" r="1"/>',
    globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.5 2.5 3.5 5.5 3.5 8.5s-1 6-3.5 8.5c-2.5-2.5-3.5-5.5-3.5-8.5s1-6 3.5-8.5z"/>',
    brain: '<path d="M9 4.5a3 3 0 0 0-3 3 3 3 0 0 0-1.5 5.5A3 3 0 0 0 8 18.5a2.5 2.5 0 0 0 4 1V5a2.5 2.5 0 0 0-3-.5zM15 4.5a3 3 0 0 1 3 3 3 3 0 0 1 1.5 5.5A3 3 0 0 1 16 18.5a2.5 2.5 0 0 1-4 1"/>',
    scale: '<path d="M12 4v16M7 20h10M4 8h16M6.5 8l-3 6a3 3 0 0 0 6 0zM17.5 8l-3 6a3 3 0 0 0 6 0z"/>',
    shield: '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/>'
  };
  function icon(name, cls) {
    const body = ICONS[name] || ICONS.info;
    return '<svg class="ic' + (cls ? ' ' + cls : '') + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + body + '</svg>';
  }

  /* ------------------------------------------------------------ state */
  const params = new URLSearchParams(window.location.search);
  const state = {
    lang: 'ru',
    theme: 'auto',         // auto | light | dark (auto follows the system)
    origin: null,          // {lat, lon, source: geo|map|landmark|search|sample, acc?, lm?, label?} — memory only
    cell: null,            // geohash-7 — the only location the server ever gets
    where: null,           // 2GIS address of the cell («Вы здесь»)
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
    deleteMsgKey: null,
    originMsgKey: null,
    formMsgKey: null,
    statusText: '',
    view: 'form',          // form | results
    panel: null,           // the second column: {kind: detail|trace|settings|about|from, idx?}
    detent: 'medium',      // phone sheet: small | medium | large
    pickMode: false,       // the next tap on the map sets the start
    fromQuery: '',
    fromItems: null
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

  /* ------------------------------------------------------------ theme (auto follows the system) */
  function themeNow() { return state.theme === 'auto' ? (darkMQ.matches ? 'dark' : 'light') : state.theme; }
  function applyTheme() {
    const th = themeNow();
    document.documentElement.dataset.theme = th;
    const b = $('#btn-theme');
    if (b) {
      b.innerHTML = icon(th === 'dark' ? 'sun' : 'moon');
      b.setAttribute('aria-label', t('appearance') + ': ' + t(th === 'dark' ? 'theme_light' : 'theme_dark'));
    }
    if (map) {
      const want = th === 'dark' ? STYLE_DARK : STYLE_LIGHT;
      if (map.__style !== want) { map.__style = want; map.setStyle(want, {diff: false}); }
    }
    renderMarkers();
  }

  /* ------------------------------------------------------------ static UI */
  const SPOKES = Array.from({length: 8}, (_, i) => '<line x1="12" y1="2.5" x2="12" y2="7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" opacity="' +
    (0.25 + i * 0.1).toFixed(2) + '" transform="rotate(' + (i * 45) + ' 12 12)"/>').join('');
  const spinner = () => '<svg class="ic spinner" viewBox="0 0 24 24" aria-hidden="true">' + SPOKES + '</svg>';
  const tile = (name, color) => '<span class="tile" style="background:' + color + '">' + icon(name) + '</span>';
  const cap1 = (s) => String(s).charAt(0).toUpperCase() + String(s).slice(1);
  const QUICK_COLORS = {child: 'var(--orange)', coffee: 'var(--brown)', museum: 'var(--indigo)', tree: 'var(--tint)', moon: 'var(--purple)', plane: 'var(--blue)'};

  function buildStaticUI() {
    $$('[data-icon]').forEach((el) => {
      el.innerHTML = icon(el.getAttribute('data-icon'));
      el.classList.add('icw');
    });
    $('#btn-go .spinner').innerHTML = SPOKES;
    $('#time-seg').innerHTML = TIME_OPTIONS.map((m) => '<button type="button" data-time="' + m + '" aria-pressed="false"></button>').join('');
    $('#group-seg').innerHTML = GROUP_OPTIONS.map((n) => '<button type="button" data-group="' + n + '" aria-pressed="false">' + n + '</button>').join('');
    $('#interest-caps').innerHTML = INTERESTS.map((x) => '<button type="button" class="cap" data-interest="' + x.id + '" aria-pressed="false"></button>').join('');
    $('#btn-zin').innerHTML = icon('plus');
    $('#btn-zout').innerHTML = icon('minus');
    $('#btn-locate-top').innerHTML = icon('loc');
    $('#btn-locate-desk').innerHTML = icon('loc');
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
    $$('#time-seg [data-time]').forEach((b) => { b.textContent = b.dataset.time + ' ' + t('min'); });
    $$('#interest-caps [data-interest]').forEach((b) => {
      const def = INTERESTS.find((x) => x.id === b.dataset.interest);
      b.textContent = t(def.key);
    });
    $('#quick').innerHTML = t('quick').map((q, k) => '<button type="button" class="qb" data-quick="' + k + '"><i style="background:' +
      (QUICK_COLORS[q[0]] || 'var(--tint)') + '">' + icon(q[0]) + '</i>' + esc(q[1]) + '</button>').join('');
    $('#mock-lang').hidden = state.lang === 'ru';
    $('#btn-zin').setAttribute('aria-label', t('zoom_in'));
    $('#btn-zout').setAttribute('aria-label', t('zoom_out'));
    ['#btn-locate-top', '#btn-locate-desk'].forEach((s) => $(s).setAttribute('aria-label', t('my_location')));
    placeholderTick(true);
    renderOrigin();
    syncChipsUI();
    renderMsgs();
    setBusy(state.busy);
    applyTheme();
    if (state.res) renderAll(false);
    $('#panel2').dataset.kind = '';   // re-render the open page in the new language
    renderPanel();
  }

  /* Rotating placeholder: the short hint first, then examples in the current language. */
  let phIdx = 0;
  function placeholderTick(reset) {
    const q = $('#q');
    const list = [t('search_ph')].concat(t('placeholders'));
    if (reset) phIdx = 0;
    else if (document.activeElement === q || q.value) return;
    else phIdx = (phIdx + 1) % list.length;
    q.placeholder = list[phIdx];
  }

  function lmName(l) {
    const n = l.names || {};
    return n[state.lang] || n.ru || n.en || n.kk || l.id || '?';
  }

  function syncChipsUI() {
    const c = state.chips;
    $$('#time-seg [data-time]').forEach((b) => b.setAttribute('aria-pressed', String(+b.dataset.time === c.time)));
    $$('#group-seg [data-group]').forEach((b) => b.setAttribute('aria-pressed', String(+b.dataset.group === c.group)));
    $$('#interest-caps [data-interest]').forEach((b) => b.setAttribute('aria-pressed', String(c.interests.includes(b.dataset.interest))));
    $('#sw-kids').setAttribute('aria-checked', String(c.kids));
    $('#sw-wheel').setAttribute('aria-checked', String(c.wheel));
    const na = $('#sw-noai');
    if (na) na.setAttribute('aria-checked', String(state.noAi));
  }

  function syncSimUI() {
    const s = state.sim;
    if (!$('#sim-on')) return;   // the controls live in the settings page
    $('#sim-on').setAttribute('aria-checked', String(s.on));
    $('#sim-dt').value = s.dt;
    $('#sim-temp').value = s.temp;
    $('#sim-wind').value = s.wind;
    $('#sim-precip').setAttribute('aria-checked', String(s.precip));
    $('#sim-temp-out').textContent = fmtTemp(s.temp);
    $('#sim-wind-out').textContent = s.wind + ' ' + t('kmh');
  }

  function renderMsgs() {
    setMsg('#origin-msg', state.originMsgKey);
    setMsg('#origin-msg-p', state.originMsgKey);
    setMsg('#form-msg', state.formMsgKey);
    setMsg('#delete-msg', state.deleteMsgKey);
  }
  function setMsg(sel, entry) {
    const el = $(sel);
    if (!el) return;
    if (!entry) { el.innerHTML = ''; el.className = 'form-msg'; return; }
    el.className = 'form-msg ' + (entry.tone || '');
    el.innerHTML = icon(entry.tone === 'ok' ? 'check' : entry.tone === 'warn' ? 'alert' : 'info') + '<div>' + esc(t(entry.key)) +
      (entry.help && entry.help.length ? '<ol class="geo-help">' + entry.help.map((h) => '<li>' + esc(h) + '</li>').join('') + '</ol>' : '') + '</div>';
  }

  /* ------------------------------------------------------------ origin */
  function setOrigin(o, opts) {
    const options = opts || {};
    state.origin = o;
    state.cell = geohashEncode(o.lat, o.lon, 7);
    state.originMsgKey = null;
    state.pickMode = false;
    if (state.formMsgKey && state.formMsgKey.key === 'need_origin') state.formMsgKey = null;
    renderMsgs();
    state.where = null;
    renderOrigin();
    fetchWhere();
    updateOriginOnMap(options.fly !== false);
    hideMapNote();
    // distances are recomputed locally from the new exact point
    if (state.res && state.res.cards) renderCards(false);
    if (state.panel && state.panel.kind === 'from') closePanel(true);
    else renderPanel();
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

  // «Откуда?»: 2GIS suggestions for a typed place or address (a page in the second column)
  let sugTimer = null, sugSeq = 0;
  function onFromInput(inp) {
    clearTimeout(sugTimer);
    const q = inp.value.trim();
    state.fromQuery = inp.value;
    const clr = $('#from-clear');
    if (clr) clr.hidden = !inp.value;
    if (q.length < 2 || state.mock) { state.fromItems = null; renderFromResults(); return; }
    sugTimer = setTimeout(async () => {
      const seq = ++sugSeq;
      let items = [];
      try {
        const r = await fetch('/api/suggest?q=' + encodeURIComponent(q) + '&cell=' + encodeURIComponent(state.cell || ''), {headers: {Accept: 'application/json'}});
        const j = r.ok ? await r.json() : {items: []};
        items = (j && j.items) || [];
      } catch (e) { items = []; }
      if (seq !== sugSeq) return;
      state.fromItems = items;
      renderFromResults();
    }, 250);
  }
  function renderFromResults() {
    const box = $('#from-results');
    if (!box) return;
    const items = state.fromItems;
    if (!items) { box.innerHTML = ''; return; }
    box.innerHTML = '<section class="sec"><div class="sec-h"><h3>' + esc(t('suggest_title')) + '</h3></div><div class="list tiles">' +
      (items.length ? items.map((it, i) => '<button type="button" class="row" data-sug="' + i + '">' + tile('pinf', 'var(--red)') +
        '<span class="t">' + esc(it.name) + '<span class="s">' + esc(it.subtitle || '') + '</span></span><span class="chev">' + icon('chevR') + '</span></button>').join('')
        : '<div class="row noicon"><span class="t" style="color:var(--label-2)">' + esc(t('from_none')) + '</span><span></span></div>') +
      '</div><p class="sec-f">' + esc(t('suggest_note')) + '</p></section>';
  }
  function fromHTML() {
    const chev = '<span class="chev">' + icon('chevR') + '</span>';
    return '<label class="search from-input">' + icon('search') + '<span class="sr-only">' + esc(t('from_label')) + '</span>' +
      '<input id="from-q" type="search" enterkeyhint="search" autocomplete="off" placeholder="' + esc(t('from_ph')) + '" value="' + esc(state.fromQuery) + '">' +
      '<button type="button" class="clear" id="from-clear" aria-label="' + esc(t('close_btn')) + '"' + (state.fromQuery ? '' : ' hidden') + '>' + icon('xfill') + '</button></label>' +
      '<div class="list tiles">' +
      '<button type="button" class="row" data-act="locate">' + tile('loc', 'var(--blue)') + '<span class="t">' + esc(t('my_location')) + '<span class="s">' + esc(t('my_location_sub')) + '</span></span>' + chev + '</button>' +
      '<button type="button" class="row" data-act="pick">' + tile('map', 'var(--tint)') + '<span class="t">' + esc(t('pick_on_map')) + '<span class="s">' + esc(t('pick_on_map_sub')) + '</span></span>' + chev + '</button></div>' +
      '<div class="form-msg" id="origin-msg-p" role="status"></div>' +
      '<div id="from-results"></div>' +
      '<section class="sec"><div class="sec-h"><h3>' + esc(t('landmarks_title')) + '</h3></div><div class="list tiles">' +
      state.landmarks.map((l, i) => '<button type="button" class="row" data-lm="' + i + '">' + tile('landmark', 'var(--indigo)') + '<span class="t">' + esc(lmName(l)) + '</span>' + chev + '</button>').join('') +
      '</div></section>';
  }
  function locateButtons() { return $$('#btn-locate, #btn-locate-top, #btn-locate-desk, #panel2 [data-act="locate"]'); }
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
    const o = state.origin, w = state.where;
    const line = $('#where-line');
    const u = (UNITS[state.lang] || UNITS.ru)[0];
    if (!o) {
      line.className = 'where none';
      line.innerHTML = '';   // the «Откуда идём» section right below already asks for the start
    } else {
      const place = w && w.address ? [w.building, w.address].filter(Boolean).join(', ') : originShort();
      line.className = 'where';
      line.innerHTML = icon('loc') + '<span class="wt">' + esc(t('you_are_here')) + ': <b>' + esc(place) + '</b>' +
        (w && w.address ? ' <small>· 2GIS</small>' : '') +
        (o.source === 'geo' && o.acc ? ' <small>±' + Math.max(1, Math.round(o.acc)) + ' ' + u + '</small>' : '') + '</span>' +
        '<button type="button" class="textbtn" data-act="from">' + esc(t('change_btn')) + '</button>';
    }
    $('#origin-sec').hidden = !!o;
    $('#cell-code').innerHTML = state.cell ? ' · ' + esc(t('cell_label')) + ' <code>' + esc(state.cell) + '</code>' : '';
    renderMapChip();
  }

  function locate() {
    const btns = locateButtons();
    if (!window.isSecureContext || !navigator.geolocation) {
      state.originMsgKey = {key: 'geo_insecure', tone: 'warn'};
      renderMsgs();
      return;
    }
    state.originMsgKey = {key: 'geo_locating', tone: 'info'};
    renderMsgs();
    btns.forEach((b) => { b.setAttribute('aria-busy', 'true'); b.classList.add('is-loading'); });
    // remember the permission state before asking: "denied" here = the site is blocked in the browser;
    // a refusal while the state is "prompt"/"granted" = location is switched off for the browser in the OS
    let permBefore = 'unknown';
    let retried = false;
    const ask = (opts) => navigator.geolocation.getCurrentPosition(onPos, onErr, opts);
    const onPos = (pos) => {
      btns.forEach((b) => { b.removeAttribute('aria-busy'); b.classList.remove('is-loading'); });
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
      btns.forEach((b) => { b.removeAttribute('aria-busy'); b.classList.remove('is-loading'); });
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
      if (state.panel && state.panel.kind === 'from') { $('#panel2').dataset.kind = ''; renderPanel(); }
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
    const style = themeNow() === 'dark' ? STYLE_DARK : STYLE_LIGHT;
    try {
      map = new window.maplibregl.Map({
        container: 'map',
        style: style,
        center: [ASTANA.lon, ASTANA.lat],
        zoom: mobileMQ.matches ? 11.2 : 11.8,
        minZoom: 9,
        maxZoom: 18,
        attributionControl: false,   // attribution is rendered in the page (always visible)
        dragRotate: false,
        pitchWithRotate: false,
        maxBounds: [[70.85, 50.82], [72.05, 51.45]]
      });
      map.__style = style;
    } catch (e) { mapUnavailable(); return; }
    map.touchZoomRotate.disableRotation();
    map.on('style.load', () => { paint2gis(themeNow() === 'dark'); ensureLayers(); syncMapData(); });
    map.on('click', onMapClick);
    map.on('zoomend', declutter);
    map.on('error', () => { /* tile hiccups are not user-facing; keep the console clean */ });
    // the dark OpenFreeMap style references a sprite pattern it does not ship; give it a transparent pixel
    map.on('styleimagemissing', (e) => {
      if (e && e.id && !map.hasImage(e.id)) map.addImage(e.id, {width: 1, height: 1, data: new Uint8Array(4)});
    });
  }
  function mapUnavailable() {
    document.body.classList.add('no-map');
    showMapNote('map_unavailable', 0);
  }

  // recolour OpenFreeMap towards the 2GIS palette: beige land, green parks, yellow arterials; blue-grey at night
  function paint2gis(dark) {
    const set = (id, prop, v) => { try { if (map.getLayer(id)) map.setPaintProperty(id, prop, v); } catch (e) { /* layer differs between styles */ } };
    const hide = (id) => { try { if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', 'none'); } catch (e) { /* ignore */ } };
    if (!dark) {
      set('background', 'background-color', '#F6F2DE');
      ['park', 'landcover_grass'].forEach((id) => set(id, 'fill-color', '#D3E8B7'));
      set('landcover_wood', 'fill-color', '#C6E1A6');
      set('park_outline', 'line-color', '#C3DCA2');
      set('landuse_residential', 'fill-color', '#F2EDD8');
      set('water', 'fill-color', '#A9D3F5');
      ['waterway_river', 'waterway_other', 'waterway_tunnel'].forEach((id) => set(id, 'line-color', '#A9D3F5'));
      set('building', 'fill-color', '#E7E1CE');
      set('building', 'fill-outline-color', '#D8D0B8');
      ['road_minor', 'road_service_track', 'bridge_street', 'road_path_pedestrian', 'road_secondary_tertiary', 'road_link', 'bridge_secondary_tertiary', 'bridge_link']
        .forEach((id) => set(id, 'line-color', '#FFFFFF'));
      ['road_trunk_primary', 'bridge_trunk_primary'].forEach((id) => set(id, 'line-color', '#FFE9A6'));
      ['road_motorway', 'bridge_motorway', 'road_motorway_link'].forEach((id) => set(id, 'line-color', '#FFD27A'));
      ['road_minor_casing', 'road_service_track_casing', 'road_secondary_tertiary_casing', 'road_link_casing', 'road_trunk_primary_casing', 'road_motorway_casing']
        .forEach((id) => set(id, 'line-color', '#E2D9BF'));
      ['poi_r1', 'poi_r7', 'poi_r20', 'building-3d'].forEach(hide);
    } else {
      set('background', 'background-color', '#1B1F24');
      set('water', 'fill-color', '#15283B');
      set('waterway', 'line-color', '#15283B');
      ['landcover_wood', 'landuse_park'].forEach((id) => set(id, 'fill-color', '#1D2E23'));
      set('landuse_residential', 'fill-color', '#1E2227');
      set('building', 'fill-color', '#292E35');
      ['highway_minor', 'highway_path'].forEach((id) => set(id, 'line-color', '#2C3139'));
      ['highway_major_inner', 'highway_major_subtle'].forEach((id) => set(id, 'line-color', '#3A4049'));
      ['highway_major_casing', 'highway_motorway_casing'].forEach((id) => set(id, 'line-color', '#2C3139'));
      set('highway_motorway_inner', 'line-color', '#4A4434');
      set('highway_motorway_subtle', 'line-color', '#3A3A33');
      ['highway_name_other', 'highway_name_motorway', 'place_other', 'place_suburb', 'place_village', 'place_town', 'place_city', 'place_city_large'].forEach((id) => {
        set(id, 'text-color', '#9AA2AC');
        set(id, 'text-halo-color', '#1B1F24');
      });
    }
  }

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  function ensureLayers() {
    if (!map) return;
    const route = cssVar('--route', '#0073FA');
    const casing = cssVar('--route-casing', '#ffffff');
    const cell = cssVar('--cell-color', route);
    // draw above every road/building layer but below the labels that come after them
    const layers = (map.getStyle() && map.getStyle().layers) || [];
    let lastGeom = -1;
    layers.forEach((l, k) => { if (l.type !== 'symbol') lastGeom = k; });
    const sym = layers.slice(lastGeom + 1).find((l) => l.type === 'symbol');
    const before = sym ? sym.id : undefined;
    if (!map.getSource('cell')) map.addSource('cell', {type: 'geojson', data: EMPTY_FC});
    if (!map.getSource('route')) map.addSource('route', {type: 'geojson', data: EMPTY_FC});
    const add = (layer) => { if (!map.getLayer(layer.id)) map.addLayer(layer, before); };
    add({id: 'cell-fill', type: 'fill', source: 'cell', paint: {'fill-color': cell, 'fill-opacity': themeNow() === 'dark' ? 0.14 : 0.08}});
    add({id: 'cell-line', type: 'line', source: 'cell', paint: {'line-color': cell, 'line-width': 1.5, 'line-dasharray': [2, 2]}});
    add({id: 'route-casing', type: 'line', source: 'route', layout: {'line-cap': 'round', 'line-join': 'round'},
      paint: {'line-color': casing, 'line-width': 9, 'line-opacity': 0.95}});
    add({id: 'route-solid', type: 'line', source: 'route', filter: ['!=', ['get', 'approx'], true],
      layout: {'line-cap': 'round', 'line-join': 'round'}, paint: {'line-color': route, 'line-width': 5}});
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
    if (target && target.closest && target.closest('.pin-wrap, .origin-marker')) return;
    // the map is the whole screen: a tap sets the start only when asked to (or while there is none)
    if (!state.pickMode && state.origin) return;
    const lat = e.lngLat.lat, lon = e.lngLat.lng;
    if (!inAstana(lat, lon)) { showMapNote('map_outside', 3500); return; }
    hideMapNote();
    setOrigin({lat: lat, lon: lon, source: 'map'}, {fly: false});
    if (mobileMQ.matches && state.detent === 'small') setDetent('medium');
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
    if (fly) map.easeTo({center: ll, zoom: Math.max(map.getZoom(), 14), offset: freeOffset(), duration: motionOK() ? 700 : 0});
  }

  function renderMapChip() {
    const chip = $('#map-chip');
    if (!chip) return;
    if (!state.origin || state.pickMode) {
      chip.className = 'map-chip glass hint';
      chip.innerHTML = icon('pin') + '<span>' + esc(t('map_pick_hint')) + '</span>';
    } else {
      chip.className = 'map-chip glass';
      chip.innerHTML = icon('lock') + '<span>' + esc(t('map_cell_chip')) + '<code> · ' + esc(state.cell) + '</code></span>';
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
  function badgeHTML(v, card) {
    if (v.num) return '<span class="badge' + (card && card.kind === 'event' ? ' ev' : '') + '" aria-hidden="true">' + v.num + '</span>';
    return '<span class="badge o" aria-hidden="true">' + icon(card && card.kind === 'event' ? 'calendar' : 'swap') + '</span>';
  }

  function renderMarkers() {
    poiMarkers.forEach((m) => { if (m) m.remove(); });
    poiMarkers = [];
    const r = state.res;
    if (!map || !r || !r.cards) return;
    const view = cardView();
    const green = cssVar('--tint', '#19AA1E'), purple = cssVar('--purple', '#AF52DE');
    r.cards.forEach((c, i) => {
      if (!isFinite(c.lat) || !isFinite(c.lon)) { poiMarkers.push(null); return; }
      const v = view.byIdx[i];
      const ev = c.kind === 'event';
      const wrap = document.createElement('div');
      wrap.className = 'pin-wrap ' + (v.alt ? 'alt' : 'main') + (ev ? ' is-event' : '') + (i === state.activeIdx ? ' is-active' : '');
      wrap.tabIndex = 0;
      wrap.setAttribute('role', 'button');
      wrap.setAttribute('aria-label', (v.num ? v.num + '. ' : t('alt_title') + ': ') + c.name);
      wrap.innerHTML = v.alt ? '<div class="pin-alt">' + icon(ev ? 'calendar' : 'swap') + '</div>'
        : '<svg class="pin-main" viewBox="0 0 36 46" aria-hidden="true"><path d="M18 44.5S3 29.6 3 18.5a15 15 0 0 1 30 0C33 29.6 18 44.5 18 44.5z" fill="' + (ev ? purple : green) +
          '" stroke="#fff" stroke-width="2.5"/><text x="18" y="23.5" text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif" font-size="15" font-weight="700" fill="#fff">' +
          v.num + '</text></svg>';
      const lab = document.createElement('span');
      lab.className = 'pin-label';
      lab.textContent = c.name.length > 24 ? c.name.slice(0, 23) + '…' : c.name;
      wrap.appendChild(lab);
      const go = (e) => { e.stopPropagation(); selectCard(i, {open: true, fly: true}); };
      wrap.addEventListener('click', go);
      wrap.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(e); } });
      const mk = new window.maplibregl.Marker({element: wrap, anchor: v.alt ? 'center' : 'bottom'}).setLngLat([c.lon, c.lat]).addTo(map);
      mk.__kind = v.alt ? 'alt' : 'main';
      poiMarkers.push(mk);
    });
    declutter();
  }

  // places in one building would sit on top of each other: a pin with its label is a box,
  // alternatives move up or down until their box is free
  function declutter() {
    if (!map) return;
    const H = map.getContainer().clientHeight - sheetPx();
    const box = (m, dy) => {
      const p = map.project(m.getLngLat());
      const lab = m.getElement().querySelector('.pin-label');
      const w = lab ? lab.offsetWidth + 6 : 0;
      const kind = m.__kind || 'me';
      const cy = (kind === 'main' ? p.y - 27 : p.y) + dy;
      const r = kind === 'me' ? 22 : 17;
      return {x1: p.x - r, x2: p.x + r + w, y1: cy - r, y2: cy + r};
    };
    const hit = (a, b) => a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;
    const fixed = poiMarkers.filter((m) => m && m.__kind === 'main');
    if (originMarker) fixed.push(originMarker);
    const placed = fixed.map((m) => box(m, 0));
    poiMarkers.filter((m) => m && m.__kind === 'alt').forEach((m) => {
      const ok = (dy) => { const b = box(m, dy); return b.y1 > 56 && b.y2 < H - 12 && !placed.some((q) => hit(q, b)); };
      const found = [0, -36, 36, -72, 72, -108, 108].find(ok);
      const dy = found == null ? 0 : found;
      m.setOffset([0, dy]);
      placed.push(box(m, dy));
    });
  }

  function sheetPx() { return mobileMQ.matches ? detentPx(state.detent) : 0; }
  function mapPadding() {
    if (mobileMQ.matches) return {top: 100, bottom: sheetPx() + 36, left: 36, right: 140};
    return {top: 90, bottom: 70, left: 432 + (state.panel ? 392 : 0) + 40, right: 220};
  }

  // camera moves use an offset into the free part of the map: MapLibre keeps `padding` from easeTo and adds it to every later fit
  function freeOffset() {
    const p = mapPadding();
    return [(p.left - p.right) / 2, (p.top - p.bottom) / 2];
  }
  function fitToResults() {
    const r = state.res;
    if (!map || !r || !r.cards || !r.cards.length) return;
    const pts = r.cards.filter((c) => isFinite(c.lat) && isFinite(c.lon)).map((c) => [c.lon, c.lat]);
    if (state.origin) pts.push([state.origin.lon, state.origin.lat]);
    if (!pts.length) return;
    const b = new window.maplibregl.LngLatBounds(pts[0], pts[0]);
    pts.forEach((p) => b.extend(p));
    try {
      map.fitBounds(b, {padding: mapPadding(), maxZoom: 16.2, duration: motionOK() ? 800 : 0});
    } catch (e) { /* the padding is larger than the map on a tiny screen */ }
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
    if (o.open) openPanel({kind: 'detail', idx: i});
    if (o.fly && map && card && isFinite(card.lat) && isFinite(card.lon)) {
      map.easeTo({center: [card.lon, card.lat], zoom: Math.max(map.getZoom(), 15), offset: freeOffset(), duration: motionOK() ? 700 : 0});
    }
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
      showView('form');
      setDetent('medium');
      const lb = $('#btn-locate');
      if (lb && !$('#origin-sec').hidden) lb.focus();
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
        renderHead();
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
    $('#view-results').setAttribute('aria-busy', String(b));
    renderStatus();
  }
  function setStatus(text) { state.statusText = text || ''; renderStatus(); }
  function renderStatus() {
    $('#status').innerHTML = (state.busy ? spinner() : '') + '<span>' + esc(state.statusText) + '</span>';
  }

  /* ------------------------------------------------------------ views: the request and the answer */
  function showView(v) {
    state.view = v;
    $('#view-form').hidden = v !== 'form';
    $('#view-results').hidden = v !== 'results';
    $('#q-clear').hidden = !$('#q').value;
    $('#sheet-body').scrollTop = 0;
  }

  /* ------------------------------------------------------------ results rendering */
  function startResults(isMock) {
    state.res = {mock: isMock, noAi: state.noAi, t0: performance.now(), context: null, constraints: null, cards: null,
      route: null, origin: null, texts: {}, trace: null, done: null, errorView: null, elapsed: null};
    state.feedback = {};
    state.activeIdx = -1;
    closePanel(true);
    $('#mock-banner').hidden = !isMock;
    ['#ctx', '#constraints', '#route', '#error', '#trace', '#done'].forEach((s) => { $(s).innerHTML = ''; });
    $('#cards').innerHTML = skeletonHTML();
    setStatus(t('status_waiting'));
    showView('results');
    renderHead();
    renderMapResults(false);
    if (mobileMQ.matches && state.detent !== 'medium') setDetent('medium');
    $('#q').blur();
    $('#results-title').focus({preventScroll: true});
  }
  function skeletonHTML() {
    const ph = (w, cls) => '<span class="ph' + (cls ? ' ' + cls : '') + '" style="width:' + w + '%"></span>';
    return '<div class="view" aria-hidden="true"><div class="list"><div class="hero"><div class="hero-top"><span class="badge" style="background:var(--fill)"></span>' +
      '<div class="ph-stack">' + ph(62, 't') + ph(36) + '</div><span></span></div><div class="ph-stack">' + ph(94) + ph(82) + ph(56) + '</div></div></div>' +
      '<div class="list">' + [62, 48].map((w) => '<div class="row noicon"><span class="ph-stack">' + ph(w, 't') + ph(w + 22) + '</span><span></span></div>').join('') + '</div></div>';
  }
  function renderHead() {
    const r = state.res;
    const h = $('#results-title');
    if (!r) { h.textContent = ''; return; }
    const total = r.route && r.route.total_min;
    const budget = r.constraints && r.constraints.time_budget_min;
    h.textContent = !r.cards ? t('submitting') : total ? t('plan_title', {n: total}) : budget ? t('plan_title', {n: budget}) : t('plan_title_free');
  }

  function renderAll(animate) {
    const r = state.res;
    if (!r) return;
    $('#mock-banner').hidden = !r.mock;
    renderHead();
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
    const cold = w.feels_c != null && w.feels_c < 0;
    const head = w.temp_c != null ? fmtTemp(w.temp_c) + (w.feels_c != null ? ', ' + t('feels', {t: fmtTemp(w.feels_c)}) : '') : (time || '');
    const sub = [];
    if (wd != null) sub.push(t('weekdays')[wd]);
    if (c.is_dark) sub.push(t('dark'));
    else if (sunset) {
      const soon = c.minutes_to_sunset != null && c.minutes_to_sunset >= 0 && c.minutes_to_sunset <= 180;
      sub.push(t('sunset_at', {t: sunset}) + (soon ? ' · ' + t('sunset_in', {m: c.minutes_to_sunset}) : ''));
    }
    if (w.wind_kmh != null) sub.push(t('wind', {v: Math.round(w.wind_kmh)}));
    if (w.precip_prob != null) {
      const p = Math.round(w.precip_prob > 1 ? w.precip_prob : w.precip_prob * 100);
      if (p >= 30) sub.push(t('precip', {p: p}));
    }
    if (c.is_holiday) sub.push(c.holiday_name || t('holiday'));
    const wIcon = c.is_dark ? ['moon', 'var(--indigo)'] : cold ? ['snow', 'var(--teal)'] : ['sun', 'var(--orange)'];
    const mIcon = {walk: ['walk', 'var(--tint)'], short: ['route', 'var(--orange)'], indoor: ['home', 'var(--red)']}[mode];
    box.innerHTML = '<section class="sec" aria-label="' + esc(t('weather_src')) + '"><div class="list tiles">' +
      '<div class="row">' + tile(wIcon[0], wIcon[1]) + '<span class="t">' + esc(head) + '<span class="s">' + esc(sub.join(' · ')) + '</span></span>' +
        (time ? '<span class="tr num">' + esc(time) + '</span>' : '<span></span>') + '</div>' +
      '<div class="row top">' + tile(mIcon[0], mIcon[1]) + '<span class="t"><b>' + esc(t('mode_' + mode)) + '</b>' +
        (c.mode_reason ? '<span class="s">' + esc(c.mode_reason) + '</span>' : '') + '</span><span></span></div></div>' +
      '<p class="sec-f">' + (sim ? '<span class="cap b">' + icon('sliders') + esc(t('simulated_tag')) + '</span> ' : '') + esc(t('weather_src')) + (w.desc ? ' · ' + esc(w.desc) : '') + '</p></section>';
  }

  function renderConstraints() {
    const r = state.res;
    const c = r && r.constraints;
    const box = $('#constraints');
    if (!c) { box.innerHTML = ''; return; }
    const bits = [];
    if (c.time_budget_min) bits.push(c.time_budget_min + ' ' + t('min'));
    if (c.adults) bits.push(t('c_adults', {n: c.adults}));
    if (c.kids) bits.push(t('c_kids', {n: c.kids}));
    if (c.mobility) bits.push(t('mob_' + c.mobility));
    if (c.reserve_min) bits.push(t('c_reserve', {n: c.reserve_min}));
    if (c.must_return) bits.push(t('c_return'));
    if (c.destination) bits.push(t('c_dest_' + c.destination));
    if (c.night) bits.push(t('c_night'));
    const wants = (c.wants || []).map(tagLabel);
    const avoid = (c.avoid || []).map(tagLabel);
    box.innerHTML = '<section class="sec"><div class="sec-h"><h3>' + esc(t('understood')) + '</h3><span>' +
      (c.parsed_by === 'llm' ? icon('sparkle') + ' ' + esc(t('parsed_llm')) : icon('cpu') + ' ' + esc(t('parsed_rules'))) + '</span></div>' +
      '<div class="caps">' + bits.map((b) => '<span class="cap">' + esc(b) + '</span>').join('') +
      (wants.length ? '<span class="cap g">' + esc(t('c_wants')) + ': ' + esc(wants.join(', ')) + '</span>' : '') +
      (avoid.length ? '<span class="cap r">' + esc(t('c_avoid')) + ': ' + esc(avoid.join(', ')) + '</span>' : '') + '</div></section>';
  }

  function whereText() {
    const w = state.where;
    return w && w.address ? [w.building, w.address].filter(Boolean).join(', ') : originShort();
  }

  function renderRoute() {
    const r = state.res;
    const box = $('#route');
    const route = r && r.route;
    const td = r && r.toDestObj;
    const destKey = r && r.constraints && r.constraints.destination ? 'dest_' + r.constraints.destination : 'dest_other';
    let dest = '';
    if (td && (td.taxi || td.transit)) {
      dest = '<section class="sec"><div class="sec-h"><h3>' + esc(t('dest_then', {name: td.name || ''})) + '</h3>' +
        (td.from ? '<span>' + esc(t('dest_from', {name: td.from})) + '</span>' : '') + '</div>' + movesHTML(td) + '</section>';
    } else if (r && r.toDest != null && isFinite(r.toDest)) {
      dest = '<p class="prov" style="--pc:var(--blue)">' + icon(destKey === 'dest_airport' ? 'plane' : 'arrow') + '<span>' + esc(t(destKey, {n: Math.round(r.toDest)})) + '</span></p>';
    }
    if (!route || !Array.isArray(route.stops) || !route.stops.length) { box.innerHTML = dest; return; }
    const view = cardView();
    const idxById = {};
    (r.cards || []).forEach((c, i) => { idxById[c.poi_id] = i; });
    const tl = {};
    (route.timeline || []).forEach((x) => { if (x && x.poi_id) tl[x.poi_id] = x; });
    const legs = route.legs || [];
    const startT = hhmm(r.context && r.context.local_time);
    const rows = [];
    rows.push('<div class="row"><span class="tm">' + esc(startT || '') + '</span><span class="dot"><i style="--c:var(--blue)"></i></span><span class="t">' + esc(t('start')) +
      '<span class="s">' + esc(whereText()) + '</span></span><span></span></div>');
    let outdoor = 0;
    route.stops.forEach((id, k) => {
      const leg = legs[k];
      if (leg) outdoor += Number(leg.outdoor_min) || 0;
      const i = idxById[id];
      const card = i != null ? r.cards[i] : null;
      const x = tl[id] || {};
      const arrive = hhmm(x.arrive), leave = hhmm(x.leave);
      const v = i != null ? view.byIdx[i] : null;
      rows.push('<button type="button" class="row' + (leg && leg.approx ? ' approx' : '') + '"' + (i != null ? ' data-idx="' + i + '" data-act="open"' : ' disabled') + '>' +
        '<span class="tm">' + esc(arrive || '') + '</span><span class="dot"><b>' + (v && v.num ? v.num : '•') + '</b></span>' +
        '<span class="t">' + esc(card ? card.name : id) + '<span class="s">' +
        esc([leg ? t('leg_walk', {n: leg.minutes}) : '', leave ? t('until', {t: leave}) : ''].filter(Boolean).join(' · ')) + '</span></span>' +
        '<span class="chev">' + icon('chevR') + '</span></button>');
    });
    const last = route.timeline && route.timeline.length ? hhmm(route.timeline[route.timeline.length - 1].leave) : null;
    if (last) rows.push('<div class="row"><span class="tm">' + esc(last) + '</span><span class="dot"><i style="--c:var(--gray)"></i></span><span class="t">' + esc(cap1(t('finish'))) + '</span><span></span></div>');
    const anyApprox = legs.some((l) => l && l.approx);
    box.innerHTML = '<section class="sec"><div class="sec-h"><h3>' + esc(t('route_title', {n: route.total_min})) + '</h3>' +
      (outdoor ? '<span>' + esc(t('outdoor_total', {n: outdoor})) + '</span>' : '') + '</div>' +
      '<div class="list itin">' + rows.join('') + '</div>' +
      (route.warm_stops || anyApprox ? '<p class="sec-f">' + (route.warm_stops ? '<span class="cap o">' + icon('flame') + esc(t('warm_stops')) + '</span> ' : '') +
        (anyApprox ? esc(t('approx_leg')) : '') + '</p>' : '') +
      '</section>' + dest;
  }

  function hoursParts(h0) {
    const h = h0 || {status: 'unknown', level: 'unknown', label: ''};
    let level = h.level || 'unknown';
    if (h.status === 'open_area') level = 'open_area';
    if (!['high', 'medium', 'low', 'unknown', 'open_area'].includes(level)) level = 'unknown';
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
    return {h: h, level: level, main: main, note: note};
  }
  function hoursLine(h0) {
    const p = hoursParts(h0);
    const cls = p.h.status === 'closed' ? 'bad' : (p.level === 'high' || p.level === 'medium') ? 'ok' : p.level === 'open_area' ? '' : 'mid';
    let s = '<div class="hline">' + (p.level === 'open_area' ? icon('tree') : '') + '<span class="' + cls + '">' + esc(p.main) + '</span>' +
      (p.note ? '<span>· ' + esc(p.note) + '</span>' : '');
    if (p.level === 'high' && p.h.status !== 'closed') s += '<span class="seal" title="' + esc(t('lvl_high')) + '">' + icon('seal') + '</span>';
    if (p.h.status === 'closes_soon') s += '<span class="cap o">' + icon('clock') + esc(t('closes_soon')) + '</span>';
    else if (p.h.status === 'closed') s += '<span class="cap r">' + icon('xCircle') + esc(t('closed_now')) + '</span>';
    return s + '</div>';
  }

  // taxi + public transport from 2GIS (for far places and the way to the airport / station)
  function movesHTML(m) {
    if (!m || (!m.taxi && !m.transit)) return '';
    const tiles = [];
    const km = (UNITS[state.lang] || UNITS.ru)[1];
    if (m.taxi) {
      tiles.push('<div class="mode" style="--c:var(--yellow)"><div class="h">' + icon('car') + esc(t('taxi_title')) + '</div><div class="v">' + esc(m.taxi.minutes) + ' ' + esc(t('min')) + '</div>' +
        '<div class="d">' + esc(fmtNum(m.taxi.km || 0, 1)) + ' ' + esc(km) + ' · ' + esc(t('traffic_note')) + '</div></div>');
    }
    if (m.transit) {
      const lines = (m.transit.lines || []).map((l) => ((l.kind || '') + ' ' + (l.names || []).join('/')).trim()).join(' → ');
      tiles.push('<div class="mode" style="--c:var(--blue)"><div class="h">' + icon('bus') + esc(t('transit_title')) + '</div><div class="v">' + esc(m.transit.minutes) + ' ' + esc(t('min')) + '</div>' +
        '<div class="d">' + esc([lines || t('transit_generic'), m.transit.transfers ? t('transfers', {n: m.transit.transfers}) : '', m.transit.walk || ''].filter(Boolean).join(' · ')) + '</div></div>');
    }
    const link = safeUrl((m.taxi && m.taxi.link) || (m.transit && m.transit.link));
    return '<div class="list"><div class="hero"><div class="modes' + (tiles.length === 1 ? ' one' : '') + '">' + tiles.join('') + '</div>' +
      (link ? '<a class="btn prom block" href="' + esc(link) + '" target="_blank" rel="noopener">' + icon('map') + esc(t('open_2gis')) + '</a>' : '') + '</div></div>';
  }

  function accessInfo(card) {
    const a = card.accessibility;
    const wants = state.chips.wheel || (state.res && state.res.constraints && ['wheelchair', 'stroller'].includes(state.res.constraints.mobility));
    if (!a || a.value === null || a.value === undefined || a.value === '') return wants ? {key: 'access_unknown', src: ''} : null;
    const v = String(a.value).toLowerCase();
    const key = (v === 'yes' || v === 'true' || v === 'designated') ? 'access_yes' : v === 'limited' ? 'access_limited' : (v === 'no' || v === 'false') ? 'access_no' : 'access_unknown';
    return {key: key, src: a.src ? srcList(a.src) : ''};
  }

  function fmtEventTime(start, end) {
    const s = hhmm(start), e = hhmm(end);
    const d = /^\d{4}-\d{2}-\d{2}/.test(start || '') ? fmtDay(start) : '';
    return [d, s ? s + (e ? '–' + e : '') : ''].filter(Boolean).join(', ');
  }
  function fmtPrice(p) {
    const s = String(p).trim();
    if (/^(0|free|бесплатно|тегін)$/i.test(s)) return t('free');
    if (/^\d+([.,]\d+)?$/.test(s)) return fmtNum(Number(s.replace(',', '.'))) + ' ₸';
    return /₸|тг|tg|kzt/i.test(s) ? s : s + ' ₸';
  }
  function eventInfo(card) {
    if (card.kind !== 'event' && !card.event) return null;
    const e = card.event || {};
    const rawPrice = e.price_kzt != null ? e.price_kzt : e.price;
    return {
      title: e.title && e.title !== card.name ? e.title : '',
      when: fmtEventTime(e.start, e.end),
      venue: e.venue_name || e.venue || '',
      price: rawPrice !== undefined && rawPrice !== null && rawPrice !== '' ? fmtPrice(rawPrice) : '',
      url: safeUrl(e.url || card.url)
    };
  }
  function statusWarn(card) {
    if (card.status === 'possibly_closed') return '<p class="warnline">' + icon('alert') + '<span>' + esc(t('status_possibly_closed')) + '</span></p>';
    if (card.status === 'unconfirmed') return '<p class="warnline soft">' + icon('help') + '<span>' + esc(t('status_unconfirmed')) + '</span></p>';
    return '';
  }
  function reviewsText(n) {
    const v = fmtNum(n, 0);
    if (state.lang === 'en') return v + (n === 1 ? ' review' : ' reviews') + ' on 2GIS';
    if (state.lang === 'kk') return v + ' пікір · 2GIS';
    return v + ' ' + plural(n, ['отзыв', 'отзыва', 'отзывов']) + ' в 2GIS';
  }
  function cardCat(card) {
    const catText = (card.kind !== 'event' && catLabel(card.category)) || card.category_label || '';
    const tags = [];
    (card.tags || []).forEach((x) => {
      const lbl = x === 'event' ? '' : tagLabel(x);
      if (lbl && lbl.toLowerCase() !== catText.toLowerCase() && !tags.includes(lbl) && tags.length < 3) tags.push(lbl);
    });
    return {catText: catText, tags: tags};
  }

  // 2GIS directions start at the centre of the privacy cell: the exact point stays on the phone
  function dirLink(card) {
    if (!isFinite(card.lat) || !isFinite(card.lon)) return null;
    const to = Number(card.lon).toFixed(6) + '%2C' + Number(card.lat).toFixed(6);
    if (!state.cell) return 'https://2gis.kz/astana/geo/' + to;
    const b = geohashBounds(state.cell);
    return 'https://2gis.kz/astana/directions/points/' + ((b.w + b.e) / 2).toFixed(6) + '%2C' + ((b.s + b.n) / 2).toFixed(6) + '%7C' + to;
  }
  function geoLink(card) {
    return isFinite(card.lat) && isFinite(card.lon) ? 'https://2gis.kz/astana/geo/' + Number(card.lon).toFixed(6) + '%2C' + Number(card.lat).toFixed(6) : null;
  }

  function scoreHTML(card) {
    const parts = card.score_parts || {};
    const keys = Object.keys(parts);
    if (!card.score && !keys.length) return '';
    const names = t('score_parts') || {};
    return '<section class="sec"><div class="sec-h"><h3>' + esc(t('why_higher')) + '</h3><span>' + esc(t('score')) + ' ' + esc(fmtNum(card.score || 0, 2)) + '</span></div>' +
      (keys.length ? '<div class="list">' + keys.map((k) => {
        const v = Math.max(0, Math.min(1, Number(parts[k]) || 0));
        return '<div class="row noicon"><span class="gauge" style="grid-column:1/-1"><span class="t">' + esc(cap1(names[k] || k)) + '</span>' +
          '<span class="bar"><i style="width:' + Math.round(v * 100) + '%"></i></span><b>' + esc(fmtNum(Number(parts[k]) || 0, 2)) + '</b></span></div>';
      }).join('') + '</div>' : '') +
      '<p class="sec-f">' + esc(t('weights_note')) + '</p></section>';
  }

  function fbHTML(card) {
    const f = state.feedback[card.poi_id];
    const locked = f && (f.st === 'ok' || f.st === 'mock' || f.st === 'sending');
    const rows = FEEDBACK.map((x) => '<button type="button" class="row ' + (x.kind === 'closed' ? 'danger' : 'tint') + '" data-fb="' + x.kind + '" data-poi="' + esc(card.poi_id) + '"' +
      (locked ? ' disabled' : '') + ' aria-pressed="' + String(!!(f && f.kind === x.kind)) + '"><span class="icn"' + (x.kind === 'closed' ? ' style="color:var(--red)"' : '') + '>' + icon(x.icon) + '</span>' +
      '<span class="t">' + esc(t(x.key)) + '</span>' + (f && f.kind === x.kind ? '<span class="check">' + icon('check') + '</span>' : '<span></span>') + '</button>').join('');
    let msg = '';
    if (f && f.st === 'ok') msg = t('fb_thanks');
    else if (f && f.st === 'mock') msg = t('fb_thanks_mock');
    else if (f && f.st === 'error') msg = t('fb_error');
    return '<section class="sec"><div class="sec-h"><h3>' + esc(t('report_title')) + '</h3></div><div class="list" role="group" aria-label="' + esc(t('fb_label')) + '">' + rows + '</div>' +
      '<p class="sec-f" role="status">' + esc(msg) + '</p></section>';
  }

  function wordsHTML(text, animate, offset) {
    if (!animate || !motionOK()) return esc(text);
    let n = offset || 0;
    return String(text).split(/(\s+)/).map((w) => (/^\s+$/.test(w) || !w ? w : '<span class="w" style="--i:' + (n++) + '">' + esc(w) + '</span>')).join('');
  }
  function provHTML(exp) {
    if (!exp) return '';
    const idea = exp.source === 'ai_idea' ? ' · ' + esc(t('exp_ai_idea')) : '';
    if (exp.template) return '<p class="prov" style="--pc:var(--label-2)">' + icon('cpu') + '<span>' + esc(t('tpl_badge_full')) + idea + '</span></p>';
    return '<p class="prov">' + icon('sparkle') + '<span>' + esc(t('ai_badge_full')) + idea + '</span></p>';
  }
  function expBodyHTML(exp, animate) {
    const why = String(exp.why || '');
    const whyWords = why.split(/\s+/).filter(Boolean).length;
    return (why ? '<div class="why"><p>' + wordsHTML(why, animate, 0) + '</p></div>' : '') +
      (exp.what_to_do ? '<div class="todo">' + icon('bulb') + '<span>' + wordsHTML(exp.what_to_do, animate, whyWords + 2) + '</span></div>' : '');
  }
  function expPending() {
    return '<div class="ph-stack"><span class="ph" style="width:92%"></span><span class="ph" style="width:72%"></span></div>' +
      '<p class="prov" style="--pc:var(--label-2);padding:0;margin-top:8px">' + spinner() + '<span>' + esc(t('writing')) + '</span></p>';
  }
  function expMissing() {
    return '<p class="prov warn" style="padding:0">' + icon('info') + '<span>' + esc(t('no_text')) + '</span></p>';
  }

  function heroHTML(card, i, animate) {
    const r = state.res;
    const v = cardView().byIdx[i] || {num: i + 1, alt: false};
    const walk = localWalk(card);
    const exp = r.texts[card.poi_id];
    const cc = cardCat(card);
    const acc = accessInfo(card);
    const ev = eventInfo(card);
    let expBody;
    if (exp) expBody = expBodyHTML(exp, animate);
    else if (r.done || r.errorView) expBody = expMissing();
    else expBody = expPending();
    const route = dirLink(card);
    const meta = ['<span>' + icon('walk') + ' <b>' + esc(walk.min + ' ' + t('min')) + '</b> · ' + esc(fmtDist(walk.m)) + '</span>'];
    if (card.visit_min) meta.push('<span>' + icon('hourglass') + ' ' + esc(t('visit', {n: card.visit_min})) + '</span>');
    if (acc && acc.key !== 'access_unknown') meta.push('<span>' + icon('wheelchair') + ' ' + esc(t(acc.key)) + '</span>');
    if (card.taxi) meta.push('<span>' + icon('car') + ' ' + esc(card.taxi.minutes + ' ' + t('min')) + '</span>');
    if (card.transit) meta.push('<span>' + icon('bus') + ' ' + esc(card.transit.minutes + ' ' + t('min')) + '</span>');
    return '<div class="sec card' + (animate ? ' card-in' : '') + (i === state.activeIdx ? ' is-active' : '') + '" id="card-' + i + '" data-idx="' + i + '" data-poi="' + esc(card.poi_id) + '" style="--d:' + (v.num || i) + '">' +
      '<article class="list" aria-labelledby="card-' + i + '-t"><div class="hero">' +
      '<div class="hero-top">' + badgeHTML(v, card) + '<div><h2 id="card-' + i + '-t">' + esc(card.name) + '</h2><div class="c">' + esc([cc.catText].concat(cc.tags).filter(Boolean).join(' · ')) +
        (ev ? ' <span class="cap p">' + icon('calendar') + esc(t('event')) + '</span>' : '') + '</div></div>' +
        (card.rating ? '<div class="rate"><b>' + icon('starf') + esc(fmtNum(card.rating, 1)) + '</b><span>' + esc(reviewsText(card.reviews || 0)) + '</span></div>' : '<span></span>') + '</div>' +
      (card.kind === 'event' ? '' : hoursLine(card.hours)) + statusWarn(card) +
      '<div class="meta">' + meta.join('') + '</div>' +
      (ev && (ev.when || ev.venue) ? '<div class="meta"><span>' + icon('calendar') + ' ' + esc([ev.when, ev.venue].filter(Boolean).join(' · ')) + '</span></div>' : '') +
      '<div class="hr"></div>' +
      '<div class="exp" id="exp-' + i + '" aria-live="polite">' + expBody + '</div>' +
      '<div class="btnrow">' + (route ? '<a class="btn prom grow" href="' + esc(route) + '" target="_blank" rel="noopener">' + icon('dirs') + esc(t('route_btn')) + '</a>' : '') +
        '<button type="button" class="btn gray' + (route ? '' : ' grow') + '" data-act="open">' + esc(t('more_btn')) + '</button>' +
        '<button type="button" class="btn gray icon" data-act="map" aria-label="' + esc(t('show_on_map') + ': ' + card.name) + '" title="' + esc(t('show_on_map')) + '">' + icon('pin') + '</button></div>' +
      '</div></article><div id="prov-' + i + '">' + provHTML(exp) + '</div></div>';
  }
  function altRowHTML(card, i) {
    const walk = localWalk(card);
    const hp = hoursParts(card.hours);
    const bits = [walk.min + ' ' + t('min'), card.kind === 'event' ? '' : hp.main, card.rating ? '★ ' + fmtNum(card.rating, 1) + (card.reviews ? ' (' + card.reviews + ')' : '') : ''].filter(Boolean);
    return '<button type="button" class="row card' + (i === state.activeIdx ? ' is-active' : '') + '" id="card-' + i + '" data-idx="' + i + '" data-poi="' + esc(card.poi_id) + '" data-act="open">' +
      badgeHTML({}, card) + '<span class="t">' + esc(card.name) + '<span class="s">' + esc(bits.join(' · ')) + '</span></span><span class="chev">' + icon('chevR') + '</span></button>';
  }

  function renderCards(animate) {
    const r = state.res;
    const box = $('#cards');
    if (!r || !r.cards) return;
    if (!r.cards.length) {
      box.innerHTML = '<div class="list tiles"><div class="row top">' + tile('search', 'var(--gray)') + '<span class="t">' + esc(t('nothing_found')) + '</span><span></span></div></div>';
      return;
    }
    const view = cardView();
    box.innerHTML = '<div class="view">' + view.main.map((x) => heroHTML(x.c, x.i, animate)).join('') +
      (view.alts.length ? '<section class="sec"><div class="sec-h"><h3>' + esc(t('if_not')) + '</h3><span>' + esc(t('alt_sub')) + '</span></div><div class="list">' +
        view.alts.map((x) => altRowHTML(x.c, x.i)).join('') + '</div><p class="sec-f">' + esc(t('alt_hint')) + '</p></section>' : '') + '</div>';
  }

  function applyText(exp, animate) {
    const r = state.res;
    const i = (r.cards || []).findIndex((c) => c.poi_id === exp.poi_id);
    if (i < 0) return; // arrives before cards: kept in r.texts, applied when cards render
    const box = $('#exp-' + i);
    if (box) {
      box.innerHTML = expBodyHTML(exp, animate);
      const pv = $('#prov-' + i);
      if (pv) pv.innerHTML = provHTML(exp);
    }
    if (state.panel && state.panel.kind === 'detail' && state.panel.idx === i) renderPanel();
  }

  function traceStats(tr) {
    const steps = Array.isArray(tr.steps) ? tr.steps : [];
    const totalStep = steps.find((x) => x.service === 'orchestrator');
    const parts = steps.filter((x) => x !== totalStep);
    const total = totalStep ? Number(totalStep.ms) || 0 : parts.reduce((s, x) => s + (Number(x.ms) || 0), 0);
    return {steps: steps, totalStep: totalStep, parts: parts, total: total};
  }
  function renderTrace() {
    const r = state.res;
    const tr = r && r.trace;
    const box = $('#trace');
    if (!tr) { box.innerHTML = ''; return; }
    const st = traceStats(tr);
    box.innerHTML = '<div class="list tiles"><button type="button" class="row" data-act="trace">' + tile('list', 'var(--indigo)') + '<span class="t">' + esc(t('trace_title')) +
      '<span class="s">' + esc(t('trace_meta', {n: st.parts.length + ' ' + plural(st.parts.length, t('step_forms')), s: fmtDur(st.total)})) + '</span></span><span class="chev">' + icon('chevR') + '</span></button></div>';
    if (state.panel && state.panel.kind === 'trace') renderPanel();
  }
  const STEP_TILE = {browser: ['lock', 'var(--gray)'], session: ['clock', 'var(--gray)'], 'events-api': ['calendar', 'var(--purple)'], 'feedback-api': ['flag', 'var(--orange)'], context: ['sun', 'var(--orange)'], nlu: ['brain', 'var(--purple)'], retrieval: ['search', 'var(--blue)'],
    ranking: ['scale', 'var(--tint)'], routing: ['route', 'var(--teal)'], 'catalog-api': ['database', 'var(--brown)'], 'places-live': ['pinf', 'var(--red)'],
    explain: ['sparkle', 'var(--purple)'], guard: ['shield', 'var(--teal)'], orchestrator: ['cpu', 'var(--indigo)']};
  function traceHTML() {
    const r = state.res;
    const tr = r && r.trace;
    if (!tr) return '<p class="note">' + esc(t('status_waiting')) + '</p>';
    const st = traceStats(tr);
    const max = Math.max(1, ...st.parts.map((s) => Number(s.ms) || 0));
    const mode = String(tr.mode || 'template');
    const modeTag = mode === 'ai' ? ['p', t('mode_ai')] : mode.indexOf('ai') === 0 ? ['p', t('mode_mixed')] : ['', t('mode_template')];
    const ul = (arr) => (arr && arr.length ? '<ul class="ul">' + arr.map((x) => '<li>' + esc(x) + '</li>').join('') + '</ul>' : '');
    return '<div class="ttl" style="padding:0 4px"><h1 class="big">' + esc(t('trace_title')) + '</h1><p>' + esc(t('trace_total', {s: fmtDur(st.total)})) + '</p></div>' +
      '<div class="list tiles">' + st.steps.map((s) => {
        const tl = STEP_TILE[s.service] || ['cpu', 'var(--gray)'];
        const pct = s === st.totalStep ? 100 : Math.max(2, (Number(s.ms) || 0) / max * 100);
        return '<div class="row top">' + tile(tl[0], tl[1]) + '<span class="t"><b>' + esc(s.name || s.service) + '</b>' + (s.detail ? '<span class="x">' + esc(s.detail) + '</span>' : '') +
          '<span class="stepbar" style="--c:' + tl[1] + '"><i style="width:' + pct.toFixed(1) + '%"></i></span></span><span class="tr num">' + esc(fmtDur(s.ms)) + '</span></div>';
      }).join('') + '</div>' +
      '<section class="sec"><div class="sec-h"><h3>' + esc(t('data_flows')) + '</h3></div><div class="list tiles">' +
      '<div class="row top">' + tile('sparkle', 'var(--purple)') + '<span class="t">' + esc(t('flows_ai')) + ul(tr.sent_to_llm) + '</span><span></span></div>' +
      '<div class="row top">' + tile('map', 'var(--tint)') + '<span class="t">' + esc(t('flows_2gis')) + '<span class="s">' + esc(t('flows_2gis_text')) + '</span></span><span></span></div>' +
      '<div class="row top">' + tile('lock', 'var(--gray)') + '<span class="t">' + esc(t('nowhere')) + ul(tr.not_sent) + '</span><span></span></div></div>' +
      '<p class="sec-f">' + esc(t('model')) + ': <b>' + esc(tr.model || '—') + '</b> · ' + esc(t('mode_label')) + ': <span class="cap ' + modeTag[0] + '">' + esc(modeTag[1]) + '</span></p>' +
      '<p class="sec-f">' + esc(t('law_note')) + '</p></section>';
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
    box.innerHTML = '<p class="prov' + (warn ? ' warn' : '') + '" style="--pc:' + (d.ai_generated ? 'var(--purple)' : 'var(--tint)') + '">' + icon(warn ? 'alert' : d.ai_generated ? 'sparkle' : 'check') +
      '<span>' + esc(t(key)) + (d.request_id ? ' · ' + esc(t('request_id')) + ' <code>' + esc(d.request_id) + '</code>' : '') + '</span></p>' +
      '<p class="note">' + esc(t('law_note')) + '</p>';
    if (r.elapsed != null) setStatus(t('status_done', {s: fmtDur(r.elapsed)}));
    // ?open=N | ?open=trace — open a page once the answer is complete (demos and screenshots)
    const op = params.get('open');
    if (op != null && !r.__opened) {
      r.__opened = true;
      if (op === 'trace') openPanel({kind: 'trace'});
      else if (r.cards && r.cards[+op]) selectCard(+op, {open: true, fly: true});
    }
  }

  function renderErrorBox() {
    const r = state.res;
    const box = $('#error');
    if (!r || !r.errorView) { box.innerHTML = ''; return; }
    const v = r.errorView;
    box.innerHTML = '<div class="list tiles alert" role="alert"><div class="row top">' + tile('alert', 'var(--orange)') + '<span class="t"><b>' + esc(t('error_title')) + '</b>' +
      '<span class="s">' + esc(v.msg) + '</span><span class="btnrow">' +
      (v.offer ? '<button type="button" class="btn tinted sm" data-act="example">' + icon('sparkle') + esc(t('show_example')) + '</button>' : '') +
      '<button type="button" class="btn gray sm" data-act="retry">' + esc(t('retry')) + '</button></span></span><span></span></div></div>';
  }

  /* ------------------------------------------------------------ second column: place, how I chose, settings, about, «Откуда?» */
  function navHTML(left, title, right) {
    return '<div class="nav">' + (left || '<span></span>') + '<h2 id="p2-title">' + esc(title || '') + '</h2><div class="r">' + (right || '') + '</div></div>';
  }
  function openPanel(p) {
    const el = $('#panel2');
    const wasOpen = !!state.panel;
    clearTimeout(closeTimer);
    state.panel = p;
    renderPanel();
    el.hidden = false;
    void el.offsetWidth;
    el.classList.add('open');
    $('#scrim').classList.add('open');
    document.body.classList.add('p2');
    if (!wasOpen && !mobileMQ.matches && state.res && state.res.cards && p.kind !== 'detail') fitToResults();
    const f = el.querySelector(p.kind === 'from' && !mobileMQ.matches ? '#from-q' : '.nav button, .nav a');
    if (f) f.focus({preventScroll: true});
  }
  let closeTimer = null;
  function closePanel(silent) {
    if (!state.panel) return;
    const was = state.panel;
    state.panel = null;
    const el = $('#panel2');
    el.classList.remove('open');
    $('#scrim').classList.remove('open');
    document.body.classList.remove('p2');
    clearTimeout(closeTimer);
    closeTimer = setTimeout(() => { if (!state.panel) { el.hidden = true; el.innerHTML = ''; el.dataset.kind = ''; } }, mobileMQ.matches ? 450 : 0);
    if (silent) return;
    if (!mobileMQ.matches && state.res && state.res.cards) fitToResults();
    if (was.kind === 'detail') {
      const c = $('#card-' + was.idx);
      const b = c && (c.matches('[data-act="open"]') ? c : c.querySelector('[data-act="open"]'));
      if (b) b.focus({preventScroll: true});
    }
  }
  function renderPanel() {
    const p = state.panel;
    if (!p) return;
    const el = $('#panel2');
    const closeBtn = '<button type="button" class="circle sm" data-act="close" aria-label="' + esc(t('close_btn')) + '">' + icon('close') + '</button>';
    const back = '<button type="button" class="l" data-act="close">' + icon('back') + esc(t('back_plan')) + '</button>';
    let nav = '', body = '';
    if (p.kind === 'detail') {
      const card = state.res && state.res.cards && state.res.cards[p.idx];
      if (!card) { closePanel(true); return; }
      nav = navHTML(back, card.name, closeBtn);
      body = detailHTML(card, p.idx);
    } else if (p.kind === 'trace') {
      nav = navHTML(back, '', closeBtn);
      body = traceHTML();
    } else if (p.kind === 'settings') {
      nav = navHTML('', t('settings'), '<button type="button" class="textbtn done" data-act="close">' + esc(t('done_btn')) + '</button>');
      body = settingsHTML();
    } else if (p.kind === 'about') {
      nav = navHTML('<button type="button" class="l" data-act="settings">' + icon('back') + esc(t('settings')) + '</button>', '', closeBtn);
      body = '<div class="ttl" style="padding:0 4px"><h1 class="big">' + esc(t('about_title')) + '</h1></div>' + aboutHTML();
    } else if (p.kind === 'from') {
      if (el.dataset.kind === 'from' && $('#from-q')) { renderFromResults(); renderMsgs(); return; }   // keep the field (and the keyboard) alive
      nav = navHTML('', t('origin_title'), '<button type="button" class="textbtn" data-act="close">' + esc(t('cancel')) + '</button>');
      body = fromHTML();
    }
    const key = p.kind + (p.idx != null ? p.idx : '');
    const prev = el.querySelector('.sheet-body');
    const top = prev && el.dataset.kind === key ? prev.scrollTop : 0;
    el.innerHTML = nav + '<div class="sheet-body">' + body + '</div>';
    el.dataset.kind = key;
    el.querySelector('.sheet-body').scrollTop = top;
    if (p.kind === 'settings') { syncSimUI(); renderMsgs(); }
    if (p.kind === 'from') { renderFromResults(); renderMsgs(); }
  }

  function detailHTML(card, i) {
    const r = state.res;
    const v = cardView().byIdx[i] || {};
    const walk = localWalk(card);
    const exp = r.texts[card.poi_id];
    const cc = cardCat(card);
    const acc = accessInfo(card);
    const ev = eventInfo(card);
    const hp = hoursParts(card.hours);
    const full = Math.floor((Number(card.rating) || 0) + 0.25);   // 4,5 → four gold stars and a grey one: never round a rating up
    const stars = Array.from({length: 5}, (_, k) => icon('starf', k < full ? '' : 'dim')).join('');
    const route = dirLink(card), geo = geoLink(card);
    const sources = card.source_label || (card.sources || []).map(srcName).join(', ');
    let h = '<div class="place-h"><h1>' + esc(card.name) + '</h1><div class="c">' + esc([cc.catText].concat(cc.tags).filter(Boolean).join(' · ')) +
      (v.alt ? ' <span class="cap">' + icon('swap') + esc(t('alt_title')) + '</span>' : '') + (ev ? ' <span class="cap p">' + icon('calendar') + esc(t('event')) + '</span>' : '') + '</div>' +
      (card.rating ? '<div class="stars"><i>' + stars + '</i><span><b style="color:var(--label)">' + esc(fmtNum(card.rating, 1)) + '</b> · ' + esc(reviewsText(card.reviews || 0)) + '</span></div>' : '') +
      '</div>' + statusWarn(card);
    h += '<div class="acts">' + (route ? '<a class="act prom" href="' + esc(route) + '" target="_blank" rel="noopener">' + icon('dirs') + esc(t('route_btn')) + '</a>' : '') +
      '<button type="button" class="act" data-act="map" data-idx="' + i + '">' + icon('pin') + esc(t('on_map_btn')) + '</button>' +
      (geo ? '<a class="act" href="' + esc(geo) + '" target="_blank" rel="noopener">' + icon('map') + esc(t('in_2gis')) + '</a>' : '') + '</div>';
    const rows = [];
    if (card.kind !== 'event') {
      rows.push('<div class="row top"><span class="icn">' + icon(hp.level === 'open_area' ? 'tree' : 'clock') + '</span><span class="t">' + hoursLine(card.hours) +
        (['high', 'medium', 'low'].includes(hp.level) ? '<span class="s">' + esc(cap1(t('lvl_' + hp.level))) + '</span>' : '') + '</span><span></span></div>');
    }
    rows.push('<div class="row"><span class="icn">' + icon('walk') + '</span><span class="t">' + esc(t('card_walk', {min: walk.min, dist: fmtDist(walk.m)})) + '</span><span></span></div>');
    if (card.visit_min) rows.push('<div class="row"><span class="icn">' + icon('hourglass') + '</span><span class="t">' + esc(cap1(t('visit', {n: card.visit_min}))) + '</span><span></span></div>');
    if (acc) rows.push('<div class="row"><span class="icn">' + icon('wheelchair') + '</span><span class="t">' + esc(t(acc.key)) + (acc.src ? '<span class="s">' + esc(t('per_src', {src: acc.src})) + '</span>' : '') + '</span><span></span></div>');
    if (ev) {
      if (ev.title) rows.push('<div class="row"><span class="icn">' + icon('calendar') + '</span><span class="t"><b>' + esc(ev.title) + '</b></span><span></span></div>');
      if (ev.when || ev.venue) rows.push('<div class="row"><span class="icn">' + icon('clock') + '</span><span class="t">' + esc([ev.when, ev.venue].filter(Boolean).join(' · ')) + '</span><span></span></div>');
      if (ev.price) rows.push('<div class="row"><span class="icn">' + icon('check') + '</span><span class="t">' + esc(t('price')) + ': ' + esc(ev.price) + '</span><span></span></div>');
      if (ev.url) rows.push('<a class="row" href="' + esc(ev.url) + '" target="_blank" rel="noopener"><span class="icn">' + icon('external') + '</span><span class="t" style="color:var(--tint-text)">' + esc(t('event_more')) + '</span><span class="chev">' + icon('chevR') + '</span></a>');
    }
    h += '<div class="list">' + rows.join('') + '</div>';
    if (card.taxi || card.transit) h += '<section class="sec"><div class="sec-h"><h3>' + esc(t('far_how')) + '</h3></div>' + movesHTML(card) + '</section>';
    let why;
    if (exp) why = '<div class="list"><div class="para">' + expBodyHTML(exp, false) + (cc.tags.length ? '<div class="caps" style="margin-top:10px">' + cc.tags.map((x) => '<span class="cap">' + esc(x) + '</span>').join('') + '</div>' : '') + '</div></div>' + provHTML(exp);
    else if (r.done || r.errorView) why = expMissing();
    else why = '<div class="list"><div class="para">' + expPending() + '</div></div>';
    h += '<section class="sec"><div class="sec-h"><h3>' + esc(t('why')) + '</h3></div>' + why + '</section>';
    if (card.description) {
      h += '<div class="list"><div class="para"><p class="desc">' + esc(card.description) + '</p>' +
        (card.description_ai ? '<p class="prov" style="padding:0;margin-top:8px">' + icon('sparkle') + '<span>' + esc(t('desc_ai')) + '</span></p>' : '') + '</div></div>';
    }
    h += scoreHTML(card);
    h += fbHTML(card);
    h += '<p class="note">' + esc(t('sources', {list: sources})) + '</p>';
    return h;
  }

  function settingsHTML() {
    const sw = (id, on, label) => '<button type="button" class="sw" role="switch" id="' + id + '" aria-checked="' + String(!!on) + '" aria-label="' + esc(label) + '"></button>';
    const chev = '<span class="chev">' + icon('chevR') + '</span>';
    return '<section class="sec"><div class="sec-h"><h3>' + esc(t('language')) + '</h3></div><div class="list">' +
      [['ru', 'Русский'], ['kk', 'Қазақша'], ['en', 'English']].map(([k, n]) => '<button type="button" class="row noicon" data-lang="' + k + '" lang="' + k + '" aria-pressed="' + String(state.lang === k) + '"><span class="t">' + n + '</span>' +
        (state.lang === k ? '<span class="check">' + icon('check') + '</span>' : '<span></span>') + '</button>').join('') + '</div></section>' +
      '<section class="sec"><div class="sec-h"><h3>' + esc(t('appearance')) + '</h3></div><div class="seg" role="group" aria-label="' + esc(t('appearance')) + '">' +
        ['auto', 'light', 'dark'].map((k) => '<button type="button" data-theme-opt="' + k + '" aria-pressed="' + String(state.theme === k) + '">' + esc(t('theme_' + k)) + '</button>').join('') + '</div></section>' +
      '<section class="sec"><div class="sec-h"><h3>' + esc(t('ai_section')) + '</h3></div><div class="list tiles"><div class="row">' + tile('cpu', 'var(--gray)') +
        '<span class="t">' + esc(t('noai_label')) + '</span>' + sw('sw-noai', state.noAi, t('noai_label')) + '</div></div>' +
        '<p class="sec-f">' + esc(t('noai_info')) + '</p></section>' +
      '<section class="sec"><div class="sec-h"><h3>' + esc(t('sim_title')) + '</h3></div><div class="list tiles">' +
        '<div class="row">' + tile('sliders', 'var(--blue)') + '<span class="t">' + esc(t('sim_enable')) + '</span>' + sw('sim-on', state.sim.on, t('sim_enable')) + '</div>' +
        '<div class="row stack"><span class="s" style="margin:0">' + esc(t('sim_presets')) + '</span><div class="caps">' +
          ['frost', 'warm', 'night'].map((p) => '<button type="button" class="cap" data-preset="' + p + '">' + esc(t('preset_' + p)) + '</button>').join('') + '</div></div>' +
        '<div class="sim-grid"><label class="field"><span>' + esc(t('sim_time')) + '</span><input type="datetime-local" id="sim-dt"></label>' +
          '<label class="field"><span>' + esc(t('sim_temp')) + ' <output id="sim-temp-out"></output></span><input type="range" id="sim-temp" min="-30" max="30" step="1"></label>' +
          '<label class="field"><span>' + esc(t('sim_wind')) + ' <output id="sim-wind-out"></output></span><input type="range" id="sim-wind" min="0" max="80" step="1"></label></div>' +
        '<div class="row">' + tile('snow', 'var(--teal)') + '<span class="t">' + esc(t('sim_precip')) + '</span>' + sw('sim-precip', state.sim.precip, t('sim_precip')) + '</div></div>' +
        '<p class="sec-f">' + esc(state.mock ? t('sim_note_mock') : t('sim_note')) + '</p></section>' +
      '<section class="sec"><div class="sec-h"><h3>' + esc(t('privacy_title')) + '</h3></div><div class="list tiles">' +
        '<div class="row">' + tile('lock', 'var(--blue)') + '<span class="t">' + esc(t('server_sees')) + '<span class="s">' + esc(t('server_sees_val')) +
          (state.cell ? ' · <code>' + esc(state.cell) + '</code>' : '') + '</span></span><span></span></div>' +
        '<div class="row">' + tile('clock', 'var(--indigo)') + '<span class="t">' + esc(t('session_row')) + '</span><span class="tr">' + esc(t('session_val')) + '</span></div>' +
        '<button type="button" class="row danger" data-act="delete">' + tile('trash', 'var(--red)') + '<span class="t">' + esc(t('delete')) + '</span><span></span></button></div>' +
        '<div class="form-msg" id="delete-msg" role="status"></div>' +
        '<p class="sec-f">' + esc(t('privacy_foot')) + '</p></section>' +
      '<section class="sec"><div class="sec-h"><h3>' + esc(t('data_section')) + '</h3></div><div class="list tiles">' +
        '<button type="button" class="row" data-act="about">' + tile('globe', 'var(--teal)') + '<span class="t">' + esc(t('about_sources')) + '</span>' + chev + '</button>' +
        '<a class="row" href="https://dev.2gis.ru" target="_blank" rel="noopener">' + tile('map', 'var(--tint)') + '<span class="t">' + esc(t('powered_2gis')) + '</span><span class="chev">' + icon('external') + '</span></a></div>' +
        '<p class="sec-f">' + esc(t('credits')) + '</p></section>';
  }

  function aboutHTML() {
    const secs = t('about_sections');
    const rows = t('src_rows');
    return secs.map((s) => '<section class="sec"><div class="sec-h"><h3>' + esc(s.h) + '</h3></div><div class="list"><div class="para"><ul class="ul" style="margin:0;color:var(--label)">' +
      s.items.map((i) => '<li>' + esc(i) + '</li>').join('') + '</ul></div></div></section>').join('') +
      '<section class="sec"><div class="sec-h"><h3>' + esc(t('about_sources')) + '</h3></div><div class="list">' +
      rows.map((r) => '<div class="row noicon src-row"><span class="t"><span>' + esc(r[0]) + '</span><span class="lic">' + esc(r[1]) + '</span><span class="use">' + esc(r[2]) + '</span></span><span></span></div>').join('') +
      '</div><p class="sec-f">' + esc(t('about_map')) + '</p></section>';
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
    if (!state.panel || state.panel.kind !== 'detail') return;
    const c = state.res && state.res.cards && state.res.cards[state.panel.idx];
    if (c && c.poi_id === poiId) renderPanel();
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
    state.where = null;
    state.chips = {time: null, kids: false, wheel: false, group: 1, interests: []};
    state.noAi = false;
    state.sim.on = false;
    state.res = null;
    state.feedback = {};
    state.theme = 'auto';
    state.fromQuery = '';
    state.fromItems = null;
    $('#q').value = '';
    ['#ctx', '#constraints', '#route', '#cards', '#error', '#trace', '#done'].forEach((s) => { $(s).innerHTML = ''; });
    $('#mock-banner').hidden = !state.mock;
    setStatus('');
    setBusy(false);
    renderMapResults(false);
    updateOriginOnMap(false);
    renderOrigin();
    syncChipsUI();
    applyTheme();
    showView('form');
    state.formMsgKey = null;
    state.originMsgKey = null;
    state.deleteMsgKey = {key: key, tone: 'ok'};
    renderPanel();
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
  }

  /* ------------------------------------------------------------ phone sheet: detents + drag (desktop: a fixed panel) */
  const DETENTS = {small: 168, medium: 0.52, large: 0.9};
  function detentPx(d) {
    const v = DETENTS[d] || DETENTS.medium;
    return v > 1 ? v : Math.round(window.innerHeight * v);
  }
  function setDetent(d) {
    state.detent = DETENTS[d] ? d : 'medium';
    const sh = $('#sheet');
    sh.dataset.detent = state.detent;
    sh.style.setProperty('--sheet-h', detentPx(state.detent) + 'px');
    document.body.classList.toggle('det-large', mobileMQ.matches && state.detent === 'large');
    $('#attrib').style.bottom = mobileMQ.matches ? (detentPx(state.detent) + 8) + 'px' : '';
  }
  function initSheetDrag() {
    const sh = $('#sheet'), g = $('#grab');
    const order = ['small', 'medium', 'large'];
    let y0 = 0, h0 = 0, moved = false;
    const settle = (d) => {
      setDetent(d);
      setTimeout(() => { declutter(); if (state.res && state.res.cards && d !== 'large') fitToResults(); }, 420);
    };
    g.addEventListener('click', () => {
      if (moved) { moved = false; return; }
      settle(order[(order.indexOf(state.detent) + 1) % order.length]);
    });
    g.addEventListener('pointerdown', (e) => {
      y0 = e.clientY;
      h0 = sh.getBoundingClientRect().height;
      moved = false;
      g.setPointerCapture(e.pointerId);
      sh.classList.add('dragging');
    });
    g.addEventListener('pointermove', (e) => {
      if (!g.hasPointerCapture(e.pointerId)) return;
      const dy = e.clientY - y0;
      if (Math.abs(dy) > 4) moved = true;
      sh.style.setProperty('--sheet-h', Math.max(120, Math.min(window.innerHeight - 40, h0 - dy)) + 'px');
    });
    const up = () => {
      sh.classList.remove('dragging');
      if (!moved) return;
      const h = sh.getBoundingClientRect().height;
      settle(order.reduce((a, d) => (Math.abs(detentPx(d) - h) < Math.abs(detentPx(a) - h) ? d : a), 'medium'));
    };
    g.addEventListener('pointerup', up);
    g.addEventListener('pointercancel', up);
    g.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowUp') { e.preventDefault(); settle(order[Math.min(2, order.indexOf(state.detent) + 1)]); }
      if (e.key === 'ArrowDown') { e.preventDefault(); settle(order[Math.max(0, order.indexOf(state.detent) - 1)]); }
    });
  }

  /* ------------------------------------------------------------ events */
  function bindEvents() {
    const q = $('#q');
    const startPick = () => {
      state.pickMode = true;
      closePanel(true);
      renderMapChip();
      showMapNote('map_pick_hint', 6000);
      if (mobileMQ.matches) setDetent('small');
    };
    $('#req-form').addEventListener('submit', (e) => { e.preventDefault(); submit(); });
    q.addEventListener('input', () => { $('#q-clear').hidden = !q.value; });
    q.addEventListener('focus', () => {
      if (state.view === 'results') showView('form');
      if (mobileMQ.matches && state.detent !== 'large') setDetent('large');
    });
    $('#q-clear').addEventListener('click', () => { q.value = ''; $('#q-clear').hidden = true; showView('form'); q.focus(); });
    $('#btn-settings').addEventListener('click', () => openPanel({kind: 'settings'}));
    $('#btn-theme').addEventListener('click', () => {
      state.theme = themeNow() === 'dark' ? 'light' : 'dark';
      LS.set(KEYS.theme, state.theme);
      applyTheme();
      if (state.panel && state.panel.kind === 'settings') renderPanel();
    });
    ['#btn-locate-top', '#btn-locate-desk', '#btn-locate'].forEach((s) => $(s).addEventListener('click', locate));
    $('#btn-zin').addEventListener('click', () => { if (map) map.zoomIn(); });
    $('#btn-zout').addEventListener('click', () => { if (map) map.zoomOut(); });
    $('#btn-mappick').addEventListener('click', startPick);
    $('#btn-find').addEventListener('click', () => openPanel({kind: 'from'}));
    $('#where-line').addEventListener('click', (e) => { if (e.target.closest('[data-act="from"]')) openPanel({kind: 'from'}); });

    // the request
    $('#quick').addEventListener('click', (e) => {
      const b = e.target.closest('[data-quick]');
      if (!b) return;
      const item = t('quick')[+b.dataset.quick];
      q.value = item[2];
      $('#q-clear').hidden = false;
      submit();
    });
    $('#time-seg').addEventListener('click', (e) => {
      const b = e.target.closest('[data-time]');
      if (!b) return;
      const m = +b.dataset.time;
      state.chips.time = state.chips.time === m ? null : m;
      syncChipsUI(); saveChips();
    });
    $('#group-seg').addEventListener('click', (e) => {
      const b = e.target.closest('[data-group]');
      if (!b) return;
      state.chips.group = +b.dataset.group;
      syncChipsUI(); saveChips();
    });
    $('#interest-caps').addEventListener('click', (e) => {
      const b = e.target.closest('[data-interest]');
      if (!b) return;
      const id = b.dataset.interest;
      const list = state.chips.interests;
      state.chips.interests = list.includes(id) ? list.filter((x) => x !== id) : list.concat(id);
      syncChipsUI(); saveChips();
    });
    $('#sw-kids').addEventListener('click', () => { state.chips.kids = !state.chips.kids; syncChipsUI(); saveChips(); });
    $('#sw-wheel').addEventListener('click', () => { state.chips.wheel = !state.chips.wheel; syncChipsUI(); saveChips(); });
    $('#btn-go').addEventListener('click', () => submit());
    $('#btn-edit').addEventListener('click', () => { showView('form'); if (mobileMQ.matches) setDetent('large'); });

    // the answer: open a place, show it on the map, how I chose, retry
    $('#view-results').addEventListener('click', (e) => {
      const act = e.target.closest('[data-act]');
      if (!act) return;
      const a = act.dataset.act;
      const holder = act.closest('[data-idx]');
      const i = holder ? +holder.dataset.idx : -1;
      if (a === 'retry') { submit(); return; }
      if (a === 'example') {
        setOrigin({lat: SAMPLE_ORIGIN.lat, lon: SAMPLE_ORIGIN.lon, source: 'sample'}, {fly: false});
        submit({mock: true});
        return;
      }
      if (a === 'trace') { openPanel({kind: 'trace'}); return; }
      if (a === 'open' && i >= 0) { selectCard(i, {open: true, fly: true}); return; }
      if (a === 'map' && i >= 0) { selectCard(i, {fly: true}); if (mobileMQ.matches) setDetent('small'); }
    });
    $('#cards').addEventListener('mouseover', (e) => {
      const el = e.target.closest('[data-idx]');
      poiMarkers.forEach((m, j) => { if (m) m.getElement().classList.toggle('is-hover', !!el && +el.dataset.idx === j); });
    });
    $('#cards').addEventListener('mouseleave', () => { poiMarkers.forEach((m) => { if (m) m.getElement().classList.remove('is-hover'); }); });

    // the second column
    const p2 = $('#panel2');
    const simTouch = () => { state.sim.on = true; if (!state.sim.dt) state.sim.dt = nowLocalInput(); syncSimUI(); };
    p2.addEventListener('click', (e) => {
      const lang = e.target.closest('[data-lang]');
      if (lang) { state.lang = lang.dataset.lang; LS.set(KEYS.lang, state.lang); applyLang(); return; }
      const th = e.target.closest('[data-theme-opt]');
      if (th) { state.theme = th.dataset.themeOpt; LS.set(KEYS.theme, state.theme); applyTheme(); renderPanel(); return; }
      const fb = e.target.closest('[data-fb]');
      if (fb) { sendFeedback(fb.dataset.poi, fb.dataset.fb); return; }
      const sug = e.target.closest('[data-sug]');
      if (sug && state.fromItems) {
        const it = state.fromItems[+sug.dataset.sug];
        if (it) setOrigin({lat: it.lat, lon: it.lon, source: 'search', label: it.name});
        return;
      }
      const lm = e.target.closest('[data-lm]');
      if (lm) {
        const l = state.landmarks[+lm.dataset.lm];
        if (l) setOrigin({lat: l.lat, lon: l.lon, source: 'landmark', lm: +lm.dataset.lm});
        return;
      }
      const pre = e.target.closest('[data-preset]');
      if (pre) { applyPreset(pre.dataset.preset); return; }
      if (e.target.closest('#from-clear')) { const inp = $('#from-q'); inp.value = ''; onFromInput(inp); inp.focus(); return; }
      const sw = e.target.closest('.sw');
      if (sw) {
        if (sw.id === 'sw-noai') { state.noAi = !state.noAi; saveChips(); syncChipsUI(); }
        if (sw.id === 'sim-on') { state.sim.on = !state.sim.on; if (state.sim.on && !state.sim.dt) state.sim.dt = nowLocalInput(); syncSimUI(); }
        if (sw.id === 'sim-precip') { state.sim.precip = !state.sim.precip; simTouch(); }
        return;
      }
      const act = e.target.closest('[data-act]');
      if (!act) return;
      const a = act.dataset.act;
      if (a === 'close') closePanel();
      else if (a === 'settings') openPanel({kind: 'settings'});
      else if (a === 'about') openPanel({kind: 'about'});
      else if (a === 'delete') deleteMyData();
      else if (a === 'locate') locate();
      else if (a === 'pick') startPick();
      else if (a === 'map') {
        const i = +act.dataset.idx;
        closePanel(true);
        selectCard(i, {fly: true});
        if (mobileMQ.matches) setDetent('small');
      }
    });
    p2.addEventListener('input', (e) => {
      const el = e.target;
      if (el.id === 'from-q') onFromInput(el);
      else if (el.id === 'sim-temp') { state.sim.temp = +el.value; simTouch(); }
      else if (el.id === 'sim-wind') { state.sim.wind = +el.value; simTouch(); }
    });
    p2.addEventListener('change', (e) => { if (e.target.id === 'sim-dt') { state.sim.dt = e.target.value; simTouch(); } });
    p2.addEventListener('keydown', (e) => {
      if (e.target.id === 'from-q' && e.key === 'Enter') {
        e.preventDefault();
        const it = state.fromItems && state.fromItems[0];
        if (it) setOrigin({lat: it.lat, lon: it.lon, source: 'search', label: it.name});
      }
    });
    $('#scrim').addEventListener('click', () => closePanel());
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && state.panel) closePanel(); });

    const onScheme = () => { if (state.theme === 'auto') applyTheme(); };
    if (darkMQ.addEventListener) darkMQ.addEventListener('change', onScheme);
    window.addEventListener('resize', () => { setDetent(state.detent); declutter(); });
    initSheetDrag();
    setInterval(() => placeholderTick(false), 3800);
  }

  /* ------------------------------------------------------------ init */
  function setupMock() {
    $('#mock-banner').hidden = false;
    setOrigin({lat: SAMPLE_ORIGIN.lat, lon: SAMPLE_ORIGIN.lon, source: 'sample'}, {fly: false});
    // pre-fill the request that matches the recorded answer (not saved to storage)
    $('#q').value = t('sample_text');
    $('#q-clear').hidden = false;
    state.chips.time = 120;
    state.chips.kids = true;
    state.chips.group = 2;
    syncChipsUI();
  }

  function init() {
    state.lang = initialLang();
    const th = LS.get(KEYS.theme);
    state.theme = th === 'light' || th === 'dark' ? th : 'auto';
    if (params.get('theme') === 'dark' || params.get('theme') === 'light') state.theme = params.get('theme');
    loadChips();
    state.sessionId = LS.get(KEYS.session);
    state.sim.dt = nowLocalInput();
    applyTheme();
    buildStaticUI();
    applyLang();
    bindEvents();
    showView('form');
    setDetent(params.get('detent') || 'medium');
    initMap();
    if (state.mock) setupMock();
    else loadLandmarks();
    if (map && state.origin) {
      map.once('load', () => {
        updateOriginOnMap(false);
        if (state.res && state.res.cards) fitToResults();   // the answer may arrive before the map has loaded
        else map.easeTo({center: [state.origin.lon, state.origin.lat], zoom: mobileMQ.matches ? 14.5 : 15, offset: freeOffset(), duration: 0});
      });
    }
    // ?panel=settings|about|from opens a page; ?mock=1&auto=1 plays the recorded answer (demos and screenshots)
    const pp = params.get('panel');
    if (pp && ['settings', 'about', 'from'].includes(pp)) openPanel({kind: pp});
    if (state.mock && params.get('auto') === '1') setTimeout(() => submit(), 400);
  }

  init();
})();
