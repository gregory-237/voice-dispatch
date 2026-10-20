class Capture extends AudioWorkletProcessor {
  constructor() { super(); this.samples=[]; this.sum=0; this.count=0; }
  process(inputs) {
    const data=inputs[0]?.[0];
    if (!data) return true;
    for (const value of data) {
      this.sum+=value;this.count++;
      if (this.count===sampleRate/8000) {
        this.samples.push(Math.round(Math.max(-1,Math.min(1,this.sum/this.count))*32767));
        this.sum=this.count=0;
        if (this.samples.length===160) {
          const pcm=Int16Array.from(this.samples);this.port.postMessage(pcm.buffer,[pcm.buffer]);this.samples=[];
        }
      }
    }
    return true;
  }
}
registerProcessor("vd-capture",Capture);
