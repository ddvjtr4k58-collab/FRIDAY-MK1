/**
 * Gemini Live session manager.
 *
 * Ported from the FRIDAY prototype. The parameters below are the reference values and
 * are reproduced exactly — they are the reason the prototype hears well and responds
 * fast, and they should not be "tuned" without an A/B measurement.
 *
 * The central constraint: a Live audio session is capped at ~15 minutes and an
 * individual WebSocket at ~10. Running continuously therefore requires context window
 * compression plus session resumption, and a rotation strategy that hands over during
 * a silence rather than waiting to be evicted mid-sentence.
 */

/** Flatten the API's per-modality token breakdown into {AUDIO: n, TEXT: n, ...}. */
function modalityTotals(usage) {
    const totals = {};
    const lists = [usage.promptTokensDetails, usage.responseTokensDetails];

    for (const list of lists) {
        if (!Array.isArray(list)) continue;

        for (const entry of list) {
            const name = String(entry.modality || 'UNKNOWN').toUpperCase();
            totals[name] = (totals[name] || 0) + (Number(entry.tokenCount) || 0);
        }
    }

    return totals;
}

const {
    GoogleGenAI,
    Modality,
    StartSensitivity,
    EndSensitivity,
    ActivityHandling
} = require('@google/genai');

const MODEL = 'gemini-3.1-flash-live-preview';
const VOICE = 'Flare';
// An unknown voiceName is rejected at setup as an invalid argument, which the failure
// handler below would otherwise classify as fatal and stop retrying — losing voice
// altogether. Both names were verified against the live API, so this only matters if a
// future model drops one of them.
const FALLBACK_VOICE = 'Aoede';

const ROTATE_SEEK_AFTER_MS = 6 * 60000;  // start hunting for a quiet moment
const ROTATE_FORCE_AFTER_MS = 9 * 60000; // give up hunting, rotate anyway
const BACKOFF_MIN_MS = 250;
const BACKOFF_MAX_MS = 30000;

// Two deadlines on getting a usable socket, because there are two ways to not
// get one and neither of them reports itself.
//
// CONNECT bounds ai.live.connect(). That promise resolves when the WebSocket
// opens and rejects when it is refused — but a socket that is accepted and then
// never upgraded settles NEITHER way, and the SDK has no timeout of its own.
// Losing a race for a concurrent-session slot produces exactly that, and it used
// to be permanent: `connecting` stayed true, the state stayed 'connecting', and
// every repair path was a no-op because each one could see a connect apparently
// still in flight. FRIDAY stayed deaf until the app was restarted.
//
// SETUP bounds the gap between an open socket and the setupComplete message.
// `ready` is only ever set by that message, so a socket that opens and then goes
// quiet is the same permanent hang one step further along.
const CONNECT_TIMEOUT_MS = 10000;
const SETUP_TIMEOUT_MS = 15000;

/**
 * Reject if `promise` has not settled within `ms`.
 *
 * A session that arrives after we have given up is CLOSED rather than kept: a
 * late winner must not become a second live socket, which is the failure this
 * whole timeout exists to recover from.
 */
function withConnectTimeout(promise, ms) {
    let abandoned = false;

    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
            abandoned = true;
            reject(new Error(`live connect did not complete within ${ms}ms`));
        }, ms);

        promise.then(
            (session) => {
                clearTimeout(timer);

                if (abandoned) {
                    try { session.close(); } catch (e) { /* nothing to close */ }
                    return;
                }

                resolve(session);
            },
            (error) => {
                clearTimeout(timer);
                if (!abandoned) reject(error);
            }
        );
    });
}
const MIC_BACKLOG_MS = 10000;
const MIC_REPLAY_MAX_AGE_MS = 3000;
const USER_SPEAKING_GRACE_MS = 1500;

class LiveSession {
    /**
     * @param {object} opts
     *   apiKey            - Gemini API key
     *   getSystemPrompt() - returns the persona/system instruction string
     *   getTools()        - returns an array of Gemini FunctionDeclarations
     *   on                - { state, audio, interrupted, transcript, toolCall, turnComplete, error }
     */
    constructor(opts) {
        this.opts = opts;
        this.ai = null;
        this.session = null;
        this.state = 'idle';
        this.ready = false;
        this.wanted = false;
        this.connecting = false;

        this.connectedAt = 0;
        this.activeVoice = VOICE;
        this.resumptionHandle = null;
        this.backoffMs = BACKOFF_MIN_MS;
        this.goAwayDeadline = 0;

        this.micBacklog = [];
        this.modelSpeaking = false;
        this.playbackIdle = true;
        this.lastUserAudioAt = 0;
        this.pendingToolIds = new Set();

        this.rotationTimer = null;
        this.reconnectTimer = null;
        this.setupTimer = null;
        // Why the last socket closed. The only account we get of a connect that
        // was accepted and then refused.
        this.lastCloseReason = '';

        // Usage for the CURRENT socket, plus the sum of every socket before it.
        this.usage = null;
        this.usageBase = { promptTokens: 0, responseTokens: 0, totalTokens: 0, byModality: {} };
    }

    /** Cumulative real usage across the whole run, including rotated sessions. */
    totalUsage() {
        const current = this.usage || { promptTokens: 0, responseTokens: 0, totalTokens: 0, byModality: {} };
        const byModality = { ...this.usageBase.byModality };

        for (const [name, tokens] of Object.entries(current.byModality || {})) {
            byModality[name] = (byModality[name] || 0) + tokens;
        }

        return {
            promptTokens: this.usageBase.promptTokens + current.promptTokens,
            responseTokens: this.usageBase.responseTokens + current.responseTokens,
            totalTokens: this.usageBase.totalTokens + current.totalTokens,
            byModality,
            metered: (this.usageBase.totalTokens + current.totalTokens) > 0
        };
    }

    /** Fold the finished socket's counts into the base before they are replaced. */
    bankUsage() {
        if (!this.usage) {
            return;
        }

        const merged = { ...this.usageBase.byModality };

        for (const [name, tokens] of Object.entries(this.usage.byModality || {})) {
            merged[name] = (merged[name] || 0) + tokens;
        }

        this.usageBase = {
            promptTokens: this.usageBase.promptTokens + this.usage.promptTokens,
            responseTokens: this.usageBase.responseTokens + this.usage.responseTokens,
            totalTokens: this.usageBase.totalTokens + this.usage.totalTokens,
            byModality: merged
        };
        this.usage = null;
    }

    emit(name, payload) {
        const fn = this.opts.on && this.opts.on[name];
        if (fn) fn(payload);
    }

    setState(next) {
        if (this.state === next) return;
        this.state = next;
        this.emit('state', next);
    }

    async connect() {
        this.wanted = true;
        await this.open('cold');
    }

    async disconnect() {
        this.wanted = false;
        this.clearTimers();
        this.closeSocket();
        this.setState('idle');
    }

    /** Presence gate: drop the socket to stop billing, keep the handle for instant resume. */
    suspend() {
        if (!this.wanted) return;
        this.clearTimers();
        this.closeSocket();
        this.setState('suspended');
    }

    async resume() {
        if (this.state !== 'suspended') return;
        await this.open('resume');
    }

    buildConfig() {
        const tools = this.opts.getTools ? this.opts.getTools() : [];
        return {
            // AUDIO and TEXT are mutually exclusive on the Live API — never both.
            responseModalities: [Modality.AUDIO],
            systemInstruction: this.opts.getSystemPrompt ? this.opts.getSystemPrompt() : undefined,
            // The Live API exposes no speaking-rate control — SpeechConfig carries only
            // a voice and a language. Pace is therefore directed in the system prompt,
            // which costs nothing and leaves the audio untouched.
            speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: this.activeVoice } } },

            // Drives the HUD transcript, and is the fallback source if a resumption
            // handle is ever refused.
            inputAudioTranscription: {},
            outputAudioTranscription: {},

            // Without this an audio session is hard-capped at 15 minutes.
            contextWindowCompression: { slidingWindow: {} },

            // Requesting this is what makes the server issue resumption handles.
            sessionResumption: this.resumptionHandle ? { handle: this.resumptionHandle } : {},

            realtimeInputConfig: {
                // Barge-in is the default, but it is the single switch this whole
                // feature rests on — pinned rather than inherited, so a change of
                // default upstream cannot quietly turn interruption off.
                activityHandling: ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                automaticActivityDetection: {
                    disabled: false,
                    // HIGH start sensitivity is what makes natural barge-in work: she
                    // has to notice you the moment you cut in, not after you have
                    // repeated yourself. Chromium's AEC removes her own voice, and the
                    // microphone is no longer attenuated while she speaks, so this is
                    // now judging Boss's real voice at full scale.
                    startOfSpeechSensitivity: StartSensitivity.START_SENSITIVITY_HIGH,
                    // END stays LOW so she does not cut you off mid-thought.
                    endOfSpeechSensitivity: EndSensitivity.END_SENSITIVITY_LOW,
                    // The minimum length of speech that counts as a turn start. At 300ms
                    // the one-word interruptions that matter most — "Wait", "No",
                    // "Actually" — sit right on the threshold and were routinely missed.
                    // 200ms clears them with margin. This is a floor on utterance length,
                    // not a loudness threshold; the loudness problem was the mic ducking.
                    prefixPaddingMs: 200,
                    silenceDurationMs: 800
                }
            },

            tools: tools.length ? [{ functionDeclarations: tools }] : undefined
        };
    }

    async open(reason) {
        if (this.connecting) return;
        // The replacement socket restarts usage counting from zero, so the
        // finished socket's totals must be banked or they are lost at rotation.
        this.bankUsage();
        this.connecting = true;
        this.ready = false;
        // Cleared per attempt so a reason from an earlier socket cannot be
        // reported against this one.
        this.lastCloseReason = '';
        this.setState(reason === 'cold' ? 'connecting' : 'reconnecting');

        // The previous socket stays up until the replacement is ready, so buffered mic
        // audio has somewhere to go and playback is never starved.
        const previous = this.session;
        this.session = null;

        try {
            if (!this.ai) this.ai = new GoogleGenAI({ apiKey: this.opts.apiKey });

            const session = await withConnectTimeout(
                this.ai.live.connect({
                    model: MODEL,
                    config: this.buildConfig(),
                    callbacks: {
                        onmessage: (msg) => this.onMessage(msg),
                        onerror: (e) => this.onSocketError(e),
                        onclose: (event) => this.onSocketClose(event)
                    }
                }),
                CONNECT_TIMEOUT_MS
            );

            this.session = session;
            this.connectedAt = Date.now();
            this.goAwayDeadline = 0;
            this.backoffMs = BACKOFF_MIN_MS;
            this.setState('setup_pending');

            if (previous) {
                try { previous.close(); } catch (e) { /* already gone */ }
            }

            this.armRotation();
            this.armSetupTimeout();
        } catch (err) {
            // The socket we were replacing is unreachable now — `this.session` was
            // cleared before the attempt and nothing else holds it. Closing it is
            // what stops a failed rotation leaving an orphan socket open: it would
            // keep billing, and it would keep occupying a concurrent-session slot,
            // which is precisely the condition that makes the NEXT connect hang.
            if (previous) {
                try { previous.close(); } catch (e) { /* already gone */ }
            }

            this.connecting = false;
            // Prefer what the server said over what the clock said. "API key not
            // valid" is an answer Jon can act on; "did not complete within
            // 10000ms" is only the symptom, and retrying it forever would hide
            // the one fact that explains the whole failure.
            this.onConnectFailure(this.lastCloseReason ? new Error(this.lastCloseReason) : err);
            this.lastCloseReason = '';
            return;
        }
        this.connecting = false;
    }

    /**
     * Give up on a socket that opened but never said setupComplete.
     *
     * `ready` is set only by that message, so without this a quiet socket is the
     * same permanent hang as a connect that never resolves — one step later.
     */
    armSetupTimeout() {
        this.clearSetupTimeout();
        this.setupTimer = setTimeout(() => {
            this.setupTimer = null;

            if (this.ready) return;

            this.emit('error', { message: 'live session never completed setup', fatal: false });
            this.closeSocket();
            this.scheduleReconnect();
        }, SETUP_TIMEOUT_MS);
    }

    clearSetupTimeout() {
        if (this.setupTimer) clearTimeout(this.setupTimer);
        this.setupTimer = null;
    }

    onConnectFailure(err) {
        const message = err && err.message ? err.message : String(err);

        if (typeof window !== 'undefined' && window.__voiceStage) {
            window.__voiceStage('live_session_connect', 'FAIL', message.slice(0, 160));
        }

        // A rejected voice is recoverable and must be caught before the fatal check,
        // which would otherwise match it as an invalid argument and leave her mute.
        if (/voice/i.test(message) && this.activeVoice !== FALLBACK_VOICE) {
            this.emit('error', {
                message: `voice "${this.activeVoice}" rejected, falling back to ${FALLBACK_VOICE}`,
                fatal: false
            });
            this.activeVoice = FALLBACK_VOICE;
            this.scheduleReconnect();
            return;
        }

        // Retrying a bad key just produces a request storm against something that
        // cannot succeed.
        if (/401|403|API key|PERMISSION_DENIED|invalid.*(key|argument)/i.test(message)) {
            this.setState('fatal');
            this.emit('error', { message, fatal: true });
            return;
        }

        // A refused resumption handle is recoverable — drop it and cold-start.
        if (/resum|handle/i.test(message) && this.resumptionHandle) {
            this.resumptionHandle = null;
        }

        this.emit('error', { message, fatal: false });
        this.scheduleReconnect();
    }

    scheduleReconnect() {
        if (!this.wanted || this.reconnectTimer) return;
        const jitter = Math.random() * this.backoffMs * 0.3;
        const delay = Math.min(this.backoffMs + jitter, BACKOFF_MAX_MS);
        this.backoffMs = Math.min(this.backoffMs * 2, BACKOFF_MAX_MS);
        this.setState('reconnecting');
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this.open('retry');
        }, delay);
    }

    armRotation() {
        if (this.rotationTimer) clearInterval(this.rotationTimer);
        this.rotationTimer = setInterval(() => this.considerRotation(), 1000);
    }

    /**
     * A handoff during silence is inaudible; one mid-sentence is not. From the 6 minute
     * mark we wait for the first genuinely idle moment, and only force it at 9.
     */
    considerRotation() {
        if (!this.session || !this.ready) return;
        const age = Date.now() - this.connectedAt;

        if (this.goAwayDeadline && Date.now() >= this.goAwayDeadline) return this.rotate();
        if (age < ROTATE_SEEK_AFTER_MS) return;
        if (age >= ROTATE_FORCE_AFTER_MS) return this.rotate();
        if (this.isIdle()) this.rotate();
    }

    isIdle() {
        return (
            !this.modelSpeaking &&
            this.playbackIdle &&
            this.pendingToolIds.size === 0 &&
            Date.now() - this.lastUserAudioAt > USER_SPEAKING_GRACE_MS
        );
    }

    rotate() {
        if (this.connecting) return;
        this.setState('rotating');
        this.clearTimers();
        this.open('rotate');
    }

    clearTimers() {
        if (this.rotationTimer) clearInterval(this.rotationTimer);
        if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
        this.rotationTimer = null;
        this.reconnectTimer = null;
        this.clearSetupTimeout();
    }

    closeSocket() {
        this.ready = false;
        this.clearSetupTimeout();
        const s = this.session;
        this.session = null;
        if (!s) return;
        try { s.close(); } catch (e) { /* already closed */ }
    }

    onMessage(msg) {
        if (msg.setupComplete) {
            this.ready = true;
            this.clearSetupTimeout();
            if (typeof window !== 'undefined' && window.__voiceStage) {
                window.__voiceStage('live_session_ready', 'PASS', `voice=${this.activeVoice}`);
            }
            this.setState('active');
            this.flushMicBacklog();
            return;
        }

        // Real metered usage from the API — the authoritative source for cost.
        //
        // Counts are cumulative per session, so they are stored rather than added,
        // and carried across a rotation by folding the finished session's totals
        // into a running base.
        if (msg.usageMetadata) {
            const usage = msg.usageMetadata;
            this.usage = {
                promptTokens: Number(usage.promptTokenCount) || 0,
                responseTokens: Number(usage.responseTokenCount) || 0,
                totalTokens: Number(usage.totalTokenCount) || 0,
                byModality: modalityTotals(usage)
            };
            this.emit('usage', this.totalUsage());
        }

        if (msg.sessionResumptionUpdate) {
            const { newHandle, resumable } = msg.sessionResumptionUpdate;
            if (resumable && newHandle) this.resumptionHandle = newHandle;
        }

        if (msg.goAway) {
            const secs = parseFloat(String(msg.goAway.timeLeft || '5s'));
            const leftMs = Number.isFinite(secs) ? secs * 1000 : 5000;
            const margin = Math.max(1500, leftMs * 0.2);
            this.goAwayDeadline = Date.now() + Math.max(0, leftMs - margin);
            this.setState('draining');
        }

        if (msg.toolCall && msg.toolCall.functionCalls && msg.toolCall.functionCalls.length) {
            const calls = msg.toolCall.functionCalls;
            calls.forEach((c) => { if (c.id) this.pendingToolIds.add(c.id); });
            this.emit('toolCall', calls);
        }

        if (msg.toolCallCancellation && msg.toolCallCancellation.ids) {
            msg.toolCallCancellation.ids.forEach((id) => this.pendingToolIds.delete(id));
        }

        const sc = msg.serverContent;
        if (!sc) return;

        if (sc.interrupted) {
            this.modelSpeaking = false;
            this.emit('interrupted');
        }

        if (sc.inputTranscription && sc.inputTranscription.text) {
            this.lastUserAudioAt = Date.now();
            this.emit('transcript', { role: 'user', text: sc.inputTranscription.text });
        }
        if (sc.outputTranscription && sc.outputTranscription.text) {
            this.emit('transcript', { role: 'friday', text: sc.outputTranscription.text });
        }

        const parts = (sc.modelTurn && sc.modelTurn.parts) || [];
        for (const part of parts) {
            if (part.inlineData && part.inlineData.data) {
                this.modelSpeaking = true;
                this.playbackIdle = false;
                this.emit('audio', part.inlineData.data); // base64, decoded by the caller
            }
        }

        if (sc.turnComplete || sc.generationComplete) {
            this.modelSpeaking = false;
            this.emit('turnComplete');
        }
    }

    onSocketError(e) {
        this.emit('error', { message: (e && e.message) || 'Live socket error', fatal: false });
        this.closeSocket();
        this.scheduleReconnect();
    }

    onSocketClose(event) {
        const reason = String((event && (event.reason || event.message)) || '').trim();

        this.ready = false;
        this.clearSetupTimeout();

        // KEEP THE REASON. A socket that is accepted and then closed reports why
        // HERE and nowhere else — the connect promise never settles at all, so it
        // carries no error to inspect. An unusable API key is exactly this shape:
        // the handshake succeeds, the server closes with "API key not valid", and
        // without this line that sentence is discarded and the session sits on
        // 'connecting' for the life of the process with nothing to explain it.
        if (reason) {
            this.lastCloseReason = reason;
        }

        // A close during a deliberate rotation is expected — the replacement is already
        // installed, so there is nothing to recover from. A close while connecting is
        // handled by whoever is awaiting that connect, which now has the reason.
        if (this.state === 'rotating' || this.connecting || !this.wanted) return;
        this.session = null;
        this.scheduleReconnect();
    }

    /**
     * Mic audio. During a handoff the session is briefly unavailable; rather than
     * dropping those frames we hold them and replay the recent ones once setup completes.
     */
    sendAudio(base64Pcm) {
        if (!this.wanted || this.state === 'suspended') return;

        if (!this.session || !this.ready) {
            this.micBacklog.push({ data: base64Pcm, ts: Date.now() });
            const cutoff = Date.now() - MIC_BACKLOG_MS;
            while (this.micBacklog.length && this.micBacklog[0].ts < cutoff) this.micBacklog.shift();
            return;
        }

        try {
            this.session.sendRealtimeInput({
                audio: { data: base64Pcm, mimeType: 'audio/pcm;rate=16000' }
            });
        } catch (e) {
            // Socket died between the ready check and the send; onclose reconnects.
        }
    }

    flushMicBacklog() {
        if (!this.micBacklog.length) return;
        // Replaying stale speech is more confusing than losing it.
        const cutoff = Date.now() - MIC_REPLAY_MAX_AGE_MS;
        const replay = this.micBacklog.filter((f) => f.ts >= cutoff);
        this.micBacklog = [];
        replay.forEach((f) => this.sendAudio(f.data));
    }

    sendText(text) {
        if (!this.session || !this.ready) return false;
        try {
            this.session.sendRealtimeInput({ text });
            return true;
        } catch (e) {
            return false;
        }
    }

    sendToolResponses(responses) {
        responses.forEach((r) => { if (r.id) this.pendingToolIds.delete(r.id); });
        if (!this.session || !this.ready) return;
        try {
            this.session.sendToolResponse({ functionResponses: responses });
        } catch (e) {
            // Delivering a result across a connection boundary is undocumented. If it
            // fails, the caller narrates the result as text so the turn self-heals.
        }
    }

    setPlaybackIdle(idle) {
        this.playbackIdle = idle;
    }

    /**
     * Wanted, but with no socket and nothing on the way to getting one.
     *
     * Every transition normally leaves either a live socket, an attempt in
     * flight, or a scheduled retry. If a close is ever swallowed — onSocketClose
     * deliberately ignores one that lands during a rotation — none of the three
     * is true and the session sits idle wanting to be connected. This is what the
     * repair path checks so that state is recoverable rather than terminal.
     */
    stalled() {
        return (
            this.wanted &&
            !this.ready &&
            !this.connecting &&
            !this.session &&
            !this.reconnectTimer &&
            this.state !== 'fatal' &&
            this.state !== 'suspended'
        );
    }
}

module.exports = {
    LiveSession,
    MODEL,
    VOICE,
    CONNECT_TIMEOUT_MS,
    SETUP_TIMEOUT_MS,
    // Exported for Tests/test_voice_startup.js. The hang it guards against is
    // invisible by construction — nothing throws, nothing logs — so it is worth
    // being able to assert on directly.
    withConnectTimeout
};
