// 用 Node 直接跑浏览器脚本里的重采样代码，再把 PCM 推给真实服务端
import fs from 'node:fs';

const src = fs.readFileSync('/tmp/autosub-v8/work_v8/BrowserAudioBridge.user.js', 'utf8');

// --- 从脚本里原地抽取 toInt16PCM（花括号配平），确保测的就是发布出去的那段代码
function extract(name) {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error('找不到函数 ' + name);
  let d = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { d++; started = true; }
    else if (src[j] === '}') { d--; if (started && d === 0) return src.slice(i, j + 1); }
  }
  throw new Error('花括号不配平');
}
const TARGET_RATE = 16000;
let tail = new Float32Array(0);
const ctx = { sampleRate: 48000 };            // 模拟 48kHz 的浏览器 AudioContext
const toInt16PCM = eval('(' + extract('toInt16PCM') + ')');

// --- 分块逻辑与脚本里的 onSamples 一致（每 8000 样本 = 0.5 秒发一块）
const CHUNK = 8000;
let pending = [], pendingSamples = 0, chunks = [];
function feed(f32) {
  const pcm = toInt16PCM(f32);
  if (!pcm) return;
  pending.push(pcm); pendingSamples += pcm.length;
  while (pendingSamples >= CHUNK) {
    const merged = new Int16Array(CHUNK);
    let off = 0;
    while (off < CHUNK && pending.length) {
      const head = pending[0], need = CHUNK - off;
      if (head.length <= need) { merged.set(head, off); off += head.length; pending.shift(); }
      else { merged.set(head.subarray(0, need), off); pending[0] = head.subarray(need); off += need; }
    }
    pendingSamples -= CHUNK;
    chunks.push(merged);
  }
}

const B = fs.readFileSync('/tmp/stream48.pcm');
const f32 = new Float32Array(B.length / 2);
for (let i = 0; i < f32.length; i++) f32[i] = B.readInt16LE(i * 2) / 32768;
console.log(`输入 48kHz ${(f32.length / 48000).toFixed(1)} 秒`);
// 按 4096 样本一块喂进去，模拟 ScriptProcessor / AudioWorklet 的节奏
for (let i = 0; i < f32.length; i += 4096) feed(f32.subarray(i, Math.min(i + 4096, f32.length)));
console.log(`重采样后得到 ${chunks.length} 块 × 0.5 秒 = ${(chunks.length * 0.5).toFixed(1)} 秒 PCM16`);

const BASE = 'http://127.0.0.1:8767';
const J = async (p, o) => (await fetch(BASE + p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(o) })).json();
const G = async p => (await fetch(BASE + p)).json();

const r = await J('/api/browser-audio/open', { audio_mode: true, model: 'base', lang: 'en', mixed: true, target: 'original', bilingual: false, t0: 0, title: '浏览器端脚本验证' });
console.log('开会话:', r);
const seq = r.seq;
let seen = 0;
const t0 = Date.now();
for (let i = 0; i < chunks.length; i++) {
  const res = await fetch(BASE + '/api/browser-audio/push', { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: Buffer.from(chunks[i].buffer) });
  if (!res.ok) console.log('push 失败', res.status);
  const s = await G(`/api/companion/sync?seq=${seq}&revision=-1`);
  if ((s.cues || []).length !== seen) {
    seen = (s.cues || []).length;
    const last = s.cues[seen - 1];
    console.log(`  [${((Date.now() - t0) / 1000).toFixed(1)}s] 字幕 ${seen} 条 · 末条 [${last.start.toFixed(1)}-${last.end.toFixed(1)}] ${last.text}`);
  }
  await new Promise(r => setTimeout(r, 500));   // 真实时间推流
}
await new Promise(r => setTimeout(r, 4000));
await J('/api/browser-audio/stop', {});
await new Promise(r => setTimeout(r, 1500));
const s = await G(`/api/companion/sync?seq=${seq}&revision=-1`);
console.log('\n最终字幕:');
for (const c of s.cues || []) console.log(`  ${String(c.idx).padStart(2)}. [${c.start.toFixed(2)}-${c.end.toFixed(2)}] ${c.text}`);
const exp = fs.readFileSync('/tmp/stream.txt', 'utf8').trim().split(/\s+/).join(' ').toLowerCase();
const got = (s.cues || []).map(c => c.text).join(' ').replace(/[,.!?]/g, '').toLowerCase();
const expw = exp.replace(/[,.!?]/g, '').split(' ');
let hit = 0; for (const w of expw) if (got.includes(w)) hit++;
console.log(`\n词覆盖率: ${(hit / expw.length * 100).toFixed(1)}%  (${hit}/${expw.length})`);
console.log(`覆盖时间: ${(s.cues || []).length ? s.cues[0].start.toFixed(2) : '-'} -> ${(s.cues || []).length ? s.cues[s.cues.length - 1].end.toFixed(2) : '-'} (音频 27.4 秒)`);
