// Audio worklets: receive-audio jitter buffer + resampler, and mic capture.
const NET_RATE = 16000;

class RxPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.cap = NET_RATE * 2;
    this.buf = new Float32Array(this.cap);
    this.r = 0; this.n = 0; this.pos = 0; this.playing = false;
    this.step = NET_RATE / sampleRate;
    this.target = (NET_RATE * 0.12) | 0;   // ~120 ms of cushion
    this.max = (NET_RATE * 0.4) | 0;       // beyond this, catch up
    // Listener-side AGC: lifts quiet radios (up to +40 dB) toward a steady level.
    this.agc = true; this.env = 0.01; this.gain = 1; this.maxGain = 100; this.agcLevel = 0.25;
    this.envAttack = 1 - Math.exp(-1 / (sampleRate * 0.005));     // 5 ms
    this.envRelease = Math.exp(-1 / (sampleRate * 0.8));          // 0.8 s
    this.gainSlew = 1 - Math.exp(-1 / (sampleRate * 0.05));      // 50 ms
    this.port.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) this.push(new Int16Array(e.data));
      else if ("agc" in e.data) this.agc = e.data.agc;
    };
  }
  push(s) {
    for (let i = 0; i < s.length; i++) {
      if (this.n === this.cap) { this.r = (this.r + 1) % this.cap; this.n--; }
      this.buf[(this.r + this.n) % this.cap] = s[i] / 32768;
      this.n++;
    }
    if (this.n > this.max) {
      const drop = this.n - this.target;
      this.r = (this.r + drop) % this.cap; this.n -= drop;
    }
  }
  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!this.playing) {
      if (this.n < this.target) { out.fill(0); return true; }
      this.playing = true;
    }
    for (let i = 0; i < out.length; i++) {
      if (this.n < 2) { this.playing = false; out.fill(0, i); break; }
      const a = this.buf[this.r], b = this.buf[(this.r + 1) % this.cap];
      const v = a + (b - a) * this.pos;
      const mag = Math.abs(v);
      this.env = mag > this.env ? this.env + (mag - this.env) * this.envAttack : this.env * this.envRelease;
      const want = this.agc ? Math.min(this.maxGain, this.agcLevel / Math.max(this.env, 1e-4)) : 1;
      this.gain += (want - this.gain) * this.gainSlew;
      out[i] = Math.max(-1, Math.min(1, v * this.gain));
      this.pos += this.step;
      while (this.pos >= 1) { this.pos -= 1; this.r = (this.r + 1) % this.cap; this.n--; }
    }
    return true;
  }
}

// Mic capture: 16 kHz 20 ms frames for the radio. The mic (not the test tone,
// which is raw) gets this device's gain, then a peak limiter at CEIL: the same
// -12 dBFS peak as the test tone the TX level is set with, so speech never
// drives the radio harder than that calibration (louder only fills it in).
// Reports the level every 100 ms for the page's meter and Mic check.
const CEIL = 10 ** (-12 / 20);

class MicCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.raw = !!(options && options.processorOptions && options.processorOptions.raw);
    this.ratio = sampleRate / NET_RATE;
    this.acc = 0; this.cnt = 0; this.t = 0;
    this.out = new Int16Array(320); this.o = 0;   // 20 ms frames
    this.gain = 1; this.lim = 1;
    this.release = 1 - Math.exp(-1 / (sampleRate * 0.15));     // 150 ms back to unity
    this.rSq = 0; this.rPk = 0; this.rMin = 1; this.rN = 0; this.rEvery = (sampleRate / 10) | 0;
    this.port.onmessage = (e) => { if ("gainDb" in e.data) this.gain = 10 ** (e.data.gainDb / 20); };
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      let x = ch[i];
      if (!this.raw) {
        const a = Math.abs(x);
        this.rSq += x * x; if (a > this.rPk) this.rPk = a;    // the mic itself, before gain
        x *= this.gain;
        const want = Math.abs(x) > CEIL ? CEIL / Math.abs(x) : 1;
        this.lim = want < this.lim ? want : this.lim + (1 - this.lim) * this.release;
        x = Math.max(-CEIL, Math.min(CEIL, x * this.lim));
        if (this.lim < this.rMin) this.rMin = this.lim;
        if (++this.rN >= this.rEvery) {
          this.port.postMessage({ lvl: { rms: Math.sqrt(this.rSq / this.rN), peak: this.rPk, lim: this.rMin } });
          this.rSq = 0; this.rPk = 0; this.rMin = 1; this.rN = 0;
        }
      }
      this.acc += x; this.cnt++; this.t += 1;
      if (this.t >= this.ratio) {
        this.t -= this.ratio;
        const v = this.acc / this.cnt;
        this.acc = 0; this.cnt = 0;
        this.out[this.o++] = Math.max(-1, Math.min(1, v)) * 32767;
        if (this.o === this.out.length) {
          this.port.postMessage(this.out.buffer, [this.out.buffer]);
          this.out = new Int16Array(320); this.o = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("rx-player", RxPlayer);
registerProcessor("mic-capture", MicCapture);
