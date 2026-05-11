/**
 * Gapless TTS audio playback via Web Audio API.
 * Schedules incoming Int16 PCM chunks back-to-back with a 20ms lookahead.
 */
export class AudioPlayer {
  private ctx: AudioContext | null = null;
  private nextAt = 0;

  async init(): Promise<void> {
    this.ctx = new AudioContext({ sampleRate: 16000 });
    await this.ctx.resume();
  }

  push(arrayBuffer: ArrayBuffer): void {
    if (!this.ctx) return;
    const int16 = new Int16Array(arrayBuffer);
    const f32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
      f32[i] = int16[i] / (int16[i] < 0 ? 0x8000 : 0x7fff);
    }
    const buf = this.ctx.createBuffer(1, f32.length, 16000);
    buf.getChannelData(0).set(f32);

    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);

    const when = Math.max(this.ctx.currentTime + 0.02, this.nextAt);
    src.start(when);
    this.nextAt = when + f32.length / 16000;
  }

  async flush(): Promise<void> {
    if (!this.ctx) return;
    const old = this.ctx;
    this.ctx = new AudioContext({ sampleRate: 16000 });
    await this.ctx.resume();
    this.nextAt = 0;
    try { await old.close(); } catch (_) { /* ignore */ }
  }
}
