// ==UserScript==
// @name         AutoSubtitleSync · Browser Audio Bridge
// @namespace    autosubtitlesync.local
// @version      3.0.0
// @description  把正在播放的声音送给你自己电脑上的 AutoSubtitleSync 做实时字幕。三种抓音方式：播放器元素 / 共享标签页音频 / 系统声音；支持跨框架 & 影子播放器探测
// @author       AutoSubtitleSync
// @match        *://*/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

/*
 * 三种抓音方式（面板里可选）
 *   ① 播放器元素：抓 <video>/<audio> 的声音（最精确、可做影子播放器前瞻；跨域/blob/MSE 可能抓不到）
 *   ② 共享标签页音频：浏览器弹窗选"当前标签页"并勾选分享音频（iframe / MSE / blob / 跨域 / WebAudio 全通吃）
 *   ③ 系统声音：声音由本机服务用 ffmpeg 直接录（最万能，含 DRM；需要装一次虚拟声卡 BlackHole）
 *
 * 跨框架：播放器常被放在 iframe 里。脚本在每个 frame 都会运行，但只有顶层显示面板；
 * 找到播放器的那个 frame 负责抓音并在自己那里叠字幕，状态与探测报告回传给顶层显示。
 *
 * 音频只发到 127.0.0.1 的本机服务，不经过任何第三方；不保存音频文件。
 */

(function () {
  'use strict';
  if (window.__AS_AUDIO_BRIDGE__) return;
  window.__AS_AUDIO_BRIDGE__ = true;

  const IS_TOP = (function () { try { return window.top === window; } catch (e) { return true; } })();
  const MSG = 'autosubtitlesync-audio-bridge-v3';

  const PORTS = []; for (let p = 8765; p <= 8785; p++) PORTS.push(p);
  const TARGET_RATE = 16000;
  const CHUNK_SAMPLES = 8000;          // 0.5 秒一块
  const MAX_INFLIGHT = 3;
  const DEFAULTS = { model: 'base', lang: 'auto', mixed: true, target: 'zh', bilingual: false, window: 6, tail: 1.5, source: 'element' };
  const WORKLET = `class ASBCapture extends AudioWorkletProcessor{
    process(inputs){const c=inputs[0]&&inputs[0][0];if(c&&c.length)this.port.postMessage(new Float32Array(c));return true;}
  } registerProcessor('asb-capture',ASBCapture);`;

  let server = null;                    // {port}
  let video = null;                     // 本 frame 的播放器（若有）
  let ctx = null, srcNode = null, sinkNode = null, procNode = null, mediaStream = null, probeHolder = null;
  let tail = new Float32Array(0), pending = [], pendingSamples = 0;
  let inflight = 0, dropped = 0, pushedBytes = 0, silentWindows = 0;
  let running = false, starting = false, displayOnly = false;
  let seq = 0, cues = [], revision = -1, startedAt = 0, lastAudioAt = 0, lastCueEnd = 0;
  let toast = '', sourceLabel = '播放器元素', boundVideo = null;
  let reportLines = [], probeBusy = false;

  // ------------------------------------------------------------------ 基础
  function req(method, url, body, timeout) {
    return new Promise(function (resolve, reject) {
      if (typeof GM_xmlhttpRequest === 'function') {
        GM_xmlhttpRequest({
          method: method, url: url, data: body, timeout: timeout || 4000,
          headers: body ? { 'Content-Type': 'application/json' } : {},
          onload: function (r) { try { resolve(JSON.parse(r.responseText || '{}')); } catch (e) { reject(new Error('返回内容无法解析')); } },
          onerror: function () { reject(new Error('无法连接本机服务')); },
          ontimeout: function () { reject(new Error('连接本机服务超时')); }
        });
        return;
      }
      fetch(url, { method: method, body: body, headers: { 'Content-Type': 'application/json' } })
        .then(function (r) { return r.json(); }).then(resolve)
        .catch(function () { reject(new Error('浏览器拦截了本地请求：请在 Tampermonkey 中运行本脚本')); });
    });
  }

  async function findServer() {
    if (server) {
      try { await req('GET', 'http://127.0.0.1:' + server.port + '/api/companion/ping', null, 1200); return server; }
      catch (e) { server = null; }
    }
    for (var i = 0; i < PORTS.length; i++) {
      try {
        var x = await req('GET', 'http://127.0.0.1:' + PORTS[i] + '/api/companion/ping', null, 700);
        if (x && x.app === 'AutoSubtitleSync') { server = { port: PORTS[i] }; return server; }
      } catch (e) { /* 端口空闲 */ }
    }
    return null;
  }

  function mainVideo() {
    var all = Array.prototype.slice.call(document.querySelectorAll('video'));
    var best = null, bestScore = 0;
    for (var i = 0; i < all.length; i++) {
      var v = all[i], r = v.getBoundingClientRect();
      var area = r.width * r.height;
      if (!area || area < 20000) continue;
      var score = area * ((!v.paused && !v.ended) ? 2.5 : 1) * (v.readyState >= 2 ? 1.2 : 1);
      if (score > bestScore) { bestScore = score; best = v; }
    }
    return best;
  }
  function localVideoInfo() {
    var v = mainVideo();
    if (!v) return { hasVideo: false, playing: false, area: 0 };
    var r = v.getBoundingClientRect();
    return { hasVideo: true, playing: !v.paused && !v.ended, area: Math.round(r.width * r.height), kind: (v.currentSrc || v.src || '').startsWith('blob:') ? 'blob:' : 'http' };
  }

  function waitFor(fn, ms, step) {
    return new Promise(function (resolve) {
      var t0 = Date.now();
      (function tick() {
        var ok = false; try { ok = !!fn(); } catch (e) { ok = false; }
        if (ok) return resolve(true);
        if (Date.now() - t0 > ms) return resolve(false);
        setTimeout(tick, step || 200);
      })();
    });
  }

  function setToast(t) { toast = t || ''; }

  // ------------------------------------------------------------------ 消息层
  function post(msg) { try { window.top.postMessage(Object.assign({ __as: MSG }, msg), '*'); } catch (e) { } }
  function postTo(win, msg) { try { win.postMessage(Object.assign({ __as: MSG }, msg), '*'); } catch (e) { } }

  var frameRegistry = new Map();      // WindowProxy -> {hasVideo, playing, area, href, ts}

  function announce() {
    if (IS_TOP) return;
    var info = localVideoInfo();
    post({ kind: 'hello', hasVideo: info.hasVideo, playing: info.playing, area: info.area, href: location.href });
  }

  function bestFrame() {
    var now = Date.now(), best = null, bestScore = 0;
    frameRegistry.forEach(function (info, win) {
      if (now - info.ts > 8000) return;
      if (!info.hasVideo) return;
      var score = (info.area || 0) * (info.playing ? 2 : 1);
      if (score > bestScore) { bestScore = score; best = { win: win, info: info }; }
    });
    return best;
  }

  async function pickFrame() {
    for (var i = 0; i < window.frames.length; i++) postTo(window.frames[i], { kind: 'ping' });
    await waitFor(function () { return !!bestFrame(); }, 700, 150);
    return bestFrame();
  }

  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || d.__as !== MSG) return;
    if (IS_TOP) {
      if (d.kind === 'hello') { frameRegistry.set(e.source, { hasVideo: !!d.hasVideo, playing: !!d.playing, area: d.area || 0, href: d.href || '', ts: Date.now() }); refreshPanel(); return; }
      if (d.kind === 'report') { pushReport(d.line); return; }
      if (d.kind === 'status') {
        childStatus = { running: !!d.running, text: d.text || '', cues: d.cues || 0, dropped: d.dropped || 0, src: d.src || '', at: Date.now() };
        refreshPanel(); return;
      }
      if (d.kind === 'stopped') { childStatus = null; refreshPanel(); return; }
      return;
    }
    // 子框架：接受顶层委派
    if (d.kind === 'ping') { announce(); return; }
    if (d.kind === 'run') {
      if (d.action === 'probe') probeShadow();
      else if (d.action === 'start') startCapture(d.source || 'element', d.prefs || {}, true);
      return;
    }
    if (d.kind === 'display') { startDisplay(d.port, d.seq, d.label); return; }
    if (d.kind === 'stop') stop(true);
  });

  var childStatus = null;

  // ------------------------------------------------------------------ PCM 管线
  function toInt16PCM(f32) {
    var ratio = ctx.sampleRate / TARGET_RATE, buf;
    if (tail.length) { buf = new Float32Array(tail.length + f32.length); buf.set(tail, 0); buf.set(f32, tail.length); }
    else buf = f32;
    if (buf.length < 2) { tail = buf; return null; }
    var nOut = Math.floor((buf.length - 1) / ratio);
    if (nOut <= 0) { tail = buf; return null; }
    var out = new Int16Array(nOut);
    for (var i = 0; i < nOut; i++) {
      var p = i * ratio, i0 = Math.floor(p), f = p - i0;
      var a = buf[i0], b = buf[i0 + 1] === undefined ? a : buf[i0 + 1];
      var s = a + (b - a) * f;
      if (s > 1) s = 1; else if (s < -1) s = -1;
      out[i] = (s * 32767) | 0;
    }
    tail = buf.slice(Math.floor(nOut * ratio));
    return out;
  }

  function onSamples(f32) {
    if (!running || displayOnly) return;
    lastAudioAt = Date.now();
    if (!displayOnly && video && video.paused && sourceLabel === '播放器元素') return;
    var pcm = toInt16PCM(f32);
    if (!pcm) return;
    var sum = 0;
    for (var i = 0; i < pcm.length; i += 8) { var x = pcm[i] / 32768; sum += x * x; }
    var rms = Math.sqrt(sum / Math.max(1, Math.ceil(pcm.length / 8)));
    if (rms < 0.0015) silentWindows++; else silentWindows = 0;
    pending.push(pcm); pendingSamples += pcm.length;
    while (pendingSamples >= CHUNK_SAMPLES) {
      var merged = new Int16Array(CHUNK_SAMPLES), off = 0;
      while (off < CHUNK_SAMPLES && pending.length) {
        var head = pending[0], need = CHUNK_SAMPLES - off;
        if (head.length <= need) { merged.set(head, off); off += head.length; pending.shift(); }
        else { merged.set(head.subarray(0, need), off); pending[0] = head.subarray(need); off += need; }
      }
      pendingSamples -= CHUNK_SAMPLES;
      sendChunk(merged);
    }
  }

  function sendChunk(int16) {
    if (!server) return;
    if (inflight >= MAX_INFLIGHT) { dropped++; return; }
    inflight++;
    var url = 'http://127.0.0.1:' + server.port + '/api/browser-audio/push';
    var payload = int16.buffer.slice(0);
    if (typeof GM_xmlhttpRequest === 'function') {
      GM_xmlhttpRequest({
        method: 'POST', url: url, data: payload, binary: true, timeout: 8000,
        headers: { 'Content-Type': 'application/octet-stream' },
        onload: function () { inflight--; pushedBytes += int16.byteLength; },
        onerror: function () { inflight--; dropped++; },
        ontimeout: function () { inflight--; dropped++; }
      });
    } else {
      fetch(url, { method: 'POST', body: payload })
        .then(function () { inflight--; pushedBytes += int16.byteLength; })
        .catch(function () { inflight--; dropped++; });
    }
  }

  async function buildPipeline(stream) {
    var tracks = stream.getAudioTracks();
    if (!tracks.length) throw new Error('这个音源没有音轨。');
    mediaStream = stream;
    ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: TARGET_RATE });
    try { await ctx.resume(); } catch (e) { }
    srcNode = ctx.createMediaStreamSource(new MediaStream(tracks));
    sinkNode = ctx.createGain(); sinkNode.gain.value = 0;      // 静音输出，避免回声
    srcNode.connect(sinkNode); sinkNode.connect(ctx.destination);
    if (ctx.audioWorklet) {
      var url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }));
      await ctx.audioWorklet.addModule(url); URL.revokeObjectURL(url);
      procNode = new AudioWorkletNode(ctx, 'asb-capture');
      procNode.port.onmessage = function (e) { onSamples(e.data); };
      srcNode.connect(procNode); procNode.connect(sinkNode);
    } else {
      procNode = ctx.createScriptProcessor(4096, 1, 1);
      procNode.onaudioprocess = function (e) { onSamples(new Float32Array(e.inputBuffer.getChannelData(0))); };
      srcNode.connect(procNode); procNode.connect(sinkNode);
    }
  }

  function hookResumeIfSuspended() {
    // 子框架里没有用户交互时，AudioContext 可能一直 suspended（Chrome 的自动播放策略），
    // 表现为"明明在播却抓不到声音"。这里挂一次性点击把它唤醒。
    if (!ctx) return;
    setTimeout(function () {
      if (!ctx || ctx.state === 'running') return;
      setToast('这一帧还差一次点击才能开始抓音：在视频画面上点一下即可（浏览器要求）');
      var wake = function () { try { if (ctx) ctx.resume(); } catch (e) { } };
      document.addEventListener('click', wake, { once: true, capture: true });
      window.addEventListener('keydown', wake, { once: true, capture: true });
    }, 900);
  }

  function teardownPipeline() {
    try { if (procNode) procNode.disconnect(); } catch (e) { }
    try { if (srcNode) srcNode.disconnect(); } catch (e) { }
    try { if (sinkNode) sinkNode.disconnect(); } catch (e) { }
    try { if (mediaStream) mediaStream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) { }
    try { if (ctx) ctx.close(); } catch (e) { }
    ctx = srcNode = sinkNode = procNode = mediaStream = null;
    tail = new Float32Array(0); pending = []; pendingSamples = 0;
  }

  // ------------------------------------------------------------------ 会话
  async function openSession(payload) {
    var r = await req('POST', 'http://127.0.0.1:' + server.port + '/api/browser-audio/open', JSON.stringify(payload), 25000);
    if (r.error) throw new Error(r.error);
    seq = r.seq; cues = []; revision = -1; startedAt = Date.now(); lastCueEnd = 0;
    return r;
  }

  function basePayload(p, extra) {
    return Object.assign({
      audio_mode: true, model: p.model, lang: p.lang, mixed: p.mixed, target: p.target,
      bilingual: p.bilingual, window: p.window, tail: p.tail,
      title: document.title || location.hostname, url: location.href
    }, extra || {});
  }

  // 抓音方式 ①：播放器元素（在本 frame 执行）
  async function startElement(p) {
    video = mainVideo();
    if (!video) throw new Error('这个页面（本框架）里没有找到播放器。');
    if (video.paused) { try { await video.play(); } catch (e) { } }
    sourceLabel = '播放器元素';
    await openSession(basePayload(p, { t0: video.currentTime || 0 }));
    var stream = video.captureStream ? video.captureStream() : (video.mozCaptureStream ? video.mozCaptureStream() : null);
    if (!stream) throw new Error('这个浏览器不能直接从播放器抓音（Safari 不支持）。请改用「共享标签页音频」。');
    if (!stream.getAudioTracks().length) throw new Error('播放器没有音轨。');
    await buildPipeline(new MediaStream(stream.getAudioTracks()));
    running = true; displayOnly = false; bindVideoEvents(); ensureOverlay();
    hookResumeIfSuspended();
  }

  // 抓音方式 ②：共享标签页音频（在顶层执行；覆盖 iframe/MSE/blob/跨域/WebAudio）
  async function startTab(p) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
      throw new Error('这个浏览器不支持"共享标签页音频"（需要 Chrome / Edge）。');
    }
    var ds = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true, preferCurrentTab: true });
    var at = ds.getAudioTracks();
    if (!at.length) {
      try { ds.getTracks().forEach(function (t) { t.stop(); }); } catch (e) { }
      throw new Error('你选的那个共享目标里没有音频。请重新选择并勾上「共享标签页音频 / 分享音频」那一项。');
    }
    sourceLabel = '共享标签页音频';
    await openSession(basePayload(p, { t0: 0 }));
    await buildPipeline(new MediaStream([at[0]]));           // 只取音频轨；视频轨留着以免共享被中断
    running = true; displayOnly = false; ensureOverlay();
    hookResumeIfSuspended();
  }

  // 抓音方式 ③：系统声音（服务端 ffmpeg 采集，最万能）
  async function startSystem(p) {
    var d = await req('GET', 'http://127.0.0.1:' + server.port + '/api/system-audio/devices', null, 25000);
    if (!d || !d.devices || !d.devices.length) {
      throw new Error((d && d.hint) || '本机没有找到可用于采集的音频输入设备。');
    }
    var idx = (d.recommended === null || d.recommended === undefined) ? d.devices[0].index : d.recommended;
    var r = await req('POST', 'http://127.0.0.1:' + server.port + '/api/system-audio/open',
      JSON.stringify({ device: idx, model: p.model, lang: p.lang, mixed: p.mixed, target: p.target, bilingual: p.bilingual,
                       window: p.window, tail: p.tail, title: document.title }), 40000);
    if (r.error) throw new Error(r.error);
    seq = r.seq; cues = []; revision = -1; startedAt = Date.now();
    sourceLabel = '系统声音'; displayOnly = true; running = true; ensureOverlay();
    setToast('正在采集系统声音（' + (d.devices.filter(function (x) { return x.index === idx; })[0] || {}).name + '）');
  }

  async function startCapture(source, p, commandedByTop) {
    if (running || starting) return;
    starting = true; reportLines = []; pushReport('正在连接本机服务…');
    try {
      if (!(await findServer())) throw new Error('没有找到本机服务：请先双击运行 AutoSubtitleSync 的启动程序，并保持窗口开着。');
      if (source === 'tab') await startTab(p);
      else if (source === 'system') await startSystem(p);
      else {
        if (!mainVideo()) throw new Error('没找到正在播放的播放器：请先点开视频播放几秒，或在面板里把「抓音来源」换成"共享标签页音频"。');
        await startElement(p);
      }
      reportLines = [];
      setToast('已开始（' + sourceLabel + '）：几秒后开始逐句出字幕。');
      postStatus();
    } catch (e) {
      setToast(e.message || String(e));
      try { teardownPipeline(); } catch (e2) { }
      running = false; displayOnly = false;
      pushReport('✗ ' + (e.message || String(e)));
      if (commandedByTop) post({ kind: 'status', running: false, text: (e.message || String(e)), cues: 0 });
    } finally { starting = false; refreshPanel(); }
  }

  function startDisplay(port, s, label) {
    server = { port: port }; seq = s; cues = []; revision = -1; startedAt = Date.now();
    displayOnly = true; running = true; sourceLabel = label || '服务端采集';
    ensureOverlay(); setToast('字幕来自服务端采集，本页面只负责显示。');
    refreshPanel();
  }

  async function stop(silent) {
    var wasRunning = running;
    running = false;
    try { teardownPipeline(); } catch (e) { }
    displayOnly = false;
    if (server && seq) {
      try { await req('POST', 'http://127.0.0.1:' + server.port + (sourceLabel === '系统声音' ? '/api/system-audio/stop' : '/api/browser-audio/stop'), '{}', 8000); } catch (e) { }
    }
    seq = 0;
    if (overlayHost) overlayHost.style.display = 'none';
    if (!silent && wasRunning) setToast('已停止。本次共收到 ' + cues.length + ' 条字幕。');
    if (!IS_TOP) post({ kind: 'stopped' });
    refreshPanel();
  }

  function bindVideoEvents() {
    if (boundVideo) ['seeked', 'ended', 'pause', 'playing'].forEach(function (k) { boundVideo.removeEventListener(k, onVideoEvent); });
    boundVideo = video;
    if (boundVideo) ['seeked', 'ended', 'pause', 'playing'].forEach(function (k) { boundVideo.addEventListener(k, onVideoEvent); });
  }

  async function onVideoEvent(e) {
    if (!running || displayOnly || sourceLabel !== '播放器元素') return;
    if (e.type === 'ended') { stop(true); setToast('视频播放结束，已停止实时字幕。'); return; }
    if (e.type === 'seeked') {
      setToast('检测到跳转，正在重新对齐时间轴…');
      try { await req('POST', 'http://127.0.0.1:' + server.port + '/api/browser-audio/stop', '{}', 5000); } catch (er) { }
      running = false;
      setTimeout(function () { startCapture('element', prefs(), !IS_TOP); }, 400);
    }
  }

  async function poll() {
    if (!running || !server || !seq) return;
    try {
      var x = await req('GET', 'http://127.0.0.1:' + server.port + '/api/companion/sync?seq=' + seq + '&revision=' + revision, null, 2500);
      if (x.stale) { stop(true); setToast('字幕会话已被其他页面接管。'); return; }
      if (Array.isArray(x.cues) && x.cues.length) {
        cues = x.cues; revision = Number(x.revision || 0);
        lastCueEnd = Number(cues[cues.length - 1].end || 0);
      }
      if (x.error) setToast(String(x.error));
    } catch (e) { setToast(e.message || String(e)); }
    postStatus();
    refreshPanel();
  }

  function postStatus() {
    if (IS_TOP) return;
    post({ kind: 'status', running: running, text: toast, cues: cues.length, dropped: dropped, src: sourceLabel });
  }

  // ------------------------------------------------------------------ 字幕条
  let overlayHost = null, overlayRoot = null, capEl = null, hudEl = null;
  function ensureOverlay() {
    if (overlayHost && overlayHost.isConnected) return;
    overlayHost = document.createElement('div');
    overlayHost.id = 'as-audio-bridge-overlay';
    overlayHost.style.cssText = 'all:initial;position:fixed;z-index:2147483646;pointer-events:none;display:none;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",sans-serif;';
    overlayRoot = overlayHost.attachShadow({ mode: 'open' });
    overlayRoot.innerHTML = '<style>*{box-sizing:border-box}' +
      '.cap{position:absolute;left:5%;right:5%;bottom:7%;display:flex;justify-content:center;text-align:center}' +
      '.text{display:inline-block;max-width:94%;white-space:pre-line;color:#fff;font-size:clamp(18px,2.15vw,31px);font-weight:680;line-height:1.38;text-shadow:0 2px 5px #000,0 0 14px #000;background:rgba(0,0,0,.5);padding:7px 12px;border-radius:8px}' +
      '.hud{position:absolute;right:10px;top:10px;background:rgba(12,17,27,.72);color:#fff;border:1px solid rgba(255,255,255,.15);border-radius:999px;padding:5px 10px;font:600 11px/1.35 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;backdrop-filter:blur(8px);max-width:70%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}</style>' +
      '<div class="cap"><div class="text" id="cap"></div></div><div class="hud" id="hud"></div>';
    document.documentElement.appendChild(overlayHost);
    capEl = overlayRoot.getElementById('cap'); hudEl = overlayRoot.getElementById('hud');
  }

  function placeOverlay() {
    var v = (video && video.isConnected) ? video : mainVideo();
    if (v) video = v;
    if (!overlayHost || !running) { if (overlayHost) overlayHost.style.display = 'none'; return; }
    if (!v) {   // 没有播放器（例如共享标签页/系统声音）：贴在窗口底部
      if (overlayHost.parentNode !== document.documentElement) document.documentElement.appendChild(overlayHost);
      Object.assign(overlayHost.style, { display: 'block', position: 'fixed', left: '0px', bottom: '0px', top: 'auto', width: '100vw', height: '26vh' });
      return;
    }
    var fs = document.fullscreenElement || document.webkitFullscreenElement;
    if (fs) {
      if (overlayHost.parentNode !== fs) fs.appendChild(overlayHost);
      Object.assign(overlayHost.style, { display: 'block', position: 'fixed', left: '0px', top: '0px', width: '100vw', height: '100vh' });
      return;
    }
    if (overlayHost.parentNode !== document.documentElement) document.documentElement.appendChild(overlayHost);
    var r = v.getBoundingClientRect();
    if (r.width < 80 || r.height < 50) { overlayHost.style.display = 'none'; return; }
    Object.assign(overlayHost.style, {
      display: 'block', position: 'fixed', left: Math.max(0, r.left) + 'px', top: Math.max(0, r.top) + 'px',
      width: Math.max(0, r.width) + 'px', height: Math.max(0, r.height) + 'px'
    });
  }

  function render() {
    if (!running) { if (overlayHost) overlayHost.style.display = 'none'; return; }
    placeOverlay();
    if (!capEl) return;
    var c = cues.length ? cues[cues.length - 1] : null;
    var text = c ? String(c.text || '') : (toast || '正在接通声音…');
    capEl.textContent = text;
    capEl.style.display = text ? 'inline-block' : 'none';
    var lag = lastCueEnd ? Math.max(0, (Date.now() - startedAt) / 1000 - lastCueEnd) : 0;
    var state;
    if (displayOnly && sourceLabel === '系统声音') state = '系统声音采集中 · ' + cues.length + ' 条 · 约慢 ' + lag.toFixed(0) + ' 秒';
    else if (dropped > 12) state = '⚠️ 处理跟不上，已丢弃部分声音';
    else if (silentWindows > 40 && sourceLabel === '播放器元素') state = '⚠️ 抓不到声音：该播放器受保护，改用「共享标签页音频」';
    else if (video && video.paused && sourceLabel === '播放器元素') state = '已暂停 · 等待播放';
    else if (!cues.length) state = '正在识别，请等几秒…';
    else state = sourceLabel + ' · 共 ' + cues.length + ' 条 · 约慢 ' + lag.toFixed(0) + ' 秒';
    hudEl.textContent = state;
  }

  // ------------------------------------------------------------------ 影子播放器探测
  function measureStream(v, ms) {
    return new Promise(function (resolve) {
      var ac = null, stream = null, node = null, gain = null, src = null, sum = 0, n = 0, peak = 0;
      try {
        stream = v.captureStream ? v.captureStream() : (v.mozCaptureStream ? v.mozCaptureStream() : null);
        if (!stream || !stream.getAudioTracks().length) return resolve({ ok: false, rms: 0, reason: '没有音轨 / 不支持 captureStream' });
        ac = new (window.AudioContext || window.webkitAudioContext)();
        src = ac.createMediaStreamSource(new MediaStream(stream.getAudioTracks()));
        gain = ac.createGain(); gain.gain.value = 0;
        node = ac.createScriptProcessor(4096, 1, 1);
        node.onaudioprocess = function (e) {
          var d = e.inputBuffer.getChannelData(0);
          for (var i = 0; i < d.length; i++) { var x = d[i]; sum += x * x; var a = x < 0 ? -x : x; if (a > peak) peak = a; n++; }
        };
        src.connect(node); node.connect(gain); gain.connect(ac.destination);
      } catch (e) { return resolve({ ok: false, rms: 0, reason: e.message || String(e) }); }
      setTimeout(function () {
        var rms = n ? Math.sqrt(sum / n) : 0;
        try { node.disconnect(); src.disconnect(); gain.disconnect(); stream.getTracks().forEach(function (t) { t.stop(); }); ac.close(); } catch (e) { }
        resolve({ ok: rms > 0.0008, rms: rms, peak: peak });
      }, ms);
    });
  }

  async function probeShadow() {
    if (probeBusy) return;
    probeBusy = true;
    if (IS_TOP) { reportLines = []; showReport(true); pushReport('正在探测，请保持视频播放（约 15 秒）…'); }
    var LEAD = 60;
    var say = function (t) { pushReport(t); };
    say('AutoSubtitleSync 影子播放器探测报告');
    say('时间：' + new Date().toLocaleString());
    say('页面：' + location.hostname + location.pathname.slice(0, 70));
    var v = mainVideo();
    if (!v) { say('✗ 这个框架里没有播放器（顶层会把探测转交到找到播放器的那个框架）'); probeBusy = false; return; }
    var raw = v.currentSrc || v.src || '';
    say('主播放器：' + (raw.startsWith('blob:') ? 'blob:（第二个播放器通常拿不到）' : (raw.startsWith('http') ? 'http(s) 直链' : (raw ? '其它' : '地址为空'))));
    say('  时长 ' + (isFinite(v.duration) && v.duration > 0 ? v.duration.toFixed(1) + ' 秒' : '未知（可能是直播）') +
        ' · readyState=' + v.readyState + ' · ' + (v.paused ? '暂停' : '播放中'));
    var canCapture = !!(v.captureStream || v.mozCaptureStream);
    say('抓音能力：' + (canCapture ? '支持 captureStream ✓' : '不支持 ✗'));
    if (!canCapture) { say('结论：该浏览器不支持从播放器抓音，请改用「共享标签页音频」或「系统声音」。'); probeBusy = false; return; }
    say('① 测主播放器的声音（3 秒）…（如果你是在子框架里测，先在这一帧的画面里点一下，否则浏览器会拦着不让取音）');
    var m = await measureStream(v, 3000);
    say('   主播放器 ' + (m.ok ? '有声音 ✓' : '几乎无声 ✗') + '  RMS=' + m.rms.toFixed(4) + (m.reason ? '  ' + m.reason : ''));
    if (!m.ok) {
      say('');
      say('结论：这个播放器抓不到声音（跨域 / DRM）——影子播放器也一样抓不到。');
      say('建议：改用「共享标签页音频」或「系统声音」这两种抓音方式。');
      probeBusy = false; return;
    }
    say('② 在页面内克隆一个静音播放器…');
    var pv = document.createElement('video');
    pv.muted = true; pv.volume = 0; pv.playsInline = true; pv.preload = 'auto';
    pv.setAttribute('playsinline', ''); pv.setAttribute('muted', '');
    pv.style.cssText = 'position:fixed;right:2px;bottom:2px;width:2px;height:2px;opacity:0.01;pointer-events:none;z-index:0';
    if (v.crossOrigin) pv.crossOrigin = v.crossOrigin;
    var log = [];
    ['loadedmetadata', 'canplay', 'playing', 'seeked', 'waiting', 'stalled', 'error'].forEach(function (k) { pv.addEventListener(k, function () { log.push(k); }); });
    try { pv.src = raw; } catch (e) { say('   ✗ 无法设置地址：' + (e.message || e)); }
    document.body.appendChild(pv); probeHolder = pv;
    try { pv.load(); } catch (e) { }
    if (!(await waitFor(function () { return pv.readyState >= 1 || pv.error; }, 6000))) {
      say('   ✗ 第二个播放器 6 秒内没加载出内容（该站多半用 blob:/MSE）');
      say('   事件：' + (log.join(', ') || '（无）'));
      say('');
      say('结论：这个网站起不了影子播放器（前瞻做不到）。');
      say('建议：改用「共享标签页音频」，字幕仍可用，只是断句偏保守。');
      cleanupProbe(); probeBusy = false; return;
    }
    if (pv.error) {
      say('   ✗ 第二个播放器报错 code=' + pv.error.code);
      say('');
      say('结论：这个站不允许第二路加载同一地址。建议改用「共享标签页音频」。');
      cleanupProbe(); probeBusy = false; return;
    }
    var seekEnd = (pv.seekable && pv.seekable.length) ? pv.seekable.end(pv.seekable.length - 1) : 0;
    say('   元数据 OK ✓  可跳转范围 0 → ' + seekEnd.toFixed(1) + ' 秒');
    if (!seekEnd) { say('   结论：内容不能跳到"未来"（直播/流式），影子播放器无从提前。'); cleanupProbe(); probeBusy = false; return; }
    var target = Math.min(seekEnd, (v.currentTime || 0) + LEAD);
    say('③ 跳到 +' + LEAD + ' 秒（目标 ' + target.toFixed(1) + ' 秒）并播放…');
    try { pv.currentTime = target; } catch (e) { say('   ✗ 跳转失败：' + (e.message || e)); }
    if (!(await waitFor(function () { return Math.abs(pv.currentTime - target) < 2 && pv.readyState >= 2; }, 8000))) {
      say('   ✗ 跳转后没就绪（currentTime=' + pv.currentTime.toFixed(1) + '）');
      say('结论：不可用，建议改用「共享标签页音频」。');
      cleanupProbe(); probeBusy = false; return;
    }
    try { await pv.play(); } catch (e) { say('   ⚠ 自动播放被拦：' + (e.message || e)); }
    var t0 = pv.currentTime;
    var moving = await waitFor(function () { return !pv.paused && pv.currentTime > t0 + 0.6; }, 5000);
    say('   ' + (moving ? '正在播放 ✓' : '未能播放 ✗') + '  currentTime=' + pv.currentTime.toFixed(1));
    say('④ 测影子播放器的声音（4 秒，静音播放，你不会听到）…');
    var m2 = await measureStream(pv, 4000);
    say('   影子播放器 ' + (m2.ok ? '有声音 ✓' : '几乎无声 ✗') + '  RMS=' + m2.rms.toFixed(4) + (m2.reason ? '  ' + m2.reason : ''));
    cleanupProbe();
    say('事件轨迹：' + (log.join(', ') || '（无）'));
    say('');
    if (moving && m2.ok) {
      say('结论：✓ 这个网站可以用影子播放器做前瞻（领先约 ' + LEAD + ' 秒）。');
      say('代价：页面会同时拉两路流，部分站点可能把进度写进观看历史。');
    } else if (moving && !m2.ok) {
      say('结论：能提前播放，但第二个播放器抓不到声音。建议改用「共享标签页音频」。');
    } else {
      say('结论：这个网站起不了会播放的影子播放器。建议改用「共享标签页音频」。');
    }
    probeBusy = false;
  }

  function cleanupProbe() {
    try { if (probeHolder) { probeHolder.pause(); probeHolder.src = ''; probeHolder.remove(); } } catch (e) { }
    probeHolder = null;
  }

  // ------------------------------------------------------------------ 顶层面板
  let panelRoot = null, msgEl = null, pillDot = null, pillText = null, startBtn = null, stopBtn = null, statsEl = null;
  let probeOut = null, copyProbeBtn = null, targetEl = null;

  function prefs() {
    if (!panelRoot) return Object.assign({}, DEFAULTS, loadPrefs());
    var $ = function (id) { return panelRoot.getElementById(id); };
    var v = {
      model: $('model').value, target: $('target').value, window: Number($('win').value),
      lang: $('lang').value, bilingual: $('bilingual').checked, source: $('source').value
    };
    v.tail = v.window <= 4 ? 1.0 : (v.window >= 8 ? 2.0 : 1.5);
    v.mixed = v.lang === 'auto';
    return v;
  }
  function loadPrefs() { try { return JSON.parse(localStorage.getItem('autosubtitlesync_audio_bridge_v3') || '{}'); } catch (e) { return {}; } }
  function savePrefs() {
    if (!panelRoot) return;
    try { localStorage.setItem('autosubtitlesync_audio_bridge_v3', JSON.stringify(prefs())); } catch (e) { }
  }

  function pushReport(line) {
    reportLines.push(line);
    if (IS_TOP) { showReport(true); if (probeOut) { probeOut.textContent = reportLines.join('\n'); probeOut.scrollTop = probeOut.scrollHeight; } }
    else { post({ kind: 'report', line: line }); }
  }
  function showReport(on) { if (probeOut) probeOut.classList.toggle('hidden', !on); if (copyProbeBtn) copyProbeBtn.classList.toggle('hidden', !on); }
  function setMsg(t, isErr) { if (!msgEl) return; msgEl.textContent = t; msgEl.className = 'msg' + (isErr ? ' err' : ''); }

  function buildPanel() {
    var host = document.createElement('div');
    host.id = 'as-audio-bridge-host';
    host.style.cssText = 'all:initial;position:fixed;right:18px;bottom:18px;z-index:2147483647;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif;';
    var sh = host.attachShadow({ mode: 'open' });
    sh.innerHTML = '<style>*{box-sizing:border-box}button,select{font:inherit}' +
      '.pill{border:1px solid rgba(255,255,255,.15);background:#111827;color:#fff;border-radius:999px;padding:10px 14px;box-shadow:0 8px 28px rgba(15,23,42,.28);font-size:13px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:8px}' +
      '.dot{width:8px;height:8px;border-radius:50%;background:#94a3b8}.dot.ok{background:#34d399}.dot.wait{background:#f59e0b}.dot.bad{background:#fb7185}' +
      '.panel{position:absolute;right:0;bottom:48px;width:340px;background:rgba(255,255,255,.985);color:#111827;border:1px solid #e5e7eb;border-radius:16px;padding:14px;box-shadow:0 18px 50px rgba(15,23,42,.22);display:none}' +
      '.panel.open{display:block}.title{font-size:14px;font-weight:800;margin-bottom:3px}.sub{font-size:11px;color:#6b7280;line-height:1.5;margin-bottom:10px}' +
      '.grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}.field{display:flex;flex-direction:column;gap:5px}.field.full{grid-column:1/-1}' +
      '.label{font-size:11px;font-weight:700;color:#4b5563}select{width:100%;border:1px solid #dfe3e8;border-radius:9px;background:#fff;padding:8px;font-size:12px;color:#111827}' +
      '.check{display:flex;align-items:center;gap:7px;font-size:12px;color:#374151;padding-top:4px}' +
      '.primary,.secondary,.stop{width:100%;border:0;border-radius:10px;padding:10px 12px;font-size:12px;font-weight:750;cursor:pointer;margin-top:9px}' +
      '.primary{background:#2563eb;color:#fff}.secondary{background:#f1f3f6;color:#252a33}.stop{background:#fff0ee;color:#b42318}' +
      '.primary:disabled{opacity:.5}.msg{font-size:11px;color:#6b7280;line-height:1.55;margin-top:9px}.msg.err{color:#b42318}' +
      '.stats{margin-top:10px;padding:9px 10px;border-radius:10px;background:#f7f8fa;border:1px solid #e9ebef;font-size:11px;color:#505866;line-height:1.55}' +
      '.report{max-height:220px;overflow:auto;background:#0b1220;color:#d7e3f4;border-radius:10px;padding:10px;font:11px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-all;margin-top:9px}' +
      '.hint{font-size:10.5px;color:#8a93a3;line-height:1.5;margin-top:7px}.hidden{display:none!important}</style>' +
      '<button class="pill" id="pill"><span class="dot" id="dot"></span><span id="pillText">实时字幕（音频桥）</span></button>' +
      '<div class="panel" id="panel">' +
      '<div class="title">AutoSubtitleSync · 浏览器音频桥</div>' +
      '<div class="sub">把正在播放的声音送给你电脑上的 AutoSubtitleSync 做实时字幕。声音只发到本机 127.0.0.1。</div>' +
      '<div class="grid">' +
      '<label class="field full"><span class="label">抓音来源</span><select id="source">' +
        '<option value="element">播放器元素（默认，可做前瞻）</option>' +
        '<option value="tab">共享标签页音频（最兼容）</option>' +
        '<option value="system">系统声音（最万能，需装虚拟声卡）</option></select></label>' +
      '<label class="field"><span class="label">识别模型</span><select id="model">' +
        '<option value="tiny">tiny · 最快</option><option value="base">base · 推荐</option><option value="small">small · 最准</option></select></label>' +
      '<label class="field"><span class="label">输出字幕</span><select id="target">' +
        '<option value="zh">中文</option><option value="original">保持原语言</option><option value="en">English</option><option value="de">Deutsch</option><option value="fr">Français</option><option value="es">Español</option></select></label>' +
      '<label class="field"><span class="label">字幕快慢</span><select id="win">' +
        '<option value="4">快（延迟小）</option><option value="6">均衡 · 推荐</option><option value="8">准（延迟更大）</option></select></label>' +
      '<label class="field"><span class="label">语言检测</span><select id="lang">' +
        '<option value="auto">自动（含混合）</option><option value="en">English</option><option value="zh">中文</option><option value="de">Deutsch</option><option value="fr">Français</option><option value="es">Español</option></select></label>' +
      '<label class="field full"><span class="check"><input type="checkbox" id="bilingual"> 原文 + 翻译 双语显示</span></label>' +
      '</div>' +
      '<button class="primary" id="start">开始实时字幕</button>' +
      '<button class="stop hidden" id="stop">停止</button>' +
      '<button class="secondary" id="open-ui">打开本机控制台</button>' +
      '<button class="secondary" id="probe">测试影子播放器（可选）</button>' +
      '<pre class="report hidden" id="probeOut"></pre>' +
      '<button class="secondary hidden" id="copyProbe">复制报告</button>' +
      '<div class="stats hidden" id="stats"></div>' +
      '<div class="hint" id="hint">「影子播放器」是给字幕加前瞻用的：页面内静默多播一路提前 60 秒的声音，让断句像导入字幕一样自然。不是所有网站都行——点上面按钮实测。</div>' +
      '<div class="msg" id="msg">第一次用：先双击运行 AutoSubtitleSync 启动程序，等它显示"已启动"，再点上面的按钮。</div>' +
      '</div>';
    document.documentElement.appendChild(host);
    panelRoot = sh;
    var $ = function (id) { return sh.getElementById(id); };
    msgEl = $('msg'); pillDot = $('dot'); pillText = $('pillText'); startBtn = $('start'); stopBtn = $('stop');
    statsEl = $('stats'); probeOut = $('probeOut'); copyProbeBtn = $('copyProbe'); targetEl = $('hint');
    var saved = Object.assign({}, DEFAULTS, loadPrefs());
    $('source').value = saved.source || 'element'; $('model').value = saved.model; $('target').value = saved.target;
    $('win').value = String(saved.window); $('lang').value = saved.lang; $('bilingual').checked = !!saved.bilingual;
    ['source', 'model', 'target', 'win', 'lang', 'bilingual'].forEach(function (id) { $(id).addEventListener('change', function () { savePrefs(); refreshPanel(); }); });
    $('pill').addEventListener('click', function () { $('panel').classList.toggle('open'); if ($('panel').classList.contains('open')) pickFrame().then(refreshPanel); });
    startBtn.addEventListener('click', startTop);
    stopBtn.addEventListener('click', stopTop);
    $('open-ui').addEventListener('click', async function () {
      var s = server || await findServer();
      if (!s) { setMsg('没有找到本机服务：请先运行 AutoSubtitleSync 启动程序。', true); return; }
      window.open('http://127.0.0.1:' + s.port + '/', '_blank', 'noopener');
    });
    $('probe').addEventListener('click', function () { if (!probeBusy) probeTop(); });
    copyProbeBtn.addEventListener('click', async function () {
      try { await navigator.clipboard.writeText(reportLines.join('\n')); copyProbeBtn.textContent = '已复制 ✓'; setTimeout(function () { copyProbeBtn.textContent = '复制报告'; }, 1500); }
      catch (e) { setMsg('复制失败，请手动选中报告文字复制。', true); }
    });
  }

  async function startTop() {
    if (running || starting) return;
    var p = prefs();
    setMsg('正在准备…', false);
    try {
      if (p.source === 'element' && !mainVideo()) {
        var pick = await pickFrame();
        if (!pick) {
          setMsg('这个页面里没找到播放器。如果视频确实在播，把「抓音来源」换成"共享标签页音频"再试——那个不需要认识播放器。', true);
          return;
        }
        setMsg('播放器在子框架里，已转交那边抓音…', false);
        postTo(pick.win, { kind: 'run', action: 'start', source: 'element', prefs: p });
        return;
      }
      await startCapture(p.source, p, false);
      if (p.source === 'tab' || p.source === 'system') {
        var pick2 = await pickFrame();
        if (pick2) postTo(pick2.win, { kind: 'display', port: server.port, seq: seq, label: sourceLabel });
      }
      setMsg(sourceLabel === '系统声音' ? '系统声音采集中：字幕会显示在播放器上（装了 BlackHole 才有声音输入）。' : '已开始：声音正在送到本机识别。', false);
    } catch (e) {
      setMsg(e.message || String(e), true);
    }
    refreshPanel();
  }

  async function stopTop() {
    var pick = await pickFrame();
    if (pick) postTo(pick.win, { kind: 'stop' });
    await stop(false);
    setMsg('已停止。', false);
  }

  async function probeTop() {
    if (mainVideo()) { await probeShadow(); return; }
    var pick = await pickFrame();
    if (!pick) { pushReport('✗ 这个页面里没找到播放器：视频可能还没开始播，或者抓音来源该换成"共享标签页音频"。'); return; }
    pushReport('播放器在子框架里（' + (pick.info.href || '').slice(0, 70) + '），已转交那边探测…');
    postTo(pick.win, { kind: 'run', action: 'probe' });
  }

  function refreshPanel() {
    if (!IS_TOP || !panelRoot) return;
    startBtn.classList.toggle('hidden', running || starting);
    stopBtn.classList.toggle('hidden', !(running || starting));
    startBtn.disabled = starting;
    var childRunning = childStatus && childStatus.running && (Date.now() - childStatus.at < 15000);
    pillText.textContent = (running || childRunning) ? '实时字幕 运行中' : '实时字幕（音频桥）';
    pillDot.className = 'dot ' + ((running || childRunning) ? 'ok' : '');
    var showStats = running || childRunning;
    statsEl.classList.toggle('hidden', !showStats);
    if (showStats) {
      var lines = [];
      if (running) lines.push('本页抓音（' + sourceLabel + '）：已送 ' + (pushedBytes / 32000).toFixed(1) + ' 秒，字幕 ' + cues.length + ' 条' + (dropped ? '，丢弃 ' + dropped + ' 块' : ''));
      if (childRunning) lines.push('子框架抓音（' + (childStatus.src || '') + '）：字幕 ' + childStatus.cues + ' 条' + (childStatus.dropped ? '，丢弃 ' + childStatus.dropped + ' 块' : ''));
      statsEl.innerHTML = lines.join('<br>');
    }
    if (!running) {
      var framesWithVideo = 0;
      frameRegistry.forEach(function (info) { if (info.hasVideo) framesWithVideo++; });
      if (mainVideo()) targetEl.textContent = '本页找到播放器，可直接用「播放器元素」抓音。';
      else if (framesWithVideo) targetEl.textContent = '检测到播放器在子框架里（' + framesWithVideo + ' 个），开始时会自动交给那边抓音。';
      else targetEl.textContent = '还没检测到播放器。视频要真的在播；找不到就用「共享标签页音频」。';
    }
  }

  // ------------------------------------------------------------------ 启动
  if (IS_TOP) {
    buildPanel();
    refreshPanel();
    setInterval(function () { placeOverlay(); render(); }, 120);
    setInterval(function () { poll(); }, 700);
    setInterval(function () { refreshPanel(); }, 1200);
    setInterval(function () {
      var v = mainVideo();
      if (running && !displayOnly && sourceLabel === '播放器元素' && v && v !== video) {
        video = v; bindVideoEvents(); setToast('检测到新的播放器，已切换…');
      }
    }, 2500);
  } else {
    setInterval(function () { placeOverlay(); render(); }, 120);
    setInterval(function () { poll(); }, 700);
    setInterval(function () { announce(); }, 3000);
    setInterval(function () { if (running) postStatus(); }, 1500);
    announce();
    // 播放器可能出现得比较晚（单页应用/懒加载），持续留意
    setInterval(function () {
      var info = localVideoInfo();
      if (info.hasVideo && !video) { video = mainVideo(); ensureOverlay(); }
    }, 2000);
    window.addEventListener('beforeunload', function () { if (running) post({ kind: 'stopped' }); });
  }

  window.addEventListener('beforeunload', function () {
    if (running && server && seq) { try { navigator.sendBeacon('http://127.0.0.1:' + server.port + '/api/browser-audio/stop', ''); } catch (e) { } }
  });
})();
