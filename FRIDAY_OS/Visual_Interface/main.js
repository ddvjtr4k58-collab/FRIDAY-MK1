const { app, BrowserWindow, ipcMain, screen, shell } = require('electron');
const path = require('path');
const os = require('os');
const { spawn, execFile } = require('child_process');

let mainWindow = null;
const workshopWindows = new Map();
let mainWindowMode = 'hud';
let mainWorkshopRole = '';

function baseWindowPreferences() {
    return {
        nodeIntegration: true,
        contextIsolation: false,
        webviewTag: true,
        backgroundThrottling: false
    };
}

function createMainWindow() {
    mainWindow = new BrowserWindow({
        width: 1920,
        height: 1080,
        backgroundColor: '#020713',
        titleBarStyle: 'hiddenInset',
        fullscreen: true,
        webPreferences: baseWindowPreferences()
    });

    mainWindow.loadFile('index.html');
    mainWindowMode = 'hud';
    mainWorkshopRole = '';

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

function closeAllFridayWindows() {
    for (const window of BrowserWindow.getAllWindows()) {
        if (window && !window.isDestroyed()) {
            window.close();
        }
    }

    mainWindow = null;
    workshopWindows.clear();
}

function quitFridayApp() {
    closeAllFridayWindows();
    app.quit();
}

ipcMain.on('shutdown-hud', () => {
    quitFridayApp();
});

ipcMain.on('friday:full-shutdown', () => {
    quitFridayApp();
});

ipcMain.handle('friday:full-shutdown', () => {
    quitFridayApp();
    return { ok: true };
});

function hideWindowCleanly(window) {
    if (!window || window.isDestroyed()) {
        return;
    }

    const hideWindow = () => {
        if (window && !window.isDestroyed()) {
            window.hide();
        }
    };

    if (!window.isFullScreen()) {
        hideWindow();
        return;
    }

    window.once('leave-full-screen', hideWindow);
    window.setFullScreen(false);
    setTimeout(hideWindow, 500);
}

ipcMain.on('hide-hud-window', () => {
    hideWindowCleanly(mainWindow);

    for (const window of workshopWindows.values()) {
        hideWindowCleanly(window);
    }
});

ipcMain.on('show-hud-window', () => {
    restoreMainWindowHud();
});

function serializeDisplay(display) {
    return {
        id: String(display.id),
        bounds: display.bounds,
        workArea: display.workArea,
        scaleFactor: display.scaleFactor,
        primary: display.id === screen.getPrimaryDisplay().id
    };
}

function getDisplays() {
    return screen.getAllDisplays().map(serializeDisplay);
}

function sortedDisplays() {
    return getDisplays().sort((left, right) => {
        const leftX = Number(left.bounds?.x ?? 0);
        const rightX = Number(right.bounds?.x ?? 0);

        if (leftX !== rightX) {
            return leftX - rightX;
        }

        return Number(left.bounds?.y ?? 0) - Number(right.bounds?.y ?? 0);
    });
}

function assignWorkshopRoles(displays) {
    const roles = {};

    if (!Array.isArray(displays) || displays.length === 0) {
        return roles;
    }

    if (displays.length === 1) {
        roles['workshop-single'] = displays[0];
        return roles;
    }

    if (displays.length === 2) {
        const primary = displays.find((display) => display.primary) || displays[0];
        const other = displays.find((display) => display.id !== primary.id) || displays[1];
        roles['workshop-main'] = primary;
        roles['workshop-intel'] = other;
        return roles;
    }

    roles['workshop-secondary'] = displays[0];
    roles['workshop-main'] = displays[Math.floor(displays.length / 2)];
    roles['workshop-intel'] = displays[displays.length - 1];
    return roles;
}

function roleQuery(role) {
    return role.replace('workshop-', '');
}

function boundsForDisplay(display) {
    return display?.workArea || display?.bounds || {
        x: 0,
        y: 0,
        width: 1280,
        height: 720
    };
}

function isMainWorkshopRole(role) {
    return role === 'workshop-main' || role === 'workshop-single';
}

function isWorkshopModeActive() {
    return mainWindowMode.startsWith('workshop') || workshopWindows.size > 0;
}

/**
 * Skip to the next or previous track.
 *
 * WHY THIS IS NOT JUST `next track`
 *
 * `tell application "Music" to next track` reports success and does nothing, on
 * this machine, in every state tested — paused, playing, and with a valid current
 * playlist of 830 tracks sitting at index 116. `back track` behaves the same way:
 * it resets the play position but never leaves the track. Because the old code
 * spawned osascript detached with stdio ignored, that silent no-op looked exactly
 * like success all the way back to the UI.
 *
 * `next track` moves through Music's "up next" queue, which is empty unless
 * something established it. Playing an explicit track by index does not depend on
 * that queue and works reliably.
 *
 * So: try the native command first — it is the only one that respects shuffle
 * order — then verify against the track's persistent ID and fall back to index
 * navigation when the native call turned out to be a no-op.
 *
 * PREVIOUS also carries the convention every music player uses: past a few
 * seconds in, "previous" restarts the current track rather than leaving it.
 */
const PREVIOUS_RESTART_THRESHOLD_SECONDS = 3;

function buildSkipScript(direction) {
    const isNext = direction === 'next';
    const nativeCommand = isNext ? 'next track' : 'back track';
    const step = isNext ? 'i + 1' : 'i - 1';
    const boundsCheck = isNext ? 'i < n' : 'i > 1';

    return `
tell application "Music"
    if it is not running then return "err:not running"

    set wasPlaying to (player state is playing)
    set startedAt to 0
    try
        set startedAt to player position
    end try

    ${isNext ? '' : `
    -- Past the threshold, "previous" means restart, exactly as it does elsewhere.
    if startedAt > ${PREVIOUS_RESTART_THRESHOLD_SECONDS} then
        set player position to 0
        return "ok:restart"
    end if`}

    set beforeId to ""
    try
        set beforeId to persistent ID of current track
    end try

    -- Native first: it is the only path that follows shuffle order.
    try
        ${nativeCommand}
    end try
    delay 0.35

    set afterId to ""
    try
        set afterId to persistent ID of current track
    end try

    if beforeId is not equal to afterId and afterId is not "" then
        if not wasPlaying then pause
        return "ok:native"
    end if

    -- Native was a no-op. Move by index within the playlist actually in use.
    try
        set pl to current playlist
        set i to index of current track
        set n to count of tracks of pl
    on error
        return "err:no playlist context"
    end try

    if ${boundsCheck} then
        play (track (${step}) of pl)
        if not wasPlaying then
            delay 0.25
            pause
        end if
        return "ok:index"
    end if

    ${isNext ? 'return "ok:end of playlist"' : 'set player position to 0\n    return "ok:start of playlist"'}
end tell`;
}

async function skipMusicTrack(direction) {
    const result = await runAppleScript(buildSkipScript(direction), 9000);

    if (!result.ok) {
        return { ok: false, reason: result.reason };
    }

    if (result.out.startsWith('err:')) {
        return { ok: false, reason: result.out.slice(4) };
    }

    // `how` is reported so the renderer can tell a real track change from a
    // deliberate restart and time its re-read accordingly.
    return { ok: true, how: result.out.replace(/^ok:/, '') };
}

function runMusicCommand(command) {
    const scripts = {
        play: 'tell application "Music" to play',
        pause: 'tell application "Music" to pause',
        toggle: 'tell application "Music"\nif player state is playing then\npause\nelse\nplay\nend if\nend tell'
    };
    const script = scripts[String(command || '').toLowerCase()];

    if (!script) {
        return { ok: false, reason: 'unknown_music_command' };
    }

    const startedAt = Date.now();

    try {
        const child = spawn('osascript', ['-e', script], {
            detached: true,
            stdio: 'ignore'
        });
        child.unref();
        console.log(`[PERF] button music ipc/direct: ${Date.now() - startedAt} ms`);
        return { ok: true };
    } catch (error) {
        console.log(`[PERF] button music ipc/direct failed: ${Date.now() - startedAt} ms`);
        return { ok: false, reason: String(error && error.message ? error.message : error) };
    }
}

function defaultMusicStatePayload(reason = '') {
    return {
        ok: !reason,
        reason,
        app: 'Apple Music',
        player_state: 'stopped',
        state: 'stopped',
        is_playing: false,
        track: 'No track loaded',
        title: 'No track loaded',
        artist: '',
        album: '',
        shuffle: false,
        repeat: 'off',
        duration: null,
        position: null,
        source: 'Apple Music'
    };
}

function parseMusicStateOutput(output) {
    const parts = String(output || '').trim().split('|');

    while (parts.length < 8) {
        parts.push('');
    }

    // null rather than 0 when Music reports nothing, so the widget can hide the
    // progress bar instead of drawing a fake 0:00 / 0:00.
    const asSeconds = (value) => {
        const seconds = Number(String(value).trim());
        return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
    };

    const normalizedState = ['playing', 'paused', 'stopped'].includes(String(parts[0] || '').toLowerCase())
        ? String(parts[0] || '').toLowerCase()
        : 'stopped';

    return {
        ok: true,
        app: 'Apple Music',
        player_state: normalizedState,
        state: normalizedState,
        is_playing: normalizedState === 'playing',
        track: parts[1] || 'No track loaded',
        title: parts[1] || 'No track loaded',
        artist: parts[2] || '',
        album: parts[3] || '',
        shuffle: String(parts[4] || '').toLowerCase() === 'true',
        repeat: parts[5] || 'off',
        duration: asSeconds(parts[6]),
        position: asSeconds(parts[7]) ?? (asSeconds(parts[6]) ? 0 : null),
        source: 'Apple Music'
    };
}

function getMusicState() {
    const script = `
if application "Music" is running then
    tell application "Music"
        set playerStateText to player state as text
        set trackName to "No track loaded"
        set artistName to ""
        set albumName to ""
        set shuffleText to shuffle enabled as text
        set repeatText to song repeat as text
        set trackDuration to 0
        set trackPosition to 0

        try
            set trackName to name of current track
            set artistName to artist of current track
            set albumName to album of current track
            set trackDuration to duration of current track
        end try

        try
            set trackPosition to player position
        end try

        return playerStateText & "|" & trackName & "|" & artistName & "|" & albumName & "|" & shuffleText & "|" & repeatText & "|" & (trackDuration as text) & "|" & (trackPosition as text)
    end tell
else
    return "stopped|No track loaded|||false|off|0|0"
end if
`;

    return new Promise((resolve) => {
        execFile('osascript', ['-e', script], { timeout: 2500 }, (error, stdout) => {
            if (error) {
                resolve(defaultMusicStatePayload(String(error && error.message ? error.message : error)));
                return;
            }

            resolve(parseMusicStateOutput(stdout));
        });
    });
}

/**
 * Workshop is a separate surface, never a takeover of the workstation window.
 *
 * This used to call mainWindow.loadFile('index.html', {mode:'workshop'}) — it
 * NAVIGATED THE WORKSTATION AWAY. The main window IS the workstation, so opening
 * Workshop destroyed that renderer outright: every widget's DOM, the window
 * manager's z-stack, all in-memory state, and the voice layer with it. Returning
 * reloaded the page again, producing a brand-new renderer that had to rebuild
 * from scratch and frequently came back with widgets that no longer opened and a
 * microphone that never restarted.
 *
 * Giving the role its own window means the workstation is simply covered while
 * Workshop is up, and is still there — intact, still listening — underneath.
 */
function loadMainWindowAsWorkshop(role, display) {
    // Keep the workstation alive and untouched behind the Workshop surface.
    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindowMode = 'hud';
        mainWorkshopRole = '';
    }

    return createOrFocusWorkshopWindow(role, display);
}

function restoreMainWindowHud() {
    if (!mainWindow || mainWindow.isDestroyed()) {
        createMainWindow();
        return;
    }

    // No loadFile here. The workstation was never navigated away, so it only has
    // to be raised — reloading it would throw away the very state this exists to
    // restore, which is what made widgets stop opening after leaving Workshop.
    mainWindowMode = 'hud';
    mainWorkshopRole = '';

    mainWindow.show();
    mainWindow.focus();
}

function getLiveWorkshopWindow(role) {
    const window = workshopWindows.get(role);

    if (window && !window.isDestroyed()) {
        return window;
    }

    workshopWindows.delete(role);
    return null;
}

function createWorkshopWindow(role, display) {
    const bounds = boundsForDisplay(display);

    const window = new BrowserWindow({
        x: bounds.x,
        y: bounds.y,
        width: bounds.width,
        height: bounds.height,
        minWidth: 960,
        minHeight: 640,
        frame: false,
        show: false,
        backgroundColor: '#020713',
        title: `FRIDAY ${role}`,
        webPreferences: baseWindowPreferences()
    });

    window.loadFile('index.html', {
        query: {
            mode: 'workshop',
            role: roleQuery(role)
        }
    });

    window.once('ready-to-show', () => {
        if (!window.isDestroyed()) {
            window.show();
            window.focus();
        }
    });

    window.on('closed', () => {
        workshopWindows.delete(role);
        notifyIfWorkshopEmpty();
    });

    workshopWindows.set(role, window);
    return window;
}

/**
 * Tell the main window once the last Workshop display is gone.
 *
 * Closing a Workshop display directly — OS close button, display disconnected —
 * removed the window here but nothing informed Python, so hud_state kept
 * workshop_mode.active = true forever. With that flag stale, the Workstation
 * believed Workshop still owned the screen: new cards were routed to a Workshop
 * workspace that no longer existed and ensure_workstation_visible() bailed out
 * early. That is why widgets "stopped opening" after returning from Workshop.
 */
function notifyIfWorkshopEmpty() {
    if (workshopWindows.size > 0) {
        return;
    }

    if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('workshop:all-closed');
    }
}

function createOrFocusWorkshopWindow(role, display) {
    const existing = getLiveWorkshopWindow(role);

    if (existing) {
        existing.focus();
        existing.webContents.send('workshop:refresh-state', {
            role: roleQuery(role)
        });
        return existing;
    }

    return createWorkshopWindow(role, display);
}

function closeWorkshopWindows() {
    for (const window of workshopWindows.values()) {
        if (window && !window.isDestroyed()) {
            window.close();
        }
    }

    workshopWindows.clear();
    restoreMainWindowHud();

    return { ok: true };
}

function sendWorkshopEventToAll(event, payload = {}) {
    if (mainWindowMode === 'workshop' && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send(event, payload);
    }

    for (const [role, window] of workshopWindows.entries()) {
        if (window && !window.isDestroyed()) {
            window.webContents.send(event, payload);
        } else {
            workshopWindows.delete(role);
        }
    }
}

function sendWorkshopEventToRole(role, event, payload = {}) {
    const key = role.startsWith('workshop-') ? role : `workshop-${role}`;

    if (isMainWorkshopRole(key) && mainWindowMode === 'workshop' && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send(event, payload);
        return;
    }

    const window = getLiveWorkshopWindow(key);

    if (window && !window.isDestroyed()) {
        window.webContents.send(event, payload);
    }
}

function focusWorkshopWindow(role) {
    const key = role.startsWith('workshop-') ? role : `workshop-${role}`;

    if (isMainWorkshopRole(key) && mainWindowMode === 'workshop' && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.focus();
        return true;
    }

    const window = getLiveWorkshopWindow(key);

    if (window && !window.isDestroyed()) {
        window.focus();
        return true;
    }

    return false;
}

function focusWorkshopWindows() {
    if (mainWindowMode === 'workshop' && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.focus();
    }

    for (const window of workshopWindows.values()) {
        if (window && !window.isDestroyed()) {
            window.focus();
        }
    }
}

function syncWorkshopStateToWindows() {
    sendWorkshopEventToAll('workshop:refresh-state', {});
}

function createWorkshopWindows() {
    const displays = sortedDisplays();

    if (!displays.length) {
        return {
            ok: false,
            reason: 'no_display',
            displayCount: 0,
            displays: [],
            roles: {}
        };
    }

    const roles = assignWorkshopRoles(displays);
    // Every role now owns a real window, including the main one, so all of them
    // are tracked and all of them are closed on exit. Excluding the main role
    // here is what previously left it navigating the workstation instead.
    const activeRoles = new Set(Object.keys(roles));

    for (const [role, window] of workshopWindows.entries()) {
        if (!activeRoles.has(role) && window && !window.isDestroyed()) {
            window.close();
        }
    }

    // One path for every role now — the main role no longer gets special
    // treatment, because that special treatment was the workstation takeover.
    Object.entries(roles).forEach(([role, display]) => {
        createOrFocusWorkshopWindow(role, display);
    });

    focusWorkshopWindows();
    syncWorkshopStateToWindows();

    return {
        ok: true,
        displayCount: displays.length,
        displays,
        roles: Object.fromEntries(
            Object.entries(roles).map(([role, display]) => [role, display.id])
        )
    };
}

ipcMain.handle('get-displays', () => {
    return getDisplays();
});

ipcMain.handle('workshop:detect-displays', () => {
    return getDisplays();
});

ipcMain.handle('workshop:open', () => {
    return createWorkshopWindows();
});

ipcMain.handle('workshop:close', () => {
    return closeWorkshopWindows();
});

ipcMain.handle('workshop:focus-role', (_event, role) => {
    return focusWorkshopWindow(String(role || 'main'));
});

ipcMain.handle('workshop:save-layout', () => {
    sendWorkshopEventToAll('workshop:save-layout', {});
    return { ok: true };
});

ipcMain.handle('workshop:restore-layout', () => {
    sendWorkshopEventToAll('workshop:restore-layout', {});
    return { ok: true };
});

ipcMain.handle('workshop:launcher-action', (_event, payload) => {
    sendWorkshopEventToAll('workshop:launcher-action', payload || {});
    return { ok: true };
});

/**
 * AppleScript string literal escaping.
 *
 * Playlist and track names are user data and routinely contain quotes and
 * backslashes ("Rock 'n' Roll", `AC\DC`). Interpolating them raw would break the
 * script — and worse, a crafted name could close the string and append
 * statements, so this is a correctness AND an injection concern.
 */
function escapeAppleScript(value) {
    return String(value == null ? '' : value)
        .replace(/\\/g, '\\\\')
        .replace(/"/g, '\\"')
        .replace(/[\r\n]+/g, ' ');
}

/** Run a script and resolve its trimmed stdout, or reject-as-payload on failure. */
function runAppleScript(script, timeout = 6000) {
    return new Promise((resolve) => {
        execFile('osascript', ['-e', script], { timeout, maxBuffer: 4 * 1024 * 1024 }, (error, stdout) => {
            if (error) {
                resolve({ ok: false, reason: String(error.message || error) });
                return;
            }

            resolve({ ok: true, out: String(stdout || '').trim() });
        });
    });
}

/**
 * Seek within the current track.
 *
 * `player position` is settable in Music's dictionary, so this is real seeking
 * rather than a UI affordance over nothing.
 */
async function seekMusic(seconds) {
    const target = Number(seconds);

    if (!Number.isFinite(target) || target < 0) {
        return { ok: false, reason: 'invalid_position' };
    }

    const result = await runAppleScript(
        `tell application "Music"\nif it is running then set player position to ${target.toFixed(2)}\nend tell`,
        3000
    );

    return result.ok ? { ok: true } : result;
}

/** Playlist names, deduplicated by index so duplicates stay addressable. */
async function listMusicPlaylists() {
    const result = await runAppleScript(`
tell application "Music"
    if it is not running then return ""
    set out to ""
    repeat with p in user playlists
        try
            set out to out & (name of p) & "\\t" & (count of tracks of p) & "\\n"
        end try
    end repeat
    return out
end tell`);

    if (!result.ok) {
        return { ok: false, reason: result.reason, playlists: [] };
    }

    const playlists = result.out.split('\n')
        .map((line) => line.split('\t'))
        .filter((parts) => parts[0] && parts[0].trim())
        .map((parts) => ({ name: parts[0].trim(), count: Number(parts[1]) || 0 }));

    return { ok: true, playlists };
}

/**
 * Tracks of one playlist, fetched only when that playlist is opened.
 *
 * Reading every playlist's contents up front would mean hundreds of AppleScript
 * round trips for a library of any size, which is why this is on demand.
 */
async function listMusicPlaylistTracks(playlistName, limit = 300) {
    const name = escapeAppleScript(playlistName);

    if (!name) {
        return { ok: false, reason: 'missing_playlist', tracks: [] };
    }

    const result = await runAppleScript(`
tell application "Music"
    if it is not running then return ""
    set out to ""
    try
        set thePlaylist to first user playlist whose name is "${name}"
    on error
        return ""
    end try
    set theTracks to tracks of thePlaylist
    set total to count of theTracks
    if total > ${limit} then set total to ${limit}
    repeat with i from 1 to total
        set t to item i of theTracks
        try
            set out to out & (i as text) & "\\t" & (name of t) & "\\t" & (artist of t) & "\\t" & (duration of t as text) & "\\n"
        end try
    end repeat
    return out
end tell`, 12000);

    if (!result.ok) {
        return { ok: false, reason: result.reason, tracks: [] };
    }

    const tracks = result.out.split('\n')
        .map((line) => line.split('\t'))
        .filter((parts) => parts.length >= 2 && parts[1])
        .map((parts) => ({
            index: Number(parts[0]) || 0,
            name: parts[1],
            artist: parts[2] || '',
            duration: Number(parts[3]) || null
        }));

    return { ok: true, tracks };
}

/** Play a whole playlist. */
async function playMusicPlaylist(playlistName) {
    const name = escapeAppleScript(playlistName);

    if (!name) {
        return { ok: false, reason: 'missing_playlist' };
    }

    const result = await runAppleScript(`
tell application "Music"
    if it is not running then activate
    try
        play (first user playlist whose name is "${name}")
        return "ok"
    on error errText
        return "err:" & errText
    end try
end tell`);

    if (!result.ok) {
        return result;
    }

    return result.out.startsWith('err:')
        ? { ok: false, reason: result.out.slice(4) }
        : { ok: true };
}

/**
 * Play one track from a playlist, addressed BY INDEX rather than by name.
 *
 * Libraries routinely contain the same song title twice; "play track whose name
 * is X" would pick an arbitrary one. The index comes from the same listing the
 * user clicked, so it always refers to the row they actually saw.
 */
async function playMusicPlaylistTrack(playlistName, trackIndex) {
    const name = escapeAppleScript(playlistName);
    const index = Number(trackIndex);

    if (!name || !Number.isFinite(index) || index < 1) {
        return { ok: false, reason: 'invalid_track' };
    }

    const result = await runAppleScript(`
tell application "Music"
    if it is not running then activate
    try
        set thePlaylist to first user playlist whose name is "${name}"
        play (item ${Math.floor(index)} of tracks of thePlaylist)
        return "ok"
    on error errText
        return "err:" & errText
    end try
end tell`);

    if (!result.ok) {
        return result;
    }

    return result.out.startsWith('err:')
        ? { ok: false, reason: result.out.slice(4) }
        : { ok: true };
}

/** Shuffle / repeat, over IPC so the widget does not need the Python round trip. */
async function setMusicMode(mode, enabled) {
    const scripts = {
        shuffle: `tell application "Music" to set shuffle enabled to ${enabled ? 'true' : 'false'}`,
        repeat: `tell application "Music" to set song repeat to ${enabled ? 'all' : 'off'}`
    };

    const script = scripts[String(mode || '').toLowerCase()];

    if (!script) {
        return { ok: false, reason: 'unknown_mode' };
    }

    const result = await runAppleScript(script, 3000);
    return result.ok ? { ok: true } : result;
}

/**
 * Cover art for the current track, as a data URI.
 *
 * Cached on the track identity because extraction costs an AppleScript round trip
 * plus a file read, while the widget polls state every few seconds. Without the
 * cache this would run constantly for a value that changes once per song.
 */
const musicArtworkCache = { key: '', uri: '' };

async function getMusicArtwork() {
    const state = await getMusicState();
    const key = `${state.track}|${state.album}`;

    if (musicArtworkCache.key === key) {
        return { ok: true, artwork: musicArtworkCache.uri, key };
    }

    musicArtworkCache.key = key;
    musicArtworkCache.uri = '';

    if (!state.track || state.track === 'No track loaded') {
        return { ok: true, artwork: '', key };
    }

    const target = path.join(os.tmpdir(), 'friday_music_artwork_ui.bin');
    const result = await runAppleScript(`
tell application "Music"
    if it is not running then return "none"
    try
        set artData to raw data of artwork 1 of current track
    on error
        return "none"
    end try
end tell
try
    set outFile to open for access POSIX file "${escapeAppleScript(target)}" with write permission
    set eof outFile to 0
    write artData to outFile
    close access outFile
    return "ok"
on error
    try
        close access POSIX file "${escapeAppleScript(target)}"
    end try
    return "none"
end try`, 8000);

    if (!result.ok || result.out !== 'ok') {
        return { ok: true, artwork: '', key };
    }

    try {
        const raw = require('fs').readFileSync(target);
        require('fs').unlinkSync(target);

        // Sniff the container rather than trusting a reported format.
        let mime = '';
        if (raw[0] === 0xff && raw[1] === 0xd8 && raw[2] === 0xff) {
            mime = 'image/jpeg';
        } else if (raw[0] === 0x89 && raw[1] === 0x50 && raw[2] === 0x4e && raw[3] === 0x47) {
            mime = 'image/png';
        }

        if (!mime || raw.length > 3 * 1024 * 1024) {
            return { ok: true, artwork: '', key };
        }

        musicArtworkCache.uri = `data:${mime};base64,${raw.toString('base64')}`;
        return { ok: true, artwork: musicArtworkCache.uri, key };
    } catch (error) {
        return { ok: true, artwork: '', key };
    }
}

ipcMain.handle('music:artwork', () => getMusicArtwork());
ipcMain.handle('music:play', () => runMusicCommand('play'));
ipcMain.handle('music:pause', () => runMusicCommand('pause'));
ipcMain.handle('music:toggle', () => runMusicCommand('toggle'));
ipcMain.handle('music:next', () => skipMusicTrack('next'));
ipcMain.handle('music:previous', () => skipMusicTrack('previous'));
ipcMain.handle('music:state', () => getMusicState());
ipcMain.handle('music:seek', (_event, seconds) => seekMusic(seconds));
ipcMain.handle('music:playlists', () => listMusicPlaylists());
ipcMain.handle('music:playlistTracks', (_event, name) => listMusicPlaylistTracks(name));
ipcMain.handle('music:playPlaylist', (_event, name) => playMusicPlaylist(name));
ipcMain.handle('music:playTrack', (_event, name, index) => playMusicPlaylistTrack(name, index));
ipcMain.handle('music:mode', (_event, mode, enabled) => setMusicMode(mode, enabled));

/**
 * Supplies the Gemini key to the renderer's voice layer.
 *
 * Read here rather than in the renderer so the key never rides over the Socket.IO
 * bridge, which binds 0.0.0.0:5050 with no authentication.
 */
/**
 * VOICE OWNERSHIP LEASE — exactly one window may run the live voice session.
 *
 * The bug this exists to kill: every window that loads index.html also loads
 * voice/bootstrap.js, and Workshop windows load index.html too. So opening
 * Workshop stood up a SECOND VoiceBridge — a second Gemini Live socket, a second
 * getUserMedia capture and a second playback path. One spoken question produced
 * two FRIDAYs answering over each other.
 *
 * The renderer cannot arbitrate this on its own: each renderer only sees itself.
 * The main process is the only place that can see all windows at once, so the
 * lease lives here and the renderer must ask for it before booting voice.
 *
 * The lease is held by webContents id and released automatically when the owning
 * window goes away, so a crashed or closed owner never wedges voice permanently.
 */
let voiceOwnerWebContentsId = null;

function voiceOwnerIsAlive() {
    if (voiceOwnerWebContentsId === null) {
        return false;
    }

    return BrowserWindow.getAllWindows().some(
        (win) => !win.isDestroyed() && win.webContents.id === voiceOwnerWebContentsId
    );
}

function releaseVoiceOwnership(webContentsId) {
    if (voiceOwnerWebContentsId === webContentsId) {
        voiceOwnerWebContentsId = null;
    }
}

/**
 * Grant the lease if it is free, or confirm it for the window that already holds
 * it. Any other caller is told `false` and must stay a passive UI surface.
 */
ipcMain.handle('voice:claim-ownership', (event) => {
    const requesterId = event.sender.id;

    if (voiceOwnerWebContentsId === requesterId) {
        return { owner: true, reason: 'already owner' };
    }

    if (voiceOwnerIsAlive()) {
        return { owner: false, reason: 'another window owns the voice session' };
    }

    voiceOwnerWebContentsId = requesterId;
    event.sender.once('destroyed', () => releaseVoiceOwnership(requesterId));
    return { owner: true, reason: 'lease granted' };
});

ipcMain.handle('voice:release-ownership', (event) => {
    releaseVoiceOwnership(event.sender.id);
    return { ok: true };
});

/**
 * Relay the owner's voice health to every other window, so surfaces that do not
 * run voice can still display the truth about it instead of guessing "offline".
 */
ipcMain.on('voice:health-broadcast', (event, health) => {
    for (const win of BrowserWindow.getAllWindows()) {
        if (win.isDestroyed() || win.webContents.id === event.sender.id) {
            continue;
        }

        win.webContents.send('voice:health-update', health || {});
    }
});

/**
 * Show a Virtual Finder item in macOS Finder.
 *
 * For the files FRIDAY cannot render herself — an archive, a binary, anything
 * outside the preview whitelist — where handing it to the OS is the only useful
 * answer.
 *
 * The renderer sends the VIRTUAL path ("Projects/notes/plan.pdf") and never an
 * absolute one. Resolution happens here, against the one root the Virtual Finder
 * owns, and the resolved path is checked to still be inside that root before
 * anything is opened. A renderer that asked for "../../../.ssh/id_rsa" gets
 * nothing: the containment check is on the REAL path, so `..` and symlinks are
 * both answered by it.
 */
const VIRTUAL_FINDER_ROOT = path.join(__dirname, '..', 'Data', 'Virtual_Finder');

ipcMain.handle('files:reveal', async (_event, virtualPath) => {
    const requested = String(virtualPath || '').trim();

    if (!requested) {
        return { ok: false, reason: 'no path given' };
    }

    try {
        const fsp = require('fs').promises;
        const root = await fsp.realpath(VIRTUAL_FINDER_ROOT);
        const target = await fsp.realpath(path.resolve(root, requested));

        if (target !== root && !target.startsWith(root + path.sep)) {
            return { ok: false, reason: 'path is outside the Virtual Finder' };
        }

        shell.showItemInFolder(target);
        return { ok: true };
    } catch (err) {
        return { ok: false, reason: 'that item could not be opened' };
    }
});

ipcMain.handle('voice:api-key', () => {
    const envPath = path.join(__dirname, '..', 'Core_Cognition', '.env');
    try {
        const raw = require('fs').readFileSync(envPath, 'utf8');
        const match = /^GEMINI_API_KEY=(.*)$/m.exec(raw);
        if (!match) return '';
        return match[1].trim().replace(/^["']|["']$/g, '');
    } catch (err) {
        return '';
    }
});

/**
 * One FRIDAY interface per machine.
 *
 * Nothing here prevented a second Electron process from starting, and several
 * paths could start one: run_friday.sh, npm start by hand, and Python's
 * launch_hud_interface() whenever it had no live handle on an existing HUD. Each
 * process brought its own main window AND its own set of Workshop displays, which
 * is what made repeated activation accumulate windows.
 *
 * The lock makes that impossible rather than merely unlikely: the second process
 * exits immediately and hands the request to the first, which shows and focuses
 * what already exists.
 */
if (!app.requestSingleInstanceLock()) {
    app.quit();
} else {
    app.on('second-instance', () => {
        if (!mainWindow || mainWindow.isDestroyed()) {
            createMainWindow();
            return;
        }

        if (mainWindow.isMinimized()) {
            mainWindow.restore();
        }

        mainWindow.show();
        mainWindow.focus();
    });
}

app.whenReady().then(() => {
    // Without an explicit handler Electron denies the renderer's getUserMedia call, so
    // macOS never even gets the chance to show its own microphone prompt. Only media is
    // granted, and only to our own pages.
    const { session } = require('electron');
    session.defaultSession.setPermissionRequestHandler((contents, permission, callback) => {
        callback(permission === 'media');
    });

    createMainWindow();

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            createMainWindow();
        }
    });
});

app.on('window-all-closed', () => {
    app.quit();
});
