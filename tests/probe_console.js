(async () => {
  const LEAD=60, R=[], say=s=>{R.push(s);console.log(s)};
  const wait=(f,ms)=>new Promise(r=>{const t=Date.now();(function k(){let o=false;try{o=!!f()}catch(e){};if(o)return r(true);if(Date.now()-t>ms)return r(false);setTimeout(k,200)})()});
  const measure=(el,ms)=>new Promise(async res=>{
    let ac,st,nd,gn,sum=0,n=0,pk=0;
    try{
      st=el.captureStream?el.captureStream():(el.mozCaptureStream?el.mozCaptureStream():null);
      if(!st||!st.getAudioTracks().length)return res({ok:false,rms:0,why:'无音轨/不支持 captureStream'});
      ac=new (window.AudioContext||window.webkitAudioContext)();try{await ac.resume()}catch(e){}
      const s=ac.createMediaStreamSource(new MediaStream(st.getAudioTracks()));
      gn=ac.createGain();gn.gain.value=0;nd=ac.createScriptProcessor(4096,1,1);
      nd.onaudioprocess=e=>{const d=e.inputBuffer.getChannelData(0);for(let i=0;i<d.length;i++){const x=d[i];sum+=x*x;const a=x<0?-x:x;if(a>pk)pk=a;n++}};
      s.connect(nd);nd.connect(gn);gn.connect(ac.destination);
    }catch(e){return res({ok:false,rms:0,why:e.message||String(e)})}
    setTimeout(()=>{const rms=n?Math.sqrt(sum/n):0;try{nd.disconnect();gn.disconnect();st.getTracks().forEach(t=>t.stop());ac.close()}catch(e){};res({ok:rms>8e-4,rms,peak:pk})},ms);
  });
  say('AutoSubtitleSync 影子播放器探测（控制台版）');
  say('时间：'+new Date().toLocaleString());
  say('页面：'+location.hostname+location.pathname.slice(0,60));
  const vs=[...document.querySelectorAll('video')].map(e=>[e,e.getBoundingClientRect()]).filter(x=>x[1].width*x[1].height>20000).sort((a,b)=>b[1].width*b[1].height-a[1].width*a[1].height);
  const v=(vs[0]||[])[0];
  if(!v){say('✗ 没找到正在播放的播放器——请先点开视频播放几秒再重跑');return}
  const raw=v.currentSrc||v.src||'';
  say('① 主播放器：'+(raw.startsWith('blob:')?'blob:（第二个播放器通常拿不到）':raw.startsWith('http')?'http(s) 直链':raw?'其它':'地址为空')+
      '  时长 '+(isFinite(v.duration)&&v.duration>0?v.duration.toFixed(1)+' 秒':'未知')+'  '+(v.paused?'暂停':'播放中'));
  if(!(v.captureStream||v.mozCaptureStream)){say('✗ 这个浏览器不支持 captureStream（需 Chrome / Edge），不用继续');return}
  const m=await measure(v,3000);
  say('   主播放器响度：'+(m.ok?'有声音 ✓':'几乎无声 ✗')+'  RMS='+m.rms.toFixed(4)+(m.why?'  '+m.why:''));
  if(!m.ok){say('结论：这个播放器本身抓不到声音（跨域 / DRM），影子播放器也一样抓不到。');return}
  say('② 在页面内克隆一个静音播放器…');
  const p=document.createElement('video');
  p.muted=true;p.volume=0;p.playsInline=true;p.preload='auto';
  p.style.cssText='position:fixed;right:2px;bottom:2px;width:2px;height:2px;opacity:.01;pointer-events:none';
  if(v.crossOrigin)p.crossOrigin=v.crossOrigin;
  const ev=[];['loadedmetadata','canplay','playing','seeked','error'].forEach(k=>p.addEventListener(k,()=>ev.push(k)));
  try{p.src=raw}catch(e){say('   ✗ 无法设置地址：'+(e.message||e))}
  document.body.appendChild(p);try{p.load()}catch(e){}
  if(!await wait(()=>p.readyState>=1||p.error,6000)){say('   ✗ 第二个播放器 6 秒内没加载出内容（该站多半用 blob:/MSE）→ 结论：起不了影子播放器');try{p.remove()}catch(e){};sayHelp();return}
  if(p.error){say('   ✗ 第二个播放器报错 code='+p.error.code+' → 结论：这个站不允许第二路加载同一地址');try{p.remove()}catch(e){};sayHelp();return}
  const seekEnd=(p.seekable&&p.seekable.length)?p.seekable.end(p.seekable.length-1):0;
  say('   元数据 OK ✓  可跳转范围 0 → '+seekEnd.toFixed(1)+' 秒');
  if(!seekEnd){say('   结论：内容不能跳到"未来"（直播 / 流式），影子播放器无从提前。');try{p.remove()}catch(e){};return}
  const tgt=Math.min(seekEnd,(v.currentTime||0)+LEAD);
  say('③ 跳到 +'+LEAD+' 秒（目标 '+tgt.toFixed(1)+' 秒）并播放…');
  try{p.currentTime=tgt}catch(e){say('   ✗ 跳转失败：'+(e.message||e))}
  if(!await wait(()=>Math.abs(p.currentTime-tgt)<2&&p.readyState>=2,8000)){say('   ✗ 跳转后没就绪（currentTime='+p.currentTime.toFixed(1)+'）→ 结论：不可用');try{p.remove()}catch(e){};sayHelp();return}
  try{await p.play()}catch(e){say('   ⚠ 自动播放被拦：'+(e.message||e))}
  const t0=p.currentTime, moving=await wait(()=>!p.paused&&p.currentTime>t0+0.6,5000);
  say('   '+(moving?'正在播放 ✓':'未能播放 ✗')+'  currentTime='+p.currentTime.toFixed(1));
  say('④ 测影子播放器的声音（4 秒，静音播放，你不会听到）…');
  const m2=await measure(p,4000);
  say('   影子播放器响度：'+(m2.ok?'有声音 ✓':'几乎无声 ✗')+'  RMS='+m2.rms.toFixed(4)+(m2.why?'  '+m2.why:''));
  try{p.pause();p.src='';p.remove()}catch(e){}
  say('事件轨迹：'+(ev.join(', ')||'（无）'));
  say('');
  say(moving&&m2.ok?'结论：✓ 这个网站可以用影子播放器做前瞻（预计领先约 '+LEAD+' 秒）。':'结论：这个网站不适用影子播放器。');
  say('（报告已复制到剪贴板，直接粘贴发我即可）');
  try{await navigator.clipboard.writeText(R.join('\n'))}catch(e){}
  function sayHelp(){say('建议：改用「粘贴视频链接」模式（服务端自己取流做前瞻）。')}
})();
