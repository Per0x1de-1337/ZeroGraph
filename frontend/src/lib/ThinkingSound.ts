/**
 * Soft pulsing tone played during PROCESSING state.
 * Two detuned oscillators (180 + 183 Hz) + 1.5 Hz LFO at ~1.2% volume.
 * Makes dead-air gaps feel shorter without being distracting.
 */
export class ThinkingSound {
  private ctx: AudioContext | null = null;
  private nodes: {
    osc1: OscillatorNode;
    osc2: OscillatorNode;
    lfo: OscillatorNode;
    master: GainNode;
  } | null = null;

  start(): void {
    if (this.nodes) return;
    this.ctx = this.ctx ?? new AudioContext({ sampleRate: 44100 });
    if (this.ctx.state === 'suspended') this.ctx.resume();

    const ctx = this.ctx;
    const osc1    = ctx.createOscillator();
    const osc2    = ctx.createOscillator();
    const lfo     = ctx.createOscillator();
    const lfoGain = ctx.createGain();
    const master  = ctx.createGain();

    osc1.type = osc2.type = 'sine';
    osc1.frequency.value = 180;
    osc2.frequency.value = 183;
    lfo.type = 'sine';
    lfo.frequency.value = 1.5;
    lfoGain.gain.value  = 0.008;
    master.gain.value   = 0;

    lfo.connect(lfoGain);
    lfoGain.connect(master.gain);
    osc1.connect(master);
    osc2.connect(master);
    master.connect(ctx.destination);

    master.gain.setValueAtTime(0, ctx.currentTime);
    master.gain.linearRampToValueAtTime(0.012, ctx.currentTime + 0.25);

    osc1.start(); osc2.start(); lfo.start();
    this.nodes = { osc1, osc2, lfo, master };
  }

  stop(): void {
    if (!this.nodes || !this.ctx) return;
    const { osc1, osc2, lfo, master } = this.nodes;
    this.nodes = null;
    master.gain.cancelScheduledValues(this.ctx.currentTime);
    master.gain.setValueAtTime(master.gain.value, this.ctx.currentTime);
    master.gain.linearRampToValueAtTime(0, this.ctx.currentTime + 0.15);
    setTimeout(() => {
      try { osc1.stop(); osc2.stop(); lfo.stop(); } catch (_) { /* ignore */ }
    }, 200);
  }
}
