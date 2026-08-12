// 127.0.0.1, never "localhost".
//
// "localhost" resolves to BOTH ::1 and 127.0.0.1 on macOS, and the IPv6 record
// comes first. The brain's Socket.IO server binds 0.0.0.0, which is IPv4 only, so
// every attempt that picked ::1 was refused outright.
//
// This is why the interface was intermittently inert: with the socket down there
// are no state updates (so it sits on the sleep screen), direct_action never
// reaches Python (so "turn on" does nothing), and the voice layer never boots (so
// the microphone is dead) — while the window itself looks perfectly normal.
const socket = io('http://127.0.0.1:5050');

const transcriptBox = document.getElementById('transcript-box');
const chatInput = document.getElementById('chat-input');
const orb = document.querySelector('.friday-orb');
const coreContainer = document.getElementById('core-container');
const statusLabel = document.getElementById('status-label');
const overrideResponse = document.getElementById('override-response');
const cardWhiteboard = document.getElementById('widget-layer');
const sleepScreen = document.getElementById('sleep-screen');
const sleepName = document.querySelector('.sleep-name');
const sleepTime = document.querySelector('.sleep-time');
const sleepDate = document.querySelector('.sleep-date');
const sleepPrompt = document.querySelector('.sleep-prompt');
const interfaceParams = new URLSearchParams(window.location.search || '');
const interfaceMode = interfaceParams.get('mode') || 'hud';
const workshopRole = interfaceParams.get('role') || '';
const isWorkshopWindow = interfaceMode === 'workshop';
const WORKSPACE_CANVAS_HEIGHT_MULTIPLIER = 3.5;
const LARGE_WORKSPACE_WIDGET_TYPES = new Set(['map', 'news', 'settings']);
const WORKSTATION_RESERVED_TOP_PADDING = 16;
const WORKSTATION_TOP_CONTROL_SELECTORS = [
    '#system-bar',
    '#top-notch',
    '#title-bar',
    '.notification-launcher-shell'
];

document.body.dataset.interfaceMode = interfaceMode;

if (isWorkshopWindow) {
    document.body.classList.add('mode-workshop', 'workshop-mode-active');
}

if (workshopRole) {
    document.body.dataset.workshopRole = workshopRole;
}

try {
    applyTheme(localStorage.getItem('friday-theme') || 'blue', false);
} catch (_) {
    applyTheme('blue', false);
}

let latestState = null;
let cardUrlCache = new Map();
let shutdownInProgress = false;
let notificationHistory = [];
let notificationCenterOpen = false;
let calendarPagePayload = null;
let calendarPageSelectedDate = null;
let widgetLauncherOpen = false;
let workshopModeActive = false;
let workshopDisplaysReported = false;
let workshopElectronOpen = false;
let workshopElectronOpening = false;
let workshopAnalyticsPayload = null;
let workshopAnalyticsTimer = null;
// Workshop shell UI state. Local to the window: which sidebar sections are open,
// whether the inspector rail is collapsed, and whether a Silent Operator turn is
// still awaiting FRIDAY's reply. None of it is authoritative state, so it is kept
// out of the backend snapshot and restored from localStorage instead.
let workshopChatPending = false;
let workshopChatPendingTimer = null;
const WORKSHOP_SIDEBAR_SECTIONS = ['chats', 'memory', 'tasks', 'files', 'context'];
let workshopSidebarOpen = null;
let workshopInspectorCollapsed = null;
let workshopComposerDraft = '';
let workshopActiveChatId = '';
// Finder widgets can exist on independent workspace surfaces. Keep their UI,
// request, and preference state attached to the originating widget body rather
// than sharing transient state across windows/workspaces.
const virtualFinderControllers = new WeakMap();
let virtualFinderLastActiveBody = null;

// Retained only for the separate Workshop virtual-desktop icon surface.
let virtualFinderCurrentPath = '';
let virtualFinderBackStack = [];
let virtualDesktopPayload = null;
let musicStateRefreshTimer = null;
let orbSpeakingTimeout = null;
let showcaseActive = false;
let showcaseCompleting = false;
let showcaseTimers = [];
let showcaseStepIndex = 0;
let settingsPayload = null;
let tasksPayload = null;
let tasksPageFilter = 'all';

const ORB_STATE_CLASSES = [
    'orb-idle',
    'orb-listening',
    'orb-user-speaking',
    'orb-thinking',
    'orb-tooling',
    'orb-speaking',
    'orb-error',
    'orb-sleep'
];

// ==========================================
// AUDIO-REACTIVE ORB
// ==========================================
// Amplitude arrives from the backend at roughly 25 updates per second for both
// the microphone and FRIDAY playback. It is smoothed here with an attack/release
// pair and published as a CSS variable, so every theme drives its own colours
// from the same normalised 0..1 signal.

const ORB_LEVEL_ATTACK = 0.55;
const ORB_LEVEL_RELEASE = 0.22;
const ORB_LEVEL_IDLE_TIMEOUT_MS = 420;

let orbAudioLevel = 0;
let orbAudioDecayTimer = null;
let orbAudioSource = '';

function applyOrbAudioLevel(level) {
    if (!coreContainer) {
        return;
    }

    const clamped = Math.max(0, Math.min(Number(level) || 0, 1));
    coreContainer.style.setProperty('--orb-audio-level', clamped.toFixed(3));
    // Scale and glow stay in a restrained band so the orb pulses with speech
    // instead of jumping around.
    coreContainer.style.setProperty('--orb-audio-scale', (1 + clamped * 0.14).toFixed(4));
    coreContainer.style.setProperty('--orb-audio-glow', (0.45 + clamped * 0.55).toFixed(3));
}

function setOrbAudioReactive(active) {
    if (!coreContainer) {
        return;
    }

    coreContainer.classList.toggle('audio-reactive', Boolean(active));

    if (!active) {
        applyOrbAudioLevel(0);
        coreContainer.removeAttribute('data-audio-source');
    } else if (orbAudioSource) {
        coreContainer.dataset.audioSource = orbAudioSource;
    }
}

function scheduleOrbAudioDecay() {
    window.clearTimeout(orbAudioDecayTimer);
    orbAudioDecayTimer = window.setTimeout(() => {
        // The stream stopped without an explicit end event: settle back to rest
        // rather than holding the last frame.
        orbAudioLevel = 0;
        applyOrbAudioLevel(0);
        setOrbAudioReactive(false);
    }, ORB_LEVEL_IDLE_TIMEOUT_MS);
}

// Driven by the incoming amplitude stream itself rather than requestAnimationFrame,
// which browsers throttle or suspend whenever the HUD window is not visible.
// Inter-frame smoothness comes from the short CSS transitions on the orb.
function pushOrbAudioLevel(level, source) {
    if (!coreContainer) {
        return;
    }

    orbAudioSource = String(source || '');

    const target = Math.max(0, Math.min(Number(level) || 0, 1));
    const coefficient = target > orbAudioLevel ? ORB_LEVEL_ATTACK : ORB_LEVEL_RELEASE;

    orbAudioLevel += (target - orbAudioLevel) * coefficient;

    if (orbAudioLevel < 0.004) {
        orbAudioLevel = 0;
    }

    setOrbAudioReactive(orbAudioLevel > 0 || target > 0);
    applyOrbAudioLevel(orbAudioLevel);
    scheduleOrbAudioDecay();
}

function resetOrbAudioLevel() {
    window.clearTimeout(orbAudioDecayTimer);
    orbAudioLevel = 0;
    setOrbAudioReactive(false);
}

const SHOWCASE_EXIT_PHRASE = 'friday exit showcase mode';
const SHOWCASE_EXIT_ALIASES = new Set([SHOWCASE_EXIT_PHRASE, 'jarvis exit showcase mode']);
const SHOWCASE_STEPS = [
    {
        name: 'activation',
        label: 'Activation',
        duration: 2500,
        title: 'FRIDAY MK1',
        subtitle: 'SHOWCASE MODE',
        detail: 'CORE SYSTEMS PRESENTATION',
        bullets: ['Input lock engaged', 'Demo channel isolated', 'Core presentation online']
    },
    {
        name: 'system_health',
        label: 'System Health',
        duration: 5000,
        title: 'SYSTEM HEALTH',
        subtitle: 'Diagnostics layer',
        detail: 'Voice online, calendar connected, music control online, telemetry active.',
        bullets: ['Fish Audio voice ready', 'Calendar bridge standing by', 'Proactive monitor available', 'System telemetry active']
    },
    {
        name: 'calendar',
        label: 'Calendar',
        duration: 5000,
        title: 'CALENDAR',
        subtitle: 'Google Calendar synchronized',
        detail: 'Operations schedule, today, tomorrow, upcoming priorities.',
        bullets: ['Today summary available', 'Tomorrow scan ready', 'Priority reminders indexed']
    },
    {
        name: 'notes',
        label: 'Notes',
        duration: 4000,
        title: 'NOTES',
        subtitle: 'Local memory',
        detail: 'Private notes stay local and ready.',
        bullets: ['Local notes ready', 'Memory panel available', 'No note edits performed']
    },
    {
        name: 'music',
        label: 'Music Control',
        duration: 4000,
        title: 'MUSIC CONTROL',
        subtitle: 'Apple Music bridge',
        detail: 'Transport controls ready. No playback changes performed.',
        bullets: ['Play/pause state aware', 'Next and back ready', 'Volume untouched']
    },
    {
        name: 'intel',
        label: 'Intel Briefing',
        duration: 5000,
        title: 'INTEL BRIEFING',
        subtitle: 'Cached briefing surface',
        detail: 'US News, Kosovo, Markets, Active Conflicts.',
        bullets: ['US News', 'Kosovo', 'Markets', 'Active Conflicts']
    },
    {
        name: 'files',
        label: 'Virtual Finder',
        duration: 5000,
        title: 'VIRTUAL FINDER',
        subtitle: 'Safe virtual workspace',
        detail: 'Rooted in Data/Virtual_Finder. No real desktop access.',
        bullets: ['Private Folder', 'Operations', 'Projects', 'School', 'FRIDAY Logs']
    },
    {
        name: 'workshop',
        label: 'Workshop Readiness',
        duration: 4000,
        title: 'WORKSHOP MODE',
        subtitle: 'Multi-workstation readiness',
        detail: 'Silent Operator, secondary workstation, multi-monitor layer.',
        bullets: ['Workshop Mode available', 'Silent Operator ready', 'Secondary workstation ready']
    },
    {
        name: 'final_layout',
        label: 'Final Layout',
        duration: 3000,
        title: 'FINAL LAYOUT',
        subtitle: 'Clean workstation',
        detail: 'Core systems staged for handoff.',
        bullets: ['Diagnostics', 'Calendar', 'Notes', 'Music', 'Intel', 'Virtual Finder']
    }
];

window.workshopWidgetState = window.workshopWidgetState || {
    main: [],
    secondary: []
};

const defaultWidgetLayouts = {
    cards: { left: 380, top: 58, width: 1120, height: 520 },
    transcript: { left: 28, top: 570, width: 620, height: 380 },
    override: { left: 1040, top: 720, width: 520, height: 250 }
};

function storageKey(id) {
    return `friday-widget-layout-${id}`;
}

function cardStorageKey(id, cardOrElement = null) {
    const largeWidget = cardOrElement?.classList?.contains?.('large-workspace-widget')
        || isLargeWorkspaceWidget(cardOrElement);

    if (largeWidget) {
        return `friday-card-layout-${cardWorkspaceScope(cardOrElement)}-${id}`;
    }

    return `friday-card-layout-${id}`;
}

function cardWorkspaceScope(cardOrElement) {
    const workshopWorkspace = cardOrElement?.dataset?.workshopWorkspace
        || (cardOrElement && typeof cardOrElement === 'object' ? cardOrElement.workspace : '');

    if (workshopWorkspace === 'secondary') {
        return 'workshop-secondary';
    }

    if (workshopWorkspace === 'main') {
        return 'workshop-main';
    }

    return 'workstation';
}

function cardStateStorageKey(id, cardOrElement = null) {
    return `friday-card-state-${cardWorkspaceScope(cardOrElement)}-${id}`;
}

function isLargeWorkspaceWidget(cardOrType) {
    if (cardOrType && typeof cardOrType === 'object') {
        return LARGE_WORKSPACE_WIDGET_TYPES.has(String(cardOrType.type || '').toLowerCase());
    }

    return LARGE_WORKSPACE_WIDGET_TYPES.has(String(cardOrType || '').toLowerCase());
}

function workspaceCardClassName(card) {
    const type = String(card?.type || 'web').toLowerCase();
    const largeClasses = isLargeWorkspaceWidget(card)
        ? ' embedded-widget large-workspace-widget'
        : '';
    return `hud-card native-widget widget-type-${type}${largeClasses}`;
}

function safeJsonParse(value) {
    try {
        return JSON.parse(value);
    } catch (_) {
        return null;
    }
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function appendSystemLine(text, color = 'var(--accent-secondary)') {
    const p = document.createElement('p');
    p.className = 'friday-text system-line';
    p.style.color = color;
    p.innerText = `> SYSTEM: ${text}`;

    transcriptBox.appendChild(p);
    transcriptBox.scrollTop = transcriptBox.scrollHeight;
}

function applyHudMode(state) {
    const mode = String(state.hud_mode || 'ACTIVE').toUpperCase();
    const orbPosition = String(state.orb_position || 'CENTER').toUpperCase();

    document.body.dataset.mode = mode.toLowerCase();
    document.body.classList.toggle('mode-sleep', mode === 'SLEEP' || mode === 'OFFLINE');
    document.body.classList.toggle('mode-active', mode === 'ACTIVE');
    applyTheme(state.theme || state.settings?.theme || 'blue');
    applyInterfaceSettings(state.settings || {});
    document.body.dataset.orbPosition = orbPosition.toLowerCase();

    if (mode === 'SLEEP' || mode === 'OFFLINE') {
        sleepScreen.classList.remove('hidden');
        statusLabel.style.visibility = 'hidden';

        document.querySelectorAll('.desktop-widget').forEach((widget) => {
            widget.classList.add('hidden-by-mode');
        });

    } else {
        sleepScreen.classList.add('hidden');
        statusLabel.style.visibility = 'visible';

        document.querySelectorAll('.desktop-widget').forEach((widget) => {
            if (widget.dataset.widgetId === 'override') {
                widget.classList.remove('hidden-by-mode');
            } else {
                widget.classList.add('hidden-by-mode');
            }
        });

    }

    coreContainer.classList.toggle('orb-docked', orbPosition === 'DOCKED_BOTTOM_RIGHT');
    coreContainer.classList.toggle('orb-centered', orbPosition !== 'DOCKED_BOTTOM_RIGHT');
}

function applyInterfaceSettings(settings = {}) {
    const safe = settings && typeof settings === 'object' ? settings : {};
    const showHotbar = safe.show_hotbar !== false;

    document.body.classList.toggle('hotbar-hidden', !showHotbar);
    document.body.dataset.notificationsEnabled = safe.show_notifications === false ? 'false' : 'true';
}

function normalizeThemeName(value) {
    const raw = String(value || 'blue').trim().toLowerCase().replace(/_/g, '-');
    const compact = raw.replace(/\s+/g, ' ');
    const aliases = {
        'friday blue': 'blue',
        'jarvis blue': 'blue', // legacy name for the same theme
        'dark blue': 'blue',
        'blue': 'blue',
        'tactical green': 'green',
        'green': 'green',
        'white mode': 'white',
        'white': 'white',
        'light': 'white',
        'midnight': 'midnight',
        'graphite': 'graphite',
        'graphite mode': 'graphite',
        'charcoal': 'graphite',
        'steel': 'graphite',
        'monochrome': 'graphite',
        'high contrast': 'high-contrast',
        'high-contrast': 'high-contrast'
    };

    return aliases[compact] || (['blue', 'green', 'white', 'midnight', 'graphite', 'high-contrast'].includes(compact) ? compact : 'blue');
}

function applyTheme(value, persist = true) {
    const theme = normalizeThemeName(value);
    document.body.dataset.theme = theme;
    document.body.classList.remove('theme-blue', 'theme-green', 'theme-white', 'theme-midnight', 'theme-graphite', 'theme-high-contrast');
    document.body.classList.add(`theme-${theme}`);

    if (persist) {
        try {
            localStorage.setItem('friday-theme', theme);
        } catch (_) {
            // Ignore storage failures; backend state remains authoritative.
        }
    }
}

function renderSleepScreen(sleepState = {}) {
    sleepName.innerText = sleepState.name || 'FRIDAY';
    sleepTime.innerText = sleepState.time || new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    sleepDate.innerText = sleepState.date || new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
    sleepPrompt.innerText = sleepState.prompt || 'Passive workstation layer ready.';
}

function applyWidgetLayouts() {
    document.querySelectorAll('.desktop-widget').forEach((widget) => {
        const id = widget.dataset.widgetId;
        const saved = safeJsonParse(localStorage.getItem(storageKey(id)));
        const layout = saved || defaultWidgetLayouts[id];

        if (!layout) {
            return;
        }

        widget.style.left = `${layout.left}px`;
        widget.style.top = `${layout.top}px`;
        widget.style.width = `${layout.width}px`;
        widget.style.height = `${layout.height}px`;
    });
}

function saveWidgetLayout(widget) {
    const id = widget.dataset.widgetId;

    if (!id) {
        return;
    }

    localStorage.setItem(storageKey(id), JSON.stringify({
        left: widget.offsetLeft,
        top: widget.offsetTop,
        width: widget.offsetWidth,
        height: widget.offsetHeight
    }));
}

// ==========================================
// WINDOW MANAGER (z-order + focus)
// ==========================================
// One authoritative stacking authority for every floating surface.
//
// The previous scheme was `String(Date.now()).slice(-6)`, which is NOT
// monotonic: the truncated millisecond counter wraps roughly every 16.7
// minutes, so a freshly opened widget could receive a *lower* value than one
// opened moments earlier and render behind it. That is the "Music opens behind
// Map" bug. Counters here only ever increase, and are rebased before they can
// grow large enough to collide with the fixed chrome layers.

const WINDOW_Z_BASE = 100;          // floating widgets live above the workspace
const WINDOW_Z_CEILING = 8000;      // stays clear of overlays/menus (>= 9000)
const windowStacks = new Map();     // workspace key -> current top z

function windowStackKey(element) {
    // Workshop windows, Workstation, and each workshop workspace stack
    // independently so raising a widget in one surface never reorders another.
    const workspace = element?.closest?.('[data-workspace]');

    if (workspace && workspace.dataset.workspace) {
        return `workspace:${workspace.dataset.workspace}`;
    }

    if (element?.closest?.('.workshop-mode')) {
        return 'workshop';
    }

    return 'workstation';
}

function rebaseWindowStack(key, elements) {
    // Renumber from the base so long sessions never drift into the chrome range.
    const ordered = elements
        .slice()
        .sort((a, b) => (Number(a.style.zIndex) || 0) - (Number(b.style.zIndex) || 0));

    ordered.forEach((el, index) => {
        el.style.zIndex = String(WINDOW_Z_BASE + index);
    });

    windowStacks.set(key, WINDOW_Z_BASE + ordered.length);
    return WINDOW_Z_BASE + ordered.length;
}

function peersInStack(key) {
    return Array.from(document.querySelectorAll('.hud-card, .friday-widget'))
        .filter((el) => windowStackKey(el) === key);
}

function nextWindowZ(element) {
    const key = windowStackKey(element);
    let top = windowStacks.get(key);

    if (!Number.isFinite(top)) {
        // Adopt whatever is already on screen so we never jump below it.
        const peak = peersInStack(key)
            .reduce((max, el) => Math.max(max, Number(el.style.zIndex) || 0), WINDOW_Z_BASE);
        top = Math.max(WINDOW_Z_BASE, peak);
    }

    if (top >= WINDOW_Z_CEILING) {
        top = rebaseWindowStack(key, peersInStack(key));
    }

    top += 1;
    windowStacks.set(key, top);
    return top;
}

function focusWindow(element) {
    if (!element || !element.style) {
        return;
    }

    const key = windowStackKey(element);

    // Already frontmost: nothing to do, and no pointless style write.
    const peers = peersInStack(key);
    const currentTop = peers.reduce(
        (max, el) => Math.max(max, Number(el.style.zIndex) || 0), 0);

    if (!(Number(element.style.zIndex) === currentTop && element.classList.contains('window-focused'))) {
        element.style.zIndex = String(nextWindowZ(element));
    }

    peers.forEach((el) => el.classList.toggle('window-focused', el === element));
}

// Any pointer press inside a floating surface raises it, which covers clicking
// body content as well as the header, drag, and resize paths.
document.addEventListener('pointerdown', (event) => {
    const surface = event.target?.closest?.('.hud-card, .friday-widget');

    if (surface) {
        focusWindow(surface);
    }
}, true);

// ==========================================
// RESPONSIVE WIDGET SIZING
// ==========================================
// Widgets adapt to their own rendered box, not the viewport, so the same widget
// is dense when small and generous when large. A single ResizeObserver drives
// every surface; there is no polling.
//
// Thresholds carry a hysteresis margin so a widget parked exactly on a boundary
// cannot oscillate between two modes while the user drags the resize handle.

const WIDGET_SIZE_STEPS = [
    { name: 'compact', maxWidth: 340, maxHeight: 250 },
    { name: 'medium', maxWidth: 560, maxHeight: 430 }
];
const WIDGET_SIZE_HYSTERESIS = 18;

function classifyWidgetSize(width, height, previous) {
    for (const step of WIDGET_SIZE_STEPS) {
        // Growing out of a mode needs to clear the boundary by the margin;
        // shrinking into it happens at the boundary itself.
        const grew = previous === step.name ? WIDGET_SIZE_HYSTERESIS : 0;

        if (width <= step.maxWidth + grew || height <= step.maxHeight + grew) {
            return step.name;
        }
    }

    return 'expanded';
}

function applyWidgetSizeClass(element) {
    if (!element || !element.isConnected) {
        return;
    }

    const width = element.clientWidth;
    const height = element.clientHeight;

    if (!width || !height) {
        return;
    }

    const previous = element.dataset.size || '';
    const next = classifyWidgetSize(width, height, previous);

    if (next !== previous) {
        element.dataset.size = next;
    }
}

const widgetSizeObserver = typeof ResizeObserver === 'function'
    ? new ResizeObserver((entries) => {
        for (const entry of entries) {
            applyWidgetSizeClass(entry.target);
        }
    })
    : null;

function observeWidgetSize(element) {
    if (!element || element.dataset.sizeObserved === 'true') {
        return;
    }

    element.dataset.sizeObserved = 'true';
    applyWidgetSizeClass(element);

    if (widgetSizeObserver) {
        widgetSizeObserver.observe(element);
    }
}

function observeAllWidgetSizes(root = document) {
    root.querySelectorAll('.hud-card, .friday-widget').forEach(observeWidgetSize);
}

// ==========================================
// WINDOW INTERACTION (drag + resize)
// ==========================================
// One engine for every floating surface, replacing the per-widget handlers that
// used to exist.
//
// Those attached `window.addEventListener('mousemove')` and `'mouseup')` PER
// WIDGET and never removed them, so every widget ever opened left two live
// global listeners behind. After a busy session dozens of dead handlers ran on
// every single mouse move, which is a large part of why dragging felt heavy.
// There is now exactly one listener of each kind for the whole document.
//
// Three other things the old path got wrong, all of which show up as "not like a
// real window manager":
//   - mouse events, so a fast drag that outran the cursor dropped the window;
//     pointer capture keeps the gesture glued to the surface.
//   - a style write per mousemove, so the browser re-laid-out faster than it
//     could paint; writes are now batched to one per animation frame.
//   - a single south-east grip; real windows resize from any edge or corner.

const RESIZE_DIRECTIONS = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];

// Floors are per widget type: a music player is unusable at the width a clock is
// fine at. Anything not listed falls back to the generic floor.
// Keyed by BOTH the card type and the card id, because widgets are identified by
// either depending on which factory built them ("tasks" vs "tasks_widget",
// "weather" vs "weather_current"). Looking at only one of the two silently fell
// back to the generic floor for half the widgets.
const WINDOW_MIN_SIZES = {
    music: { width: 300, height: 132 },
    weather: { width: 300, height: 200 },
    weather_current: { width: 300, height: 200 },
    tasks: { width: 300, height: 220 },
    tasks_widget: { width: 300, height: 220 },
    notes: { width: 280, height: 200 },
    sticky_notes: { width: 280, height: 200 },
    notifications: { width: 320, height: 240 },
    notification_center: { width: 320, height: 240 },
    system_health: { width: 320, height: 240 },
    virtual_finder: { width: 420, height: 300 },
    files: { width: 420, height: 300 },
    settings: { width: 460, height: 340 },
    calendar: { width: 360, height: 280 },
    news: { width: 420, height: 320 },
    news_briefing: { width: 420, height: 320 },
    intel: { width: 420, height: 320 },
    map: { width: 480, height: 360 },
    map_fullscreen: { width: 480, height: 360 }
};
const WINDOW_MIN_FALLBACK = { width: 260, height: 170 };
const WINDOW_MIN_LARGE = { width: 620, height: 420 };
const WINDOW_EDGE_INSET = 12;

let activeWindowGesture = null;
let windowGestureFrame = 0;

function windowMinSizeFor(element) {
    const candidates = [
        element?.dataset?.cardType,
        element?.dataset?.cardId,
        element?.dataset?.widgetId
    ];

    for (const candidate of candidates) {
        const match = WINDOW_MIN_SIZES[String(candidate || '').toLowerCase()];

        if (match) {
            return match;
        }
    }

    return element.classList.contains('large-workspace-widget')
        ? WINDOW_MIN_LARGE
        : WINDOW_MIN_FALLBACK;
}

function windowGestureLayer(element) {
    return element?.closest('.workshop-widget-layer, #widget-layer') || null;
}

function windowSizeLimits(element) {
    const layer = windowGestureLayer(element);
    // A hidden or not-yet-laid-out surface measures zero, and so does the viewport
    // behind it. Without a floor here every limit collapses to the minimum and the
    // window becomes unresizable rather than merely mis-measured.
    const layerWidth = layer?.clientWidth || window.innerWidth || 1280;
    const layerHeight = layer?.clientHeight || window.innerHeight || 800;
    const floor = windowMinSizeFor(element);

    // A floor taller than the workspace would make the window unresizable, so it
    // yields to the available room rather than the other way round.
    const minWidth = Math.min(floor.width, Math.max(200, layerWidth - WINDOW_EDGE_INSET * 2));
    const minHeight = Math.min(floor.height, Math.max(140, layerHeight - WINDOW_EDGE_INSET * 2));

    return {
        minWidth,
        minHeight,
        maxWidth: Math.max(minWidth, layerWidth - WINDOW_EDGE_INSET),
        maxHeight: Math.max(minHeight, layerHeight - WINDOW_EDGE_INSET),
        layer,
        layerWidth,
        layerHeight
    };
}

function windowReservedTop(element) {
    const layer = windowGestureLayer(element);

    if (!layer || typeof getWorkstationReservedTop !== 'function') {
        return 0;
    }

    try {
        return getWorkstationReservedTop(layer, element) || 0;
    } catch (error) {
        return 0;
    }
}

function ensureWindowResizeHandles(element) {
    if (!element || element.dataset.wmHandles === 'true') {
        return;
    }

    element.dataset.wmHandles = 'true';

    // The old corner grip stays as the visual affordance people already aim for;
    // CSS makes it decorative and the real hit target is the `se` handle below.
    const fragment = document.createDocumentFragment();

    RESIZE_DIRECTIONS.forEach((direction) => {
        const handle = document.createElement('div');
        handle.className = `window-resize-handle window-resize-${direction}`;
        handle.dataset.resizeDir = direction;
        fragment.appendChild(handle);
    });

    element.appendChild(fragment);
}

// ══════════════════════════════════════════════════════════════════════════
// MOTION
// ══════════════════════════════════════════════════════════════════════════
// One way for anything to leave the screen.
//
// The close BUTTON already animated, but nothing else did: a spoken "close
// music", "clear the workspace", a Workshop workspace being torn down, and the
// ordinary state reconcile all reached straight for element.remove(). Those are
// most of the ways a window actually closes, which is why the interface felt
// abrupt — the one path that was polished was the one path Jon used least.
//
// Everything now routes through here.

/** How long to wait for a close transition before removing the node anyway. */
const MOTION_CLOSE_FALLBACK_MS = 400;

/**
 * Play an element out, then remove it.
 *
 * Safe to call twice on the same element: the second call is ignored rather
 * than restarting the animation or removing the node early.
 *
 * `data-closing` is set SYNCHRONOUSLY, and every lookup that reconciles windows
 * against backend state skips elements carrying it. Without that, a window
 * closing over ~180ms would still match `[data-card-id="..."]` and a state
 * update arriving mid-flight would adopt the dying node as though it were live —
 * a widget that reopens into a corpse fading to nothing.
 */
function closeElementWithMotion(element, options = {}) {
    if (!element || element.dataset.closing === 'true') {
        return;
    }

    element.dataset.closing = 'true';

    const finish = () => {
        if (!element.isConnected) {
            return;
        }

        element.remove();
        options.onRemoved?.();
    };

    // Nothing to play out for an element that was never on screen.
    if (!element.isConnected || !element.offsetParent && element.offsetHeight === 0) {
        finish();
        return;
    }

    element.classList.add(options.className || 'widget-closing');

    // transitionend is the honest signal, but it does not fire for an element in
    // a hidden tab, a display:none ancestor, or with motion reduced to nothing.
    // The timer guarantees removal regardless, so a window can never be left
    // half-faded on screen.
    let done = false;

    const settle = () => {
        if (done) {
            return;
        }

        done = true;
        element.removeEventListener('transitionend', onEnd);
        finish();
    };

    const onEnd = (event) => {
        // Only the element's own opacity — not a child's, and not the transform
        // that finishes alongside it.
        if (event.target === element && event.propertyName === 'opacity') {
            settle();
        }
    };

    element.addEventListener('transitionend', onEnd);
    window.setTimeout(settle, options.fallbackMs || MOTION_CLOSE_FALLBACK_MS);
}

/** Play out every matching element inside `root`. */
function closeAllWithMotion(root, selector) {
    if (!root) {
        return;
    }

    root.querySelectorAll(selector).forEach((element) => closeElementWithMotion(element));
}

function beginWindowGesture(element, event, mode, direction = '') {
    if (activeWindowGesture || event.button !== 0) {
        return;
    }

    activeWindowGesture = {
        element,
        mode,
        direction,
        startX: event.clientX,
        startY: event.clientY,
        lastX: event.clientX,
        lastY: event.clientY,
        startLeft: element.offsetLeft,
        startTop: element.offsetTop,
        startWidth: element.offsetWidth,
        startHeight: element.offsetHeight,
        limits: windowSizeLimits(element),
        reservedTop: windowReservedTop(element),
        pointerId: event.pointerId,
        captureTarget: event.currentTarget
    };

    try {
        event.currentTarget.setPointerCapture(event.pointerId);
    } catch (error) {
        // Capture is an optimisation, not a requirement; document listeners still fire.
    }

    element.classList.add(mode === 'drag' ? 'dragging' : 'resizing');
    document.body.classList.add('window-gesture-active');
    focusWindow(element);
    event.preventDefault();
}

function applyWindowGesture() {
    windowGestureFrame = 0;

    const gesture = activeWindowGesture;

    if (!gesture || !gesture.element.isConnected) {
        return;
    }

    const { element, limits } = gesture;
    const deltaX = gesture.lastX - gesture.startX;
    const deltaY = gesture.lastY - gesture.startY;

    if (gesture.mode === 'drag') {
        const position = clampWorkspaceCardPosition(
            element,
            gesture.startLeft + deltaX,
            gesture.startTop + deltaY
        );
        element.style.left = `${position.left}px`;
        element.style.top = `${position.top}px`;
        return;
    }

    const direction = gesture.direction;
    let width = gesture.startWidth;
    let height = gesture.startHeight;
    let left = gesture.startLeft;
    let top = gesture.startTop;

    if (direction.includes('e')) {
        width = gesture.startWidth + deltaX;
    }

    if (direction.includes('s')) {
        height = gesture.startHeight + deltaY;
    }

    if (direction.includes('w')) {
        width = gesture.startWidth - deltaX;
    }

    if (direction.includes('n')) {
        height = gesture.startHeight - deltaY;
    }

    width = Math.min(limits.maxWidth, Math.max(limits.minWidth, width));
    height = Math.min(limits.maxHeight, Math.max(limits.minHeight, height));

    // Dragging a west or north edge moves the origin, so the opposite edge has to
    // stay pinned — otherwise the window slides while it is being resized.
    if (direction.includes('w')) {
        left = gesture.startLeft + (gesture.startWidth - width);
    }

    if (direction.includes('n')) {
        top = gesture.startTop + (gesture.startHeight - height);
    }

    if (left < 0) {
        width += left;
        left = 0;
    }

    if (top < gesture.reservedTop) {
        height -= gesture.reservedTop - top;
        top = gesture.reservedTop;
    }

    element.style.left = `${Math.round(left)}px`;
    element.style.top = `${Math.round(top)}px`;
    element.style.width = `${Math.round(Math.max(limits.minWidth, width))}px`;
    element.style.height = `${Math.round(Math.max(limits.minHeight, height))}px`;
}

function persistWindowLayout(element) {
    if (element.classList.contains('hud-card')) {
        saveCardLayout(element);
        return;
    }

    if (element.classList.contains('desktop-widget')) {
        saveWidgetLayout(element);
    }
}

document.addEventListener('pointermove', (event) => {
    if (!activeWindowGesture || event.pointerId !== activeWindowGesture.pointerId) {
        return;
    }

    activeWindowGesture.lastX = event.clientX;
    activeWindowGesture.lastY = event.clientY;

    // Coalesced to one write per frame: the browser cannot paint faster than
    // this, and writing per event only forces extra layout passes.
    if (!windowGestureFrame) {
        windowGestureFrame = requestAnimationFrame(applyWindowGesture);
    }
});

function endWindowGesture(event) {
    const gesture = activeWindowGesture;

    if (!gesture || (event && event.pointerId !== gesture.pointerId)) {
        return;
    }

    if (windowGestureFrame) {
        cancelAnimationFrame(windowGestureFrame);
        windowGestureFrame = 0;
        applyWindowGesture();
    }

    activeWindowGesture = null;

    try {
        gesture.captureTarget?.releasePointerCapture?.(gesture.pointerId);
    } catch (error) {
        // Capture already lost; nothing to release.
    }

    const { element } = gesture;
    element.classList.remove('dragging', 'resizing');
    document.body.classList.remove('window-gesture-active');

    if (!element.isConnected) {
        return;
    }

    const position = clampWorkspaceCardPosition(element, element.offsetLeft, element.offsetTop);
    element.style.left = `${position.left}px`;
    element.style.top = `${position.top}px`;
    persistWindowLayout(element);
}

document.addEventListener('pointerup', endWindowGesture);
document.addEventListener('pointercancel', endWindowGesture);

/**
 * Double-clicking a title bar zooms the window to fill the workspace, and again
 * restores it — the same gesture macOS uses.
 */
function toggleWindowZoom(element) {
    const limits = windowSizeLimits(element);

    if (element.dataset.wmRestore) {
        const restore = safeJsonParse(element.dataset.wmRestore);
        delete element.dataset.wmRestore;
        element.classList.remove('window-zoomed');

        if (restore) {
            element.style.left = `${restore.left}px`;
            element.style.top = `${restore.top}px`;
            element.style.width = `${restore.width}px`;
            element.style.height = `${restore.height}px`;
        }

        persistWindowLayout(element);
        return;
    }

    element.dataset.wmRestore = JSON.stringify({
        left: element.offsetLeft,
        top: element.offsetTop,
        width: element.offsetWidth,
        height: element.offsetHeight
    });

    const reserved = windowReservedTop(element);
    element.classList.add('window-zoomed');
    element.style.left = `${WINDOW_EDGE_INSET / 2}px`;
    element.style.top = `${reserved + WINDOW_EDGE_INSET / 2}px`;
    element.style.width = `${limits.layerWidth - WINDOW_EDGE_INSET}px`;
    element.style.height = `${limits.layerHeight - reserved - WINDOW_EDGE_INSET}px`;
    persistWindowLayout(element);
}

/**
 * Idempotent: safe to call again on an element that is already wired.
 */
function enableWindowInteractions(element) {
    if (!element || element.dataset.wmReady === 'true') {
        return;
    }

    element.dataset.wmReady = 'true';
    ensureWindowResizeHandles(element);

    const header = element.querySelector('.hud-card-header, .widget-header');

    if (header) {
        header.addEventListener('pointerdown', (event) => {
            // Controls inside the title bar are not drag surfaces.
            if (event.target.closest('button, input, select, textarea, a, .hud-card-close')) {
                return;
            }

            beginWindowGesture(element, event, 'drag');
        });

        header.addEventListener('dblclick', (event) => {
            if (event.target.closest('button, input, select, textarea, a')) {
                return;
            }

            toggleWindowZoom(element);
        });
    }

    element.querySelectorAll('.window-resize-handle').forEach((handle) => {
        handle.addEventListener('pointerdown', (event) => {
            event.stopPropagation();
            beginWindowGesture(element, event, 'resize', handle.dataset.resizeDir || 'se');
        });
    });
}

function makeWidgetMovable(widget) {
    enableWindowInteractions(widget);
}

function makeWidgetResizable(widget) {
    enableWindowInteractions(widget);
}

function initializeWidgets() {
    applyWidgetLayouts();

    document.querySelectorAll('.desktop-widget').forEach(enableWindowInteractions);
}

function renderOrb(status) {
    const normalized = String(status || 'IDLE').toUpperCase();
    const stateName = ['IDLE', 'LISTENING', 'USER_SPEAKING', 'THINKING', 'TOOLING', 'SPEAKING', 'ERROR', 'SLEEP'].includes(normalized)
        ? normalized.toLowerCase().replace('_', '-')
        : 'idle';

    coreContainer.dataset.status = normalized;
    statusLabel.innerText = normalized === 'USER_SPEAKING' ? 'LISTENING' : normalized;

    orb.classList.remove(
        ...ORB_STATE_CLASSES,
        'status-idle',
        'status-listening',
        'status-thinking',
        'status-tooling',
        'status-speaking',
        'status-error',
        'speaking-flare'
    );

    if (normalized === 'IDLE' && orbSpeakingTimeout) {
        window.clearTimeout(orbSpeakingTimeout);
        orbSpeakingTimeout = null;
    }

    // Amplitude only drives the orb while somebody is actually talking.
    if (!['LISTENING', 'USER_SPEAKING', 'SPEAKING'].includes(normalized)) {
        resetOrbAudioLevel();
    }

    orb.classList.add(`orb-${stateName}`);
}

function normalizeShowcaseText(value) {
    return String(value || '')
        .toLowerCase()
        .replace(/[,.!?]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

function isShowcaseExitText(value) {
    return SHOWCASE_EXIT_ALIASES.has(normalizeShowcaseText(value));
}

function clearShowcaseTimers() {
    showcaseTimers.forEach((timer) => window.clearTimeout(timer));
    showcaseTimers = [];
}

function setShowcaseTimer(callback, delayMs) {
    const timer = window.setTimeout(() => {
        showcaseTimers = showcaseTimers.filter((item) => item !== timer);
        callback();
    }, delayMs);
    showcaseTimers.push(timer);
    return timer;
}

function ensureShowcaseOverlay() {
    let overlay = document.querySelector('.showcase-overlay');

    if (overlay) {
        return overlay;
    }

    overlay = document.createElement('section');
    overlay.className = 'showcase-overlay';
    overlay.setAttribute('aria-label', 'FRIDAY Showcase Mode');
    overlay.innerHTML = `
        <div class="showcase-scanline"></div>
        <div class="showcase-grid"></div>
        <div class="showcase-frame showcase-step active">
            <header class="showcase-header">
                <div>
                    <div class="showcase-title">FRIDAY MK1</div>
                    <div class="showcase-subtitle">SHOWCASE MODE</div>
                </div>
                <div class="showcase-step-label">ACTIVATION</div>
            </header>
            <main class="showcase-body">
                <section class="showcase-primary">
                    <span class="showcase-kicker">CORE SYSTEMS PRESENTATION</span>
                    <h2>FRIDAY MK1</h2>
                    <p>Initializing controlled demo sequence.</p>
                </section>
                <aside class="showcase-panel">
                    <div class="showcase-panel-title">SYSTEMS</div>
                    <div class="showcase-bullets"></div>
                </aside>
            </main>
            <footer class="showcase-footer">
                <div class="showcase-progress"><span></span></div>
                <div class="showcase-status">Input locked. Say FRIDAY exit showcase mode to abort.</div>
            </footer>
        </div>
    `;
    document.body.appendChild(overlay);
    return overlay;
}

function renderShowcaseStep(step, index) {
    const overlay = ensureShowcaseOverlay();
    const stepElement = overlay.querySelector('.showcase-step');
    const progress = Math.min(100, Math.max(0, ((index + 1) / SHOWCASE_STEPS.length) * 100));

    const updateContent = () => {
        overlay.dataset.step = step.name;
        overlay.querySelector('.showcase-title').textContent = step.title;
        overlay.querySelector('.showcase-subtitle').textContent = step.subtitle;
        overlay.querySelector('.showcase-step-label').textContent = step.label.toUpperCase();
        overlay.querySelector('.showcase-kicker').textContent = step.detail;
        overlay.querySelector('.showcase-primary h2').textContent = step.title;
        overlay.querySelector('.showcase-primary p').textContent = step.detail;
        overlay.querySelector('.showcase-bullets').innerHTML = step.bullets.map((item) => `
            <div class="showcase-highlight"><span></span><strong>${escapeHtml(item)}</strong></div>
        `).join('');
        overlay.querySelector('.showcase-progress span').style.width = `${progress}%`;
        overlay.classList.add('active');
        overlay.classList.remove('exiting');
        document.body.classList.add('showcase-dim');

        stepElement?.classList.remove('exiting', 'active', 'showcase-final');
        stepElement?.classList.add('entering');
        window.requestAnimationFrame(() => {
            stepElement?.classList.remove('entering');
            stepElement?.classList.add('active');
        });
    };

    if (overlay.dataset.step && overlay.dataset.step !== step.name && stepElement) {
        stepElement.classList.remove('entering', 'active');
        stepElement.classList.add('exiting');
        setShowcaseTimer(updateContent, 280);
        return;
    }

    updateContent();
}

function runShowcaseStep(index = 0) {
    if (!showcaseActive) {
        return;
    }

    if (index >= SHOWCASE_STEPS.length) {
        completeShowcaseMode();
        return;
    }

    showcaseStepIndex = index;
    const step = SHOWCASE_STEPS[index];
    renderShowcaseStep(step, index);
    socket.emit('showcase_step', { step: step.name, index });

    setShowcaseTimer(() => {
        runShowcaseStep(index + 1);
    }, step.duration);
}

function startShowcaseMode(options = {}) {
    if (showcaseActive && !options.restart) {
        return;
    }

    clearShowcaseTimers();
    showcaseActive = true;
    showcaseCompleting = false;
    showcaseStepIndex = 0;
    ensureShowcaseOverlay();
    runShowcaseStep(0);
    const watchdogMs = SHOWCASE_STEPS.reduce((total, step) => total + step.duration, 0) + 10000;

    setShowcaseTimer(() => {
        abortShowcaseMode({ notify: true });
    }, watchdogMs);
}

function renderShowcaseFinal() {
    const overlay = ensureShowcaseOverlay();
    const stepElement = overlay.querySelector('.showcase-step');

    overlay.dataset.step = 'complete';
    overlay.classList.add('active');
    overlay.classList.remove('exiting');
    overlay.querySelector('.showcase-title').textContent = 'CORE SYSTEMS';
    overlay.querySelector('.showcase-subtitle').textContent = 'DEMONSTRATED';
    overlay.querySelector('.showcase-step-label').textContent = 'COMPLETE';
    overlay.querySelector('.showcase-kicker').textContent = 'Core systems demonstrated.';
    overlay.querySelector('.showcase-primary h2').textContent = 'CORE SYSTEMS DEMONSTRATED';
    overlay.querySelector('.showcase-primary p').textContent = 'Returning to Sleep Screen.';
    overlay.querySelector('.showcase-bullets').innerHTML = [
        'Diagnostics complete',
        'Calendar ready',
        'Local memory ready',
        'Music controls ready',
        'Input lock releasing'
    ].map((item) => `
        <div class="showcase-highlight showcase-pulse"><span></span><strong>${escapeHtml(item)}</strong></div>
    `).join('');
    overlay.querySelector('.showcase-progress span').style.width = '100%';
    stepElement?.classList.remove('entering', 'exiting');
    stepElement?.classList.add('active', 'showcase-final');
}

function returnToSleepAfterShowcase() {
    renderOrb('SLEEP');
}

function clearShowcaseVisuals(options = {}) {
    const overlay = document.querySelector('.showcase-overlay');
    document.body.classList.remove('showcase-dim');

    if (overlay) {
        if (options.immediate) {
            overlay.remove();
        } else {
            overlay.classList.add('exiting');
            overlay.classList.remove('active');
            setShowcaseTimer(() => {
                if (!showcaseActive) {
                    overlay.remove();
                }
            }, 1050);
        }
    }

    closeCalendarPage();
}

function completeShowcaseMode() {
    if (!showcaseActive || showcaseCompleting) {
        return;
    }

    clearShowcaseTimers();
    showcaseCompleting = true;
    renderShowcaseFinal();

    setShowcaseTimer(() => {
        const overlay = document.querySelector('.showcase-overlay');
        overlay?.classList.add('exiting');
        overlay?.classList.remove('active');
    }, 2500);

    setShowcaseTimer(() => {
        showcaseActive = false;
        showcaseCompleting = false;
        clearShowcaseVisuals({ immediate: true });
        returnToSleepAfterShowcase();
        socket.emit('showcase_complete', { completed: true });
    }, 3500);
}

function abortShowcaseMode(options = {}) {
    clearShowcaseTimers();
    showcaseActive = false;
    showcaseCompleting = false;
    showcaseStepIndex = 0;
    clearShowcaseVisuals({ immediate: options.immediate !== false });
    returnToSleepAfterShowcase();

    if (options.notify) {
        socket.emit('showcase_aborted', { aborted: true });
    }
}

function stopShowcaseMode(options = {}) {
    if (options.completed && showcaseActive) {
        completeShowcaseMode();
        return;
    }

    abortShowcaseMode({ immediate: options.returnSleep !== false, notify: false });
}

function renderTranscript(messages) {
    if (!Array.isArray(messages)) {
        return;
    }

    transcriptBox.innerHTML = '';

    for (const item of messages) {
        const p = document.createElement('p');
        const source = String(item.source || 'system').replace(/[^a-zA-Z0-9_-]/g, '');
        const speaker = String(item.speaker || 'SYSTEM').toUpperCase();
        const text = item.text || '';
        const timestamp = item.timestamp ? ` [${item.timestamp}]` : '';

        p.className = `friday-text source-${source}`;
        p.innerText = `> ${speaker}: ${text}${timestamp}`;

        transcriptBox.appendChild(p);
    }

    transcriptBox.scrollTop = transcriptBox.scrollHeight;
}

function renderOverrideResponse(response) {
    if (!response || !response.text) {
        overrideResponse.className = 'override-response idle';
        overrideResponse.innerText = 'Override channel standing by.';
        return;
    }

    overrideResponse.className = 'override-response active';
    overrideResponse.innerText = response.text;
}

function applySavedCardLayout(card, element, largeIndex = 0) {
    const storedLayout = safeJsonParse(localStorage.getItem(cardStorageKey(card.id, card)));
    const saved = isLargeWorkspaceWidget(card) && storedLayout?.embeddedWidget !== true
        ? null
        : storedLayout;
    const viewport = document.getElementById('workstation-scroll');
    const viewportWidth = viewport?.clientWidth || window.innerWidth || 1280;
    const viewportHeight = viewport?.clientHeight || window.innerHeight || 720;
    const availableLargeWidth = Math.max(260, viewportWidth - 48);
    const availableLargeHeight = Math.max(260, viewportHeight - 72);
    const largeWidth = Math.min(availableLargeWidth, Math.max(Math.min(640, availableLargeWidth), Math.round(viewportWidth * 0.88)));
    const largeHeight = Math.min(availableLargeHeight, Math.max(Math.min(460, availableLargeHeight), Math.round(viewportHeight * 0.82)));
    const existingLargeBottom = Array.from(document.querySelectorAll('#widget-layer .hud-card.large-workspace-widget')).reduce((bottom, item) => (
        Math.max(bottom, item.offsetTop + item.offsetHeight)
    ), 0);
    const layout = saved || (isLargeWorkspaceWidget(card) ? {
        left: Math.max(24, Math.round((viewportWidth - largeWidth) / 2)),
        top: existingLargeBottom > 0
            ? existingLargeBottom + 56
            : 56 + Math.max(0, Number(largeIndex) || 0) * (largeHeight + 56),
        width: largeWidth,
        height: largeHeight
    } : {
        left: Number(card.x ?? 80),
        top: Number(card.y ?? 120),
        width: Number(card.width ?? 420),
        height: Number(card.height ?? 320)
    });

    const fallbackWidth = isLargeWorkspaceWidget(card) ? largeWidth : 420;
    const fallbackHeight = isLargeWorkspaceWidget(card) ? largeHeight : 320;
    const requestedWidth = Number(layout.width);
    const requestedHeight = Number(layout.height);
    const requestedLeft = Number(layout.left);
    const requestedTop = Number(layout.top);
    const maxWidth = Math.max(260, viewportWidth - 24);
    const maxHeight = Math.max(180, viewportHeight * WORKSPACE_CANVAS_HEIGHT_MULTIPLIER - 24);
    const minWidth = isLargeWorkspaceWidget(card) ? Math.min(680, maxWidth) : 260;
    const minHeight = isLargeWorkspaceWidget(card) ? Math.min(480, maxHeight) : 180;
    const width = Math.min(maxWidth, Math.max(minWidth, Number.isFinite(requestedWidth) ? requestedWidth : fallbackWidth));
    const height = Math.min(maxHeight, Math.max(minHeight, Number.isFinite(requestedHeight) ? requestedHeight : fallbackHeight));

    element.style.left = `${Number.isFinite(requestedLeft) ? requestedLeft : 24}px`;
    element.style.top = `${Number.isFinite(requestedTop) ? requestedTop : 56}px`;
    element.style.width = `${width}px`;
    element.style.height = `${height}px`;
}

function saveCardLayout(cardElement) {
    const id = cardElement.dataset.cardId;

    if (!id) {
        return;
    }

    localStorage.setItem(cardStorageKey(id, cardElement), JSON.stringify({
        left: cardElement.offsetLeft,
        top: cardElement.offsetTop,
        width: cardElement.offsetWidth,
        height: cardElement.offsetHeight,
        embeddedWidget: cardElement.classList.contains('large-workspace-widget')
    }));
}

function workspaceScrollStorageKey(workspace) {
    const normalized = String(workspace || 'workstation').trim().toLowerCase();
    const target = normalized === 'single' ? 'main' : normalized;
    return `friday-workspace-scroll-${target}`;
}

function sizeWorkspaceCanvas(viewport) {
    const canvas = viewport?.querySelector?.('.workspace-scroll-canvas');
    const viewportHeight = viewport?.clientHeight || 0;

    if (!canvas || !viewportHeight) {
        return;
    }

    canvas.style.height = `${Math.round(viewportHeight * WORKSPACE_CANVAS_HEIGHT_MULTIPLIER)}px`;
    canvas.style.setProperty('--workspace-viewport-height', `${viewportHeight}px`);
}

function initializeWorkspaceScroll(viewport, workspace = '') {
    if (!viewport) {
        return;
    }

    const target = String(workspace || viewport.dataset.workspaceScroll || 'workstation').toLowerCase();
    viewport.dataset.workspaceScroll = target;
    sizeWorkspaceCanvas(viewport);

    const storageKey = workspaceScrollStorageKey(target);

    if (viewport.dataset.workspaceScrollInitialized !== 'true') {
        viewport.dataset.workspaceScrollInitialized = 'true';
        viewport.addEventListener('scroll', () => {
            if (viewport._workspaceScrollSaveFrame) {
                return;
            }

            viewport._workspaceScrollSaveFrame = window.requestAnimationFrame(() => {
                viewport._workspaceScrollSaveFrame = null;
                localStorage.setItem(storageKey, String(Math.round(viewport.scrollTop)));
            });
        }, { passive: true });
    }

    if (!viewport.clientHeight || viewport.dataset.workspaceScrollRestored === 'true') {
        return;
    }

    viewport.dataset.workspaceScrollRestored = 'true';
    const savedValue = localStorage.getItem(storageKey);

    if (savedValue === null) {
        return;
    }

    window.requestAnimationFrame(() => {
        sizeWorkspaceCanvas(viewport);
        const savedTop = Number(savedValue);

        if (!Number.isFinite(savedTop)) {
            return;
        }

        const maxTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
        viewport.scrollTop = Math.min(maxTop, Math.max(0, savedTop));
    });
}

function getWorkstationReservedTop(
    layer = document.getElementById('widget-layer'),
    cardElement = null
) {
    if (isWorkshopWindow || !layer || layer.id !== 'widget-layer') {
        return 0;
    }

    const layerRect = layer.getBoundingClientRect();
    const viewport = document.getElementById('workstation-scroll');
    const viewportRect = viewport?.getBoundingClientRect();
    const cardRect = cardElement?.getBoundingClientRect?.();
    const cardIsAboveViewport = Boolean(
        viewportRect
        && cardRect
        && cardRect.bottom <= viewportRect.top
    );
    const workspaceOriginTop = cardIsAboveViewport && viewportRect
        ? viewportRect.top
        : layerRect.top;
    let lowestControlBottom = null;

    WORKSTATION_TOP_CONTROL_SELECTORS.forEach((selector) => {
        const control = document.querySelector(selector);

        if (!control) {
            return;
        }

        const style = window.getComputedStyle(control);
        const opacity = Number.parseFloat(style.opacity);

        if (
            style.display === 'none'
            || style.visibility === 'hidden'
            || (Number.isFinite(opacity) && opacity <= 0)
        ) {
            return;
        }

        const controlRect = control.getBoundingClientRect();

        if (!controlRect.width || !controlRect.height) {
            return;
        }

        lowestControlBottom = lowestControlBottom === null
            ? controlRect.bottom
            : Math.max(lowestControlBottom, controlRect.bottom);
    });

    if (lowestControlBottom === null) {
        return 0;
    }

    return Math.max(
        0,
        lowestControlBottom - workspaceOriginTop + WORKSTATION_RESERVED_TOP_PADDING
    );
}

function clampWorkspaceCardPosition(cardElement, requestedLeft, requestedTop) {
    const layer = cardElement?.closest('.workshop-widget-layer, #widget-layer');

    if (!layer) {
        return {
            left: Number(requestedLeft),
            top: Number(requestedTop),
            clamped: false
        };
    }

    const visibleEdge = 64;
    const layerRect = layer.getBoundingClientRect();
    const layerWidth = layerRect.width;
    const layerHeight = layerRect.height;
    const cardWidth = cardElement.offsetWidth;
    const cardHeight = cardElement.offsetHeight;

    if (!layerWidth || !layerHeight || !cardWidth || !cardHeight) {
        return {
            left: Number(requestedLeft),
            top: Number(requestedTop),
            clamped: false
        };
    }

    const sourceLeft = Number(requestedLeft);
    const sourceTop = Number(requestedTop);
    const safeLeft = Number.isFinite(sourceLeft) ? sourceLeft : 0;
    const safeTop = Number.isFinite(sourceTop) ? sourceTop : 0;
    const fitsWidth = cardWidth <= layerWidth;
    const fitsHeight = cardHeight <= layerHeight;
    const minLeft = fitsWidth ? 0 : visibleEdge - cardWidth;
    const maxLeft = fitsWidth ? layerWidth - cardWidth : layerWidth - visibleEdge;
    const minTop = getWorkstationReservedTop(layer, cardElement);
    const maxTop = fitsHeight ? layerHeight - cardHeight : Math.max(0, layerHeight - visibleEdge);
    const left = minLeft <= maxLeft
        ? Math.min(maxLeft, Math.max(minLeft, safeLeft))
        : Math.max(0, (layerWidth - cardWidth) / 2);
    const top = Math.min(maxTop, Math.max(minTop, safeTop));

    return {
        left,
        top,
        clamped: left !== sourceLeft || top !== sourceTop
    };
}

function reclampWorkspaceCards() {
    document.querySelectorAll('.workshop-widget-layer .hud-card, #widget-layer .hud-card').forEach((card) => {
        const position = clampWorkspaceCardPosition(card, card.offsetLeft, card.offsetTop);

        if (!position.clamped) {
            return;
        }

        card.style.left = `${position.left}px`;
        card.style.top = `${position.top}px`;
        saveCardLayout(card);
    });
}

function handleWorkspaceResize() {
    document.querySelectorAll('.workspace-scroll-viewport').forEach((viewport) => {
        sizeWorkspaceCanvas(viewport);
    });
    reclampWorkspaceCards();
}

window.addEventListener('resize', handleWorkspaceResize);

function inferCardTitle(url) {
    const value = String(url || '').toLowerCase();

    if (value.includes('youtube')) return 'YOUTUBE';
    if (value.includes('wikipedia')) return 'WIKIPEDIA';
    if (value.includes('weather') || value.includes('wttr.in')) return 'WEATHER';
    if (value.includes('news.google')) return 'GOOGLE_NEWS';
    if (value.includes('tbm=isch')) return 'VISUAL_RESULTS';
    if (value.includes('apnews')) return 'AP_NEWS';
    if (value.includes('reuters')) return 'REUTERS';
    if (value.includes('google')) return 'GOOGLE';

    return '';
}

function cardWorkshopWorkspace(card) {
    return String(card?.workspace || '').trim().toLowerCase();
}

function isWorkshopCardRecord(card) {
    return cardWorkshopWorkspace(card) !== '';
}

function workstationCards(cards) {
    return Array.isArray(cards) ? cards.filter((card) => !isWorkshopCardRecord(card)) : [];
}

function getWorkstationWidgetContainer() {
    return document.querySelector('#widget-layer');
}

function getWorkshopWidgetContainer() {
    return document.getElementById('workshop-widget-layer') || document.querySelector('.workshop-widget-layer');
}

function getActiveWidgetContainer() {
    const workshopContainer = getWorkshopWidgetContainer();

    if (isWorkshopSurfaceActive() && workshopContainer) {
        return workshopContainer;
    }

    return getWorkstationWidgetContainer();
}

function tagCardForWorkspace(cardElement, card) {
    const workspace = cardWorkshopWorkspace(card);

    cardElement.classList.toggle('workshop-card', Boolean(workspace));
    cardElement.classList.toggle('workstation-card', !workspace);
    cardElement.dataset.workspace = workspace ? 'workshop' : 'workstation';

    if (workspace) {
        cardElement.dataset.workshopWorkspace = workspace;
    } else {
        delete cardElement.dataset.workshopWorkspace;
    }
}

function createCardElement(card, largeIndex = 0) {
    const wrapper = document.createElement('div');
    wrapper.className = workspaceCardClassName(card);
    wrapper.dataset.cardId = card.id || '';
    wrapper.dataset.cardType = card.type || 'web';
    tagCardForWorkspace(wrapper, card);

    if (String(card.type || '').toLowerCase() === 'map') {
        wrapper.dataset.mapPayloadKey = `${card.data?.lat ?? ''}:${card.data?.lon ?? ''}:${card.data?.destination ?? ''}`;
    }

    const safeTitle = escapeHtml(card.title || inferCardTitle(card.url) || 'FRIDAY_WIDGET');
    const safeId = escapeHtml(card.id || 'unknown');

    wrapper.innerHTML = `
        <div class="hud-card-header">
            <span class="hud-card-title">${safeTitle}</span>
            <div class="hud-card-controls">
                <span class="hud-card-id">${safeId}</span>
                <button class="hud-card-close" title="Close widget">×</button>
            </div>
        </div>

        <div class="native-widget-body"></div>

        <div class="hud-card-resize-handle"></div>
    `;

    renderNativeWidgetBody(card, wrapper);
    applySavedCardLayout(card, wrapper, largeIndex);
    attachCardDrag(wrapper);
    attachCardResize(wrapper);
    attachCardClose(wrapper);
    attachMusicControls(wrapper);
    attachNewsControls(wrapper);
    cardUrlCache.set(card.id, card.url || 'about:blank');

    return wrapper;
}

function renderNativeWidgetBody(card, element) {
    const body = element.querySelector('.native-widget-body');

    if (!body) {
        return;
    }

    const type = String(card.type || 'web').toLowerCase();
    const data = card.data || {};

    if (type === 'weather') {
        renderWeatherWidget(data, body);
        return;
    }

    if (type === 'summary') {
        renderSummaryWidget(data, body);
        return;
    }

    if (type === 'map') {
        renderMapWidget(data, body);
        return;
    }

    if (type === 'music') {
        renderMusicWidget(data, body);
        return;
    }

    if (type === 'calendar_agenda') {
        renderCalendarAgendaWidget(data, body);
        return;
    }

    if (type === 'sticky_notes') {
        renderStickyNotesWidget(data, body);
        return;
    }

    if (type === 'system_health') {
        renderSystemHealthWidget(data, body);
        return;
    }

    if (type === 'virtual_finder') {
        renderVirtualFinderWidget(data, body);
        return;
    }

    if (type === 'news') {
        const savedState = safeJsonParse(localStorage.getItem(cardStateStorageKey(card.id, card))) || {};
        const payloadTab = data.active_tab || 'briefing';
        element._newsData = data;
        element._newsActiveTab = element._newsActiveTab
            || (payloadTab === 'briefing' ? savedState.activeTab : payloadTab)
            || 'briefing';
        renderNewsWidget(data, body, element._newsActiveTab);
        return;
    }

    if (type === 'notification_center') {
        renderNotificationCenterWidget(data, body);
        return;
    }

    if (type === 'tasks') {
        renderTasksWidget(data, body);
        return;
    }

    if (type === 'settings') {
        renderSettingsWidget(data, body);
        return;
    }

    if (type === 'proactive_alert') {
        renderProactiveAlertWidget(data, body);
        return;
    }

    renderLegacyWebWidget(card, body);
}

// ==========================================
// TACTICAL MAP — APPLICATION SURFACE
// ==========================================
// On the Workstation the map is not a floating widget: it takes over the whole
// FRIDAY surface. The workstation is hidden with `visibility`, never unmounted,
// so widget positions, sizes, z-order and scroll survive the round trip exactly
// — there is no layout snapshot to save or restore.
function getMapAppLayer(create = false) {
    let layer = document.getElementById('map-app-layer');

    if (!layer && create) {
        layer = document.createElement('section');
        layer.id = 'map-app-layer';
        layer.className = 'map-app-layer';
        layer.setAttribute('role', 'application');
        layer.setAttribute('aria-label', 'Tactical Map');
        document.body.appendChild(layer);
    }

    return layer;
}

function unmountMapApp() {
    const layer = getMapAppLayer();

    if (layer) {
        // Fades out over the workstation rather than cutting to it. The
        // workstation itself is only ever hidden with `visibility`, so widget
        // positions, sizes, z-order and scroll are untouched throughout and are
        // exactly as they were when the map lifts away.
        closeElementWithMotion(layer, { className: 'motion-surface-out' });
    }

    document.body.classList.remove('map-app-open');
}

function closeMapApp(cardId) {
    // Closing through the backend keeps hud state in sync; the card disappearing
    // from the next state_update is what unmounts this layer for good.
    if (cardId) {
        cardUrlCache.delete(cardId);
        socket.emit('close_hud_card', { card_id: cardId });
    }

    unmountMapApp();
}

function mapAppCardId() {
    return getMapAppLayer()?.dataset.cardId || '';
}

function renderMapApp(card) {
    const layer = getMapAppLayer(true);
    document.body.classList.add('map-app-open');

    // Re-mounting would reset pan and zoom, so an already-live map is left alone
    // and only its labels are refreshed.
    if (layer.dataset.cardId === card.id && layer.querySelector('.map-stage')) {
        const destination = String(card.data?.destination || 'Local map');
        const subtitle = layer.querySelector('.map-app-subtitle');

        if (subtitle && !layer.dataset.userLabel) {
            subtitle.textContent = destination;
        }

        return;
    }

    layer.dataset.cardId = card.id;
    layer.innerHTML = `
        <header class="map-app-bar">
            <button class="map-app-back" type="button">
                <span aria-hidden="true">←</span> Back
            </button>
            <div class="map-app-title">
                <strong>Tactical Map</strong>
                <span class="map-app-subtitle">${escapeHtml(String(card.data?.destination || 'Local map'))}</span>
            </div>
            <button class="map-app-close" type="button" aria-label="Close map">×</button>
        </header>
        <div class="map-app-body"></div>
    `;

    renderMapWidget(card.data || {}, layer.querySelector('.map-app-body'));

    layer.querySelectorAll('.map-app-back, .map-app-close').forEach((button) => {
        button.addEventListener('click', () => closeMapApp(card.id));
    });
}

function renderMapWidget(data, body) {
    const cardId = body.closest('.hud-card')?.dataset.cardId || 'map_fullscreen';
    const baseDestination = String(data.destination || 'Local Map');
    const savedState = safeJsonParse(localStorage.getItem(cardStateStorageKey(cardId, body.closest('.hud-card')))) || {};
    const restoreSavedState = savedState.baseDestination === baseDestination;
    const restoredLabel = restoreSavedState ? savedState.label : '';
    const destination = escapeHtml(restoredLabel || baseDestination);
    const source = escapeHtml(data.source || 'OpenStreetMap Tiles');
    const latNumber = data.lat !== null && data.lat !== undefined ? Number(data.lat) : null;
    const lonNumber = data.lon !== null && data.lon !== undefined ? Number(data.lon) : null;
    const savedLat = Number(savedState.lat);
    const savedLon = Number(savedState.lon);
    const savedZoom = Number(savedState.zoom);
    const initialLat = restoreSavedState && Number.isFinite(savedLat)
        ? savedLat
        : Number.isFinite(latNumber) ? latNumber : 20.0;
    const initialLon = restoreSavedState && Number.isFinite(savedLon)
        ? savedLon
        : Number.isFinite(lonNumber) ? lonNumber : 0.0;
    const payloadZoom = Number.isFinite(Number(data.zoom)) ? Number(data.zoom) : 11;
    const initialZoom = Math.max(2, Math.min(18, restoreSavedState && Number.isFinite(savedZoom) ? savedZoom : payloadZoom));
    const lat = Number.isFinite(initialLat) ? initialLat.toFixed(4) : 'N/A';
    const lon = Number.isFinite(initialLon) ? initialLon.toFixed(4) : 'N/A';

    body.innerHTML = `
        <section class="map-stage" data-map-lat="${initialLat}" data-map-lon="${initialLon}" data-map-zoom="${initialZoom}" data-map-base-destination="${escapeHtml(baseDestination)}">
            <div class="map-viewport">
                <div class="map-tile-grid"></div>
                <div class="map-center-reticle"><span></span></div>
            </div>

            <div class="map-scanline-layer"></div>
            <div class="map-grid-layer"></div>
            <div class="map-fade-layer"></div>

            <div class="map-top-rail">
                <span>Tactical map</span>
                <strong class="map-current-destination">${destination}</strong>
            </div>

            <form class="map-search-panel">
                <label for="map-search-input">Address or location</label>
                <div class="map-search-row">
                    <input id="map-search-input" class="map-search-input" type="text" placeholder="Enter address or city..." autocomplete="off">
                    <button class="map-search-button" type="submit">Search</button>
                </div>
                <div class="map-search-hint">Drag to pan. Scroll or use controls to zoom.</div>
            </form>

            <div class="map-zoom-controls">
                <button type="button" class="map-zoom-in">+</button>
                <button type="button" class="map-zoom-out">−</button>
            </div>

            <div class="map-target-label">
                <span>Destination</span>
                <strong class="map-target-name">${destination}</strong>
            </div>

            <div class="map-coordinates">
                <span class="map-lat-readout">Lat ${lat}</span>
                <span class="map-lon-readout">Lon ${lon}</span>
            </div>

            <div class="map-source">Source: ${source}</div>
        </section>
    `;

    initializeTacticalMap(body.querySelector('.map-stage'));
}

function latLonToTilePoint(lat, lon, zoom) {
    const sinLat = Math.sin(lat * Math.PI / 180);
    const scale = Math.pow(2, zoom) * 256;
    const x = (lon + 180) / 360 * scale;
    const y = (0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI)) * scale;
    return { x, y };
}

function tilePointToLatLon(x, y, zoom) {
    const scale = Math.pow(2, zoom) * 256;
    const lon = x / scale * 360 - 180;
    const n = Math.PI - 2 * Math.PI * y / scale;
    const lat = 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
    return { lat, lon };
}

function clampMapCoordinates(lat, lon, zoom = 11) {
    const parsedLat = Number(lat);
    let parsedLon = Number(lon);
    const safeZoom = Number.isFinite(Number(zoom)) ? Number(zoom) : 11;

    let minLat = -84.5;
    let maxLat = 84.5;

    if (safeZoom <= 2) {
        minLat = -8;
        maxLat = 8;
    } else if (safeZoom <= 3) {
        minLat = -18;
        maxLat = 18;
    } else if (safeZoom <= 4) {
        minLat = -45;
        maxLat = 65;
    } else if (safeZoom <= 5) {
        minLat = -62;
        maxLat = 74;
    }

    const safeLat = Number.isFinite(parsedLat)
        ? Math.max(minLat, Math.min(maxLat, parsedLat))
        : 0;

    if (!Number.isFinite(parsedLon)) {
        parsedLon = 0;
    }

    while (parsedLon < -180) {
        parsedLon += 360;
    }

    while (parsedLon > 180) {
        parsedLon -= 360;
    }

    return {
        lat: safeLat,
        lon: parsedLon
    };
}

function buildTacticalTileGrid(lat, lon, zoom = 11) {
    const safeCenter = clampMapCoordinates(lat, lon, zoom);
    const center = latLonToTilePoint(safeCenter.lat, safeCenter.lon, zoom);
    const centerTileX = Math.floor(center.x / 256);
    const centerTileY = Math.floor(center.y / 256);
    const offsetX = center.x - centerTileX * 256;
    const offsetY = center.y - centerTileY * 256;
    const tileCount = Math.pow(2, zoom);
    const range = [-3, -2, -1, 0, 1, 2, 3];
    const tiles = [];

    range.forEach((rowOffset) => {
        range.forEach((colOffset) => {
            const tileX = centerTileX + colOffset;
            const tileY = centerTileY + rowOffset;
            const safeX = ((tileX % tileCount) + tileCount) % tileCount;

            if (tileY < 0 || tileY >= tileCount) {
                return;
            }

            const left = 768 + (colOffset * 256) - offsetX;
            const top = 768 + (rowOffset * 256) - offsetY;

            tiles.push(`
                <img
                    class="map-tile"
                    src="https://tile.openstreetmap.org/${zoom}/${safeX}/${tileY}.png"
                    style="left: ${left}px; top: ${top}px;"
                    alt=""
                    loading="eager"
                >
            `);
        });
    });

    return tiles.join('');
}

function renderTacticalTiles(stage, options = {}) {
    if (!stage) {
        return;
    }

    const rawLat = Number(stage.dataset.mapLat || 41.8781);
    const rawLon = Number(stage.dataset.mapLon || -87.6298);
    const zoom = Number(stage.dataset.mapZoom || 11);
    const safeCoords = clampMapCoordinates(rawLat, rawLon, zoom);
    const lat = safeCoords.lat;
    const lon = safeCoords.lon;

    stage.dataset.mapLat = String(lat);
    stage.dataset.mapLon = String(lon);
    stage.dataset.zoomBand = zoom <= 2 ? 'planet' : zoom <= 5 ? 'wide' : 'local';

    const tileGrid = stage.querySelector('.map-tile-grid');

    if (!tileGrid) {
        return;
    }

    tileGrid.innerHTML = buildTacticalTileGrid(lat, lon, zoom);

    const latReadout = stage.querySelector('.map-lat-readout');
    const lonReadout = stage.querySelector('.map-lon-readout');

    if (latReadout) {
        latReadout.textContent = `Lat ${lat.toFixed(4)}`;
    }

    if (lonReadout) {
        lonReadout.textContent = `Lon ${lon.toFixed(4)}`;
    }

    if (options.updateLabel !== false) {
        scheduleMapSemanticLabelUpdate(stage);
    }
}

function isWithinKosovo(lat, lon) {
    return Number.isFinite(lat) &&
        Number.isFinite(lon) &&
        lat >= 41.80 &&
        lat <= 43.35 &&
        lon >= 20.00 &&
        lon <= 21.90;
}

function animateTacticalMapTo(stage, targetLat, targetLon, options = {}) {
    if (!stage || !Number.isFinite(targetLat) || !Number.isFinite(targetLon)) {
        return;
    }

    const requestedZoom = Number.isFinite(Number(options.zoom))
        ? Math.max(2, Math.min(18, Number(options.zoom)))
        : Number(stage.dataset.mapZoom || 11);

    const safeTarget = clampMapCoordinates(targetLat, targetLon, requestedZoom);
    const startCoords = clampMapCoordinates(
        Number(stage.dataset.mapLat || safeTarget.lat),
        Number(stage.dataset.mapLon || safeTarget.lon),
        Number(stage.dataset.mapZoom || requestedZoom)
    );

    const startLat = startCoords.lat;
    const startLon = startCoords.lon;
    targetLat = safeTarget.lat;
    targetLon = safeTarget.lon;

    const startZoom = Number(stage.dataset.mapZoom || 11);
    const targetZoom = requestedZoom;
    const label = options.label || '';
    const duration = Number.isFinite(Number(options.duration)) ? Number(options.duration) : 900;
    const startTime = performance.now();

    if (stage._mapAnimationFrame) {
        cancelAnimationFrame(stage._mapAnimationFrame);
    }

    function easeInOutCubic(t) {
        return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    }

    function frame(now) {
        const elapsed = now - startTime;
        const progress = Math.min(1, elapsed / duration);
        const eased = easeInOutCubic(progress);
        const nextLat = startLat + (targetLat - startLat) * eased;
        const nextLon = startLon + (targetLon - startLon) * eased;
        const nextZoom = startZoom + (targetZoom - startZoom) * eased;

        stage.dataset.mapLat = String(nextLat);
        stage.dataset.mapLon = String(nextLon);
        stage.dataset.mapZoom = String(Math.round(nextZoom));
        renderTacticalTiles(stage, { updateLabel: progress >= 1 });

        if (progress < 1) {
            stage._mapAnimationFrame = requestAnimationFrame(frame);
            return;
        }

        stage.dataset.mapLat = String(targetLat);
        stage.dataset.mapLon = String(targetLon);
        stage.dataset.mapZoom = String(targetZoom);

        if (label) {
            applyMapSemanticLabel(stage, label);
        } else {
            renderTacticalTiles(stage);
            saveTacticalMapState(stage);
        }
    }

    stage._mapAnimationFrame = requestAnimationFrame(frame);
}

function getApproximateContinent(lat, lon) {
    if (lat < -60) {
        return 'ANTARCTICA';
    }

    if (lat >= 35 && lon >= -25 && lon <= 45) {
        return 'EUROPE';
    }

    if (lat >= -35 && lat <= 35 && lon >= -20 && lon <= 55) {
        return 'AFRICA';
    }

    if (lat >= 5 && lon >= 45 && lon <= 180) {
        return 'ASIA';
    }

    if (lat < 5 && lon >= 95 && lon <= 180) {
        return 'OCEANIA';
    }

    if (lon >= -170 && lon <= -30 && lat >= 15) {
        return 'NORTH AMERICA';
    }

    if (lon >= -95 && lon <= -30 && lat < 15) {
        return 'SOUTH AMERICA';
    }

    return 'PLANET EARTH';
}

function chooseMapReverseZoom(zoom) {
    if (zoom <= 3) {
        return 0;
    }

    if (zoom <= 5) {
        return 3;
    }

    if (zoom <= 7) {
        return 5;
    }

    if (zoom <= 10) {
        return 10;
    }

    if (zoom <= 13) {
        return 12;
    }

    return 16;
}

function normalizeMapPoliticalLabel(label) {
    if (!label) {
        return '';
    }

    const value = String(label).trim();
    const lower = value.toLowerCase();

    if (
        lower === 'kosovo' ||
        lower === 'republic of kosovo' ||
        lower === 'kosova' ||
        lower === 'republic of kosova' ||
        lower.includes('kosovo') ||
        lower.includes('kosova') ||
        lower.includes('republic of serbia') ||
        lower.includes('autonomous province of kosovo') ||
        lower.includes('autonomous province of kosovo and metohija') ||
        lower.includes('kosovo and metohija')
    ) {
        return 'Kosovo';
    }

    return value;
}

function saveTacticalMapState(stage) {
    const cardId = stage?.closest('.hud-card')?.dataset.cardId;

    if (!stage || !cardId) {
        return;
    }

    const label = stage.querySelector('.map-current-destination')?.textContent?.trim() || '';
    localStorage.setItem(cardStateStorageKey(cardId, stage.closest('.hud-card')), JSON.stringify({
        baseDestination: stage.dataset.mapBaseDestination || 'Local Map',
        lat: Number(stage.dataset.mapLat),
        lon: Number(stage.dataset.mapLon),
        zoom: Number(stage.dataset.mapZoom),
        label
    }));
}

function applyMapSemanticLabel(stage, label) {
    if (!stage || !label) {
        return;
    }

    const cleanLabel = escapeHtml(normalizeMapPoliticalLabel(label).toUpperCase());
    const targetName = stage.querySelector('.map-target-name');
    const currentDestination = stage.querySelector('.map-current-destination');

    if (targetName) {
        targetName.innerHTML = cleanLabel;
    }

    if (currentDestination) {
        currentDestination.innerHTML = cleanLabel;
    }

    saveTacticalMapState(stage);
}

function chooseMapLabelByZoom(address, fallback, zoom) {
    if (!address || typeof address !== 'object') {
        return normalizeMapPoliticalLabel(fallback);
    }

    if (zoom <= 5) {
        return normalizeMapPoliticalLabel(address.continent || fallback);
    }

    if (zoom <= 7) {
        return normalizeMapPoliticalLabel(address.country || address.state || address.region || fallback);
    }

    if (zoom <= 9) {
        return normalizeMapPoliticalLabel(address.state || address.region || address.country || fallback);
    }

    if (zoom <= 12) {
        return normalizeMapPoliticalLabel(
            address.city ||
            address.town ||
            address.village ||
            address.municipality ||
            address.county ||
            address.state ||
            address.country ||
            fallback
        );
    }

    return normalizeMapPoliticalLabel(
        address.road ||
        address.neighbourhood ||
        address.suburb ||
        address.city_district ||
        address.city ||
        address.town ||
        address.village ||
        address.municipality ||
        address.county ||
        address.country ||
        fallback
    );
}

function getBestMapLabelFromAddress(address, fallback, zoom = 11) {
    return chooseMapLabelByZoom(address, fallback, zoom);
}

function scheduleMapSemanticLabelUpdate(stage) {
    if (!stage) {
        return;
    }

    const lat = Number(stage.dataset.mapLat || 41.8781);
    const lon = Number(stage.dataset.mapLon || -87.6298);
    const zoom = Number(stage.dataset.mapZoom || 11);

    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        return;
    }

    window.clearTimeout(stage._semanticLabelTimer);

    stage._semanticLabelTimer = window.setTimeout(async () => {
        const latestLat = Number(stage.dataset.mapLat || lat);
        const latestLon = Number(stage.dataset.mapLon || lon);
        const latestZoom = Number(stage.dataset.mapZoom || zoom);

        if (latestZoom <= 3) {
            applyMapSemanticLabel(stage, 'PLANET EARTH');
            return;
        }

        if (isWithinKosovo(latestLat, latestLon)) {
            applyMapSemanticLabel(stage, 'Kosovo');
            return;
        }

        if (latestZoom <= 5) {
            applyMapSemanticLabel(stage, getApproximateContinent(latestLat, latestLon));
            return;
        }

        const reverseZoom = chooseMapReverseZoom(latestZoom);
        const cacheKey = `${latestLat.toFixed(2)}:${latestLon.toFixed(2)}:${reverseZoom}`;

        if (stage.dataset.lastSemanticKey === cacheKey) {
            return;
        }

        stage.dataset.lastSemanticKey = cacheKey;

        try {
            const reverseUrl = `https://nominatim.openstreetmap.org/reverse?lat=${encodeURIComponent(latestLat)}&lon=${encodeURIComponent(latestLon)}&format=json&zoom=${reverseZoom}&addressdetails=1`;
            const response = await fetch(reverseUrl, {
                headers: {
                    'Accept': 'application/json'
                }
            });
            const result = await response.json();
            const label = getBestMapLabelFromAddress(
                result.address,
                result.name || result.display_name || getApproximateContinent(latestLat, latestLon),
                latestZoom
            );

            applyMapSemanticLabel(stage, label || 'PLANET EARTH');
        } catch (error) {
            applyMapSemanticLabel(stage, getApproximateContinent(latestLat, latestLon));
        }
    }, 850);
}

function setTacticalMapLocation(stage, lat, lon, label = '') {
    if (!stage || !Number.isFinite(lat) || !Number.isFinite(lon)) {
        return;
    }

    const normalizedLabel = normalizeMapPoliticalLabel(label);

    animateTacticalMapTo(stage, lat, lon, {
        label: normalizedLabel,
        zoom: 11,
        duration: 950
    });
}

function initializeTacticalMap(stage) {
    if (!stage || stage.dataset.initialized === 'true') {
        return;
    }

    stage.dataset.initialized = 'true';
    renderTacticalTiles(stage);

    const viewport = stage.querySelector('.map-viewport');
    const searchForm = stage.querySelector('.map-search-panel');
    const searchInput = stage.querySelector('.map-search-input');
    const zoomIn = stage.querySelector('.map-zoom-in');
    const zoomOut = stage.querySelector('.map-zoom-out');
    let isDragging = false;
    let lastX = 0;
    let lastY = 0;

    function changeZoom(delta) {
        const currentZoom = Number(stage.dataset.mapZoom || 11);
        const nextZoom = Math.max(2, Math.min(18, currentZoom + delta));

        if (nextZoom === currentZoom) {
            return;
        }

        stage.dataset.mapZoom = String(nextZoom);
        renderTacticalTiles(stage);
        saveTacticalMapState(stage);
    }

    if (zoomIn) {
        zoomIn.addEventListener('click', () => changeZoom(1));
    }

    if (zoomOut) {
        zoomOut.addEventListener('click', () => changeZoom(-1));
    }

    if (viewport) {
        viewport.addEventListener('wheel', (event) => {
            event.preventDefault();
            changeZoom(event.deltaY < 0 ? 1 : -1);
        }, { passive: false });

        viewport.addEventListener('pointerdown', (event) => {
            isDragging = true;
            lastX = event.clientX;
            lastY = event.clientY;
            viewport.setPointerCapture(event.pointerId);
            viewport.classList.add('is-dragging');
        });

        viewport.addEventListener('pointermove', (event) => {
            if (!isDragging) {
                return;
            }

            const dx = event.clientX - lastX;
            const dy = event.clientY - lastY;
            lastX = event.clientX;
            lastY = event.clientY;

            const zoom = Number(stage.dataset.mapZoom || 11);
            const lat = Number(stage.dataset.mapLat || 41.8781);
            const lon = Number(stage.dataset.mapLon || -87.6298);
            const point = latLonToTilePoint(lat, lon, zoom);
            const next = tilePointToLatLon(point.x - dx, point.y - dy, zoom);
            const safeNext = clampMapCoordinates(next.lat, next.lon, zoom);

            stage.dataset.mapLat = String(safeNext.lat);
            stage.dataset.mapLon = String(safeNext.lon);
            renderTacticalTiles(stage);
        });

        const stopDragging = (event) => {
            if (!isDragging) {
                return;
            }

            isDragging = false;
            viewport.classList.remove('is-dragging');
            saveTacticalMapState(stage);

            try {
                viewport.releasePointerCapture(event.pointerId);
            } catch (error) {
                // Pointer was already released.
            }
        };

        viewport.addEventListener('pointerup', stopDragging);
        viewport.addEventListener('pointercancel', stopDragging);
        viewport.addEventListener('pointerleave', stopDragging);
    }

    if (searchForm && searchInput) {
        searchForm.addEventListener('submit', async (event) => {
            event.preventDefault();
            const query = searchInput.value.trim();

            if (!query) {
                return;
            }

            searchForm.classList.add('is-searching');

            try {
                const url = `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(query)}&format=json&limit=1`;
                const response = await fetch(url, {
                    headers: {
                        'Accept': 'application/json'
                    }
                });
                const results = await response.json();

                if (Array.isArray(results) && results.length) {
                    const result = results[0];
                    const lat = Number(result.lat);
                    const lon = Number(result.lon);
                    const label = normalizeMapPoliticalLabel((result.display_name || query).split(',')[0]);
                    setTacticalMapLocation(stage, lat, lon, label);
                    searchInput.value = '';
                }
            } catch (error) {
                console.warn('FRIDAY map search failed:', error);
            } finally {
                searchForm.classList.remove('is-searching');
            }
        });
    }
}

/** Weather glyph per condition. Drawn from the real weather code, never guessed. */
function weatherGlyph(condition, isDay = true) {
    const key = String(condition || '').toLowerCase();
    if (key.includes('rain')) return '☂';
    if (key.includes('cloud')) return '☁';
    if (key.includes('clear')) return isDay ? '☀' : '☾';
    return '◍';
}

function weatherTime(iso) {
    if (!iso) return '';
    const parsed = new Date(iso);
    if (Number.isNaN(parsed.getTime())) return String(iso).split('T').pop().slice(0, 5);
    return parsed.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

/**
 * Weather, as an application rather than a readout.
 *
 * Every value here comes from the Open-Meteo response. A metric the API did not
 * return is OMITTED — the tile simply does not render — because a dash in a
 * pressure field reads as a broken sensor rather than as "not reported".
 */
function renderWeatherWidget(data, body) {
    const hourly = Array.isArray(data.forecast) ? data.forecast : [];
    const daily = Array.isArray(data.daily) ? data.daily : [];
    const isDay = data.is_day !== false;

    const num = (value, suffix = '') =>
        (value === null || value === undefined || value === '') ? null : `${value}${suffix}`;

    // Only real readings become tiles.
    const metrics = [
        ['Feels like', num(data.feels_like, '°')],
        ['Humidity', num(data.humidity, '%')],
        ['Wind', num(data.wind, ' mph')],
        ['Rain', num(hourly[0]?.precip, '%')],
        ['Pressure', num(data.pressure, ' hPa')],
        ['Visibility', num(data.visibility_miles, ' mi')],
        ['UV index', num(data.uv_index)],
        ['Sunrise', data.sunrise ? weatherTime(data.sunrise) : null],
        ['Sunset', data.sunset ? weatherTime(data.sunset) : null]
    ].filter(([, value]) => value !== null);

    const metricsHtml = metrics.map(([label, value]) => `
        <div class="wx-metric">
            <span class="wx-metric-label">${escapeHtml(label)}</span>
            <strong class="wx-metric-value">${escapeHtml(String(value))}</strong>
        </div>`).join('');

    // Day/night per HOUR, from the real sunrise and sunset.
    //
    // Using the current is_day flag for every row put a sun over 22:00, which is
    // invented information even though every other value in the row is real.
    const sunriseMs = data.sunrise ? new Date(data.sunrise).getTime() : null;
    const sunsetMs = data.sunset ? new Date(data.sunset).getTime() : null;

    const hourIsDay = (iso) => {
        if (!iso || sunriseMs === null || sunsetMs === null) return isDay;
        const at = new Date(iso).getTime();
        if (Number.isNaN(at)) return isDay;
        // Compare time-of-day so the window applies to every day in the strip.
        const minutes = (ms) => {
            const d = new Date(ms);
            return d.getHours() * 60 + d.getMinutes();
        };
        const m = minutes(at);
        return m >= minutes(sunriseMs) && m < minutes(sunsetMs);
    };

    const hourlyHtml = hourly.map((item) => `
        <div class="wx-hour ${item.is_now ? 'is-now' : ''}">
            <span class="wx-hour-time">${item.is_now ? 'Now' : escapeHtml(String(item.time || ''))}</span>
            <span class="wx-hour-glyph">${weatherGlyph(item.condition, hourIsDay(item.iso))}</span>
            <span class="wx-hour-temp">${item.temp ?? '--'}°</span>
            ${Number(item.precip) > 0 ? `<span class="wx-hour-rain">${item.precip}%</span>` : '<span class="wx-hour-rain"></span>'}
        </div>`).join('');

    // A shared scale makes the 7-day bars comparable at a glance.
    const highs = daily.map((d) => d.high).filter((v) => v !== null && v !== undefined);
    const lows = daily.map((d) => d.low).filter((v) => v !== null && v !== undefined);
    const scaleMax = highs.length ? Math.max(...highs) : 1;
    const scaleMin = lows.length ? Math.min(...lows) : 0;
    const span = Math.max(1, scaleMax - scaleMin);

    const dailyHtml = daily.map((day) => {
        const left = ((day.low - scaleMin) / span) * 100;
        const width = Math.max(6, ((day.high - day.low) / span) * 100);
        const label = day.when === 'today' ? 'Today' : (day.day || '').slice(0, 3);

        return `
            <div class="wx-day">
                <span class="wx-day-name">${escapeHtml(label)}</span>
                <span class="wx-day-glyph">${weatherGlyph(day.condition, true)}</span>
                <span class="wx-day-rain">${Number(day.precip_chance) > 0 ? day.precip_chance + '%' : ''}</span>
                <span class="wx-day-low">${day.low ?? '--'}°</span>
                <span class="wx-day-track"><span class="wx-day-bar" style="left:${left.toFixed(1)}%;width:${width.toFixed(1)}%"></span></span>
                <span class="wx-day-high">${day.high ?? '--'}°</span>
            </div>`;
    }).join('');

    body.innerHTML = `
        <div class="wx-app" data-daynight="${isDay ? 'day' : 'night'}">
            <header class="wx-now">
                <div class="wx-now-text">
                    <div class="wx-location">${escapeHtml(data.location || 'Local')}</div>
                    <div class="wx-temp">${data.temperature ?? '--'}<span>°</span></div>
                    <div class="wx-condition">
                        <span class="wx-condition-glyph">${weatherGlyph(data.condition, isDay)}</span>
                        ${escapeHtml(data.condition || 'Conditions')}
                    </div>
                </div>
            </header>

            <section class="wx-section wx-hourly">
                ${hourlyHtml || '<div class="wx-empty">Hourly forecast not reported.</div>'}
            </section>

            <section class="wx-section wx-daily">
                <div class="wx-section-title">7-day forecast</div>
                ${dailyHtml || '<div class="wx-empty">Daily forecast not reported.</div>'}
            </section>

            <section class="wx-section wx-metrics">
                ${metricsHtml || '<div class="wx-empty">No additional readings reported.</div>'}
            </section>

            <footer class="wx-source">${escapeHtml(data.source || 'Telemetry')}</footer>
        </div>
    `;
}

function renderSummaryWidget(data, body) {
    const headline = escapeHtml(data.headline || 'Briefing');
    const summary = escapeHtml(data.summary || 'Awaiting briefing data.');
    const items = Array.isArray(data.items) ? data.items : [];
    const itemsHtml = items.map((item) => `<li>${escapeHtml(item)}</li>`).join('');

    body.innerHTML = `
        <div class="summary-widget">
            <div class="summary-headline">${headline}</div>
            <div class="summary-copy">${summary}</div>
            <ul class="summary-list">${itemsHtml}</ul>
        </div>
    `;
}

function renderStickyNotesWidget(data, body) {
    const notes = Array.isArray(data?.notes) ? data.notes.slice().reverse() : [];
    const notesHtml = notes.length ? notes.map((note) => `
        <article class="sticky-note-item" data-note-id="${escapeHtml(note.id || '')}">
            <button class="sticky-note-delete" type="button" data-note-delete="${escapeHtml(note.id || '')}" aria-label="Delete note">×</button>
            <p>${escapeHtml(note.text || '')}</p>
            <time>${escapeHtml(formatNoteTime(note.created_at))}</time>
        </article>
    `).join('') : '<div class="sticky-notes-empty">No notes logged.</div>';

    body.innerHTML = `
        <section class="sticky-notes-widget">
            <div class="sticky-notes-panel-head">
                <span>NOTES</span>
                <strong>LOCAL MEMORY</strong>
            </div>
            <div class="sticky-notes-list">${notesHtml}</div>
            <form class="sticky-note-compose">
                <input type="text" placeholder="Type note..." autocomplete="off">
                <button type="submit">ADD</button>
            </form>
        </section>
    `;

    const form = body.querySelector('.sticky-note-compose');
    const input = form?.querySelector('input');

    form?.addEventListener('submit', (event) => {
        event.preventDefault();
        const text = input?.value.trim() || '';

        if (!text) {
            return;
        }

        socket.emit('manual_override', {
            text: `add note ${text}`
        });
        input.value = '';
    });

    body.querySelectorAll('[data-note-delete]').forEach((button) => {
        button.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            const noteId = button.dataset.noteDelete || '';

            if (noteId) {
                socket.emit('manual_override', {
                    text: `delete note ${noteId}`
                });
            }
        });
    });
}

function formatNoteTime(value) {
    if (!value) {
        return 'UNKNOWN';
    }

    try {
        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return String(value).toUpperCase();
        }

        return date.toLocaleString([], {
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit'
        }).toUpperCase();
    } catch (_) {
        return String(value).toUpperCase();
    }
}

function normalizeTasksPayload(payload = {}) {
    const safe = payload && typeof payload === 'object' ? payload : {};
    const tasks = Array.isArray(safe.tasks) ? safe.tasks : [];
    const active = Array.isArray(safe.active) ? safe.active : tasks.filter((task) => !task.completed);
    const completed = Array.isArray(safe.completed) ? safe.completed : tasks.filter((task) => task.completed);

    return {
        ...safe,
        tasks,
        active,
        completed,
        overdue: Array.isArray(safe.overdue) ? safe.overdue : [],
        today: Array.isArray(safe.today) ? safe.today : [],
        tomorrow: Array.isArray(safe.tomorrow) ? safe.tomorrow : [],
        this_week: Array.isArray(safe.this_week) ? safe.this_week : [],
        later: Array.isArray(safe.later) ? safe.later : [],
        counts: safe.counts || {
            total_active: active.length,
            overdue: 0,
            today: 0,
            tomorrow: 0,
            completed: completed.length
        },
        next_task: safe.next_task || active[0] || null
    };
}

function formatTaskDue(value) {
    if (!value) {
        return 'NO DUE';
    }

    try {
        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return String(value).toUpperCase();
        }

        return date.toLocaleString([], {
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit'
        }).toUpperCase();
    } catch (_) {
        return String(value).toUpperCase();
    }
}

function formatTaskTime(value) {
    if (!value) {
        return 'ANYTIME';
    }

    try {
        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return String(value).toUpperCase();
        }

        return date.toLocaleTimeString([], {
            hour: 'numeric',
            minute: '2-digit'
        }).toUpperCase();
    } catch (_) {
        return String(value).toUpperCase();
    }
}

function taskRowHtml(task, compact = false) {
    const priority = String(task?.priority || 'normal').toLowerCase();
    const completed = task?.completed === true;
    const taskKey = task?.id || task?.title || '';
    const due = formatTaskDue(task?.due_at);

    return `
        <article class="task-row priority-${escapeHtml(priority)} ${completed ? 'completed' : ''}" data-task-id="${escapeHtml(task?.id || '')}">
            <button class="task-check" type="button" data-task-complete="${escapeHtml(taskKey)}" aria-label="Complete task"></button>
            <div class="task-row-main">
                <strong>${escapeHtml(task?.title || 'Untitled task')}</strong>
                ${compact || !task?.notes ? '' : `<span>${escapeHtml(task.notes)}</span>`}
            </div>
            ${due ? `<time class="task-due">${escapeHtml(due)}</time>` : ''}
            <span class="task-priority-dot" title="${escapeHtml(priority)} priority"></span>
            <button class="task-delete" type="button" data-task-delete="${escapeHtml(taskKey)}" aria-label="Delete task">×</button>
        </article>
    `;
}

function renderTaskGroup(title, tasks, emptyText = 'Nothing here') {
    const items = Array.isArray(tasks) ? tasks : [];

    // An empty group is dropped entirely rather than rendered as a header over a
    // "CLEAR" placeholder — four empty headers stacked up was most of the dead
    // space in the old widget.
    if (!items.length) {
        return '';
    }

    return `
        <section class="tasks-group" data-group="${escapeHtml(String(title).toLowerCase())}">
            <div class="tasks-group-title">
                <span>${escapeHtml(title)}</span>
                <strong>${items.length}</strong>
            </div>
            <div class="tasks-group-list">
                ${items.map((task) => taskRowHtml(task)).join('')}
            </div>
        </section>
    `;
}

function attachTaskControls(root) {
    root.querySelectorAll('[data-task-complete]').forEach((button) => {
        button.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            emitDirectAction('complete_task', 'tasks_ui', { query: button.dataset.taskComplete || '' });
        });
    });

    root.querySelectorAll('[data-task-delete]').forEach((button) => {
        button.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            emitDirectAction('delete_task', 'tasks_ui', { query: button.dataset.taskDelete || '' });
        });
    });
}

/**
 * Tasks, in three densities.
 *
 * Density now comes from the window's own size rather than an EXP button, so the
 * widget matches how big the user actually made it. Empty groups are omitted, and
 * the group list is the flex child that grows, so a tall window shows more tasks
 * instead of more blank panel.
 */
function renderTasksWidget(data, body) {
    const payload = normalizeTasksPayload(data || tasksPayload || {});
    const counts = payload.counts || {};
    const nextTask = payload.next_task;

    const overdue = Number(counts.overdue || 0);
    const today = Number(counts.today || 0);
    const upcoming = [...(payload.this_week || []), ...(payload.later || [])];
    const completed = Array.isArray(payload.completed) ? payload.completed : [];

    const groups = [
        renderTaskGroup('Overdue', payload.overdue),
        renderTaskGroup('Today', payload.today),
        renderTaskGroup('Tomorrow', payload.tomorrow),
        renderTaskGroup('Upcoming', upcoming)
    ].join('');

    const completedGroup = completed.length
        ? `
            <section class="tasks-group tasks-group-done">
                <div class="tasks-group-title">
                    <span>Completed</span>
                    <strong>${completed.length}</strong>
                </div>
                <div class="tasks-group-list">
                    ${completed.slice(0, 12).map((task) => taskRowHtml(task, true)).join('')}
                </div>
            </section>`
        : '';

    const allClear = !groups && !completedGroup;

    body.innerHTML = `
        <section class="tasks-panel">
            <div class="tasks-summary">
                <div class="tasks-stat ${overdue ? 'is-alert' : ''}">
                    <strong>${overdue}</strong><span>Overdue</span>
                </div>
                <div class="tasks-stat">
                    <strong>${today}</strong><span>Today</span>
                </div>
                <div class="tasks-next-up">
                    <span class="tasks-label">Next</span>
                    <strong title="${escapeHtml(nextTask?.title || '')}">${escapeHtml(nextTask?.title || 'Nothing scheduled')}</strong>
                    ${nextTask ? `<em>${escapeHtml(formatTaskTime(nextTask.due_at))}</em>` : ''}
                </div>
            </div>

            <div class="tasks-scroll">
                ${allClear ? '<div class="tasks-allclear">All clear.</div>' : groups + completedGroup}
            </div>

            <form class="tasks-compose">
                <input name="title" type="text" placeholder="Add a task" autocomplete="off" aria-label="Task title">
                <input name="due" type="text" placeholder="When" autocomplete="off" aria-label="Due date">
                <select name="priority" aria-label="Task priority">
                    <option value="normal">Normal</option>
                    <option value="low">Low</option>
                    <option value="high">High</option>
                    <option value="urgent">Urgent</option>
                </select>
                <button type="submit" title="Add task">+</button>
            </form>
        </section>
    `;

    const form = body.querySelector('.tasks-compose');

    form?.addEventListener('submit', (event) => {
        event.preventDefault();
        const title = form.elements.title?.value?.trim() || '';
        const due = form.elements.due?.value?.trim() || '';
        const priority = form.elements.priority?.value || 'normal';

        if (!title) {
            return;
        }

        emitDirectAction('add_task', 'tasks_ui', {
            title: due ? `${title} ${due}` : title,
            priority
        });
        form.reset();
    });

    attachTaskControls(body);
}

function renderSystemHealthWidget(data, body) {
    const components = Array.isArray(data?.components) ? data.components : [];
    const metrics = data?.metrics || {};
    const componentHtml = components.map((component) => {
        const status = String(component.status || 'DEGRADED').toLowerCase();

        return `
            <div class="health-component status-${escapeHtml(status)}">
                <span>${escapeHtml(component.name || 'Component')}</span>
                <strong>${escapeHtml(component.status || 'DEGRADED')}</strong>
                <p>${escapeHtml(component.detail || '')}</p>
            </div>
        `;
    }).join('');

    body.innerHTML = `
        <section class="system-health-widget">
            <div class="health-metric-grid">
                <div><span>CPU</span><strong>${escapeHtml(metrics.cpu_percent ?? '--')}%</strong></div>
                <div><span>MEMORY</span><strong>${escapeHtml(metrics.memory_percent ?? '--')}%</strong></div>
                <div><span>DISK FREE</span><strong>${escapeHtml(metrics.disk_free_gb ?? '--')} GB</strong></div>
                <div><span>PRESENCE</span><strong>${escapeHtml(metrics.presence || '--')}</strong></div>
                <div><span>PRESENCE MODE</span><strong>${escapeHtml(metrics.presence_mode || '--')}</strong></div>
                <div><span>PRESENCE SOURCE</span><strong>${escapeHtml(metrics.presence_source || '--')}</strong></div>
                <div><span>IDLE TIME</span><strong>${escapeHtml(settingsIdleLabel(metrics.presence_idle_seconds))}</strong></div>
                <div><span>AWAY TIMEOUT</span><strong>${escapeHtml(metrics.presence_away_timeout_minutes ?? '--')} min</strong></div>
            </div>

            <div class="health-component-grid">
                ${componentHtml || '<div class="health-empty">No diagnostics available.</div>'}
            </div>

            <div class="health-system-strip">
                <span>PY ${escapeHtml(metrics.python || '--')}</span>
                <span>${escapeHtml(metrics.battery || 'battery unavailable')}</span>
                <span>Last seen ${escapeHtml(settingsTimestamp(metrics.presence_last_seen))}</span>
                <span>${escapeHtml(metrics.platform || 'platform unavailable')}</span>
            </div>
        </section>
    `;
}

const VIRTUAL_FINDER_VIEW_MODES = new Set(['list', 'grid']);
const VIRTUAL_FINDER_SORT_FIELDS = new Set(['name', 'type', 'modified', 'size', 'created']);
const VIRTUAL_FINDER_FILE_TYPES = [
    { value: 'txt', label: 'Text (.txt)' },
    { value: 'md', label: 'Markdown (.md)' },
    { value: 'json', label: 'JSON (.json)' },
    { value: 'py', label: 'Python (.py)' },
    { value: 'js', label: 'JavaScript (.js)' },
    { value: 'empty', label: 'No extension' }
];
const VIRTUAL_FINDER_DRAG_TYPE = 'application/x-friday-virtual-items';

function normalizeVirtualFinderPath(value) {
    return String(value || '')
        .replace(/\\/g, '/')
        .split('/')
        .map((part) => part.trim())
        .filter((part) => part && part !== '.' && part !== '..')
        .join('/');
}

function virtualFinderWorkspace(body) {
    const card = body?.closest?.('.hud-card');
    const workspace = card?.dataset?.workshopWorkspace || '';

    if (['main', 'secondary'].includes(workspace)) {
        return workspace;
    }

    if (workspace === 'single') {
        const cardId = card?.dataset?.cardId || 'virtual_finder';
        const storedWorkspace = cardWorkshopWorkspace(
            latestState?.active_cards?.find((entry) => entry?.id === cardId)
        );
        return ['main', 'secondary'].includes(storedWorkspace) ? storedWorkspace : 'main';
    }

    return '';
}

function virtualFinderRequestPayload(body, payload = {}) {
    const workspace = virtualFinderWorkspace(body);
    return workspace ? { ...payload, workspace } : payload;
}

function virtualFinderBodies() {
    return Array.from(document.querySelectorAll(
        '.hud-card.widget-type-virtual_finder .native-widget-body'
    ));
}

function virtualFinderActiveBody(fallback = null, workspace = '') {
    if (fallback?.isConnected) {
        return fallback;
    }

    const targetWorkspace = String(workspace || '').toLowerCase();
    const bodies = virtualFinderBodies();

    if (targetWorkspace) {
        const targeted = bodies.find((candidate) => virtualFinderWorkspace(candidate) === targetWorkspace);

        if (targeted) {
            return targeted;
        }
    }

    if (virtualFinderLastActiveBody?.isConnected) {
        return virtualFinderLastActiveBody;
    }

    return bodies.find((candidate) => candidate.offsetParent !== null) || bodies[0] || null;
}

function virtualFinderPreferenceStorageKey(body) {
    const card = body?.closest?.('.hud-card');
    return card ? cardStateStorageKey(card.dataset.cardId || 'virtual_finder', card) : '';
}

function normalizeVirtualFinderFavorites(value) {
    const favorites = [];
    const seen = new Set();

    (Array.isArray(value) ? value : []).forEach((entry) => {
        const path = normalizeVirtualFinderPath(
            typeof entry === 'string' ? entry : entry?.path
        );

        if (!path || seen.has(path)) {
            return;
        }

        seen.add(path);
        favorites.push({
            path,
            name: String(
                typeof entry === 'string'
                    ? path.split('/').at(-1)
                    : entry?.name || path.split('/').at(-1)
            )
        });
    });

    return favorites.slice(0, 24);
}

function getVirtualFinderController(body) {
    if (!body) {
        return null;
    }

    const existing = virtualFinderControllers.get(body);

    if (existing) {
        return existing;
    }

    const storageKey = virtualFinderPreferenceStorageKey(body);
    const stored = storageKey
        ? safeJsonParse(localStorage.getItem(storageKey)) || {}
        : {};
    const preferences = stored.virtualFinder && typeof stored.virtualFinder === 'object'
        ? stored.virtualFinder
        : stored;
    const viewMode = VIRTUAL_FINDER_VIEW_MODES.has(preferences.viewMode)
        ? preferences.viewMode
        : 'list';
    const sortField = VIRTUAL_FINDER_SORT_FIELDS.has(preferences.sortField)
        ? preferences.sortField
        : 'name';
    const sortDirection = preferences.sortDirection === 'desc' ? 'desc' : 'asc';
    const preferredPath = normalizeVirtualFinderPath(preferences.currentPath || '');
    const previewPath = normalizeVirtualFinderPath(preferences.previewPath || '');
    const controller = {
        body,
        initialized: false,
        currentPath: '',
        parentPath: '',
        rootLabel: 'Virtual Finder',
        searchQuery: '',
        searchDraft: '',
        searchDirty: false,
        pendingSearchQuery: null,
        searchTimer: null,
        backStack: [],
        forwardStack: [],
        requestSerial: 0,
        requests: new Map(),
        statusTimer: null,
        status: { status: 'idle', operation: '', message: '', token: '' },
        createMode: '',
        createDraft: '',
        createFileType: 'txt',
        createError: '',
        renamePath: '',
        renameDraft: '',
        renameError: '',
        selectedPaths: new Set(),
        selectionAnchor: '',
        focusedPath: '',
        clipboard: { mode: '', paths: [] },
        dragPaths: [],
        favoriteDragPath: '',
        contextMenu: null,
        dialog: null,
        previewOpen: preferences.previewOpen === true,
        previewPath,
        previewStatus: 'idle',
        previewError: '',
        previewData: null,
        previewLoadedPath: '',
        viewMode,
        sortField,
        sortDirection,
        sidebarCollapsed: preferences.sidebarCollapsed === true,
        favoritesCollapsed: preferences.favoritesCollapsed === true,
        favorites: normalizeVirtualFinderFavorites(preferences.favorites),
        preferredPath,
        restorePathAttempted: false,
        restorePathScheduled: false,
        listScrollTop: 0,
        lastData: {},
        items: [],
        itemByPath: new Map(),
        folderTree: [],
        storage: {},
        pendingFocus: null,
        outsideMenuHandler: null
    };

    virtualFinderControllers.set(body, controller);
    return controller;
}

function saveVirtualFinderPreferences(body, controller = getVirtualFinderController(body)) {
    const storageKey = virtualFinderPreferenceStorageKey(body);

    if (!storageKey || !controller) {
        return;
    }

    const existing = safeJsonParse(localStorage.getItem(storageKey)) || {};
    const payload = {
        version: 1,
        viewMode: controller.viewMode,
        sortField: controller.sortField,
        sortDirection: controller.sortDirection,
        sidebarCollapsed: controller.sidebarCollapsed,
        favoritesCollapsed: controller.favoritesCollapsed,
        favorites: controller.favorites.map((favorite) => ({
            name: String(favorite.name || favorite.path.split('/').at(-1) || 'Favorite'),
            path: normalizeVirtualFinderPath(favorite.path)
        })).filter((favorite) => favorite.path),
        currentPath: normalizeVirtualFinderPath(controller.currentPath),
        previewOpen: controller.previewOpen,
        previewPath: normalizeVirtualFinderPath(controller.previewPath)
    };

    localStorage.setItem(storageKey, JSON.stringify({
        ...existing,
        virtualFinder: payload
    }));
}

function virtualFinderLocationIcon(name) {
    const icons = {
        'private folder': '◈',
        'meholli industries': '▦',
        projects: '▰',
        school: '△',
        'friday logs': '≡',
        'jarvis logs': '≡' // pre-rename folder name, still on disk for anyone
                           // who has not started the app since the migration
    };
    return icons[String(name || '').trim().toLowerCase()] || '▰';
}

function virtualFinderItemIcon(item) {
    if (item?.type === 'folder') {
        return '▰';
    }

    const kind = String(item?.kind || '').toLowerCase();
    const extension = String(item?.extension || '').toLowerCase();

    if (kind === 'image') return '▧';
    if (kind === 'markdown') return 'M↓';
    if (kind === 'json') return '{}';
    if (kind === 'python') return 'PY';
    if (kind === 'javascript') return 'JS';
    if (kind === 'log') return '≋';
    if (kind === 'document' || ['.pdf', '.doc', '.docx', '.rtf'].includes(extension)) return '▤';
    if (kind === 'text') return '≡';
    return '◇';
}

function formatVirtualFinderSize(value, type) {
    if (String(type || '').toLowerCase() === 'folder') {
        return '—';
    }

    const bytes = Number(value);

    if (!Number.isFinite(bytes) || bytes < 0) {
        return '—';
    }

    if (bytes < 1024) {
        return `${Math.round(bytes)} B`;
    }

    const units = ['KB', 'MB', 'GB', 'TB'];
    let size = bytes / 1024;
    let unitIndex = 0;

    while (size >= 1024 && unitIndex < units.length - 1) {
        size /= 1024;
        unitIndex += 1;
    }

    const precision = size >= 10 ? 0 : 1;
    return `${size.toFixed(precision)} ${units[unitIndex]}`;
}

function formatVirtualFinderTimestamp(value) {
    const timestamp = Number(value);

    if (!Number.isFinite(timestamp) || timestamp <= 0) {
        return { label: '—', title: 'Metadata unavailable' };
    }

    const date = new Date(timestamp < 1000000000000 ? timestamp * 1000 : timestamp);

    if (Number.isNaN(date.getTime())) {
        return { label: '—', title: 'Metadata unavailable' };
    }

    return {
        label: date.toLocaleDateString([], {
            year: 'numeric',
            month: 'short',
            day: '2-digit'
        }),
        title: date.toLocaleString()
    };
}

function formatVirtualFinderModified(value) {
    return formatVirtualFinderTimestamp(value);
}

function virtualFinderDisplayMessage(value, fallback) {
    const message = String(value || fallback || '').trim().replace(/\s+Sir\.?$/i, '');

    if (!message) {
        return '';
    }

    return /[.!?]$/.test(message) ? message : `${message}.`;
}

function normalizeVirtualFinderItem(item) {
    const type = String(item?.type || 'file').toLowerCase() === 'folder' ? 'folder' : 'file';
    const path = normalizeVirtualFinderPath(item?.path || '');
    const name = String(item?.name || path.split('/').at(-1) || 'Untitled');
    const itemCount = Number(item?.item_count);

    return {
        ...(item && typeof item === 'object' ? item : {}),
        name,
        path,
        type,
        kind: String(item?.kind || (type === 'folder' ? 'folder' : 'file')).toLowerCase(),
        size: Number.isFinite(Number(item?.size)) ? Number(item.size) : null,
        modified: Number.isFinite(Number(item?.modified)) ? Number(item.modified) : null,
        created: Number.isFinite(Number(item?.created)) ? Number(item.created) : null,
        item_count: Number.isFinite(itemCount) && itemCount >= 0 ? itemCount : null,
        previewable: item?.previewable === true,
        protected: item?.protected === true
    };
}

function flattenVirtualFinderFolderTree(source) {
    const folders = [];
    const seen = new Set();

    const visit = (node, parentPath = '', depth = 0) => {
        if (typeof node === 'string') {
            const path = normalizeVirtualFinderPath(node);

            if (path && !seen.has(path)) {
                seen.add(path);
                folders.push({ path, name: path.split('/').at(-1), depth, protected: false });
            }
            return;
        }

        if (!node || typeof node !== 'object') {
            return;
        }

        if (Array.isArray(node)) {
            node.forEach((entry) => visit(entry, parentPath, depth));
            return;
        }

        const name = String(node.name || '').trim();
        const path = normalizeVirtualFinderPath(
            node.path || (name ? [parentPath, name].filter(Boolean).join('/') : parentPath)
        );
        const nodeDepth = Number.isFinite(Number(node.depth)) ? Number(node.depth) : depth;

        if (path && !seen.has(path)) {
            seen.add(path);
            folders.push({
                path,
                name: name || path.split('/').at(-1),
                depth: Math.max(0, nodeDepth),
                protected: node.protected === true
            });
        }

        const children = node.children || node.folders || node.items;

        if (Array.isArray(children)) {
            children.forEach((child) => visit(child, path, nodeDepth + 1));
        } else if (!name && !node.path) {
            Object.entries(node).forEach(([key, value]) => {
                if (['root', 'storage'].includes(key)) {
                    return;
                }

                if (Array.isArray(value)) {
                    value.forEach((child) => visit(child, parentPath, depth));
                } else if (value && typeof value === 'object') {
                    visit({ name: key, ...value }, parentPath, depth);
                }
            });
        }
    };

    visit(source);
    return folders.sort((left, right) => left.path.localeCompare(right.path, undefined, {
        sensitivity: 'base'
    }));
}

function sortVirtualFinderItems(items, controller) {
    const direction = controller.sortDirection === 'desc' ? -1 : 1;
    const field = VIRTUAL_FINDER_SORT_FIELDS.has(controller.sortField)
        ? controller.sortField
        : 'name';
    const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

    return [...items].sort((left, right) => {
        if (left.type !== right.type) {
            return left.type === 'folder' ? -1 : 1;
        }

        let comparison = 0;

        if (field === 'name') {
            comparison = collator.compare(left.name, right.name);
        } else if (field === 'type') {
            comparison = collator.compare(left.kind || left.type, right.kind || right.type);
        } else {
            const leftValue = Number(left[field]);
            const rightValue = Number(right[field]);
            const leftSafe = Number.isFinite(leftValue) ? leftValue : -1;
            const rightSafe = Number.isFinite(rightValue) ? rightValue : -1;
            comparison = leftSafe - rightSafe;
        }

        if (comparison === 0) {
            comparison = collator.compare(left.name, right.name);
        }

        return comparison * direction;
    });
}

function virtualFinderAckData(response) {
    return response?.data && typeof response.data === 'object'
        ? response.data
        : response && typeof response === 'object'
            ? response
            : {};
}

function virtualFinderRequestPending(controller, channel) {
    return Boolean(controller?.requests?.has(channel));
}

function updateVirtualFinderTransientUi(body, controller = getVirtualFinderController(body)) {
    const finder = body?.querySelector?.('.virtual-finder');

    if (!finder || !controller) {
        return;
    }

    const status = finder.querySelector('.virtual-finder-status');
    const list = finder.querySelector('.virtual-finder-list');
    const createForm = finder.querySelector('.virtual-finder-create');
    const createInput = createForm?.querySelector('.virtual-finder-create-name');
    const createError = createForm?.querySelector('.virtual-finder-create-error');
    const listLoading = controller.status.status === 'loading'
        && ['folder', 'search', 'refresh'].includes(controller.status.operation);

    if (status) {
        status.hidden = controller.status.status === 'idle' || !controller.status.message;
        status.className = `virtual-finder-status status-${controller.status.status}`;
        status.textContent = controller.status.message || '';
    }

    list?.classList.toggle('is-loading', listLoading);
    list?.setAttribute('aria-busy', listLoading ? 'true' : 'false');

    if (createForm) {
        createForm.hidden = !controller.createMode;
        createForm.setAttribute('aria-busy', virtualFinderRequestPending(controller, 'mutation') ? 'true' : 'false');
    }

    if (createInput && createInput.value !== controller.createDraft) {
        createInput.value = controller.createDraft;
    }

    if (createError) {
        createError.hidden = !controller.createError;
        createError.textContent = controller.createError;
    }

    const createSubmit = createForm?.querySelector('button[type="submit"]');

    if (createSubmit) {
        createSubmit.disabled = virtualFinderRequestPending(controller, 'mutation');
    }

    finder.querySelectorAll('[data-finder-disable-while-mutation]').forEach((control) => {
        control.disabled = virtualFinderRequestPending(controller, 'mutation');
    });
}

function setVirtualFinderRequestState(body, status = 'idle', operation = '', message = '', options = {}) {
    const controller = getVirtualFinderController(body);

    if (!controller) {
        return;
    }

    if (controller.statusTimer) {
        window.clearTimeout(controller.statusTimer);
        controller.statusTimer = null;
    }

    const token = options.token || '';
    controller.status = { status, operation, message, token };
    updateVirtualFinderTransientUi(body, controller);

    if (Number(options.autoClearMs) > 0) {
        const expected = { status, message, token };
        controller.statusTimer = window.setTimeout(() => {
            if (
                controller.status.status === expected.status
                && controller.status.message === expected.message
                && controller.status.token === expected.token
            ) {
                controller.status = { status: 'idle', operation: '', message: '', token: '' };
                updateVirtualFinderTransientUi(body, controller);
            }
        }, Number(options.autoClearMs));
    }
}

function emitVirtualFinderRequest(body, eventName, payload, options = {}) {
    const originBody = virtualFinderActiveBody(body, options.workspace);
    const controller = getVirtualFinderController(originBody);

    if (!originBody || !controller) {
        return;
    }

    const channel = String(options.channel || options.operation || 'request');
    const previousRequest = controller.requests.get(channel);

    if (previousRequest?.timer) {
        window.clearTimeout(previousRequest.timer);
    }

    const requestId = ++controller.requestSerial;
    const statusToken = `${channel}:${requestId}`;
    const request = { id: requestId, timer: null };
    controller.requests.set(channel, request);

    if (options.showStatus !== false) {
        setVirtualFinderRequestState(
            originBody,
            'loading',
            options.operation || channel,
            options.loadingMessage || 'Loading virtual folder…',
            { token: statusToken }
        );
    }

    const settleError = (response, fallbackMessage) => {
        if (controller.requests.get(channel)?.id !== requestId) {
            return;
        }

        controller.requests.delete(channel);
        const message = virtualFinderDisplayMessage(response?.message, fallbackMessage);
        options.onError?.(response || { ok: false, message });

        if (options.showStatus !== false) {
            setVirtualFinderRequestState(
                originBody,
                'error',
                options.operation || channel,
                message || 'Virtual Finder request failed.',
                { token: statusToken, autoClearMs: 7000 }
            );
        }
    };

    request.timer = window.setTimeout(() => {
        settleError(
            { ok: false, message: options.timeoutMessage || 'Virtual Finder did not respond. Try again.' },
            options.timeoutMessage || 'Virtual Finder did not respond. Try again.'
        );
    }, Number(options.timeoutMs) || 6500);

    socket.emit(
        eventName,
        virtualFinderRequestPayload(originBody, payload),
        (response = {}) => {
            if (controller.requests.get(channel)?.id !== requestId) {
                return;
            }

            window.clearTimeout(request.timer);

            if (response?.ok !== true) {
                settleError(response, options.errorMessage);
                return;
            }

            controller.requests.delete(channel);
            options.onSuccess?.(response, virtualFinderAckData(response));

            if (options.showStatus !== false) {
                const successMessage = typeof options.successMessage === 'function'
                    ? options.successMessage(response)
                    : options.successMessage;

                if (successMessage) {
                    setVirtualFinderRequestState(
                        originBody,
                        'success',
                        options.operation || channel,
                        successMessage,
                        { token: statusToken, autoClearMs: 2600 }
                    );
                } else if (controller.status.token === statusToken) {
                    setVirtualFinderRequestState(originBody, 'idle');
                }
            }
        }
    );
}

function resetVirtualFinderSearch(controller) {
    if (!controller) {
        return;
    }

    if (controller.searchTimer) {
        window.clearTimeout(controller.searchTimer);
        controller.searchTimer = null;
    }

    controller.searchDraft = '';
    controller.searchDirty = false;
    controller.pendingSearchQuery = null;
    controller.searchQuery = '';
}

function clearVirtualFinderTransientPanels(controller) {
    if (!controller) {
        return;
    }

    controller.createMode = '';
    controller.createDraft = '';
    controller.createError = '';
    controller.renamePath = '';
    controller.renameDraft = '';
    controller.renameError = '';
    controller.contextMenu = null;
    controller.dialog = null;
    controller.selectedPaths.clear();
    controller.selectionAnchor = '';
}

function openVirtualFinderPath(body, nextPath, options = {}) {
    const originBody = virtualFinderActiveBody(body, options.workspace);
    const controller = getVirtualFinderController(originBody);

    if (!originBody || !controller) {
        return;
    }

    const normalizedPath = normalizeVirtualFinderPath(nextPath);
    const currentPath = normalizeVirtualFinderPath(controller.currentPath);
    let pushedCurrent = false;

    if (options.remember !== false && normalizedPath !== currentPath) {
        controller.backStack.push(currentPath);
        controller.forwardStack = [];
        pushedCurrent = true;
    }

    if (options.resetSearch !== false) {
        resetVirtualFinderSearch(controller);
    }

    clearVirtualFinderTransientPanels(controller);
    updateVirtualFinderTransientUi(originBody, controller);

    emitVirtualFinderRequest(
        originBody,
        'virtual_finder_open_path',
        { path: normalizedPath },
        {
            channel: 'navigation',
            operation: options.operation || 'folder',
            loadingMessage: options.loadingMessage || 'Loading virtual folder…',
            errorMessage: 'Unable to load that virtual folder.',
            onError: (response) => {
                if (pushedCurrent && controller.backStack.at(-1) === currentPath) {
                    controller.backStack.pop();
                }

                options.onError?.(response);
            },
            onSuccess: (response, responseData) => {
                controller.currentPath = normalizeVirtualFinderPath(
                    responseData.current_path ?? normalizedPath
                );
                saveVirtualFinderPreferences(originBody, controller);
                options.onSuccess?.(response, responseData);
            }
        }
    );
}

function navigateVirtualFinderHistory(body, direction) {
    const controller = getVirtualFinderController(body);
    const sourceStack = direction === 'forward' ? controller?.forwardStack : controller?.backStack;
    const destinationStack = direction === 'forward' ? controller?.backStack : controller?.forwardStack;

    if (!controller || !sourceStack?.length) {
        return;
    }

    const targetPath = sourceStack.pop();
    const currentPath = normalizeVirtualFinderPath(controller.currentPath);
    destinationStack.push(currentPath);

    openVirtualFinderPath(body, targetPath, {
        remember: false,
        onError: () => {
            if (destinationStack.at(-1) === currentPath) {
                destinationStack.pop();
            }
            sourceStack.push(targetPath);
        }
    });
}

function submitVirtualFinderSearch(body, query) {
    const originBody = virtualFinderActiveBody(body);
    const controller = getVirtualFinderController(originBody);

    if (!originBody || !controller) {
        return;
    }

    if (controller.searchTimer) {
        window.clearTimeout(controller.searchTimer);
        controller.searchTimer = null;
    }

    const normalizedQuery = String(query || '').trim();
    controller.searchDraft = normalizedQuery;
    controller.searchDirty = true;
    controller.pendingSearchQuery = normalizedQuery;

    emitVirtualFinderRequest(
        originBody,
        'virtual_finder_search',
        {
            query: normalizedQuery,
            path: normalizeVirtualFinderPath(controller.currentPath)
        },
        {
            channel: 'search',
            operation: 'search',
            loadingMessage: normalizedQuery ? 'Searching this virtual location…' : 'Restoring folder contents…',
            errorMessage: 'Virtual Finder search failed.',
            onSuccess: (_response, responseData) => {
                const appliedQuery = String(responseData.search_query ?? normalizedQuery).trim();

                if (controller.searchDraft === normalizedQuery) {
                    controller.searchDraft = appliedQuery;
                    controller.searchDirty = false;
                }

                if (controller.pendingSearchQuery === normalizedQuery) {
                    controller.pendingSearchQuery = null;
                }
            },
            onError: () => {
                if (controller.pendingSearchQuery === normalizedQuery) {
                    controller.pendingSearchQuery = null;
                }
            }
        }
    );
}

function refreshVirtualFinder(body) {
    const controller = getVirtualFinderController(body);

    if (!controller) {
        return;
    }

    if (controller.searchQuery || controller.searchDraft.trim()) {
        submitVirtualFinderSearch(body, controller.searchDraft || controller.searchQuery);
        return;
    }

    openVirtualFinderPath(body, controller.currentPath, {
        remember: false,
        resetSearch: false,
        operation: 'refresh',
        loadingMessage: 'Refreshing virtual folder…'
    });
}

function validateVirtualFinderItemName(value, itemLabel = 'item') {
    const name = String(value || '').replace(/\s+/g, ' ').trim();

    if (!name) {
        return `Enter a ${itemLabel} name.`;
    }

    if (/[^a-zA-Z0-9 _.-]/.test(name)) {
        return 'Use only letters, numbers, spaces, periods, underscores, or hyphens.';
    }

    const backendCandidate = name
        .replace(/^[,.;:!?/]+|[,.;:!?/]+$/g, '')
        .replace(/^(my|the|a|an)\s+/i, '')
        .trim();

    if (!backendCandidate || backendCandidate === '.' || backendCandidate === '..') {
        return `Choose a more specific ${itemLabel} name.`;
    }

    return '';
}

function validateVirtualFinderFolderName(value) {
    return validateVirtualFinderItemName(value, 'folder');
}

function virtualFinderSelectedItems(controller, paths = null) {
    const candidates = Array.isArray(paths) ? paths : Array.from(controller?.selectedPaths || []);
    return candidates
        .map((path) => controller?.itemByPath?.get(normalizeVirtualFinderPath(path)))
        .filter(Boolean);
}

function virtualFinderActionablePaths(controller, paths = null) {
    const candidates = Array.from(new Set(
        (Array.isArray(paths) ? paths : Array.from(controller?.selectedPaths || []))
            .map(normalizeVirtualFinderPath)
            .filter(Boolean)
    ));
    const items = virtualFinderSelectedItems(controller, candidates);

    if (items.length !== candidates.length || items.some((item) => item.protected)) {
        return [];
    }

    return items.map((item) => item.path);
}

function virtualFinderPathIsProtected(controller, path) {
    const normalizedPath = normalizeVirtualFinderPath(path);

    if (!controller || !normalizedPath) {
        return false;
    }

    if (controller.itemByPath.get(normalizedPath)?.protected) {
        return true;
    }

    return (Array.isArray(controller.lastData?.sidebar) ? controller.lastData.sidebar : [])
        .some((entry) => (
            normalizeVirtualFinderPath(typeof entry === 'string' ? entry : entry?.path) === normalizedPath
            && entry?.protected === true
        ));
}

function performVirtualFinderMutation(body, eventName, payload, options = {}) {
    const controller = getVirtualFinderController(body);

    if (!controller || virtualFinderRequestPending(controller, 'mutation')) {
        return;
    }

    emitVirtualFinderRequest(
        body,
        eventName,
        {
            ...payload,
            current_path: normalizeVirtualFinderPath(controller.currentPath),
            query: String(controller.searchQuery || '').trim()
        },
        {
            channel: 'mutation',
            operation: options.operation || 'mutation',
            loadingMessage: options.loadingMessage || 'Updating virtual storage…',
            errorMessage: options.errorMessage || 'Virtual Finder operation failed.',
            successMessage: (response) => virtualFinderDisplayMessage(
                response?.message,
                options.successMessage || 'Virtual storage updated.'
            ),
            onSuccess: (response, responseData) => {
                options.onSuccess?.(response, responseData);
                renderVirtualFinderWidget(controller.lastData, body);
            },
            onError: (response) => {
                options.onError?.(response, virtualFinderAckData(response));
            }
        }
    );
}

function virtualFinderTransferIssue(controller, paths, destination, mode = 'move') {
    const normalizedPaths = Array.from(new Set((Array.isArray(paths) ? paths : [])
        .map(normalizeVirtualFinderPath)
        .filter(Boolean)));
    const normalizedDestination = normalizeVirtualFinderPath(destination);
    const sourceSet = new Set(normalizedPaths);

    for (const path of normalizedPaths) {
        if (normalizedDestination === path || normalizedDestination.startsWith(`${path}/`)) {
            return 'A folder cannot be transferred into itself.';
        }

        const parentPath = path.split('/').slice(0, -1).join('/');

        if (mode === 'move' && parentPath === normalizedDestination) {
            return 'That item is already in this virtual folder.';
        }

        const name = path.split('/').at(-1) || '';
        const targetPath = normalizeVirtualFinderPath(
            [normalizedDestination, name].filter(Boolean).join('/')
        );

        if (
            controller?.itemByPath?.has(targetPath)
            && (mode === 'copy' || !sourceSet.has(targetPath))
        ) {
            return `The destination already contains ${name}.`;
        }
    }

    return '';
}

function transferVirtualFinderItems(body, paths, destination, mode = 'move', options = {}) {
    const controller = getVirtualFinderController(body);
    const normalizedPaths = Array.from(new Set((Array.isArray(paths) ? paths : [])
        .map(normalizeVirtualFinderPath)
        .filter(Boolean)));
    const normalizedDestination = normalizeVirtualFinderPath(destination);

    if (!controller || !normalizedPaths.length) {
        return;
    }

    const transferMode = mode === 'copy' ? 'copy' : 'move';
    const transferIssue = virtualFinderTransferIssue(
        controller,
        normalizedPaths,
        normalizedDestination,
        transferMode
    );

    if (transferIssue) {
        setVirtualFinderRequestState(
            body,
            'error',
            'transfer',
            transferIssue,
            { autoClearMs: 5000 }
        );
        return;
    }

    performVirtualFinderMutation(
        body,
        'virtual_finder_transfer',
        {
            paths: normalizedPaths,
            destination: normalizedDestination,
            mode: transferMode
        },
        {
            operation: 'transfer',
            loadingMessage: transferMode === 'copy' ? 'Copying virtual items…' : 'Moving virtual items…',
            errorMessage: transferMode === 'copy' ? 'Unable to copy those items.' : 'Unable to move those items.',
            successMessage: transferMode === 'copy' ? 'Items copied.' : 'Items moved.',
            onSuccess: (response, responseData) => {
                if (controller.clipboard.mode === 'cut' || transferMode === 'move') {
                    controller.clipboard = { mode: '', paths: [] };
                }
                controller.selectedPaths.clear();
                controller.dialog = null;
                options.onSuccess?.(response, responseData);
            },
            onError: options.onError
        }
    );
}

function safeVirtualFinderImageSource(preview) {
    const direct = String(preview?.data_url || preview?.url || '');

    if (/^data:image\/(?:png|jpeg|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(direct) && direct.length <= 8000000) {
        return direct.replace(/\s+/g, '');
    }

    const mime = String(preview?.mime_type || preview?.mime || '').toLowerCase();
    const data = String(preview?.image_data || preview?.base64 || '');

    if (/^image\/(?:png|jpeg|gif|webp)$/.test(mime) && /^[a-z0-9+/=\s]+$/i.test(data) && data.length <= 8000000) {
        return `data:${mime};base64,${data.replace(/\s+/g, '')}`;
    }

    return '';
}

function openVirtualFinderPreview(body, path, options = {}) {
    const controller = getVirtualFinderController(body);
    const normalizedPath = normalizeVirtualFinderPath(path);
    const item = controller?.itemByPath?.get(normalizedPath);

    if (!controller || !normalizedPath || !item || item.type === 'folder') {
        return;
    }

    controller.previewOpen = true;
    controller.previewPath = normalizedPath;
    controller.previewError = '';
    controller.previewData = {
        ...item,
        preview_kind: item.previewable ? 'metadata' : 'unavailable'
    };
    controller.previewStatus = item.previewable ? 'loading' : 'ready';
    saveVirtualFinderPreferences(body, controller);
    renderVirtualFinderWidget(controller.lastData, body);

    if (!item.previewable) {
        return;
    }

    emitVirtualFinderRequest(
        body,
        'virtual_finder_preview',
        { path: normalizedPath },
        {
            channel: 'preview',
            operation: 'preview',
            showStatus: false,
            timeoutMs: 7000,
            onSuccess: (_response, responseData) => {
                if (controller.previewPath !== normalizedPath) {
                    return;
                }

                const preview = responseData.preview && typeof responseData.preview === 'object'
                    ? responseData.preview
                    : responseData;
                const textValue = preview.text ?? preview.content;
                const imageSource = safeVirtualFinderImageSource(preview);
                controller.previewData = {
                    ...item,
                    ...preview,
                    text: textValue === undefined || textValue === null
                        ? ''
                        : String(textValue).slice(0, 200000),
                    imageSource,
                    preview_kind: imageSource
                        ? 'image'
                        : textValue !== undefined && textValue !== null
                            ? 'text'
                            : 'metadata'
                };
                controller.previewStatus = 'ready';
                controller.previewError = '';
                controller.previewLoadedPath = normalizedPath;
                renderVirtualFinderWidget(controller.lastData, body);
            },
            onError: (response) => {
                if (controller.previewPath !== normalizedPath) {
                    return;
                }

                controller.previewStatus = 'error';
                controller.previewError = virtualFinderDisplayMessage(
                    response?.message,
                    'Preview unavailable.'
                );
                renderVirtualFinderWidget(controller.lastData, body);
            }
        }
    );
}

function closeVirtualFinderPreview(body) {
    const controller = getVirtualFinderController(body);

    if (!controller) {
        return;
    }

    controller.previewOpen = false;
    controller.previewPath = '';
    controller.previewStatus = 'idle';
    controller.previewData = null;
    controller.previewError = '';
    saveVirtualFinderPreferences(body, controller);
    renderVirtualFinderWidget(controller.lastData, body);
}

function beginVirtualFinderCreate(body, mode) {
    const controller = getVirtualFinderController(body);

    if (!controller) {
        return;
    }

    const nextMode = mode === 'file' ? 'file' : 'folder';
    controller.createMode = controller.createMode === nextMode ? '' : nextMode;
    controller.createDraft = controller.createMode
        ? nextMode === 'file' ? 'Untitled File' : 'Untitled Folder'
        : '';
    controller.createFileType = controller.createFileType || 'txt';
    controller.createError = '';
    controller.renamePath = '';
    controller.contextMenu = null;
    controller.pendingFocus = controller.createMode ? { key: 'create-name', select: true } : null;
    renderVirtualFinderWidget(controller.lastData, body);
}

function beginVirtualFinderRename(body, path) {
    const controller = getVirtualFinderController(body);
    const item = controller?.itemByPath?.get(normalizeVirtualFinderPath(path));

    if (!controller || !item || item.protected) {
        return;
    }

    controller.renamePath = item.path;
    controller.renameDraft = item.name;
    controller.renameError = '';
    controller.createMode = '';
    controller.contextMenu = null;
    const extensionIndex = item.type === 'file' ? item.name.lastIndexOf('.') : -1;
    controller.pendingFocus = {
        key: `rename:${item.path}`,
        start: 0,
        end: extensionIndex > 0 ? extensionIndex : item.name.length
    };
    renderVirtualFinderWidget(controller.lastData, body);
}

function openVirtualFinderDeleteDialog(body, paths) {
    const controller = getVirtualFinderController(body);
    const items = virtualFinderSelectedItems(controller, paths);

    if (!controller || !items.length) {
        return;
    }

    if (items.some((item) => item.protected)) {
        setVirtualFinderRequestState(
            body,
            'error',
            'delete',
            'Protected virtual locations cannot be deleted.',
            { autoClearMs: 5000 }
        );
        return;
    }

    const confirmNonEmpty = items.some((item) => item.type === 'folder' && Number(item.item_count) > 0);
    controller.dialog = {
        type: 'delete',
        paths: items.map((item) => item.path),
        names: items.map((item) => item.name),
        confirmNonEmpty,
        confirmText: '',
        error: ''
    };
    controller.contextMenu = null;
    controller.pendingFocus = { key: confirmNonEmpty ? 'dialog-confirm-text' : 'dialog-confirm' };
    renderVirtualFinderWidget(controller.lastData, body);
}

function openVirtualFinderMoveDialog(body, paths) {
    const controller = getVirtualFinderController(body);
    const actionablePaths = virtualFinderActionablePaths(controller, paths);

    if (!controller || !actionablePaths.length) {
        return;
    }

    const destinations = virtualFinderMoveDestinations(controller, actionablePaths);

    if (!destinations.length) {
        setVirtualFinderRequestState(
            body,
            'error',
            'transfer',
            'No valid virtual destination is available.',
            { autoClearMs: 5000 }
        );
        return;
    }

    controller.dialog = {
        type: 'move',
        paths: actionablePaths,
        destination: destinations[0].path,
        error: ''
    };
    controller.contextMenu = null;
    controller.pendingFocus = { key: 'dialog-destination' };
    renderVirtualFinderWidget(controller.lastData, body);
}

function openVirtualFinderInfoDialog(body, path) {
    const controller = getVirtualFinderController(body);
    const item = controller?.itemByPath?.get(normalizeVirtualFinderPath(path));

    if (!controller || !item) {
        return;
    }

    controller.dialog = { type: 'info', path: item.path, error: '' };
    controller.contextMenu = null;
    controller.pendingFocus = { key: 'dialog-close' };
    renderVirtualFinderWidget(controller.lastData, body);
}

function closeVirtualFinderDialog(body) {
    const controller = getVirtualFinderController(body);
    const returnPath = controller?.dialog?.path || controller?.dialog?.paths?.[0] || controller?.focusedPath;

    if (!controller) {
        return;
    }

    controller.dialog = null;
    controller.pendingFocus = returnPath ? { path: returnPath } : null;
    renderVirtualFinderWidget(controller.lastData, body);
}

function addVirtualFinderFavorite(body, path) {
    const controller = getVirtualFinderController(body);
    const normalizedPath = normalizeVirtualFinderPath(path);

    if (
        !controller
        || !normalizedPath
        || virtualFinderPathIsProtected(controller, normalizedPath)
        || controller.favorites.some((favorite) => favorite.path === normalizedPath)
    ) {
        return;
    }

    const item = controller.itemByPath.get(normalizedPath);
    controller.favorites.push({
        path: normalizedPath,
        name: item?.name || normalizedPath.split('/').at(-1)
    });
    controller.favorites = normalizeVirtualFinderFavorites(controller.favorites);
    controller.favoritesCollapsed = false;
    saveVirtualFinderPreferences(body, controller);
    renderVirtualFinderWidget(controller.lastData, body);
}

function updateVirtualFinderFavorite(body, path, action) {
    const controller = getVirtualFinderController(body);
    const normalizedPath = normalizeVirtualFinderPath(path);
    const index = controller?.favorites?.findIndex((favorite) => favorite.path === normalizedPath) ?? -1;

    if (!controller || index < 0) {
        return;
    }

    if (action === 'remove') {
        controller.favorites.splice(index, 1);
    } else if (action === 'up' && index > 0) {
        [controller.favorites[index - 1], controller.favorites[index]] = [
            controller.favorites[index],
            controller.favorites[index - 1]
        ];
    } else if (action === 'down' && index < controller.favorites.length - 1) {
        [controller.favorites[index], controller.favorites[index + 1]] = [
            controller.favorites[index + 1],
            controller.favorites[index]
        ];
    }

    saveVirtualFinderPreferences(body, controller);
    renderVirtualFinderWidget(controller.lastData, body);
}

function reorderVirtualFinderFavorite(body, sourcePath, targetPath) {
    const controller = getVirtualFinderController(body);
    const source = normalizeVirtualFinderPath(sourcePath);
    const target = normalizeVirtualFinderPath(targetPath);
    const sourceIndex = controller?.favorites?.findIndex((favorite) => favorite.path === source) ?? -1;
    const targetIndex = controller?.favorites?.findIndex((favorite) => favorite.path === target) ?? -1;

    if (!controller || sourceIndex < 0 || targetIndex < 0 || sourceIndex === targetIndex) {
        return;
    }

    const [favorite] = controller.favorites.splice(sourceIndex, 1);
    controller.favorites.splice(targetIndex, 0, favorite);
    controller.favoriteDragPath = '';
    saveVirtualFinderPreferences(body, controller);
    renderVirtualFinderWidget(controller.lastData, body);
}

function updateVirtualFinderSelectionUi(body, controller = getVirtualFinderController(body)) {
    const root = body?.querySelector?.('.virtual-finder');

    if (!root || !controller) {
        return;
    }

    root.querySelectorAll('.virtual-finder-row').forEach((row) => {
        const selected = controller.selectedPaths.has(row.dataset.path || '');
        row.classList.toggle('selected', selected);
        row.setAttribute('aria-selected', selected ? 'true' : 'false');
    });

    const selectionHtml = renderVirtualFinderSelectionBar(controller);
    const selectionBar = root.querySelector('.virtual-finder-selection-bar');

    if (selectionBar && selectionHtml) {
        selectionBar.outerHTML = selectionHtml;
    } else if (selectionBar) {
        selectionBar.remove();
    } else if (selectionHtml) {
        root.querySelector('.virtual-finder-content')?.insertAdjacentHTML('beforebegin', selectionHtml);
    }

    const footerCount = root.querySelector('.virtual-finder-footer > span:first-child');

    if (footerCount) {
        const itemCountLabel = `${controller.items.length} ${controller.items.length === 1 ? 'item' : 'items'}`;
        footerCount.textContent = `${itemCountLabel}${controller.selectedPaths.size ? ` · ${controller.selectedPaths.size} selected` : ''}`;
    }
}

function setVirtualFinderSelection(body, path, event = {}) {
    const controller = getVirtualFinderController(body);
    const normalizedPath = normalizeVirtualFinderPath(path);
    const visiblePaths = controller?.items?.map((item) => item.path) || [];

    if (!controller || !normalizedPath || !controller.itemByPath.has(normalizedPath)) {
        return;
    }

    if (event.shiftKey && controller.selectionAnchor) {
        const anchorIndex = visiblePaths.indexOf(controller.selectionAnchor);
        const targetIndex = visiblePaths.indexOf(normalizedPath);

        if (anchorIndex >= 0 && targetIndex >= 0) {
            if (!event.metaKey && !event.ctrlKey) {
                controller.selectedPaths.clear();
            }

            const start = Math.min(anchorIndex, targetIndex);
            const end = Math.max(anchorIndex, targetIndex);
            visiblePaths.slice(start, end + 1).forEach((itemPath) => controller.selectedPaths.add(itemPath));
        }
    } else if (event.metaKey || event.ctrlKey) {
        if (controller.selectedPaths.has(normalizedPath)) {
            controller.selectedPaths.delete(normalizedPath);
        } else {
            controller.selectedPaths.add(normalizedPath);
        }
        controller.selectionAnchor = normalizedPath;
    } else {
        controller.selectedPaths.clear();
        controller.selectedPaths.add(normalizedPath);
        controller.selectionAnchor = normalizedPath;
    }

    controller.focusedPath = normalizedPath;
    controller.pendingFocus = null;
    updateVirtualFinderSelectionUi(body, controller);
    focusVirtualFinderItem(body, normalizedPath);
}

function selectAllVirtualFinderItems(body) {
    const controller = getVirtualFinderController(body);

    if (!controller) {
        return;
    }

    controller.selectedPaths = new Set(controller.items.map((item) => item.path));
    controller.selectionAnchor = controller.items[0]?.path || '';
    controller.focusedPath = controller.items[0]?.path || '';
    controller.pendingFocus = null;
    updateVirtualFinderSelectionUi(body, controller);
    if (controller.focusedPath) focusVirtualFinderItem(body, controller.focusedPath);
}

function copyVirtualFinderSelection(body, mode = 'copy', paths = null) {
    const controller = getVirtualFinderController(body);
    const actionablePaths = virtualFinderActionablePaths(controller, paths);

    if (!controller || !actionablePaths.length) {
        return;
    }

    controller.clipboard = {
        mode: mode === 'cut' ? 'cut' : 'copy',
        paths: actionablePaths
    };
    controller.contextMenu = null;
    setVirtualFinderRequestState(
        body,
        'success',
        'clipboard',
        `${actionablePaths.length} ${actionablePaths.length === 1 ? 'item' : 'items'} ready to ${controller.clipboard.mode}.`,
        { autoClearMs: 2600 }
    );
    renderVirtualFinderWidget(controller.lastData, body);
}

function virtualFinderPathsForAction(controller, explicitPath = '') {
    const normalizedExplicit = normalizeVirtualFinderPath(explicitPath);

    if (normalizedExplicit && !controller.selectedPaths.has(normalizedExplicit)) {
        return [normalizedExplicit];
    }

    if (controller.selectedPaths.size) {
        return Array.from(controller.selectedPaths);
    }

    return normalizedExplicit ? [normalizedExplicit] : [];
}

function executeVirtualFinderAction(body, action, explicitPath = '', destination = '') {
    const controller = getVirtualFinderController(body);

    if (!controller) {
        return;
    }

    if (action === 'new-folder') {
        beginVirtualFinderCreate(body, 'folder');
        return;
    }

    if (action === 'new-file') {
        beginVirtualFinderCreate(body, 'file');
        return;
    }

    if (action === 'refresh') {
        refreshVirtualFinder(body);
        return;
    }

    if (action === 'view-list' || action === 'view-grid') {
        controller.viewMode = action === 'view-grid' ? 'grid' : 'list';
        controller.contextMenu = null;
        saveVirtualFinderPreferences(body, controller);
        renderVirtualFinderWidget(controller.lastData, body);
        return;
    }

    const normalizedExplicit = normalizeVirtualFinderPath(explicitPath);
    const paths = virtualFinderPathsForAction(controller, normalizedExplicit);
    const firstPath = normalizedExplicit || paths[0];
    const firstItem = controller.itemByPath.get(firstPath);

    if (action === 'open' && firstItem?.type === 'folder') {
        openVirtualFinderPath(body, firstItem.path);
    } else if (action === 'preview' && firstItem?.type === 'file') {
        openVirtualFinderPreview(body, firstItem.path);
    } else if (action === 'info' && firstItem) {
        openVirtualFinderInfoDialog(body, firstItem.path);
    } else if (action === 'rename' && firstItem && !firstItem.protected) {
        beginVirtualFinderRename(body, firstItem.path);
    } else if (action === 'delete') {
        openVirtualFinderDeleteDialog(body, paths);
    } else if (action === 'move') {
        openVirtualFinderMoveDialog(body, paths);
    } else if (action === 'copy') {
        copyVirtualFinderSelection(body, 'copy', paths);
    } else if (action === 'cut') {
        copyVirtualFinderSelection(body, 'cut', paths);
    } else if (action === 'paste' && controller.clipboard.paths.length) {
        transferVirtualFinderItems(
            body,
            controller.clipboard.paths,
            normalizeVirtualFinderPath(destination || controller.currentPath),
            controller.clipboard.mode
        );
    } else if (action === 'favorite' && firstItem?.type === 'folder') {
        addVirtualFinderFavorite(body, firstItem.path);
    } else if (action === 'clear-selection') {
        controller.selectedPaths.clear();
        controller.selectionAnchor = '';
        controller.contextMenu = null;
        updateVirtualFinderSelectionUi(body, controller);
    }
}

function captureVirtualFinderFocus(body) {
    const active = document.activeElement;

    if (!active || !body?.contains(active)) {
        return null;
    }

    const row = active.closest?.('.virtual-finder-row');
    const key = active.dataset?.finderFocusKey || '';
    const focus = {
        key,
        path: row?.dataset?.path || '',
        start: null,
        end: null
    };

    if (typeof active.selectionStart === 'number' && typeof active.selectionEnd === 'number') {
        focus.start = active.selectionStart;
        focus.end = active.selectionEnd;
    }

    return focus;
}

function restoreVirtualFinderFocus(body, controller, capturedFocus) {
    const request = controller.pendingFocus || capturedFocus;
    controller.pendingFocus = null;

    if (!request) {
        return;
    }

    let target = null;

    if (request.key) {
        target = body.querySelector(`[data-finder-focus-key="${CSS.escape(request.key)}"]`);
    }

    if (!target && request.path) {
        target = body.querySelector(`.virtual-finder-row[data-path="${CSS.escape(request.path)}"]`);
    }

    if (!target || target.closest('[hidden]')) {
        return;
    }

    target.focus({ preventScroll: true });

    if (request.select && typeof target.select === 'function') {
        target.select();
        return;
    }

    if (
        Number.isInteger(request.start)
        && Number.isInteger(request.end)
        && typeof target.setSelectionRange === 'function'
    ) {
        const length = String(target.value || '').length;
        target.setSelectionRange(Math.min(length, request.start), Math.min(length, request.end));
    }
}

function renderVirtualFinderBreadcrumb(controller) {
    const pathParts = controller.currentPath ? controller.currentPath.split('/') : [];
    const segments = [
        { label: controller.rootLabel, path: '' },
        ...pathParts.map((part, index) => ({
            label: part,
            path: pathParts.slice(0, index + 1).join('/')
        }))
    ];

    return segments.map((segment, index) => `
        ${index ? '<span class="virtual-finder-breadcrumb-separator" aria-hidden="true">/</span>' : ''}
        <button
            type="button"
            class="virtual-finder-breadcrumb-segment ${index === segments.length - 1 ? 'current' : ''}"
            data-finder-path="${escapeHtml(segment.path)}"
            ${index === segments.length - 1 ? 'aria-current="page"' : ''}
            title="Open ${escapeHtml(segment.label)}"
        >${escapeHtml(segment.label)}</button>
    `).join('');
}

function renderVirtualFinderStorage(storage, itemCount) {
    const source = storage && typeof storage === 'object' ? storage : {};
    const used = Number(source.used_bytes ?? source.total_bytes ?? source.bytes ?? source.size);
    const total = Number(source.capacity_bytes ?? source.capacity);
    const count = Number(source.item_count ?? source.items ?? itemCount);
    const percentage = Number.isFinite(used) && Number.isFinite(total) && total > 0
        ? Math.max(0, Math.min(100, Math.round((used / total) * 100)))
        : 0;
    const label = Number.isFinite(used)
        ? `${formatVirtualFinderSize(used, 'file')} used${Number.isFinite(count) ? ` · ${count} items` : ''}`
        : `${Number.isFinite(count) ? count : itemCount} virtual items`;
    const meter = Number.isFinite(total) && total > 0
        ? `<div class="virtual-finder-storage-meter" style="--finder-storage-percent:${percentage}%" aria-hidden="true"><span></span></div>`
        : '';

    return `
        <div class="virtual-finder-storage-badge" title="${escapeHtml(label)}">
            <span></span> ${escapeHtml(label)}
            ${meter}
        </div>
    `;
}

function renderVirtualFinderSidebar(controller, sidebar) {
    const activeTopLocation = controller.currentPath.split('/')[0] || '';
    const locations = (Array.isArray(sidebar) ? sidebar : []).map((entry) => {
        const name = String(typeof entry === 'string' ? entry : entry?.name || 'Folder');
        const path = normalizeVirtualFinderPath(typeof entry === 'string' ? entry : entry?.path);
        return { name, path };
    }).filter((entry) => entry.path);
    const sidebarOptions = locations.map((entry) => `
        <option value="${escapeHtml(entry.path)}" ${entry.path === activeTopLocation ? 'selected' : ''}>${escapeHtml(entry.name)}</option>
    `).join('');
    const locationHtml = locations.map((entry) => {
        const selected = entry.path === activeTopLocation;
        return `
            <button
                class="virtual-finder-location ${selected ? 'selected' : ''}"
                type="button"
                data-finder-location
                data-finder-drop-path="${escapeHtml(entry.path)}"
                data-path="${escapeHtml(entry.path)}"
                ${selected ? 'aria-current="location"' : ''}
                title="Open ${escapeHtml(entry.name)}"
            >
                <span class="virtual-finder-location-icon" aria-hidden="true">${virtualFinderLocationIcon(entry.name)}</span>
                <span>${escapeHtml(entry.name)}</span>
            </button>
        `;
    }).join('');
    const favoriteHtml = controller.favorites.map((favorite, index) => `
        <div class="virtual-finder-favorite" data-path="${escapeHtml(favorite.path)}" draggable="true">
            <button
                class="virtual-finder-location"
                type="button"
                data-finder-favorite-open
                data-finder-drop-path="${escapeHtml(favorite.path)}"
                data-path="${escapeHtml(favorite.path)}"
                title="Open ${escapeHtml(favorite.name)}"
            >
                <span class="virtual-finder-location-icon" aria-hidden="true">☆</span>
                <span>${escapeHtml(favorite.name)}</span>
            </button>
            <span class="virtual-finder-favorite-actions">
                <button type="button" data-finder-favorite-action="up" data-path="${escapeHtml(favorite.path)}" ${index === 0 ? 'disabled' : ''} title="Move favorite up">↑</button>
                <button type="button" data-finder-favorite-action="down" data-path="${escapeHtml(favorite.path)}" ${index === controller.favorites.length - 1 ? 'disabled' : ''} title="Move favorite down">↓</button>
                <button type="button" data-finder-favorite-action="remove" data-path="${escapeHtml(favorite.path)}" title="Remove favorite">×</button>
            </span>
        </div>
    `).join('');
    const currentIsFavorite = controller.favorites.some((favorite) => favorite.path === controller.currentPath);

    return `
        <aside class="virtual-finder-sidebar" aria-label="Virtual locations">
            <div class="virtual-finder-sidebar-head">
                <div>
                    <div class="virtual-finder-brand">Virtual Finder</div>
                    <div class="virtual-finder-sidebar-subtitle">Secure storage</div>
                </div>
                <span class="virtual-finder-root-icon" aria-hidden="true">◈</span>
            </div>
            <label class="virtual-finder-location-select">
                <span>Location</span>
                <select aria-label="Virtual Finder location" data-finder-focus-key="location-select">
                    <option value="">Virtual Finder</option>
                    ${sidebarOptions}
                </select>
            </label>
            <section class="virtual-finder-sidebar-section">
                <div class="virtual-finder-sidebar-section-head"><span>LOCATIONS</span></div>
                <nav class="virtual-finder-locations" aria-label="Virtual locations">${locationHtml}</nav>
            </section>
            <section class="virtual-finder-sidebar-section ${controller.favoritesCollapsed ? 'is-collapsed' : ''}">
                <div class="virtual-finder-sidebar-section-head">
                    <button class="virtual-finder-sidebar-section-toggle" type="button" data-finder-toggle-favorites aria-expanded="${controller.favoritesCollapsed ? 'false' : 'true'}">
                        <span aria-hidden="true">${controller.favoritesCollapsed ? '▸' : '▾'}</span> FAVORITES
                    </button>
                    ${controller.currentPath
                        && !currentIsFavorite
                        && !virtualFinderPathIsProtected(controller, controller.currentPath)
                        ? `<button class="virtual-finder-favorite-add" type="button" data-finder-add-current-favorite title="Add current folder to favorites">+</button>`
                        : ''}
                </div>
                <nav class="virtual-finder-favorites" aria-label="Favorite virtual folders">
                    ${favoriteHtml || '<span class="virtual-finder-sidebar-subtitle">No custom favorites</span>'}
                </nav>
            </section>
            ${renderVirtualFinderStorage(controller.storage, controller.items.length)}
        </aside>
    `;
}

function virtualFinderSortHeader(controller, field, label, className) {
    const active = controller.sortField === field;
    const ariaSort = active ? (controller.sortDirection === 'desc' ? 'descending' : 'ascending') : 'none';
    return `
        <button class="${className} virtual-finder-sort-header" type="button" role="columnheader" data-finder-sort="${field}" aria-sort="${ariaSort}">
            ${label}${active ? `<span aria-hidden="true">${controller.sortDirection === 'desc' ? '↓' : '↑'}</span>` : ''}
        </button>
    `;
}

function renderVirtualFinderToolbar(controller) {
    const sortOptions = [
        ['name', 'Name'],
        ['type', 'Type'],
        ['modified', 'Modified'],
        ['size', 'Size'],
        ['created', 'Created']
    ].map(([value, label]) => `
        <option value="${value}" ${controller.sortField === value ? 'selected' : ''}>${label}</option>
    `).join('');

    return `
        <header class="virtual-finder-toolbar">
            <div class="virtual-finder-nav-actions" aria-label="Folder navigation">
                <button class="virtual-finder-control" type="button" data-finder-nav="back" ${controller.backStack.length ? '' : 'disabled'} title="Back"><span aria-hidden="true">←</span> Back</button>
                <button class="virtual-finder-control virtual-finder-forward" type="button" data-finder-nav="forward" ${controller.forwardStack.length ? '' : 'disabled'} title="Forward"><span aria-hidden="true">→</span></button>
                <button class="virtual-finder-control" type="button" data-finder-nav="up" data-path="${escapeHtml(controller.parentPath)}" ${controller.currentPath ? '' : 'disabled'} title="Up one folder"><span aria-hidden="true">↑</span> Up</button>
                <button class="virtual-finder-control virtual-finder-refresh" type="button" data-finder-nav="refresh" title="Refresh"><span aria-hidden="true">↻</span></button>
            </div>
            <nav class="virtual-finder-breadcrumb" aria-label="Current virtual path">${renderVirtualFinderBreadcrumb(controller)}</nav>
            <div class="virtual-finder-toolbar-actions">
                <button class="virtual-finder-control virtual-finder-sidebar-toggle" type="button" data-finder-toggle-sidebar aria-pressed="${controller.sidebarCollapsed ? 'true' : 'false'}" title="Toggle sidebar">☰</button>
                <button class="virtual-finder-new-folder" type="button" data-finder-create="folder" data-finder-focus-key="new-folder" aria-expanded="${controller.createMode === 'folder' ? 'true' : 'false'}"><span aria-hidden="true">+</span> Folder</button>
                <button class="virtual-finder-new-file" type="button" data-finder-create="file" data-finder-focus-key="new-file" aria-expanded="${controller.createMode === 'file' ? 'true' : 'false'}"><span aria-hidden="true">+</span> File</button>
                ${controller.clipboard.paths.length
                    ? `<button class="virtual-finder-control virtual-finder-paste" type="button" data-finder-action="paste" title="Paste ${controller.clipboard.paths.length} item(s)">Paste</button>`
                    : ''}
            </div>
            <span class="virtual-finder-view-controls" aria-label="View and sort controls">
                <button class="virtual-finder-view-toggle ${controller.viewMode === 'list' ? 'selected' : ''}" type="button" data-finder-view="list" aria-pressed="${controller.viewMode === 'list' ? 'true' : 'false'}" title="List view">☷</button>
                <button class="virtual-finder-view-toggle ${controller.viewMode === 'grid' ? 'selected' : ''}" type="button" data-finder-view="grid" aria-pressed="${controller.viewMode === 'grid' ? 'true' : 'false'}" title="Grid view">▦</button>
                <select class="virtual-finder-sort-field" aria-label="Sort items" data-finder-focus-key="sort-field">${sortOptions}</select>
                <button class="virtual-finder-sort-direction" type="button" data-finder-sort-direction title="Reverse sort">${controller.sortDirection === 'desc' ? '↓' : '↑'}</button>
            </span>
            <form class="virtual-finder-search" role="search">
                <span class="virtual-finder-search-icon" aria-hidden="true">⌕</span>
                <input
                    type="search"
                    value="${escapeHtml(controller.searchDraft)}"
                    placeholder="Search this virtual location…"
                    aria-label="Search current virtual location"
                    autocomplete="off"
                    spellcheck="false"
                    data-finder-focus-key="search"
                >
                <button class="virtual-finder-search-clear" type="button" aria-label="Clear search" title="Clear search" ${controller.searchDraft ? '' : 'hidden'}>×</button>
                <button class="virtual-finder-search-submit" type="submit">Search</button>
            </form>
        </header>
    `;
}

function renderVirtualFinderCreateForm(controller) {
    if (!controller.createMode) {
        return '<form class="virtual-finder-create" hidden></form>';
    }

    const isFile = controller.createMode === 'file';
    const fileTypes = VIRTUAL_FINDER_FILE_TYPES.map((type) => `
        <option value="${type.value}" ${controller.createFileType === type.value ? 'selected' : ''}>${type.label}</option>
    `).join('');

    return `
        <form class="virtual-finder-create mode-${controller.createMode}" novalidate>
            <span class="virtual-finder-create-icon" aria-hidden="true">+</span>
            <input
                class="virtual-finder-create-name"
                type="text"
                value="${escapeHtml(controller.createDraft)}"
                placeholder="${isFile ? 'File name' : 'Folder name'}"
                aria-label="New ${isFile ? 'file' : 'folder'} name"
                autocomplete="off"
                maxlength="120"
                data-finder-focus-key="create-name"
            >
            ${isFile
                ? `<select class="virtual-finder-create-type" aria-label="New file type" data-finder-focus-key="create-type">${fileTypes}</select>`
                : ''}
            <button type="submit" data-finder-disable-while-mutation>Create</button>
            <button type="button" data-finder-create-cancel>Cancel</button>
            <div class="virtual-finder-create-error" role="alert" ${controller.createError ? '' : 'hidden'}>${escapeHtml(controller.createError)}</div>
        </form>
    `;
}

function renderVirtualFinderRename(controller, item) {
    if (controller.renamePath !== item.path) {
        const folderCount = item.type === 'folder' && Number.isFinite(item.item_count)
            ? `${item.item_count} ${item.item_count === 1 ? 'item' : 'items'}`
            : '';
        const resultParent = controller.searchQuery && item.path.includes('/')
            ? item.path.split('/').slice(0, -1).join(' / ')
            : '';
        return `
            <span class="virtual-finder-name-copy">
                <strong title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</strong>
                ${resultParent || folderCount
                    ? `<small title="${escapeHtml(resultParent || folderCount)}">${escapeHtml(resultParent || folderCount)}</small>`
                    : ''}
            </span>
        `;
    }

    return `
        <form class="virtual-finder-rename-form" data-path="${escapeHtml(item.path)}" novalidate>
            <input
                class="virtual-finder-rename-input"
                type="text"
                value="${escapeHtml(controller.renameDraft)}"
                aria-label="Rename ${escapeHtml(item.name)}"
                autocomplete="off"
                maxlength="120"
                data-finder-focus-key="rename:${escapeHtml(item.path)}"
            >
            <span class="virtual-finder-rename-actions">
                <button type="submit" title="Rename">✓</button>
                <button type="button" data-finder-rename-cancel title="Cancel">×</button>
            </span>
            ${controller.renameError ? `<span class="virtual-finder-create-error" role="alert">${escapeHtml(controller.renameError)}</span>` : ''}
        </form>
    `;
}

function virtualFinderKindLabel(item) {
    if (item?.type === 'folder') {
        return 'Folder';
    }

    const kind = String(item?.kind || '').trim();

    if (!kind || kind === 'file') {
        return 'File';
    }

    return kind.charAt(0).toUpperCase() + kind.slice(1);
}

function renderVirtualFinderRows(controller) {
    return controller.items.map((item, index) => {
        const selected = controller.selectedPaths.has(item.path);
        const focused = controller.focusedPath === item.path || (!controller.focusedPath && index === 0);
        const cut = controller.clipboard.mode === 'cut' && controller.clipboard.paths.includes(item.path);
        const modified = formatVirtualFinderModified(item.modified);
        // `kind` arrives lowercase from the backend; title-case it so the Type
        // column does not mix "Folder" with "text".
        const rawKind = item.kind && item.kind !== 'file' ? String(item.kind) : '';
        const typeLabel = item.type === 'folder'
            ? 'Folder'
            : rawKind
                ? rawKind.charAt(0).toUpperCase() + rawKind.slice(1)
                : 'File';

        return `
            <div
                class="virtual-finder-row ${item.type} ${selected ? 'selected' : ''} ${focused ? 'focused' : ''} ${cut ? 'cut' : ''}"
                role="row"
                tabindex="${focused ? '0' : '-1'}"
                draggable="${item.protected ? 'false' : 'true'}"
                data-type="${escapeHtml(item.type)}"
                data-kind="${escapeHtml(item.kind)}"
                data-path="${escapeHtml(item.path)}"
                data-item-index="${index}"
                data-protected="${item.protected ? 'true' : 'false'}"
                ${item.type === 'folder' ? `data-finder-drop-path="${escapeHtml(item.path)}"` : ''}
                aria-selected="${selected ? 'true' : 'false'}"
                aria-label="${escapeHtml(`${item.type === 'folder' ? 'Folder' : 'File'} ${item.name}`)}"
            >
                <div class="virtual-finder-name-cell finder-col-name" role="cell">
                    <span class="virtual-finder-item-icon" aria-hidden="true">${escapeHtml(virtualFinderItemIcon(item))}</span>
                    ${renderVirtualFinderRename(controller, item)}
                </div>
                <span class="virtual-finder-metadata finder-col-type" role="cell">${escapeHtml(typeLabel)}</span>
                <span class="virtual-finder-metadata finder-col-modified" role="cell" title="${escapeHtml(modified.title)}">${escapeHtml(modified.label)}</span>
                <span class="virtual-finder-metadata finder-col-size" role="cell">${escapeHtml(formatVirtualFinderSize(item.size, item.type))}</span>
                <span class="virtual-finder-row-actions finder-col-actions" role="cell">
                    <button type="button" data-finder-row-menu data-path="${escapeHtml(item.path)}" aria-label="Actions for ${escapeHtml(item.name)}" title="More actions">⋯</button>
                </span>
            </div>
        `;
    }).join('');
}

function renderVirtualFinderSelectionBar(controller) {
    const selectedCount = controller.selectedPaths.size;

    if (!selectedCount) {
        return '';
    }

    const selectedItems = virtualFinderSelectedItems(controller);
    const canMutate = selectedItems.length === selectedCount
        && selectedItems.every((item) => !item.protected);

    return `
        <div class="virtual-finder-selection-bar" role="toolbar" aria-label="Selected item actions">
            <span class="virtual-finder-selection-count">${selectedCount} selected</span>
            <span class="virtual-finder-selection-actions">
                ${canMutate ? '<button type="button" data-finder-action="copy">Copy</button><button type="button" data-finder-action="cut">Cut</button><button type="button" data-finder-action="move">Move to…</button><button type="button" class="danger" data-finder-action="delete">Delete…</button>' : ''}
                ${selectedCount === 1 ? '<button type="button" data-finder-action="info">Get info</button>' : ''}
                <button type="button" data-finder-action="clear-selection">Clear</button>
            </span>
        </div>
    `;
}

function renderVirtualFinderPreview(controller) {
    if (!controller.previewOpen) {
        return '';
    }

    const item = controller.itemByPath.get(controller.previewPath) || controller.previewData || {};
    const title = item.name || controller.previewPath.split('/').at(-1) || 'Preview';
    let content = '';

    if (controller.previewStatus === 'loading') {
        content = '<div class="virtual-finder-preview-empty">Loading preview…</div>';
    } else if (controller.previewStatus === 'error') {
        content = `<div class="virtual-finder-preview-error" role="alert">${escapeHtml(controller.previewError || 'Preview unavailable.')}</div>`;
    } else if (controller.previewData?.preview_kind === 'image' && controller.previewData.imageSource) {
        content = `<img class="virtual-finder-preview-image" src="${escapeHtml(controller.previewData.imageSource)}" alt="Preview of ${escapeHtml(title)}">`;
    } else if (controller.previewData?.preview_kind === 'text') {
        content = `<pre class="virtual-finder-preview-text">${escapeHtml(controller.previewData.text || '')}</pre>`;
    } else if (controller.previewData?.preview_kind === 'unavailable') {
        content = `
            <div class="virtual-finder-preview-empty">
                <span aria-hidden="true">◇</span>
                <strong>Preview unavailable</strong>
                <p>This virtual file type can only be shown in Get Info.</p>
            </div>
        `;
    } else {
        content = `
            <div class="virtual-finder-preview-empty">
                <span aria-hidden="true">${escapeHtml(virtualFinderItemIcon(item))}</span>
                <strong>${escapeHtml(title)}</strong>
                <p>${escapeHtml(item.kind || item.type || 'Metadata')}</p>
            </div>
        `;
    }

    return `
        <aside class="virtual-finder-preview" aria-label="File preview">
            <header class="virtual-finder-preview-head">
                <span title="${escapeHtml(title)}">${escapeHtml(title)}</span>
                <button class="virtual-finder-preview-close" type="button" data-finder-preview-close aria-label="Close preview">×</button>
            </header>
            <div class="virtual-finder-preview-body">${content}</div>
            <div class="virtual-finder-preview-meta">
                <span>${escapeHtml(virtualFinderKindLabel(item))}</span>
                <span>${escapeHtml(formatVirtualFinderSize(item.size, item.type))}</span>
            </div>
        </aside>
    `;
}

function renderVirtualFinderContextMenu(controller) {
    const menu = controller.contextMenu;
    const item = menu ? controller.itemByPath.get(menu.path) : null;

    if (!menu) {
        return '';
    }

    if (menu.background === true) {
        return `
            <div
                class="virtual-finder-context-menu virtual-finder-context-background"
                role="menu"
                style="left:${Math.max(0, Number(menu.x) || 0)}px;top:${Math.max(0, Number(menu.y) || 0)}px"
                data-finder-context-path=""
            >
                <button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="new-folder">New folder</button>
                <button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="new-file">New file</button>
                ${controller.clipboard.paths.length ? '<button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="paste">Paste</button>' : ''}
                <div class="virtual-finder-context-item separator" role="separator"></div>
                <button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="view-list" ${controller.viewMode === 'list' ? 'disabled' : ''}>SWITCH TO LIST</button>
                <button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="view-grid" ${controller.viewMode === 'grid' ? 'disabled' : ''}>SWITCH TO GRID</button>
                <button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="refresh">Refresh</button>
            </div>
        `;
    }

    if (!item) {
        return '';
    }

    const paths = virtualFinderPathsForAction(controller, item.path);
    const selectedItems = virtualFinderSelectedItems(controller, paths);
    const canMutate = selectedItems.length > 0 && selectedItems.every((entry) => !entry.protected);
    const isFavorite = controller.favorites.some((favorite) => favorite.path === item.path);
    const singleSelection = paths.length === 1;

    return `
        <div
            class="virtual-finder-context-menu"
            role="menu"
            style="left:${Math.max(0, Number(menu.x) || 0)}px;top:${Math.max(0, Number(menu.y) || 0)}px"
            data-finder-context-path="${escapeHtml(item.path)}"
        >
            ${singleSelection && item.type === 'folder' ? '<button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="open">Open</button>' : ''}
            ${singleSelection && item.type === 'file' && item.previewable ? '<button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="preview">Open</button>' : ''}
            ${singleSelection ? '<button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="info">Get info</button>' : ''}
            ${singleSelection && item.type === 'folder' && !item.protected && !isFavorite ? '<button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="favorite">Add to favourites</button>' : ''}
            ${canMutate ? '<div class="virtual-finder-context-item separator" role="separator"></div>' : ''}
            ${canMutate ? `${singleSelection ? '<button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="rename">Rename</button>' : ''}<button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="copy">Copy</button><button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="cut">Cut</button><button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="move">Move to…</button><button class="virtual-finder-context-item danger" type="button" role="menuitem" data-finder-context-action="delete">Delete…</button>` : ''}
            ${singleSelection && item.type === 'folder' && controller.clipboard.paths.length ? '<div class="virtual-finder-context-item separator" role="separator"></div><button class="virtual-finder-context-item" type="button" role="menuitem" data-finder-context-action="paste">Paste here</button>' : ''}
        </div>
    `;
}

function virtualFinderMoveDestinations(controller, paths) {
    const blockedPaths = Array.isArray(paths) ? paths : [];
    return [
        { path: '', name: controller.rootLabel, depth: 0 },
        ...controller.folderTree
    ].filter((folder) => (
        !blockedPaths.some((path) => (
            folder.path === path || folder.path.startsWith(`${path}/`)
        ))
        && !virtualFinderTransferIssue(controller, blockedPaths, folder.path, 'move')
    ));
}

function renderVirtualFinderDialog(controller) {
    const dialog = controller.dialog;

    if (!dialog) {
        return '';
    }

    let title = 'Virtual Finder';
    let bodyHtml = '';
    let confirmLabel = 'Confirm';

    if (dialog.type === 'delete') {
        title = 'Delete items';
        confirmLabel = 'Delete';
        const itemNames = (Array.isArray(dialog.names) ? dialog.names : [])
            .map((name) => `<li>${escapeHtml(name)}</li>`)
            .join('');
        bodyHtml = `
            <p>Delete ${dialog.paths.length} selected ${dialog.paths.length === 1 ? 'item' : 'items'} from virtual storage?</p>
            ${itemNames ? `<ul class="virtual-finder-dialog-items">${itemNames}</ul>` : ''}
            ${dialog.confirmNonEmpty
                ? '<label>Type <strong>DELETE</strong> to confirm removal of a non-empty folder.<input class="virtual-finder-dialog-input" type="text" value="' + escapeHtml(dialog.confirmText || '') + '" autocomplete="off" data-finder-focus-key="dialog-confirm-text"></label>'
                : '<p>This action cannot be undone.</p>'}
        `;
    } else if (dialog.type === 'move') {
        title = 'Move to';
        confirmLabel = 'Move';
        const destinations = virtualFinderMoveDestinations(controller, dialog.paths);
        bodyHtml = `
            <p>Choose a destination inside Virtual Finder.</p>
            <select class="virtual-finder-folder-picker" data-finder-dialog-destination data-finder-focus-key="dialog-destination">
                ${destinations.map((folder) => `
                    <option value="${escapeHtml(folder.path)}" ${dialog.destination === folder.path ? 'selected' : ''}>${escapeHtml(`${'— '.repeat(Math.max(0, Number(folder.depth) || 0))}${folder.name}`)}</option>
                `).join('')}
            </select>
        `;
    } else if (dialog.type === 'info') {
        title = 'Get info';
        confirmLabel = 'Close';
        const item = controller.itemByPath.get(dialog.path) || {};
        const modified = formatVirtualFinderTimestamp(item.modified);
        const created = formatVirtualFinderTimestamp(item.created);
        bodyHtml = `
            <dl class="virtual-finder-info-grid">
                <dt>Name</dt><dd>${escapeHtml(item.name || '—')}</dd>
                <dt>Virtual Path</dt><dd>${escapeHtml(item.path ? `/${item.path}` : '/')}</dd>
                <dt>Kind</dt><dd>${escapeHtml(item.kind || item.type || '—')}</dd>
                <dt>Size</dt><dd>${escapeHtml(formatVirtualFinderSize(item.size, item.type))}</dd>
                <dt>Created</dt><dd title="${escapeHtml(created.title)}">${escapeHtml(created.label)}</dd>
                <dt>Modified</dt><dd title="${escapeHtml(modified.title)}">${escapeHtml(modified.label)}</dd>
                ${item.type === 'folder' && Number.isFinite(item.item_count) ? `<dt>Contents</dt><dd>${item.item_count} items</dd>` : ''}
                <dt>Protected</dt><dd>${item.protected ? 'Yes' : 'No'}</dd>
            </dl>
        `;
    }

    return `
        <div class="virtual-finder-dialog-backdrop" data-finder-dialog-backdrop>
            <section class="virtual-finder-dialog" role="dialog" aria-modal="true" aria-labelledby="virtual-finder-dialog-title">
                <header class="virtual-finder-dialog-head">
                    <strong id="virtual-finder-dialog-title">${escapeHtml(title)}</strong>
                    <button type="button" data-finder-dialog-cancel aria-label="Close dialog">×</button>
                </header>
                <div class="virtual-finder-dialog-body">${bodyHtml}</div>
                ${dialog.error ? `<div class="virtual-finder-dialog-error" role="alert">${escapeHtml(dialog.error)}</div>` : ''}
                <footer class="virtual-finder-dialog-actions">
                    ${dialog.type === 'info' ? '' : '<button type="button" data-finder-dialog-cancel>Cancel</button>'}
                    <button class="${dialog.type === 'delete' ? 'danger' : ''}" type="button" data-finder-dialog-confirm data-finder-focus-key="${dialog.type === 'info' ? 'dialog-close' : 'dialog-confirm'}">${escapeHtml(confirmLabel)}</button>
                </footer>
            </section>
        </div>
    `;
}

function openVirtualFinderContextMenu(body, path, clientX, clientY) {
    const controller = getVirtualFinderController(body);
    const root = body?.querySelector?.('.virtual-finder');
    const normalizedPath = normalizeVirtualFinderPath(path);
    const background = !normalizedPath;

    if (!controller || !root || (!background && !controller.itemByPath.has(normalizedPath))) {
        return;
    }

    if (background) {
        controller.selectedPaths.clear();
        controller.selectionAnchor = '';
    } else if (!controller.selectedPaths.has(normalizedPath)) {
        controller.selectedPaths.clear();
        controller.selectedPaths.add(normalizedPath);
        controller.selectionAnchor = normalizedPath;
    }

    if (!background) {
        controller.focusedPath = normalizedPath;
    }
    const rect = root.getBoundingClientRect();
    controller.contextMenu = {
        path: normalizedPath,
        background,
        x: Math.max(6, clientX - rect.left),
        y: Math.max(6, clientY - rect.top)
    };
    controller.pendingFocus = { key: 'context-first' };
    renderVirtualFinderWidget(controller.lastData, body);

    const renderedRoot = body.querySelector('.virtual-finder');
    const renderedMenu = renderedRoot?.querySelector('.virtual-finder-context-menu');

    if (renderedRoot && renderedMenu) {
        const measuredX = Math.max(
            6,
            Math.min(renderedRoot.clientWidth - renderedMenu.offsetWidth - 6, controller.contextMenu.x)
        );
        const measuredY = Math.max(
            6,
            Math.min(renderedRoot.clientHeight - renderedMenu.offsetHeight - 6, controller.contextMenu.y)
        );
        controller.contextMenu.x = measuredX;
        controller.contextMenu.y = measuredY;
        renderedMenu.style.left = `${measuredX}px`;
        renderedMenu.style.top = `${measuredY}px`;
    }

    const firstItem = renderedMenu?.querySelector('.virtual-finder-context-item[role="menuitem"]:not(:disabled)');
    firstItem?.setAttribute('data-finder-focus-key', 'context-first');
    firstItem?.focus({ preventScroll: true });
}

function confirmVirtualFinderDialog(body) {
    const controller = getVirtualFinderController(body);
    const dialog = controller?.dialog;

    if (!controller || !dialog) {
        return;
    }

    if (dialog.type === 'info') {
        closeVirtualFinderDialog(body);
        return;
    }

    if (dialog.type === 'move') {
        transferVirtualFinderItems(body, dialog.paths, dialog.destination || '', 'move', {
            onError: (response) => {
                dialog.error = virtualFinderDisplayMessage(response?.message, 'Unable to move those items.');
                renderVirtualFinderWidget(controller.lastData, body);
            }
        });
        return;
    }

    if (dialog.type === 'delete') {
        if (dialog.confirmNonEmpty && String(dialog.confirmText || '').trim() !== 'DELETE') {
            dialog.error = 'Type DELETE to confirm.';
            controller.pendingFocus = { key: 'dialog-confirm-text', select: true };
            renderVirtualFinderWidget(controller.lastData, body);
            return;
        }

        performVirtualFinderMutation(
            body,
            'virtual_finder_delete',
            {
                paths: dialog.paths,
                confirmed: true,
                confirm_non_empty: dialog.confirmNonEmpty
            },
            {
                operation: 'delete',
                loadingMessage: 'Deleting virtual items…',
                errorMessage: 'Unable to delete those virtual items.',
                successMessage: 'Virtual items deleted.',
                onSuccess: () => {
                    controller.selectedPaths.clear();
                    controller.dialog = null;
                    if (dialog.paths.includes(controller.previewPath)) {
                        controller.previewOpen = false;
                        controller.previewPath = '';
                        controller.previewData = null;
                    }
                    saveVirtualFinderPreferences(body, controller);
                },
                onError: (response, responseData) => {
                    if (
                        response?.code === 'non_empty_confirmation_required'
                        || responseData.confirm_non_empty === true
                        || responseData.contains_non_empty === true
                        || responseData.requires_confirmation === true
                    ) {
                        dialog.confirmNonEmpty = true;
                    }
                    if (Array.isArray(responseData.items) && responseData.items.length) {
                        dialog.names = responseData.items.map((item) => String(item?.name || '')).filter(Boolean);
                    }
                    dialog.error = virtualFinderDisplayMessage(response?.message, 'Unable to delete those virtual items.');
                    renderVirtualFinderWidget(controller.lastData, body);
                }
            }
        );
    }
}

function submitVirtualFinderCreate(body, form) {
    const controller = getVirtualFinderController(body);

    if (!controller || !controller.createMode || virtualFinderRequestPending(controller, 'mutation')) {
        return;
    }

    const input = form.querySelector('.virtual-finder-create-name');
    const name = String(input?.value || controller.createDraft).replace(/\s+/g, ' ').trim();
    const error = validateVirtualFinderItemName(name, controller.createMode);

    if (error) {
        controller.createError = error;
        updateVirtualFinderTransientUi(body, controller);
        input?.focus();
        return;
    }

    controller.createDraft = name;
    controller.createError = '';
    const isFile = controller.createMode === 'file';
    performVirtualFinderMutation(
        body,
        isFile ? 'virtual_finder_create_file' : 'virtual_finder_create_folder',
        {
            name,
            parent: controller.currentPath,
            ...(isFile ? { file_type: controller.createFileType } : {})
        },
        {
            operation: 'create',
            loadingMessage: isFile ? 'Creating virtual file…' : 'Creating folder…',
            errorMessage: isFile ? 'Unable to create that virtual file.' : 'Unable to create that virtual folder.',
            successMessage: isFile ? 'Virtual file created.' : 'Folder created.',
            onSuccess: () => {
                controller.createMode = '';
                controller.createDraft = '';
                controller.createError = '';
                controller.pendingFocus = { key: isFile ? 'new-file' : 'new-folder' };
            },
            onError: (response) => {
                controller.createError = virtualFinderDisplayMessage(
                    response?.message,
                    isFile ? 'Unable to create that virtual file.' : 'Unable to create that virtual folder.'
                );
                updateVirtualFinderTransientUi(body, controller);
            }
        }
    );
}

function submitVirtualFinderRename(body, form) {
    const controller = getVirtualFinderController(body);
    const path = normalizeVirtualFinderPath(form?.dataset?.path || controller?.renamePath);
    const input = form?.querySelector('.virtual-finder-rename-input');
    const newName = String(input?.value || controller?.renameDraft || '').replace(/\s+/g, ' ').trim();
    const error = validateVirtualFinderItemName(newName, 'item');

    if (!controller || !path || virtualFinderRequestPending(controller, 'mutation')) {
        return;
    }

    if (error) {
        controller.renameError = error;
        const currentName = controller.itemByPath.get(path)?.name || newName;
        const extensionIndex = currentName.lastIndexOf('.');
        controller.pendingFocus = {
            key: `rename:${path}`,
            start: 0,
            end: extensionIndex > 0 ? extensionIndex : currentName.length
        };
        renderVirtualFinderWidget(controller.lastData, body);
        return;
    }

    controller.renameDraft = newName;
    controller.renameError = '';
    performVirtualFinderMutation(
        body,
        'virtual_finder_rename',
        { path, new_name: newName },
        {
            operation: 'rename',
            loadingMessage: 'Renaming virtual item…',
            errorMessage: 'Unable to rename that virtual item.',
            successMessage: 'Virtual item renamed.',
            onSuccess: (_response, responseData) => {
                const renamedPath = normalizeVirtualFinderPath(
                    responseData.path || responseData.new_path || responseData.item?.path || ''
                );
                controller.renamePath = '';
                controller.renameDraft = '';
                controller.renameError = '';
                controller.selectedPaths.clear();
                if (renamedPath) {
                    controller.selectedPaths.add(renamedPath);
                    controller.focusedPath = renamedPath;
                }
            },
            onError: (response) => {
                controller.renameError = virtualFinderDisplayMessage(response?.message, 'Unable to rename that virtual item.');
                const currentName = controller.itemByPath.get(path)?.name || newName;
                const extensionIndex = currentName.lastIndexOf('.');
                controller.pendingFocus = {
                    key: `rename:${path}`,
                    start: 0,
                    end: extensionIndex > 0 ? extensionIndex : currentName.length
                };
                renderVirtualFinderWidget(controller.lastData, body);
            }
        }
    );
}

function focusVirtualFinderItem(body, path) {
    const controller = getVirtualFinderController(body);
    const normalizedPath = normalizeVirtualFinderPath(path);

    if (!controller || !controller.itemByPath.has(normalizedPath)) {
        return;
    }

    controller.focusedPath = normalizedPath;
    body.querySelectorAll('.virtual-finder-row').forEach((row) => {
        const focused = row.dataset.path === normalizedPath;
        row.classList.toggle('focused', focused);
        row.tabIndex = focused ? 0 : -1;
    });
    body.querySelector(`.virtual-finder-row[data-path="${CSS.escape(normalizedPath)}"]`)?.focus({ preventScroll: true });
}

function moveVirtualFinderFocus(body, delta, absolute = '') {
    const controller = getVirtualFinderController(body);

    if (!controller?.items?.length) {
        return;
    }

    const paths = controller.items.map((item) => item.path);
    let index = paths.indexOf(controller.focusedPath);

    if (absolute === 'first') {
        index = 0;
    } else if (absolute === 'last') {
        index = paths.length - 1;
    } else {
        index = Math.max(0, Math.min(paths.length - 1, (index < 0 ? 0 : index) + delta));
    }

    focusVirtualFinderItem(body, paths[index]);
}

function handleVirtualFinderKeyboard(body, event) {
    const controller = getVirtualFinderController(body);
    const target = event.target;
    const editing = target.matches?.('input, textarea, select, button, [contenteditable="true"]');
    const modifier = event.metaKey || event.ctrlKey;

    if (!controller) {
        return;
    }

    if (event.key === 'Escape') {
        if (controller.contextMenu || controller.dialog || controller.renamePath || controller.createMode) {
            event.preventDefault();
            event.stopPropagation();
            controller.contextMenu = null;
            controller.dialog = null;
            controller.renamePath = '';
            controller.createMode = '';
            controller.createDraft = '';
            controller.createError = '';
            controller.pendingFocus = controller.focusedPath ? { path: controller.focusedPath } : null;
            renderVirtualFinderWidget(controller.lastData, body);
        } else if (controller.previewOpen) {
            event.preventDefault();
            closeVirtualFinderPreview(body);
        }
        return;
    }

    if (modifier && event.key.toLowerCase() === 'f') {
        event.preventDefault();
        const searchInput = body.querySelector('[data-finder-focus-key="search"]');
        searchInput?.focus({ preventScroll: true });
        searchInput?.select?.();
        return;
    }

    if (editing) {
        return;
    }

    if (modifier && event.key.toLowerCase() === 'a') {
        event.preventDefault();
        selectAllVirtualFinderItems(body);
        return;
    }

    if (modifier && event.key.toLowerCase() === 'c') {
        event.preventDefault();
        copyVirtualFinderSelection(body, 'copy');
        return;
    }

    if (modifier && event.key.toLowerCase() === 'x') {
        event.preventDefault();
        copyVirtualFinderSelection(body, 'cut');
        return;
    }

    if (modifier && event.key.toLowerCase() === 'v' && controller.clipboard.paths.length) {
        event.preventDefault();
        executeVirtualFinderAction(body, 'paste');
        return;
    }

    if (event.altKey && event.key === 'ArrowLeft') {
        event.preventDefault();
        navigateVirtualFinderHistory(body, 'back');
        return;
    }

    if (event.altKey && event.key === 'ArrowRight') {
        event.preventDefault();
        navigateVirtualFinderHistory(body, 'forward');
        return;
    }

    const row = target.closest?.('.virtual-finder-row');
    const rowPath = normalizeVirtualFinderPath(row?.dataset?.path);

    if (event.key === 'Delete' || event.key === 'Backspace') {
        event.preventDefault();

        if (rowPath && !controller.selectedPaths.has(rowPath)) {
            controller.selectedPaths.clear();
            controller.selectedPaths.add(rowPath);
        }

        const paths = controller.selectedPaths.size
            ? Array.from(controller.selectedPaths)
            : controller.focusedPath ? [controller.focusedPath] : [];

        if (paths.length) openVirtualFinderDeleteDialog(body, paths);
        return;
    }

    if (event.key === 'Enter') {
        const path = rowPath || (controller.selectedPaths.size === 1
            ? Array.from(controller.selectedPaths)[0]
            : controller.focusedPath);
        const item = controller.itemByPath.get(path || '');

        if (item) {
            event.preventDefault();
            executeVirtualFinderAction(body, item.type === 'folder' ? 'open' : 'preview', item.path);
        }
        return;
    }

    if (!row) {
        return;
    }

    const path = row.dataset.path || '';
    const indexDelta = controller.viewMode === 'grid' && ['ArrowUp', 'ArrowDown'].includes(event.key)
        ? Math.max(1, Math.floor((body.querySelector('.virtual-finder-list')?.clientWidth || 1) / 150))
        : 1;

    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
        event.preventDefault();
        moveVirtualFinderFocus(body, event.key === 'ArrowDown' ? indexDelta : 1);
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
        event.preventDefault();
        moveVirtualFinderFocus(body, event.key === 'ArrowUp' ? -indexDelta : -1);
    } else if (event.key === 'Home') {
        event.preventDefault();
        moveVirtualFinderFocus(body, 0, 'first');
    } else if (event.key === 'End') {
        event.preventDefault();
        moveVirtualFinderFocus(body, 0, 'last');
    } else if (event.key === ' ') {
        event.preventDefault();
        setVirtualFinderSelection(body, path, { metaKey: true });
    } else if (event.key === 'F2') {
        event.preventDefault();
        beginVirtualFinderRename(body, path);
    } else if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
        event.preventDefault();
        const rect = row.getBoundingClientRect();
        openVirtualFinderContextMenu(body, path, rect.left + 24, rect.top + 20);
    }
}

function attachVirtualFinderInteractions(body, controller) {
    const root = body.querySelector('.virtual-finder');

    if (!root) {
        return;
    }

    const markActive = () => {
        virtualFinderLastActiveBody = body;
    };
    root.addEventListener('pointerdown', markActive);
    root.addEventListener('focusin', markActive);

    if (!controller.outsideMenuHandler) {
        controller.outsideMenuHandler = (event) => {
            if (!body.isConnected) {
                document.removeEventListener('pointerdown', controller.outsideMenuHandler, true);
                controller.outsideMenuHandler = null;
                return;
            }

            if (controller.contextMenu && !event.target.closest?.('.virtual-finder-context-menu')) {
                controller.contextMenu = null;
                body.querySelector('.virtual-finder-context-menu')?.remove();
            }
        };
        document.addEventListener('pointerdown', controller.outsideMenuHandler, true);
    }

    root.addEventListener('click', (event) => {
        const target = event.target;
        const nav = target.closest('[data-finder-nav]');
        const pathButton = target.closest('[data-finder-path], [data-finder-location], [data-finder-favorite-open]');
        const createButton = target.closest('[data-finder-create]');
        const favoriteAction = target.closest('[data-finder-favorite-action]');
        const rowAction = target.closest('[data-finder-row-action]');
        const rowMenu = target.closest('[data-finder-row-menu]');
        const generalAction = target.closest('[data-finder-action]');
        const contextAction = target.closest('[data-finder-context-action]');

        if (nav) {
            const action = nav.dataset.finderNav;
            if (action === 'back') navigateVirtualFinderHistory(body, 'back');
            if (action === 'forward') navigateVirtualFinderHistory(body, 'forward');
            if (action === 'up') openVirtualFinderPath(body, nav.dataset.path || '');
            if (action === 'refresh') refreshVirtualFinder(body);
            return;
        }

        if (pathButton) {
            const nextPath = pathButton.dataset.finderPath ?? pathButton.dataset.path ?? '';
            if (normalizeVirtualFinderPath(nextPath) !== controller.currentPath || controller.searchQuery) {
                openVirtualFinderPath(body, nextPath);
            }
            return;
        }

        if (createButton) {
            beginVirtualFinderCreate(body, createButton.dataset.finderCreate);
            return;
        }

        if (target.closest('[data-finder-create-cancel]')) {
            controller.createMode = '';
            controller.createDraft = '';
            controller.createError = '';
            renderVirtualFinderWidget(controller.lastData, body);
            return;
        }

        if (target.closest('[data-finder-rename-cancel]')) {
            controller.renamePath = '';
            controller.renameDraft = '';
            controller.renameError = '';
            renderVirtualFinderWidget(controller.lastData, body);
            return;
        }

        if (target.closest('[data-finder-toggle-sidebar]')) {
            controller.sidebarCollapsed = !controller.sidebarCollapsed;
            saveVirtualFinderPreferences(body, controller);
            renderVirtualFinderWidget(controller.lastData, body);
            return;
        }

        if (target.closest('[data-finder-toggle-favorites]')) {
            controller.favoritesCollapsed = !controller.favoritesCollapsed;
            saveVirtualFinderPreferences(body, controller);
            renderVirtualFinderWidget(controller.lastData, body);
            return;
        }

        if (target.closest('[data-finder-add-current-favorite]')) {
            addVirtualFinderFavorite(body, controller.currentPath);
            return;
        }

        if (favoriteAction) {
            updateVirtualFinderFavorite(body, favoriteAction.dataset.path, favoriteAction.dataset.finderFavoriteAction);
            return;
        }

        const viewButton = target.closest('[data-finder-view]');
        if (viewButton && VIRTUAL_FINDER_VIEW_MODES.has(viewButton.dataset.finderView)) {
            controller.viewMode = viewButton.dataset.finderView;
            saveVirtualFinderPreferences(body, controller);
            renderVirtualFinderWidget(controller.lastData, body);
            return;
        }

        if (target.closest('[data-finder-sort-direction]')) {
            controller.sortDirection = controller.sortDirection === 'desc' ? 'asc' : 'desc';
            saveVirtualFinderPreferences(body, controller);
            renderVirtualFinderWidget(controller.lastData, body);
            return;
        }

        const sortHeader = target.closest('[data-finder-sort]');
        if (sortHeader) {
            const field = sortHeader.dataset.finderSort;
            if (controller.sortField === field) {
                controller.sortDirection = controller.sortDirection === 'desc' ? 'asc' : 'desc';
            } else if (VIRTUAL_FINDER_SORT_FIELDS.has(field)) {
                controller.sortField = field;
                controller.sortDirection = 'asc';
            }
            saveVirtualFinderPreferences(body, controller);
            renderVirtualFinderWidget(controller.lastData, body);
            return;
        }

        if (rowAction) {
            event.stopPropagation();
            executeVirtualFinderAction(body, rowAction.dataset.finderRowAction, rowAction.dataset.path);
            return;
        }

        if (rowMenu) {
            event.stopPropagation();
            const rect = rowMenu.getBoundingClientRect();
            openVirtualFinderContextMenu(body, rowMenu.dataset.path, rect.right, rect.bottom);
            return;
        }

        if (generalAction) {
            executeVirtualFinderAction(body, generalAction.dataset.finderAction, generalAction.dataset.path, generalAction.dataset.destination);
            return;
        }

        if (contextAction) {
            const menu = contextAction.closest('.virtual-finder-context-menu');
            const menuPath = menu?.dataset.finderContextPath || '';
            controller.contextMenu = null;
            menu?.remove();
            executeVirtualFinderAction(body, contextAction.dataset.finderContextAction, menuPath, menuPath);
            return;
        }

        if (target.closest('[data-finder-preview-close]')) {
            closeVirtualFinderPreview(body);
            return;
        }

        if (target.closest('[data-finder-dialog-cancel]')) {
            closeVirtualFinderDialog(body);
            return;
        }

        if (target.closest('[data-finder-dialog-confirm]')) {
            confirmVirtualFinderDialog(body);
            return;
        }

        if (target.matches('[data-finder-dialog-backdrop]')) {
            closeVirtualFinderDialog(body);
            return;
        }

        const row = target.closest('.virtual-finder-row');
        if (row && !target.closest('button, input, select, form')) {
            setVirtualFinderSelection(body, row.dataset.path, event);
            return;
        }

        if (controller.contextMenu && !target.closest('.virtual-finder-context-menu')) {
            controller.contextMenu = null;
            renderVirtualFinderWidget(controller.lastData, body);
        }
    });

    root.addEventListener('dblclick', (event) => {
        if (event.target.closest('button, input, select, form')) {
            return;
        }

        const row = event.target.closest('.virtual-finder-row');
        const item = row ? controller.itemByPath.get(row.dataset.path || '') : null;

        if (item) {
            executeVirtualFinderAction(body, item.type === 'folder' ? 'open' : 'preview', item.path);
        }
    });

    root.addEventListener('contextmenu', (event) => {
        const row = event.target.closest('.virtual-finder-row');

        if (event.target.closest('input, select, form')) {
            return;
        }

        if (!row && !event.target.closest('.virtual-finder-browser-pane')) {
            return;
        }

        event.preventDefault();
        openVirtualFinderContextMenu(body, row?.dataset.path || '', event.clientX, event.clientY);
    });

    root.addEventListener('submit', (event) => {
        event.preventDefault();

        if (event.target.matches('.virtual-finder-search')) {
            submitVirtualFinderSearch(body, event.target.querySelector('input')?.value || '');
        } else if (event.target.matches('.virtual-finder-create')) {
            submitVirtualFinderCreate(body, event.target);
        } else if (event.target.matches('.virtual-finder-rename-form')) {
            submitVirtualFinderRename(body, event.target);
        }
    });

    root.addEventListener('input', (event) => {
        if (event.target.matches('.virtual-finder-search input')) {
            controller.searchDraft = event.target.value;
            controller.searchDirty = true;
            const clear = root.querySelector('.virtual-finder-search-clear');
            if (clear) clear.hidden = !event.target.value;

            if (controller.searchTimer) {
                window.clearTimeout(controller.searchTimer);
            }

            controller.searchTimer = window.setTimeout(() => {
                submitVirtualFinderSearch(body, controller.searchDraft);
            }, 150);
        } else if (event.target.matches('.virtual-finder-create-name')) {
            controller.createDraft = event.target.value;
            controller.createError = '';
        } else if (event.target.matches('.virtual-finder-rename-input')) {
            controller.renameDraft = event.target.value;
            controller.renameError = '';
        } else if (event.target.matches('.virtual-finder-dialog-input') && controller.dialog?.type === 'delete') {
            controller.dialog.confirmText = event.target.value;
            controller.dialog.error = '';
        }
    });

    root.addEventListener('change', (event) => {
        if (event.target.matches('.virtual-finder-location-select select')) {
            openVirtualFinderPath(body, event.target.value || '');
        } else if (event.target.matches('.virtual-finder-create-type')) {
            controller.createFileType = event.target.value || 'txt';
        } else if (event.target.matches('.virtual-finder-sort-field')) {
            if (VIRTUAL_FINDER_SORT_FIELDS.has(event.target.value)) {
                controller.sortField = event.target.value;
                saveVirtualFinderPreferences(body, controller);
                renderVirtualFinderWidget(controller.lastData, body);
            }
        } else if (event.target.matches('[data-finder-dialog-destination]') && controller.dialog?.type === 'move') {
            controller.dialog.destination = normalizeVirtualFinderPath(event.target.value);
            controller.dialog.error = '';
        }
    });

    root.querySelector('.virtual-finder-search-clear')?.addEventListener('click', () => {
        controller.searchDraft = '';
        controller.searchDirty = true;
        submitVirtualFinderSearch(body, '');
        controller.pendingFocus = { key: 'search' };
    });

    root.addEventListener('keydown', (event) => handleVirtualFinderKeyboard(body, event));
    root.querySelector('.virtual-finder-list')?.addEventListener('scroll', (event) => {
        controller.listScrollTop = event.currentTarget.scrollTop;
        if (controller.contextMenu) {
            controller.contextMenu = null;
            renderVirtualFinderWidget(controller.lastData, body);
        }
    }, { passive: true });

    root.addEventListener('focusin', (event) => {
        const row = event.target.closest?.('.virtual-finder-row');
        if (row) controller.focusedPath = row.dataset.path || '';
    });

    root.addEventListener('dragstart', (event) => {
        const favorite = event.target.closest('.virtual-finder-favorite');

        if (favorite) {
            controller.favoriteDragPath = normalizeVirtualFinderPath(favorite.dataset.path);

            if (!controller.favoriteDragPath) {
                event.preventDefault();
                return;
            }

            controller.dragPaths = [];
            favorite.classList.add('drag-source');
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData(
                VIRTUAL_FINDER_DRAG_TYPE,
                JSON.stringify({ favorite: controller.favoriteDragPath })
            );
            return;
        }

        const row = event.target.closest('.virtual-finder-row');
        const path = normalizeVirtualFinderPath(row?.dataset?.path);
        const item = controller.itemByPath.get(path);

        if (!row || !item || item.protected) {
            event.preventDefault();
            return;
        }

        if (!controller.selectedPaths.has(path)) {
            controller.selectedPaths.clear();
            controller.selectedPaths.add(path);
            controller.selectionAnchor = path;
        }

        controller.dragPaths = virtualFinderActionablePaths(controller);

        if (!controller.dragPaths.length) {
            event.preventDefault();
            setVirtualFinderRequestState(
                body,
                'error',
                'transfer',
                'Protected virtual locations cannot be transferred.',
                { autoClearMs: 5000 }
            );
            return;
        }

        row.classList.add('drag-source');
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData(VIRTUAL_FINDER_DRAG_TYPE, JSON.stringify({ paths: controller.dragPaths }));
    });

    root.addEventListener('dragover', (event) => {
        if (controller.favoriteDragPath) {
            const favoriteTarget = event.target.closest('.virtual-finder-favorite');

            root.querySelectorAll('.favorite-drop-target').forEach((item) => {
                item.classList.remove('favorite-drop-target');
            });

            if (
                favoriteTarget
                && normalizeVirtualFinderPath(favoriteTarget.dataset.path) !== controller.favoriteDragPath
            ) {
                event.preventDefault();
                event.dataTransfer.dropEffect = 'move';
                favoriteTarget.classList.add('favorite-drop-target');
            }
            return;
        }

        const target = event.target.closest('[data-finder-drop-path]');

        if (!target || !controller.dragPaths.length) {
            return;
        }

        event.preventDefault();
        const transferIssue = virtualFinderTransferIssue(
            controller,
            controller.dragPaths,
            target.dataset.finderDropPath || '',
            'move'
        );
        event.dataTransfer.dropEffect = transferIssue ? 'none' : 'move';
        root.querySelectorAll('.drop-target, .drop-invalid').forEach((item) => {
            item.classList.remove('drop-target', 'drop-invalid');
        });
        target.classList.add(transferIssue ? 'drop-invalid' : 'drop-target');
        target.dataset.finderDropError = transferIssue;
    });

    root.addEventListener('drop', (event) => {
        if (controller.favoriteDragPath) {
            const favoriteTarget = event.target.closest('.virtual-finder-favorite');
            const sourcePath = controller.favoriteDragPath;
            const targetPath = normalizeVirtualFinderPath(favoriteTarget?.dataset?.path);
            controller.favoriteDragPath = '';
            root.querySelectorAll('.favorite-drop-target').forEach((item) => {
                item.classList.remove('favorite-drop-target');
            });

            if (favoriteTarget && targetPath && targetPath !== sourcePath) {
                event.preventDefault();
                reorderVirtualFinderFavorite(body, sourcePath, targetPath);
            }
            return;
        }

        const target = event.target.closest('[data-finder-drop-path]');

        root.querySelectorAll('.drop-target, .drop-invalid').forEach((item) => {
            item.classList.remove('drop-target', 'drop-invalid');
            delete item.dataset.finderDropError;
        });

        if (!target || !controller.dragPaths.length) {
            return;
        }

        event.preventDefault();
        const transferIssue = virtualFinderTransferIssue(
            controller,
            controller.dragPaths,
            target.dataset.finderDropPath || '',
            'move'
        );

        if (transferIssue) {
            setVirtualFinderRequestState(
                body,
                'error',
                'transfer',
                transferIssue,
                { autoClearMs: 5000 }
            );
            controller.dragPaths = [];
            return;
        }

        transferVirtualFinderItems(body, controller.dragPaths, target.dataset.finderDropPath || '', 'move');
        controller.dragPaths = [];
    });

    root.addEventListener('dragend', () => {
        controller.dragPaths = [];
        controller.favoriteDragPath = '';
        root.querySelectorAll('.drag-source, .drop-target, .drop-invalid, .favorite-drop-target').forEach((item) => {
            item.classList.remove('drag-source', 'drop-target', 'drop-invalid', 'favorite-drop-target');
            delete item.dataset.finderDropError;
        });
    });
}

function renderVirtualFinderWidget(data, body) {
    const controller = getVirtualFinderController(body);

    if (!controller) {
        return;
    }

    const capturedFocus = captureVirtualFinderFocus(body);
    const previousList = body.querySelector('.virtual-finder-list');
    if (previousList) controller.listScrollTop = previousList.scrollTop;

    const wasInitialized = controller.initialized;
    const previousPath = controller.currentPath;
    const path = normalizeVirtualFinderPath(data?.current_path || data?.path || '');
    const parentPath = normalizeVirtualFinderPath(data?.parent_path || '');
    const searchQuery = String(data?.search_query || '').trim();
    const rootLabel = String(data?.root || 'Virtual Finder').trim() || 'Virtual Finder';
    const rawItems = (Array.isArray(data?.items) ? data.items : [])
        .map(normalizeVirtualFinderItem)
        .filter((item) => item.path);
    const sidebar = Array.isArray(data?.sidebar) ? data.sidebar : [];

    controller.initialized = true;
    controller.lastData = data || {};
    controller.currentPath = path;
    controller.parentPath = parentPath;
    controller.rootLabel = rootLabel;
    controller.searchQuery = searchQuery;
    controller.storage = data?.storage && typeof data.storage === 'object' ? data.storage : {};
    controller.folderTree = flattenVirtualFinderFolderTree(data?.folder_tree || []);

    if (!controller.folderTree.length) {
        controller.folderTree = rawItems
            .filter((item) => item.type === 'folder')
            .map((item) => ({ path: item.path, name: item.name, depth: 0, protected: item.protected }));
    }

    if (wasInitialized && previousPath !== path) {
        controller.selectedPaths.clear();
        controller.selectionAnchor = '';
        controller.focusedPath = '';
        controller.renamePath = '';
        controller.contextMenu = null;
        controller.listScrollTop = 0;
        controller.previewPath = '';
        controller.previewStatus = 'idle';
        controller.previewError = '';
        controller.previewData = null;
        controller.previewLoadedPath = '';
    }

    if (!controller.searchDirty && controller.pendingSearchQuery === null) {
        controller.searchDraft = searchQuery;
    } else if (
        controller.pendingSearchQuery === searchQuery
        && controller.searchDraft.trim() === controller.pendingSearchQuery
    ) {
        controller.searchDraft = searchQuery;
        controller.searchDirty = false;
        controller.pendingSearchQuery = null;
    }

    controller.items = sortVirtualFinderItems(rawItems, controller);
    controller.itemByPath = new Map(controller.items.map((item) => [item.path, item]));
    controller.selectedPaths = new Set(
        Array.from(controller.selectedPaths).filter((selectedPath) => controller.itemByPath.has(selectedPath))
    );

    if (
        controller.previewOpen
        && controller.previewPath
        && !controller.itemByPath.has(controller.previewPath)
    ) {
        controller.previewPath = '';
        controller.previewStatus = 'idle';
        controller.previewError = '';
        controller.previewData = null;
        controller.previewLoadedPath = '';
    }

    if (controller.focusedPath && !controller.itemByPath.has(controller.focusedPath)) {
        controller.focusedPath = '';
    }

    if (!controller.focusedPath) {
        controller.focusedPath = controller.items[0]?.path || '';
    }

    virtualFinderCurrentPath = path;
    virtualFinderLastActiveBody = body;
    const statusState = controller.status;
    const itemRows = renderVirtualFinderRows(controller);
    const emptyState = searchQuery
        ? '<div class="virtual-finder-empty" role="status"><span aria-hidden="true">⌕</span><strong>No files match your search.</strong><p>Try a different name or clear the search.</p></div>'
        : '<div class="virtual-finder-empty" role="status"><span aria-hidden="true">□</span><strong>This folder is empty.</strong><p>Create a folder or add files to begin.</p></div>';
    const itemCountLabel = `${controller.items.length} ${controller.items.length === 1 ? 'item' : 'items'}`;
    const rootClasses = [
        'virtual-finder',
        controller.sidebarCollapsed ? 'sidebar-collapsed' : '',
        controller.previewOpen ? 'preview-open' : ''
    ].filter(Boolean).join(' ');

    body.innerHTML = `
        <section class="${rootClasses}" data-view="${escapeHtml(controller.viewMode)}" aria-label="Virtual Finder">
            ${renderVirtualFinderSidebar(controller, sidebar)}
            <main class="virtual-finder-main">
                ${renderVirtualFinderToolbar(controller)}
                <div class="virtual-finder-status status-${escapeHtml(statusState.status)}" role="status" ${statusState.status === 'idle' || !statusState.message ? 'hidden' : ''}>${escapeHtml(statusState.message)}</div>
                ${renderVirtualFinderCreateForm(controller)}
                ${renderVirtualFinderSelectionBar(controller)}
                <div class="virtual-finder-content">
                    <section class="virtual-finder-browser-pane" data-finder-drop-path="${escapeHtml(path)}">
                        <section class="virtual-finder-list-shell" role="grid" aria-label="Folder contents" aria-colcount="5" aria-multiselectable="true">
                            <div class="virtual-finder-list-header" role="row">
                                ${virtualFinderSortHeader(controller, 'name', 'Name', 'finder-col-name')}
                                ${virtualFinderSortHeader(controller, 'type', 'Type', 'finder-col-type')}
                                ${virtualFinderSortHeader(controller, 'modified', 'Modified', 'finder-col-modified')}
                                ${virtualFinderSortHeader(controller, 'size', 'Size', 'finder-col-size')}
                                <span class="finder-col-actions" role="columnheader">Actions</span>
                            </div>
                            <div class="virtual-finder-list ${statusState.status === 'loading' ? 'is-loading' : ''}" role="rowgroup" aria-busy="${statusState.status === 'loading' ? 'true' : 'false'}">
                                ${itemRows || emptyState}
                            </div>
                        </section>
                    </section>
                    ${renderVirtualFinderPreview(controller)}
                </div>
                <footer class="virtual-finder-footer">
                    <span>${escapeHtml(itemCountLabel)}${controller.selectedPaths.size ? ` · ${controller.selectedPaths.size} selected` : ''}</span>
                    <span>${searchQuery ? `Search: ${escapeHtml(searchQuery)}` : escapeHtml(path ? `/${path}` : '/')}</span>
                </footer>
            </main>
            ${renderVirtualFinderContextMenu(controller)}
            ${renderVirtualFinderDialog(controller)}
        </section>
    `;

    attachVirtualFinderInteractions(body, controller);
    updateVirtualFinderTransientUi(body, controller);

    const nextList = body.querySelector('.virtual-finder-list');
    if (nextList) nextList.scrollTop = controller.listScrollTop;
    restoreVirtualFinderFocus(body, controller, capturedFocus);

    const shouldRestorePath = !wasInitialized
        && !path
        && controller.preferredPath
        && !controller.restorePathAttempted;

    if (shouldRestorePath && !controller.restorePathScheduled) {
        controller.restorePathScheduled = true;
        window.requestAnimationFrame(() => {
            controller.restorePathScheduled = false;
            controller.restorePathAttempted = true;

            if (!body.isConnected) {
                return;
            }

            openVirtualFinderPath(body, controller.preferredPath, {
                remember: false,
                onError: () => {
                    controller.preferredPath = '';
                    controller.currentPath = '';
                    saveVirtualFinderPreferences(body, controller);
                }
            });
        });
    } else if (!shouldRestorePath) {
        controller.preferredPath = path;
        saveVirtualFinderPreferences(body, controller);
    }

    if (
        controller.previewOpen
        && controller.previewPath
        && controller.previewLoadedPath !== controller.previewPath
        && controller.previewStatus === 'idle'
        && controller.itemByPath.has(controller.previewPath)
    ) {
        window.requestAnimationFrame(() => {
            if (body.isConnected) openVirtualFinderPreview(body, controller.previewPath, { restore: true });
        });
    }
}

function renderProactiveAlertWidget(data, body) {
    const topic = escapeHtml(String(data.topic || 'monitor').toUpperCase());
    const priority = escapeHtml(String(data.priority || 'low').toUpperCase());
    const title = escapeHtml(data.title || 'PROACTIVE ALERT');
    const message = escapeHtml(data.message || '');
    const timestamp = escapeHtml(data.timestamp || '');
    const items = Array.isArray(data.items) ? data.items.slice(0, 3) : [];
    const itemsHtml = items.map((item) => `<li>${escapeHtml(item)}</li>`).join('');

    body.innerHTML = `
        <section class="proactive-alert-widget priority-${String(data.priority || 'low').toLowerCase()}">
            <div class="proactive-alert-meta">
                <span>${topic}</span>
                <strong>${priority}</strong>
            </div>
            <div class="proactive-alert-title">${title}</div>
            <div class="proactive-alert-message">${message}</div>
            ${itemsHtml ? `<ul class="proactive-alert-items">${itemsHtml}</ul>` : ''}
            <div class="proactive-alert-time">${timestamp}</div>
        </section>
    `;
}

function notificationTopicLabel(notification) {
    const value = String(notification?.target || notification?.topic || '').toLowerCase();
    const labels = {
        kosovo: 'KOSOVO',
        markets: 'MARKETS',
        conflict: 'ACTIVE WARS',
        weather: 'WEATHER',
        calendar: 'CALENDAR',
        tasks: 'TASKS',
        presence: 'PRESENCE'
    };

    return labels[value] || 'ALERT';
}

function notificationCommand(notification) {
    const target = String(notification?.target || notification?.topic || '').toLowerCase();

    if (target === 'kosovo') {
        return 'what is going on in Kosovo';
    }

    if (target === 'markets') {
        return 'how are the markets today';
    }

    if (target === 'conflict') {
        return 'what are the current conflicts';
    }

    if (target === 'weather') {
        return 'weather';
    }

    if (target === 'tasks') {
        return 'open tasks';
    }

    if (target === 'presence') {
        return 'presence status';
    }

    return 'open notifications';
}

function routeNotification(notification) {
    socket.emit('manual_override', {
        text: notificationCommand(notification)
    });
}

function ensureNotificationLauncher() {
    let shell = document.querySelector('.notification-launcher-shell');

    if (shell) {
        return shell;
    }

    shell = document.createElement('div');
    shell.className = 'notification-launcher-shell';
    shell.innerHTML = `
        <button class="notification-launcher" type="button" aria-label="Toggle notification center">
            <span>NOTIFICATIONS</span>
            <strong class="notification-count">0</strong>
        </button>
        <section class="notification-center-panel" aria-label="Notification center"></section>
    `;

    document.body.appendChild(shell);

    const launcher = shell.querySelector('.notification-launcher');

    launcher.addEventListener('click', () => {
        toggleNotificationCenter();
    });

    renderNotificationCenterPanel();
    return shell;
}

function setNotificationHistory(notifications) {
    notificationHistory = Array.isArray(notifications)
        ? notifications.filter((notification) => notification && typeof notification === 'object').slice(0, 50)
        : [];

    updateNotificationLauncher();
}

function mergeNotificationHistory(notification) {
    if (!notification || typeof notification !== 'object') {
        return;
    }

    const id = String(notification.id || '');
    const existingIndex = id
        ? notificationHistory.findIndex((item) => String(item.id || '') === id)
        : -1;

    if (existingIndex >= 0) {
        notificationHistory.splice(existingIndex, 1);
    }

    notificationHistory.unshift(notification);
    notificationHistory = notificationHistory.slice(0, 50);
    updateNotificationLauncher();
}

function updateNotificationLauncher() {
    const shell = ensureNotificationLauncher();
    const launcher = shell.querySelector('.notification-launcher');
    const count = shell.querySelector('.notification-count');
    const hasHighPriority = notificationHistory.some((notification) => {
        return String(notification.priority || '').toLowerCase() === 'high';
    });

    if (count) {
        count.innerText = String(notificationHistory.length);
    }

    if (launcher) {
        launcher.classList.toggle('has-alerts', notificationHistory.length > 0);
        launcher.classList.toggle('has-high-alerts', hasHighPriority);
    }

    renderNotificationCenterPanel();
}

function toggleNotificationCenter(forceOpen) {
    const shell = ensureNotificationLauncher();
    notificationCenterOpen = typeof forceOpen === 'boolean' ? forceOpen : !notificationCenterOpen;
    shell.classList.toggle('open', notificationCenterOpen);
    renderNotificationCenterPanel();
}

function renderNotificationRows(notifications, rowClass = 'notification-row') {
    if (!notifications.length) {
        return '<div class="notification-center-empty">No notifications logged.</div>';
    }

    return notifications.map((notification) => `
        <button class="${rowClass} priority-${escapeHtml(String(notification.priority || 'low').toLowerCase())}" type="button" data-notification-id="${escapeHtml(notification.id || '')}">
            <div class="notification-center-row-top">
                <span>${escapeHtml(notificationTopicLabel(notification))}</span>
                <strong>${escapeHtml(formatNotificationTime(notification.timestamp))}</strong>
            </div>
            <div class="notification-center-title">${escapeHtml(notification.title || 'Proactive alert')}</div>
            <div class="notification-center-message">${escapeHtml(notification.message || '')}</div>
        </button>
    `).join('');
}

function attachNotificationRowRoutes(root, notifications, rowSelector = '.notification-row') {
    root.querySelectorAll(rowSelector).forEach((row) => {
        row.addEventListener('click', () => {
            const id = row.dataset.notificationId || '';
            const notification = notifications.find((item) => String(item.id || '') === id);
            routeNotification(notification || {});
        });
    });
}

function renderNotificationCenterPanel() {
    const shell = document.querySelector('.notification-launcher-shell');

    if (!shell) {
        return;
    }

    const panel = shell.querySelector('.notification-center-panel');

    if (!panel) {
        return;
    }

    const notifications = notificationHistory.slice(0, 50);

    panel.innerHTML = `
        <div class="notification-center-panel-top">
            <span>NOTIFICATION CENTER</span>
            <button class="notification-center-close" type="button" aria-label="Close notification center">CLOSE</button>
        </div>
        <div class="notification-center-list">${renderNotificationRows(notifications)}</div>
    `;

    const closeButton = panel.querySelector('.notification-center-close');

    if (closeButton) {
        closeButton.addEventListener('click', () => {
            toggleNotificationCenter(false);
        });
    }

    attachNotificationRowRoutes(panel, notifications);
}

function getNotificationToastContainer() {
    let container = document.querySelector('.notification-toast-container');

    if (!container) {
        container = document.createElement('div');
        container.className = 'notification-toast-container';
        document.body.appendChild(container);
    }

    return container;
}

function formatNotificationTime(value) {
    if (!value) {
        return '';
    }

    try {
        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return String(value);
        }

        return date.toLocaleTimeString([], {
            hour: '2-digit',
            minute: '2-digit'
        });
    } catch (_) {
        return String(value);
    }
}

function showProactiveNotification(notification) {
    if (!notification || typeof notification !== 'object') {
        return;
    }

    mergeNotificationHistory(notification);

    const settings = latestState?.settings || settingsPayload?.settings || {};

    if (settings.show_notifications === false) {
        return;
    }

    const container = getNotificationToastContainer();
    const toast = document.createElement('button');
    const priority = String(notification.priority || 'low').toLowerCase();

    toast.type = 'button';
    toast.className = `notification-toast priority-${priority}`;
    toast.innerHTML = `
        <div class="notification-toast-top">
            <span>${escapeHtml(notificationTopicLabel(notification))}</span>
            <strong>${escapeHtml(formatNotificationTime(notification.timestamp))}</strong>
        </div>
        <div class="notification-toast-title">${escapeHtml(notification.title || 'Proactive alert')}</div>
        <div class="notification-toast-message">${escapeHtml(notification.message || '')}</div>
    `;

    toast.addEventListener('click', () => {
        routeNotification(notification);
        toast.classList.add('toast-closing');
        window.setTimeout(() => toast.remove(), 180);
    });

    container.prepend(toast);

    window.setTimeout(() => {
        toast.classList.add('toast-closing');
        window.setTimeout(() => toast.remove(), 260);
    }, 5000);
}

/**
 * A quiet note that FRIDAY learned something.
 *
 * Deliberately NOT a proactive notification: it is never spoken, never enters
 * the notification history, and cannot be clicked through to anywhere. A memory
 * forming is a side effect of the conversation, not a turn in it, so it appears
 * briefly and leaves. It only ever fires when something was really stored or
 * really changed — the backend does not send one otherwise.
 */
function showMemoryLearnedToast(payload) {
    if (!payload || typeof payload !== 'object' || !payload.text) {
        return;
    }

    const settings = latestState?.settings || settingsPayload?.settings || {};

    if (settings.show_notifications === false) {
        return;
    }

    const container = getNotificationToastContainer();
    const toast = document.createElement('div');
    const label = [payload.category, payload.previous ? 'updated' : '']
        .filter(Boolean)
        .join(' · ');

    toast.className = 'notification-toast memory-toast';
    toast.innerHTML = `
        <div class="notification-toast-top">
            <span>${escapeHtml(payload.title || 'Learned')}</span>
            ${label ? `<strong>${escapeHtml(label)}</strong>` : ''}
        </div>
        <div class="notification-toast-message">${escapeHtml(payload.text)}</div>
    `;

    container.prepend(toast);

    window.setTimeout(() => {
        toast.classList.add('toast-closing');
        window.setTimeout(() => toast.remove(), 260);
    }, 3200);
}

// ==========================================
// NOTIFICATION CENTER
// ==========================================
// Every row here is a real record from proactive_manager. There is no synthetic
// content and no placeholder row: an empty store renders an empty state.

const NOTIFICATION_GLYPHS = {
    calendar: '◫', weather: '☁', markets: '$', kosovo: '⚑', conflict: '⚔',
    tasks: '☑', system: '∿', music: '♫', news: '≡', default: '◉'
};

function notificationGlyph(topic) {
    return NOTIFICATION_GLYPHS[String(topic || '').toLowerCase()] || NOTIFICATION_GLYPHS.default;
}

/** Today / Yesterday / Earlier, by real calendar day rather than elapsed hours. */
function notificationBucket(timestamp) {
    const when = new Date(timestamp);

    if (Number.isNaN(when.getTime())) {
        return 'Earlier';
    }

    const startOfToday = new Date();
    startOfToday.setHours(0, 0, 0, 0);

    if (when >= startOfToday) return 'Today';

    const startOfYesterday = new Date(startOfToday);
    startOfYesterday.setDate(startOfYesterday.getDate() - 1);

    return when >= startOfYesterday ? 'Yesterday' : 'Earlier';
}

function notificationTime(timestamp) {
    const when = new Date(timestamp);
    if (Number.isNaN(when.getTime())) return '';
    return when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

/**
 * Collapse runs from the same source into one stack.
 *
 * The monitors emit repeatedly on the same topic, so an ungrouped list is mostly
 * the same headline over and over. The newest carries the stack; the rest are
 * available underneath it.
 */
function stackNotifications(items) {
    const stacks = [];
    const byTopic = new Map();

    items.forEach((item) => {
        const key = String(item.topic || 'other').toLowerCase();
        const existing = byTopic.get(key);

        if (existing) {
            existing.items.push(item);
            return;
        }

        const stack = { key, lead: item, items: [item] };
        byTopic.set(key, stack);
        stacks.push(stack);
    });

    return stacks;
}

function notificationRowHtml(item, { stackCount = 0 } = {}) {
    const unread = !item.read;
    const priority = String(item.priority || 'low').toLowerCase();

    return `
        <article class="nc-row ${unread ? 'is-unread' : ''} priority-${escapeHtml(priority)}"
                 role="option" tabindex="-1"
                 data-notification-id="${escapeHtml(String(item.id || ''))}"
                 aria-label="${escapeHtml(item.title || 'Notification')}">
            <span class="nc-glyph">${notificationGlyph(item.topic)}</span>
            <div class="nc-body">
                <div class="nc-head">
                    <span class="nc-title">${escapeHtml(item.title || 'Notification')}</span>
                    <span class="nc-time">${escapeHtml(notificationTime(item.timestamp))}</span>
                </div>
                ${item.message ? `<div class="nc-message">${escapeHtml(item.message)}</div>` : ''}
                ${stackCount > 1 ? `<button class="nc-stack-toggle" type="button" data-stack-toggle>${stackCount - 1} more from this source</button>` : ''}
            </div>
            <span class="nc-priority" title="${escapeHtml(priority)} priority"></span>
            <button class="nc-dismiss" type="button" data-notification-dismiss
                    title="Dismiss" aria-label="Dismiss notification">×</button>
        </article>
    `;
}

function renderNotificationCenterWidget(data, body) {
    const notifications = Array.isArray(data.notifications) ? data.notifications : [];
    const unread = notifications.filter((n) => !n.read).length;

    if (!notifications.length) {
        body.innerHTML = `
            <section class="nc-app">
                <header class="nc-header">
                    <span class="nc-heading">Notifications</span>
                </header>
                <div class="nc-empty">
                    <span class="nc-empty-glyph">◉</span>
                    <span class="nc-empty-title">No notifications</span>
                    <span class="nc-empty-hint">Monitors will report here when something changes.</span>
                </div>
            </section>
        `;
        return;
    }

    const buckets = [['Today', []], ['Yesterday', []], ['Earlier', []]];
    const byName = new Map(buckets);

    notifications.forEach((item) => {
        byName.get(notificationBucket(item.timestamp)).push(item);
    });

    const groupsHtml = buckets.filter(([, items]) => items.length).map(([name, items]) => {
        const stacks = stackNotifications(items);

        return `
            <section class="nc-group" data-group="${escapeHtml(name.toLowerCase())}">
                <div class="nc-group-title">
                    <span>${escapeHtml(name)}</span>
                    <span class="nc-group-count">${items.length}</span>
                </div>
                ${stacks.map((stack) => `
                    <div class="nc-stack" data-stack-key="${escapeHtml(stack.key)}">
                        ${notificationRowHtml(stack.lead, { stackCount: stack.items.length })}
                        <div class="nc-stack-rest" hidden>
                            ${stack.items.slice(1).map((item) => notificationRowHtml(item)).join('')}
                        </div>
                    </div>`).join('')}
            </section>`;
    }).join('');

    body.innerHTML = `
        <section class="nc-app">
            <header class="nc-header">
                <span class="nc-heading">Notifications</span>
                ${unread ? `<span class="nc-unread-badge">${unread}</span>` : ''}
                <div class="nc-header-actions">
                    <button class="nc-action" type="button" data-nc-action="mark_read"
                            ${unread ? '' : 'disabled'}>Mark all read</button>
                    <button class="nc-action nc-action-danger" type="button" data-nc-action="clear_all">Clear all</button>
                </div>
            </header>

            <div class="nc-list" role="listbox" tabindex="0" aria-label="Notifications">
                ${groupsHtml}
            </div>
        </section>
    `;

    attachNotificationCenterControls(body);
}

/**
 * Actions go to the backend and the panel redraws from the rebroadcast history,
 * so what is on screen is always what is actually stored.
 */
function attachNotificationCenterControls(body) {
    const list = body.querySelector('.nc-list');

    const send = (action, ids) => socket.emit('notification_action', { action, ids: ids || [] });

    body.addEventListener('click', (event) => {
        const headerAction = event.target.closest('[data-nc-action]');

        if (headerAction) {
            event.preventDefault();
            event.stopPropagation();
            send(headerAction.dataset.ncAction);
            return;
        }

        const toggle = event.target.closest('[data-stack-toggle]');

        if (toggle) {
            event.preventDefault();
            event.stopPropagation();
            const rest = toggle.closest('.nc-stack')?.querySelector('.nc-stack-rest');

            if (rest) {
                rest.hidden = !rest.hidden;
                toggle.textContent = rest.hidden
                    ? `${rest.children.length} more from this source`
                    : 'Show less';
            }
            return;
        }

        const dismiss = event.target.closest('[data-notification-dismiss]');

        if (dismiss) {
            event.preventDefault();
            event.stopPropagation();
            const row = dismiss.closest('.nc-row');
            // Animate out first so the row does not vanish under the cursor, then
            // remove it from layout regardless of what the backend does. Fading
            // alone left an invisible row still holding its space, so a dismissal
            // that was never confirmed would leave a permanent gap in the list.
            row?.classList.add('is-leaving');
            window.setTimeout(() => { if (row) row.style.display = 'none'; }, 220);
            send('dismiss', [row?.dataset.notificationId]);
            return;
        }

        const row = event.target.closest('.nc-row');

        if (row) {
            focusNotificationRow(row);
            send('mark_read', [row.dataset.notificationId]);
        }
    });

    if (!list) {
        return;
    }

    // Keyboard: move with arrows, mark read with Enter, dismiss with Delete.
    list.addEventListener('keydown', (event) => {
        const rows = [...list.querySelectorAll('.nc-row')].filter((r) => r.offsetParent !== null);

        if (!rows.length) {
            return;
        }

        const current = list.querySelector('.nc-row.is-active');
        const index = Math.max(0, rows.indexOf(current));

        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            const next = rows[Math.min(rows.length - 1, Math.max(0,
                index + (event.key === 'ArrowDown' ? 1 : -1)))];
            focusNotificationRow(next);
            return;
        }

        if (!current) {
            return;
        }

        if (event.key === 'Enter') {
            event.preventDefault();
            send('mark_read', [current.dataset.notificationId]);
            return;
        }

        if (event.key === 'Delete' || event.key === 'Backspace') {
            event.preventDefault();
            current.classList.add('is-leaving');
            window.setTimeout(() => { current.style.display = 'none'; }, 220);
            send('dismiss', [current.dataset.notificationId]);
        }
    });
}

function focusNotificationRow(row) {
    if (!row) {
        return;
    }

    row.closest('.nc-list')?.querySelectorAll('.nc-row.is-active')
        .forEach((el) => el.classList.remove('is-active'));
    row.classList.add('is-active');
    row.scrollIntoView({ block: 'nearest' });
}

function hotbarItems() {
    return [
        { icon: '♫', label: 'Music', subtitle: 'Controls', command: 'open_music' },
        { icon: '◫', label: 'Calendar', subtitle: 'Operations page', command: 'open_calendar' },
        { icon: '✎', label: 'Notes', subtitle: 'Local notes', command: 'open_notes' },
        { icon: '☑', label: 'Tasks', subtitle: 'Mission queue', command: 'open_tasks' },
        { icon: '☁', label: 'Weather', subtitle: 'Local forecast', command: 'open_weather' },
        { icon: '≡', label: 'Intel Briefing', subtitle: 'News desk', command: 'open_intel' },
        { icon: '⌖', label: 'Tactical Map', subtitle: 'Map page', command: 'open_map' },
        { icon: '∿', label: 'System Health', subtitle: 'Diagnostics', command: 'open_system_health' },
        { icon: '⚙', label: 'Settings', subtitle: 'System config', command: 'open_settings' },
        { icon: '◉', label: 'Notifications', subtitle: 'Alert center', command: 'open_notifications' },
        { icon: '◇', label: 'Proactive Monitor', subtitle: 'Alert status', command: 'open_proactive_monitor' },
        { icon: '⌧', label: 'Clear HUD', subtitle: 'Reset workspace', command: 'clear_hud' },
        { icon: '◌', label: 'Sleep Screen', subtitle: 'Standby layer', command: 'open_sleep_screen' },
        { icon: '⌂', label: 'Workstation / Home', subtitle: 'Return to HUD', command: 'open_workstation' }
    ];
}

function createHotbar() {
    let shell = document.querySelector('.friday-hotbar-shell');

    if (shell) {
        return shell;
    }

    shell = document.createElement('section');
    shell.className = 'friday-hotbar-shell';
    shell.innerHTML = `
        <nav class="friday-hotbar" aria-label="FRIDAY hotbar">
            <button class="hotbar-button primary" type="button" data-hotbar-command="open_music"><span>♫</span><strong>MUSIC</strong></button>
            <button class="hotbar-button primary" type="button" data-hotbar-command="open_calendar"><span>◫</span><strong>CAL</strong></button>
            <button class="hotbar-button primary" type="button" data-hotbar-command="open_notes"><span>✎</span><strong>NOTES</strong></button>
            <button class="hotbar-button primary" type="button" data-hotbar-command="open_task_widget"><span>☑</span><strong>TASKS</strong></button>
            <button class="hotbar-settings-button" type="button" data-hotbar-command="open_settings" aria-label="Open settings">⚙</button>
            <button class="hotbar-grid-button" type="button" aria-label="Open widget launcher"><span></span><span></span><span></span><span></span></button>
        </nav>
        <section class="widget-launcher-panel" aria-label="Widget launcher"></section>
    `;

    document.body.appendChild(shell);

    shell.querySelectorAll('[data-hotbar-command]').forEach((button) => {
        button.addEventListener('click', () => {
            handleHotbarAction(button.dataset.hotbarCommand || '');
        });
    });

    shell.querySelector('.hotbar-grid-button')?.addEventListener('click', () => {
        toggleWidgetLauncher();
    });

    renderWidgetLauncher();
    return shell;
}

function toggleWidgetLauncher(forceOpen) {
    const shell = createHotbar();
    widgetLauncherOpen = typeof forceOpen === 'boolean' ? forceOpen : !widgetLauncherOpen;
    shell.classList.toggle('launcher-open', widgetLauncherOpen);
}

function renderWidgetLauncher() {
    const shell = createHotbar();
    const panel = shell.querySelector('.widget-launcher-panel');

    if (!panel) {
        return;
    }

    panel.innerHTML = `
        <div class="widget-launcher-top">
            <span>WIDGET LAUNCHER</span>
            <button type="button" class="widget-launcher-close">CLOSE</button>
        </div>
        <div class="widget-launcher-grid">
            ${hotbarItems().map((item) => `
                <button class="widget-launcher-item" type="button" data-launch-command="${escapeHtml(item.command)}" data-launch-local="${escapeHtml(item.local || '')}">
                    <span class="widget-launcher-icon">${escapeHtml(item.icon)}</span>
                    <strong class="widget-launcher-label">${escapeHtml(item.label)}</strong>
                    <em class="widget-launcher-subtitle">${escapeHtml(item.subtitle)}</em>
                </button>
            `).join('')}
        </div>
    `;

    panel.querySelector('.widget-launcher-close')?.addEventListener('click', () => {
        toggleWidgetLauncher(false);
    });

    panel.querySelectorAll('[data-launch-command]').forEach((button) => {
        button.addEventListener('click', () => {
            const localAction = button.dataset.launchLocal || '';

            handleHotbarAction(button.dataset.launchCommand || '');
            toggleWidgetLauncher(false);
        });
    });
}

function handleHotbarAction(command) {
    const text = String(command || '').trim();

    if (!text) {
        return;
    }

    const workshopWidget = normalizeWorkshopWidgetType(text);

    if (isWorkshopSurfaceActive() && WORKSHOP_WIDGET_COMMANDS[workshopWidget]) {
        openWorkshopWidget(workshopWidget, { source: 'hotbar' });
        return;
    }

    emitDirectAction(text, 'hotbar');
}

function ensureSettingsPage() {
    let page = document.querySelector('.settings-page');

    if (page) {
        return page;
    }

    page = document.createElement('section');
    page.className = 'settings-page';
    page.setAttribute('aria-label', 'FRIDAY settings page');
    document.body.appendChild(page);
    return page;
}

function closeSettingsPageLocal() {
    const page = document.querySelector('.settings-page');

    if (!page) {
        return;
    }

    page.classList.remove('active');
    document.body.classList.remove('settings-page-open');
}

function ensureTasksPage() {
    let page = document.querySelector('.tasks-page');

    if (page) {
        return page;
    }

    page = document.createElement('section');
    page.className = 'tasks-page';
    page.setAttribute('aria-label', 'FRIDAY Tasks and Reminders');
    document.body.appendChild(page);
    return page;
}

function closeTasksPageLocal() {
    const page = document.querySelector('.tasks-page');

    if (!page) {
        return;
    }

    page.classList.remove('active');
    document.body.classList.remove('tasks-page-open');
}

function taskPageFilterTasks(payload) {
    const safe = normalizeTasksPayload(payload || tasksPayload || {});

    if (tasksPageFilter === 'today') {
        return {
            title: 'TODAY',
            groups: [['TODAY', safe.today]]
        };
    }

    if (tasksPageFilter === 'upcoming') {
        return {
            title: 'UPCOMING',
            groups: [
                ['TOMORROW', safe.tomorrow],
                ['THIS WEEK', safe.this_week],
                ['LATER', safe.later]
            ]
        };
    }

    if (tasksPageFilter === 'completed') {
        return {
            title: 'COMPLETED',
            groups: [['COMPLETED', safe.completed]]
        };
    }

    return {
        title: 'ALL',
        groups: [
            ['OVERDUE', safe.overdue],
            ['TODAY', safe.today],
            ['TOMORROW', safe.tomorrow],
            ['THIS WEEK', safe.this_week],
            ['LATER', safe.later],
            ['COMPLETED', safe.completed]
        ]
    };
}

function renderTasksPage(payload = {}) {
    tasksPayload = normalizeTasksPayload(payload || tasksPayload || {});
    closeSettingsPageLocal();
    const page = ensureTasksPage();
    const counts = tasksPayload.counts || {};
    const filtered = taskPageFilterTasks(tasksPayload);

    page.classList.add('active');
    document.body.classList.add('tasks-page-open');

    page.innerHTML = `
        <div class="tasks-page-gridlines"></div>
        <header class="tasks-page-header">
            <div>
                <div class="tasks-page-kicker">MISSION QUEUE</div>
                <h1>TASKS / REMINDERS</h1>
            </div>
            <div class="tasks-page-counts">
                <div><span>ACTIVE</span><strong>${escapeHtml(counts.total_active || 0)}</strong></div>
                <div><span>OVERDUE</span><strong>${escapeHtml(counts.overdue || 0)}</strong></div>
                <div><span>TODAY</span><strong>${escapeHtml(counts.today || 0)}</strong></div>
                <div><span>DONE</span><strong>${escapeHtml(counts.completed || 0)}</strong></div>
            </div>
            <button class="tasks-page-close" type="button">RETURN</button>
        </header>
        <main class="tasks-page-layout">
            <aside class="tasks-page-sidebar">
                ${['all', 'today', 'upcoming', 'completed'].map((filter) => `
                    <button class="${tasksPageFilter === filter ? 'active' : ''}" type="button" data-tasks-filter="${filter}">${filter.toUpperCase()}</button>
                `).join('')}
                <button type="button" data-tasks-direct="open_task_widget">OPEN WIDGET</button>
                <button type="button" data-tasks-direct="clear_completed_tasks">CLEAR DONE</button>
            </aside>
            <section class="tasks-page-main">
                <div class="tasks-page-section-title">${escapeHtml(filtered.title)}</div>
                <div class="tasks-page-groups">
                    ${filtered.groups.map(([title, items]) => renderTaskGroup(title, items)).join('')}
                </div>
            </section>
            <aside class="tasks-page-add">
                <div class="tasks-page-section-title">ADD TASK</div>
                <form class="tasks-page-form">
                    <label><span>Task title</span><input name="title" type="text" autocomplete="off"></label>
                    <label><span>Due text</span><input name="due" type="text" placeholder="tomorrow at 8 PM" autocomplete="off"></label>
                    <label><span>Priority</span>
                        <select name="priority">
                            <option value="normal">NORMAL</option>
                            <option value="low">LOW</option>
                            <option value="high">HIGH</option>
                            <option value="urgent">URGENT</option>
                        </select>
                    </label>
                    <button type="submit">ADD TASK</button>
                </form>
            </aside>
        </main>
    `;

    page.querySelector('.tasks-page-close')?.addEventListener('click', () => {
        emitDirectAction('close_tasks', 'tasks_ui');
    });

    page.querySelectorAll('[data-tasks-filter]').forEach((button) => {
        button.addEventListener('click', () => {
            tasksPageFilter = button.dataset.tasksFilter || 'all';
            renderTasksPage(tasksPayload);
        });
    });

    page.querySelectorAll('[data-tasks-direct]').forEach((button) => {
        button.addEventListener('click', () => {
            emitDirectAction(button.dataset.tasksDirect || '', 'tasks_ui');
        });
    });

    page.querySelector('.tasks-page-form')?.addEventListener('submit', (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const title = form.elements.title?.value?.trim() || '';
        const due = form.elements.due?.value?.trim() || '';
        const priority = form.elements.priority?.value || 'normal';

        if (!title) {
            return;
        }

        emitDirectAction('add_task', 'tasks_ui', {
            title: due ? `${title} ${due}` : title,
            priority
        });
        form.reset();
    });

    attachTaskControls(page);
}

function updateTaskSurfaces(payload = {}) {
    tasksPayload = normalizeTasksPayload(payload || tasksPayload || {});

    document.querySelectorAll('.hud-card.widget-type-tasks .native-widget-body').forEach((body) => {
        renderTasksWidget(tasksPayload, body);
    });

    if (document.querySelector('.tasks-page.active')) {
        renderTasksPage(tasksPayload);
    }
}

function openSettingsPage(payload = {}) {
    settingsPayload = payload || settingsPayload || {};
    closeTasksPageLocal();
    const page = ensureSettingsPage();
    renderSettingsPage();
    page.classList.add('active');
    document.body.classList.add('settings-page-open');
}

function settingsStatusClass(value) {
    const text = String(value ?? '').toLowerCase();

    if (text.includes('online') || text.includes('connected') || text.includes('found') || text.includes('present') || text.includes('available') || text === 'on') {
        return 'online';
    }

    if (text.includes('disabled') || text.includes('hidden') || text.includes('safe')) {
        return 'disabled';
    }

    if (text.includes('missing') || text.includes('offline') || text.includes('not connected') || text.includes('unavailable') || text.includes('away')) {
        return 'offline';
    }

    return 'neutral';
}

function settingsBadge(value) {
    const text = String(value ?? 'Unknown');
    return `<span class="settings-badge ${settingsStatusClass(text)}">${escapeHtml(text)}</span>`;
}

function settingsMetric(label, value, badge = false) {
    const renderedValue = badge ? settingsBadge(value) : `<strong>${escapeHtml(value ?? 'Unknown')}</strong>`;
    return `
        <div class="settings-metric">
            <span>${escapeHtml(label)}</span>
            ${renderedValue}
        </div>
    `;
}

function settingsTimestamp(value) {
    if (!value) {
        return 'Unknown';
    }

    try {
        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return String(value);
        }

        return date.toLocaleString([], {
            month: 'short',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit'
        });
    } catch (_) {
        return String(value);
    }
}

function settingsIdleLabel(value) {
    if (value === null || value === undefined || value === '') {
        return 'Unknown';
    }

    const seconds = Number(value);

    if (!Number.isFinite(seconds)) {
        return 'Unknown';
    }

    if (seconds < 60) {
        return `${Math.max(0, Math.round(seconds))} seconds`;
    }

    const minutes = Math.round(seconds / 60);
    return `${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`;
}

function settingsSection(title, itemsHtml, extraClass = '') {
    return `
        <section class="settings-section ${escapeHtml(extraClass)}">
            <div class="settings-section-title">${escapeHtml(title)}</div>
            <div class="settings-section-body">${itemsHtml}</div>
        </section>
    `;
}

function settingsToggleRow(label, key, enabled, action, locked = false, detail = '') {
    const safeAction = action || `toggle_${key}`;
    const detailText = locked && !detail ? 'Locked' : detail;

    return `
        <div class="settings-toggle-row ${locked ? 'locked' : ''}">
            <span>${escapeHtml(label)}${detailText ? `<em>${escapeHtml(detailText)}</em>` : ''}</span>
            <button class="settings-toggle ${enabled ? 'on' : ''} ${locked ? 'locked' : ''}" type="button"
                data-settings-toggle="${escapeHtml(key)}"
                data-settings-action="${escapeHtml(safeAction)}"
                aria-pressed="${enabled ? 'true' : 'false'}"
                ${locked ? 'disabled aria-disabled="true"' : ''}><i></i></button>
        </div>
    `;
}

function settingsVoiceProviderControls(provider, fishConfigured, fishState) {
    const active = ['friday_local', 'fish', 'auto'].includes(String(provider || '').toLowerCase())
        ? String(provider).toLowerCase()
        : 'friday_local';
    const fishDisabled = fishConfigured ? '' : 'disabled aria-disabled="true"';

    let note = '';

    if (!fishConfigured) {
        note = 'Fish not configured';
    } else if (fishState === 'backoff') {
        note = 'Fish unavailable - local fallback active';
    }

    // Fish Audio is the production FRIDAY voice and is listed first.
    return `
        <div class="settings-choice-row">
            <span>Voice Provider${note ? `<em>${escapeHtml(note)}</em>` : ''}</span>
            <div>
                <button class="${active === 'friday_local' ? 'active' : ''}" type="button" data-settings-voice-provider="friday_local">FRIDAY LOCAL</button>
                <button class="${active === 'fish' ? 'active' : ''}" type="button" data-settings-voice-provider="fish" ${fishDisabled}>FISH AUDIO</button>
                <button class="${active === 'auto' ? 'active' : ''}" type="button" data-settings-voice-provider="auto" ${fishDisabled}>AUTO</button>
            </div>
        </div>
    `;
}

function settingsStartupControls(startupMode) {
    const mode = String(startupMode || 'sleep').toLowerCase() === 'workstation' ? 'workstation' : 'sleep';

    return `
        <div class="settings-choice-row">
            <span>Startup Mode</span>
            <div>
                <button class="${mode === 'sleep' ? 'active' : ''}" type="button" data-settings-startup="sleep">SLEEP</button>
                <button class="${mode === 'workstation' ? 'active' : ''}" type="button" data-settings-startup="workstation">WORKSTATION</button>
            </div>
        </div>
    `;
}

function renderThemeCards(payload) {
    const currentTheme = normalizeThemeName(payload.theme || payload.settings?.theme || latestState?.theme || 'blue');
    const themes = [
        { id: 'blue', label: 'FRIDAY Blue', subtitle: 'Default workstation' },
        { id: 'green', label: 'Tactical Green', subtitle: 'Green HUD accents' },
        { id: 'white', label: 'White Mode', subtitle: 'Soft light interface' },
        { id: 'midnight', label: 'Midnight', subtitle: 'Reduced glow, deeper black' },
        { id: 'graphite', label: 'Graphite', subtitle: 'Monochrome engineering HUD' },
        { id: 'high-contrast', label: 'High Contrast', subtitle: 'Stronger borders and text' }
    ];

    return `
        <div class="settings-theme-grid">
            ${themes.map((theme) => `
                <button class="settings-theme-card theme-preview-${escapeHtml(theme.id)} ${currentTheme === theme.id ? 'active' : ''}" type="button" data-settings-theme="${escapeHtml(theme.id)}">
                    <span class="settings-theme-swatch"></span>
                    <strong>${escapeHtml(theme.label)}</strong>
                    <em>${escapeHtml(theme.subtitle)}</em>
                </button>
            `).join('')}
        </div>
    `;
}

function attachSettingsControlHandlers(root) {
    if (!root) {
        return;
    }

    root.querySelectorAll('[data-settings-theme]').forEach((button) => {
        button.addEventListener('click', () => {
            const theme = button.dataset.settingsTheme || 'blue';
            applyTheme(theme);
            emitDirectAction('set_theme', 'settings_ui', { value: theme });
        });
    });

    root.querySelectorAll('[data-settings-toggle]').forEach((button) => {
        button.addEventListener('click', () => {
            if (button.disabled || button.classList.contains('locked')) {
                return;
            }

            const action = button.dataset.settingsAction || '';
            const enabled = !button.classList.contains('on');

            button.classList.toggle('on', enabled);
            button.setAttribute('aria-pressed', enabled ? 'true' : 'false');

            if (action) {
                emitDirectAction(action, 'settings_ui', { value: enabled });
            }
        });
    });

    root.querySelectorAll('[data-settings-voice-provider]').forEach((button) => {
        button.addEventListener('click', () => {
            if (button.disabled) {
                return;
            }

            emitDirectAction('set_voice_provider', 'settings_ui', {
                value: button.dataset.settingsVoiceProvider || 'local'
            });
        });
    });

    root.querySelector('[data-settings-save-voice-name]')?.addEventListener('click', () => {
        const input = root.querySelector('[data-settings-voice-name-input]');
        emitDirectAction('set_voice_local_name', 'settings_ui', { value: input?.value || '' });
    });

    root.querySelector('[data-settings-save-voice-rate]')?.addEventListener('click', () => {
        const input = root.querySelector('[data-settings-voice-rate-input]');
        emitDirectAction('set_voice_rate', 'settings_ui', { value: input?.value || 178 });
    });

    root.querySelector('[data-settings-save-voice-pause]')?.addEventListener('click', () => {
        const input = root.querySelector('[data-settings-voice-pause-input]');
        emitDirectAction('set_voice_pause_threshold', 'settings_ui', { value: input?.value || 0.6 });
    });

    root.querySelector('[data-settings-save-voice-followup]')?.addEventListener('click', () => {
        const input = root.querySelector('[data-settings-voice-followup-input]');
        emitDirectAction('set_voice_follow_up_window', 'settings_ui', { value: input?.value ?? 10 });
    });

    root.querySelectorAll('[data-settings-startup]').forEach((button) => {
        button.addEventListener('click', () => {
            const mode = button.dataset.settingsStartup || 'sleep';
            emitDirectAction('set_startup_mode', 'settings_ui', { value: mode });
        });
    });

    root.querySelector('[data-settings-save-location]')?.addEventListener('click', () => {
        const input = root.querySelector('[data-settings-location-input]');
        emitDirectAction('set_default_location', 'settings_ui', { value: input?.value || '' });
    });

    root.querySelector('[data-settings-save-presence-timeout]')?.addEventListener('click', () => {
        const input = root.querySelector('[data-settings-presence-timeout-input]');
        emitDirectAction('set_presence_idle_timeout', 'settings_ui', { value: input?.value || 10 });
    });

    root.querySelector('[data-settings-save-camera-door-index]')?.addEventListener('click', () => {
        const input = root.querySelector('[data-settings-camera-door-index-input]');
        emitDirectAction('set_camera_presence_door_index', 'settings_ui', { value: input?.value || 1 });
    });

    root.querySelector('[data-settings-save-camera-desk-index]')?.addEventListener('click', () => {
        const input = root.querySelector('[data-settings-camera-desk-index-input]');
        emitDirectAction('set_camera_presence_desk_index', 'settings_ui', { value: input?.value || 0 });
    });

    root.querySelectorAll('[data-settings-direct]').forEach((button) => {
        button.addEventListener('click', () => {
            emitDirectAction(button.dataset.settingsDirect || '', 'settings_ui');
        });
    });
}

function renderSettingsWidget(data, body) {
    settingsPayload = settingsPayload || data || {};
    body.innerHTML = '<section class="settings-page embedded-settings-surface active embedded-widget" aria-label="FRIDAY embedded settings"></section>';
    renderSettingsSurface(body.querySelector('.embedded-settings-surface'), true);
}

function updateSettingsWidgetSurfaces(payload = settingsPayload) {
    document.querySelectorAll('.hud-card.widget-type-settings .native-widget-body').forEach((body) => {
        renderSettingsWidget(payload || {}, body);
    });
}

function renderSettingsSurface(page = ensureSettingsPage(), embedded = false) {
    if (!page) {
        return;
    }

    page.dataset.embedded = embedded ? 'true' : 'false';
    const payload = settingsPayload || {};
    const settings = payload.settings || latestState?.settings || {};
    const currentTheme = normalizeThemeName(payload.theme || settings.theme || latestState?.theme || 'blue');
    const voiceEnabled = payload.voice_enabled !== undefined ? payload.voice_enabled : settings.voice_enabled !== false;
    const voiceProvider = String(payload.voice_provider || settings.voice_provider || 'friday_local').toLowerCase();
    const fishState = String(payload.fish_state || '').toLowerCase();
    const voiceRate = Number(payload.voice_rate ?? settings.voice_rate ?? 178);
    const voicePauseThreshold = Number(payload.voice_pause_threshold ?? settings.voice_pause_threshold ?? 0.6);
    const voiceFollowUpWindow = Number(payload.voice_follow_up_window_seconds ?? settings.voice_follow_up_window_seconds ?? 10);
    const voiceAudioReactiveOrb = payload.voice_audio_reactive_orb !== undefined ? payload.voice_audio_reactive_orb : settings.voice_audio_reactive_orb !== false;
    const voicePerformanceLogs = payload.voice_performance_logs !== undefined ? payload.voice_performance_logs : settings.voice_performance_logs !== false;
    const voiceInterruptEnabled = payload.voice_interrupt_enabled !== undefined ? payload.voice_interrupt_enabled : settings.voice_interrupt_enabled !== false;
    const performanceLogs = payload.performance_logs !== undefined ? payload.performance_logs : settings.performance_logs !== false;
    const showNotifications = payload.show_notifications !== undefined ? payload.show_notifications : settings.show_notifications !== false;
    const showHotbar = payload.show_hotbar !== undefined ? payload.show_hotbar : settings.show_hotbar !== false;
    const showTaskWidgetOnWake = payload.show_task_widget_on_wake !== undefined ? payload.show_task_widget_on_wake : settings.show_task_widget_on_wake === true;
    const silentOperatorEnabled = payload.silent_operator_enabled !== undefined ? payload.silent_operator_enabled : settings.silent_operator_enabled !== false;
    const workshopRestoreLayout = payload.workshop_restore_layout !== undefined ? payload.workshop_restore_layout : settings.workshop_restore_layout !== false;
    const showcaseReturnSleep = payload.showcase_return_to_sleep !== undefined ? payload.showcase_return_to_sleep : settings.showcase_return_to_sleep !== false;
    const privacyMode = payload.privacy_mode !== undefined ? payload.privacy_mode : settings.privacy_mode === true;
    const startupMode = payload.startup_mode || settings.startup_mode || 'sleep';
    const defaultLocation = payload.default_location || settings.default_location || 'Warrenville, IL';
    const calendarStatus = payload.calendar_connected ? 'CONNECTED' : 'NOT CONNECTED';
    const presence = payload.presence || {};
    const presenceModeEnabled = payload.presence_mode_enabled !== undefined ? payload.presence_mode_enabled : settings.presence_mode_enabled === true;
    const presenceReturnGreeting = payload.presence_return_greeting_enabled !== undefined ? payload.presence_return_greeting_enabled : settings.presence_return_greeting_enabled !== false;
    const presenceAutoSleep = payload.presence_auto_sleep_enabled !== undefined ? payload.presence_auto_sleep_enabled : settings.presence_auto_sleep_enabled !== false;
    const presenceAutoLaunchUi = payload.presence_auto_launch_ui !== undefined ? payload.presence_auto_launch_ui : settings.presence_auto_launch_ui === true;
    const presenceAutoOpenWorkstation = payload.presence_auto_open_workstation !== undefined ? payload.presence_auto_open_workstation : settings.presence_auto_open_workstation === true;
    const presenceTimeout = Number(payload.presence_away_timeout_minutes || presence.away_timeout_minutes || settings.presence_idle_timeout_minutes || 10);
    const presenceStatus = payload.presence_user || presence.user_presence || 'Unknown';
    const presenceIdleSeconds = payload.presence_idle_seconds !== undefined ? payload.presence_idle_seconds : presence.idle_seconds;
    const presenceLastSeen = payload.presence_last_seen || presence.last_seen_at;
    const presenceAwaySince = payload.presence_away_since || presence.away_since;
    const cameraPresence = payload.camera_presence || {};
    const cameraPresenceEnabled = payload.camera_presence_enabled !== undefined ? payload.camera_presence_enabled : settings.camera_presence_enabled === true;
    const cameraPresenceMode = payload.camera_presence_mode || cameraPresence.mode || 'IDLE';
    const cameraPresenceActiveCamera = payload.camera_presence_active_camera || cameraPresence.active_camera_label || 'None';
    const cameraPresenceDoorIndex = Number(payload.camera_presence_door_device_index ?? cameraPresence.door_camera_index ?? settings.camera_presence_door_device_index ?? 1);
    const cameraPresenceDeskIndex = Number(payload.camera_presence_desk_device_index ?? cameraPresence.desk_camera_index ?? settings.camera_presence_desk_device_index ?? 0);
    const cameraPresenceLastDoor = payload.camera_presence_last_door_seen_at || cameraPresence.last_door_seen_at;
    const cameraPresenceLastDesk = payload.camera_presence_last_desk_seen_at || cameraPresence.last_desk_seen_at;
    const cameraPresenceLastSwitch = payload.camera_presence_last_camera_switch_at || cameraPresence.last_camera_switch_at;
    const cameraPresenceMotionScore = payload.camera_presence_last_motion_score ?? cameraPresence.last_motion_score ?? 0;
    const cameraPresenceStatus = payload.camera_presence_status || cameraPresence.camera_status || 'Unavailable';
    const cameraPresenceInterval = Number(payload.camera_presence_check_interval_seconds ?? cameraPresence.check_interval_seconds ?? settings.camera_presence_check_interval_seconds ?? 10);
    const cameraPresenceUnknownGreeting = payload.camera_presence_unknown_greeting_enabled !== undefined ? payload.camera_presence_unknown_greeting_enabled : cameraPresence.unknown_greeting_enabled === true;
    const cameraPresencePrimaryGreeting = payload.camera_presence_primary_user_greeting_enabled !== undefined ? payload.camera_presence_primary_user_greeting_enabled : cameraPresence.primary_user_greeting_enabled === true;
    const cameraPresenceGreetingsEnabled = cameraPresenceUnknownGreeting || cameraPresencePrimaryGreeting;
    const cameraPresenceAutoHandoff = payload.camera_presence_auto_handoff_enabled !== undefined ? payload.camera_presence_auto_handoff_enabled : cameraPresence.auto_handoff_enabled === true;

    page.innerHTML = `
        <div class="settings-gridlines"></div>
        <header class="settings-header">
            <div>
                <span class="settings-kicker">SYSTEM CONTROL</span>
                <h1>FRIDAY MK1 SETTINGS</h1>
            </div>
            <div class="settings-status-strip">
                <div><span>MODE</span><strong>${escapeHtml(payload.active_mode || 'Workstation')}</strong></div>
                <div><span>PORT</span><strong>5050</strong></div>
                <div><span>CALENDAR</span><strong>${escapeHtml(calendarStatus)}</strong></div>
            </div>
            <button class="settings-close" type="button" data-settings-direct="close_settings">CLOSE</button>
        </header>

        <main class="settings-layout">
            <div class="settings-column">
                ${settingsSection('System Status', [
                    settingsMetric('FRIDAY OS', 'Online', true),
                    settingsMetric('Port', payload.port || 5050),
                    settingsMetric('Python Version', payload.python_version || 'Unknown'),
                    settingsMetric('Electron UI', payload.electron_ui || 'Online', true),
                    settingsMetric('Active Mode', payload.active_mode || 'Workstation'),
                    settingsMetric('Startup Mode', startupMode === 'workstation' ? 'Workstation' : 'Sleep'),
                    settingsMetric('Performance Logs', performanceLogs ? 'On' : 'Off', true),
                    settingsMetric('Notifications', showNotifications ? 'On' : 'Off', true)
                ].join(''))}
                ${settingsSection('AI Models', [
                    settingsMetric('Gemini API', payload.gemini_api || 'Missing', true),
                    settingsMetric('Main Model', payload.main_model || 'gemini-3-flash-preview'),
                    settingsMetric('Silent Operator', payload.silent_operator_model || 'gemini-2.5-flash'),
                    settingsMetric('Ollama', payload.ollama || 'Not configured', true),
                    settingsMetric('Fast Router', payload.fast_router || 'Disabled', true)
                ].join(''))}
                ${settingsSection('Voice / Audio', [
                    settingsMetric('Assistant', payload.assistant_name || 'FRIDAY', true),
                    settingsMetric('Voice Mode', voiceEnabled ? 'Enabled' : 'Disabled', true),
                    settingsMetric('Active Provider', payload.voice_provider_label || voiceProvider, true),
                    settingsMetric('Fish Audio', payload.fish_audio || 'Unknown', true),
                    settingsMetric('Local Fallback Voice', payload.voice_local_name_resolved || 'System default', true),
                    settingsMetric('Speaking Rate', `${voiceRate} wpm`),
                    settingsMetric('End-of-speech Pause', `${voicePauseThreshold.toFixed(2)} s`),
                    settingsMetric('Follow-up Window', `${voiceFollowUpWindow} seconds`),
                    settingsMetric('Echo Settle', `${payload.voice_echo_settle_ms ?? 220} ms`),
                    settingsMetric('Microphone', payload.microphone || 'Idle', true),
                    settingsMetric('System Volume Control', 'Disabled', true)
                ].join('') + `
                    ${settingsToggleRow('Voice Enabled', 'voice', voiceEnabled, 'toggle_voice')}
                    ${settingsVoiceProviderControls(voiceProvider, payload.fish_configured !== false, fishState)}
                    <label class="settings-input-row">
                        <span>Local Voice Name</span>
                        <input type="text" placeholder="Auto-detect" value="${escapeHtml(payload.voice_local_name || '')}" data-settings-voice-name-input>
                    </label>
                    <button class="settings-action-button" type="button" data-settings-save-voice-name>SAVE VOICE</button>
                    <label class="settings-input-row">
                        <span>Speaking Rate</span>
                        <input type="number" min="90" max="300" step="2" value="${escapeHtml(voiceRate)}" data-settings-voice-rate-input>
                    </label>
                    <button class="settings-action-button" type="button" data-settings-save-voice-rate>SAVE RATE</button>
                    <label class="settings-input-row">
                        <span>End-of-speech Pause (s)</span>
                        <input type="number" min="0.3" max="2" step="0.05" value="${escapeHtml(voicePauseThreshold)}" data-settings-voice-pause-input>
                    </label>
                    <button class="settings-action-button" type="button" data-settings-save-voice-pause>SAVE PAUSE</button>
                    <label class="settings-input-row">
                        <span>Follow-up Window (s)</span>
                        <input type="number" min="0" max="60" step="1" value="${escapeHtml(voiceFollowUpWindow)}" data-settings-voice-followup-input>
                    </label>
                    <button class="settings-action-button" type="button" data-settings-save-voice-followup>SAVE WINDOW</button>
                    ${settingsToggleRow('Audio Reactive Orb', 'voice_audio_reactive_orb', voiceAudioReactiveOrb, 'toggle_voice_audio_reactive_orb')}
                    ${settingsToggleRow('Stop-Speaking Commands', 'voice_interrupt', voiceInterruptEnabled, 'toggle_voice_interrupt')}
                    ${settingsToggleRow('Voice Performance Logs', 'voice_performance_logs', voicePerformanceLogs, 'toggle_voice_performance_logs')}
                    ${settingsToggleRow('System Volume Control Disabled', 'system_volume', false, '', true)}
                `)}
            </div>

            <div class="settings-column wide">
                ${settingsSection('Interface / Theme', `
                    <div class="settings-current-theme"><span>Current Theme</span><strong>${escapeHtml(payload.theme_label || currentTheme.toUpperCase())}</strong></div>
                    ${renderThemeCards(payload)}
                    ${settingsStartupControls(startupMode)}
                    ${settingsToggleRow('Show Hotbar', 'hotbar', showHotbar, 'toggle_hotbar')}
                    ${settingsToggleRow('Show Notifications', 'notifications', showNotifications, 'toggle_notifications')}
                `, 'theme-section')}
                ${settingsSection('Presence Mode', [
                    settingsMetric('Current Status', presenceStatus, true),
                    settingsMetric('Presence Source', payload.presence_source || presence.presence_source || 'Mac idle'),
                    settingsMetric('Idle Time', settingsIdleLabel(presenceIdleSeconds)),
                    settingsMetric('Away Timeout', `${Number.isFinite(presenceTimeout) ? Math.max(1, presenceTimeout) : 10} minutes`),
                    settingsMetric('Last Seen', settingsTimestamp(presenceLastSeen)),
                    settingsMetric('Away Since', settingsTimestamp(presenceAwaySince))
                ].join('') + `
                    ${settingsToggleRow('Presence Mode', 'presence_mode', presenceModeEnabled, 'toggle_presence_mode')}
                    ${settingsToggleRow('Return Greeting', 'presence_return_greeting', presenceReturnGreeting, 'toggle_presence_return_greeting')}
                    ${settingsToggleRow('Auto Sleep When Away', 'presence_auto_sleep', presenceAutoSleep, 'toggle_presence_auto_sleep')}
                    ${settingsToggleRow('Auto Launch UI', 'presence_auto_launch_ui', presenceAutoLaunchUi, 'toggle_presence_auto_launch_ui')}
                    ${settingsToggleRow('Auto Open Workstation', 'presence_auto_open_workstation', presenceAutoOpenWorkstation, 'toggle_presence_auto_open_workstation')}
                    <label class="settings-input-row">
                        <span>Away Timeout</span>
                        <input type="number" min="1" step="1" value="${escapeHtml(Number.isFinite(presenceTimeout) ? Math.max(1, presenceTimeout) : 10)}" data-settings-presence-timeout-input>
                    </label>
                    <button class="settings-action-button" type="button" data-settings-save-presence-timeout>SAVE TIMEOUT</button>
                    <button class="settings-action-button" type="button" data-settings-direct="refresh_presence_status">REFRESH STATUS</button>
                `)}
                ${settingsSection('Camera Presence', [
                    settingsMetric('Current Mode', cameraPresenceMode, true),
                    settingsMetric('Active Camera', cameraPresenceActiveCamera),
                    settingsMetric('Door Camera Index', Number.isFinite(cameraPresenceDoorIndex) ? cameraPresenceDoorIndex : 1),
                    settingsMetric('Desk Camera Index', Number.isFinite(cameraPresenceDeskIndex) ? cameraPresenceDeskIndex : 0),
                    settingsMetric('Last Door Detection', settingsTimestamp(cameraPresenceLastDoor)),
                    settingsMetric('Last Desk Detection', settingsTimestamp(cameraPresenceLastDesk)),
                    settingsMetric('Last Camera Switch', settingsTimestamp(cameraPresenceLastSwitch)),
                    settingsMetric('Last Motion Score', cameraPresenceMotionScore),
                    settingsMetric('Check Interval', `${Number.isFinite(cameraPresenceInterval) ? Math.max(5, cameraPresenceInterval) : 10} seconds`),
                    settingsMetric('Greetings', cameraPresenceGreetingsEnabled ? 'Enabled' : 'Disabled', true),
                    settingsMetric('Auto Handoff', cameraPresenceAutoHandoff ? 'On' : 'Off', true),
                    settingsMetric('Camera Status', cameraPresenceStatus, true)
                ].join('') + `
                    ${settingsToggleRow('Camera Presence', 'camera_presence', cameraPresenceEnabled, 'toggle_camera_presence')}
                    <label class="settings-input-row">
                        <span>Door Camera Index</span>
                        <input type="number" min="0" step="1" value="${escapeHtml(Number.isFinite(cameraPresenceDoorIndex) ? cameraPresenceDoorIndex : 1)}" data-settings-camera-door-index-input>
                    </label>
                    <button class="settings-action-button" type="button" data-settings-save-camera-door-index>SAVE DOOR CAMERA</button>
                    <button class="settings-action-button" type="button" data-settings-direct="test_door_camera">TEST DOOR CAMERA</button>
                    <label class="settings-input-row">
                        <span>Desk Camera Index</span>
                        <input type="number" min="0" step="1" value="${escapeHtml(Number.isFinite(cameraPresenceDeskIndex) ? cameraPresenceDeskIndex : 0)}" data-settings-camera-desk-index-input>
                    </label>
                    <button class="settings-action-button" type="button" data-settings-save-camera-desk-index>SAVE DESK CAMERA</button>
                    <button class="settings-action-button" type="button" data-settings-direct="test_desk_camera">TEST DESK CAMERA</button>
                    <button class="settings-action-button" type="button" data-settings-direct="refresh_camera_presence_status">REFRESH CAMERA STATUS</button>
                `)}
                ${settingsSection('Calendar', [
                    settingsMetric('Google Calendar', calendarStatus, true),
                    settingsMetric('Credentials File', payload.calendar_credentials || 'Missing', true),
                    settingsMetric('Token', payload.calendar_token || 'Missing', true),
                    settingsMetric('Next Event', payload.next_event_summary || 'Unavailable')
                ].join('') + `
                    <label class="settings-input-row">
                        <span>Default Location</span>
                        <input type="text" value="${escapeHtml(defaultLocation)}" data-settings-location-input>
                    </label>
                    <button class="settings-action-button" type="button" data-settings-save-location>SAVE LOCATION</button>
                `)}
                ${settingsSection('Privacy / Safety', [
                    settingsMetric('Guest Mode', 'Coming soon / Disabled', true),
                    settingsToggleRow('Privacy Mode', 'privacy_mode', privacyMode, 'toggle_privacy_mode', false, 'Coming soon'),
                    settingsToggleRow('Face ID Disabled', 'face_id', false, '', true),
                    settingsToggleRow('Desk View Disabled', 'desk_view', false, '', true),
                    settingsMetric('Onshape', payload.onshape || 'Disabled', true),
                    settingsMetric('API Keys', payload.api_keys || 'Hidden', true),
                    settingsMetric('Local Files', payload.local_files || 'Safe root only', true)
                ].join(''))}
            </div>

            <div class="settings-column">
                ${settingsSection('Workshop / Showcase', [
                    settingsMetric('Workshop Mode', payload.workshop_available ? 'Available' : 'Unavailable', true),
                    settingsMetric('Workshop Active', payload.workshop_active ? 'Active' : 'Inactive', true),
                    settingsMetric('Showcase Mode', payload.showcase_available ? 'Available' : 'Unavailable', true),
                    settingsMetric('Showcase Active', payload.showcase_active ? 'Active' : 'Inactive', true),
                    settingsMetric('Layout Memory', payload.layout_saved ? 'Saved' : 'Empty', true),
                    settingsMetric('Displays', payload.display_count || 0)
                ].join('') + `
                    ${settingsToggleRow('Show Task Widget on Wake', 'task_widget_on_wake', showTaskWidgetOnWake, 'toggle_task_widget_on_wake')}
                    ${settingsToggleRow('Silent Operator Enabled', 'silent_operator', silentOperatorEnabled, 'toggle_silent_operator')}
                    ${settingsToggleRow('Workshop Restore Layout', 'workshop_restore_layout', workshopRestoreLayout, 'toggle_workshop_restore_layout')}
                    ${settingsToggleRow('Showcase Return to Sleep', 'showcase_return_sleep', showcaseReturnSleep, 'toggle_showcase_return_sleep')}
                `)}
                ${settingsSection('Developer / Diagnostics', `
                    ${settingsToggleRow('Performance Logs', 'performance', performanceLogs, 'toggle_performance_logs')}
                    <button class="settings-action-button" type="button" data-settings-direct="open_system_health">OPEN SYSTEM HEALTH</button>
                    <button class="settings-action-button" type="button" data-settings-direct="run_health_check">RUN HEALTH CHECK</button>
                    <button class="settings-action-button" type="button" data-settings-direct="clear_hud">CLEAR HUD</button>
                    <button class="settings-action-button" type="button" data-settings-direct="clear_hud">RESET LAYOUT</button>
                    ${settingsMetric('Last Health Check', payload.health_check_status || 'Not run', true)}
                    ${settingsMetric('Health Summary', payload.health_check_summary || 'Health check has not run in this session')}
                    <div class="settings-build-row"><span>Build</span><strong>${escapeHtml(payload.build || 'FRIDAY MK1')}</strong></div>
                `)}
            </div>
        </main>

        <footer class="settings-footer">
            <button class="settings-return" type="button" data-settings-direct="close_settings">RETURN TO WORKSTATION</button>
        </footer>
    `;

    attachSettingsControlHandlers(page);
}

function renderSettingsPage() {
    renderSettingsSurface(ensureSettingsPage(), false);
}

function focusManualOverride() {
    if (chatInput) {
        chatInput.focus();
    }

    overrideResponse.className = 'override-response idle';
    overrideResponse.innerText = 'Manual override focused.';
}

// Every dock entry carries a written label; the glyph is only a visual anchor.
// `widget` is the card type the entry opens, so the dock can show what is
// actually on the workspace rather than guessing.
function workshopDockAppItems() {
    return [
        { icon: '♫', label: 'Music', command: 'open_music', widget: 'music' },
        { icon: '▤', label: 'Calendar', command: 'open_calendar', widget: 'calendar' },
        { icon: '✎', label: 'Notes', command: 'open_notes', widget: 'notes' },
        { icon: '☑', label: 'Tasks', command: 'open_task_widget', widget: 'tasks' },
        { icon: '☁', label: 'Weather', command: 'open_weather', widget: 'weather' },
        { icon: '≣', label: 'Intel', command: 'open_intel', widget: 'news' },
        { icon: '⌖', label: 'Map', command: 'open_map', widget: 'map' },
        { icon: '❐', label: 'Files', command: 'open_files', widget: 'files' },
        { icon: '✚', label: 'Health', command: 'open_system_health', widget: 'system_health' },
        { icon: '⚑', label: 'Alerts', command: 'open_notifications', widget: 'notifications' },
        { icon: '⚙', label: 'Settings', command: 'open_settings', widget: 'settings' }
    ];
}

function workshopDockSystemItems() {
    return [
        { icon: '⊘', label: 'Clear', command: 'clear_hud' },
        { icon: '⌸', label: 'Save', command: 'save_workshop_layout' },
        { icon: '✕', label: 'Exit', command: 'close_workshop_mode' }
    ];
}

/**
 * Widget types currently open on this Workshop surface, so the dock can mark
 * them. Derived from real card state — nothing is assumed from click history.
 */
function workshopOpenWidgetTypes() {
    const cards = Array.isArray(latestState?.active_cards) ? latestState.active_cards : [];
    const open = new Set();

    cards.forEach((card) => {
        const rawType = String(card.type || '').toLowerCase();
        const rawId = String(card.id || '').toLowerCase();

        // Card types are not the dock's keys — "calendar_agenda" is the Calendar
        // app, "virtual_finder" is Files. Resolve through the same alias table
        // the widget launcher uses so the dock marks the right entry.
        const canonical = Object.keys(WORKSHOP_WIDGET_CARD_ALIASES).find((key) => {
            const alias = WORKSHOP_WIDGET_CARD_ALIASES[key];
            return alias.types.includes(rawType) || alias.ids.includes(rawId);
        });

        if (canonical) {
            open.add(canonical);
        }
    });

    return open;
}

function electronIpcRenderer() {
    try {
        const electronRequire = window.require || require;

        if (typeof electronRequire === 'function') {
            return electronRequire('electron').ipcRenderer;
        }
    } catch (_) {
        return null;
    }

    return null;
}

function musicIpcChannel(action) {
    const channels = {
        music_play: 'music:play',
        music_resume: 'music:play',
        music_pause: 'music:pause',
        music_toggle: 'music:toggle',
        music_next: 'music:next',
        music_previous: 'music:previous'
    };

    return channels[String(action || '')] || '';
}

function normalizeMusicPlayerState(value) {
    const state = String(value || '').toLowerCase();
    return ['playing', 'paused', 'stopped'].includes(state) ? state : 'stopped';
}

function updateMusicToggleIcon(button, isPlaying) {
    if (!button) {
        return;
    }

    button.dataset.musicCommand = 'toggle';
    button.dataset.playerState = isPlaying ? 'playing' : 'paused';
    button.setAttribute('aria-label', isPlaying ? 'Pause' : 'Play');

    const icon = button.querySelector('.music-toggle-icon');

    if (icon) {
        icon.textContent = isPlaying ? 'Ⅱ' : '▶';
    }
}

// ==========================================
// MUSIC STATE
// ==========================================
// The widget is patched in place from a periodic poll, never re-rendered wholesale.
// A full re-render on every poll would wipe the playlist scroll position, close the
// open playlist, and drop focus out of the filter box every few seconds.
//
// `musicClock` is the source of truth for the progress bar: Music.app is asked for
// the real position on a slow cadence, and the bar is advanced locally in between.
// Asking AppleScript for `player position` at animation rate would spawn an
// osascript process several times a second for a number that is entirely
// predictable while a track plays.

const musicClock = {
    position: null,     // seconds, as of syncedAt
    duration: null,
    playing: false,
    syncedAt: 0,
    trackKey: ''
};

let musicTickTimer = null;

function musicHasWidget() {
    return Boolean(document.querySelector('.music-panel'));
}

/** Position right now: last real reading plus elapsed time if playing. */
function musicCurrentPosition() {
    if (musicClock.position === null) {
        return null;
    }

    if (!musicClock.playing) {
        return musicClock.position;
    }

    const drift = (Date.now() - musicClock.syncedAt) / 1000;
    const projected = musicClock.position + drift;

    return musicClock.duration ? Math.min(projected, musicClock.duration) : projected;
}

/** Repaint only the bar and the two timestamps. No layout churn. */
function paintMusicProgress() {
    const position = musicCurrentPosition();
    const duration = musicClock.duration;

    document.querySelectorAll('.music-panel').forEach((panel) => {
        const wrap = panel.querySelector('.music-progress');

        if (!wrap) {
            return;
        }

        // No duration reported means nothing honest to draw.
        if (!duration || position === null) {
            wrap.hidden = true;
            return;
        }

        wrap.hidden = false;
        const percent = Math.max(0, Math.min(100, (position / duration) * 100));
        const fill = wrap.querySelector('.music-progress-fill');
        const times = wrap.querySelectorAll('.music-progress-times span');

        if (fill && !wrap.dataset.seeking) {
            fill.style.width = `${percent.toFixed(2)}%`;
        }

        if (times.length === 2 && !wrap.dataset.seeking) {
            times[0].textContent = formatClockSeconds(position);
            times[1].textContent = formatClockSeconds(duration);
        }
    });
}

function ensureMusicTicker() {
    if (musicTickTimer || !musicHasWidget()) {
        return;
    }

    musicTickTimer = window.setInterval(() => {
        if (!musicHasWidget()) {
            window.clearInterval(musicTickTimer);
            musicTickTimer = null;
            return;
        }

        paintMusicProgress();
    }, 500);
}

/** Patch every open music window from a real state payload. */
function applyMusicStatePayload(payload = {}) {
    const playerState = normalizeMusicPlayerState(payload.player_state || payload.state);
    const isPlaying = payload.is_playing === true || playerState === 'playing';
    const track = payload.track || payload.title || '';
    const trackKey = `${track}|${payload.album || ''}`;
    const trackChanged = trackKey !== musicClock.trackKey;

    musicClock.trackKey = trackKey;
    musicClock.playing = isPlaying;
    musicClock.duration = Number.isFinite(payload.duration) ? payload.duration : null;
    musicClock.position = Number.isFinite(payload.position) ? payload.position : null;
    musicClock.syncedAt = Date.now();

    document.querySelectorAll('.music-panel').forEach((panel) => {
        panel.dataset.playing = String(isPlaying);
        panel.dataset.hasTrack = String(Boolean(track) && track !== 'No track loaded');

        const title = panel.querySelector('.music-title');
        const artist = panel.querySelector('.music-artist');
        const album = panel.querySelector('.music-album');
        const art = panel.querySelector('.music-art');
        const play = panel.querySelector('.music-btn-play');

        if (title && track) {
            title.textContent = track === 'No track loaded' ? 'Nothing playing' : track;
            title.title = title.textContent;
        }

        if (artist) {
            artist.textContent = payload.artist || 'Unknown artist';
        }

        if (album) {
            album.textContent = payload.album || '';
        }

        if (art) {
            art.classList.toggle('is-playing', isPlaying);
        }

        if (play) {
            play.textContent = isPlaying ? '⏸' : '▶';
            play.dataset.playerState = playerState;
            play.title = isPlaying ? 'Pause' : 'Play';
            play.setAttribute('aria-label', play.title);
        }

        // Shuffle / repeat reflect Music, not what was last clicked here.
        const shuffleChip = panel.querySelector('[data-music-mode="shuffle"]');
        const repeatChip = panel.querySelector('[data-music-mode="repeat"]');

        if (shuffleChip) {
            shuffleChip.classList.toggle('is-on', payload.shuffle === true);
            shuffleChip.dataset.modeOn = String(payload.shuffle === true);
        }

        if (repeatChip) {
            const repeatOn = String(payload.repeat || 'off').toLowerCase() !== 'off';
            repeatChip.classList.toggle('is-on', repeatOn);
            repeatChip.dataset.modeOn = String(repeatOn);
        }
    });

    paintMusicProgress();
    ensureMusicTicker();

    if (trackChanged) {
        refreshMusicArtwork();
    }
}

/** Cover art follows the track; only fetched when the track actually changed. */
function refreshMusicArtwork() {
    const ipcRenderer = electronIpcRenderer();

    if (!ipcRenderer || !musicHasWidget()) {
        return;
    }

    ipcRenderer.invoke('music:artwork').then((result) => {
        if (!result || result.ok === false) {
            return;
        }

        document.querySelectorAll('.music-art').forEach((art) => {
            if (result.artwork) {
                art.innerHTML = `<img class="music-art-image" src="${result.artwork}" alt="Album artwork">`;
            } else {
                art.innerHTML = '<div class="music-art-fallback" aria-hidden="true"><span>♫</span></div>';
            }
        });
    }).catch(() => {});
}

function refreshMusicState() {
    if (!musicHasWidget()) {
        return Promise.resolve(null);
    }

    const ipcRenderer = electronIpcRenderer();

    if (!ipcRenderer) {
        return Promise.resolve(null);
    }

    return ipcRenderer.invoke('music:state')
        .then((payload) => {
            if (payload && payload.ok !== false) {
                applyMusicStatePayload(payload);
            }

            return payload;
        })
        .catch(() => null)
        .finally(() => {
            if (musicHasWidget()) {
                // Playing needs frequent correction so local interpolation cannot
                // drift; paused state barely changes, so back off and leave the CPU
                // and AppleScript alone.
                scheduleMusicStateRefresh(musicClock.playing ? 4000 : 10000);
            }
        });
}

function scheduleMusicStateRefresh(delayMs = 500) {
    if (musicStateRefreshTimer) {
        window.clearTimeout(musicStateRefreshTimer);
    }

    musicStateRefreshTimer = window.setTimeout(() => {
        musicStateRefreshTimer = null;
        refreshMusicState();
    }, Math.max(0, Number(delayMs) || 0));
}

function emitBackendDirectAction(action, payload = {}) {
    socket.emit('direct_action', {
        source: 'button',
        silent: true,
        ...payload,
        action
    });
}

function dispatchDirectAction(action, payload = {}) {
    const directAction = String(action || '').trim();

    if (!directAction) {
        return;
    }

    const channel = musicIpcChannel(directAction);

    if (channel) {
        const ipcRenderer = electronIpcRenderer();
        const startedAt = performance.now();

        if (ipcRenderer) {
            ipcRenderer.invoke(channel)
                .then((result) => {
                    console.log(`[PERF] button music ipc/direct: ${Math.round(performance.now() - startedAt)} ms`);

                    if (!result?.ok) {
                        emitBackendDirectAction(directAction, payload);
                    }
                })
                .catch(() => {
                    emitBackendDirectAction(directAction, payload);
                });
            return;
        }
    }

    emitBackendDirectAction(directAction, payload);
}

function emitDirectAction(action, source = 'button', extra = {}) {
    dispatchDirectAction(action, {
        source,
        ...extra
    });
}

async function detectWorkshopDisplays() {
    const ipcRenderer = electronIpcRenderer();
    let displays = [];

    if (ipcRenderer) {
        try {
            const detected = await ipcRenderer.invoke('workshop:detect-displays');

            if (Array.isArray(detected)) {
                displays = detected;
            }
        } catch (_) {
            displays = [];
        }
    }

    if (!displays.length && window.screen) {
        displays = [{
            id: 'browser-display',
            bounds: {
                x: 0,
                y: 0,
                width: window.screen.width || window.innerWidth,
                height: window.screen.height || window.innerHeight
            },
            workArea: {
                x: 0,
                y: 0,
                width: window.innerWidth,
                height: window.innerHeight
            },
            scaleFactor: window.devicePixelRatio || 1,
            primary: true
        }];
    }

    return displays;
}

function assignWorkshopRolesFromDisplays(displays) {
    const sorted = [...(Array.isArray(displays) ? displays : [])].sort((left, right) => {
        const leftX = Number(left.bounds?.x ?? 0);
        const rightX = Number(right.bounds?.x ?? 0);

        if (leftX !== rightX) {
            return leftX - rightX;
        }

        return Number(left.bounds?.y ?? 0) - Number(right.bounds?.y ?? 0);
    });
    const roles = {};

    if (!sorted.length) {
        return roles;
    }

    if (sorted.length === 1) {
        roles['workshop-single'] = sorted[0].id;
        return roles;
    }

    if (sorted.length === 2) {
        const primary = sorted.find((display) => display.primary) || sorted[0];
        const other = sorted.find((display) => display.id !== primary.id) || sorted[1];
        roles['workshop-main'] = primary.id;
        roles['workshop-intel'] = other.id;
        return roles;
    }

    roles['workshop-secondary'] = sorted[0].id;
    roles['workshop-main'] = sorted[Math.floor(sorted.length / 2)].id;
    roles['workshop-intel'] = sorted[sorted.length - 1].id;
    return roles;
}

async function reportWorkshopDisplaysOnce() {
    if (workshopDisplaysReported) {
        return;
    }

    workshopDisplaysReported = true;
    const displays = await detectWorkshopDisplays();

    socket.emit('workshop_displays_detected', {
        displays,
        roles: assignWorkshopRolesFromDisplays(displays),
        display_count: displays.length
    });
}

function requestWorkshopAnalytics() {
    socket.emit('workshop_analytics_request');
}

function formatWorkshopTime(value) {
    if (!value) {
        return 'PENDING';
    }

    try {
        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return String(value).toUpperCase();
        }

        return date.toLocaleTimeString([], {
            hour: '2-digit',
            minute: '2-digit'
        }).toUpperCase();
    } catch (_) {
        return String(value).toUpperCase();
    }
}

// ==========================================
// WORKSHOP — MARKDOWN
// ==========================================
// Small, deliberately limited Markdown renderer for Silent Operator replies.
// Everything is HTML-escaped BEFORE any formatting runs, so model output can
// never inject markup; the rules below only ever add tags of our own.
function workshopInlineMarkdown(escapedText) {
    const codeSpans = [];
    let out = String(escapedText ?? '');

    // Inline code is extracted first so emphasis rules cannot reach inside it.
    out = out.replace(/`([^`\n]+)`/g, (_match, code) => {
        codeSpans.push(code);
        return `\u0001CODE${codeSpans.length - 1}\u0001`;
    });

    out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/(^|[^*\w])\*([^*\n]+)\*(?![*\w])/g, '$1<em>$2</em>');
    out = out.replace(/(^|[^_\w])_([^_\n]+)_(?![_\w])/g, '$1<em>$2</em>');
    // Links render as inert text with the target in the tooltip. Nothing in the
    // Workshop should be able to navigate the Electron window out from under us.
    out = out.replace(
        /\[([^\]\n]+)\]\((https?:[^)\s]+)\)/g,
        (_match, label, url) => `<span class="ws-md-link" title="${url}">${label}</span>`
    );

    return out.replace(/\u0001CODE(\d+)\u0001/g, (_match, index) => `<code>${codeSpans[Number(index)]}</code>`);
}

function workshopMarkdownBlocks(escapedSegment) {
    const lines = String(escapedSegment ?? '').split('\n');
    const out = [];
    let paragraph = [];
    let quote = [];
    let listItems = [];
    let listType = '';

    const flushParagraph = () => {
        if (paragraph.length) {
            out.push(`<p>${workshopInlineMarkdown(paragraph.join('<br>'))}</p>`);
            paragraph = [];
        }
    };

    const flushQuote = () => {
        if (quote.length) {
            out.push(`<blockquote class="ws-md-quote">${workshopInlineMarkdown(quote.join('<br>'))}</blockquote>`);
            quote = [];
        }
    };

    const flushList = () => {
        if (listItems.length) {
            const tag = listType === 'ol' ? 'ol' : 'ul';
            const rows = listItems.map((item) => `<li>${workshopInlineMarkdown(item)}</li>`).join('');
            out.push(`<${tag} class="ws-md-list">${rows}</${tag}>`);
            listItems = [];
            listType = '';
        }
    };

    const flushAll = () => {
        flushParagraph();
        flushQuote();
        flushList();
    };

    lines.forEach((line) => {
        const trimmed = line.trim();

        if (!trimmed) {
            flushAll();
            return;
        }

        const heading = trimmed.match(/^(#{1,4})\s+(.*)$/);

        if (heading) {
            flushAll();
            const level = Math.min(heading[1].length + 2, 6);
            out.push(`<h${level} class="ws-md-heading">${workshopInlineMarkdown(heading[2])}</h${level}>`);
            return;
        }

        if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
            flushAll();
            out.push('<hr class="ws-md-rule">');
            return;
        }

        // ">" arrives as "&gt;" because the segment is already escaped.
        const quoted = trimmed.match(/^&gt;\s?(.*)$/);

        if (quoted) {
            flushParagraph();
            flushList();
            quote.push(quoted[1]);
            return;
        }

        const ordered = trimmed.match(/^(\d{1,3})[.)]\s+(.*)$/);
        const unordered = trimmed.match(/^[-*+]\s+(.*)$/);

        if (ordered || unordered) {
            flushParagraph();
            flushQuote();
            const nextType = ordered ? 'ol' : 'ul';

            if (listType && listType !== nextType) {
                flushList();
            }

            listType = nextType;
            listItems.push(ordered ? ordered[2] : unordered[1]);
            return;
        }

        flushList();
        flushQuote();
        paragraph.push(trimmed);
    });

    flushAll();
    return out.join('');
}

function renderWorkshopMarkdown(value) {
    const raw = String(value ?? '');

    if (!raw.trim()) {
        return '';
    }

    // Split on fences first: odd-indexed segments are literal code.
    return escapeHtml(raw).split('```').map((segment, index) => {
        if (index % 2 === 0) {
            return workshopMarkdownBlocks(segment);
        }

        const lines = segment.replace(/^\r?\n/, '').split('\n');
        let language = '';

        if (lines.length > 1 && lines[0].trim() && !/\s/.test(lines[0].trim())) {
            language = lines.shift().trim();
        }

        const code = lines.join('\n').replace(/\s+$/, '');
        const label = language ? `<span class="ws-md-code-lang">${language}</span>` : '';
        return `<pre class="ws-md-code">${label}<code>${code}</code></pre>`;
    }).join('');
}

// ==========================================
// WORKSHOP — SILENT OPERATOR THREAD
// ==========================================
function workshopMessageRole(entry) {
    const role = String(entry?.role || '').trim().toLowerCase();

    if (role === 'friday' || role === 'jarvis' || role === 'assistant' || role === 'system') {
        return 'friday';
    }

    return 'user';
}

function workshopMessageAuthor(entry) {
    if (workshopMessageRole(entry) === 'friday') {
        return 'FRIDAY';
    }

    const raw = String(entry?.role || '').trim();
    return raw && raw.toLowerCase() !== 'user' ? raw : 'You';
}

function workshopMessageHtml(entry) {
    const role = workshopMessageRole(entry);
    const body = renderWorkshopMarkdown(entry?.text || '');

    return `
        <article class="ws-message role-${role}" data-message-id="${escapeHtml(String(entry?.id || ''))}">
            <header class="ws-message-head">
                <span class="ws-message-author">${escapeHtml(workshopMessageAuthor(entry))}</span>
                <time>${escapeHtml(formatWorkshopTime(entry?.timestamp))}</time>
            </header>
            <div class="ws-message-body">${body || '<p class="ws-md-muted">No content.</p>'}</div>
        </article>
    `;
}

function workshopPendingMessageHtml() {
    return `
        <article class="ws-message role-friday ws-message-pending" data-message-pending="1">
            <header class="ws-message-head">
                <span class="ws-message-author">FRIDAY</span>
                <time>Working</time>
            </header>
            <div class="ws-message-body">
                <div class="ws-typing" role="status" aria-label="FRIDAY is responding">
                    <span></span><span></span><span></span>
                </div>
            </div>
        </article>
    `;
}

function renderWorkshopThreadEmpty() {
    return `
        <div class="ws-thread-empty">
            <div class="ws-thread-empty-mark" aria-hidden="true">◆</div>
            <h2>Silent Operator</h2>
            <p>A text channel straight to FRIDAY. Replies land here instead of being spoken.</p>
        </div>
    `;
}

function renderWorkshopChatMessages(history) {
    const items = Array.isArray(history) ? history : [];

    if (!items.length) {
        return renderWorkshopThreadEmpty();
    }

    const rendered = items.slice(-60).map((entry) => workshopMessageHtml(entry)).join('');
    return rendered + (workshopChatPending ? workshopPendingMessageHtml() : '');
}

function workshopThreadElement() {
    return document.querySelector('.ws-thread');
}

function scrollWorkshopThread(force = false) {
    const thread = workshopThreadElement();

    if (!thread) {
        return;
    }

    // Only auto-scroll when the reader is already at the bottom, so scrolling back
    // through a long answer is not yanked away by the next message.
    const distance = thread.scrollHeight - thread.scrollTop - thread.clientHeight;

    if (!force && distance >= 120) {
        return;
    }

    const jump = () => {
        // 'instant' for the forced case: a smooth animation started immediately
        // after innerHTML gets cancelled by the layout that follows it.
        thread.scrollTo({ top: thread.scrollHeight, behavior: force ? 'instant' : 'smooth' });
    };

    jump();

    if (force) {
        // Run again once layout has settled, since the first call can land before
        // the freshly inserted messages have their final height.
        window.requestAnimationFrame(jump);
    }
}

function setWorkshopChatPending(pending) {
    const next = Boolean(pending);

    if (workshopChatPendingTimer) {
        window.clearTimeout(workshopChatPendingTimer);
        workshopChatPendingTimer = null;
    }

    workshopChatPending = next;

    if (next) {
        // A reply that never lands must not leave the indicator spinning forever.
        workshopChatPendingTimer = window.setTimeout(() => {
            setWorkshopChatPending(false);
        }, 120000);
    }

    const thread = workshopThreadElement();

    if (!thread) {
        return;
    }

    const existing = thread.querySelector('[data-message-pending]');

    if (next && !existing) {
        thread.insertAdjacentHTML('beforeend', workshopPendingMessageHtml());
        scrollWorkshopThread(true);
    } else if (!next && existing) {
        existing.remove();
    }
}

function applyWorkshopChatDelta(payload) {
    const message = payload?.message;

    if (!message || typeof message !== 'object') {
        return;
    }

    latestState = latestState || {};
    latestState.workshop_mode = latestState.workshop_mode || { active: workshopModeActive, chat_history: [] };
    const history = Array.isArray(latestState.workshop_mode.chat_history)
        ? latestState.workshop_mode.chat_history
        : [];
    let appended = false;

    if (!history.some((entry) => entry.id === message.id)) {
        history.push(message);
        latestState.workshop_mode.chat_history = history.slice(-80);
        appended = true;
    }

    if (workshopMessageRole(message) === 'friday') {
        setWorkshopChatPending(false);
    }

    if (!isWorkshopWindow) {
        return;
    }

    const thread = workshopThreadElement();

    if (!thread) {
        renderWorkshopMode(latestState.workshop_mode || {});
        return;
    }

    if (!appended) {
        return;
    }

    const messageChat = String(message.chat_id || '');

    if (messageChat && workshopActiveChatId && messageChat !== workshopActiveChatId) {
        // Belongs to another conversation. It is already stored server-side and
        // will be there when that chat is opened.
        return;
    }

    // Append rather than re-render: a rebuild would drop text selection and
    // scroll position mid-answer.
    thread.querySelector('.ws-thread-empty')?.remove();
    const pendingNode = thread.querySelector('[data-message-pending]');
    const html = workshopMessageHtml(message);

    if (pendingNode) {
        pendingNode.insertAdjacentHTML('beforebegin', html);
    } else {
        thread.insertAdjacentHTML('beforeend', html);
    }

    scrollWorkshopThread();
}

// ==========================================
// WORKSHOP — MEMORY PANEL
// ==========================================
// Renders the real Memory v2 store (see Core_Cognition/memory_manager.py), which
// arrives on workshop_mode.memory. Nothing here is applied optimistically: every
// control emits a socket event and the panel only changes once the backend has
// broadcast the new state back, so what is on screen is what is on disk.

// 'learned' shows what FRIDAY worked out on her own; 'pending' shows what she
// has noticed but not accepted yet, which is a different kind of record and gets
// its own card with Save/Dismiss controls.
const WORKSHOP_MEMORY_FILTERS = ['all', 'user', 'project', 'pinned', 'recent', 'learned', 'pending'];
const WORKSHOP_MEMORY_RECENT_DAYS = 7;
const WORKSHOP_LEARNED_SOURCES = ['learned', 'inferred'];

let workshopMemoryFilter = 'all';
let workshopMemoryQuery = '';
// Which cards are open. Kept out of the DOM so an expanded card survives the
// re-render that follows any backend change.
const workshopMemoryExpanded = new Set();

function workshopMemoryPayload(workshopState = {}) {
    const memory = workshopState.memory;

    if (memory && Array.isArray(memory.items)) {
        return memory;
    }

    // Pre-Memory-v2 payload: project notes lived directly on workshop state.
    const legacy = Array.isArray(workshopState.project_memory)
        ? workshopState.project_memory
        : Array.isArray(workshopState.memory_items)
        ? workshopState.memory_items
        : [];

    return {
        items: legacy.map((item) => ({ ...item, scope: 'project', updated_at: item.timestamp })),
        projects: [],
        active_project_id: '',
        stats: {},
        candidates: [],
        pending_count: 0
    };
}

function workshopMemoryCandidates(workshopState = {}) {
    const payload = workshopMemoryPayload(workshopState);
    return Array.isArray(payload.candidates) ? payload.candidates : [];
}

function workshopMemoryList(workshopState = {}) {
    const items = workshopMemoryPayload(workshopState).items || [];

    // Pinned first, then most recently updated.
    return [...items].sort((left, right) => {
        const pin = Number(Boolean(right.pinned)) - Number(Boolean(left.pinned));

        if (pin !== 0) {
            return pin;
        }

        return (Date.parse(right.updated_at || '') || 0) - (Date.parse(left.updated_at || '') || 0);
    });
}

function filterWorkshopMemories(items) {
    const query = workshopMemoryQuery.trim().toLowerCase();
    const recentCutoff = Date.now() - WORKSHOP_MEMORY_RECENT_DAYS * 86400000;

    return (Array.isArray(items) ? items : []).filter((item) => {
        if (workshopMemoryFilter === 'user' && item.scope !== 'user') return false;
        if (workshopMemoryFilter === 'project' && item.scope !== 'project') return false;
        if (workshopMemoryFilter === 'pinned' && !item.pinned) return false;

        if (workshopMemoryFilter === 'recent') {
            const updated = Date.parse(item.updated_at || item.created_at || '') || 0;
            if (updated < recentCutoff) return false;
        }

        if (workshopMemoryFilter === 'learned' && !WORKSHOP_LEARNED_SOURCES.includes(String(item.source || ''))) {
            return false;
        }

        if (!query) return true;

        const haystack = [item.text, item.category, item.project_name, item.source]
            .filter(Boolean)
            .join(' ')
            .toLowerCase();

        return haystack.includes(query);
    });
}

function formatMemoryDate(value) {
    if (!value) return '';

    const date = new Date(value);

    if (Number.isNaN(date.getTime())) return '';

    const now = new Date();

    if (date.toDateString() === now.toDateString()) {
        return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }

    return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function renderMemoryMetaRow(label, value) {
    if (value === undefined || value === null || value === '') return '';

    return `
        <div class="ws-memory-meta-row">
            <dt>${escapeHtml(label)}</dt>
            <dd>${escapeHtml(String(value))}</dd>
        </div>
    `;
}

function renderWorkshopMemoryItems(items) {
    const list = Array.isArray(items) ? items : [];

    if (!list.length) {
        const empty = workshopMemoryQuery.trim() || workshopMemoryFilter !== 'all'
            ? { title: 'No matches', hint: 'Try another filter or clear the search.' }
            : { title: 'Nothing remembered yet', hint: 'Ask FRIDAY to remember something, or add it here.' };

        return `
            <div class="ws-empty">
                <strong>${escapeHtml(empty.title)}</strong>
                <span>${escapeHtml(empty.hint)}</span>
            </div>
        `;
    }

    return list.slice(0, 60).map((item) => {
        const rawId = String(item.id || '');
        const id = escapeHtml(rawId);
        const pinned = Boolean(item.pinned);
        const expanded = workshopMemoryExpanded.has(rawId);
        const scope = String(item.scope || 'user');
        const scopeLabel = scope === 'project' ? (item.project_name || 'Project') : 'Personal';

        return `
            <article class="ws-memory-card${pinned ? ' pinned' : ''}${expanded ? ' expanded' : ''}" data-memory-id="${id}">
                <p class="ws-memory-text">${escapeHtml(item.text || '')}</p>
                <div class="ws-memory-tags">
                    <span class="ws-memory-tag scope-${escapeHtml(scope)}">${escapeHtml(scopeLabel)}</span>
                    ${item.category ? `<span class="ws-memory-tag">${escapeHtml(item.category)}</span>` : ''}
                </div>
                <footer class="ws-memory-foot">
                    <time>${escapeHtml(formatMemoryDate(item.updated_at || item.created_at || item.timestamp))}</time>
                    <div class="ws-memory-actions">
                        <button
                            type="button"
                            class="ws-icon-button${pinned ? ' active' : ''}"
                            data-memory-action="pin"
                            data-memory-id="${id}"
                            data-memory-pinned="${pinned ? '1' : '0'}"
                            title="${pinned ? 'Unpin' : 'Pin'}"
                            aria-label="${pinned ? 'Unpin memory' : 'Pin memory'}"
                        >${pinned ? '★' : '☆'}</button>
                        <button
                            type="button"
                            class="ws-icon-button danger"
                            data-memory-action="remove"
                            data-memory-id="${id}"
                            title="Forget"
                            aria-label="Forget memory"
                        >×</button>
                    </div>
                </footer>
                ${expanded ? `
                    <div class="ws-memory-detail">
                        <form class="ws-memory-edit" data-memory-id="${id}">
                            <textarea rows="3" aria-label="Edit memory">${escapeHtml(item.text || '')}</textarea>
                            <button type="submit" class="ws-ghost-button">Save</button>
                        </form>
                        <dl class="ws-memory-meta">
                            ${renderMemoryMetaRow('Importance', item.importance)}
                            ${renderMemoryMetaRow('Source', item.source)}
                            ${renderMemoryMetaRow('Created', formatMemoryDate(item.created_at))}
                            ${renderMemoryMetaRow('Updated', formatMemoryDate(item.updated_at))}
                            ${renderMemoryMetaRow('Used', item.access_count)}
                            ${renderMemoryMetaRow('Last used', formatMemoryDate(item.last_accessed))}
                        </dl>
                    </div>
                ` : ''}
            </article>
        `;
    }).join('');
}

// A pending candidate is an observation, not a fact, so it deliberately looks
// different from a memory card: it shows how sure FRIDAY is and how many times
// she has heard it, and its two controls are decisions rather than edits.
function renderWorkshopCandidateItems(items) {
    const list = Array.isArray(items) ? items : [];

    if (!list.length) {
        return `
            <div class="ws-empty">
                <strong>Nothing pending</strong>
                <span>Things FRIDAY notices but isn't sure about will wait here.</span>
            </div>
        `;
    }

    return list.map((item) => {
        const id = escapeHtml(String(item.id || ''));
        const confidence = Math.max(0, Math.min(1, Number(item.confidence) || 0));
        const percent = Math.round(confidence * 100);
        const occurrences = Number(item.occurrences) || 1;
        const conflicted = Boolean(item.contradicts);

        return `
            <article class="ws-candidate-card${conflicted ? ' conflicted' : ''}" data-candidate-id="${id}">
                <p class="ws-memory-text">${escapeHtml(item.text || '')}</p>
                ${conflicted ? '<p class="ws-candidate-conflict">Conflicts with something already saved</p>' : ''}
                <div class="ws-candidate-meter" role="img" aria-label="Confidence ${percent} percent">
                    <span style="width:${percent}%"></span>
                </div>
                <footer class="ws-candidate-foot">
                    <span class="ws-candidate-stats">${percent}% · heard ${occurrences}×</span>
                    <span class="ws-candidate-actions">
                        <button
                            type="button"
                            class="ws-ghost-button"
                            data-candidate-action="promote"
                            data-candidate-id="${id}"
                        >Save</button>
                        <button
                            type="button"
                            class="ws-ghost-button danger"
                            data-candidate-action="reject"
                            data-candidate-id="${id}"
                        >Dismiss</button>
                    </span>
                </footer>
            </article>
        `;
    }).join('');
}

function renderWorkshopMemoryPanel(workshopState = {}) {
    const payload = workshopMemoryPayload(workshopState);
    const all = workshopMemoryList(workshopState);
    const visible = filterWorkshopMemories(all);
    const candidates = workshopMemoryCandidates(workshopState);
    const projectLabel = (payload.projects || []).find(
        (entry) => entry.id === payload.active_project_id
    );

    const chips = WORKSHOP_MEMORY_FILTERS.map((key) => {
        const count = key === 'pending' && candidates.length ? ` ${candidates.length}` : '';

        return `
            <button
                type="button"
                class="ws-memory-chip${workshopMemoryFilter === key ? ' active' : ''}"
                data-memory-filter="${escapeHtml(key)}"
                aria-pressed="${workshopMemoryFilter === key ? 'true' : 'false'}"
            >${escapeHtml(key.toUpperCase() + count)}</button>
        `;
    }).join('');

    return `
        <form class="ws-memory-form">
            <input
                type="text"
                placeholder="Tell FRIDAY something to remember..."
                autocomplete="off"
                aria-label="New memory"
            >
            <select class="ws-memory-scope" aria-label="Memory scope">
                <option value="user">Personal</option>
                <option value="project" selected>${escapeHtml(projectLabel ? projectLabel.name : 'Project')}</option>
            </select>
            <button type="submit" class="ws-ghost-button">Save</button>
        </form>
        <input
            type="search"
            class="ws-memory-search"
            placeholder="Search memory"
            aria-label="Search memory"
            autocomplete="off"
            value="${escapeHtml(workshopMemoryQuery)}"
        >
        <div class="ws-memory-chips" role="group" aria-label="Filter memory">${chips}</div>
        <div class="ws-memory-list">${
            workshopMemoryFilter === 'pending'
                ? renderWorkshopCandidateItems(candidates)
                : renderWorkshopMemoryItems(visible)
        }</div>
    `;
}


/** The list body for the current filter, memories or candidates. */
function renderWorkshopMemoryListBody(workshopState = {}) {
    if (workshopMemoryFilter === 'pending') {
        return renderWorkshopCandidateItems(workshopMemoryCandidates(workshopState));
    }

    return renderWorkshopMemoryItems(filterWorkshopMemories(workshopMemoryList(workshopState)));
}

// ==========================================
// WORKSHOP — SYSTEM ANALYTICS
// ==========================================
function workshopStatCard(label, value, detail = '', tone = '') {
    const toneClass = tone ? ` tone-${escapeHtml(tone)}` : '';
    const detailHtml = detail ? `<span class="ws-stat-detail">${escapeHtml(detail)}</span>` : '';

    return `
        <div class="ws-stat${toneClass}">
            <span class="ws-stat-label">${escapeHtml(label)}</span>
            <strong class="ws-stat-value">${escapeHtml(value)}</strong>
            ${detailHtml}
        </div>
    `;
}

function workshopMeterCard(label, value, percent, tone = '') {
    const safePercent = Number.isFinite(Number(percent))
        ? Math.max(0, Math.min(100, Number(percent)))
        : null;
    const meter = safePercent === null
        ? ''
        : `<div class="ws-meter"><div class="ws-meter-fill" style="width:${safePercent}%"></div></div>`;

    return `
        <div class="ws-stat${tone ? ` tone-${escapeHtml(tone)}` : ''}">
            <span class="ws-stat-label">${escapeHtml(label)}</span>
            <strong class="ws-stat-value">${escapeHtml(value)}</strong>
            ${meter}
        </div>
    `;
}

function workshopBatteryCard(raw) {
    const text = String(raw ?? '').trim();

    if (!text || text.toLowerCase() === 'unavailable') {
        return workshopStatCard('Battery', 'Unavailable', '', 'muted');
    }

    // system_tools reports "88 percent, charging".
    const match = text.match(/^(\d+)\s*percent,\s*(.+)$/i);

    if (!match) {
        return workshopStatCard('Battery', text, '', '');
    }

    const percent = Number(match[1]);
    const state = match[2].trim();
    const label = state.toLowerCase() === 'charging' ? 'Charging' : 'On battery';
    const tone = state.toLowerCase() === 'charging' ? 'ok' : percent <= 20 ? 'warn' : '';

    return `
        <div class="ws-stat${tone ? ` tone-${tone}` : ''}">
            <span class="ws-stat-label">Battery</span>
            <strong class="ws-stat-value">${percent}%</strong>
            <span class="ws-stat-detail">${escapeHtml(label)}</span>
            <div class="ws-meter"><div class="ws-meter-fill" style="width:${Math.max(0, Math.min(100, percent))}%"></div></div>
        </div>
    `;
}

function workshopVoiceCard(payload) {
    // The Workshop window runs the same live-voice bootstrap as the HUD, so the
    // honest reading is "is the live layer enabled in this process", plus the
    // saved fallback provider the backend reports.
    let liveVoice = true;

    try {
        liveVoice = String((process.env.FRIDAY_LIVE_VOICE ?? process.env.JARVIS_LIVE_VOICE) || '1') !== '0';
    } catch (_) {
        liveVoice = true;
    }

    const provider = String(payload?.voice_provider || '').trim();
    const localName = String(payload?.voice_local_name || '').trim();
    const fallback = provider === 'fish'
        ? 'Fish fallback'
        : provider === 'auto'
        ? 'Auto fallback'
        : localName
        ? `Local fallback · ${localName}`
        : 'Local fallback';

    if (liveVoice) {
        return workshopStatCard('Voice', 'Gemini Live', fallback, 'ok');
    }

    return workshopStatCard('Voice', provider ? fallback : 'Local synthesis', 'Live voice disabled', 'muted');
}

function workshopWeatherCard(payload) {
    const card = payload?.weather_card;

    if (!card || card.temperature === null || card.temperature === undefined) {
        return workshopStatCard('Weather', 'Unavailable', '', 'muted');
    }

    const place = String(card.location || 'Local').split(',')[0].trim();
    return workshopStatCard('Weather', `${card.temperature}°`, `${place} · ${card.condition || 'Unknown'}`);
}

function renderWorkshopAnalytics(payload) {
    if (payload?.error) {
        return `
            <div class="ws-empty warn">
                <strong>Analytics unavailable</strong>
                <span>${escapeHtml(payload.error)}</span>
            </div>
        `;
    }

    if (!payload) {
        return `
            <div class="ws-empty">
                <strong>Reading telemetry</strong>
                <span>Waiting for the first sample from the brain.</span>
            </div>
        `;
    }

    const metrics = payload.metrics || {};
    const calendarConnected = Boolean(payload.calendar_connected);
    const monitors = String(payload.proactive_status || '').trim();
    const cpu = metrics.cpu_percent;
    const memory = metrics.memory_percent;
    const diskFree = metrics.disk_free_gb;
    const diskTotal = metrics.disk_total_gb;
    const diskUsedPercent = Number.isFinite(Number(diskFree)) && Number(diskTotal) > 0
        ? ((Number(diskTotal) - Number(diskFree)) / Number(diskTotal)) * 100
        : null;

    return `
        <div class="ws-stat-grid">
            ${workshopBatteryCard(metrics.battery)}
            ${Number.isFinite(Number(cpu))
                ? workshopMeterCard('CPU', `${cpu}%`, cpu, Number(cpu) >= 85 ? 'warn' : '')
                : workshopStatCard('CPU', 'Unavailable', '', 'muted')}
            ${Number.isFinite(Number(memory))
                ? workshopMeterCard('Memory', `${memory}%`, memory, Number(memory) >= 90 ? 'warn' : '')
                : workshopStatCard('Memory', 'Unavailable', '', 'muted')}
            ${Number.isFinite(Number(diskFree))
                ? workshopMeterCard(
                    'Disk',
                    `${diskFree} GB free`,
                    diskUsedPercent,
                    Number(diskFree) < 15 ? 'warn' : ''
                )
                : workshopStatCard('Disk', 'Unavailable', '', 'muted')}
            ${workshopVoiceCard(payload)}
            ${workshopStatCard(
                'Calendar',
                calendarConnected ? 'Connected' : 'Not connected',
                '',
                calendarConnected ? 'ok' : 'muted'
            )}
            ${workshopWeatherCard(payload)}
        </div>
        ${monitors
            ? `<div class="ws-inspector-note"><span class="ws-stat-label">Monitoring</span><p>${escapeHtml(monitors)}</p></div>`
            : ''}
    `;
}

async function openWorkshopElectronWindows(focusExisting = false) {
    if (workshopElectronOpening) {
        return;
    }

    if (workshopElectronOpen && !focusExisting) {
        return;
    }

    const ipcRenderer = electronIpcRenderer();

    if (!ipcRenderer) {
        return;
    }

    workshopElectronOpening = true;

    try {
        const result = await ipcRenderer.invoke('workshop:open');

        if (result?.ok) {
            workshopElectronOpen = true;
            socket.emit('workshop_displays_detected', {
                displays: result.displays || [],
                roles: result.roles || {},
                display_count: result.displayCount || 0
            });
        } else {
            socket.emit('workshop_unavailable', {
                reason: result?.reason || 'no_display'
            });
        }
    } catch (error) {
        appendSystemLine(`Workshop window creation failed: ${error.message || error}.`, '#ff4f6d');
    } finally {
        workshopElectronOpening = false;
    }
}

async function closeWorkshopElectronWindows() {
    const ipcRenderer = electronIpcRenderer();

    if (!ipcRenderer) {
        workshopElectronOpen = false;
        workshopElectronOpening = false;
        return;
    }

    try {
        await ipcRenderer.invoke('workshop:close');
    } catch (_) {
        // Window may already be closed.
    }

    workshopElectronOpen = false;
    workshopElectronOpening = false;
}

function workshopWorkspaceForRole() {
    if (workshopRole === 'secondary') {
        return 'secondary';
    }

    return 'main';
}

const WORKSHOP_WIDGET_COMMANDS = {
    calendar: 'open_calendar',
    music: 'open_music',
    notes: 'open_notes',
    tasks: 'open_task_widget',
    files: 'open_files',
    weather: 'open_weather',
    settings: 'open_settings',
    notifications: 'open_notifications',
    system_health: 'open_system_health',
    news: 'open_intel',
    map: 'open_map'
};

const WORKSHOP_WIDGET_CARD_ALIASES = {
    calendar: { types: ['calendar_agenda', 'calendar'], ids: ['calendar_agenda', 'calendar_page'] },
    music: { types: ['music'], ids: ['music_controls'] },
    notes: { types: ['sticky_notes', 'notes'], ids: ['sticky_notes'] },
    tasks: { types: ['tasks'], ids: ['tasks_widget'] },
    files: { types: ['virtual_finder', 'files'], ids: ['virtual_finder'] },
    weather: { types: ['weather'], ids: ['weather_current'] },
    settings: { types: ['settings'], ids: ['settings_widget'] },
    notifications: { types: ['notification_center', 'notifications'], ids: ['notification_center'] },
    system_health: { types: ['system_health'], ids: ['system_health'] },
    news: { types: ['news', 'intel'], ids: ['news_briefing'] },
    map: { types: ['map'], ids: ['map_fullscreen'] }
};

const WORKSHOP_FALLBACK_WIDGETS = {
    calendar: { title: 'CALENDAR', body: 'Calendar panel loaded.' },
    music: { title: 'MUSIC', body: 'Music controls loaded.' },
    notes: { title: 'NOTES', body: 'Notes panel loaded.' },
    tasks: { title: 'TASKS', body: 'Tasks panel loaded.' },
    files: { title: 'FILES', body: 'Virtual Finder loaded.' },
    weather: { title: 'WEATHER', body: 'Weather panel loaded.' },
    settings: { title: 'SETTINGS', body: 'Settings panel loaded.' }
};

let workshopWidgetZIndex = 60;

function normalizeWorkshopWidgetType(value) {
    const normalized = String(value || '')
        .trim()
        .toLowerCase()
        .replace(/-/g, '_')
        .replace(/\s+/g, '_');

    const aliases = {
        open_calendar: 'calendar',
        calendar: 'calendar',
        open_music: 'music',
        music: 'music',
        open_notes: 'notes',
        sticky_notes: 'notes',
        notes: 'notes',
        open_tasks: 'tasks',
        open_task_widget: 'tasks',
        tasks: 'tasks',
        reminders: 'tasks',
        open_files: 'files',
        open_file_manager: 'files',
        open_virtual_finder: 'files',
        virtual_finder: 'files',
        files: 'files',
        open_weather: 'weather',
        weather: 'weather',
        open_settings: 'settings',
        settings: 'settings',
        open_notifications: 'notifications',
        notification_center: 'notifications',
        notifications: 'notifications',
        open_system_health: 'system_health',
        system_health: 'system_health',
        diagnostics: 'system_health',
        open_intel: 'news',
        open_news: 'news',
        intel: 'news',
        news: 'news',
        open_map: 'map',
        tactical_map: 'map',
        map: 'map'
    };

    return aliases[normalized] || normalized;
}

function workshopFallbackConfig(widget) {
    const aliases = WORKSHOP_WIDGET_CARD_ALIASES[widget] || { types: [widget], ids: [widget] };
    const fallback = WORKSHOP_FALLBACK_WIDGETS[widget] || {
        title: String(widget || 'widget').replace(/_/g, ' ').toUpperCase(),
        body: 'Widget panel loaded.'
    };

    return {
        id: aliases.ids?.[0] || `workshop_${widget}`,
        type: aliases.types?.[0] || widget,
        title: fallback.title,
        body: fallback.body
    };
}

function ensureWorkshopWidgetLayer(root = null) {
    const scopedRoot = root && root.nodeType === 1 ? root : null;
    let layer = scopedRoot?.querySelector?.('#workshop-widget-layer')
        || document.getElementById('workshop-widget-layer')
        || scopedRoot?.querySelector?.('.workshop-widget-layer')
        || document.querySelector('.workshop-widget-layer');

    if (!layer) {
        let workshopRoot = null;

        if (scopedRoot?.matches?.('.workshop-workspace')) {
            workshopRoot = scopedRoot;
        } else {
            workshopRoot = scopedRoot?.querySelector?.('.workshop-workspace')
                || document.querySelector('#workshop-root .workshop-workspace')
                || document.querySelector('.workshop-root .workshop-workspace')
                || document.querySelector('#workshop .workshop-workspace')
                || document.querySelector('.workshop-mode .workshop-workspace')
                || document.querySelector('.workshop-window-root .workshop-workspace')
                || document.querySelector('.workshop-window-host .workshop-workspace')
                || document.querySelector('#workshop-root')
                || document.querySelector('.workshop-root')
                || document.querySelector('#workshop')
                || document.querySelector('.workshop-mode')
                || document.querySelector('.workshop-window-root')
                || document.querySelector('.workshop-window-host')
                || (isWorkshopSurfaceActive() ? ensureWorkshopWindowRoot() : null)
                || document.body;
        }

        layer = document.createElement('div');
        layer.id = 'workshop-widget-layer';
        layer.className = 'workshop-widget-layer';
        const layerParent = workshopRoot.matches?.('.workspace-scroll-canvas')
            ? workshopRoot
            : workshopRoot.querySelector?.('.workshop-workspace > .workspace-scroll-canvas') || workshopRoot;
        layerParent.appendChild(layer);
    }

    layer.id = 'workshop-widget-layer';
    layer.classList.add('workshop-widget-layer');
    layer.style.display = 'block';
    layer.style.visibility = 'visible';
    layer.style.opacity = '1';
    layer.style.pointerEvents = 'none';
    layer.style.zIndex = '50';
    return layer;
}

function isWorkshopSurfaceActive() {
    return isWorkshopWindow
        || workshopModeActive
        || Boolean(latestState?.workshop_mode?.active)
        || document.body.classList.contains('workshop-mode-active');
}

function findWorkshopWidgetCard(widgetType, layer = null) {
    const widget = normalizeWorkshopWidgetType(widgetType);
    const aliases = WORKSHOP_WIDGET_CARD_ALIASES[widget] || { types: [widget], ids: [widget] };
    const typeSet = new Set((aliases.types || []).map((item) => String(item || '').toLowerCase()));
    const idSet = new Set((aliases.ids || []).map((item) => String(item || '').toLowerCase()));
    const container = layer || getWorkshopWidgetContainer();
    const cards = container ? Array.from(container.querySelectorAll('.hud-card')) : [];

    return cards.find((element) => (
        typeSet.has(String(element.dataset.cardType || '').toLowerCase())
        || idSet.has(String(element.dataset.cardId || '').toLowerCase())
    ));
}

function nextWorkshopWidgetZIndex() {
    workshopWidgetZIndex += 1;
    return workshopWidgetZIndex;
}

function forceVisibleWorkshopCard(card, index = 0) {
    if (!card) {
        return;
    }

    const offset = Math.max(0, Math.min(6, Number(index) || 0));

    card.classList.add('workshop-card', 'workshop-widget-card');
    card.classList.remove('workstation-card');
    card.classList.remove('widget-closing');
    card.dataset.workspace = 'workshop';
    card.dataset.workshopWorkspace = card.dataset.workshopWorkspace || workshopWorkspaceForRole();
    if (card.classList.contains('large-workspace-widget')) {
        card.style.setProperty('display', 'flex', 'important');
    } else {
        card.style.display = 'block';
    }
    card.style.visibility = 'visible';
    card.style.opacity = '1';
    card.style.position = 'absolute';

    if (!card.style.left) {
        card.style.left = `${80 + offset * 34}px`;
    }

    if (!card.style.top) {
        card.style.top = `${90 + offset * 30}px`;
    }

    if (!card.style.width) {
        card.style.width = '420px';
    }

    card.style.minHeight = card.style.minHeight || '260px';
    card.style.zIndex = String(nextWindowZ(card));
}

function focusWorkshopWidget(widgetType, layer = null) {
    const card = findWorkshopWidgetCard(widgetType, layer);

    if (!card) {
        return false;
    }

    forceVisibleWorkshopCard(card);
    return true;
}

function createWorkshopFallbackCard(widgetType, payload = {}) {
    const widget = normalizeWorkshopWidgetType(widgetType);
    const config = workshopFallbackConfig(widget);
    const card = document.createElement('div');
    const workspace = String(payload.workspace || workshopWorkspaceForRole() || 'main').toLowerCase();

    card.className = `hud-card native-widget workshop-card workshop-widget-card workshop-fallback-card widget-type-${config.type}`;
    card.dataset.cardId = config.id;
    card.dataset.cardType = config.type;
    card.dataset.workspace = 'workshop';
    card.dataset.workshopWidget = widget;
    card.dataset.workshopWorkspace = workspace;
    card.dataset.workshopFallback = 'true';
    card.innerHTML = `
        <div class="hud-card-header">
            <span class="hud-card-title">${escapeHtml(config.title)}</span>
            <div class="hud-card-controls">
                <span class="hud-card-id">${escapeHtml(config.id)}</span>
                <button class="hud-card-close" title="Close widget">×</button>
            </div>
        </div>

        <div class="native-widget-body">
            <section class="workshop-fallback-widget">
                <strong>${escapeHtml(config.body)}</strong>
                <span>${escapeHtml(config.title)} data loading.</span>
            </section>
        </div>

        <div class="hud-card-resize-handle"></div>
    `;

    attachCardDrag(card);
    attachCardResize(card);
    attachCardClose(card);
    attachMusicControls(card);
    attachNewsControls(card);
    cardUrlCache.set(config.id, 'about:blank');
    return card;
}

function showWorkshopUnavailableCard(widgetType) {
    const layer = ensureWorkshopWidgetLayer();

    if (!layer) {
        return;
    }

    const widget = normalizeWorkshopWidgetType(widgetType);
    const id = `workshop_unavailable_${widget}`;
    let card = layer.querySelector(`.hud-card:not([data-closing])[data-card-id="${CSS.escape(id)}"]`);

    if (!card) {
        card = createCardElement({
            id,
            type: 'summary',
            title: 'WIDGET UNAVAILABLE',
            data: {
                headline: 'Widget unavailable.',
                summary: 'This Workshop widget is not implemented yet.',
                items: []
            }
        });
        applyWorkshopCardLayout(card, { id, type: 'summary' }, layer.querySelectorAll('.hud-card').length, workshopRole || 'main');
        forceVisibleWorkshopCard(card, layer.querySelectorAll('.hud-card').length);
        layer.appendChild(card);
    }

    forceVisibleWorkshopCard(card);
}

function openWorkshopWidget(widgetType, payload = {}) {
    console.log('[Workshop] open widget', widgetType);

    const widget = normalizeWorkshopWidgetType(widgetType || payload.widget || payload.type);
    const command = WORKSHOP_WIDGET_COMMANDS[widget];
    const fromBackend = payload.fromBackend === true || payload.source === 'backend';
    const targetWorkspace = String(payload?.workspace || 'main').toLowerCase();

    if (!isWorkshopSurfaceActive()) {
        if (command && !fromBackend) {
            emitDirectAction(command, payload.source || 'hotbar');
        }
        return;
    }

    if (!isWorkshopWindow || !workspaceMatchesWindow(targetWorkspace)) {
        if (command && !fromBackend) {
            emitDirectAction(command, payload.source || 'workshop_dock', {
                ...payload,
                widget,
                workspace: targetWorkspace,
                workshop_role: payload.workshop_role || workshopRole || 'hud'
            });
        }
        return;
    }

    if (!widget || !WORKSHOP_WIDGET_CARD_ALIASES[widget]) {
        showWorkshopUnavailableCard(widget || 'unknown');
        return;
    }

    const layer = ensureWorkshopWidgetLayer();
    console.log('[Workshop] layer found', layer);

    const existingCard = findWorkshopWidgetCard(widget, layer);

    if (existingCard) {
        forceVisibleWorkshopCard(existingCard);
        return;
    }

    const card = createWorkshopFallbackCard(widget, payload);
    forceVisibleWorkshopCard(card, layer.querySelectorAll('.hud-card').length);
    layer.appendChild(card);
    console.log('[Workshop] card appended', card);

    if (fromBackend) {
        return;
    }

    if (!command) {
        showWorkshopUnavailableCard(widget);
        return;
    }

    emitDirectAction(command, payload.source || 'workshop_dock', {
        ...payload,
        widget,
        workspace: targetWorkspace,
        workshop_role: payload.workshop_role || workshopRole || 'hud'
    });
}

window.openWorkshopWidget = openWorkshopWidget;

function workspaceMatchesWindow(workspace) {
    const target = String(workspace || 'main').toLowerCase();

    if (!isWorkshopWindow) {
        return true;
    }

    if (workshopRole === 'single') {
        return target === 'main' || target === 'secondary';
    }

    if (workshopRole === 'secondary') {
        return target === 'secondary';
    }

    if (workshopRole === 'main') {
        return target === 'main';
    }

    return false;
}

function workshopCardsForWorkspace(workspace) {
    const cards = Array.isArray(latestState?.active_cards) ? latestState.active_cards : [];

    return cards.filter((card) => {
        const cardWorkspace = cardWorkshopWorkspace(card);

        if (!cardWorkspace) {
            return false;
        }

        if (workspace === 'single') {
            return cardWorkspace === 'main' || cardWorkspace === 'secondary';
        }

        return cardWorkspace === workspace;
    });
}

function applyWorkshopCardLayout(cardElement, card, index, workspace) {
    const storedLayout = safeJsonParse(localStorage.getItem(cardStorageKey(card.id, card)));
    const saved = isLargeWorkspaceWidget(card) && storedLayout?.embeddedWidget !== true
        ? null
        : storedLayout;
    const type = String(card.type || '').toLowerCase();
    const large = isLargeWorkspaceWidget(card)
        || ['calendar_agenda', 'virtual_finder'].includes(type);
    const workspaceViewport = cardElement.closest('.workshop-workspace');
    const layer = cardElement.closest('.workshop-widget-layer');
    const layerWidth = layer?.clientWidth || workspaceViewport?.clientWidth || window.innerWidth;
    const layerHeight = layer?.clientHeight || (workspaceViewport?.clientHeight || window.innerHeight) * WORKSPACE_CANVAS_HEIGHT_MULTIPLIER;
    const viewportHeight = workspaceViewport?.clientHeight || window.innerHeight;

    if (saved) {
        const maxWidth = Math.max(260, layerWidth - 24);
        const maxHeight = Math.max(180, layerHeight - 24);
        const minWidth = large ? Math.min(680, maxWidth) : 260;
        const minHeight = large ? Math.min(480, maxHeight) : 180;
        const width = Math.min(maxWidth, Math.max(minWidth, Number(saved.width) || minWidth));
        const height = Math.min(maxHeight, Math.max(minHeight, Number(saved.height) || minHeight));
        cardElement.style.width = `${width}px`;
        cardElement.style.height = `${height}px`;
        cardElement.classList.add('workshop-card');
        cardElement.dataset.workshopWorkspace = workspace;
        forceVisibleWorkshopCard(cardElement, index);
        const position = clampWorkspaceCardPosition(cardElement, saved.left, saved.top);
        cardElement.style.left = `${position.left}px`;
        cardElement.style.top = `${position.top}px`;

        if (position.clamped || width !== Number(saved.width) || height !== Number(saved.height)) {
            saveCardLayout(cardElement);
        }
        return;
    }

    const availableLargeWidth = Math.max(260, layerWidth - 48);
    const availableLargeHeight = Math.max(260, viewportHeight - 72);
    const largeWidth = Math.min(availableLargeWidth, Math.max(Math.min(640, availableLargeWidth), Math.round(layerWidth * 0.88)));
    const largeHeight = Math.min(availableLargeHeight, Math.max(Math.min(460, availableLargeHeight), Math.round(viewportHeight * 0.82)));
    const existingLargeBottom = layer
        ? Array.from(layer.querySelectorAll('.hud-card.large-workspace-widget')).reduce((bottom, item) => (
            item === cardElement ? bottom : Math.max(bottom, item.offsetTop + item.offsetHeight)
        ), 0)
        : 0;
    const precedingCompactCards = layer
        ? Array.from(layer.querySelectorAll('.hud-card:not(.large-workspace-widget)')).filter((item) => item !== cardElement).length
        : index;
    const left = large
        ? Math.max(24, Math.round((layerWidth - largeWidth) / 2))
        : 24 + (precedingCompactCards % 2) * 380;
    const top = large
        ? existingLargeBottom > 0 ? existingLargeBottom + 56 : 56
        : existingLargeBottom > 0
            ? existingLargeBottom + 48 + Math.floor(precedingCompactCards / 2) * 270
            : 84 + Math.floor(index / 2) * 270;
    const width = large ? `${largeWidth}px` : '350px';
    const height = large ? `${largeHeight}px` : '240px';

    cardElement.style.width = width;
    cardElement.style.height = height;
    cardElement.classList.add('workshop-card');
    cardElement.dataset.workshopWorkspace = workspace;
    forceVisibleWorkshopCard(cardElement, index);
    const position = clampWorkspaceCardPosition(cardElement, left, top);
    cardElement.style.left = `${position.left}px`;
    cardElement.style.top = `${position.top}px`;
}

function renderWorkshopWidgets(container, workspace) {
    const layer = container.querySelector('#workshop-widget-layer')
        || container.querySelector('.workshop-widget-layer')
        || ensureWorkshopWidgetLayer(container);

    if (!layer) {
        return;
    }

    const cards = workshopCardsForWorkspace(workspace);
    const activeIds = new Set(cards.map((card) => String(card.id || '')));
    const stateKey = workspace === 'secondary' ? 'secondary' : 'main';
    const hasDesktopIcons = ['secondary', 'single'].includes(workspace)
        && Array.isArray(virtualDesktopPayload?.items)
        && virtualDesktopPayload.items.length > 0;
    window.workshopWidgetState[stateKey] = cards.map((card) => ({
        id: card.id,
        type: card.type,
        x: card.x,
        y: card.y,
        width: card.width,
        height: card.height,
        payload: card.data || {}
    }));

    layer.querySelectorAll('.workshop-workspace-empty').forEach((empty) => empty.remove());

    layer.querySelectorAll('.hud-card:not([data-closing])').forEach((element) => {
        if (!activeIds.has(element.dataset.cardId || '') && element.dataset.workshopFallback !== 'true') {
            closeElementWithMotion(element);
        }
    });

    if (!cards.length) {
        if (hasDesktopIcons || layer.querySelector('.hud-card[data-workshop-fallback="true"]')) {
            return;
        }

        if (!layer.querySelector('.workshop-workspace-empty')) {
            const empty = document.createElement('div');
            empty.className = 'workshop-workspace-empty';
            empty.innerHTML = `
                <strong>${workspace === 'secondary' ? 'Overflow surface ready' : 'Workspace clear'}</strong>
                <span>Open an app from the dock to place it here.</span>
            `;
            layer.appendChild(empty);
        }
        return;
    }

    cards.forEach((card, index) => {
        let element = layer.querySelector(`.hud-card:not([data-closing])[data-card-id="${CSS.escape(card.id)}"]`);

        if (!element) {
            element = createCardElement(card);
            layer.appendChild(element);
            applyWorkshopCardLayout(element, card, index, workspace);
        } else if (!element.classList.contains('dragging') && !element.classList.contains('resizing')) {
            const wasFallback = element.dataset.workshopFallback === 'true';
            updateCardElement(card, element);
            element.classList.add('workshop-card');
            element.dataset.workshopWorkspace = workspace;
            element.classList.remove('workshop-fallback-card');
            delete element.dataset.workshopFallback;

            if (wasFallback) {
                applyWorkshopCardLayout(element, card, index, workspace);
                return;
            }

            forceVisibleWorkshopCard(element, index);
            const position = clampWorkspaceCardPosition(element, element.offsetLeft, element.offsetTop);
            element.style.left = `${position.left}px`;
            element.style.top = `${position.top}px`;

            if (position.clamped) {
                saveCardLayout(element);
            }
        }
    });

    const largeBottom = Array.from(layer.querySelectorAll('.hud-card.large-workspace-widget')).reduce((bottom, item) => (
        Math.max(bottom, item.offsetTop + item.offsetHeight)
    ), 0);

    if (largeBottom > 0) {
        let compactIndex = 0;

        layer.querySelectorAll('.hud-card:not(.large-workspace-widget)').forEach((element) => {
            const cardId = element.dataset.cardId || '';

            if (!activeIds.has(cardId) || localStorage.getItem(cardStorageKey(cardId))) {
                return;
            }

            const left = 24 + (compactIndex % 2) * 380;
            const top = largeBottom + 48 + Math.floor(compactIndex / 2) * 270;
            const position = clampWorkspaceCardPosition(element, left, top);
            element.style.left = `${position.left}px`;
            element.style.top = `${position.top}px`;
            compactIndex += 1;
        });
    }
}


function createVirtualDesktopFolder(parent = '') {
    const name = window.prompt('Folder name');

    if (!name || !name.trim()) {
        return;
    }

    socket.emit('virtual_finder_create_folder', {
        name: name.trim(),
        parent
    });
}


function renderVirtualDesktopIcons(payload, workspaceElement = null) {
    const workspace = workspaceElement || document.querySelector('.workshop-window-root.workshop-secondary .workshop-workspace');

    if (!workspace) {
        return;
    }

    const canvas = workspace.querySelector('.workspace-scroll-canvas') || workspace;
    let layer = canvas.querySelector('.virtual-desktop-icon-layer');

    if (!layer) {
        layer = document.createElement('div');
        layer.className = 'virtual-desktop-icon-layer';
        canvas.prepend(layer);
    }

    const items = Array.isArray(payload?.items) ? payload.items : [];

    if (items.length) {
        workspace.querySelectorAll('.workshop-workspace-empty').forEach((empty) => empty.remove());
    }

    layer.innerHTML = `
        <button class="virtual-desktop-create" type="button" aria-label="Create virtual folder" title="Create folder">+</button>
        <div class="virtual-desktop-icons">
            ${items.map((item) => `
                <button class="virtual-desktop-icon ${item.type === 'folder' ? 'folder' : 'file'}" type="button" data-type="${escapeHtml(item.type || 'item')}" data-path="${escapeHtml(item.path || '')}">
                    <span>${item.type === 'folder' ? '▰' : '◇'}</span>
                    <strong>${escapeHtml(item.name || 'Untitled')}</strong>
                </button>
            `).join('')}
        </div>
    `;

    layer.querySelector('.virtual-desktop-create')?.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        createVirtualDesktopFolder('');
    });

    layer.oncontextmenu = (event) => {
        if (event.target.closest('.virtual-desktop-icon')) {
            return;
        }

        event.preventDefault();
        createVirtualDesktopFolder('');
    };

    layer.querySelectorAll('.virtual-desktop-icon').forEach((button) => {
        const openItem = () => {
            layer.querySelectorAll('.virtual-desktop-icon').forEach((item) => item.classList.remove('selected'));
            button.classList.add('selected');

            if ((button.dataset.type || '') !== 'folder') {
                return;
            }

            virtualFinderBackStack.push(virtualFinderCurrentPath || '');
            socket.emit('virtual_finder_open_path', {
                path: button.dataset.path || ''
            });
        };

        button.addEventListener('click', openItem);
    });
}


function handleWorkshopAction(command, extra = {}) {
    const text = String(command || '').trim();

    if (!text) {
        return;
    }

    const workshopWidget = normalizeWorkshopWidgetType(text);

    if (WORKSHOP_WIDGET_COMMANDS[workshopWidget]) {
        openWorkshopWidget(workshopWidget, {
            ...extra,
            source: 'workshop_dock',
            workshop_role: workshopRole || 'hud'
        });
        return;
    }

    emitDirectAction(text, 'workshop_dock', {
        ...extra,
        workshop_role: workshopRole || 'hud'
    });
}

function renderWorkshopDock(options = {}) {
    // Windows that carry the app sidebar already expose Save/Exit there, so the
    // dock stays purely a launcher for them.
    const includeSystem = options.includeSystem !== false;
    const open = workshopOpenWidgetTypes();

    const dockButton = (item) => {
        const isOpen = Boolean(item.widget && open.has(item.widget));

        return `
            <button
                class="workshop-dock-icon${isOpen ? ' is-open' : ''}"
                type="button"
                data-workshop-command="${escapeHtml(item.command)}"
                title="${escapeHtml(isOpen ? `${item.label} — open` : item.label)}"
                aria-label="${escapeHtml(item.label)}"
                ${isOpen ? 'aria-pressed="true"' : ''}
            >
                <span class="workshop-dock-glyph" aria-hidden="true">${escapeHtml(item.icon)}</span>
                <span class="workshop-dock-label">${escapeHtml(item.label)}</span>
            </button>
        `;
    };

    return `
        <nav class="workshop-dock" aria-label="Workshop dock">
            <div class="workshop-dock-group" role="group" aria-label="Applications">
                ${workshopDockAppItems().map(dockButton).join('')}
            </div>
            ${includeSystem
                ? `<div class="workshop-dock-divider" aria-hidden="true"></div>
                   <div class="workshop-dock-group" role="group" aria-label="Workshop tools">
                       ${workshopDockSystemItems().map(dockButton).join('')}
                   </div>`
                : ''}
        </nav>
    `;
}

// ==========================================
// WORKSHOP — APPLICATION SHELL
// ==========================================
function workshopSidebarState() {
    if (!workshopSidebarOpen) {
        const stored = safeJsonParse(
            (() => {
                try {
                    return localStorage.getItem('friday.workshop.sidebar');
                } catch (_) {
                    return null;
                }
            })()
        );

        workshopSidebarOpen = {
            chats: stored?.chats !== false,
            memory: stored?.memory !== false,
            tasks: stored?.tasks !== false,
            files: Boolean(stored?.files),
            context: Boolean(stored?.context)
        };
    }

    return workshopSidebarOpen;
}

function persistWorkshopSidebarState() {
    try {
        localStorage.setItem('friday.workshop.sidebar', JSON.stringify(workshopSidebarState()));
    } catch (_) {
        // Layout preference only; a storage failure must never break the shell.
    }
}

function workshopInspectorCollapsedState() {
    if (workshopInspectorCollapsed === null) {
        try {
            workshopInspectorCollapsed = localStorage.getItem('friday.workshop.inspector') === 'collapsed';
        } catch (_) {
            workshopInspectorCollapsed = false;
        }
    }

    return workshopInspectorCollapsed;
}

function persistWorkshopInspectorState() {
    try {
        localStorage.setItem(
            'friday.workshop.inspector',
            workshopInspectorCollapsedState() ? 'collapsed' : 'open'
        );
    } catch (_) {
        // Layout preference only.
    }
}

function workshopSidebarSection(key, title, count, bodyHtml, actionHtml = '') {
    const open = Boolean(workshopSidebarState()[key]);
    const countHtml = count === null || count === undefined
        ? ''
        : `<span class="ws-section-count">${escapeHtml(String(count))}</span>`;

    return `
        <section class="ws-section${open ? ' open' : ''}" data-section="${escapeHtml(key)}">
            <h2 class="ws-section-head">
                <button type="button" class="ws-section-toggle" data-section-toggle="${escapeHtml(key)}" aria-expanded="${open ? 'true' : 'false'}">
                    <span class="ws-section-chevron" aria-hidden="true">›</span>
                    <span class="ws-section-title">${escapeHtml(title)}</span>
                    ${countHtml}
                </button>
                ${actionHtml}
            </h2>
            <div class="ws-section-body motion-collapsible">
                <div class="ws-section-body-inner">${bodyHtml}</div>
            </div>
        </section>
    `;
}

function renderWorkshopTaskSection() {
    const payload = normalizeTasksPayload(tasksPayload || {});
    const counts = payload.counts || {};
    const active = Array.isArray(payload.active) ? payload.active : [];

    if (!active.length) {
        return {
            count: 0,
            html: `
                <div class="ws-empty">
                    <strong>No active tasks</strong>
                    <span>Anything you add lands here and in the Tasks app.</span>
                </div>
            `
        };
    }

    const overdue = Number(counts.overdue || 0);
    const today = Number(counts.today || 0);
    const summary = [
        overdue ? `${overdue} overdue` : '',
        today ? `${today} today` : ''
    ].filter(Boolean).join(' · ');

    const rows = active.slice(0, 8).map((task) => `
        <li class="ws-task-row${task.overdue ? ' overdue' : ''}">
            <span class="ws-task-dot" aria-hidden="true"></span>
            <span class="ws-task-title">${escapeHtml(task.title || task.text || 'Untitled task')}</span>
            ${task.due ? `<time>${escapeHtml(formatTaskDue(task.due))}</time>` : ''}
        </li>
    `).join('');

    return {
        count: active.length,
        html: `
            ${summary ? `<p class="ws-section-summary">${escapeHtml(summary)}</p>` : ''}
            <ul class="ws-task-list">${rows}</ul>
            ${active.length > 8 ? `<p class="ws-section-more">+${active.length - 8} more</p>` : ''}
        `
    };
}

// ══════════════════════════════════════════════════════════════════════════
// WORKSHOP FILES
// ══════════════════════════════════════════════════════════════════════════
// A browser over the SAME Virtual Finder the Workstation widget uses, and over
// nothing else. Every listing, preview and metadata read goes through
// file_tools.py, which owns path validation, the protected root folders, the
// preview whitelist, the size caps and the sensitive-name blocks. There is one
// filesystem here and one set of rules for reaching it; this panel is a second
// VIEW, never a second store.
//
// It calls virtual_finder_list rather than virtual_finder_open_path because
// open_path is the WIDGET's navigation — it calls create_virtual_finder_widget,
// so using it here would drag the Workstation Finder onto the screen and move it
// every time a folder was clicked in the sidebar. Browsing here is a read.
//
// The section used to render virtualDesktopPayload.items as ten inert <li>s:
// the folders were real, and clicking one did nothing because there was nothing
// to click.

const WORKSHOP_FILES_TIMEOUT_MS = 8000;
const WORKSHOP_FILES_MAX_ROWS = 300;
const WORKSHOP_FILE_VIEWER_MAX_CHARS = 200000;

let workshopFilesState = {
    path: '',
    parentPath: '',
    items: [],
    status: 'idle',
    error: '',
    // Where "Back" goes. Distinct from "Up", which is the parent folder: after
    // Projects -> Notes -> (up) -> Projects, Back still returns to Notes.
    backStack: [],
    serial: 0
};

let workshopFileViewer = {
    open: false,
    path: '',
    item: null,
    status: 'idle',
    error: '',
    text: '',
    imageSource: '',
    kind: '',
    serial: 0
};

/**
 * One request, one settlement.
 *
 * A timeout as well as an ack, because a socket that has quietly gone away
 * answers neither, and a panel stuck on "Loading…" with no way back is worse
 * than an error.
 */
function workshopFilesRequest(eventName, payload, handlers = {}) {
    let settled = false;

    const settle = (handler, response) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);

        try {
            handler?.(response);
        } catch (error) {
            console.error('[workshop files] handler failed:', error);
        }
    };

    const timer = window.setTimeout(() => {
        settle(handlers.onError, { ok: false, message: 'Files did not respond. Try again.' });
    }, WORKSHOP_FILES_TIMEOUT_MS);

    try {
        socket.emit(eventName, payload, (response) => {
            const result = response && typeof response === 'object' ? response : { ok: false };
            settle(result.ok === true ? handlers.onSuccess : handlers.onError, result);
        });
    } catch (_) {
        settle(handlers.onError, { ok: false, message: 'No link to FRIDAY right now.' });
    }
}

function workshopFilesMessage(response, fallback) {
    const message = String(response?.message || '').trim();
    return message || fallback;
}

/**
 * Load a folder into the panel.
 *
 * `serial` is what makes fast clicking safe: a reply that is no longer the most
 * recent request is dropped rather than painted, so the panel cannot end up
 * showing one folder's contents under another folder's name.
 */
function loadWorkshopFolder(path, options = {}) {
    const target = String(path || '');
    const previous = workshopFilesState.path;
    const serial = ++workshopFilesState.serial;

    workshopFilesState.status = 'loading';
    workshopFilesState.error = '';
    refreshWorkshopFilesSection();

    workshopFilesRequest('virtual_finder_list', { path: target }, {
        onSuccess: (response) => {
            if (serial !== workshopFilesState.serial) return;

            const data = response.data && typeof response.data === 'object' ? response.data : {};

            if (options.pushHistory && previous !== target) {
                workshopFilesState.backStack.push(previous);
            }

            workshopFilesState.path = String(data.current_path ?? target);
            workshopFilesState.parentPath = String(data.parent_path ?? '');
            workshopFilesState.items = Array.isArray(data.items) ? data.items : [];
            workshopFilesState.status = 'ready';
            workshopFilesState.error = '';
            refreshWorkshopFilesSection();
        },
        onError: (response) => {
            if (serial !== workshopFilesState.serial) return;

            workshopFilesState.status = 'error';
            workshopFilesState.error = workshopFilesMessage(response, 'That folder could not be opened.');
            refreshWorkshopFilesSection();
        }
    });
}

function workshopFilesGoBack() {
    if (!workshopFilesState.backStack.length) return;

    // Popped before the request rather than after, so a failed load does not
    // leave the same entry on the stack to be walked into again.
    const target = workshopFilesState.backStack.pop();
    loadWorkshopFolder(target, { pushHistory: false });
}

function workshopFilesGoUp() {
    if (!workshopFilesState.path) return;

    loadWorkshopFolder(workshopFilesState.parentPath || '', { pushHistory: true });
}

/**
 * Open a file in the read-only viewer.
 *
 * `previewable` is the backend's judgement, not this panel's: file_tools decides
 * from the extension whitelist, the size cap and the sensitive-name rules. A
 * file it will not render gets its metadata instead, plus the one thing that is
 * genuinely useful for a format FRIDAY cannot display — handing it to Finder.
 */
function openWorkshopFile(item) {
    if (!item || item.type === 'folder') return;

    const path = String(item.path || '');
    const serial = ++workshopFileViewer.serial;

    workshopFileViewer = {
        open: true,
        path,
        item,
        status: item.previewable ? 'loading' : 'ready',
        error: '',
        text: '',
        imageSource: '',
        kind: item.previewable ? '' : 'metadata',
        serial
    };
    refreshWorkshopFilesSection();

    if (!item.previewable) return;

    workshopFilesRequest('virtual_finder_preview', { path }, {
        onSuccess: (response) => {
            if (serial !== workshopFileViewer.serial) return;

            const data = response.data && typeof response.data === 'object' ? response.data : {};
            const preview = data.preview && typeof data.preview === 'object' ? data.preview : data;
            const text = preview.text ?? preview.content;
            const imageSource = safeVirtualFinderImageSource(preview);

            workshopFileViewer.imageSource = imageSource || '';
            workshopFileViewer.text = text === undefined || text === null
                ? ''
                : String(text).slice(0, WORKSHOP_FILE_VIEWER_MAX_CHARS);
            workshopFileViewer.kind = imageSource
                ? 'image'
                : (text === undefined || text === null ? 'metadata' : 'text');
            workshopFileViewer.status = 'ready';
            refreshWorkshopFilesSection();
        },
        onError: (response) => {
            if (serial !== workshopFileViewer.serial) return;

            workshopFileViewer.status = 'error';
            workshopFileViewer.kind = 'metadata';
            workshopFileViewer.error = workshopFilesMessage(response, 'That file could not be read.');
            refreshWorkshopFilesSection();
        }
    });
}

function closeWorkshopFileViewer() {
    if (!workshopFileViewer.open) return;

    workshopFileViewer.serial += 1;
    workshopFileViewer.open = false;
    workshopFileViewer.item = null;
    workshopFileViewer.text = '';
    workshopFileViewer.imageSource = '';
    refreshWorkshopFilesSection();
}

/**
 * Hand a file to macOS Finder.
 *
 * The VIRTUAL path is sent and never an absolute one: the main process resolves
 * it against the Virtual Finder root and refuses anything that lands outside.
 */
function revealWorkshopFile(path) {
    const target = String(path || '');
    if (!target) return;

    let ipc = null;

    try {
        ipc = require('electron').ipcRenderer;
    } catch (_) {
        ipc = null;
    }

    if (!ipc) {
        workshopFileViewer.error = 'Finder is only available in the desktop app.';
        refreshWorkshopFilesSection();
        return;
    }

    ipc.invoke('files:reveal', target).then((result) => {
        if (result && result.ok) return;

        workshopFileViewer.error = `Could not open in Finder: ${(result && result.reason) || 'unknown error'}.`;
        refreshWorkshopFilesSection();
    }).catch(() => {
        workshopFileViewer.error = 'Could not open in Finder.';
        refreshWorkshopFilesSection();
    });
}

function workshopFileGlyph(item) {
    if (item.type === 'folder') return '▰';
    if (item.kind === 'image') return '▣';
    if (item.previewable) return '▤';
    return '◇';
}

function renderWorkshopFileViewer() {
    const viewer = workshopFileViewer;
    const item = viewer.item || {};
    const name = String(item.name || 'File');
    const meta = [
        formatVirtualFinderSize(item.size, item.type),
        String(item.extension || '').replace('.', '').toUpperCase()
    ].filter(Boolean).join(' · ');

    let body = '';

    if (viewer.status === 'loading') {
        body = '<p class="ws-file-viewer-note">Reading…</p>';
    } else if (viewer.status === 'error') {
        body = `<p class="ws-file-viewer-note error">${escapeHtml(viewer.error)}</p>`;
    } else if (viewer.kind === 'image' && viewer.imageSource) {
        body = `<img class="ws-file-viewer-image" src="${escapeHtml(viewer.imageSource)}" alt="${escapeHtml(name)}">`;
    } else if (viewer.kind === 'text') {
        body = viewer.text
            ? `<pre class="ws-file-viewer-text">${escapeHtml(viewer.text)}</pre>`
            : '<p class="ws-file-viewer-note">This file is empty.</p>';
    } else {
        body = `
            <p class="ws-file-viewer-note">
                FRIDAY cannot display this format. Open it in Finder to use it.
            </p>
        `;
    }

    return `
        <div class="ws-file-viewer" data-ws-file-viewer>
            <header class="ws-file-viewer-head">
                <div class="ws-file-viewer-title">
                    <strong>${escapeHtml(name)}</strong>
                    ${meta ? `<span>${escapeHtml(meta)}</span>` : ''}
                </div>
                <button type="button" class="ws-icon-button" data-ws-file-close aria-label="Close file">✕</button>
            </header>
            <div class="ws-file-viewer-body">${body}</div>
            <footer class="ws-file-viewer-foot">
                <span class="ws-file-viewer-readonly">Read-only</span>
                <button type="button" class="ws-ghost-button" data-ws-file-reveal="${escapeHtml(viewer.path)}">
                    Open in Finder
                </button>
            </footer>
        </div>
    `;
}

function renderWorkshopFilesSection() {
    const state = workshopFilesState;

    // First paint reuses the desktop payload that is already in hand, so the
    // panel has real contents before any request goes out.
    const items = state.status === 'idle'
        ? (Array.isArray(virtualDesktopPayload?.items) ? virtualDesktopPayload.items : [])
        : state.items;

    const atRoot = !state.path;
    const crumbs = state.path ? state.path.split('/').filter(Boolean) : [];
    const location = atRoot ? 'Virtual Finder' : crumbs.join(' / ');

    const nav = `
        <div class="ws-file-nav">
            <button type="button" class="ws-icon-button" data-ws-files-nav="back"
                    ${state.backStack.length ? '' : 'disabled'} aria-label="Back" title="Back">‹</button>
            <button type="button" class="ws-icon-button" data-ws-files-nav="up"
                    ${atRoot ? 'disabled' : ''} aria-label="Up one folder" title="Up one folder">↑</button>
            <span class="ws-file-crumb" title="${escapeHtml(location)}">${escapeHtml(location)}</span>
            <button type="button" class="ws-icon-button" data-ws-files-nav="refresh"
                    aria-label="Refresh" title="Refresh">⟳</button>
        </div>
    `;

    if (workshopFileViewer.open) {
        return { count: items.length, html: nav + renderWorkshopFileViewer() };
    }

    if (state.status === 'loading') {
        return { count: items.length, html: `${nav}<p class="ws-file-note">Loading…</p>` };
    }

    if (state.status === 'error') {
        return {
            count: items.length,
            html: `
                ${nav}
                <div class="ws-empty warn">
                    <strong>Could not open that folder</strong>
                    <span>${escapeHtml(state.error)}</span>
                </div>
            `
        };
    }

    if (!items.length) {
        return {
            count: 0,
            html: `
                ${nav}
                <div class="ws-empty">
                    <strong>${atRoot ? 'Workspace is empty' : 'This folder is empty'}</strong>
                    <span>${atRoot ? 'Open Files to create folders in virtual storage.' : 'Nothing filed in here yet.'}</span>
                </div>
            `
        };
    }

    const rows = items.slice(0, WORKSHOP_FILES_MAX_ROWS).map((item) => {
        const detail = item.type === 'folder'
            ? `${item.item_count || 0} item${item.item_count === 1 ? '' : 's'}`
            : formatVirtualFinderSize(item.size, item.type);

        return `
            <li>
                <button type="button" class="ws-file-row" data-ws-file="${escapeHtml(item.path || '')}"
                        data-ws-file-type="${escapeHtml(item.type || 'file')}">
                    <span class="ws-file-glyph" aria-hidden="true">${workshopFileGlyph(item)}</span>
                    <span class="ws-file-name">${escapeHtml(item.name || 'Untitled')}</span>
                    <span class="ws-file-detail">${escapeHtml(detail)}</span>
                </button>
            </li>
        `;
    }).join('');

    return {
        count: items.length,
        html: `
            ${nav}
            <ul class="ws-file-list">${rows}</ul>
            ${items.length > WORKSHOP_FILES_MAX_ROWS
                ? `<p class="ws-section-more">+${items.length - WORKSHOP_FILES_MAX_ROWS} more</p>`
                : ''}
        `
    };
}

/**
 * Repaint the Files section alone.
 *
 * Deliberately NOT a whole-sidebar re-render: navigating a folder must not
 * rebuild the chat list, and must not steal focus out of the composer while
 * someone is typing.
 */
function refreshWorkshopFilesSection() {
    const rendered = renderWorkshopFilesSection();

    document.querySelectorAll('.ws-section[data-section="files"]').forEach((section) => {
        // The inner wrapper, not the body itself: the body is the collapsible
        // grid track and replacing its contents would remove the wrapper the
        // collapse animation needs.
        const body = section.querySelector('.ws-section-body-inner');
        const count = section.querySelector('.ws-section-count');

        if (body) body.innerHTML = rendered.html;
        if (count) count.textContent = String(rendered.count);

        // A click that opened a folder should reveal what it opened.
        if (!section.classList.contains('open')) {
            section.classList.add('open');
            section.querySelector('.ws-section-toggle')?.setAttribute('aria-expanded', 'true');
        }
    });
}

/**
 * Route a click inside the Files section. Returns true when it was handled.
 */
function handleWorkshopFilesClick(target) {
    const nav = target.closest('[data-ws-files-nav]');

    if (nav) {
        const action = nav.dataset.wsFilesNav || '';

        if (action === 'back') workshopFilesGoBack();
        else if (action === 'up') workshopFilesGoUp();
        else if (action === 'refresh') loadWorkshopFolder(workshopFilesState.path, { pushHistory: false });

        return true;
    }

    if (target.closest('[data-ws-file-close]')) {
        closeWorkshopFileViewer();
        return true;
    }

    const reveal = target.closest('[data-ws-file-reveal]');

    if (reveal) {
        revealWorkshopFile(reveal.dataset.wsFileReveal || '');
        return true;
    }

    const row = target.closest('[data-ws-file]');

    if (row) {
        const path = row.dataset.wsFile || '';

        if (row.dataset.wsFileType === 'folder') {
            loadWorkshopFolder(path, { pushHistory: true });
        } else {
            const item = (workshopFilesState.status === 'idle'
                ? (virtualDesktopPayload?.items || [])
                : workshopFilesState.items
            ).find((entry) => String(entry.path || '') === path);

            openWorkshopFile(item);
        }

        return true;
    }

    return false;
}

function renderWorkshopContextSection(workshopState = {}) {
    const history = Array.isArray(workshopState.chat_history) ? workshopState.chat_history : [];
    const memory = workshopMemoryList(workshopState);
    const displays = Number(workshopState.display_count || 0);
    const workspace = String(workshopState.active_workspace || 'main');
    const rows = [
        ['Session', `${history.length} message${history.length === 1 ? '' : 's'}`],
        ['Memory', `${memory.length} memor${memory.length === 1 ? 'y' : 'ies'}`],
        ['Workspace', workspace.charAt(0).toUpperCase() + workspace.slice(1)],
        ['Displays', displays ? String(displays) : 'Detecting'],
        ['Window role', (workshopRole || 'single').replace(/^\w/, (char) => char.toUpperCase())]
    ];

    return {
        count: null,
        html: `
            <dl class="ws-context-list">
                ${rows.map(([label, value]) => `
                    <div class="ws-context-row">
                        <dt>${escapeHtml(label)}</dt>
                        <dd>${escapeHtml(value)}</dd>
                    </div>
                `).join('')}
            </dl>
        `
    };
}

function formatWorkshopChatDate(value) {
    if (!value) {
        return '';
    }

    const date = new Date(value);

    if (Number.isNaN(date.getTime())) {
        return '';
    }

    const now = new Date();
    const sameDay = date.toDateString() === now.toDateString();

    if (sameDay) {
        return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }

    const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);

    if (date.toDateString() === yesterday.toDateString()) {
        return 'Yesterday';
    }

    return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function renderWorkshopChatList(workshopState = {}) {
    const sessions = Array.isArray(workshopState.chat_sessions) ? workshopState.chat_sessions : [];
    const activeId = String(workshopState.active_chat_id || '');

    if (!sessions.length) {
        return '<div class="ws-empty"><strong>No conversations</strong><span>Start one with New chat.</span></div>';
    }

    // Most recently touched first.
    const ordered = [...sessions].sort((left, right) => {
        const a = Date.parse(right.updated_at || right.created_at || '') || 0;
        const b = Date.parse(left.updated_at || left.created_at || '') || 0;
        return a - b;
    });

    return `<div class="ws-chat-list">${ordered.map((session) => {
        const id = escapeHtml(String(session.id || ''));
        const selected = String(session.id || '') === activeId;
        const count = Array.isArray(session.messages) ? session.messages.length : 0;

        return `
            <div class="ws-chat-row${selected ? ' selected' : ''}" data-chat-id="${id}">
                <button
                    type="button"
                    class="ws-chat-open"
                    data-chat-select="${id}"
                    ${selected ? 'aria-current="true"' : ''}
                    title="${escapeHtml(String(session.title || 'New chat'))}"
                >
                    <span class="ws-chat-title">${escapeHtml(String(session.title || 'New chat'))}</span>
                    <span class="ws-chat-meta">
                        <time>${escapeHtml(formatWorkshopChatDate(session.updated_at || session.created_at))}</time>
                        ${count ? `<span class="ws-chat-count">${count}</span>` : ''}
                    </span>
                </button>
                <span class="ws-chat-actions">
                    <button type="button" class="ws-icon-button" data-chat-rename="${id}" title="Rename chat" aria-label="Rename chat">✎</button>
                    <button type="button" class="ws-icon-button danger" data-chat-delete="${id}" title="Delete chat" aria-label="Delete chat">×</button>
                </span>
            </div>
        `;
    }).join('')}</div>`;
}

function renderWorkshopSidebar(workshopState = {}) {
    const memory = workshopMemoryList(workshopState);
    const tasks = renderWorkshopTaskSection();
    const files = renderWorkshopFilesSection();
    const context = renderWorkshopContextSection(workshopState);

    const memoryBody = renderWorkshopMemoryPanel(workshopState);

    return `
        <aside class="ws-sidebar" aria-label="Workshop sidebar">
            <header class="ws-sidebar-head">
                <div class="ws-brand">
                    <span class="ws-brand-mark" aria-hidden="true">◆</span>
                    <div class="ws-brand-text">
                        <strong>FRIDAY</strong>
                        <span>Workshop</span>
                    </div>
                </div>
            </header>

            <button type="button" class="ws-new-chat" data-chat-new>
                <span aria-hidden="true">+</span> New chat
            </button>

            <nav class="ws-quick-actions" aria-label="Open application">
                <button type="button" class="ws-quick-action" data-workshop-command="open_files">Files</button>
                <button type="button" class="ws-quick-action" data-workshop-command="open_task_widget">Tasks</button>
                <button type="button" class="ws-quick-action" data-workshop-command="open_calendar">Calendar</button>
                <button type="button" class="ws-quick-action" data-workshop-command="open_settings">Settings</button>
            </nav>

            <div class="ws-sidebar-scroll">
                ${workshopSidebarSection(
                    'chats',
                    'Chats',
                    Array.isArray(workshopState.chat_sessions) ? workshopState.chat_sessions.length : 0,
                    renderWorkshopChatList(workshopState)
                )}
                ${workshopSidebarSection('memory', 'Memory', memory.length, memoryBody)}
                ${workshopSidebarSection('tasks', 'Active Tasks', tasks.count, tasks.html)}
                ${workshopSidebarSection('files', 'Files', files.count, files.html)}
                ${workshopSidebarSection('context', 'Context', context.count, context.html)}
            </div>

            <footer class="ws-sidebar-foot">
                <button type="button" class="ws-ghost-button" data-workshop-command="save_workshop_layout">Save layout</button>
                <button type="button" class="ws-ghost-button danger" data-workshop-command="close_workshop_mode">Exit</button>
            </footer>
        </aside>
    `;
}

function renderWorkshopComposer() {
    return `
        <form class="ws-composer" aria-label="Message FRIDAY">
            <div class="ws-composer-field">
                <textarea
                    class="ws-composer-input"
                    rows="1"
                    placeholder="Ask FRIDAY..."
                    aria-label="Ask FRIDAY"
                    autocomplete="off"
                    spellcheck="true"
                ></textarea>
                <button type="submit" class="ws-send-button" aria-label="Send message">
                    <span aria-hidden="true">↑</span>
                </button>
            </div>
            <p class="ws-composer-hint">
                <span><kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line</span>
                <span class="ws-composer-status" data-composer-status></span>
            </p>
        </form>
    `;
}

function renderWorkshopConversation(workshopState = {}) {
    const chatHistory = Array.isArray(workshopState.chat_history) ? workshopState.chat_history : [];

    return `
        <main class="ws-main" aria-label="Silent Operator conversation">
            <header class="ws-main-head">
                <div class="ws-main-title">
                    <h1>Silent Operator</h1>
                    <span>Text channel · replies are not spoken</span>
                </div>
                <button
                    type="button"
                    class="ws-icon-button ws-inspector-toggle"
                    data-inspector-toggle
                    aria-label="Toggle system panel"
                    title="Toggle system panel"
                >▮</button>
            </header>
            <div class="ws-thread" tabindex="0">${renderWorkshopChatMessages(chatHistory)}</div>
            ${renderWorkshopComposer()}
        </main>
    `;
}

function renderWorkshopInspector(workshopState = {}) {
    const generated = workshopAnalyticsPayload?.generated_at || workshopAnalyticsPayload?.timestamp;
    const widgets = (Array.isArray(workshopState.main_widgets) ? workshopState.main_widgets.length : 0)
        + (Array.isArray(workshopState.secondary_widgets) ? workshopState.secondary_widgets.length : 0);

    return `
        <aside class="ws-inspector${workshopInspectorCollapsedState() ? ' collapsed' : ''}" aria-label="System analytics">
            <header class="ws-inspector-head">
                <span class="ws-inspector-title">System</span>
                <time class="ws-inspector-time">${generated ? escapeHtml(formatWorkshopTime(generated)) : ''}</time>
            </header>
            <div class="ws-inspector-scroll">
                ${renderWorkshopAnalytics(workshopAnalyticsPayload)}
                <div class="ws-inspector-note">
                    <span class="ws-stat-label">Workspace</span>
                    <p>${widgets} open widget${widgets === 1 ? '' : 's'}${
                        workshopState.layout_saved_at
                            ? ` · layout saved ${escapeHtml(formatWorkshopTime(workshopState.layout_saved_at))}`
                            : ''
                    }</p>
                </div>
            </div>
        </aside>
    `;
}

function renderWorkshopIntelPanel(workshopState = {}) {
    return `
        <div class="ws-app${workshopInspectorCollapsedState() ? ' inspector-collapsed' : ''}">
            ${renderWorkshopSidebar(workshopState)}
            ${renderWorkshopConversation(workshopState)}
            ${renderWorkshopInspector(workshopState)}
        </div>
    `;
}

function renderWorkshopWorkspaceSurface(scrollKey) {
    return `
        <div class="workshop-workspace workspace-scroll-viewport" data-workspace-scroll="${escapeHtml(scrollKey)}">
            <div class="workspace-scroll-canvas">
                <div class="virtual-desktop-icon-layer"></div>
                <div id="workshop-widget-layer" class="workshop-widget-layer"></div>
            </div>
        </div>
    `;
}

function renderWorkshopWorkspaceShell(role, title, options = {}) {
    const includeDock = options.includeDock !== false;
    const subtitle = role === 'secondary' ? 'Secondary workspace' : 'Main workstation';
    const detail = role === 'secondary' ? 'Overflow surface' : 'Engineering surface';

    return `
        <section class="workshop-window-root workshop-${escapeHtml(role)}">
            <header class="ws-surface-head">
                <div class="ws-brand">
                    <span class="ws-brand-mark" aria-hidden="true">◆</span>
                    <div class="ws-brand-text">
                        <strong>${escapeHtml(subtitle)}</strong>
                        <span>${escapeHtml(title)}</span>
                    </div>
                </div>
                <span class="ws-surface-detail">${escapeHtml(detail)}</span>
            </header>
            <main class="ws-surface-body">
                ${renderWorkshopWorkspaceSurface(role)}
            </main>
            ${includeDock ? renderWorkshopDock() : ''}
        </section>
    `;
}

function renderWorkshopSingleShell(workshopState = {}) {
    return `
        <section class="workshop-window-root workshop-single">
            <div class="ws-app${workshopInspectorCollapsedState() ? ' inspector-collapsed' : ''}">
                ${renderWorkshopSidebar(workshopState)}
                <main class="ws-main" aria-label="Workshop main area">
                    <header class="ws-main-head">
                        <div class="ws-tabs" role="tablist" aria-label="Workshop view">
                            <button type="button" class="ws-tab active" role="tab" aria-selected="true" data-workshop-view="operator">Silent Operator</button>
                            <button type="button" class="ws-tab" role="tab" aria-selected="false" data-workshop-view="workspace">Workspace</button>
                        </div>
                        <button
                            type="button"
                            class="ws-icon-button ws-inspector-toggle"
                            data-inspector-toggle
                            aria-label="Toggle system panel"
                            title="Toggle system panel"
                        >▮</button>
                    </header>
                    <div class="ws-views">
                        <div class="ws-view active" data-view="operator">
                            <div class="ws-thread" tabindex="0">${renderWorkshopChatMessages(workshopState.chat_history)}</div>
                            ${renderWorkshopComposer()}
                        </div>
                        <div class="ws-view" data-view="workspace">
                            ${renderWorkshopWorkspaceSurface('main')}
                            ${renderWorkshopDock({ includeSystem: false })}
                        </div>
                    </div>
                </main>
                ${renderWorkshopInspector(workshopState)}
            </div>
        </section>
    `;
}

function ensureWorkshopWindowRoot() {
    let root = document.querySelector('.workshop-window-host');

    if (root) {
        return root;
    }

    root = document.createElement('section');
    root.className = 'workshop-window-host';
    document.body.appendChild(root);
    return root;
}

function autoGrowWorkshopComposer(textarea) {
    if (!textarea) {
        return;
    }

    // Empty field: drop the inline height entirely and let the single-row CSS
    // height stand. Measuring scrollHeight here is unreliable immediately after
    // innerHTML, before flex has resolved the field's width.
    if (!textarea.value) {
        textarea.style.height = '';
        textarea.classList.remove('scrolling');
        return;
    }

    // Grow with the content up to a ceiling, then let the field scroll — so a long
    // prompt stays comfortable without eating the conversation above it.
    textarea.style.height = 'auto';
    const max = 220;
    const next = Math.min(textarea.scrollHeight, max);
    textarea.style.height = `${next}px`;
    textarea.classList.toggle('scrolling', textarea.scrollHeight > max);
}

function submitWorkshopComposer(textarea) {
    const text = String(textarea?.value || '').trim();

    if (!text) {
        return;
    }

    if (showcaseActive && !isShowcaseExitText(text)) {
        textarea.value = '';
        workshopComposerDraft = '';
        autoGrowWorkshopComposer(textarea);
        return;
    }

    socket.emit('workshop_chat_submit', {
        role: 'Jon',
        text,
        chat_id: workshopActiveChatId,
        metadata: { source: 'workshop_operator', window_role: workshopRole }
    });

    textarea.value = '';
    workshopComposerDraft = '';
    autoGrowWorkshopComposer(textarea);
    setWorkshopChatPending(true);
}

/**
 * ONE delegated listener set per Workshop root.
 *
 * The bug this replaces: handlers were attached per-element by attachWorkshopForms
 * and then AGAIN by rebindWorkshopShellControls on every state_update. When a
 * refresh skipped replacing the sidebar — which it does whenever focus is inside
 * it — the existing elements kept their old handlers and gained another set. One
 * Pin click then fired two, three, N times, and an even count silently undid
 * itself. That is why pinning appeared to do nothing.
 *
 * Delegation makes that class of bug impossible: the listeners live on the root,
 * which is never re-created, so re-rendering the panels beneath it can neither
 * duplicate nor orphan them.
 */
function installWorkshopDelegation(root) {
    if (!root || root.dataset.workshopDelegated === 'true') {
        return;
    }

    root.dataset.workshopDelegated = 'true';

    root.addEventListener('click', (event) => {
        const target = event.target;

        // Files first: its rows and navigation live inside a sidebar section, so
        // letting the section toggle see the click would collapse the panel out
        // from under whatever was just opened.
        if (target.closest('.ws-section[data-section="files"]') && handleWorkshopFilesClick(target)) {
            event.preventDefault();
            return;
        }

        const command = target.closest('[data-workshop-command]');

        if (command) {
            handleWorkshopAction(command.dataset.workshopCommand || '');
            return;
        }

        const sectionToggle = target.closest('[data-section-toggle]');

        if (sectionToggle) {
            const key = sectionToggle.dataset.sectionToggle || '';

            if (!WORKSHOP_SIDEBAR_SECTIONS.includes(key)) {
                return;
            }

            const state = workshopSidebarState();
            state[key] = !state[key];
            persistWorkshopSidebarState();
            sectionToggle.closest('.ws-section')?.classList.toggle('open', state[key]);
            sectionToggle.setAttribute('aria-expanded', state[key] ? 'true' : 'false');
            return;
        }

        const memoryFilter = target.closest('[data-memory-filter]');

        if (memoryFilter) {
            event.preventDefault();
            const key = memoryFilter.dataset.memoryFilter || 'all';

            if (WORKSHOP_MEMORY_FILTERS.includes(key)) {
                workshopMemoryFilter = key;
                applyWorkshopMemoryFilter(root);
            }

            return;
        }

        const candidateAction = target.closest('[data-candidate-action]');

        if (candidateAction) {
            event.preventDefault();
            event.stopPropagation();
            const id = candidateAction.dataset.candidateId || '';

            if (!id) {
                return;
            }

            // Same rule as everything else in this panel: emit, then wait for
            // the backend to say what actually happened.
            socket.emit(
                candidateAction.dataset.candidateAction === 'promote'
                    ? 'memory_candidate_promote'
                    : 'memory_candidate_reject',
                { id }
            );
            return;
        }

        const memoryAction = target.closest('[data-memory-action]');

        if (memoryAction) {
            event.preventDefault();
            event.stopPropagation();
            const id = memoryAction.dataset.memoryId || '';

            if (!id) {
                return;
            }

            if (memoryAction.dataset.memoryAction === 'remove') {
                workshopMemoryExpanded.delete(id);
                socket.emit('memory_delete', { id });
                return;
            }

            // No optimistic flip: the card only shows pinned once the backend has
            // broadcast the new state back.
            socket.emit('memory_pin', {
                id,
                pinned: memoryAction.dataset.memoryPinned !== '1'
            });
            return;
        }

        const memoryCard = target.closest('.ws-memory-card');

        if (memoryCard && !target.closest('.ws-memory-detail')) {
            // Expansion is view state, so it is applied locally — but it is kept
            // in a Set rather than only as a class, so the card stays open across
            // the re-render that follows any backend change.
            const id = memoryCard.dataset.memoryId || '';

            if (workshopMemoryExpanded.has(id)) {
                workshopMemoryExpanded.delete(id);
            } else {
                workshopMemoryExpanded.add(id);
            }

            applyWorkshopMemoryFilter(root);
            return;
        }

        if (target.closest('[data-inspector-toggle]')) {
            workshopInspectorCollapsed = !workshopInspectorCollapsedState();
            persistWorkshopInspectorState();
            root.querySelector('.ws-app')?.classList.toggle('inspector-collapsed', workshopInspectorCollapsed);
            root.querySelector('.ws-inspector')?.classList.toggle('collapsed', workshopInspectorCollapsed);
            return;
        }

        if (target.closest('[data-chat-new]')) {
            workshopComposerDraft = '';
            setWorkshopChatPending(false);
            socket.emit('workshop_chat_new', {});
            return;
        }

        const chatRename = target.closest('[data-chat-rename]');

        if (chatRename) {
            event.preventDefault();
            event.stopPropagation();
            const id = chatRename.dataset.chatRename || '';
            const current = chatRename.closest('.ws-chat-row')?.querySelector('.ws-chat-title')?.textContent || '';
            const next = window.prompt('Rename chat', current);

            if (next && next.trim()) {
                socket.emit('workshop_chat_rename', { id, title: next.trim() });
            }
            return;
        }

        const chatDelete = target.closest('[data-chat-delete]');

        if (chatDelete) {
            event.preventDefault();
            event.stopPropagation();
            const id = chatDelete.dataset.chatDelete || '';
            const title = chatDelete.closest('.ws-chat-row')?.querySelector('.ws-chat-title')?.textContent || 'this chat';

            if (window.confirm(`Delete "${title}"? This cannot be undone.`)) {
                socket.emit('workshop_chat_delete', { id });
            }
            return;
        }

        const chatSelect = target.closest('[data-chat-select]');

        if (chatSelect) {
            const id = chatSelect.dataset.chatSelect || '';

            if (!id || id === workshopActiveChatId) {
                return;
            }

            setWorkshopChatPending(false);
            socket.emit('workshop_chat_select', { id });
            return;
        }

        const viewTab = target.closest('[data-workshop-view]');

        if (viewTab) {
            const view = viewTab.dataset.workshopView || 'operator';

            root.querySelectorAll('[data-workshop-view]').forEach((item) => {
                const active = item === viewTab;
                item.classList.toggle('active', active);
                item.setAttribute('aria-selected', active ? 'true' : 'false');
            });

            // Inactive views stay laid out (only faded and click-through) so the
            // widget layer keeps real dimensions for card positioning.
            root.querySelectorAll('.ws-view').forEach((panel) => {
                panel.classList.toggle('active', panel.dataset.view === view);
            });

            if (view === 'operator') {
                scrollWorkshopThread(true);
                root.querySelector('.ws-composer-input')?.focus();
            } else {
                reclampWorkspaceCards();
            }
        }
    });

    root.addEventListener('submit', (event) => {
        const composer = event.target.closest('.ws-composer');

        if (composer) {
            event.preventDefault();
            submitWorkshopComposer(composer.querySelector('.ws-composer-input'));
            return;
        }

        const memoryEditForm = event.target.closest('.ws-memory-edit');

        if (memoryEditForm) {
            event.preventDefault();
            const id = memoryEditForm.dataset.memoryId || '';
            const text = memoryEditForm.querySelector('textarea')?.value.trim() || '';

            if (id && text) {
                // The id is preserved by the backend; only the text and
                // updated_at change.
                socket.emit('memory_edit', { id, text });
            }

            return;
        }

        const memoryForm = event.target.closest('.ws-memory-form');

        if (memoryForm) {
            event.preventDefault();
            const input = memoryForm.querySelector('input');
            const text = input?.value.trim() || '';

            if (!text) {
                return;
            }

            socket.emit('memory_add', {
                text,
                scope: memoryForm.querySelector('.ws-memory-scope')?.value || 'project',
                source: 'workshop_operator'
            });
            input.value = '';
        }
    });

    // input and keydown bubble, so the composer is reachable by delegation too
    // and survives any re-render of the surrounding shell.
    root.addEventListener('input', (event) => {
        const composerInput = event.target.closest('.ws-composer-input');

        if (composerInput) {
            workshopComposerDraft = composerInput.value;
            autoGrowWorkshopComposer(composerInput);
            return;
        }

        const memorySearch = event.target.closest('.ws-memory-search');

        if (memorySearch) {
            // Searching is a local view over the memories already delivered, so
            // it filters instantly without a round trip to the backend.
            workshopMemoryQuery = memorySearch.value || '';
            applyWorkshopMemoryFilter(root);
        }
    });

    root.addEventListener('keydown', (event) => {
        const composerInput = event.target.closest('.ws-composer-input');

        if (!composerInput || event.key !== 'Enter' || event.shiftKey || event.isComposing) {
            return;
        }

        event.preventDefault();
        submitWorkshopComposer(composerInput);
    });
}

/**
 * Restore the composer draft and size after a shell (re)render. Event wiring is
 * owned entirely by installWorkshopDelegation.
 */
function syncWorkshopComposer(root) {
    const composerInput = root.querySelector('.ws-composer-input');

    if (!composerInput) {
        return;
    }

    if (document.activeElement !== composerInput) {
        composerInput.value = workshopComposerDraft;
    }

    autoGrowWorkshopComposer(composerInput);
}

function attachWorkshopForms(root) {
    installWorkshopDelegation(root);
    syncWorkshopComposer(root);
    scrollWorkshopThread(true);
}

function renderWorkshopWindow(workshopState = {}) {
    const root = ensureWorkshopWindowRoot();
    const role = workshopRole || 'single';

    if (!workshopState.active) {
        if (role === 'main' || role === 'single') {
            closeWorkshopElectronWindows();
        } else {
            window.close();
        }
        return;
    }

    if (!workshopAnalyticsTimer) {
        requestWorkshopAnalytics();
        workshopAnalyticsTimer = window.setInterval(requestWorkshopAnalytics, 45000);
    }

    reportWorkshopDisplaysOnce();

    if (role === 'intel') {
        if (root.dataset.renderedRole !== 'intel') {
            workshopActiveChatId = String(workshopState.active_chat_id || '');
            root.innerHTML = `
                <section class="workshop-window-root workshop-intel">
                    ${renderWorkshopIntelPanel(workshopState)}
                </section>
            `;
            root.dataset.renderedRole = 'intel';
            attachWorkshopForms(root);
        } else {
            refreshWorkshopShell(root, workshopState);
        }
        return;
    }

    if (role === 'secondary') {
        if (root.dataset.renderedRole !== 'secondary') {
            root.innerHTML = renderWorkshopWorkspaceShell('secondary', 'Workshop mode', { includeDock: false });
            root.dataset.renderedRole = 'secondary';
            attachWorkshopForms(root);
        }
        const workspace = root.querySelector('.workshop-workspace');
        initializeWorkspaceScroll(workspace, 'secondary');
        renderVirtualDesktopIcons(virtualDesktopPayload, workspace);
        renderWorkshopWidgets(root, 'secondary');
        return;
    }

    if (role === 'single') {
        if (root.dataset.renderedRole !== 'single') {
            workshopActiveChatId = String(workshopState.active_chat_id || '');
            root.innerHTML = renderWorkshopSingleShell(workshopState);
            root.dataset.renderedRole = 'single';
            attachWorkshopForms(root);
        } else {
            refreshWorkshopShell(root, workshopState);
        }
        const workspace = root.querySelector('.workshop-workspace');
        initializeWorkspaceScroll(workspace, 'main');
        renderVirtualDesktopIcons(virtualDesktopPayload, workspace);
        renderWorkshopWidgets(root, 'single');
        return;
    }

    if (root.dataset.renderedRole !== 'main') {
        root.innerHTML = renderWorkshopWorkspaceShell('main', 'Workshop mode');
        root.dataset.renderedRole = 'main';
        attachWorkshopForms(root);
    }
    initializeWorkspaceScroll(root.querySelector('.workshop-workspace'), 'main');
    renderWorkshopWidgets(root, 'main');
}

/**
 * Refresh the parts of the Workshop shell that track backend state, leaving the
 * conversation alone.
 *
 * The thread is owned by applyWorkshopChatDelta — rebuilding it on every
 * state_update would throw away scroll position and text selection mid-answer.
 * Panels the user is actively typing into are skipped for the same reason.
 */
/**
 * Re-render the memory list and its filter chips from the state already held.
 *
 * Used for view-only changes — a filter chip, the search box, expanding a card —
 * which must never wait on the backend. Mutations go the other way round: emit,
 * then re-render from what the backend broadcasts back.
 */
function applyWorkshopMemoryFilter(root) {
    const host = root || document.querySelector('.workshop-window-host') || document;
    const workshopState = (latestState && latestState.workshop_mode) || {};

    host.querySelectorAll('[data-memory-filter]').forEach((chip) => {
        const active = chip.dataset.memoryFilter === workshopMemoryFilter;
        chip.classList.toggle('active', active);
        chip.setAttribute('aria-pressed', active ? 'true' : 'false');
    });

    const list = host.querySelector('.ws-memory-list');

    if (list) {
        list.innerHTML = renderWorkshopMemoryListBody(workshopState);
    }
}


function refreshWorkshopMemoryList(root, workshopState = {}) {
    const list = root.querySelector('.ws-memory-list');

    if (!list || list.contains(document.activeElement)) {
        return;
    }

    list.innerHTML = renderWorkshopMemoryListBody(workshopState);
}


function refreshWorkshopShell(root, workshopState = {}) {
    const active = document.activeElement;
    const sidebar = root.querySelector('.ws-sidebar');
    const inspector = root.querySelector('.ws-inspector');

    if (!sidebar && !inspector) {
        // Markup predates this shell (or was replaced): rebuild from scratch.
        root.dataset.renderedRole = '';
        renderWorkshopWindow(workshopState);
        return;
    }

    if (sidebar && !sidebar.contains(active)) {
        sidebar.outerHTML = renderWorkshopSidebar(workshopState);
    } else {
        // Focus is inside the sidebar, so it is not safe to replace wholesale —
        // but a pin, an edit or a deletion has to appear as soon as the backend
        // confirms it, and clicking one of those buttons is exactly what puts
        // focus in here. The memory list alone is refreshed instead, and skipped
        // while the user is actually typing inside it.
        refreshWorkshopMemoryList(root, workshopState);
    }

    if (inspector && !inspector.contains(active)) {
        inspector.outerHTML = renderWorkshopInspector(workshopState);
    }

    const thread = root.querySelector('.ws-thread');
    const history = Array.isArray(workshopState.chat_history) ? workshopState.chat_history : [];
    const nextChatId = String(workshopState.active_chat_id || '');
    const chatChanged = nextChatId !== workshopActiveChatId;

    if (chatChanged) {
        // A different conversation is on screen now, so the thread genuinely has
        // to be rebuilt — this is the one case where losing scroll is correct.
        workshopActiveChatId = nextChatId;
        setWorkshopChatPending(false);

        if (thread) {
            thread.innerHTML = renderWorkshopChatMessages(history);
            scrollWorkshopThread(true);
        }
    } else if (thread && history.length && !thread.querySelector('.ws-message')) {
        // Seed a thread that came up before any history existed.
        thread.innerHTML = renderWorkshopChatMessages(history);
        scrollWorkshopThread(true);
    }

    syncWorkshopComposer(root);
}


function renderWorkshopMode(workshopState = {}, focusExisting = false) {
    if (isWorkshopWindow) {
        workshopModeActive = Boolean(workshopState.active);
        document.body.classList.toggle('mode-workshop', workshopModeActive);
        document.body.classList.toggle('workshop-mode-active', workshopModeActive);
        renderWorkshopWindow(workshopState);
        return;
    }

    if (workshopState.active) {
        workshopModeActive = true;
        document.body.classList.add('mode-workshop', 'workshop-mode-active');
        openWorkshopElectronWindows(focusExisting);
    } else {
        const shouldCloseWorkshopWindows = workshopModeActive || workshopElectronOpen || workshopElectronOpening;
        workshopModeActive = false;
        document.body.classList.remove('mode-workshop', 'workshop-mode-active');

        if (shouldCloseWorkshopWindows) {
            closeWorkshopElectronWindows();
        }
    }
}

// ==========================================
// CALENDAR — AGENDA WIDGET
// ==========================================
// One markup tree for every widget size; CSS reveals progressively more of it as
// the card grows (compact = next event, medium = today, expanded = + week).
function calendarDateFromIso(value) {
    const parts = String(value || '').split('-').map(Number);

    if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) {
        return null;
    }

    const date = new Date(parts[0], parts[1] - 1, parts[2]);
    return Number.isNaN(date.getTime()) ? null : date;
}

function calendarIsoFromDate(date) {
    return [
        date.getFullYear(),
        String(date.getMonth() + 1).padStart(2, '0'),
        String(date.getDate()).padStart(2, '0')
    ].join('-');
}

function calendarDayOffset(iso, todayIso) {
    const target = calendarDateFromIso(iso);
    const base = calendarDateFromIso(todayIso);

    if (!target || !base) {
        return null;
    }

    return Math.round((target - base) / 86400000);
}

function formatCalendarRelativeDate(iso, todayIso) {
    const offset = calendarDayOffset(iso, todayIso);

    if (offset === 0) {
        return 'Today';
    }

    if (offset === 1) {
        return 'Tomorrow';
    }

    const date = calendarDateFromIso(iso);

    if (!date) {
        return String(iso || '');
    }

    return date.toLocaleDateString([], {
        weekday: 'short',
        month: 'short',
        day: 'numeric'
    });
}

function calendarWeekPreview(months, todayIso, weekOffset = 0, focusIso = '') {
    const base = calendarDateFromIso(todayIso);

    if (!base) {
        return [];
    }

    const days = [];
    const shift = Number(weekOffset || 0) * 7;

    for (let offset = 0; offset < 7; offset += 1) {
        const date = new Date(base.getFullYear(), base.getMonth(), base.getDate() + shift + offset);
        const iso = calendarIsoFromDate(date);

        days.push({
            iso,
            weekday: date.toLocaleDateString([], { weekday: 'short' }).slice(0, 2),
            day: date.getDate(),
            count: calendarEventsForDate(months, iso).length,
            isToday: iso === todayIso,
            isFocused: iso === focusIso
        });
    }

    return days;
}

/**
 * Label for the week strip, e.g. "Aug 10 – 16" or "Aug 31 – Sep 6".
 */
function calendarWeekRangeLabel(week) {
    if (!week.length) {
        return '';
    }

    const first = calendarDateFromIso(week[0].iso);
    const last = calendarDateFromIso(week[week.length - 1].iso);

    if (!first || !last) {
        return '';
    }

    const firstLabel = first.toLocaleDateString([], { month: 'short', day: 'numeric' });
    const lastLabel = first.getMonth() === last.getMonth()
        ? String(last.getDate())
        : last.toLocaleDateString([], { month: 'short', day: 'numeric' });

    return `${firstLabel} – ${lastLabel}`;
}

/**
 * Real event detail. Every field shown is one Google actually returned; absent
 * fields are omitted rather than filled in.
 */
function calendarEventDetailHtml(event) {
    const rows = [];
    const startIso = String(event.start || '');
    const endIso = String(event.end || '');

    const timeRange = (() => {
        if (event.all_day) {
            return 'All day';
        }

        const start = startIso ? new Date(startIso) : null;
        const end = endIso ? new Date(endIso) : null;
        const fmt = (d) => d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

        if (start && !Number.isNaN(start.getTime())) {
            return end && !Number.isNaN(end.getTime())
                ? `${fmt(start)} – ${fmt(end)}`
                : fmt(start);
        }

        return String(event.time || '').trim();
    })();

    const dateLabel = (() => {
        const date = calendarDateFromIso(String(event.date || ''));
        return date ? date.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' }) : '';
    })();

    if (dateLabel) rows.push(['When', timeRange ? `${dateLabel} · ${timeRange}` : dateLabel]);
    if (event.location) rows.push(['Where', String(event.location)]);
    if (event.organizer) rows.push(['Organiser', String(event.organizer)]);

    const attendees = Array.isArray(event.attendees) ? event.attendees : [];

    if (attendees.length) {
        const names = attendees
            .map((person) => String(person.name || person.email || '').trim())
            .filter(Boolean);

        if (names.length) {
            rows.push(['Attendees', names.join(', ')]);
        }
    }

    if (event.calendar) rows.push(['Calendar', String(event.calendar)]);

    const description = String(event.description || '').trim();

    return `
        <div class="cal-event-detail">
            ${rows.length ? `<dl class="cal-detail-grid">${rows.map(([label, value]) => `
                <div class="cal-detail-row">
                    <dt>${escapeHtml(label)}</dt>
                    <dd>${escapeHtml(value)}</dd>
                </div>
            `).join('')}</dl>` : ''}
            ${description ? `<p class="cal-detail-notes">${escapeHtml(description)}</p>` : ''}
        </div>
    `;
}

function groupCalendarEventsByDate(events) {
    const groups = new Map();

    (Array.isArray(events) ? events : []).forEach((event) => {
        const key = String(event.date || '');

        if (!groups.has(key)) {
            groups.set(key, []);
        }

        groups.get(key).push(event);
    });

    return Array.from(groups.entries());
}

function calendarEventCard(event) {
    const allDay = Boolean(event.all_day);
    const time = allDay ? 'All day' : String(event.time || '').trim() || 'All day';
    const meta = String(event.location || '').trim();

    return `
        <article
            class="cal-event${allDay ? ' all-day' : ''} has-detail"
            tabindex="0"
            role="button"
            aria-expanded="false"
            data-cal-event="${escapeHtml(String(event.id || event.title || ''))}"
        >
            <time class="cal-event-time">${escapeHtml(time)}</time>
            <div class="cal-event-body">
                <strong class="cal-event-title">${escapeHtml(event.title || 'Untitled event')}</strong>
                ${meta ? `<span class="cal-event-meta">${escapeHtml(meta)}</span>` : ''}
                ${calendarEventDetailHtml(event)}
            </div>
        </article>
    `;
}

function calendarEventList(events, emptyMessage) {
    if (!Array.isArray(events) || !events.length) {
        return `<p class="cal-empty">${escapeHtml(emptyMessage)}</p>`;
    }

    return `<div class="cal-events">${events.map(calendarEventCard).join('')}</div>`;
}

function calendarGroupedEventList(events, todayIso, emptyMessage) {
    const groups = groupCalendarEventsByDate(events);

    if (!groups.length) {
        return `<p class="cal-empty">${escapeHtml(emptyMessage)}</p>`;
    }

    return groups.map(([date, items]) => `
        <div class="cal-group">
            <h4 class="cal-group-label">${escapeHtml(formatCalendarRelativeDate(date, todayIso))}</h4>
            <div class="cal-events">${items.map(calendarEventCard).join('')}</div>
        </div>
    `).join('');
}

function calendarNextEvent(todayEvents, upcomingEvents) {
    const today = Array.isArray(todayEvents) ? todayEvents : [];
    const upcoming = Array.isArray(upcomingEvents) ? upcomingEvents : [];
    return today.find((event) => !event.all_day) || today[0] || upcoming[0] || null;
}

function renderCalendarAgendaWidget(data, body) {
    const connected = data && data.connected !== false;
    const today = String(data?.today || new Date().toISOString().slice(0, 10));
    const status = connected ? (data?.status || 'Google Calendar synced') : 'Calendar not connected';

    if (!connected) {
        body.innerHTML = `
            <section class="cal-widget disconnected">
                <div class="cal-disconnected">
                    <strong>Calendar not connected</strong>
                    <p>${escapeHtml(data?.error || status)}</p>
                </div>
            </section>
        `;
        return;
    }

    const months = Array.isArray(data.months) ? data.months : [];
    const todayEvents = Array.isArray(data.today_events) ? data.today_events : [];
    const upcomingEvents = Array.isArray(data.upcoming_events) ? data.upcoming_events : [];
    // "Upcoming" means after today — today has its own block above it.
    const laterEvents = upcomingEvents.filter((event) => {
        const offset = calendarDayOffset(String(event.date || ''), today);
        return offset === null || offset > 0;
    });
    const next = calendarNextEvent(todayEvents, upcomingEvents);

    // Interaction state lives on the widget body so it survives payload refreshes
    // without leaking between multiple Calendar widgets.
    const weekOffset = Number(body._calWeekOffset || 0);
    const focusDate = String(body._calFocusDate || today);
    const week = calendarWeekPreview(months, today, weekOffset, focusDate);
    const weekLabel = calendarWeekRangeLabel(week);
    const focusIsToday = focusDate === today;
    const focusEvents = focusIsToday ? todayEvents : calendarEventsForDate(months, focusDate);
    const focusDateObj = calendarDateFromIso(focusDate);
    const focusHeading = focusIsToday
        ? 'Today'
        : focusDateObj
            ? focusDateObj.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' })
            : focusDate;
    const todayDate = calendarDateFromIso(today);
    const headline = todayDate
        ? todayDate.toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' })
        : today;

    body.innerHTML = `
        <section class="cal-widget">
            <header class="cal-head">
                <div class="cal-head-main">
                    <strong class="cal-head-date">${escapeHtml(headline)}</strong>
                    <span class="cal-head-status">${escapeHtml(status)}</span>
                </div>
                <div class="cal-head-badge">
                    <strong>${todayEvents.length}</strong>
                    <span>today</span>
                </div>
            </header>

            <div class="cal-next${next ? '' : ' empty'}">
                <span class="cal-next-label">Next</span>
                ${next
                    ? `<div class="cal-next-body">
                            <strong>${escapeHtml(next.title || 'Untitled event')}</strong>
                            <span>${escapeHtml([
                                formatCalendarRelativeDate(String(next.date || today), today),
                                next.all_day ? 'All day' : String(next.time || '').trim(),
                                next.location || ''
                            ].filter(Boolean).join(' · '))}</span>
                       </div>`
                    : '<div class="cal-next-body"><strong>Nothing scheduled</strong><span>The next 60 days are clear.</span></div>'}
            </div>

            <div class="cal-scroll">
                <section class="cal-block" data-block="today">
                    <h3 class="cal-block-title">
                        ${escapeHtml(focusHeading)}
                        ${focusIsToday ? '' : '<button type="button" class="cal-clear-focus" data-cal-focus-today>Back to today</button>'}
                    </h3>
                    ${calendarEventList(
                        focusEvents,
                        focusIsToday ? 'Nothing on the calendar today.' : 'Nothing scheduled that day.'
                    )}
                </section>

                <section class="cal-block" data-block="upcoming">
                    <h3 class="cal-block-title">Upcoming</h3>
                    ${calendarGroupedEventList(laterEvents.slice(0, 8), today, 'No upcoming events.')}
                </section>

                <section class="cal-block" data-block="week">
                    <div class="cal-week-head">
                        <h3 class="cal-block-title">${escapeHtml(weekLabel || 'Week')}</h3>
                        <div class="cal-week-nav">
                            <button type="button" class="cal-nav-button" data-cal-week="-1" aria-label="Previous week" title="Previous week">←</button>
                            <button type="button" class="cal-nav-button${weekOffset === 0 && focusIsToday ? ' active' : ''}" data-cal-week="today">Today</button>
                            <button type="button" class="cal-nav-button" data-cal-week="1" aria-label="Next week" title="Next week">→</button>
                        </div>
                    </div>
                    <div class="cal-week">
                        ${week.map((day) => `
                            <button
                                type="button"
                                class="cal-week-day${day.isToday ? ' today' : ''}${day.isFocused ? ' focused' : ''}${day.count ? ' has-events' : ''}"
                                data-cal-date="${escapeHtml(day.iso)}"
                                aria-pressed="${day.isFocused ? 'true' : 'false'}"
                                title="${escapeHtml(`${day.count} ${day.count === 1 ? 'event' : 'events'}`)}"
                            >
                                <span class="cal-week-name">${escapeHtml(day.weekday)}</span>
                                <strong class="cal-week-number">${day.day}</strong>
                                <span class="cal-week-count">${day.count ? day.count : ''}</span>
                            </button>
                        `).join('')}
                    </div>
                </section>
            </div>
        </section>
    `;

    const rerender = () => renderCalendarAgendaWidget(data, body);

    body.querySelectorAll('[data-cal-date]').forEach((button) => {
        button.addEventListener('click', () => {
            body._calFocusDate = button.dataset.calDate || today;
            rerender();
        });
    });

    body.querySelectorAll('[data-cal-week]').forEach((button) => {
        button.addEventListener('click', () => {
            const value = button.dataset.calWeek;

            if (value === 'today') {
                body._calWeekOffset = 0;
                body._calFocusDate = today;
            } else {
                body._calWeekOffset = Number(body._calWeekOffset || 0) + Number(value);
            }

            rerender();
        });
    });

    body.querySelectorAll('[data-cal-focus-today]').forEach((button) => {
        button.addEventListener('click', (event) => {
            event.stopPropagation();
            body._calWeekOffset = 0;
            body._calFocusDate = today;
            rerender();
        });
    });

    body.querySelectorAll('.cal-event.has-detail').forEach((card) => {
        const toggle = () => {
            const open = card.classList.toggle('expanded');
            card.setAttribute('aria-expanded', open ? 'true' : 'false');
        };

        card.addEventListener('click', toggle);
        card.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                toggle();
            }
        });
    });
}

function ensureCalendarPage() {
    let page = document.querySelector('.calendar-page');

    if (page) {
        return page;
    }

    page = document.createElement('section');
    page.className = 'calendar-page';
    page.setAttribute('aria-label', 'FRIDAY calendar page');
    document.body.appendChild(page);
    return page;
}

function openCalendarPage(data) {
    calendarPagePayload = data || {};
    calendarPageSelectedDate = calendarPageSelectedDate || String(calendarPagePayload?.today || new Date().toISOString().slice(0, 10));

    // The agenda widget makes way for the full calendar page, and should be
    // seen to do so rather than blinking out from under it.
    closeAllWithMotion(document, '.hud-card.widget-type-calendar_agenda:not([data-closing])');

    const page = ensureCalendarPage();
    renderCalendarPage();
    page.classList.add('active');
    document.body.classList.add('calendar-page-open');
}

function closeCalendarPage() {
    const page = document.querySelector('.calendar-page');

    if (!page) {
        return;
    }

    page.classList.remove('active');
    document.body.classList.remove('calendar-page-open');
}

function renderCalendarPage() {
    const page = ensureCalendarPage();
    const data = calendarPagePayload || {};
    const connected = data && data.connected !== false;
    const today = String(data?.today || new Date().toISOString().slice(0, 10));
    const selectedDate = calendarPageSelectedDate || today;
    const months = Array.isArray(data?.months) ? data.months : [];
    const todayEvents = Array.isArray(data?.today_events) ? data.today_events : [];
    const tomorrowEvents = Array.isArray(data?.tomorrow_events) ? data.tomorrow_events : [];
    const upcomingEvents = Array.isArray(data?.upcoming_events) ? data.upcoming_events : [];
    const reminders = Array.isArray(data?.priority_reminders) ? data.priority_reminders : [];
    const birthdays = Array.isArray(data?.birthdays) ? data.birthdays : [];
    const selectedEvents = calendarEventsForDate(months, selectedDate);
    const status = connected ? (data.status || 'Google Calendar synced') : 'Calendar not connected';
    const syncTime = new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    const priorityDates = new Set(reminders.map((reminder) => String(reminder?.event?.date || '')).filter(Boolean));
    const birthdayDates = new Set(birthdays.map((birthday) => String(birthday?.iso_date || '')).filter(Boolean));

    page.innerHTML = `
        <div class="calendar-page-gridlines"></div>
        <header class="calendar-page-header">
            <div>
                <span class="calendar-page-kicker">OPERATIONS CALENDAR</span>
                <h1 class="calendar-page-title">CALENDAR</h1>
            </div>
            <div class="calendar-page-header-meta">
                <span>${escapeHtml(data.year || new Date().getFullYear())}</span>
                <strong class="calendar-sync-badge ${connected ? '' : 'offline'}">${escapeHtml(status)}</strong>
                <span>SYNC ${escapeHtml(syncTime)}</span>
            </div>
            <div class="calendar-page-actions">
                <button class="calendar-page-sync" type="button">SYNC</button>
                <button class="calendar-page-close" type="button" aria-label="Close calendar page">CLOSE</button>
            </div>
        </header>

        <section class="calendar-page-status-strip">
            <div><span>TODAY</span><strong>${todayEvents.length}</strong></div>
            <div><span>TOMORROW</span><strong>${tomorrowEvents.length}</strong></div>
            <div class="wide"><span>NEXT EVENT</span><strong>${escapeHtml(upcomingEvents[0]?.title || 'NONE')}</strong></div>
            <div><span>PRIORITY</span><strong>${reminders.length}</strong></div>
        </section>

        <main class="calendar-page-grid">
            <section class="calendar-year-grid-full">
                ${connected
                    ? months.map((month) => renderCalendarMonthFull(month, today, selectedDate, data.year, priorityDates, birthdayDates)).join('')
                    : `<div class="calendar-page-empty">${escapeHtml(status)}.</div>`}
            </section>

            <aside class="calendar-event-side-panel">
                <section class="calendar-side-section primary">
                    <div class="calendar-side-heading">
                        <span>SELECTED DATE</span>
                        <strong>${escapeHtml(formatCalendarDateLabel(selectedDate))}</strong>
                    </div>
                    ${renderCalendarEventDetails(selectedEvents, 'Clear.')}
                </section>

                <section class="calendar-side-section split">
                    <div>
                        <div class="calendar-side-heading compact"><span>TODAY</span></div>
                        ${renderCalendarEventDetails(todayEvents.slice(0, 3), 'Clear.')}
                    </div>
                    <div>
                        <div class="calendar-side-heading compact"><span>TOMORROW</span></div>
                        ${renderCalendarEventDetails(tomorrowEvents.slice(0, 3), 'Clear.')}
                    </div>
                </section>

                <section class="calendar-side-section">
                    <div class="calendar-side-heading compact"><span>UPCOMING</span></div>
                    ${renderCalendarEventDetails(upcomingEvents.slice(0, 10), 'No upcoming events.')}
                </section>

                <section class="calendar-side-section">
                    <div class="calendar-side-heading compact"><span>PRIORITY</span></div>
                    ${renderCalendarPagePriorityRows(reminders)}
                </section>

                <section class="calendar-side-section">
                    <div class="calendar-side-heading compact"><span>BIRTHDAYS</span></div>
                    ${renderCalendarBirthdayRows(birthdays)}
                </section>
            </aside>
        </main>
    `;

    page.querySelector('.calendar-page-close')?.addEventListener('click', closeCalendarPage);
    page.querySelector('.calendar-page-sync')?.addEventListener('click', () => {
        socket.emit('manual_override', { text: 'refresh calendar' });
    });

    page.querySelectorAll('.calendar-day-full[data-date]').forEach((button) => {
        button.addEventListener('click', () => {
            calendarPageSelectedDate = button.dataset.date;
            renderCalendarPage();
        });
    });

    page.querySelectorAll('.calendar-event-detail').forEach((row) => {
        row.addEventListener('click', () => {
            row.classList.toggle('expanded');
        });
    });
}

function renderCalendarMonthFull(month, today, selectedDate, payloadYear, priorityDates, birthdayDates) {
    const monthNumber = Number(month.month || 1);
    const monthEvents = Array.isArray(month.events) ? month.events : [];
    const eventDates = new Set(monthEvents.map((event) => String(event.date || '')));
    const inferredYear = monthEvents[0]?.date ? Number(String(monthEvents[0].date).slice(0, 4)) : null;
    const requestedYear = Number(payloadYear || new Date().getFullYear());
    const safeYear = Number.isFinite(inferredYear) ? inferredYear : requestedYear;
    const dayCount = new Date(safeYear, monthNumber, 0).getDate();
    const firstDay = new Date(safeYear, monthNumber - 1, 1).getDay();
    const cells = [];

    for (let index = 0; index < firstDay; index += 1) {
        cells.push('<span class="calendar-day-full empty"></span>');
    }

    for (let day = 1; day <= dayCount; day += 1) {
        const date = `${safeYear}-${String(monthNumber).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const markers = [
            eventDates.has(date) ? '<span class="calendar-event-marker"></span>' : '',
            priorityDates.has(date) ? '<span class="calendar-priority-marker"></span>' : '',
            birthdayDates.has(date) ? '<span class="calendar-birthday-marker"></span>' : ''
        ].filter(Boolean).join('');
        const classes = [
            'calendar-day-full',
            eventDates.has(date) ? 'has-events' : '',
            priorityDates.has(date) ? 'has-priority' : '',
            birthdayDates.has(date) ? 'has-birthday' : '',
            date === today ? 'today' : '',
            date === selectedDate ? 'selected' : ''
        ].filter(Boolean).join(' ');

        cells.push(`
            <button class="${classes}" type="button" data-date="${escapeHtml(date)}">
                <span>${day}</span>
                <em>${markers}</em>
            </button>
        `);
    }

    return `
        <section class="calendar-month-full">
            <div class="calendar-month-full-title">
                <span>${escapeHtml(month.name || 'Month')}</span>
                <strong>${monthEvents.length}</strong>
            </div>
            <div class="calendar-week-row full">
                <span>S</span><span>M</span><span>T</span><span>W</span><span>T</span><span>F</span><span>S</span>
            </div>
            <div class="calendar-month-days-full">${cells.join('')}</div>
        </section>
    `;
}

function renderCalendarEventDetails(events, emptyText = 'Clear.') {
    if (!Array.isArray(events) || !events.length) {
        return `<div class="calendar-page-empty">${escapeHtml(emptyText)}</div>`;
    }

    return events.map((event) => `
        <button class="calendar-event-detail" type="button">
            <span>${escapeHtml(event.time || 'All day')}</span>
            <strong>${escapeHtml(event.title || 'Untitled event')}</strong>
            ${event.location ? `<em>${escapeHtml(event.location)}</em>` : ''}
            ${event.description ? `<p>${escapeHtml(event.description)}</p>` : ''}
        </button>
    `).join('');
}

function renderCalendarPagePriorityRows(reminders) {
    if (!Array.isArray(reminders) || !reminders.length) {
        return '<div class="calendar-page-empty">No priority items.</div>';
    }

    return reminders.slice(0, 8).map((reminder) => `
        <div class="calendar-priority-chip priority-${escapeHtml(String(reminder.priority || 'low').toLowerCase())}">
            <span>${escapeHtml(String(reminder.type || 'event').toUpperCase())}</span>
            <strong>${escapeHtml(reminder.event?.title || 'Calendar item')}</strong>
            <p>${escapeHtml(reminder.message || '')}</p>
        </div>
    `).join('');
}

function renderCalendarBirthdayRows(birthdays) {
    if (!Array.isArray(birthdays) || !birthdays.length) {
        return '<div class="calendar-page-empty">No birthday records.</div>';
    }

    return birthdays.slice(0, 8).map((birthday) => `
        <div class="calendar-birthday-row ${birthday.family ? 'family' : ''}">
            <span>${escapeHtml(birthday.date || 'Unknown date')}</span>
            <strong>${escapeHtml(birthday.name || birthday.title || 'Birthday')}</strong>
            <em>${escapeHtml(String(birthday.days_until ?? ''))} DAYS</em>
        </div>
    `).join('');
}

function calendarEventsForDate(months, dateString) {
    const events = [];

    months.forEach((month) => {
        const monthEvents = Array.isArray(month.events) ? month.events : [];

        monthEvents.forEach((event) => {
            if (String(event.date || '') === dateString) {
                events.push(event);
            }
        });
    });

    return events;
}

function formatCalendarDateLabel(dateString) {
    if (!dateString) {
        return 'No date';
    }

    try {
        const [year, month, day] = String(dateString).split('-').map(Number);
        const date = new Date(year, month - 1, day);
        return date.toLocaleDateString([], {
            weekday: 'short',
            month: 'short',
            day: 'numeric'
        }).toUpperCase();
    } catch (_) {
        return String(dateString);
    }
}

document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') {
        return;
    }

    closeCalendarPage();

    const mapCardId = mapAppCardId();

    if (mapCardId) {
        closeMapApp(mapCardId);
    }
});


function formatNewsTime(value) {
    if (!value) {
        return 'LIVE';
    }

    try {
        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return 'LIVE';
        }

        return date.toLocaleTimeString([], {
            hour: '2-digit',
            minute: '2-digit'
        });
    } catch (_) {
        return 'LIVE';
    }
}

function buildSparklinePoints(values, width = 180, height = 54) {
    const numbers = Array.isArray(values) ? values.map(Number).filter(Number.isFinite) : [];

    if (numbers.length < 2) {
        return '';
    }

    const min = Math.min(...numbers);
    const max = Math.max(...numbers);
    const range = Math.max(1, max - min);

    return numbers.map((value, index) => {
        const x = (index / (numbers.length - 1)) * width;
        const y = height - ((value - min) / range) * height;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
}

function renderMarketVisualPanel(visual) {
    if (!visual || !Array.isArray(visual.cards)) {
        return '';
    }

    const cardHtml = visual.cards.slice(0, 4).map((card) => {
        const points = buildSparklinePoints(card.trend, 180, 54);
        const direction = String(card.direction || 'flat').toLowerCase();

        return `
            <div class="market-graph-card direction-${escapeHtml(direction)}">
                <div class="market-graph-top">
                    <span>${escapeHtml(card.symbol || 'MARKET')}</span>
                    <strong>${escapeHtml(card.change || '--')}</strong>
                </div>
                <div class="market-graph-label">${escapeHtml(card.label || 'Telemetry')}</div>
                <svg class="market-sparkline" viewBox="0 0 180 54" preserveAspectRatio="none">
                    <polyline points="${escapeHtml(points)}"></polyline>
                </svg>
            </div>
        `;
    }).join('');

    const macroHtml = Array.isArray(visual.macro) ? visual.macro.slice(0, 3).map((item) => {
        const level = Math.max(0, Math.min(100, Number(item.level || 0)));

        return `
            <div class="market-macro-row">
                <div>
                    <span>${escapeHtml(item.label || 'Signal')}</span>
                    <strong>${escapeHtml(item.value || '--')}</strong>
                </div>
                <div class="market-macro-track"><span style="width: ${level}%"></span></div>
            </div>
        `;
    }).join('') : '';

    return `
        <section class="market-visual-panel">
            <div class="visual-panel-header">
                <span>${escapeHtml(visual.title || 'MARKET WATCH')}</span>
                <strong>LIVE SIGNALS</strong>
            </div>
            <div class="market-graph-grid">${cardHtml}</div>
            <div class="market-macro-stack">${macroHtml}</div>
        </section>
    `;
}

function renderConflictVisualPanel(visual) {
    if (!visual || !Array.isArray(visual.hotspots)) {
        return '';
    }

    const hotspotHtml = visual.hotspots.map((spot) => {
        const x = Math.max(0, Math.min(100, Number(spot.x || 50)));
        const y = Math.max(0, Math.min(100, Number(spot.y || 50)));
        const severity = escapeHtml(String(spot.severity || 'watch').toLowerCase());
        const label = escapeHtml(spot.label || 'Hotspot');
        const region = escapeHtml(spot.region || 'Conflict Zone');

        return `
            <button class="conflict-hotspot severity-${severity}" style="left: ${x}%; top: ${y}%;" title="${label}">
                <span></span>
                <em>${label}</em>
                <small>${region}</small>
            </button>
        `;
    }).join('');

    const hotspotListHtml = visual.hotspots.slice(0, 5).map((spot) => {
        return `
            <div class="conflict-hotspot-row severity-${escapeHtml(String(spot.severity || 'watch').toLowerCase())}">
                <span>${escapeHtml(spot.label || 'Hotspot')}</span>
                <strong>${escapeHtml(spot.severity || 'watch')}</strong>
            </div>
        `;
    }).join('');

    const threatBarsHtml = Array.isArray(visual.threat_bars) ? visual.threat_bars.slice(0, 4).map((bar) => {
        const level = Math.max(0, Math.min(100, Number(bar.level || 0)));

        return `
            <div class="conflict-threat-row">
                <span>${escapeHtml(bar.label || 'Threat')}</span>
                <strong style="width: ${level}%"></strong>
            </div>
        `;
    }).join('') : '';

    return `
        <section class="conflict-visual-panel">
            <div class="visual-panel-header">
                <span>${escapeHtml(visual.title || 'CONFLICT WATCH')}</span>
                <strong>${escapeHtml(visual.map_label || 'GLOBAL HOTSPOTS')}</strong>
            </div>
            <div class="conflict-map-shell">
                <div class="conflict-map-grid"></div>
                <div class="conflict-map-glow"></div>
                <svg class="conflict-world-svg" viewBox="0 0 1000 520" preserveAspectRatio="none" aria-hidden="true">
                    <path class="conflict-land" d="M72 178 C122 105 228 92 318 128 C382 154 448 120 520 96 C617 64 752 84 825 140 C895 194 872 276 793 303 C720 328 678 294 608 322 C514 362 460 420 348 384 C250 352 176 399 105 342 C46 295 28 235 72 178 Z" />
                    <path class="conflict-land muted" d="M292 344 C348 316 412 328 454 374 C493 417 456 470 390 474 C319 478 256 425 272 376 C276 362 282 352 292 344 Z" />
                    <path class="conflict-land muted" d="M675 312 C742 288 842 305 914 358 C966 397 938 464 858 466 C771 468 675 418 648 362 C638 340 649 322 675 312 Z" />
                    <path class="conflict-line" d="M520 96 C554 150 568 224 558 310" />
                    <path class="conflict-line" d="M620 126 C662 192 656 256 618 322" />
                    <path class="conflict-line" d="M438 128 C408 194 414 272 454 374" />
                </svg>
                ${hotspotHtml}
            </div>
            <div class="conflict-panel-bottom">
                <div class="conflict-hotspot-list">${hotspotListHtml}</div>
                <div class="conflict-threat-stack">${threatBarsHtml}</div>
            </div>
        </section>
    `;
}

function renderNewsWidget(data, body, activeTab = 'briefing') {
    const tabs = data.tabs || {};
    const tabKeys = Object.keys(tabs);
    const normalizedTab = tabs[activeTab] ? activeTab : (tabs[data.active_tab] ? data.active_tab : (tabKeys[0] || 'briefing'));
    const selectedTab = tabs[normalizedTab] || { label: 'BRIEFING', region: 'Live Feed', items: [] };
    const items = Array.isArray(selectedTab.items) ? selectedTab.items : [];
    const liveFeed = selectedTab.live_feed || null;
    const visual = selectedTab.visual || null;
    const stats = selectedTab.stats || {};
    const headline = escapeHtml(data.headline || 'INTEL BRIEFING');
    const region = escapeHtml(selectedTab.region || selectedTab.label || 'Live Feed');
    const source = escapeHtml(data.source || 'Google News RSS');
    const timestamp = escapeHtml(data.timestamp || '');
    const topSources = Array.isArray(stats.top_sources) ? stats.top_sources : [];

    const tabHtml = tabKeys.map((key) => {
        const tab = tabs[key] || {};
        const label = escapeHtml(tab.label || key.toUpperCase());

        return `<button class="news-tab-button ${normalizedTab === key ? 'active' : ''}" data-news-tab="${escapeHtml(key)}">${label}</button>`;
    }).join('');

    const sourceBarsHtml = topSources.slice(0, 4).map((sourceItem) => {
        const count = Number(sourceItem.count || 0);
        const width = Math.max(18, Math.min(100, count * 24));

        return `
            <div class="news-source-bar">
                <span>${escapeHtml(sourceItem.name || 'Source')}</span>
                <strong style="width: ${width}%"></strong>
            </div>
        `;
    }).join('');

    const liveSourceUrl = liveFeed ? (liveFeed.live_page_url || liveFeed.embed_url || liveFeed.fallback_url || '') : '';

    const liveFeedHtml = liveFeed && liveSourceUrl ? `
        <section class="news-live-panel">
            <div class="news-live-header">
                <span class="news-live-dot"></span>
                <span>${escapeHtml(liveFeed.label || 'LIVE FEED')}</span>
                <strong>${escapeHtml(liveFeed.provider || 'Live News')}</strong>
            </div>

            <div class="news-live-frame-wrap">
                <webview
                    class="news-live-frame news-live-webview"
                    src="${escapeHtml(liveSourceUrl)}"
                    partition="persist:friday-news"
                    allowpopups>
                </webview>

                <div class="news-live-mask">
                    <span>LIVE VIDEO SOURCE</span>
                    <strong>${escapeHtml(liveFeed.provider || 'Live News')}</strong>
                </div>
            </div>

            ${liveFeed.fallback_url ? `<button class="news-live-fallback" data-news-link="${escapeHtml(liveFeed.fallback_url)}">OPEN LIVE SOURCE</button>` : ''}
        </section>
    ` : '';

    const visualHtml = normalizedTab === 'markets'
        ? renderMarketVisualPanel(visual)
        : normalizedTab === 'conflict'
            ? renderConflictVisualPanel(visual)
            : '';

    const itemHtml = items.slice(0, 10).map((item, index) => {
        const title = escapeHtml(item.title || 'Untitled story');
        const itemSource = escapeHtml(item.source || 'News');
        const published = escapeHtml(formatNewsTime(item.published));
        const link = item.link ? escapeHtml(item.link) : '';
        const number = String(index + 1).padStart(2, '0');

        return `
            <article class="news-headline-card" ${link ? `data-news-link="${link}"` : ''}>
                <div class="news-headline-number">${number}</div>
                <div class="news-headline-content">
                    <div class="news-headline-title">${title}</div>
                    <div class="news-headline-meta">
                        <span>${itemSource}</span>
                        <span>${published}</span>
                    </div>
                </div>
            </article>
        `;
    }).join('');

    body.innerHTML = `
        <div class="news-widget intel-briefing-widget news-tab-${escapeHtml(normalizedTab)}" data-active-news-tab="${escapeHtml(normalizedTab)}">
            <div class="news-top-grid">
                <div>
                    <div class="news-kicker">GLOBAL INTEL FEED</div>
                    <div class="news-main-title">${headline}</div>
                    <div class="news-region-label">${region}</div>
                </div>
                <div class="news-radar">
                    <span></span>
                </div>
            </div>

            <div class="news-tab-row intel-tab-row">
                ${tabHtml}
            </div>

            <div class="news-intel-grid ${liveFeedHtml ? 'has-live-feed' : ''}">
                <div class="news-primary-column">
                    ${liveFeedHtml}
                    ${visualHtml}
                    <div class="news-headline-list ${liveFeedHtml ? 'has-live-feed' : ''} ${visualHtml ? 'has-visual-panel' : ''}">
                        ${itemHtml || '<div class="news-empty">No headlines available.</div>'}
                    </div>
                </div>

                <aside class="news-side-panel">
                    <div class="news-stat-card">
                        <span>HEADLINES</span>
                        <strong>${escapeHtml(stats.headline_count ?? items.length)}</strong>
                    </div>
                    <div class="news-stat-card">
                        <span>SOURCES</span>
                        <strong>${escapeHtml(stats.source_count ?? topSources.length)}</strong>
                    </div>
                    <div class="news-source-stack">
                        <div class="news-source-title">SOURCE DENSITY</div>
                        ${sourceBarsHtml || '<div class="news-empty mini">No source telemetry.</div>'}
                    </div>
                </aside>
            </div>

            <div class="news-footer-row">
                <span>SOURCE ${source}</span>
                <span>${timestamp ? `SCAN ${timestamp}` : 'LIVE RSS'}</span>
            </div>
        </div>
    `;
}

function formatClockSeconds(value) {
    const total = Number(value);

    if (!Number.isFinite(total) || total < 0) {
        return '';
    }

    const minutes = Math.floor(total / 60);
    const seconds = Math.floor(total % 60);
    return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

/**
 * Music, in three densities.
 *
 * Everything drawn here comes from Music.app: artwork is the real embedded cover,
 * the progress bar is real player position, and the playlist list is the user's
 * own. Where Music does not report something — no artwork on a track, no duration
 * while stopped — the corresponding element is omitted rather than filled with a
 * placeholder value, because a made-up 0:00 reads as broken rather than empty.
 */
function renderMusicWidget(data, body) {
    const rawState = normalizeMusicPlayerState(data.player_state || data.state || 'stopped');
    const playing = rawState === 'playing';
    const hasTrack = Boolean(data.track || data.title) && (data.track || data.title) !== 'No track loaded';

    const title = escapeHtml(data.track || data.title || 'Nothing playing');
    const artist = escapeHtml(data.artist || '');
    const album = escapeHtml(data.album || '');
    const artwork = typeof data.artwork === 'string' && data.artwork.startsWith('data:') ? data.artwork : '';
    const playlists = Array.isArray(data.playlists) ? data.playlists : [];

    const duration = Number(data.duration);
    const position = Number(data.position);
    const hasProgress = Number.isFinite(duration) && duration > 0 && Number.isFinite(position);
    const percent = hasProgress ? Math.max(0, Math.min(100, (position / duration) * 100)) : 0;

    const shuffleOn = data.shuffle === true;
    const repeatMode = String(data.repeat || 'off').toLowerCase();
    const repeatOn = repeatMode !== 'off';

    const artworkMarkup = artwork
        ? `<img class="music-art-image" src="${escapeHtml(artwork)}" alt="${album || title} artwork">`
        : `<div class="music-art-fallback" aria-hidden="true"><span>♫</span></div>`;

    // Always emitted, hidden when there is nothing real to show.
    //
    // This used to be omitted entirely without a duration, which meant a card
    // created before the first state poll — the normal path, since Python builds
    // the card and the duration arrives over IPC a moment later — had no progress
    // element for paintMusicProgress to reveal. The bar could then never appear
    // for the life of that window.
    const progressMarkup = `
        <div class="music-progress"${hasProgress ? '' : ' hidden'}>
            <div class="music-progress-track"><div class="music-progress-fill" style="width:${hasProgress ? percent.toFixed(2) : 0}%"></div></div>
            <div class="music-progress-times">
                <span>${hasProgress ? formatClockSeconds(position) : ''}</span>
                <span>${hasProgress ? formatClockSeconds(duration) : ''}</span>
            </div>
        </div>`;

    const playlistMarkup = `
        <div class="music-library" data-music-library data-view="home"
             data-playlists="${escapeHtml(JSON.stringify(playlists))}"></div>`;

    body.innerHTML = `
        <div class="music-panel" data-playing="${playing}" data-has-track="${hasTrack}">
            <div class="music-now">
                <div class="music-art ${playing ? 'is-playing' : ''}">${artworkMarkup}</div>

                <div class="music-now-text">
                    <div class="music-title" title="${title}">${title}</div>
                    <div class="music-artist" title="${artist}">${artist || 'Unknown artist'}</div>
                    <div class="music-album" title="${album}">${album}</div>
                </div>

                <div class="music-transport">
                    <button class="music-btn music-btn-ghost" data-music-command="previous"
                            title="Previous" aria-label="Previous">⏮</button>
                    <button class="music-btn music-btn-play" data-music-command="toggle"
                            data-player-state="${rawState}"
                            title="${playing ? 'Pause' : 'Play'}" aria-label="${playing ? 'Pause' : 'Play'}">${playing ? '⏸' : '▶'}</button>
                    <button class="music-btn music-btn-ghost" data-music-command="next"
                            title="Next" aria-label="Next">⏭</button>
                </div>
            </div>

            ${progressMarkup}

            <div class="music-modes">
                <button class="music-chip ${shuffleOn ? 'is-on' : ''}"
                        data-music-mode="shuffle" data-mode-on="${shuffleOn}"
                        title="Shuffle ${shuffleOn ? 'on' : 'off'}">⤮ Shuffle</button>
                <button class="music-chip ${repeatOn ? 'is-on' : ''}"
                        data-music-mode="repeat" data-mode-on="${repeatOn}"
                        title="Repeat ${escapeHtml(repeatMode)}">⟳ Repeat</button>
                <span class="music-source">${escapeHtml(data.source || 'Apple Music')}</span>
            </div>

            ${playlistMarkup}
        </div>
    `;

    // The card wiring runs once per card; a re-render needs the library rebuilt.
    const card = body.closest('.hud-card');

    if (card) {
        attachMusicLibrary(card);
        attachMusicSeek(card);
    }

    // Deferred, not immediate: this runs from createCardElement, which builds the
    // body BEFORE the card is inserted into the document. A synchronous refresh
    // therefore found no .music-panel, bailed out, and — because the poll loop
    // only reschedules itself from a successful pass — never started at all. That
    // is what left the progress bar frozen and the widget blind to Music.app.
    scheduleMusicStateRefresh(60);
}

/** Filters the visible rows in place; purely local, no round trip. */
function attachMusicFilter(body) {
    const input = body.querySelector('[data-music-filter]');

    if (!input) {
        return;
    }

    input.addEventListener('input', () => {
        const term = input.value.trim().toLowerCase();

        body.querySelectorAll('.music-playlist-row, .music-track-row').forEach((row) => {
            const haystack = row.dataset.searchKey || '';
            row.hidden = Boolean(term) && !haystack.includes(term);
        });
    });

    // Typing in the widget must not be swallowed by window-level shortcuts.
    input.addEventListener('keydown', (event) => event.stopPropagation());
}

// Tracks are cached per playlist for the lifetime of the window: reading a
// playlist costs an AppleScript round trip proportional to its length, and the
// contents do not change while the user is browsing.
const musicTrackCache = new Map();

function musicLibraryRoot(card) {
    return card.querySelector('[data-music-library]');
}

/** Renders whichever level of the library the card is currently showing. */
function renderMusicLibrary(card) {
    const root = musicLibraryRoot(card);

    if (!root) {
        return;
    }

    const view = root.dataset.view || 'home';
    const playlistName = root.dataset.playlist || '';

    if (view === 'playlist') {
        const cached = musicTrackCache.get(playlistName);

        root.innerHTML = `
            <div class="music-library-head">
                <button class="music-back" type="button" data-music-back aria-label="Back to playlists">‹</button>
                <span class="music-section-label music-crumb" title="${escapeHtml(playlistName)}">${escapeHtml(playlistName)}</span>
                <button class="music-play-all" type="button" data-music-play-playlist="${escapeHtml(playlistName)}">Play</button>
            </div>
            <input class="music-search" type="search" placeholder="Filter tracks" data-music-filter aria-label="Filter tracks">
            <div class="music-playlist-list">
                ${cached === undefined
                    ? '<div class="music-loading">Reading playlist…</div>'
                    : (cached.length
                        ? cached.map((track) => `
                            <div class="music-track-row" role="button" tabindex="0"
                                 data-music-play-track="${escapeHtml(String(track.index))}"
                                 data-search-key="${escapeHtml(`${track.name} ${track.artist}`.toLowerCase())}">
                                <span class="music-track-index">${escapeHtml(String(track.index))}</span>
                                <span class="music-track-main">
                                    <span class="music-track-name">${escapeHtml(track.name)}</span>
                                    <span class="music-track-artist">${escapeHtml(track.artist)}</span>
                                </span>
                                ${track.duration ? `<span class="music-track-time">${formatClockSeconds(track.duration)}</span>` : ''}
                            </div>`).join('')
                        : '<div class="music-loading">This playlist reported no tracks.</div>')}
            </div>
        `;

        attachMusicFilter(root);
        return;
    }

    const playlists = safeJsonParse(root.dataset.playlists) || [];

    root.innerHTML = `
        <div class="music-library-head">
            <span class="music-section-label">Your playlists</span>
            <span class="music-count">${playlists.length}</span>
        </div>
        <input class="music-search" type="search" placeholder="Filter playlists" data-music-filter aria-label="Filter playlists">
        <div class="music-playlist-list">
            ${playlists.length
                ? playlists.map((entry) => {
                    const name = typeof entry === 'string' ? entry : entry.name;
                    const count = typeof entry === 'string' ? null : entry.count;
                    return `
                        <div class="music-playlist-row" role="button" tabindex="0"
                             data-music-open-playlist="${escapeHtml(name)}"
                             data-search-key="${escapeHtml(String(name).toLowerCase())}">
                            <span class="music-playlist-glyph">♪</span>
                            <span class="music-playlist-name">${escapeHtml(name)}</span>
                            ${count ? `<span class="music-track-time">${count}</span>` : ''}
                            <span class="music-chevron">›</span>
                        </div>`;
                }).join('')
                : '<div class="music-loading">No playlists reported by Music.</div>'}
        </div>
    `;

    attachMusicFilter(root);
}

/** Wires playlist browsing and playback. All actions hit Music.app for real. */
function attachMusicLibrary(card) {
    const root = musicLibraryRoot(card);

    if (!root || root.dataset.libraryWired === 'true') {
        return;
    }

    root.dataset.libraryWired = 'true';

    const activate = (event) => {
        const ipcRenderer = electronIpcRenderer();

        if (!ipcRenderer) {
            return;
        }

        const back = event.target.closest('[data-music-back]');
        const openRow = event.target.closest('[data-music-open-playlist]');
        const playAll = event.target.closest('[data-music-play-playlist]');
        const trackRow = event.target.closest('[data-music-play-track]');

        if (back) {
            event.preventDefault();
            event.stopPropagation();
            root.dataset.view = 'home';
            delete root.dataset.playlist;
            renderMusicLibrary(card);
            return;
        }

        if (playAll) {
            event.preventDefault();
            event.stopPropagation();
            ipcRenderer.invoke('music:playPlaylist', playAll.dataset.musicPlayPlaylist)
                .then(() => {
                    scheduleMusicStateRefresh(400);
                    window.setTimeout(() => refreshMusicState(), 1200);
                })
                .catch(() => {});
            return;
        }

        if (trackRow) {
            event.preventDefault();
            event.stopPropagation();
            const index = Number(trackRow.dataset.musicPlayTrack);
            ipcRenderer.invoke('music:playTrack', root.dataset.playlist || '', index)
                .then(() => {
                    scheduleMusicStateRefresh(400);
                    window.setTimeout(() => refreshMusicState(), 1200);
                })
                .catch(() => {});
            return;
        }

        if (openRow) {
            event.preventDefault();
            event.stopPropagation();
            const name = openRow.dataset.musicOpenPlaylist;
            root.dataset.view = 'playlist';
            root.dataset.playlist = name;
            renderMusicLibrary(card);

            if (musicTrackCache.has(name)) {
                return;
            }

            // Loaded only on open, so browsing does not read the whole library.
            ipcRenderer.invoke('music:playlistTracks', name)
                .then((result) => {
                    musicTrackCache.set(name, (result && result.tracks) || []);

                    if (root.dataset.view === 'playlist' && root.dataset.playlist === name) {
                        renderMusicLibrary(card);
                    }
                })
                .catch(() => {
                    musicTrackCache.set(name, []);
                    renderMusicLibrary(card);
                });
        }
    };

    root.addEventListener('click', activate);
    root.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
            activate(event);
        }
    });

    renderMusicLibrary(card);
}

function renderLegacyWebWidget(card, body) {
    const safeUrl = escapeHtml(card.url || 'about:blank');

    body.innerHTML = `
        <webview class="hud-card-webview" src="${safeUrl}"></webview>
    `;
}

function updateCardElement(card, element) {
    const title = element.querySelector('.hud-card-title');
    const idLabel = element.querySelector('.hud-card-id');
    const closeButton = element.querySelector('.hud-card-close');
    const webview = element.querySelector('.hud-card-webview');

    element.dataset.cardType = card.type || 'web';
    element.className = workspaceCardClassName(card);
    tagCardForWorkspace(element, card);

    if (String(card.type || '').toLowerCase() === 'map') {
        const stage = element.querySelector('.map-stage');
        const data = card.data || {};
        const nextLat = data.lat !== null && data.lat !== undefined ? Number(data.lat) : null;
        const nextLon = data.lon !== null && data.lon !== undefined ? Number(data.lon) : null;
        const nextKey = `${data.lat ?? ''}:${data.lon ?? ''}:${data.destination ?? ''}`;
        const previousKey = element.dataset.mapPayloadKey || '';

        if (stage && Number.isFinite(nextLat) && Number.isFinite(nextLon) && nextKey !== previousKey) {
            element.dataset.mapPayloadKey = nextKey;
            stage.dataset.mapBaseDestination = String(data.destination || 'Local Map');

            animateTacticalMapTo(stage, nextLat, nextLon, {
                label: normalizeMapPoliticalLabel(data.destination || 'Local Map'),
                zoom: Number.isFinite(Number(data.zoom)) ? Number(data.zoom) : 11,
                duration: 1100
            });
            return;
        }

        if (stage) {
            return;
        }
    }

    if (title) {
        title.innerText = card.title || inferCardTitle(card.url) || 'HUD_CARD';
    }

    if (idLabel) {
        idLabel.innerText = card.id || 'unknown';
    }

    if (closeButton) {
        closeButton.title = `Close ${card.title || inferCardTitle(card.url) || 'HUD card'}`;
    }

    renderNativeWidgetBody(card, element);

    const nextUrl = card.url || 'about:blank';
    const previousUrl = cardUrlCache.get(card.id);
    const nextWebview = element.querySelector('.hud-card-webview');

    if (nextWebview && nextUrl !== previousUrl) {
        nextWebview.src = nextUrl;
        cardUrlCache.set(card.id, nextUrl);
    }
}

function renderCards(cards) {
    const container = getWorkstationWidgetContainer();
    const workstation = workstationCards(cards);
    // The map owns the whole surface on the Workstation, so it is pulled out of
    // the floating-widget list entirely. Workshop windows keep it as a widget.
    const mapCard = isWorkshopWindow
        ? null
        : workstation.find((card) => String(card.type || '').toLowerCase() === 'map');
    const visibleCards = mapCard
        ? workstation.filter((card) => card !== mapCard)
        : workstation;

    if (mapCard) {
        renderMapApp(mapCard);
    } else if (document.body.classList.contains('map-app-open')) {
        unmountMapApp();
    }

    if (!container) {
        return;
    }

    if (!visibleCards.length) {
        container.classList.remove('has-widgets');
        // "Clear the workspace" used to blank the container in one frame. Each
        // window plays out instead and removes itself; innerHTML is not touched,
        // because emptying the parent would delete them mid-animation.
        closeAllWithMotion(container, '.hud-card:not([data-closing])');
        cardUrlCache.clear();
        return;
    }

    container.classList.add('has-widgets');

    const activeIds = new Set(visibleCards.map((card) => card.id));

    container.querySelectorAll('.hud-card:not([data-closing])').forEach((element) => {
        if (!activeIds.has(element.dataset.cardId)) {
            closeElementWithMotion(element);
        }
    });

    let largeIndex = 0;

    for (const card of visibleCards) {
        let element = container.querySelector(`.hud-card:not([data-closing])[data-card-id="${CSS.escape(card.id)}"]`);
        const cardLargeIndex = isLargeWorkspaceWidget(card) ? largeIndex : 0;

        if (isLargeWorkspaceWidget(card)) {
            largeIndex += 1;
        }

        if (!element) {
            const hadSavedLayout = localStorage.getItem(cardStorageKey(card.id, card)) !== null;
            element = createCardElement(card, cardLargeIndex);
            container.appendChild(element);
            // A freshly opened widget always lands on top of the stack.
            focusWindow(element);
            observeWidgetSize(element);
            const restoredTop = element.offsetTop;
            const violatedReservedTop = restoredTop < getWorkstationReservedTop(container, element);
            const position = clampWorkspaceCardPosition(element, element.offsetLeft, restoredTop);
            element.style.left = `${position.left}px`;
            element.style.top = `${position.top}px`;

            if (hadSavedLayout && violatedReservedTop && position.top !== restoredTop) {
                saveCardLayout(element);
            }
        } else {
            updateCardElement(card, element);
        }
    }

    autoArrangeCards(visibleCards);
}

function autoArrangeCards(cards) {
    if (!Array.isArray(cards) || cards.length === 0) {
        return;
    }

    const largeCards = cards.filter((card) => isLargeWorkspaceWidget(card));
    const largeBottom = largeCards.reduce((bottom, card) => {
        const element = cardWhiteboard.querySelector(`.hud-card:not([data-closing])[data-card-id="${CSS.escape(card.id)}"]`);
        return element ? Math.max(bottom, element.offsetTop + element.offsetHeight) : bottom;
    }, 0);
    let compactIndex = 0;

    cards.forEach((card, index) => {
        const element = cardWhiteboard.querySelector(`.hud-card:not([data-closing])[data-card-id="${CSS.escape(card.id)}"]`);

        if (!element) {
            return;
        }

        if (isLargeWorkspaceWidget(card) || localStorage.getItem(cardStorageKey(card.id, card))) {
            return;
        }

        const followsLargeWidget = largeBottom > 0;
        const baseLeft = followsLargeWidget
            ? 64 + (compactIndex % 2) * 470
            : Number(card.x ?? 64);
        const baseTop = followsLargeWidget
            ? largeBottom + 48 + Math.floor(compactIndex / 2) * 330
            : Number(card.y ?? 76);
        const baseWidth = Number(card.width ?? 430);
        const baseHeight = Number(card.height ?? 280);

        element.style.left = `${baseLeft + (followsLargeWidget ? 0 : index * 34)}px`;
        element.style.top = `${baseTop + (followsLargeWidget ? 0 : index * 34)}px`;
        element.style.width = `${baseWidth}px`;
        element.style.height = `${baseHeight}px`;
        const position = clampWorkspaceCardPosition(element, element.offsetLeft, element.offsetTop);
        element.style.top = `${position.top}px`;
        compactIndex += 1;
    });
}

function renderState(state) {
    latestState = state;
    const showcaseState = state.showcase_mode || {};
    applyTheme(state.theme || state.settings?.theme || settingsPayload?.settings?.theme || 'blue');
    applyInterfaceSettings(state.settings || settingsPayload?.settings || {});

    if (isWorkshopWindow) {
        if (Array.isArray(state.notification_history)) {
            setNotificationHistory(state.notification_history);
        }

        if (showcaseState.active) {
            startShowcaseMode();
        } else if (showcaseActive) {
            stopShowcaseMode();
        }

        renderWorkshopMode(state.workshop_mode || {});
        return;
    }

    applyHudMode(state);
    renderSleepScreen(state.sleep_screen || {});

    const hudMode = String(state.hud_mode || '').toUpperCase();
    const aiStatus = String(state.ai_status || 'IDLE').toUpperCase();
    // voice_phase carries the finer listening/user-speaking distinction; ai_status
    // remains the fallback for any state produced outside the voice pipeline.
    const voicePhase = String(state.voice_phase || '').toUpperCase();
    const resolvedStatus = voicePhase === 'USER_SPEAKING' ? 'USER_SPEAKING' : aiStatus;
    const orbStatus = hudMode === 'SLEEP' && aiStatus !== 'SPEAKING' ? 'SLEEP' : resolvedStatus;
    renderOrb(orbStatus);
    renderTranscript(state.live_transcript);
    renderOverrideResponse(state.override_response);
    renderCards(state.active_cards);
    initializeWorkspaceScroll(document.getElementById('workstation-scroll'), 'workstation');
    createHotbar();
    renderWorkshopMode(state.workshop_mode || {});

    if (Array.isArray(state.notification_history)) {
        setNotificationHistory(state.notification_history);
    }

    if (state.last_error && state.last_error.text) {
        appendSystemLine(`ERROR: ${state.last_error.text}`, '#ff4f6d');
    }

    if (state.shutdown_requested === true) {
        shutdownInterface('brain_state_shutdown');
    }

    if (showcaseState.active) {
        startShowcaseMode();
    } else if (showcaseActive) {
        stopShowcaseMode();
    }
}

function attachCardClose(card) {
    const closeButton = card.querySelector('.hud-card-close');

    if (!closeButton) {
        return;
    }

    closeButton.addEventListener('mousedown', (event) => {
        event.preventDefault();
        event.stopPropagation();
    });

    closeButton.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();

        const cardId = card.dataset.cardId;

        if (!cardId) {
            closeElementWithMotion(card);
            return;
        }

        // The backend is told IMMEDIATELY, not after the animation. This used to
        // sit inside a 260ms setTimeout, so every close held its state update
        // back by a quarter second and a second click in that window closed a
        // window the backend still believed was open. The motion is presentation;
        // it must not sit in front of the command.
        cardUrlCache.delete(cardId);
        socket.emit('close_hud_card', { card_id: cardId });
        closeElementWithMotion(card);
    });
}

/**
 * Every music control goes straight to Music.app over IPC and then re-reads real
 * state. Transport used to be dispatched to Python over the socket, which was a
 * longer path for the same AppleScript and gave no result to confirm against.
 *
 * There is no optimistic UI here beyond a momentary pressed state: the icon is
 * only ever set from what Music actually reports, which is what stops the widget
 * desynchronising when a click is dropped or the user acts in Music.app directly.
 */
function attachMusicControls(card) {
    if (String(card.dataset.cardType || '').toLowerCase() !== 'music') {
        return;
    }

    const ipcChannels = {
        toggle: 'music:toggle',
        play: 'music:play',
        resume: 'music:play',
        pause: 'music:pause',
        next: 'music:next',
        previous: 'music:previous'
    };

    card.addEventListener('click', (event) => {
        const modeButton = event.target.closest('[data-music-mode]');
        const button = event.target.closest('[data-music-command]');
        const ipcRenderer = electronIpcRenderer();

        if (modeButton && ipcRenderer) {
            event.preventDefault();
            event.stopPropagation();

            const mode = modeButton.dataset.musicMode;
            const turningOn = modeButton.dataset.modeOn !== 'true';

            ipcRenderer.invoke('music:mode', mode, turningOn)
                .then(() => scheduleMusicStateRefresh(250))
                .catch(() => {});
            return;
        }

        if (!button) {
            return;
        }

        event.preventDefault();
        event.stopPropagation();

        const command = String(button.dataset.musicCommand || '');
        const channel = ipcChannels[command];

        if (!channel || !ipcRenderer) {
            return;
        }

        button.classList.add('is-pressed');
        window.setTimeout(() => button.classList.remove('is-pressed'), 160);

        const changesTrack = command === 'next' || command === 'previous';

        ipcRenderer.invoke(channel)
            .then(() => {
                // Music.app does not settle on a new track synchronously. Measured
                // against the real app, a read at 900ms still returned the OLD
                // title and duration — which is what made next/previous look like
                // they had failed even once the AppleScript was doing its job.
                //
                // Play/pause settles immediately, so it only needs the early read.
                scheduleMusicStateRefresh(250);

                if (changesTrack) {
                    window.setTimeout(() => refreshMusicState(), 1300);
                    window.setTimeout(() => refreshMusicState(), 2500);
                }
            })
            .catch(() => {});
    });

    attachMusicSeek(card);
    attachMusicLibrary(card);
}

/**
 * Click or drag anywhere on the progress bar to seek.
 *
 * Offered because `player position` is settable in Music's scripting dictionary —
 * verified against the running app, not assumed. While dragging, the bar is driven
 * by the pointer and the periodic repaint stands down (`data-seeking`) so the two
 * do not fight over the same element.
 */
function attachMusicSeek(card) {
    const wrap = card.querySelector('.music-progress');
    const track = wrap?.querySelector('.music-progress-track');

    if (!wrap || !track) {
        return;
    }

    const positionFromEvent = (event) => {
        const rect = track.getBoundingClientRect();
        const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
        return ratio * (musicClock.duration || 0);
    };

    const preview = (event) => {
        if (!musicClock.duration) {
            return;
        }

        const seconds = positionFromEvent(event);
        const fill = wrap.querySelector('.music-progress-fill');
        const times = wrap.querySelectorAll('.music-progress-times span');

        if (fill) {
            fill.style.width = `${((seconds / musicClock.duration) * 100).toFixed(2)}%`;
        }

        if (times.length === 2) {
            times[0].textContent = formatClockSeconds(seconds);
        }
    };

    track.addEventListener('pointerdown', (event) => {
        if (!musicClock.duration) {
            return;
        }

        event.preventDefault();
        event.stopPropagation();
        wrap.dataset.seeking = 'true';
        try { track.setPointerCapture(event.pointerId); } catch (_) { /* optional */ }
        preview(event);

        const move = (moveEvent) => preview(moveEvent);

        const up = (upEvent) => {
            track.removeEventListener('pointermove', move);
            track.removeEventListener('pointerup', up);
            track.removeEventListener('pointercancel', up);

            const seconds = positionFromEvent(upEvent);
            delete wrap.dataset.seeking;

            const ipcRenderer = electronIpcRenderer();

            if (!ipcRenderer) {
                return;
            }

            // Assume the seek landed so the bar does not snap backwards for a
            // moment; the next real read corrects it either way.
            musicClock.position = seconds;
            musicClock.syncedAt = Date.now();

            ipcRenderer.invoke('music:seek', seconds)
                .then(() => scheduleMusicStateRefresh(250))
                .catch(() => {});
        };

        track.addEventListener('pointermove', move);
        track.addEventListener('pointerup', up);
        track.addEventListener('pointercancel', up);
    });
}

function attachNewsControls(card) {
    if (String(card.dataset.cardType || '').toLowerCase() !== 'news') {
        return;
    }

    card.addEventListener('click', (event) => {
        const tabButton = event.target.closest('[data-news-tab]');
        const headlineCard = event.target.closest('[data-news-link]');

        if (tabButton) {
            event.preventDefault();
            event.stopPropagation();

            const tab = tabButton.dataset.newsTab || 'us';
            const body = card.querySelector('.native-widget-body');

            card._newsActiveTab = tab;
            localStorage.setItem(cardStateStorageKey(card.dataset.cardId, card), JSON.stringify({ activeTab: tab }));

            if (body && card._newsData) {
                renderNewsWidget(card._newsData, body, tab);
            }

            return;
        }

        if (headlineCard) {
            const link = headlineCard.dataset.newsLink;

            if (link) {
                try {
                    window.open(link, '_blank');
                } catch (_) {
                    // Ignore failed external open.
                }
            }
        }
    });
}

// Both kept as thin wrappers: every widget factory in this file calls them, and
// the engine above is idempotent, so the two paths converge on one implementation
// without touching several dozen call sites.
function attachCardDrag(card) {
    enableWindowInteractions(card);
}

function attachCardResize(card) {
    enableWindowInteractions(card);
}

function shutdownInterface(reason = 'shutdown_requested') {
    if (shutdownInProgress) {
        return;
    }

    shutdownInProgress = true;
    appendSystemLine(`HUD shutdown requested: ${reason}.`);
    document.body.classList.add('hud-shutdown');

    setTimeout(() => {
        try {
            const electronRequire = window.require || require;

            if (typeof electronRequire === 'function') {
                const { ipcRenderer } = electronRequire('electron');
                ipcRenderer.send('shutdown-hud');
                return;
            }
        } catch (_) {
            // Fallback below.
        }

        try {
            window.close();
        } catch (_) {
            appendSystemLine('Window close failed. Close the Electron window manually.', '#ff4f6d');
        }
    }, 250);
}

function sendHudWindowCommand(channel) {
    try {
        const electronRequire = window.require || require;

        if (typeof electronRequire === 'function') {
            const { ipcRenderer } = electronRequire('electron');
            ipcRenderer.send(channel);
            return true;
        }
    } catch (_) {
        return false;
    }

    return false;
}

function hideHudInterface(reason = 'interface_offline') {
    appendSystemLine(`HUD hidden: ${reason}.`);

    if (sendHudWindowCommand('hide-hud-window')) {
        return;
    }

    try {
        window.close();
        return;
    } catch (_) {
        // Fallback below.
    }

    document.body.classList.add('hud-interface-hidden');
}

function showHudInterface() {
    document.body.classList.remove('hud-interface-hidden', 'hud-shutdown');
    shutdownInProgress = false;
    sendHudWindowCommand('show-hud-window');
}

socket.on('connect', () => {
    appendSystemLine('Neural Link Established on Port 5050.');
});

socket.on('connect_error', () => {
    if (shutdownInProgress || (latestState && latestState.shutdown_requested === true)) {
        shutdownInterface('brain_disconnected_after_shutdown');
        return;
    }

    appendSystemLine('ERROR: Cannot find Brain on Port 5050.', '#ff4f6d');
    renderOrb('ERROR');
});

socket.on('disconnect', () => {
    if (shutdownInProgress || (latestState && latestState.shutdown_requested === true)) {
        shutdownInterface('brain_disconnected_after_shutdown');
    }
});

socket.on('shutdown_hud', (data) => {
    const reason = data && data.reason ? data.reason : 'brain_shutdown';
    shutdownInterface(reason);
});

socket.on('friday_full_shutdown', () => {
    if (sendHudWindowCommand('friday:full-shutdown')) {
        return;
    }

    shutdownInterface('brain_full_shutdown');
});

socket.on('hide_hud', (data) => {
    const reason = data && data.reason ? data.reason : 'interface_offline';
    hideHudInterface(reason);
});

socket.on('show_hud', () => {
    showHudInterface();
});

socket.on('state_update', (state) => {
    updateFaceIdIndicator(state);
    renderState(state);
});

socket.on('workshop_chat_delta', (payload) => {
    applyWorkshopChatDelta(payload);
});

socket.on('proactive_notification', (notification) => {
    showProactiveNotification(notification);
});

socket.on('memory_learned', (payload) => {
    showMemoryLearnedToast(payload);
});

socket.on('notification_history_updated', (payload) => {
    const notifications = Array.isArray(payload?.notifications) ? payload.notifications : [];
    setNotificationHistory(notifications);
});

socket.on('notification_center_toggle', (payload) => {
    const notifications = Array.isArray(payload?.notifications) ? payload.notifications : [];
    setNotificationHistory(notifications);
    toggleNotificationCenter(true);
});

socket.on('notification_center_close', () => {
    toggleNotificationCenter(false);
});

socket.on('close_calendar_page', () => {
    closeCalendarPage();
});

socket.on('virtual_finder_navigate', (payload) => {
    const action = String(payload?.action || 'home').trim().toLowerCase();
    const workspace = String(payload?.workspace || '').trim().toLowerCase();
    const body = virtualFinderActiveBody(null, workspace);

    if (body) {
        const controller = getVirtualFinderController(body);

        if (action === 'back') {
            navigateVirtualFinderHistory(body, 'back');
        } else if (action === 'forward') {
            navigateVirtualFinderHistory(body, 'forward');
        } else if (action === 'up') {
            openVirtualFinderPath(body, controller?.parentPath || '');
        } else if (action === 'refresh') {
            refreshVirtualFinder(body);
        } else {
            openVirtualFinderPath(body, payload?.path || '');
        }
        return;
    }

    if (action === 'back') {
        const previous = virtualFinderBackStack.pop();

        socket.emit('virtual_finder_open_path', {
            path: previous || '',
            ...(workspace ? { workspace } : {})
        }, (response = {}) => {
            if (response?.ok === false && previous !== undefined) {
                virtualFinderBackStack.push(previous);
            }
        });
        return;
    }

    socket.emit('virtual_finder_open_path', {
        path: payload?.path || '',
        ...(workspace ? { workspace } : {})
    });
});

socket.on('virtual_desktop_update', (payload) => {
    virtualDesktopPayload = payload || {};

    if (!isWorkshopWindow) {
        return;
    }

    if (workshopRole === 'secondary' || workshopRole === 'single') {
        renderVirtualDesktopIcons(virtualDesktopPayload, document.querySelector('.workshop-workspace'));
    }
});

socket.on('open_calendar_page', (payload) => {
    const data = payload || {};

    if (isWorkshopWindow) {
        if (workspaceMatchesWindow(data.workspace || 'main')) {
            openCalendarPage(data);
        }

        return;
    }

    if (!latestState?.workshop_mode?.active) {
        openCalendarPage(data);
    }
});

socket.on('open_settings_page', (payload) => {
    if (isWorkshopWindow) {
        return;
    }

    settingsPayload = payload || settingsPayload || {};
    emitDirectAction('open_settings', 'legacy_settings_event');
});

socket.on('settings_payload_update', (payload) => {
    settingsPayload = payload || {};
    applyTheme(settingsPayload.theme || settingsPayload.settings?.theme || latestState?.theme || 'blue');
    applyInterfaceSettings(settingsPayload.settings || {});

    if (document.querySelector('.settings-page.active')) {
        renderSettingsPage();
    }

    updateSettingsWidgetSurfaces(settingsPayload);
});

socket.on('presence_status_update', (payload) => {
    const presence = payload && typeof payload === 'object' ? payload : {};
    settingsPayload = settingsPayload || {};
    settingsPayload.presence = presence;
    settingsPayload.presence_mode = presence.presence_mode;
    settingsPayload.presence_mode_enabled = presence.presence_mode_enabled;
    settingsPayload.presence_source = presence.presence_source;
    settingsPayload.presence_user = presence.user_presence;
    settingsPayload.presence_idle_seconds = presence.idle_seconds;
    settingsPayload.presence_away_timeout_minutes = presence.away_timeout_minutes;
    settingsPayload.presence_last_seen = presence.last_seen_at;
    settingsPayload.presence_away_since = presence.away_since;

    if (document.querySelector('.settings-page.active')) {
        renderSettingsPage();
    }

    updateSettingsWidgetSurfaces(settingsPayload);
});

socket.on('camera_presence_status_update', (payload) => {
    const cameraPresence = payload && typeof payload === 'object' ? payload : {};
    settingsPayload = settingsPayload || {};
    settingsPayload.camera_presence = cameraPresence;
    settingsPayload.camera_presence_enabled = cameraPresence.enabled === true;
    settingsPayload.camera_presence_mode = cameraPresence.mode || 'IDLE';
    settingsPayload.camera_presence_active_camera = cameraPresence.active_camera_label || 'None';
    settingsPayload.camera_presence_door_device_index = cameraPresence.door_camera_index;
    settingsPayload.camera_presence_desk_device_index = cameraPresence.desk_camera_index;
    settingsPayload.camera_presence_last_door_seen_at = cameraPresence.last_door_seen_at;
    settingsPayload.camera_presence_last_desk_seen_at = cameraPresence.last_desk_seen_at;
    settingsPayload.camera_presence_last_camera_switch_at = cameraPresence.last_camera_switch_at;
    settingsPayload.camera_presence_last_motion_score = cameraPresence.last_motion_score;
    settingsPayload.camera_presence_status = cameraPresence.camera_status || cameraPresence.status || 'Unavailable';
    settingsPayload.camera_presence_check_interval_seconds = cameraPresence.check_interval_seconds;
    settingsPayload.camera_presence_unknown_greeting_enabled = cameraPresence.unknown_greeting_enabled === true;
    settingsPayload.camera_presence_primary_user_greeting_enabled = cameraPresence.primary_user_greeting_enabled === true;
    settingsPayload.camera_presence_auto_handoff_enabled = cameraPresence.auto_handoff_enabled === true;

    if (document.querySelector('.settings-page.active')) {
        renderSettingsPage();
    }

    updateSettingsWidgetSurfaces(settingsPayload);
});

socket.on('close_settings_page', () => {
    closeSettingsPageLocal();
});

socket.on('tasks_payload_update', (payload) => {
    updateTaskSurfaces(payload || {});
});

socket.on('open_tasks_page', (payload) => {
    if (isWorkshopWindow) {
        return;
    }

    renderTasksPage(payload || tasksPayload || {});
});

socket.on('close_tasks_page', () => {
    closeTasksPageLocal();
});

socket.on('open_tasks_widget', (payload) => {
    updateTaskSurfaces(payload || tasksPayload || {});
});

socket.on('close_tasks_widget', () => {
    closeAllWithMotion(document, '.hud-card.widget-type-tasks:not([data-closing])');
});

socket.on('workshop_analytics_update', (payload) => {
    workshopAnalyticsPayload = payload || {};

    if (isWorkshopWindow && !latestState) {
        return;
    }

    renderWorkshopMode((latestState && latestState.workshop_mode) || { active: workshopModeActive });
});

socket.on('workshop_mode_toggle', (payload) => {
    if (latestState) {
        latestState.workshop_mode = payload || {};
    }

    renderWorkshopMode(payload || {}, true);
});

function handleWorkshopOpenWidgetEvent(payload) {
    const data = payload && typeof payload === 'object' ? payload : { widget: payload };
    const targetWorkspace = String(payload?.workspace || 'main').toLowerCase();

    if (!isWorkshopWindow || !workspaceMatchesWindow(targetWorkspace)) {
        return;
    }

    openWorkshopWidget(data.widget || data.type, {
        ...data,
        workspace: targetWorkspace,
        fromBackend: true,
        source: data.source || 'backend'
    });
}

function handleWorkshopDirectActionEvent(payload) {
    const data = payload && typeof payload === 'object' ? payload : { action: payload };
    const action = data.action || data.command || data.type || data.value || '';
    const widget = normalizeWorkshopWidgetType(action);
    const targetWorkspace = String(payload?.workspace || 'main').toLowerCase();

    if (!isWorkshopWindow
        || !workspaceMatchesWindow(targetWorkspace)
        || !WORKSHOP_WIDGET_COMMANDS[widget]) {
        return;
    }

    openWorkshopWidget(widget, {
        ...data,
        workspace: targetWorkspace,
        fromBackend: true,
        source: data.source || 'backend_direct'
    });
}

socket.on('workshop_open_widget', handleWorkshopOpenWidgetEvent);
socket.on('open_workshop_widget', handleWorkshopOpenWidgetEvent);
socket.on('workshop_widget_open', handleWorkshopOpenWidgetEvent);
socket.on('direct_action', handleWorkshopDirectActionEvent);

socket.on('showcase_start', (payload) => {
    startShowcaseMode({ restart: payload?.restart === true });
});

socket.on('showcase_stop', (payload) => {
    stopShowcaseMode({ returnSleep: payload?.return_sleep === true });
});

socket.on('showcase_return_sleep', () => {
    stopShowcaseMode({ returnSleep: true });
});

socket.on('showcase_step', (payload) => {
    if (!payload?.step || !showcaseActive) {
        return;
    }

    const index = SHOWCASE_STEPS.findIndex((step) => step.name === payload.step);

    if (index >= 0) {
        renderShowcaseStep(SHOWCASE_STEPS[index], index);
    }
});

{
    const ipcRenderer = electronIpcRenderer();

    if (ipcRenderer) {
        ipcRenderer.on('workshop:refresh-state', () => {
            if (latestState) {
                renderWorkshopMode(latestState.workshop_mode || {});
            }
        });

        ipcRenderer.on('workshop:launcher-action', (_event, payload) => {
            if (payload?.action) {
                handleWorkshopAction(payload.action, payload);
            }
        });

        // The last Workshop display has gone. Python has to hear about it, or
        // workshop_mode stays active in hud_state and the Workstation keeps
        // routing new widgets to a workspace that no longer exists.
        ipcRenderer.on('workshop:all-closed', () => {
            socket.emit('workshop_displays_closed', {});
        });
    }
}

socket.on('focus_manual_override', () => {
    focusManualOverride();
});

function updateFaceIdIndicator(payload) {
    const indicator = document.getElementById('face-id-indicator');

    if (!indicator) {
        return;
    }

    const label = indicator.querySelector('.face-id-label');
    const state = String(
        payload?.presence_state ||
        payload?.presenceState ||
        payload?.state ||
        'SCANNING'
    ).toUpperCase();

    const previousState = indicator.dataset.presenceState || '';
    indicator.dataset.presenceState = state;

    if (previousState === state && state === 'AUTHORIZED') {
        return;
    }

    if (previousState === state && indicator.classList.contains('hidden')) {
        return;
    }

    indicator.classList.remove('scanning', 'success', 'failure', 'hidden');

    if (state === 'AUTHORIZED') {
        indicator.classList.add('success');

        if (label) {
            label.textContent = 'AUTHORIZED';
        }

        window.clearTimeout(indicator._hideTimer);
        indicator._hideTimer = window.setTimeout(() => {
            indicator.classList.add('hidden');
        }, 2000);
        return;
    }

    if (state === 'UNAUTHORIZED') {
        indicator.classList.add('failure');

        if (label) {
            label.textContent = 'UNKNOWN';
        }

        document.body.dataset.mode = 'sleep';
        return;
    }

    if (state === 'VACANT') {
        indicator.classList.add('scanning');

        if (label) {
            label.textContent = 'VACANT';
        }

        document.body.dataset.mode = 'sleep';
        return;
    }

    indicator.classList.add('scanning');

    if (label) {
        label.textContent = 'SCANNING';
    }
}

socket.on('presence_update', updateFaceIdIndicator);

chatInput.addEventListener('keypress', function (event) {
    if (event.key === 'Enter' && chatInput.value.trim() !== '') {
        const text = chatInput.value.trim();

        if (showcaseActive && !isShowcaseExitText(text)) {
            chatInput.value = '';
            return;
        }

        overrideResponse.className = 'override-response pending';
        overrideResponse.innerText = 'Processing override...';

        socket.emit('manual_override', {
            text: text
        });

        chatInput.value = '';
    }
});

socket.on('change_url', (data) => {
    if (!data || !data.url) {
        return;
    }

    cardWhiteboard.classList.add('has-widgets');

    renderCards([
        {
            id: 'legacy_web_view',
            type: 'web',
            title: inferCardTitle(data.url) || 'SYS_WEB_VIEW',
            url: data.url,
            x: 80,
            y: 80,
            width: 520,
            height: 360,
            data: {}
        }
    ]);
});

socket.on('friday_speech', (data) => {
    if (!latestState || !Array.isArray(latestState.live_transcript)) {
        const p = document.createElement('p');

        p.className = 'friday-text';
        p.style.color = 'var(--accent-secondary)';
        p.innerText = `> FRIDAY: ${data.text}`;

        transcriptBox.appendChild(p);
        transcriptBox.scrollTop = transcriptBox.scrollHeight;
    }
});

// friday_speech_start / friday_speech_end are raised by the audio engine against
// real playback, so the speaking animation begins with audible speech and ends
// the moment the player exits.
socket.on('friday_speech_start', () => {
    renderOrb('SPEAKING');
});

socket.on('friday_speech_end', () => {
    resetOrbAudioLevel();
    const hudMode = String(latestState?.hud_mode || '').toUpperCase();
    renderOrb(hudMode === 'SLEEP' ? 'SLEEP' : 'IDLE');
});

// Single authoritative phase feed: IDLE / LISTENING / USER_SPEAKING / THINKING /
// FRIDAY_SPEAKING. Compact enough to send on every transition without paying for
// a full HUD state broadcast.
socket.on('voice_state', (payload) => {
    const phase = String(payload?.phase || '').toUpperCase();

    if (!phase) {
        return;
    }

    if (latestState) {
        latestState.voice_phase = phase;
        latestState.ai_status = String(payload?.ai_status || latestState.ai_status || 'IDLE');
    }

    const hudMode = String(latestState?.hud_mode || '').toUpperCase();

    if (phase === 'IDLE' && hudMode === 'SLEEP') {
        renderOrb('SLEEP');
        return;
    }

    renderOrb(phase === 'FRIDAY_SPEAKING' ? 'SPEAKING' : phase);
});

socket.on('user_audio_level', (payload) => {
    pushOrbAudioLevel(payload?.level, payload?.state || 'listening');
});

socket.on('friday_audio_level', (payload) => {
    pushOrbAudioLevel(payload?.level, 'friday_speaking');
});

if (!isWorkshopWindow) {
    initializeWorkspaceScroll(document.getElementById('workstation-scroll'), 'workstation');
}

initializeWidgets();
observeAllWidgetSizes();
ensureNotificationLauncher();
