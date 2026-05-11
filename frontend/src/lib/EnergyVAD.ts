/**
 * Adaptive noise-floor VAD.
 *
 * Threshold = noiseFloor × multiplier, so the VAD self-calibrates to the room
 * instead of using a fixed energy cutoff. Requires 3 consecutive frames above
 * threshold before declaring speech start (single-spike rejection).
 */
export class EnergyVAD {
  private readonly multiplier: number;
  private readonly onsetRequired: number;
  private readonly silenceFrames: number;

  private active = false;
  private silenceCount = 0;
  private onsetCount = 0;

  private noiseFloor = 0.008;
  private readonly noiseAlpha = 0.015;

  onspeechstart: (() => void) | null = null;
  onspeechend: (() => void) | null = null;

  constructor({
    multiplier = 4.5,
    onsetFrames = 3,
    silenceFrames = 20,
  }: { multiplier?: number; onsetFrames?: number; silenceFrames?: number } = {}) {
    this.multiplier = multiplier;
    this.onsetRequired = onsetFrames;
    this.silenceFrames = silenceFrames;
  }

  feed(rms: number): boolean {
    const threshold = this.noiseFloor * this.multiplier;

    if (rms > threshold) {
      this.silenceCount = 0;
      this.onsetCount++;
      if (!this.active && this.onsetCount >= this.onsetRequired) {
        this.active = true;
        this.onspeechstart?.();
      }
      return this.active;
    } else {
      this.onsetCount = 0;
      if (!this.active) {
        this.noiseFloor =
          this.noiseFloor * (1 - this.noiseAlpha) + rms * this.noiseAlpha;
      }
      if (this.active) {
        this.silenceCount++;
        if (this.silenceCount >= this.silenceFrames) {
          this.active = false;
          this.onspeechend?.();
        }
      }
      return this.active;
    }
  }

  get isActive(): boolean { return this.active; }
  get floor(): number    { return this.noiseFloor; }
  get threshold(): number { return this.noiseFloor * this.multiplier; }
}
