/**
 * Playback worklet.
 *
 * Holds its own ring buffer, fed by postMessage from the main thread as audio arrives
 * from the model.
 *
 * NOTE — this deliberately differs from the FRIDAY prototype, which used a
 * SharedArrayBuffer. SAB requires cross-origin isolation (COOP/COEP), and FRIDAY loads
 * its renderer over file://, where those headers cannot be applied. Passing chunks
 * through the worklet port avoids that constraint entirely. The only cost is that a
 * barge-in flush costs one message hop (~a few ms) instead of an Atomics write.
 *
 * The ring is what decouples playback from connection liveness: when the WebSocket
 * rotates or drops mid-sentence, whatever already arrived keeps playing. That is what
 * makes reconnects inaudible.
 */

const RING_SAMPLES = 24000 * 16; // ~16 seconds at 24 kHz

class PlaybackProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.ring = new Float32Array(RING_SAMPLES);
        this.read = 0;
        this.write = 0;
        this.count = 0;
        this.wasPlaying = false;

        this.port.onmessage = (event) => {
            const msg = event.data || {};

            if (msg.type === 'chunk') {
                this.enqueue(new Int16Array(msg.buffer));
                return;
            }

            // Barge-in. Drop everything so she stops mid-word.
            if (msg.type === 'flush') {
                this.read = 0;
                this.write = 0;
                this.count = 0;
                this.wasPlaying = false;
                this.port.postMessage({ type: 'flushed' });
            }
        };
    }

    enqueue(pcm) {
        // The model streams faster than real time, so a long reply arrives well before
        // the speaker catches up. Overrunning here would truncate the end of every long
        // answer, so report it rather than silently discarding.
        const space = RING_SAMPLES - this.count;
        const n = Math.min(pcm.length, space);

        for (let i = 0; i < n; i++) {
            this.ring[this.write] = pcm[i] / 32768;
            this.write = this.write + 1 === RING_SAMPLES ? 0 : this.write + 1;
        }
        this.count += n;

        if (n < pcm.length) {
            this.port.postMessage({ type: 'overrun', dropped: pcm.length - n });
        }
    }

    process(_inputs, outputs) {
        const out = outputs[0] && outputs[0][0];
        if (!out) return true;

        const n = Math.min(out.length, this.count);
        let peak = 0;

        for (let i = 0; i < n; i++) {
            const sample = this.ring[this.read];
            out[i] = sample;
            const abs = sample < 0 ? -sample : sample;
            if (abs > peak) peak = abs;
            this.read = this.read + 1 === RING_SAMPLES ? 0 : this.read + 1;
        }
        this.count -= n;

        // Underrun between turns is normal — emit silence rather than clicking.
        for (let i = n; i < out.length; i++) out[i] = 0;

        if (n > 0) {
            if (!this.wasPlaying) {
                this.wasPlaying = true;
                // Real audio onset. state_manager's begin_speech_playback() hangs off
                // this, so it must fire here and not when we decided to speak.
                this.port.postMessage({ type: 'started' });
            }
            this.port.postMessage({ type: 'level', peak });
        } else if (this.wasPlaying) {
            this.wasPlaying = false;
            this.port.postMessage({ type: 'drained' });
        }

        return true;
    }
}

registerProcessor('friday-playback', PlaybackProcessor);
