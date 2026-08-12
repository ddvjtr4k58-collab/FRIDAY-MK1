/**
 * Voice startup test suite.
 *
 * Offline and deterministic: no Gemini key, no network, no microphone, no
 * Electron. bootstrap.js is evaluated inside a `vm` context with stubs standing
 * in for the renderer globals, and LiveSession is driven with a fake transport.
 *
 * Run from FRIDAY_OS:
 *
 *     node Tests/test_voice_startup.js
 *
 * What it exists to catch — both are silent failures in production, which is
 * exactly why they need a test:
 *
 *   1. THE BOOT RACE. `boot()` used to read its `starting` guard, then await the
 *      ownership claim, and only then set the guard. Two callers arriving in the
 *      same tick both passed the check and both built a VoiceBridge: two
 *      microphones and two Gemini Live sessions on one API key. Four callers
 *      routinely land together on a cold start, and the second Live session is
 *      what left the first one's connect hanging forever.
 *
 *   2. THE PERMANENT HANG. ai.live.connect() resolves when the socket opens and
 *      rejects when it is refused, but settles neither way when a socket is
 *      accepted and never upgraded. With no timeout, `connecting` stayed true and
 *      the state stayed 'connecting' for the life of the process, and every
 *      repair path was a no-op because each could see an attempt still in flight.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HERE = __dirname;
const INTERFACE_DIR = path.join(HERE, '..', 'Visual_Interface');
const BOOTSTRAP = path.join(INTERFACE_DIR, 'voice', 'bootstrap.js');

const PASSED = [];
const FAILED = [];

function check(name, condition, detail) {
    if (condition) {
        PASSED.push(name);
        console.log('[PASS] ' + name);
    } else {
        FAILED.push(name);
        console.log('[FAIL] ' + name + (detail ? ' — ' + detail : ''));
    }
}

const repr = (value) => JSON.stringify(value);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ==========================================================
// RENDERER HARNESS
// ==========================================================
/**
 * Evaluate bootstrap.js against stubbed renderer globals.
 *
 * `require` is supplied rather than inherited because in Electron the script is
 * loaded from index.html, so its relative requires resolve against
 * Visual_Interface/ and not against voice/. The stub reproduces that.
 */
function loadBootstrap(options) {
    const settings = options || {};
    const mode = settings.mode || 'hud';

    const state = {
        bridges: [],
        engineStarts: 0,
        sessionConnects: 0,
        ownershipClaims: 0,
        bootAttempts: 0,
        stages: [],
        socketEvents: {},
        intervals: []
    };

    class FakeVoiceBridge {
        constructor() {
            state.bridges.push(this);
            this.engine = { stop: async () => {} };
            this.ready = false;
        }

        async start() {
            state.engineStarts += 1;
            // A real start awaits getUserMedia, a worklet load and a WebSocket
            // handshake. The window between the guard and this resolving is the
            // whole bug, so it must be a real one.
            await wait(20);
            state.sessionConnects += 1;
            this.ready = true;
        }

        async ensureReady() {
            return this.health();
        }

        health() {
            return {
                ok: this.ready,
                listening: this.ready,
                capturing: this.ready,
                sessionReady: this.ready,
                sessionState: this.ready ? 'active' : 'connecting',
                reason: this.ready ? '' : 'live session connecting'
            };
        }

        setToolManifest() {}
        setMemoryContext() {}
    }

    const socket = {
        connected: Boolean(settings.socketConnected),
        emit(name, payload) {
            if (name === 'voice_stage') state.stages.push(payload);
        },
        on(name, handler) {
            (state.socketEvents[name] = state.socketEvents[name] || []).push(handler);
        },
        once(name, handler) {
            this.on(name, handler);
        },
        fire(name, payload) {
            (state.socketEvents[name] || []).forEach((handler) => handler(payload));
        }
    };

    const ipcRenderer = {
        async invoke(channel) {
            if (channel === 'voice:claim-ownership') {
                state.ownershipClaims += 1;
                // The await that opened the race. One tick is enough.
                await wait(0);
                return { owner: true, reason: 'lease granted' };
            }

            if (channel === 'voice:api-key') {
                // Fetched once per boot attempt, and only after the ownership
                // lease is held — so this counts attempts, where the claim count
                // does not: ownership is cached after the first grant.
                state.bootAttempts += 1;
                await wait(0);
                return settings.apiKey === undefined ? 'x'.repeat(39) : settings.apiKey;
            }

            return null;
        },
        on() {},
        send() {}
    };

    const stubRequire = (name) => {
        if (name === 'electron') return { ipcRenderer };
        if (name === 'fs') return { appendFileSync: () => {} };
        if (name === './voice/voice-bridge') return { VoiceBridge: FakeVoiceBridge };
        throw new Error('unexpected require: ' + name);
    };

    const context = {
        console: { log: () => {}, warn: () => {}, error: () => {} },
        process: { env: {} },
        require: stubRequire,
        socket,
        URLSearchParams,
        setTimeout,
        clearTimeout,
        Promise,
        Date,
        String,
        Boolean,
        Array,
        Object,
        Number,
        Math,
        Error
    };

    context.window = {
        location: { search: mode === 'hud' ? '' : '?mode=' + mode },
        setInterval: (fn, ms) => {
            state.intervals.push({ fn, ms });
            return state.intervals.length;
        }
    };

    context.document = {
        readyState: settings.readyState || 'complete',
        body: { dataset: {} },
        addEventListener: (name, handler) => {
            (state.socketEvents['dom:' + name] = state.socketEvents['dom:' + name] || []).push(handler);
        }
    };

    context.globalThis = context;

    vm.runInNewContext(fs.readFileSync(BOOTSTRAP, 'utf8'), context, { filename: BOOTSTRAP });

    return { state, context, socket };
}

// ==========================================================
// 1. THE BOOT RACE
// ==========================================================
async function testSingleBoot() {
    // Every trigger firing at once, which is what a cold start actually looks
    // like: DOM ready runs the script, the socket connects a few milliseconds
    // later, and the watchdog is already armed.
    const harness = loadBootstrap({ socketConnected: true });

    harness.context.window.__ensureVoiceReady();
    harness.context.window.__ensureVoiceReady();
    harness.socket.fire('connect');
    harness.context.window.__ensureVoiceReady();

    await wait(120);

    check(
        'four concurrent startup triggers build exactly one voice bridge',
        harness.state.bridges.length === 1,
        harness.state.bridges.length + ' bridges'
    );
    check(
        'exactly one microphone capture is started',
        harness.state.engineStarts === 1,
        harness.state.engineStarts + ' engine starts'
    );
    check(
        'exactly one Gemini Live session is opened',
        harness.state.sessionConnects === 1,
        harness.state.sessionConnects + ' connects'
    );

    // The watchdog and later socket events must not build a second one either.
    harness.socket.fire('connect');
    harness.context.window.__ensureVoiceReady();
    const watchdog = harness.state.intervals[0];
    if (watchdog) watchdog.fn();

    await wait(80);

    check(
        'the watchdog and a socket reconnect reuse the existing bridge',
        harness.state.bridges.length === 1,
        harness.state.bridges.length + ' bridges'
    );
}

async function testStaggeredTriggers() {
    // The other ordering seen in the stage log: the socket connects DURING boot,
    // between bridge_created and the microphone coming up.
    const harness = loadBootstrap({ socketConnected: false });

    harness.context.window.__ensureVoiceReady();
    await wait(5);
    harness.socket.fire('connect');
    await wait(5);
    harness.context.window.__ensureVoiceReady();

    await wait(120);

    check(
        'a socket that connects mid-boot does not start a second bridge',
        harness.state.bridges.length === 1,
        harness.state.bridges.length + ' bridges'
    );
    check(
        'and does not claim voice ownership twice',
        harness.state.ownershipClaims === 1,
        harness.state.ownershipClaims + ' claims'
    );
}

// ==========================================================
// 2. WORKSHOP MUST NOT OWN VOICE
// ==========================================================
async function testWorkshopIsPassive() {
    const harness = loadBootstrap({ mode: 'workshop', socketConnected: true });

    harness.context.window.__ensureVoiceReady();
    harness.socket.fire('connect');

    await wait(80);

    check(
        'a Workshop surface never builds a voice bridge',
        harness.state.bridges.length === 0,
        harness.state.bridges.length + ' bridges'
    );
    check(
        'a Workshop surface never requests the microphone',
        harness.state.engineStarts === 0
    );
    check(
        'a Workshop surface never claims voice ownership',
        harness.state.ownershipClaims === 0
    );
    check(
        'a Workshop surface reports itself passive rather than guessing',
        harness.context.document.body.dataset.voice === 'passive',
        String(harness.context.document.body.dataset.voice)
    );
}

// ==========================================================
// 3. A FAILED BOOT MUST BE RETRYABLE
// ==========================================================
async function testMissingKeyDoesNotWedge() {
    const harness = loadBootstrap({ apiKey: '', socketConnected: true });

    harness.context.window.__ensureVoiceReady();
    await wait(60);

    check('no API key means no bridge', harness.state.bridges.length === 0);

    // The single-flight guard must be released, or the retry when a key appears
    // would be refused for the life of the page.
    harness.context.window.__ensureVoiceReady();
    await wait(60);

    check(
        'a boot that could not start is attempted again rather than latched off',
        harness.state.bootAttempts === 2,
        harness.state.bootAttempts + ' attempts'
    );
    check('and still built no bridge without a key', harness.state.bridges.length === 0);
}

// ==========================================================
// 4. THE PERMANENT HANG
// ==========================================================
async function testConnectTimeout() {
    const { withConnectTimeout } = require(path.join(INTERFACE_DIR, 'voice', 'live-session.js'));

    // A connect that never settles — the exact shape of the production hang.
    let rejected = null;
    const started = Date.now();

    try {
        await withConnectTimeout(new Promise(() => {}), 60);
    } catch (err) {
        rejected = err;
    }

    check(
        'a connect that never settles is given up on rather than awaited forever',
        Boolean(rejected) && /did not complete/.test(rejected.message),
        rejected ? rejected.message : 'resolved'
    );
    check(
        'and it is given up on near its deadline',
        Date.now() - started < 500,
        Date.now() - started + ' ms'
    );

    // A late winner must not survive as a second live socket, which is the very
    // condition that causes the hang in the first place.
    let closed = false;
    const late = new Promise((resolve) => {
        setTimeout(() => resolve({ close: () => { closed = true; } }), 80);
    });

    try {
        await withConnectTimeout(late, 30);
    } catch (_) { /* expected */ }

    await wait(120);
    check('a session that arrives after the deadline is closed, not kept', closed);

    // The ordinary case must be untouched.
    const session = await withConnectTimeout(Promise.resolve({ close: () => { closed = 'wrong'; } }), 1000);
    check('a connect that succeeds in time is returned unchanged', Boolean(session) && closed === true);
}

// ==========================================================
// 5. RECOVERY WITHOUT A LOOP
// ==========================================================
function makeSession(connectImpl) {
    const { LiveSession } = require(path.join(INTERFACE_DIR, 'voice', 'live-session.js'));
    const states = [];
    const session = new LiveSession({
        apiKey: 'test',
        getSystemPrompt: () => 'persona',
        getTools: () => [],
        on: { state: (s) => states.push(s), error: () => {} }
    });

    session.ai = { live: { connect: connectImpl } };
    return { session, states };
}

async function testRecovery() {
    // A refused connect schedules exactly one retry, with backoff.
    const refused = makeSession(async () => { throw new Error('service unavailable'); });
    refused.session.wanted = true;
    await refused.session.open('cold');

    check(
        'a refused connect leaves the session reconnecting, not connecting',
        refused.session.state === 'reconnecting',
        refused.session.state
    );
    check('a refused connect clears the in-flight flag', refused.session.connecting === false);
    check('a refused connect schedules exactly one retry', Boolean(refused.session.reconnectTimer));
    check(
        'a session with a retry pending is not treated as stalled',
        refused.session.stalled() === false
    );
    refused.session.clearTimers();

    // A bad key must stop, not storm.
    const badKey = makeSession(async () => { throw new Error('401 API key not valid'); });
    badKey.session.wanted = true;
    await badKey.session.open('cold');

    check('a rejected API key is fatal', badKey.session.state === 'fatal', badKey.session.state);
    check('a rejected API key schedules no retry', !badKey.session.reconnectTimer);
    check('a fatal session is never reported as stalled', badKey.session.stalled() === false);

    // The dead state the repair path exists for: wanted, no socket, nothing due.
    const stalled = makeSession(async () => { throw new Error('nope'); });
    stalled.session.wanted = true;
    stalled.session.state = 'setup_pending';
    check(
        'a session that is wanted with no socket and no retry is reported stalled',
        stalled.session.stalled() === true
    );

    // THE SHAPE AN UNUSABLE API KEY ACTUALLY TAKES.
    //
    // Not a rejected connect — the handshake succeeds, the server closes the
    // socket a moment later with the reason, and the connect promise never
    // settles either way. The close event is the only account of it there will
    // ever be, so throwing it away is what left FRIDAY on 'connecting' with
    // nothing to explain why.
    const AUTH_REASON = 'API key not valid. Please pass a valid API key.';
    let closeSocket = null;

    // The connect promise NEVER settles, which is the whole point: the SDK gives
    // no error to inspect, so the close event is the only account of the failure.
    const rejectedKey = makeSession((options) => {
        closeSocket = () => options.callbacks.onclose({ reason: AUTH_REASON });
        return new Promise(() => {});
    });
    rejectedKey.session.wanted = true;

    rejectedKey.session.open('cold');
    await wait(5);

    check(
        'a connect that never settles leaves the session connecting',
        rejectedKey.session.connecting === true && rejectedKey.session.state === 'connecting',
        `${rejectedKey.session.state}/${rejectedKey.session.connecting}`
    );

    closeSocket();

    check(
        'a socket closed mid-connect keeps the reason instead of discarding it',
        rejectedKey.session.lastCloseReason === AUTH_REASON,
        repr(rejectedKey.session.lastCloseReason)
    );
    check(
        'and does not race the pending attempt with a reconnect of its own',
        !rejectedKey.session.reconnectTimer
    );

    // What open()'s catch does when its deadline expires: report what the server
    // said rather than the timeout.
    rejectedKey.session.connecting = false;
    rejectedKey.session.onConnectFailure(new Error(rejectedKey.session.lastCloseReason));

    check(
        'an unusable key is reported as fatal rather than retried forever',
        rejectedKey.session.state === 'fatal',
        rejectedKey.session.state
    );
    check('a fatal key schedules no reconnect storm', !rejectedKey.session.reconnectTimer);
    rejectedKey.session.clearTimers();

    // Setup watchdog: armed on connect, cleared by setupComplete.
    const opened = makeSession(async () => ({ close: () => {} }));
    opened.session.wanted = true;
    await opened.session.open('cold');

    check(
        'an open socket waits for setup with a deadline',
        Boolean(opened.session.setupTimer),
        'no setup timer armed'
    );
    check('an open socket is not ready until setup completes', opened.session.ready === false);

    opened.session.onMessage({ setupComplete: {} });

    check('setupComplete makes the session ready', opened.session.ready === true);
    check('setupComplete cancels the setup deadline', opened.session.setupTimer === null);
    check('a ready session is active', opened.session.state === 'active', opened.session.state);
    opened.session.clearTimers();
}

async function main() {
    const tests = [
        ['single boot under concurrent triggers', testSingleBoot],
        ['staggered startup triggers', testStaggeredTriggers],
        ['workshop is passive', testWorkshopIsPassive],
        ['a failed boot is retryable', testMissingKeyDoesNotWedge],
        ['connect timeout', testConnectTimeout],
        ['recovery without a loop', testRecovery]
    ];

    for (const [label, test] of tests) {
        console.log('\n== ' + label + ' ==');

        try {
            await test();
        } catch (err) {
            FAILED.push(label);
            console.log('[FAIL] ' + label + ' raised ' + (err && err.stack ? err.stack : err));
        }
    }

    console.log('\n' + '='.repeat(52));
    console.log('Voice startup: ' + PASSED.length + ' passed, ' + FAILED.length + ' failed');

    if (FAILED.length) {
        FAILED.forEach((name) => console.log('  failed: ' + name));
    }

    process.exit(FAILED.length ? 1 : 0);
}

main();
