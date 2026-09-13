// ==UserScript==
// @name         AutoSubtitleSync · Browser Audio Bridge
// @namespace    autosubtitlesync.local
// @version      2.1.0
// @description  直接从网页播放器抓音频（不解析链接、不下载视频），推给本机 AutoSubtitleSync 生成实时字幕并叠加在画面上；带"影子播放器"可行性探测
// @author       AutoSubtitleSync
// @match        *://*/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

/*
 * 工作方式
 *   播放器音频 --captureStream--> AudioContext --(16kHz 单声道 PCM16)--> 本机服务
 *   本机服务用 Whisper 实时识别 -> /api/companion/sync -> 这里渲染字幕条
 *
 * 只在本机抓音：音频以 0.5 秒为块发到 127.0.0.1 的本地服务，不经过任何第三方服务器。
 * 不支持 Safari（Safari 没有 captureStream）；受 DRM/跨域保护的播放器可能抓不到声音，
 * 这种情况脚本会给出提示，可改用页面上的「粘贴视频链接」模式。
 */

(function () {
  'use strict';
  if (window.__AS_AUDIO_BRIDGE__) return;
  window.__AS_AUDIO_BRIDGE__ = true;

  const PORTS = []; for (let p = 8765; p <= 8785; p++) PORTS.push(p);
  const TARGET_RATE = 16000;
  const CHUNK_SAMPLES = 8000;        // 0.5 秒
  const MAX_INFLIGHT = 3;

  const DEFAULTS = { model: 'base', lang: 'auto', mixed: true, target: 'zh', bilingual: false, window: 6, tail: 1.5 };

  let server = null;            // {port, version}
  let video = null;             // 当前绑定的播放器
  let ctx = null, srcNode = null, sinkNode = null, procNode = null, streamRef = null;
  let tail = new Float32Array(0);
  let pending = [];             // Int16Array 待发送
  let pendingSamples = 0;
  let inflight = 0, dropped = 0, pushedBytes = 0, silentWindows = 0;
  let running = false, starting = false;
  let seq = 0, cues = [], revision = -1, lastCueEnd = 0, lastCueText = '';
  let startedAt = 0, lastAudioAt = 0, lastPoll = 0;
  let toast = '';

  // ---------------------------------------------------------------- 本地服务
  function req(method, url, body, timeout, onBinary) {
    return new Promise((resolve, reject) => {
      if (typeof GM_xmlhttpRequest === 'function') {
        GM_xmlhttpRequest({
          method, url, data: body, timeout: timeout || 4000, binary: !!onBinary,
          headers: body && !onBinary ? { 'Content-Type': 'application/json' } : {},
          onload: r => { try { resolve(JSON.parse(r.responseText || '{}')); } catch (e) { reject(new Error('返回内容无法解析')); } },
          onerror: () => reject(new Error('无法连接本机服务')), ontimeout: () => reject(new Error('连接本机服务超时'))
        });
        return;
      }
      fetch(url, { method, body, headers: { 'Content-Type': 'application/json' } })
        .then(r => r.json()).then(resolve).catch(() => reject(new Error('浏览器拦截了本地请求：请在 Tampermonkey 中运行本脚本')));
    });
  }

  async function findServer() {
    if (server) {
      try { await req('GET', `http://127.0.0.1:${server.port}/api/companion/ping`, null, 1200); return server; }
      catch (e) { server = null; }
    }
    for (const p of PORTS) {
      try {
        const x = await req('GET', `http://127.0.0.1:${p}/api/companion/ping`, null, 700);
        if (x && x.app === 'AutoSubtitleSync') { server = { port: p, version: x.version || '' }; return server; }
      } catch (e) { /* 端口空闲 */ }
    }
    return null;
  }

  // ---------------------------------------------------------------- 播放器
  function mainVideo() {
    const all = Array.from(document.querySelectorAll('video'));
    let best = null, bestScore = 0;
    for (const v of all) {
      const r = v.getBoundingClientRect();
      const area = r.width * r.height;
      if (!area || area < 20000) continue;
      const playing = (!v.paused && !v.ended) ? 2.5 : 1;
      const score = area * playing * (v.readyState >= 2 ? 1.2 : 1);
      if (score > bestScore) { bestScore = score; best = v; }
    }
    return best;
  }

  // ---------------------------------------------------------------- 音频管线
  const WORKLET = `class ASBCapture extends AudioWorkletProcessor{
    process(inputs){const c=inputs[0]&&inputs[0][0];if(c&&c.length)this.port.postMessage(new Float32Array(c));return true;}
  } registerProcessor('asb-capture',ASBCapture);`;

  function toInt16PCM(f32) {
    // 重采样到 16k（AudioContext 采样率未必是 16k），跨块保持连续
    const ratio = ctx.sampleRate / TARGET_RATE;
    let buf;
    if (tail.length) { buf = new Float32Array(tail.length + f32.length); buf.set(tail, 0); buf.set(f32, tail.length); }
    else buf = f32;
    if (buf.length < 2) { tail = buf; return null; }
    const nOut = Math.floor((buf.length - 1) / ratio);
    if (nOut <= 0) { tail = buf; return null; }
    const out = new Int16Array(nOut);
    for (let i = 0; i < nOut; i++) {
      const p = i * ratio, i0 = Math.floor(p), f = p - i0;
      const a = buf[i0], b = buf[i0 + 1] === undefined ? a : buf[i0 + 1];
      let s = a + (b - a) * f;
      if (s > 1) s = 1; else if (s < -1) s = -1;
      out[i] = (s * 32767) | 0;
    }
    tail = buf.slice(Math.floor(nOut * ratio));
    return out;
  }

  function onSamples(f32) {
    if (!running) return;
    lastAudioAt = Date.now();
    if (video && video.paused) return;            // 暂停时不吃数据，服务端会自然等待
    const pcm = toInt16PCM(f32);
    if (!pcm) return;
    const rms = Math.sqrt(pcm.reduce((a, v) => a + (v / 32768) * (v / 32768), 0) / pcm.length);
    if (rms < 0.0015) silentWindows++; else silentWindows = 0;
    pending.push(pcm); pendingSamples += pcm.length;
    while (pendingSamples >= CHUNK_SAMPLES) {
      const merged = new Int16Array(CHUNK_SAMPLES);
      let off = 0;
      while (off < CHUNK_SAMPLES && pending.length) {
        const head = pending[0], need = CHUNK_SAMPLES - off;
        if (head.length <= need) { merged.set(head, off); off += head.length; pending.shift(); }
        else { merged.set(head.subarray(0, need), off); pending[0] = head.subarray(need); off += need; }
      }
      pendingSamples -= CHUNK_SAMPLES;
      sendChunk(merged);
    }
  }

  function sendChunk(int16) {
    if (!server) return;
    if (inflight >= MAX_INFLIGHT) { dropped++; return; }   // 网络/解码跟不上时丢最旧的块
    inflight++;
    const url = `http://127.0.0.1:${server.port}/api/browser-audio/push`;
    const payload = int16.buffer.slice(0);
    if (typeof GM_xmlhttpRequest === 'function') {
      GM_xmlhttpRequest({
        method: 'POST', url, data: payload, binary: true, timeout: 8000,
        headers: { 'Content-Type': 'application/octet-stream' },
        onload: () => { inflight--; pushedBytes += int16.byteLength; },
        onerror: () => { inflight--; dropped++; },
        ontimeout: () => { inflight--; dropped++; }
      });
    } else {
      fetch(url, { method: 'POST', body: payload })
        .then(() => { inflight--; pushedBytes += int16.byteLength; })
        .catch(() => { inflight--; dropped++; });
    }
  }

  async function buildPipeline(v) {
    const stream = v.captureStream ? v.captureStream() : (v.mozCaptureStream ? v.mozCaptureStream() : null);
    if (!stream) throw new Error('这个浏览器不能直接从播放器抓音频（Safari 不支持）。请改用 Chrome / Edge，或使用页面上的「粘贴视频链接」模式。');
    const tracks = stream.getAudioTracks();
    streamRef = stream;
    ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: TARGET_RATE });
    try { await ctx.resume(); } catch (e) { }
    srcNode = ctx.createMediaStreamSource(new MediaStream(tracks.length ? tracks : []));
    sinkNode = ctx.createGain(); sinkNode.gain.value = 0;   // 静音输出，避免把声音再放一遍（v8 旧版这里是回声 bug）
    srcNode.connect(sinkNode); sinkNode.connect(ctx.destination);
    if (ctx.audioWorklet) {
      const url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }));
      await ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      procNode = new AudioWorkletNode(ctx, 'asb-capture');
      procNode.port.onmessage = e => onSamples(e.data);
      srcNode.connect(procNode); procNode.connect(sinkNode);
    } else {
      procNode = ctx.createScriptProcessor(4096, 1, 1);
      procNode.onaudioprocess = e => onSamples(new Float32Array(e.inputBuffer.getChannelData(0)));
      srcNode.connect(procNode); procNode.connect(sinkNode);
    }
  }

  function teardownPipeline() {
    try { if (procNode) procNode.disconnect(); } catch (e) { }
    try { if (srcNode) srcNode.disconnect(); } catch (e) { }
    try { if (sinkNode) sinkNode.disconnect(); } catch (e) { }
    try { if (streamRef) streamRef.getTracks().forEach(t => t.stop()); } catch (e) { }
    try { if (ctx) ctx.close(); } catch (e) { }
    ctx = srcNode = sinkNode = procNode = streamRef = null;
    tail = new Float32Array(0); pending = []; pendingSamples = 0;
  }

  // ---------------------------------------------------------------- 会话
  async function start() {
    if (running || starting) return;
    starting = true; setMsg('正在连接本机服务…');
    try {
      if (!(await findServer())) throw new Error('没有找到本机服务：请先双击运行 AutoSubtitleSync 的启动程序，并保持窗口开着。');
      video = mainVideo();
      if (!video) throw new Error('没有找到正在播放的 HTML5 播放器：请先点开视频播放几秒再点开始。');
      if (video.paused) { try { await video.play(); } catch (e) { } }
      const p = prefs();
      const r = await req('POST', `http://127.0.0.1:${server.port}/api/browser-audio/open`, JSON.stringify({
        audio_mode: true, model: p.model, lang: p.lang, mixed: p.mixed, target: p.target, bilingual: p.bilingual,
        t0: video.currentTime || 0, title: document.title || location.hostname, url: location.href,
        window: p.window, tail: p.tail
      }), 20000);
      if (r.error) throw new Error(r.error);
      seq = r.seq; cues = []; revision = -1; lastCueEnd = 0; lastCueText = '';
      pushedBytes = 0; dropped = 0; silentWindows = 0; startedAt = Date.now();
      await buildPipeline(video);
      running = true; toast = '';
      bindVideoEvents();
      ensureOverlay(); setMsg('已开始：声音正在送到本机识别，字幕会在几秒后逐句出现。', false);
    } catch (e) {
      setMsg(e.message || String(e), true);
      teardownPipeline();
    } finally { starting = false; refreshUI(); }
  }

  async function stop(silent) {
    if (!running) { if (!silent) setMsg('当前没有在运行。', false); return; }
    running = false;
    teardownPipeline();
    if (server && seq) { try { await req('POST', `http://127.0.0.1:${server.port}/api/browser-audio/stop`, '{}', 8000); } catch (e) { } }
    if (overlayHost) overlayHost.style.display = 'none';
    if (!silent) setMsg(`已停止。本次共收到 ${cues.length} 条字幕。`, false);
    refreshUI();
  }

  let boundVideo = null;
  function bindVideoEvents() {
    if (boundVideo) { ['seeked', 'ended', 'pause', 'playing'].forEach(k => boundVideo.removeEventListener(k, onVideoEvent)); }
    boundVideo = video;
    if (boundVideo) ['seeked', 'ended', 'pause', 'playing'].forEach(k => boundVideo.addEventListener(k, onVideoEvent));
  }

  async function onVideoEvent(e) {
    if (!running) return;
    if (e.type === 'ended') { stop(true); setMsg('视频播放结束，已停止实时字幕。', false); return; }
    if (e.type === 'seeked') {
      // 时间轴跳变后旧字幕会对不上，直接开一个新会话（历史字幕会清空）
      seq = 0; cues = []; revision = -1; lastCueEnd = 0;
      toast = '跳转后重新对齐时间轴…';
      try { await req('POST', `http://127.0.0.1:${server.port}/api/browser-audio/stop`, '{}', 5000); } catch (er) { }
      setTimeout(() => { running = false; start(); }, 300);
    }
  }

  // ---------------------------------------------------------------- 轮询字幕
  async function poll() {
    if (!running || !server || !seq) return;
    try {
      const x = await req('GET', `http://127.0.0.1:${server.port}/api/companion/sync?seq=${seq}&revision=${revision}`, null, 2500);
      if (x.stale) { stop(true); setMsg('字幕会话已被其他页面接管。', true); return; }
      if (Array.isArray(x.cues) && x.cues.length) { cues = x.cues; revision = Number(x.revision || 0); }
      if (x.error) toast = String(x.error);
    } catch (e) { toast = e.message || String(e); }
    refreshUI();
  }

  // ---------------------------------------------------------------- 字幕条
  let overlayHost = null, overlayRoot = null, capEl = null, hudEl = null;
  let probeOut = null, copyProbeBtn = null, probeBusy = false;
  function ensureOverlay() {
    if (overlayHost && overlayHost.isConnected) return;
    overlayHost = document.createElement('div');
    overlayHost.id = 'as-audio-bridge-overlay';
    overlayHost.style.cssText = 'all:initial;position:fixed;z-index:2147483646;pointer-events:none;display:none;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",sans-serif;';
    overlayRoot = overlayHost.attachShadow({ mode: 'open' });
    overlayRoot.innerHTML = `<style>*{box-sizing:border-box}
      .cap{position:absolute;left:5%;right:5%;bottom:7%;display:flex;justify-content:center;text-align:center}
      .text{display:inline-block;max-width:94%;white-space:pre-line;color:#fff;font-size:clamp(18px,2.15vw,31px);font-weight:680;line-height:1.38;text-shadow:0 2px 5px #000,0 0 14px #000;background:rgba(0,0,0,.5);padding:7px 12px;border-radius:8px}
      .hud{position:absolute;right:10px;top:10px;background:rgba(12,17,27,.72);color:#fff;border:1px solid rgba(255,255,255,.15);border-radius:999px;padding:5px 10px;font:600 11px/1.35 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;backdrop-filter:blur(8px);max-width:60%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}</style>
      <div class="cap"><div class="text" id="cap"></div></div><div class="hud" id="hud"></div>`;
    document.documentElement.appendChild(overlayHost);
    capEl = overlayRoot.getElementById('cap'); hudEl = overlayRoot.getElementById('hud');
  }

  function placeOverlay() {
    const v = video && video.isConnected ? video : (video = mainVideo());
    if (!overlayHost || !v || !running) { if (overlayHost) overlayHost.style.display = 'none'; return; }
    const fs = document.fullscreenElement || document.webkitFullscreenElement;
    if (fs) {
      if (overlayHost.parentNode !== fs) fs.appendChild(overlayHost);
      Object.assign(overlayHost.style, { display: 'block', position: 'fixed', left: '0px', top: '0px', width: '100vw', height: '100vh' });
      return;
    }
    if (overlayHost.parentNode !== document.documentElement) document.documentElement.appendChild(overlayHost);
    const r = v.getBoundingClientRect();
    if (r.width < 80 || r.height < 50) { overlayHost.style.display = 'none'; return; }
    Object.assign(overlayHost.style, {
      display: 'block', position: 'fixed', left: `${Math.max(0, r.left)}px`, top: `${Math.max(0, r.top)}px`,
      width: `${Math.max(0, r.width)}px`, height: `${Math.max(0, r.height)}px`
    });
  }

  function render() {
    if (!running) { if (overlayHost) overlayHost.style.display = 'none'; return; }
    placeOverlay();
    if (!capEl) return;
    // 实时字幕：永远显示最新识别出的那一句（这是直播字幕与"播放进度对齐"的字幕的区别）
    const c = cues.length ? cues[cues.length - 1] : null;
    const text = toast || (c ? String(c.text || '') : (pushedBytes > 16000 ? '' : '正在接通声音…'));
    capEl.textContent = text;
    capEl.style.display = text ? 'inline-block' : 'none';
    if (c) { lastCueEnd = Number(c.end || 0); }
    const lag = lastCueEnd ? Math.max(0, (Date.now() - startedAt) / 1000 - lastCueEnd) : 0;
    let state;
    if (dropped > 12) state = '⚠️ 处理跟不上，已丢弃部分声音';
    else if (silentWindows > 40) state = '⚠️ 抓不到声音：该播放器可能受保护，改用链接模式';
    else if (video && video.paused) state = '已暂停 · 等待播放';
    else if (!cues.length) state = '正在识别，请等几秒…';
    else state = `实时字幕 · 共 ${cues.length} 条 · 约慢 ${lag.toFixed(0)} 秒`;
    hudEl.textContent = state;
  }

  // ---------------------------------------------------------------- 控制面板
  let panelRoot = null, msgEl = null, pillDot = null, pillText = null, startBtn = null, stopBtn = null, statsEl = null;
  function buildPanel() {
    const host = document.createElement('div');
    host.id = 'as-audio-bridge-host';
    host.style.cssText = 'all:initial;position:fixed;right:18px;bottom:18px;z-index:2147483647;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif;';
    const sh = host.attachShadow({ mode: 'open' });
    sh.innerHTML = `<style>*{box-sizing:border-box}button,select{font:inherit}
      .pill{border:1px solid rgba(255,255,255,.15);background:#111827;color:#fff;border-radius:999px;padding:10px 14px;box-shadow:0 8px 28px rgba(15,23,42,.28);font-size:13px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:8px}
      .dot{width:8px;height:8px;border-radius:50%;background:#94a3b8}.dot.ok{background:#34d399}.dot.wait{background:#f59e0b}.dot.bad{background:#fb7185}
      .panel{position:absolute;right:0;bottom:48px;width:330px;background:rgba(255,255,255,.985);color:#111827;border:1px solid #e5e7eb;border-radius:16px;padding:14px;box-shadow:0 18px 50px rgba(15,23,42,.22);display:none}
      .panel.open{display:block}.title{font-size:14px;font-weight:800;margin-bottom:3px}.sub{font-size:11px;color:#6b7280;line-height:1.5;margin-bottom:10px}
      .grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}.field{display:flex;flex-direction:column;gap:5px}.field.full{grid-column:1/-1}
      .label{font-size:11px;font-weight:700;color:#4b5563}select{width:100%;border:1px solid #dfe3e8;border-radius:9px;background:#fff;padding:8px;font-size:12px;color:#111827}
      .check{display:flex;align-items:center;gap:7px;font-size:12px;color:#374151;padding-top:4px}
      .primary,.secondary,.stop{width:100%;border:0;border-radius:10px;padding:10px 12px;font-size:12px;font-weight:750;cursor:pointer;margin-top:9px}
      .primary{background:#2563eb;color:#fff}.secondary{background:#f1f3f6;color:#252a33}.stop{background:#fff0ee;color:#b42318}
      .primary:disabled{opacity:.5}.msg{font-size:11px;color:#6b7280;line-height:1.55;margin-top:9px}.msg.err{color:#b42318}
      .stats{margin-top:10px;padding:9px 10px;border-radius:10px;background:#f7f8fa;border:1px solid #e9ebef;font-size:11px;color:#505866;line-height:1.55;font-variant-numeric:tabular-nums}
      .hidden{display:none!important}
      .report{max-height:210px;overflow:auto;background:#0b1220;color:#d7e3f4;border-radius:10px;padding:10px;font:11px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-all;margin-top:9px}
      .hint{font-size:10.5px;color:#8a93a3;line-height:1.5;margin-top:7px}</style>
      <button class="pill" id="pill"><span class="dot" id="dot"></span><span id="pillText">实时字幕（音频桥）</span></button>
      <div class="panel" id="panel">
        <div class="title">AutoSubtitleSync · 浏览器音频桥</div>
        <div class="sub">直接抓取当前网页播放器的声音，在你电脑上实时识别成字幕。不解析链接、不下载视频。</div>
        <div class="grid">
          <label class="field"><span class="label">识别模型</span><select id="model">
            <option value="tiny">tiny · 最快</option><option value="base">base · 推荐</option><option value="small">small · 最准</option></select></label>
          <label class="field"><span class="label">输出字幕</span><select id="target">
            <option value="zh">中文</option><option value="original">保持原语言</option><option value="en">English</option><option value="de">Deutsch</option><option value="fr">Français</option><option value="es">Español</option></select></label>
          <label class="field"><span class="label">字幕快慢</span><select id="win">
            <option value="4">快（延迟小、精度略低）</option><option value="6">均衡 · 推荐</option><option value="8">准（延迟更大）</option></select></label>
          <label class="field"><span class="label">语言检测</span><select id="lang">
            <option value="auto">自动（含混合语言）</option><option value="en">English</option><option value="zh">中文</option><option value="de">Deutsch</option><option value="fr">Français</option><option value="es">Español</option></select></label>
          <label class="field full"><span class="check"><input type="checkbox" id="bilingual"> 原文 + 翻译 双语显示</span></label>
        </div>
        <button class="primary" id="start">开始实时字幕</button>
        <button class="stop hidden" id="stop">停止</button>
        <button class="secondary" id="open-ui">打开本机控制台</button>
        <button class="secondary" id="probe">测试影子播放器（可选）</button>
        <pre class="report hidden" id="probeOut"></pre>
        <button class="secondary hidden" id="copyProbe">复制报告</button>
        <div class="hint">「影子播放器」是给字幕加前瞻用的：同一页面里克隆一个静音播放器，提前 60 秒播放同一段视频，让识别拿到"未来"的声音，断句就能像导入字幕那样自然。不是所有网站都允许（blob:/DRM 的通常不行）——点上面的按钮实测，把报告发给我。</div>
        <div class="stats hidden" id="stats"></div>
        <div class="msg" id="msg">第一次用：先双击运行 AutoSubtitleSync 启动程序，等它显示"已启动"，再点上面的按钮。</div>
      </div>`;
    document.documentElement.appendChild(host);
    panelRoot = sh;
    const $ = id => sh.getElementById(id);
    msgEl = $('msg'); pillDot = $('dot'); pillText = $('pillText'); startBtn = $('start'); stopBtn = $('stop'); statsEl = $('stats'); probeOut = $('probeOut'); copyProbeBtn = $('copyProbe');
    const saved = loadPrefs();
    $('model').value = saved.model; $('target').value = saved.target; $('win').value = String(saved.window);
    $('lang').value = saved.lang; $('bilingual').checked = !!saved.bilingual;
    ['model', 'target', 'win', 'lang', 'bilingual'].forEach(id => $(id).addEventListener('change', () => { savePrefs(); refreshUI(); }));
    $('pill').addEventListener('click', () => $('panel').classList.toggle('open'));
    startBtn.addEventListener('click', start);
    stopBtn.addEventListener('click', () => stop(false));
    $('probe').addEventListener('click', () => { if (!probeBusy) probeShadow(); });
    copyProbeBtn.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(probeOut.textContent || ''); copyProbeBtn.textContent = '已复制 ✓'; setTimeout(() => copyProbeBtn.textContent = '复制报告', 1500); }
      catch (e) { setMsg('复制失败，请手动选中报告文字复制。', true); }
    });
    $('open-ui').addEventListener('click', async () => {
      const s = server || await findServer();
      if (!s) { setMsg('没有找到本机服务：请先运行 AutoSubtitleSync 启动程序。', true); return; }
      window.open(`http://127.0.0.1:${s.port}/`, '_blank', 'noopener');
    });
  }

  function prefs() { return Object.assign({}, DEFAULTS, loadPrefs()); }
  function loadPrefs() {
    try { return JSON.parse(localStorage.getItem('autosubtitlesync_audio_bridge_v2') || '{}'); } catch (e) { return {}; }
  }
  function savePrefs() {
    if (!panelRoot) return;
    const v = { model: panelRoot.getElementById('model').value, target: panelRoot.getElementById('target').value, window: Number(panelRoot.getElementById('win').value), lang: panelRoot.getElementById('lang').value, bilingual: panelRoot.getElementById('bilingual').checked };
    v.tail = v.window <= 4 ? 1.0 : (v.window >= 8 ? 2.0 : 1.5);
    v.mixed = v.lang === 'auto';
    try { localStorage.setItem('autosubtitlesync_audio_bridge_v2', JSON.stringify(v)); } catch (e) { }
  }
  function setMsg(t, isErr) { if (!msgEl) return; msgEl.textContent = t; msgEl.className = 'msg' + (isErr ? ' err' : ''); }

  function refreshUI() {
    if (!panelRoot) return;
    startBtn.classList.toggle('hidden', running || starting);
    stopBtn.classList.toggle('hidden', !(running || starting));
    startBtn.disabled = starting;
    pillText.textContent = running ? '实时字幕 运行中' : '实时字幕（音频桥）';
    pillDot.className = 'dot ' + (running ? 'ok' : (msgEl && msgEl.className.includes('err') ? 'bad' : ''));
    statsEl.classList.toggle('hidden', !running);
    if (running) {
      const secs = (Date.now() - startedAt) / 1000;
      statsEl.innerHTML = `已送入声音：<b>${(pushedBytes / 32000).toFixed(1)} 秒</b>（播放了 ${secs.toFixed(0)} 秒）<br>已识别字幕：<b>${cues.length} 条</b>${dropped ? `<br>丢弃的音频块：<b>${dropped}</b>（电脑处理不过来时会丢）` : ''}`;
    }
  }

  // ---------------------------------------------------------------- 影子播放器探测
  function waitFor(fn, ms, step) {
    return new Promise(resolve => {
      const t0 = Date.now();
      const tick = () => {
        let r = false;
        try { r = fn(); } catch (e) { r = false; }
        if (r) return resolve(true);
        if (Date.now() - t0 > ms) return resolve(false);
        setTimeout(tick, step || 200);
      };
      tick();
    });
  }

  function measureStream(v, ms) {
    // 抓 v 的声音 ms 毫秒，返回有效响度（用来自动判断"抓得到 / 抓不到声音"）
    return new Promise(async resolve => {
      let ac = null, stream = null, node = null, gain = null, src = null;
      let sum = 0, n = 0, peak = 0, frames = 0;
      try {
        stream = v.captureStream ? v.captureStream() : (v.mozCaptureStream ? v.mozCaptureStream() : null);
        if (!stream || !stream.getAudioTracks().length) return resolve({ ok: false, rms: 0, reason: '没有音轨 / 不支持 captureStream' });
        ac = new (window.AudioContext || window.webkitAudioContext)();
        try { await ac.resume(); } catch (e) { }
        src = ac.createMediaStreamSource(new MediaStream(stream.getAudioTracks()));
        gain = ac.createGain(); gain.gain.value = 0;          // 静音：探测时不会外放
        node = ac.createScriptProcessor(4096, 1, 1);
        node.onaudioprocess = e => {
          const d = e.inputBuffer.getChannelData(0);
          for (let i = 0; i < d.length; i++) { const x = d[i]; sum += x * x; const a = x < 0 ? -x : x; if (a > peak) peak = a; n++; }
          frames++;
        };
        src.connect(node); node.connect(gain); gain.connect(ac.destination);
      } catch (e) {
        try { if (ac) ac.close(); } catch (e2) { }
        return resolve({ ok: false, rms: 0, reason: e.message || String(e) });
      }
      setTimeout(() => {
        const rms = n ? Math.sqrt(sum / n) : 0;
        try { node.disconnect(); src.disconnect(); gain.disconnect(); } catch (e) { }
        try { stream.getTracks().forEach(t => t.stop()); } catch (e) { }
        try { ac.close(); } catch (e) { }
        resolve({ ok: rms > 0.0008, rms, peak, frames });
      }, ms);
    });
  }

  async function probeShadow() {
    probeBusy = true;
    const out = [];
    const say = t => { out.push(t); probeOut.textContent = out.join('\n'); };
    probeOut.classList.remove('hidden'); copyProbeBtn.classList.remove('hidden');
    probeOut.textContent = '正在探测，请保持视频播放（约 12 秒）…';
    out.length = 0;
    const LEAD = 60;
    say('AutoSubtitleSync 影子播放器探测报告');
    say('时间：' + new Date().toLocaleString());
    say('页面：' + location.hostname + location.pathname.slice(0, 70));
    const v = mainVideo();
    if (!v) { say('✗ 没找到正在播放的播放器——请先点开视频播放几秒再点这个按钮。'); probeBusy = false; return; }
    const raw = v.currentSrc || v.src || '';
    const isBlob = raw.startsWith('blob:');
    say('主播放器：' + (isBlob ? 'blob:（页面内部生成，第二个播放器通常拿不到）' : (raw.startsWith('http') ? 'http(s) 直链' : (raw ? '其它（' + raw.slice(0, 24) + '…）' : '地址为空'))));
    say('  时长 ' + (isFinite(v.duration) && v.duration > 0 ? v.duration.toFixed(1) + ' 秒' : '未知（可能是直播）') +
        ' · readyState=' + v.readyState + ' · ' + (v.paused ? '暂停' : '播放中') + ' · 静音=' + (v.muted || v.volume === 0));
    const canCapture = !!(v.captureStream || v.mozCaptureStream);
    say('抓音能力：' + (canCapture ? '支持 captureStream ✓' : '不支持 ✗（需 Chrome / Edge）'));
    if (!canCapture) { say(''); say('结论：这个浏览器不支持音频桥，影子播放器方案无从谈起。'); probeBusy = false; return; }

    say('');
    say('① 测主播放器的声音（3 秒）…');
    const m = await measureStream(v, 3000);
    say('   主播放器 ' + (m.ok ? '有声音 ✓' : '几乎无声 ✗') + '  RMS=' + m.rms.toFixed(4) + (m.reason ? '  ' + m.reason : ''));
    if (!m.ok) {
      say('');
      say('结论：这个播放器本身抓不到声音（跨域 / DRM 保护），影子播放器也一样抓不到。');
      say('建议：改用「粘贴视频链接」模式（让服务端自己取流），或换一个网站再测。');
      probeBusy = false; return;
    }

    say('');
    say('② 尝试在页面内克隆一个静音播放器（同源同地址）…');
    const pv = document.createElement('video');
    pv.muted = true; pv.volume = 0; pv.playsInline = true; pv.preload = 'auto';
    pv.setAttribute('playsinline', ''); pv.setAttribute('muted', '');
    pv.style.cssText = 'position:fixed;right:2px;bottom:2px;width:2px;height:2px;opacity:0.01;pointer-events:none;z-index:0';
    if (v.crossOrigin) pv.crossOrigin = v.crossOrigin;
    try { pv.src = raw; } catch (e) { say('   ✗ 无法设置地址：' + (e.message || e)); }
    document.body.appendChild(pv);
    const log = [];
    ['loadedmetadata', 'canplay', 'playing', 'seeked', 'waiting', 'stalled', 'error'].forEach(k => pv.addEventListener(k, () => log.push(k)));
    try { pv.load(); } catch (e) { }
    const gotMeta = await waitFor(() => pv.readyState >= 1 || pv.error, 6000);
    if (!gotMeta) {
      say('   ✗ 第二个播放器 6 秒内没有加载出任何东西（该网站多半用 blob:/MSE 喂数据）');
      say('   事件：' + (log.join(', ') || '（无）'));
      say('');
      say('结论：这个网站起不了影子播放器。');
      say('建议：改用「粘贴视频链接」模式（服务端用 yt-dlp 取流自己做前瞻）——这条对大多数主流站点有效。');
      try { pv.src = ''; pv.remove(); } catch (e) { }
      probeBusy = false; return;
    }
    if (pv.error) {
      say('   ✗ 第二个播放器报错：code=' + pv.error.code + ' ' + (pv.error.message || ''));
      say('');
      say('结论：这个网站不允许同一地址被第二个播放器加载（常见于 blob:/带鉴权的地址）。');
      say('建议：改用「粘贴视频链接」模式。');
      try { pv.src = ''; pv.remove(); } catch (e) { }
      probeBusy = false; return;
    }
    say('   元数据加载成功 ✓（时长 ' + (isFinite(pv.duration) && pv.duration > 0 ? pv.duration.toFixed(1) + ' 秒' : '未知') + '）');
    const seekable = pv.seekable && pv.seekable.length ? pv.seekable.end(pv.seekable.length - 1) : 0;
    say('   可跳转范围：' + (seekable ? '0 → ' + seekable.toFixed(1) + ' 秒' : '不可跳转（直播/流式）'));
    if (!seekable) {
      say('');
      say('结论：这个内容不能被跳到"未来"，影子播放器无从提前。');
      say('（如果你看的是真直播，这是正常的：直播没有未来。若你落后于直播边缘，也仍拿不到更前面的音频。）');
      try { pv.src = ''; pv.remove(); } catch (e) { }
      probeBusy = false; return;
    }

    const target = Math.min(seekable, (v.currentTime || 0) + LEAD);
    say('');
    say('③ 让它跳到 +' + LEAD + ' 秒（目标 ' + target.toFixed(1) + ' 秒）并播放…');
    try { pv.currentTime = target; } catch (e) { say('   ✗ 跳转失败：' + (e.message || e)); }
    const seeked = await waitFor(() => Math.abs(pv.currentTime - target) < 2 && pv.readyState >= 2, 8000);
    if (!seeked) {
      say('   ✗ 跳转后没有就绪（currentTime=' + pv.currentTime.toFixed(1) + ' readyState=' + pv.readyState + '）');
      say('   事件：' + (log.join(', ') || '（无）'));
      say('');
      say('结论：这个网站能起第二个播放器但跳不进去，影子播放器不可用。');
      try { pv.src = ''; pv.remove(); } catch (e) { }
      probeBusy = false; return;
    }
    try { await pv.play(); } catch (e) { say('   ⚠ 自动播放被拦：' + (e.message || e)); }
    const t0 = pv.currentTime;
    const moving = await waitFor(() => !pv.paused && pv.currentTime > t0 + 0.6, 5000);
    say('   ' + (moving ? '正在播放 ✓' : '未能播放 ✗（可能被站点/浏览器拦下）') + '  currentTime=' + pv.currentTime.toFixed(1));
    say('');
    say('④ 测第二个播放器的声音（4 秒，静音播放，你不会听到）…');
    const m2 = await measureStream(pv, 4000);
    say('   影子播放器 ' + (m2.ok ? '有声音 ✓' : '几乎无声 ✗') + '  RMS=' + m2.rms.toFixed(4) + (m2.reason ? '  ' + m2.reason : ''));
    try { pv.pause(); pv.src = ''; pv.remove(); } catch (e) { }

    say('');
    say('事件轨迹：' + (log.join(', ') || '（无）'));
    say('');
    if (moving && m2.ok) {
      say('结论：✓ 这个网站可以用影子播放器做前瞻。');
      say('预期效果：识别拿到比播放领先约 ' + LEAD + ' 秒的声音，断句能像导入字幕一样自然，字幕也能按进度对齐显示。');
      say('代价：这个页面会同时拉两路流（流量约翻倍），部分站点可能把进度写进观看历史。');
    } else if (moving && !m2.ok) {
      say('结论：这个网站能提前播放，但第二个播放器的声音抓不到（站点对第二路做了限制）。');
      say('建议：改用「粘贴视频链接」模式。');
    } else {
      say('结论：这个网站起不了会播放的影子播放器，方案不适用。');
      say('建议：改用「粘贴视频链接」模式。');
    }
    probeBusy = false;
  }

  // ---------------------------------------------------------------- 启动
  buildPanel();
  setInterval(() => { placeOverlay(); render(); }, 120);
  setInterval(poll, 700);
  setInterval(refreshUI, 1000);
  setInterval(async () => {                    // 换播放器（单页应用换视频）时自动重新绑定
    if (!running) return;
    const v = mainVideo();
    if (v && v !== video) {
      video = v; bindVideoEvents(); toast = '检测到新的播放器，已切换…';
      try { if (srcNode && ctx) { teardownPipeline(); await buildPipeline(v); } } catch (e) { toast = e.message || String(e); }
    }
  }, 2500);
  window.addEventListener('beforeunload', () => { if (running && server && seq) { try { navigator.sendBeacon(`http://127.0.0.1:${server.port}/api/browser-audio/stop`, ''); } catch (e) { } } });
})();
