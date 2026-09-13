// ==UserScript==
// @name         AutoSubtitleSync Browser Companion · In-page Overlay
// @namespace    autosubtitlesync.local
// @version      2.0.0
// @description  Detect HTML5 video inside nested/cross-origin iframe players, prepare Semantic Look-ahead subtitles on your Mac, and render them over the real player.
// @match        http://*/*
// @match        https://*/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      127.0.0.1
// ==/UserScript==

(function () {
  'use strict';

  const NS='autosubtitlesync-v76';
  const IS_TOP=(window.top===window.self);
  const FRAME_ID=`f_${Math.random().toString(36).slice(2)}_${Date.now().toString(36)}`;
  const PORTS=Array.from({length:21},(_,i)=>8765+i);
  const KEY='autosubtitlesync_companion_prefs_v3';
  const TOKEN_KEY='autosubtitlesync_bridge_token_v1';
  function loadToken(){try{let t=GM_getValue(TOKEN_KEY,'');if(!t){t=(crypto.randomUUID?crypto.randomUUID():`${Date.now()}_${Math.random().toString(36).slice(2)}`);GM_setValue(TOKEN_KEY,t);}return t;}catch(_){return `${Date.now()}_${Math.random().toString(36).slice(2)}`;}}
  const BRIDGE_TOKEN=loadToken();
  const DEFAULTS={target:'zh',lookahead:90,bilingual:false,model:'small'};
  const REPORT_MS=700;
  const $doc=document;

  let server=null;
  let currentVideo=null;
  const boundVideos=new WeakSet();
  let agentActive=false,sessionSeq=0,sessionStart=0,ready=false,buffering=false,intendedPlay=false;
  let cues=[],revision=-1,stableThrough=0,processedTime=0,currentLookahead=90,isLive=false,lastServerError='',syncBusy=false,restartTimer=null;
  let overlayHost=null,overlayRoot=null,cap=null,hud=null;

  function whenReady(fn){
    if(document.documentElement) fn();
    else document.addEventListener('DOMContentLoaded',fn,{once:true});
  }
  function getPrefs(){try{return Object.assign({},DEFAULTS,GM_getValue(KEY,{}));}catch(_){return {...DEFAULTS};}}
  function savePrefs(p){try{GM_setValue(KEY,p);}catch(_){}}
  function fmt(t){t=Math.max(0,Number(t)||0);const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=Math.floor(t%60);return h?`${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`:`${m}:${String(s).padStart(2,'0')}`;}
  function req(method,url,body,timeout=2200){
    return new Promise((resolve,reject)=>GM_xmlhttpRequest({method,url,timeout,headers:body?{'Content-Type':'application/json'}:{},data:body?JSON.stringify(body):undefined,onload:r=>{let x={};try{x=JSON.parse(r.responseText||'{}')}catch(_){};if(r.status>=200&&r.status<300)resolve(x);else reject(new Error(x.error||`HTTP ${r.status}`));},onerror:()=>reject(new Error('无法连接本机 AutoSubtitleSync')),ontimeout:()=>reject(new Error('连接本机 AutoSubtitleSync 超时'))}));
  }
  async function findServer(){
    if(server)return server;
    for(const port of PORTS){try{const x=await req('GET',`http://127.0.0.1:${port}/api/companion/ping`,null,300);if(x&&x.app==='AutoSubtitleSync'){server={port,...x};return server;}}catch(_){}}
    return null;
  }

  function mainVideo(){
    if(currentVideo&&currentVideo.isConnected){const r=currentVideo.getBoundingClientRect();if(r.width>80&&r.height>50)return currentVideo;}
    const vids=[...document.querySelectorAll('video')].filter(v=>{const r=v.getBoundingClientRect();return r.width>80&&r.height>50;});
    currentVideo=vids.sort((a,b)=>{const ra=a.getBoundingClientRect(),rb=b.getBoundingClientRect();return rb.width*rb.height-ra.width*ra.height;})[0]||null;
    if(currentVideo)bindVideo(currentVideo);
    return currentVideo;
  }
  function bindVideo(v){
    if(boundVideos.has(v))return;boundVideos.add(v);
    v.addEventListener('play',()=>{if(agentActive&&!buffering)intendedPlay=true;});
    v.addEventListener('pause',()=>{if(agentActive&&!buffering)intendedPlay=false;});
    v.addEventListener('seeked',()=>{if(!agentActive)return;clearTimeout(restartTimer);restartTimer=setTimeout(()=>maybeRestartForSeek(v),650);});
  }
  function isHttp(u){return /^https?:\/\//i.test(String(u||''));}
  function mediaScore(u){
    const x=String(u||'').toLowerCase();
    if(!isHttp(x))return -1;
    let s=0;
    if(/\.m3u8(?:[?#]|$)/.test(x)||x.includes('m3u8'))s=100;
    else if(/\.mpd(?:[?#]|$)/.test(x)||x.includes('manifest.mpd'))s=95;
    else if(/\.(mp4|m4v)(?:[?#]|$)/.test(x))s=80;
    else if(/\.webm(?:[?#]|$)/.test(x))s=70;
    else if(x.includes('manifest')||x.includes('playlist'))s=55;
    if(/(?:^|[\/_-])(ads?|preroll|vast)(?:[\/_-]|$)/.test(x))s-=45;
    return s;
  }
  function mediaHint(){
    const v=mainVideo();const candidates=[];
    if(v){
      if(isHttp(v.currentSrc))candidates.push([v.currentSrc,130,'video.currentSrc']);
      if(isHttp(v.src))candidates.push([v.src,125,'video.src']);
      for(const s of v.querySelectorAll('source[src]'))if(isHttp(s.src))candidates.push([s.src,120,'source']);
    }
    try{
      const entries=performance.getEntriesByType('resource').slice(-500);
      for(let i=entries.length-1;i>=0;i--){const u=entries[i].name;const sc=mediaScore(u);if(sc>0)candidates.push([u,sc+(i/Math.max(1,entries.length))*5,'performance']);}
    }catch(_){}
    candidates.sort((a,b)=>b[1]-a[1]);
    const hit=candidates[0];return hit?{url:hit[0],kind:hit[2],score:hit[1]}:{url:'',kind:'',score:0};
  }
  function frameContext(topUrl='',topTitle=''){
    const v=mainVideo();const r=v?v.getBoundingClientRect():{width:0,height:0};const mh=mediaHint();
    return {frame_id:FRAME_ID,url:isHttp(location.href)?location.href:(isHttp(topUrl)?topUrl:''),frame_url:location.href,top_url:topUrl,title:document.title||topTitle||'在线视频',top_title:topTitle||'',current_time:v&&Number.isFinite(v.currentTime)?Math.max(0,v.currentTime):0,duration:v&&Number.isFinite(v.duration)?Math.max(0,v.duration):0,media_url:mh.url,media_kind:mh.kind,referer:isHttp(location.href)?location.href:(topUrl||''),user_agent:navigator.userAgent||'',has_video:!!v,area:Math.max(0,r.width*r.height),paused:v?!!v.paused:true,ready_state:v?Number(v.readyState||0):0};
  }
  function reportFrame(){
    const c=frameContext();
    const {media_url,user_agent,referer,...safe}=c;
    try{window.top.postMessage({ns:NS,token:BRIDGE_TOKEN,type:'frame-report',...safe,has_media_hint:!!media_url},'*');}catch(_){}
  }
  setInterval(reportFrame,REPORT_MS);setTimeout(reportFrame,250);

  function ensureOverlay(){
    if(overlayHost&&overlayHost.isConnected)return;
    if(!document.documentElement)return;
    overlayHost=document.createElement('div');overlayHost.id='autosubtitlesync-caption-overlay-v76';overlayHost.style.cssText='all:initial;position:fixed;z-index:2147483646;pointer-events:none;display:none;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",sans-serif;';
    overlayRoot=overlayHost.attachShadow({mode:'open'});
    overlayRoot.innerHTML=`<style>*{box-sizing:border-box}.cap{position:absolute;left:5%;right:5%;bottom:7%;display:flex;justify-content:center;text-align:center}.text{display:inline-block;max-width:94%;white-space:pre-line;color:#fff;font-size:clamp(18px,2.15vw,31px);font-weight:680;line-height:1.38;text-shadow:0 2px 5px #000,0 0 14px #000;background:rgba(0,0,0,.48);padding:7px 12px;border-radius:8px}.hud{position:absolute;right:10px;top:10px;background:rgba(12,17,27,.72);color:#fff;border:1px solid rgba(255,255,255,.15);border-radius:999px;padding:5px 9px;font:600 11px/1.2 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;backdrop-filter:blur(8px)}.hud.ready{opacity:.42}</style><div class="cap"><div class="text" id="caption"></div></div><div class="hud" id="hud"></div>`;
    document.documentElement.appendChild(overlayHost);cap=overlayRoot.getElementById('caption');hud=overlayRoot.getElementById('hud');
  }
  function placeOverlay(){
    ensureOverlay();const v=mainVideo();if(!overlayHost||!v||!agentActive){if(overlayHost)overlayHost.style.display='none';return;}
    const fs=document.fullscreenElement;
    if(fs&&fs.contains(v)){
      if(overlayHost.parentNode!==fs)fs.appendChild(overlayHost);
      Object.assign(overlayHost.style,{display:'block',position:'fixed',left:'0px',top:'0px',width:'100vw',height:'100vh',zIndex:'2147483646'});
    }else{
      if(overlayHost.parentNode!==document.documentElement)document.documentElement.appendChild(overlayHost);
      const r=v.getBoundingClientRect();if(r.width<80||r.height<50){overlayHost.style.display='none';return;}
      Object.assign(overlayHost.style,{display:'block',position:'fixed',left:`${Math.max(0,r.left)}px`,top:`${Math.max(0,r.top)}px`,width:`${Math.max(0,r.width)}px`,height:`${Math.max(0,r.height)}px`,zIndex:'2147483646'});
    }
  }
  document.addEventListener('fullscreenchange',()=>setTimeout(placeOverlay,80));
  function cueAt(t){let lo=0,hi=cues.length-1,best=null;while(lo<=hi){const m=(lo+hi)>>1,c=cues[m];if(t<c.start)hi=m-1;else{best=c;lo=m+1;}}return best&&t<=best.end+.22?best:null;}
  function renderCaption(){
    if(!agentActive){if(overlayHost)overlayHost.style.display='none';return;}const v=mainVideo();if(!v)return;placeOverlay();const c=cueAt(v.currentTime);cap.textContent=c?String(c.text||''):'';cap.style.display=c?'inline-block':'none';const lead=Math.max(0,stableThrough-v.currentTime);
    if(lastServerError){hud.textContent='字幕服务错误';hud.className='hud';}else if(!ready){hud.textContent=`准备字幕 · ${Math.floor(lead)} / ${Math.floor(currentLookahead)}s`;hud.className='hud';}else if(buffering){hud.textContent=`补充字幕缓冲 · Ahead ${Math.floor(lead)}s`;hud.className='hud';}else{hud.textContent=`字幕已就绪 · Ahead ${Math.floor(lead)}s`;hud.className='hud ready';}
  }
  async function safePlay(v){try{await v.play();return true;}catch(_){postStatus('字幕已准备好；浏览器阻止自动恢复，请手动点播放。',false);return false;}}
  async function manageBuffer(){
    if(!agentActive)return;const v=mainVideo();if(!v)return;const lead=stableThrough-v.currentTime;const low=Math.max(15,currentLookahead*.5),resume=Math.max(20,currentLookahead*.90);
    if(isLive){ready=true;buffering=false;return;}
    if(!ready){if(lead>=currentLookahead||(lastServerError===''&&processedTime>0&&!syncBusy&&lead>=Math.max(20,currentLookahead*.8))){ready=true;buffering=false;if(intendedPlay)await safePlay(v);}else if(!v.paused){intendedPlay=true;buffering=true;v.pause();}return;}
    if(lead<low&&!lastServerError){if(!v.paused){intendedPlay=true;buffering=true;v.pause();}}else if(buffering&&lead>=resume){buffering=false;if(intendedPlay)await safePlay(v);}
  }
  function postStatus(message='',error=false){
    const v=mainVideo();const now=v?Number(v.currentTime||0):0;const lead=Math.max(0,stableThrough-now);try{window.top.postMessage({ns:NS,token:BRIDGE_TOKEN,type:'agent-status',frame_id:FRAME_ID,active:agentActive,ready,buffering,error:error?message:lastServerError,message:message||'',lead,stable_through:stableThrough,processed_time:processedTime,count:cues.length,current_time:now,frame_url:location.href},'*');}catch(_){}
  }
  async function sync(){
    if(!agentActive||!server||!sessionSeq||syncBusy)return;syncBusy=true;
    try{const x=await req('GET',`http://127.0.0.1:${server.port}/api/companion/sync?seq=${sessionSeq}&revision=${revision}`,null,1500);if(x.stale){lastServerError='字幕会话已被其他页面替换';agentActive=false;postStatus(lastServerError,true);return;}lastServerError=x.error||'';stableThrough=Number(x.stable_through||0);processedTime=Number(x.processed_time||0);currentLookahead=Number(x.lookahead||currentLookahead);isLive=!!x.is_live;if(Array.isArray(x.cues)){cues=x.cues;revision=Number(x.revision||0);}else revision=Number(x.revision??revision);await manageBuffer();postStatus(lastServerError,lastServerError!=='');}catch(e){lastServerError=e.message||String(e);postStatus(lastServerError,true);}finally{syncBusy=false;}
  }
  async function startAgent(payload){
    if(payload.frame_id&&payload.frame_id!==FRAME_ID)return;
    server=await findServer();if(!server)throw new Error('没有检测到本机 AutoSubtitleSync。请先运行 AutoSubtitleSync.command。');
    const v=mainVideo();if(!v)throw new Error('这个播放器 frame 里没有检测到 HTML5 video。');
    const p=payload.prefs||DEFAULTS;intendedPlay=!v.paused;v.pause();buffering=true;ready=false;cues=[];revision=-1;lastServerError='';currentLookahead=Number(p.lookahead||90);
    const c=frameContext(payload.top_url||'',payload.top_title||'');stableThrough=c.current_time;processedTime=c.current_time;
    const x=await req('POST',`http://127.0.0.1:${server.port}/api/companion/open`,{...c,target:p.target||'zh',lookahead:currentLookahead,bilingual:!!p.bilingual,model:p.model||'small',lang:'auto',mixed:true,session_mode:'auto'},6500);
    sessionSeq=Number(x.seq||0);sessionStart=Number(x.start_at||c.current_time);agentActive=true;placeOverlay();postStatus(`已接管播放器 frame；从 ${fmt(sessionStart)} 开始建立 ${currentLookahead}s Semantic Look-ahead。`,false);
  }
  async function stopAgent(silent=false){
    if(server&&agentActive){try{await req('POST',`http://127.0.0.1:${server.port}/api/online/stop`,{},1800);}catch(_){}}
    agentActive=false;ready=false;buffering=false;sessionSeq=0;cues=[];revision=-1;if(overlayHost)overlayHost.style.display='none';if(!silent)postStatus('本页字幕已停止。',false);
  }
  async function restartAt(t){
    if(!agentActive)return;const v=mainVideo();if(!v)return;const shouldPlay=!v.paused||intendedPlay;v.pause();intendedPlay=shouldPlay;buffering=true;ready=false;
    try{await req('POST',`http://127.0.0.1:${server.port}/api/online/stop`,{},1800);}catch(_){}await new Promise(r=>setTimeout(r,250));
    const p=getPrefs(),c=frameContext();c.current_time=Math.max(0,t);c.top_url='';currentLookahead=Number(p.lookahead||90);cues=[];revision=-1;stableThrough=t;processedTime=t;
    try{const x=await req('POST',`http://127.0.0.1:${server.port}/api/companion/open`,{...c,target:p.target,lookahead:p.lookahead,bilingual:!!p.bilingual,model:p.model||'small',lang:'auto',mixed:true,session_mode:'auto'},6500);sessionSeq=Number(x.seq||0);sessionStart=Number(x.start_at||t);postStatus(`检测到跳转，已从 ${fmt(t)} 重新建立字幕上下文。`,false);}catch(e){lastServerError=e.message||String(e);postStatus(lastServerError,true);}
  }
  function maybeRestartForSeek(v){const t=Number(v.currentTime||0);if(!agentActive)return;if(t>=sessionStart-2&&t<=stableThrough-3)return;restartAt(t);}

  window.addEventListener('message',e=>{
    const d=e.data;if(!d||d.ns!==NS||d.token!==BRIDGE_TOKEN)return;
    if(d.type==='activate'&&(!d.frame_id||d.frame_id===FRAME_ID)){startAgent(d).catch(err=>postStatus(err.message||String(err),true));}
    else if(d.type==='stop'&&(!d.frame_id||d.frame_id===FRAME_ID)){stopAgent(false);}
  });
  setInterval(()=>{mainVideo();renderCaption();},120);setInterval(sync,520);

  // ---------------- Top-frame coordinator UI ----------------
  if(!IS_TOP)return;
  const frames=new Map();let activeFrameId='',activeSource=null,controlUiUrl='';
  function cleanFrames(){const now=Date.now();for(const [id,x] of frames)if(now-x.seen>3500)frames.delete(id);}
  function bestFrame(){cleanFrames();const arr=[...frames.values()].filter(x=>x.data.has_video&&x.data.area>4000);arr.sort((a,b)=>{const sa=a.data.area*(a.data.has_media_hint?1.15:1)+(a.data.ready_state||0)*800;const sb=b.data.area*(b.data.has_media_hint?1.15:1)+(b.data.ready_state||0)*800;return sb-sa;});return arr[0]||null;}
  window.addEventListener('message',e=>{
    const d=e.data;if(!d||d.ns!==NS||d.token!==BRIDGE_TOKEN)return;
    if(d.type==='frame-report'){frames.set(d.frame_id,{source:e.source,data:d,seen:Date.now()});if(uiReady)refreshFrameLine();}
    else if(d.type==='agent-status'&&d.frame_id===activeFrameId){updateTopStatus(d);}
  });

  let uiReady=false,$=null;
  whenReady(()=>{
    const host=document.createElement('div');host.id='autosubtitlesync-companion-host-v76';host.style.cssText='all:initial;position:fixed;right:18px;bottom:18px;z-index:2147483647;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif;';
    const sh=host.attachShadow({mode:'open'});sh.innerHTML=`<style>*{box-sizing:border-box}button,select,input{font:inherit}.pill{border:1px solid rgba(255,255,255,.15);background:#111827;color:#fff;border-radius:999px;padding:10px 14px;box-shadow:0 8px 28px rgba(15,23,42,.28);font-size:13px;font-weight:700;cursor:pointer;display:flex;align-items:center;gap:8px}.dot{width:8px;height:8px;border-radius:50%;background:#94a3b8}.dot.ok{background:#34d399}.dot.wait{background:#f59e0b}.dot.bad{background:#fb7185}.panel{position:absolute;right:0;bottom:48px;width:326px;background:rgba(255,255,255,.985);color:#111827;border:1px solid #e5e7eb;border-radius:16px;padding:14px;box-shadow:0 18px 50px rgba(15,23,42,.22);display:none}.panel.open{display:block}.title{font-size:14px;font-weight:800;margin-bottom:3px}.sub{font-size:11px;color:#6b7280;line-height:1.45;margin-bottom:10px;max-height:34px;overflow:hidden}.frame{padding:8px 9px;border:1px solid #e7e9ee;background:#f8f9fb;border-radius:10px;font-size:11px;color:#555f70;line-height:1.45;margin-bottom:10px}.frame b{color:#111827}.grid{display:grid;grid-template-columns:1fr 1fr;gap:9px}.field{display:flex;flex-direction:column;gap:5px}.field.full{grid-column:1/-1}.label{font-size:11px;font-weight:700;color:#4b5563}select{width:100%;border:1px solid #dfe3e8;border-radius:9px;background:#fff;padding:8px;font-size:12px;color:#111827}.check{display:flex;align-items:center;gap:7px;font-size:12px;color:#374151;padding-top:4px}.primary,.secondary,.stop{width:100%;border:0;border-radius:10px;padding:10px 12px;font-size:12px;font-weight:750;cursor:pointer;margin-top:9px}.primary{background:#2563eb;color:#fff}.secondary{background:#f1f3f6;color:#252a33}.stop{background:#fff0ee;color:#b42318}.primary:disabled{opacity:.5}.msg{font-size:11px;color:#6b7280;line-height:1.5;margin-top:9px}.msg.err{color:#b42318}.status{margin-top:10px;padding:9px 10px;border-radius:10px;background:#f7f8fa;border:1px solid #e9ebef;font-size:11px;color:#505866;line-height:1.55}.status b{color:#111827}.hidden{display:none!important}</style><button class="pill" id="pill"><span class="dot" id="dot"></span><span id="pillText">字幕助手</span></button><div class="panel" id="panel"><div class="title">AutoSubtitleSync · Iframe Bridge</div><div class="sub" id="pageTitle"></div><div class="frame" id="frameLine">正在检测网页播放器…</div><div class="grid"><label class="field"><span class="label">输出字幕</span><select id="target"><option value="zh">中文</option><option value="en">English</option><option value="original">保持原语言</option><option value="de">Deutsch</option><option value="fr">Français</option><option value="es">Español</option></select></label><label class="field"><span class="label">Semantic Ahead</span><select id="ahead"><option value="60">60 秒</option><option value="90">90 秒 · 推荐</option><option value="120">120 秒</option><option value="180">180 秒</option></select></label><label class="field full"><span class="check"><input type="checkbox" id="bilingual"> 原文 + 翻译双语</span></label></div><button class="primary" id="start">在真实播放器上启用字幕</button><button class="secondary" id="control">打开本地控制台</button><button class="stop hidden" id="stop">停止本页字幕</button><div class="status hidden" id="status"></div><div class="msg" id="msg">会自动寻找主页面或嵌套 iframe 中真正的 HTML5 播放器。</div></div>`;
    document.documentElement.appendChild(host);$=id=>sh.getElementById(id);uiReady=true;
    const p=getPrefs();$('target').value=p.target;$('ahead').value=String(p.lookahead);$('bilingual').checked=!!p.bilingual;
    $('pageTitle').textContent=document.title||location.hostname;refreshFrameLine();
    $('pill').addEventListener('click',async()=>{$('panel').classList.toggle('open');if($('panel').classList.contains('open')){await detectTop();refreshFrameLine();}});
    $('start').addEventListener('click',startTop);
    $('stop').addEventListener('click',stopTop);
    $('control').addEventListener('click',async()=>{const s=await detectTop();if(!s){setMsg('请先启动 AutoSubtitleSync.command。',true);return;}window.open(controlUiUrl||`http://127.0.0.1:${s.port}/`,'_blank','noopener');});
    setInterval(refreshFrameLine,900);setTimeout(detectTop,900);
  });
  async function detectTop(){const s=await findServer();if(!uiReady)return s;$('dot').className='dot '+(s?'ok':'bad');if(!s)setMsg('未检测到本机 AutoSubtitleSync。请先运行 Mac 里的 AutoSubtitleSync.command。',true);return s;}
  function setMsg(t,err=false){if(!uiReady)return;$('msg').className='msg'+(err?' err':'');$('msg').textContent=t;}
  function refreshFrameLine(){if(!uiReady)return;const b=bestFrame();if(!b){$('frameLine').innerHTML='<b>未检测到播放器</b><br>请先让网页中的视频真正加载/播放几秒。';return;}const d=b.data;let host='当前页面';try{host=new URL(d.frame_url).hostname||host}catch(_){};$('frameLine').innerHTML=`<b>已检测播放器 · ${host}</b><br>${d.has_media_hint?'已发现可供本地识别的媒体入口':'媒体由浏览器动态播放；将尝试 iframe 页面解析'} · 当前 ${fmt(d.current_time)}`;}
  async function startTop(){
    const b=bestFrame();if(!b){setMsg('还没有检测到真正的视频播放器。请先按网页播放键，让视频加载几秒后再试。',true);return;}
    const s=await detectTop();if(!s)return;const p={target:$('target').value,lookahead:Number($('ahead').value||90),bilingual:$('bilingual').checked,model:'small'};savePrefs(p);activeFrameId=b.data.frame_id;activeSource=b.source;$('start').disabled=true;$('dot').className='dot wait';setMsg('正在把真实播放器交给本机字幕引擎…');
    try{b.source.postMessage({ns:NS,token:BRIDGE_TOKEN,type:'activate',frame_id:activeFrameId,prefs:p,top_url:location.href,top_title:document.title},'*');$('stop').classList.remove('hidden');$('status').classList.remove('hidden');controlUiUrl=`http://127.0.0.1:${s.port}/`;}
    catch(e){setMsg(e.message||String(e),true);$('dot').className='dot bad';}
    finally{$('start').disabled=false;}
  }
  function stopTop(){if(activeSource){try{activeSource.postMessage({ns:NS,token:BRIDGE_TOKEN,type:'stop',frame_id:activeFrameId},'*');}catch(_){}}activeFrameId='';activeSource=null;if(uiReady){$('stop').classList.add('hidden');$('status').classList.add('hidden');$('pillText').textContent='字幕助手';$('dot').className='dot';setMsg('本页字幕已停止。');}}
  function updateTopStatus(d){if(!uiReady)return;const lead=Math.max(0,Number(d.lead||0));$('status').classList.remove('hidden');$('status').innerHTML=`<b>${d.ready?(d.buffering?'补充缓冲':'字幕运行中'):'Semantic Look-ahead 准备中'}</b><br>播放 ${fmt(d.current_time)} · 稳定字幕 ${fmt(d.stable_through)} · Ahead ${Math.floor(lead)}s<br>已生成 ${d.count||0} 条自然句字幕`;$('pillText').textContent=d.ready?`字幕 · +${Math.floor(lead)}s`:`准备 · ${Math.floor(lead)}s`;if(d.error){$('dot').className='dot bad';setMsg(d.error,true);}else{$('dot').className='dot '+(d.ready?'ok':'wait');if(d.message)setMsg(d.message,false);}}
})();
