/**
 * AudioWorklet processor: runs on a dedicated audio thread.
 *
 * Receives Float32 samples from the mic at 16kHz, accumulates them into
 * 20ms chunks (320 samples), converts to Int16 PCM, and posts to main thread.
 *
 * Also computes per-chunk RMS energy for the main thread's VAD.
 */
class PCMProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this._buf = [];
        this._CHUNK = 320; // 20ms @ 16kHz
    }

    process(inputs) {
        const channel = inputs[0]?.[0];
        if (!channel) return true;

        for (let i = 0; i < channel.length; i++) {
            this._buf.push(channel[i]);
        }

        while (this._buf.length >= this._CHUNK) {
            const floats = this._buf.splice(0, this._CHUNK);

            // Convert Float32 → Int16
            const int16 = new Int16Array(floats.length);
            let sumSq = 0;
            for (let i = 0; i < floats.length; i++) {
                const s = Math.max(-1, Math.min(1, floats[i]));
                int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
                sumSq += s * s;
            }
            const rms = Math.sqrt(sumSq / floats.length);

            // Transfer ownership of buffer (zero-copy)
            this.port.postMessage({ pcm: int16.buffer, rms }, [int16.buffer]);
        }

        return true; // keep processor alive
    }
}

registerProcessor("pcm-processor", PCMProcessor);
