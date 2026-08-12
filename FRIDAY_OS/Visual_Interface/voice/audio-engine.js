/**
 * Audio capture and playback for the live voice pipeline.
 *
 * Ported from the FRIDAY prototype, whose parameters are treated as the reference and
 * are reproduced exactly: 16 kHz mono capture with echo cancellation on and noise
 * suppression OFF, 20 ms Int16 frames, 24 kHz playback through a worklet ring buffer.
 *
 * Why audio lives in the renderer at all: Chromium's WebRTC echo canceller uses audio
 * played inside this same process as its reference signal. Capturing in Python while
 * playing through Chromium (or vice versa) leaves AEC blind, which on laptop speakers
 * with an always-open mic is an immediate feedback loop.
 *
 * noiseSuppression is off on purpose — gemini-3.1-flash-live handles background noise
 * itself, and Chromium's suppressor degrades the prosody cues the native-audio model
 * relies on.
 */

const MIC_SAMPLE_RATE = 16000;
const PLAYBACK_SAMPLE_RATE = 24000;

function readDuckingGain() {
    const raw = typeof process !== 'undefined' && process.env
        ? (process.env.FRIDAY_MIC_DUCK ?? process.env.JARVIS_MIC_DUCK)
        : undefined;
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 && value <= 1 ? value : 1;
}

class VoiceAudioEngine {
    /**
     * @param {object} handlers
     *   onFrame(ArrayBuffer)   - 20 ms of 16 kHz Int16 mic audio, ready for the wire
     *   onMicLevel(number)     - 0..1, drives the orb while listening
     *   onPlaybackStart()      - REAL audio onset (never "we decided to speak")
     *   onPlaybackLevel(number)- 0..1 while she speaks
     *   onPlaybackEnd()        - player genuinely drained
     *   onDiag(string)         - human-readable diagnostics
     */
    constructor(handlers = {}) {
        this.handlers = handlers;
        this.captureCtx = null;
        this.playbackCtx = null;
        this.captureNode = null;
        this.playbackNode = null;
        this.stream = null;
        // 1 = no attenuation. The microphone goes up the wire at full scale even while
        // she is speaking: ducking hit Boss's interruption exactly as hard as it hit her
        // echo, so it spent headroom during double talk and bought nothing. Chromium's
        // AEC is what removes her voice, and it can tell the two apart. See the header
        // of capture-worklet.js for what was and was not measured here.
        //
        // Escape hatch for a machine where AEC genuinely fails and she interrupts
        // herself: FRIDAY_MIC_DUCK=0.35 restores the old behaviour. Reach for it only
        // after confirming echo cancellation is the actual problem.
        this.duckingGain = readDuckingGain();
        this.muted = false;
        this.started = false;
        this.framesProduced = 0;
    }

    diag(message) {
        if (this.handlers.onDiag) this.handlers.onDiag(message);
    }

    stage(name, status, detail) {
        if (typeof window !== 'undefined' && window.__voiceStage) {
            window.__voiceStage(name, status, detail);
        }
    }

    async start() {
        if (this.started) return;
        this.started = true;

        try {
            await this.startPlayback();
            this.stage('playback_context', 'PASS', `state=${this.playbackCtx.state}`);
        } catch (error) {
            this.stage('playback_context', 'FAIL', String(error && error.message || error));
            this.started = false;
            throw error;
        }

        await this.startCapture();
    }

    async startCapture() {
        this.stage('mic_permission_request', 'PASS', 'calling getUserMedia');

        try {
            this.stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    channelCount: 1,
                    echoCancellation: true,
                    autoGainControl: true,
                    noiseSuppression: false
                }
            });
        } catch (err) {
            // name is the part that matters: NotAllowedError = denied by macOS/
            // Electron, NotFoundError = no input device, NotReadableError = the
            // device is held by something else.
            this.stage('mic_stream', 'FAIL',
                `${(err && err.name) || 'Error'}: ${(err && err.message) || String(err)}`);
            this.diag('getUserMedia FAILED: ' + (err && err.message ? err.message : String(err)));
            throw err;
        }

        const track = this.stream.getAudioTracks()[0];
        this.stage('mic_stream', track && track.readyState === 'live' ? 'PASS' : 'FAIL',
            `track=${track ? track.readyState : 'none'} label="${track ? track.label : 'none'}"`);
        this.diag('mic acquired: "' + (track ? track.label : 'unknown') + '"');

        // Asking for a 16 kHz context makes Chromium resample the 48 kHz device stream
        // properly. Doing it by hand in the worklet would be worse and slower.
        this.captureCtx = new AudioContext({ sampleRate: MIC_SAMPLE_RATE, latencyHint: 'interactive' });

        try {
            await this.captureCtx.audioWorklet.addModule('voice/capture-worklet.js');
            this.stage('capture_worklet', 'PASS');
        } catch (error) {
            this.stage('capture_worklet', 'FAIL', String((error && error.message) || error));
            throw error;
        }

        const source = this.captureCtx.createMediaStreamSource(this.stream);
        this.captureNode = new AudioWorkletNode(this.captureCtx, 'friday-capture');

        this.captureNode.port.onmessage = (event) => {
            const msg = event.data || {};
            if (msg.type !== 'frame') return;
            this.framesProduced++;
            if (this.handlers.onFrame) this.handlers.onFrame(msg.buffer);
            if (this.handlers.onMicLevel && !this.muted) this.handlers.onMicLevel(msg.peak || 0);
        };

        source.connect(this.captureNode);

        // Web Audio only pulls nodes on a path to a destination — an unconnected worklet
        // never has process() called and produces no frames at all. Route through a
        // muted gain node so the graph stays live without anything reaching the speakers.
        const sink = this.captureCtx.createGain();
        sink.gain.value = 0;
        this.captureNode.connect(sink);
        sink.connect(this.captureCtx.destination);

        if (this.captureCtx.state === 'suspended') await this.captureCtx.resume();
        this.stage('capture_context', this.captureCtx.state === 'running' ? 'PASS' : 'FAIL',
            `state=${this.captureCtx.state} rate=${this.captureCtx.sampleRate}`);
        this.diag('capture running @' + this.captureCtx.sampleRate + 'Hz');
    }

    async startPlayback() {
        this.playbackCtx = new AudioContext({
            sampleRate: PLAYBACK_SAMPLE_RATE,
            latencyHint: 'interactive'
        });
        await this.playbackCtx.audioWorklet.addModule('voice/playback-worklet.js');

        this.playbackNode = new AudioWorkletNode(this.playbackCtx, 'friday-playback', {
            outputChannelCount: [1]
        });

        this.playbackNode.port.onmessage = (event) => {
            const msg = event.data || {};
            switch (msg.type) {
                case 'started':
                    // No-op unless FRIDAY_MIC_DUCK was set: her voice is removed by AEC,
                    // not by turning the microphone down. See duckingGain above.
                    this.setDuck(true);
                    if (this.handlers.onPlaybackStart) this.handlers.onPlaybackStart();
                    break;
                case 'level':
                    if (this.handlers.onPlaybackLevel) this.handlers.onPlaybackLevel(msg.peak || 0);
                    break;
                case 'drained':
                    this.setDuck(false);
                    if (this.handlers.onPlaybackEnd) this.handlers.onPlaybackEnd();
                    break;
                case 'flushed':
                    this.setDuck(false);
                    if (this.handlers.onPlaybackEnd) this.handlers.onPlaybackEnd();
                    break;
                case 'overrun':
                    this.diag('playback ring overrun, dropped ' + msg.dropped + ' samples');
                    break;
            }
        };

        this.playbackNode.connect(this.playbackCtx.destination);
        if (this.playbackCtx.state === 'suspended') await this.playbackCtx.resume();
    }

    /** Queue model audio (24 kHz Int16) for playback. */
    play(arrayBuffer) {
        if (!this.playbackNode) return;
        this.playbackNode.port.postMessage({ type: 'chunk', buffer: arrayBuffer }, [arrayBuffer]);
        if (this.playbackCtx && this.playbackCtx.state === 'suspended') this.playbackCtx.resume();
    }

    /** Barge-in: stop speaking immediately and discard everything buffered. */
    flush() {
        if (!this.playbackNode) return;
        this.playbackNode.port.postMessage({ type: 'flush' });
    }

    setDuck(active) {
        if (!this.captureNode) return;
        this.captureNode.port.postMessage({
            type: 'duck',
            gain: active ? this.duckingGain : 1
        });
    }

    setMuted(muted) {
        this.muted = muted;
        if (this.captureNode) this.captureNode.port.postMessage({ type: 'mute', muted });
    }

    setDuckingGain(gain) {
        const value = Number(gain);
        if (Number.isFinite(value)) this.duckingGain = Math.max(0, Math.min(1, value));
    }

    /**
     * The raw facts about whether audio is actually moving.
     *
     * Deliberately reports observable state — track readyState, AudioContext
     * state, a monotonic frame counter — rather than a "started" boolean. The
     * boolean was true in every broken case we care about: a stopped MediaStream
     * track, a suspended AudioContext, and a half-initialised engine all leave it
     * set while no microphone audio reaches the wire.
     */
    health() {
        const track = this.stream && this.stream.getAudioTracks
            ? this.stream.getAudioTracks()[0] || null
            : null;

        return {
            started: this.started,
            micTrack: track ? track.readyState : 'none',
            micEnabled: track ? track.enabled !== false : false,
            micLabel: track ? track.label : '',
            captureState: this.captureCtx ? this.captureCtx.state : 'none',
            playbackState: this.playbackCtx ? this.playbackCtx.state : 'none',
            framesProduced: this.framesProduced,
            muted: this.muted
        };
    }

    /** True only when microphone audio can actually flow right now. */
    isCapturing() {
        const h = this.health();
        return h.started && h.micTrack === 'live' && h.captureState === 'running';
    }

    /**
     * Repair whatever is broken, doing the least work that will fix it.
     *
     * Suspended contexts are resumed in place; only a genuinely dead microphone
     * track causes capture to be torn down and reacquired, because reacquiring
     * needlessly would drop audio mid-sentence.
     */
    async ensureRunning() {
        if (!this.started) {
            await this.start();
            return this.health();
        }

        if (this.playbackCtx && this.playbackCtx.state === 'suspended') {
            await this.playbackCtx.resume().catch(() => {});
        }

        if (this.captureCtx && this.captureCtx.state === 'suspended') {
            await this.captureCtx.resume().catch(() => {});
        }

        const track = this.stream && this.stream.getAudioTracks
            ? this.stream.getAudioTracks()[0] || null
            : null;

        // A track that ended (device unplugged, permission revoked, OS reclaimed
        // the input) can never restart — it has to be replaced.
        if (!track || track.readyState !== 'live') {
            this.diag('microphone track is ' + (track ? track.readyState : 'missing') + '; reacquiring');

            try {
                if (this.stream) {
                    this.stream.getTracks().forEach((t) => t.stop());
                }

                if (this.captureCtx) {
                    await this.captureCtx.close().catch(() => {});
                }

                this.captureCtx = null;
                this.captureNode = null;
                this.stream = null;
                await this.startCapture();
            } catch (error) {
                this.diag('microphone reacquire FAILED: ' + (error && error.message ? error.message : error));
            }
        }

        return this.health();
    }

    async stop() {
        if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
        if (this.captureCtx) await this.captureCtx.close().catch(() => {});
        if (this.playbackCtx) await this.playbackCtx.close().catch(() => {});
        this.captureCtx = null;
        this.playbackCtx = null;
        this.captureNode = null;
        this.playbackNode = null;
        this.stream = null;
        this.started = false;
    }
}

module.exports = { VoiceAudioEngine, MIC_SAMPLE_RATE, PLAYBACK_SAMPLE_RATE };
