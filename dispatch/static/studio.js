const start = document.querySelector("#start"), stop = document.querySelector("#stop");
const state = document.querySelector("#state"), transcript = document.querySelector("#transcript");
let ctx, mic, socket, next = 0;
const playing = new Set();
function interrupt() {
  for (const node of playing) node.stop();
  playing.clear();
  next = 0;
}
async function finish(message) {
  const current = socket;
  socket = null;
  if (current && current.readyState < 2) current.close();
  interrupt();
  mic?.getTracks().forEach(t => t.stop());
  if (ctx && ctx.state !== "closed") await ctx.close();
  ctx = mic = null;
  start.disabled = false; stop.disabled = true; state.textContent = message;
}
start.onclick = async () => {
  start.disabled = true; transcript.replaceChildren(); state.textContent = "Подключение…";
  try {
    mic = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1}});
    ctx = new AudioContext({sampleRate:16000});
    await ctx.audioWorklet.addModule("/static/capture.js");
    await ctx.resume();
    const capture = new AudioWorkletNode(ctx,"vd-capture");
    ctx.createMediaStreamSource(mic).connect(capture);
    capture.connect(ctx.destination);
    socket = new WebSocket((location.protocol==="https:"?"wss://":"ws://")+location.host+"/ws/voice");
    socket.binaryType = "arraybuffer";
    capture.port.onmessage = e => { if (socket?.readyState===1) socket.send(e.data); };
    socket.onopen = () => {state.textContent="На линии";stop.disabled=false;};
    socket.onmessage = e => {
      if (typeof e.data==="string") {
        const item=JSON.parse(e.data);
        if (item.type==="interrupt") interrupt();
        if (item.type==="error") state.textContent=item.message;
        if (item.type==="turn") {
          const row=document.createElement("div"), label=document.createElement("small");
          row.className="line";label.textContent=item.speaker==="resident"?"Житель":"Ассистент";
          row.append(label,document.createTextNode(item.text));transcript.append(row);
        }
        return;
      }
      const pcm=new Int16Array(e.data), buffer=ctx.createBuffer(1,pcm.length,8000);
      buffer.copyToChannel(Float32Array.from(pcm,x=>x/32768),0);
      const src=ctx.createBufferSource();src.buffer=buffer;src.connect(ctx.destination);
      next=Math.max(next,ctx.currentTime+.015);src.start(next);next+=buffer.duration;
      playing.add(src);src.onended=()=>playing.delete(src);
    };
    socket.onclose=()=>{if(socket) finish(state.textContent.includes("Сеанс прерван")?state.textContent:"Разговор завершён");};
  } catch(e) { await finish("Подключение не удалось · "+e.message); }
};
stop.onclick=()=>finish("Разговор завершён");
