/**
 * Microphone capture worklet.
 *
 * Runs inside an AudioContext created at 16 kHz, so Chromium has already resampled the
 * device stream with a proper polyphase filter. All this does is frame and convert to
 * the 16-bit PCM the Gemini Live API expects: 20 ms frames of 320 samples / 640 bytes.
 *
 * WHY THERE IS NO DUCKING ON THE TRANSMITTED SIGNAL
 *
 * This used to multiply the mic by 0.35 while FRIDAY was speaking, and that attenuated
 * audio was what went up the wire — so an interruption reached the server's VAD about
 * 9 dB down.
 *
 * Measured honestly, that attenuation alone does not defeat detection: replaying
 * synthesized speech into a live session, start-of-speech was still committed in
 * ~280ms at peaks as low as 0.035. So ducking was not, by itself, the reason barge-in
 * felt broken, and it should not be described as such.
 *
 * It is still wrong, for a simpler reason: a gain multiplier cannot tell her voice from
 * Boss's, so it buys nothing. Her voice is removed by Chromium's echo canceller — which
 * is the entire reason capture and playback live in the same process — and AEC is the
 * mechanism that CAN tell the two apart. What ducking actually did was spend 9 dB of
 * headroom at the one moment it is most needed: during double talk, where AEC's residual
 * suppressor has already eaten into the near-end voice. The two losses compound, and
 * only one of them is doing useful work.
 *
 * So the wire gets the microphone at full scale. duckGain is retained as an escape hatch
 * for a machine where AEC genuinely fails (see FRIDAY_MIC_DUCK in audio-engine.js) and
 * is 1 — inaudible — by default.
 */

const FRAME_SAMPLES = 320;

class CaptureProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.buffer = new Float32Array(FRAME_SAMPLES);
        this.filled = 0;
        this.duckGain = 1;
        this.muted = false;

        this.port.onmessage = (event) => {
            const msg = event.data || {};
            if (msg.type === 'duck') this.duckGain = msg.gain;
            if (msg.type === 'mute') this.muted = msg.muted;
        };
    }

    process(inputs) {
        const channel = inputs[0] && inputs[0][0];
        if (!channel) return true;

        // Mute is an explicit instruction and still gates hard; ducking is not.
        const gain = this.muted ? 0 : this.duckGain;
        let peak = 0;

        for (let i = 0; i < channel.length; i++) {
            const sample = channel[i] * gain;
            this.buffer[this.filled++] = sample;

            const abs = sample < 0 ? -sample : sample;
            if (abs > peak) peak = abs;

            if (this.filled === FRAME_SAMPLES) {
                const pcm = new Int16Array(FRAME_SAMPLES);
                for (let s = 0; s < FRAME_SAMPLES; s++) {
                    // Clamp before scaling so loud input saturates instead of wrapping
                    // round into a loud click.
                    const v = Math.max(-1, Math.min(1, this.buffer[s]));
                    pcm[s] = v < 0 ? v * 0x8000 : v * 0x7fff;
                }
                // `peak` rides along so the orb can show mic amplitude without the main
                // thread having to run a second analyser over the same audio.
                this.port.postMessage({ type: 'frame', buffer: pcm.buffer, peak }, [pcm.buffer]);
                this.filled = 0;
                peak = 0;
            }
        }

        return true;
    }
}

registerProcessor('friday-capture', CaptureProcessor);
