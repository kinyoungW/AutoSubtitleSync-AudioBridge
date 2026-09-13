"""End-to-end test: browser audio bridge -> server -> live cues.

Simulates exactly what BrowserAudioBridge.user.js will do: open an audio
session, then push 16 kHz PCM16 chunks in real time, polling the same
/api/companion/sync endpoint the overlay uses.
"""
import json, os, sys, threading, time, urllib.request, difflib

MODEL = os.environ.get('TEST_MODEL', 'base')
# TEST_MODEL_DIR 指向本机 faster-whisper 模型目录（CT2 格式）
MODEL_DIR = os.environ.get('TEST_MODEL_DIR') or f'/tmp/ct2-{MODEL}'

sys.path.insert(0, '/tmp/autosub-v8/work_v8')
os.chdir('/tmp/autosub-v8/work_v8')

import server
from faster_whisper import WhisperModel

# sandbox-only shim: the real path downloads from HF (blocked here); the
# ModelScope-mirrored CT2 tiny model is byte-identical in layout.
_M = WhisperModel(MODEL_DIR, device='cpu', compute_type='int8')
server.load_whisper = lambda size: _M

PORT = 8766
httpd = server.ThreadingHTTPServer(('127.0.0.1', PORT), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)
BASE = f'http://127.0.0.1:{PORT}'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def post(path, obj=None, raw=None):
    data = raw if raw is not None else json.dumps(obj or {}).encode()
    ctype = 'application/octet-stream' if raw is not None else 'application/json'
    req = urllib.request.Request(BASE + path, data=data, headers={'Content-Type': ctype})
    try:
        with OPENER.open(req, timeout=30) as r:
            return json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        return {'_http_error': e.code, 'body': e.read().decode()[:200]}

def get(path):
    with OPENER.open(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode() or '{}')

pcm = open('/tmp/stream.pcm', 'rb').read()
expected = open('/tmp/stream.txt', 'r').read().split()
CH = 16000  # 0.5 s
chunks = [pcm[i:i+CH] for i in range(0, len(pcm), CH)]
fails = []

print("=== 1) ping ===")
print("  ", get('/api/companion/ping'))

print("=== 2) 打开音频会话（模拟浏览器桥） ===")
r = post('/api/browser-audio/open', {'model': 'tiny', 'lang': 'en', 'mixed': True,
                                     'target': 'original', 'bilingual': False, 't0': 0.0,
                                     'title': '音频桥测试'})   # 用服务端默认窗口参数
print("  ", r)
seq = r.get('seq')
if not seq:
    print("FAIL: 无法开启音频会话"); sys.exit(1)

print(f"=== 3) 实时推流 {len(chunks)} 块（{len(pcm)/32000:.1f} 秒音频） ===")
seen = 0
seen_lat = {}
t_start = time.time()
for n, c in enumerate(chunks, 1):
    post('/api/browser-audio/push', raw=c)
    s = get(f'/api/companion/sync?seq={seq}&revision=-1')
    cues = s.get('cues') or []
    if len(cues) != seen:
        seen = len(cues)
        last = cues[-1] if cues else {}
        print(f"  [{time.time()-t_start:5.1f}s 已推 {n*0.5:5.1f}s 音频] 字幕 {len(cues)} 条 | "
              f"processed={s.get('processed_time', 0):.1f}s stable={s.get('stable_through', 0):.1f}s "
              f"audio_mode={s.get('audio_mode')} | 末条: [{last.get('start', 0):.1f}-{last.get('end', 0):.1f}] {last.get('text', '')[:44]}")
    for c in cues:
        if c['idx'] not in seen_lat:
            seen_lat[c['idx']] = round((time.time() - t_start) - c['end'], 1)
    dt = 0.5 - (time.time() - t_start - n * 0.5)
    if dt > 0:
        time.sleep(dt)
print(f"  推流完成，用时 {time.time()-t_start:.1f}s")

print("=== 4) 推流停止后等待 8 秒（模拟用户暂停/关闭页面） ===")
time.sleep(8)
s = get(f'/api/companion/sync?seq={seq}&revision=-1')
print(f"  收尾后字幕 {len(s.get('cues') or [])} 条，processed={s.get('processed_time', 0):.1f}s")

print("=== 5) 停止会话 ===")
print("  ", post('/api/browser-audio/stop'))
time.sleep(2)
st = get('/api/state')
s = get(f'/api/companion/sync?seq={seq}&revision=-1')
cues = s.get('cues') or []
print(f"  online_running={st.get('online_running')} audio_mode={st.get('online_audio_mode')} "
      f"bytes={st.get('browser_audio_bytes')} done={st.get('online_done')}")

lat = []
for c in cues:
    pass
print("=== 6) 最终字幕时间轴 ===")
for c in cues:
    print(f"  {c['idx']:>2}. [{c['start']:6.2f} - {c['end']:6.2f}] {c['text']}")

print("=== 7) 校验 ===")
joined = " ".join(c['text'] for c in cues).lower()
exp_join = " ".join(expected).lower()
ratio = difflib.SequenceMatcher(None, exp_join, joined).ratio()
print(f"  字幕条数: {len(cues)}")
print(f"  文本还原度(与原始讲稿相似度): {ratio*100:.1f}%")
print(f"  覆盖时间: {cues[0]['start']:.2f}s -> {cues[-1]['end']:.2f}s (音频总长 27.4s)")
mono = all(cues[i]['start'] <= cues[i+1]['start'] for i in range(len(cues)-1))
print(f"  时间戳单调递增: {mono}")
print(f"  端点 /api/companion/sync 返回 audio_mode 标记: {s.get('audio_mode')}")
print(f"  实时延迟（字幕出现时落后该句语音的秒数）: {sorted(set(seen_lat.values()))}")
if seen_lat:
    print(f"  平均 {sum(seen_lat.values())/len(seen_lat):.1f}s  最大 {max(seen_lat.values()):.1f}s")

if len(cues) < 4: fails.append("字幕条数不足")
if cues and cues[0]['start'] > 2.0: fails.append(f"开头音频丢失（首条字幕起点 {cues[0]['start']}s）")
if ratio < 0.6: fails.append(f"文本还原度过低 {ratio:.2f}")
if not mono: fails.append("时间戳顺序错误")
if cues and abs(cues[-1]['end'] - 27.4) > 8: fails.append("末条字幕时间戳偏离音频长度")
if not st.get('online_audio_mode'): fails.append("state 未标记 audio_mode")
if st.get('online_running'): fails.append("停止后 online_running 仍为 True")

print("=== 8) 实时字幕导出 SRT ===")
e = post('/api/export')
print("  ", e)
if e.get('path') and os.path.exists(e['path']):
    print("  文件内容前几行:")
    for line in open(e['path'], encoding='utf-8').read().splitlines()[:8]:
        print("   ", line)
    os.remove(e['path'])
else:
    fails.append("导出 SRT 失败")

print("=== 9) 回归：URL 模式仍在（无 audio_mode 时不会误入音频分支） ===")
r2 = post('/api/companion/open', {'url': 'not-a-url'})
print("  ", r2)
if not (r2.get('_http_error') == 400 or 'error' in r2): fails.append("URL 校验被破坏")

print()
if fails:
    print("FAIL:", " | ".join(fails)); sys.exit(1)
print("ALL PASS ✅  （音频桥 → 服务端 → 字幕端点 全链路打通）")
