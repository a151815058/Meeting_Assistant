// AudioWorklet: downsample the microphone stream to 16 kHz mono PCM16 (REQ-01/REQ-02).
// Each output sample is the average of the input samples it covers (a simple
// anti-aliasing box filter), which is sufficient for speech recognition.
const TARGET_RATE = 16000;

class PcmDownsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    this.rest = new Float32Array(0);
    this.pos = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;

    const merged = new Float32Array(this.rest.length + input.length);
    merged.set(this.rest);
    merged.set(input, this.rest.length);

    const out = [];
    let pos = this.pos;
    while (pos + this.ratio <= merged.length) {
      const start = Math.floor(pos);
      const end = Math.max(start + 1, Math.floor(pos + this.ratio));
      let sum = 0;
      for (let i = start; i < end; i++) sum += merged[i];
      out.push(sum / (end - start));
      pos += this.ratio;
    }
    const consumed = Math.floor(pos);
    this.rest = merged.slice(consumed);
    this.pos = pos - consumed;

    if (out.length) {
      const pcm = new Int16Array(out.length);
      for (let i = 0; i < out.length; i++) {
        const s = Math.max(-1, Math.min(1, out[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}

registerProcessor("pcm-downsampler", PcmDownsampler);
