/**
 * Boots the live voice layer once the HUD socket is up.
 *
 * Loaded as a separate script after renderer.js so it can reuse the existing `socket`
 * global rather than opening a second connection to 5050. Nothing else in renderer.js
 * is modified — the voice layer is additive.
 *
 * Set FRIDAY_LIVE_VOICE=0 to fall back to the legacy Python audio_engine path.
 */

(function () {
    'use strict';

    const DISABLED = String((process.env.FRIDAY_LIVE_VOICE ?? process.env.JARVIS_LIVE_VOICE) || '1') === '0';

    if (DISABLED) {
        console.log('[voice] live voice disabled (FRIDAY_LIVE_VOICE=0) — legacy path in use');
        return;
    }

    if (typeof socket === 'undefined') {
        console.error('[voice] socket not found; renderer.js must load first');
        return;
    }

    // ── ONE FRIDAY ────────────────────────────────────────────────────────────
    // Workshop windows load this same index.html, so without a guard every extra
    // surface booted its own VoiceBridge: another Gemini Live session, another
    // microphone capture, another playback path. Asking one question got two
    // FRIDAYs answering simultaneously.
    //
    // Workshop is a UI surface, not another assistant. Only the main HUD window
    // may own voice, and the main process arbitrates that with a lease so the
    // invariant is enforced rather than merely conventional.
    const interfaceMode = new URLSearchParams(window.location.search || '').get('mode') || 'hud';
    const isSecondarySurface = interfaceMode !== 'hud';

    function electronIpc() {
        try {
            return require('electron').ipcRenderer;
        } catch (_) {
            return null;
        }
    }

    /**
     * Reflect the owner window's voice state without running a session here, so a
     * passive surface reports the truth instead of a local guess of "offline".
     */
    function installPassiveVoiceSurface(reason) {
        const ipc = electronIpc();

        window.__fridayVoice = null;
        window.__fridayVoiceHealth = { ok: false, listening: false, reason, passive: true };
        window.__ensureVoiceReady = async () => window.__fridayVoiceHealth;
        window.__fridayVoiceStatus = () => window.__fridayVoiceHealth;
        document.body.dataset.voice = 'passive';

        if (ipc) {
            ipc.on('voice:health-update', (_event, health) => {
                const safe = health && typeof health === 'object' ? health : {};
                window.__fridayVoiceHealth = { ...safe, passive: true };
                document.body.dataset.voice = safe.ok ? 'listening' : 'offline';
            });
        }

        console.log('[voice] passive surface (' + reason + ') — no session, no microphone');
    }

    if (isSecondarySurface) {
        installPassiveVoiceSurface('workshop surface; main window owns voice');
        return;
    }

    let bridge = null;
    let ownsVoice = false;

    // ── SINGLE FLIGHT ─────────────────────────────────────────────────────────
    // Both of these hold the ONE in-flight attempt, and both are assigned
    // synchronously before anything is awaited.
    //
    // This is what a boolean `starting` flag could not do. `boot()` used to read
    // its guard, then `await claimVoiceOwnership()`, and only then set the flag —
    // so two callers arriving in the same tick both saw `starting === false`,
    // both suspended on the await, and both went on to build a bridge. That is
    // two VoiceAudioEngines on two microphones and two Gemini Live sessions on
    // one API key, and it is the bug this file exists to prevent.
    //
    // There are four callers and at least two of them routinely land together on
    // a cold start: DOM ready, the `socket.connected` check below, the socket
    // `connect` event, and the 5-second watchdog.
    let bootPromise = null;
    let ensurePromise = null;

    /**
     * Ask the main process for the voice lease. Everything downstream refuses to
     * touch the microphone until this returns true.
     */
    async function claimVoiceOwnership() {
        if (ownsVoice) {
            return true;
        }

        const ipc = electronIpc();

        if (!ipc) {
            // Plain browser (test harness): nothing else can be competing.
            ownsVoice = true;
            return true;
        }

        try {
            const result = await ipc.invoke('voice:claim-ownership');
            ownsVoice = Boolean(result && result.owner);

            if (!ownsVoice) {
                console.warn('[voice] ownership refused: ' + ((result && result.reason) || 'unknown'));
            }

            return ownsVoice;
        } catch (err) {
            console.error('[voice] ownership claim failed:', err);
            return false;
        }
    }

    // ── Startup instrumentation ───────────────────────────────────────────────
    // Written to a FILE as well as the console and the socket, because the two
    // failure modes we care about — a renderer that never reaches the socket, and
    // a microphone denied before anything is emitted — are exactly the cases where
    // console and socket tell us nothing.
    const STAGE_LOG = '/tmp/friday-voice-stages.log';
    let stageSeq = 0;

    function stage(name, status, detail) {
        stageSeq += 1;
        const line = `${new Date().toISOString()} [${String(stageSeq).padStart(2, '0')}] `
            + `${status.padEnd(7)} ${name}${detail ? ' :: ' + detail : ''}`;

        console.log('[voice-stage] ' + line);

        try {
            require('fs').appendFileSync(STAGE_LOG, line + '\n');
        } catch (_) { /* logging must never break startup */ }

        try {
            socket.emit('voice_stage', { seq: stageSeq, name, status, detail: detail || '' });
        } catch (_) { /* socket may not be up; the file still has it */ }
    }

    window.__voiceStage = stage;
    stage('renderer_script_loaded', 'PASS', 'bootstrap.js evaluated');

    /**
     * Start the voice layer at most once, however many callers ask at once.
     *
     * The guard is set BEFORE the first await and concurrent callers are handed
     * the same promise, so "has a boot already begun?" cannot be answered wrongly
     * by a caller that arrived while the first one was suspended.
     */
    function boot() {
        if (bridge) return Promise.resolve();
        if (bootPromise) return bootPromise;

        bootPromise = runBoot().finally(() => {
            bootPromise = null;
        });

        return bootPromise;
    }

    async function runBoot() {
        // The lease is the gate. Without it this window must never open a Gemini
        // Live session or request a microphone. It keeps Workshop out; it cannot
        // help with two claims from THIS window, which both return "already
        // owner" — that is what the single-flight guard above is for.
        if (!(await claimVoiceOwnership())) {
            stage('voice_ownership', 'SKIPPED', 'another window owns the voice session');
            installPassiveVoiceSurface('voice owned by another window');
            return;
        }

        stage('boot_entered', 'PASS');

        try {
            const { ipcRenderer } = require('electron');
            const apiKey = await ipcRenderer.invoke('voice:api-key');

            if (!apiKey) {
                stage('api_key', 'FAIL', 'no GEMINI_API_KEY in Core_Cognition/.env');
                stage('bridge_created', 'SKIPPED', 'no api key');
                stage('microphone', 'SKIPPED', 'no api key');
                stage('live_session', 'SKIPPED', 'no api key');
                console.error('[voice] no GEMINI_API_KEY in Core_Cognition/.env — voice layer idle');
                socket.emit('voice_diagnostic', { message: 'no API key; live voice not started' });
                return;
            }

            stage('api_key', 'PASS', `${apiKey.length} chars`);

            const { VoiceBridge } = require('./voice/voice-bridge');
            bridge = new VoiceBridge(socket, apiKey);
            window.__fridayVoice = bridge;
            stage('bridge_created', 'PASS');

            // Ask Python for the action vocabulary before connecting, so the model's
            // tool enum matches perform_direct_action's allowlist exactly.
            socket.emit('voice_request_manifest', {});

            await bridge.start();

            const h = bridge.health();
            stage('bridge_started', h.ok ? 'PASS' : 'FAIL',
                  `capturing=${h.capturing} sessionReady=${h.sessionReady} ${h.reason}`);
            console.log('[voice] live voice layer running');
        } catch (err) {
            // The bridge is DISCARDED on failure, and this is the whole point.
            //
            // `bridge` is assigned before start(), so a start() that threw — a
            // microphone prompt answered late, a suspended AudioContext, a worklet
            // that lost a load race over file:// — would otherwise leave a
            // non-null half-built bridge that blocks EVERY later attempt for the
            // life of the page. Voice would be dead for the session while
            // window.__fridayVoice existed and the interface reported itself
            // online. Tearing the engine down also releases the microphone, so
            // the retry is not fighting its own abandoned capture for the device.
            try {
                if (bridge && bridge.engine) await bridge.engine.stop();
            } catch (_) { /* nothing to salvage */ }

            bridge = null;
            window.__fridayVoice = null;

            stage('boot', 'FAIL', (err && (err.name + ': ' + err.message)) || String(err));
            console.error('[voice] failed to start:', err);
            socket.emit('voice_diagnostic', {
                message: 'start failed: ' + (err && err.message ? err.message : String(err))
            });
        }

        reportVoiceStatus();
    }

    /**
     * THE authoritative "make voice work" path. Everything that wants a live
     * microphone calls this instead of implementing its own startup logic.
     *
     * Idempotent by construction: a healthy session and a live microphone are
     * reused untouched, so calling it five times produces one session and one
     * capture path.
     */
    function ensureVoiceReady() {
        // Single-flight for the same reason boot() is: the watchdog, the socket
        // `connect` handler and DOM ready all call this, and two overlapping
        // repairs on one session are how a reconnect turns into a reconnect loop.
        if (ensurePromise) return ensurePromise;

        ensurePromise = runEnsureVoiceReady().finally(() => {
            ensurePromise = null;
        });

        return ensurePromise;
    }

    async function runEnsureVoiceReady() {
        if (!bridge) {
            await boot();
        } else {
            try {
                await bridge.ensureReady();
            } catch (err) {
                console.error('[voice] ensureReady failed:', err);
            }
        }

        return reportVoiceStatus();
    }

    /**
     * Publish what is ACTUALLY true, so the interface can say "voice offline"
     * instead of quietly implying it is listening.
     */
    function reportVoiceStatus() {
        const health = bridge && bridge.health
            ? bridge.health()
            : { ok: false, listening: false, reason: 'voice layer not started', sessionState: 'none' };

        window.__fridayVoiceHealth = health;
        socket.emit('voice_health', health);
        document.body.dataset.voice = health.ok ? 'listening' : 'offline';

        // Passive surfaces (Workshop) render voice state but never compute it.
        const ipc = electronIpc();

        if (ipc) {
            try {
                ipc.send('voice:health-broadcast', health);
            } catch (_) { /* relay is best-effort */ }
        }

        return health;
    }

    window.__ensureVoiceReady = ensureVoiceReady;
    window.__fridayVoiceStatus = reportVoiceStatus;

    socket.on('voice_tool_manifest', (payload) => {
        const actions = (payload && payload.actions) || [];
        if (bridge) {
            bridge.setToolManifest(actions);
        } else {
            // Manifest arrived before boot finished; stash and apply on start.
            socket.once('voice_ready', () => bridge && bridge.setToolManifest(actions));
            pendingManifest = actions;
        }
    });

    let pendingManifest = null;
    let pendingMemoryBlock = null;

    // Core identity memories, sent alongside the tool manifest. Stashed the same
    // way, because it can arrive before the bridge exists.
    socket.on('voice_memory_context', (payload) => {
        const block = (payload && payload.block) || '';

        if (bridge) {
            bridge.setMemoryContext(block);
        } else {
            pendingMemoryBlock = block;
        }
    });

    socket.on('voice_tool_result', (payload) => {
        if (bridge) bridge.onToolResult(payload || {});
    });

    // Data reads come back on their own channel but resolve the same pending call.
    socket.on('voice_data_result', (payload) => {
        if (bridge) bridge.onToolResult(payload || {});
    });

    // Presence gate — Python already tracks Mac idle time in presence_manager.py.
    socket.on('voice_suspend', () => bridge && bridge.suspend());
    socket.on('voice_resume', () => bridge && bridge.resume());
    socket.on('voice_set_muted', (payload) => bridge && bridge.setMuted(!!(payload && payload.muted)));

    // Typed input in the HUD should reach the same conversation as speech.
    socket.on('voice_speak_text', (payload) => {
        if (bridge && payload && payload.text) bridge.sendText(payload.text);
    });

    // Completed actions, alerts and greetings raised by the Python side. Tagged as
    // system events rather than pushed in as speech, so FRIDAY reports them in her
    // own words instead of replying to them as if Boss had spoken.
    socket.on('voice_system_event', (payload) => {
        if (!bridge || !payload || !payload.text) return;
        bridge.sendSystemEvent(payload.text, payload.mode);
    });

    // Turning on the interface must also turn on the ears. Python raises this
    // from ensure_workstation_visible(), so waking the desktop and making the
    // microphone live are one action rather than two that can disagree.
    socket.on('voice_ensure_ready', () => {
        ensureVoiceReady();
    });

    // A watchdog, because the ways audio dies are mostly silent: the OS reclaims
    // the input, the device changes, a context gets suspended while hidden.
    // Nothing here reconnects a healthy session — ensureReady is a no-op when
    // everything is already running.
    window.setInterval(() => {
        // NOTE the missing early return.
        //
        // This used to start `if (!bridge) return;` — it gave up in exactly the
        // state it existed to recover from. A boot that never ran, or one that
        // failed and discarded its bridge, leaves `bridge` null, so the watchdog
        // bailed out and voice stayed dead for the rest of the session with no
        // retry anywhere. That is why a cold launch could come up mute and stay
        // mute until something manually re-triggered initialisation.
        if (!bridge) {
            ensureVoiceReady();
            return;
        }

        const health = bridge.health();

        if (!health.ok && !health.suspended && health.sessionState !== 'fatal') {
            console.warn('[voice] unhealthy (' + health.reason + '), repairing');
            ensureVoiceReady();
        } else {
            reportVoiceStatus();
        }
    }, 5000);

    // Voice does not depend on the brain, so its startup must not either.
    //
    // Initialisation used to be triggered ONLY by socket connect. The API key
    // comes over IPC and the microphone is local; the socket carries diagnostics
    // and the tool manifest, both of which can arrive late. Tying boot to the
    // socket meant a brain that was slow, restarting, or briefly unreachable left
    // FRIDAY visibly up and completely deaf.
    //
    // Everything below funnels into ensureVoiceReady(), which is the single
    // initialisation path: it boots when absent, repairs when broken, and is a
    // no-op when already healthy.
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => ensureVoiceReady());
    } else {
        ensureVoiceReady();
    }

    socket.on('connect', () => {
        // Voice now starts on DOM ready, which is BEFORE this fires. Socket.IO
        // drops emits made while disconnected, so every stage line, diagnostic and
        // health report from that early boot was lost — the microphone was live
        // and the brain had no idea. Re-announce as soon as there is a socket.
        stage('socket_connected', 'PASS', 'republishing voice status');
        reportVoiceStatus();
        socket.emit('voice_request_manifest', {});

        ensureVoiceReady().then(() => {
            if (pendingManifest && bridge) {
                bridge.setToolManifest(pendingManifest);
                pendingManifest = null;
            }

            if (pendingMemoryBlock !== null && bridge) {
                bridge.setMemoryContext(pendingMemoryBlock);
                pendingMemoryBlock = null;
            }
        });
    });

    // Covers a socket that was already connected before this script ran. Routed
    // through the same single path as every other trigger.
    if (socket.connected) ensureVoiceReady();
})();
