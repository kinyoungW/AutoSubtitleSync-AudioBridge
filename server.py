#!/usr/bin/env python3
import json, os, re, statistics, subprocess, sys, threading, time, webbrowser, tempfile, urllib.request, urllib.error, shutil
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    from browser_audio_bridge import push_pcm, STATS as AUDIO_STATS
except Exception:
    def push_pcm(x): pass
    AUDIO_STATS={'chunks':0,'bytes':0}

# v8-patch: real audio-stream consumer driving the same ONLINE_* cue pipeline
try:
    import audio_stream
except Exception:
    audio_stream = None

from rapidfuzz import process, fuzz
from faster_whisper import WhisperModel
from imageio_ffmpeg import get_ffmpeg_exe
import ctranslate2
import sentencepiece as spm
import numpy as np
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})")
SUPPORTED_LANGS = {"zh":"中文", "en":"English", "fr":"Français", "es":"Español", "de":"Deutsch"}

@dataclass
class Cue:
    idx: int
    start: float
    end: float
    text: str
    language: str = ""
    original_text: str = ""

@dataclass
class ASRSeg:
    start: float
    end: float
    text: str
    words: list | None = None

STATE = {
    "video": "", "srt": "", "running": False, "progress": 0,
    "message": "请选择视频，然后选择“自动生成”或“校准已有 SRT”", "result": None,
    "error": "", "exported": "", "mode": "generate",
    "mux_running": False, "mux_progress": 0, "mux_message": "", "mp4_exported": "",
    "online_running": False, "online_url": "", "online_title": "", "online_message": "",
    "online_player_url": "", "online_is_live": False, "online_count": 0, "online_error": "",
    "online_done": False, "online_duration": 0.0,
    "online_processed_time": 0.0, "online_stable_through": 0.0, "online_lookahead": 90.0,
    "online_revision": 0, "online_semantic_tail": 12.0,
    "online_start_at": 0.0,
    "online_phase": "IDLE", "online_phase_label": "等待开始",
    "online_session_browser": "", "online_session_mode": "auto",
    "online_error_kind": "", "online_error_hint": "", "online_resolve_attempts": [],
    "browser_sessions": [],
    "companion_seq": 0, "companion_url": "", "companion_title": "",
    "companion_start_time": 0.0, "companion_received": False,
    "online_audio_mode": False, "browser_audio_received": False,
    "browser_audio_bytes": 0, "browser_audio_at": 0.0
}
LOCK = threading.Lock()
CUES_CACHE = None
RESULT_CACHE = None
TRANSLATION_LOCK = threading.RLock()
TRANSLATION_CACHE = {}
TEXT_TRANSLATION_CACHE = {}
TRANSLATION_MODEL_DIR = Path.home() / ".cache" / "AutoSubtitleSync" / "translation_models"
TRANSLATION_REPOS = {
    ("en","zh"): "Sams200/opus-mt-en-zh",
    ("zh","en"): "Sams200/opus-mt-zh-en",
    ("en","de"): "Sams200/opus-mt-en-de",
    ("de","en"): "Sams200/opus-mt-de-en",
    ("en","fr"): "Sams200/opus-mt-en-fr",
    ("fr","en"): "Sams200/opus-mt-fr-en",
    ("en","es"): "Sams200/opus-mt-en-es",
    ("es","en"): "Sams200/opus-mt-es-en",
}


ONLINE_CUES=[]
ONLINE_RAW=[]
ONLINE_REVISION=0
ONLINE_LOCK=threading.RLock()
ONLINE_STOP=threading.Event()
ONLINE_PROC=None
ONLINE_MEDIA={"url":"","headers":{},"protocol":"","ext":"","title":"","duration":0.0,"is_live":False,"session_browser":"","source_kind":""}


def set_state(**kwargs):
    with LOCK:
        STATE.update(kwargs)


def get_state():
    with LOCK:
        return dict(STATE)


def ts_to_sec(s):
    m = TIME_RE.search(s.strip())
    if not m:
        raise ValueError(f"Bad timestamp: {s}")
    h, mi, se, ms = map(int, m.groups())
    return h*3600 + mi*60 + se + ms/1000.0


def sec_to_ts(x):
    x = max(0.0, x)
    n = int(round(x*1000))
    h, n = divmod(n, 3600000)
    mi, n = divmod(n, 60000)
    se, ms = divmod(n, 1000)
    return f"{h:02d}:{mi:02d}:{se:02d},{ms:03d}"


def read_text_auto(path):
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "cp1252"):
        try:
            return Path(path).read_text(encoding=enc)
        except Exception:
            pass
    return Path(path).read_text(errors="replace")


def parse_srt(path):
    txt = read_text_auto(path).replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", txt.strip())
    cues = []
    for block in blocks:
        lines = [x.strip("\ufeff") for x in block.split("\n") if x.strip()]
        if len(lines) < 2:
            continue
        if "-->" in lines[0]:
            idx, tl, text_lines = len(cues)+1, lines[0], lines[1:]
        elif len(lines) >= 3 and "-->" in lines[1]:
            try: idx = int(lines[0].strip())
            except Exception: idx = len(cues)+1
            tl, text_lines = lines[1], lines[2:]
        else:
            continue
        try:
            a, b = [x.strip() for x in tl.split("-->", 1)]
            st, en = ts_to_sec(a), ts_to_sec(b)
        except Exception:
            continue
        cues.append(Cue(idx, st, en, " ".join(x.strip() for x in text_lines).strip()))
    if not cues:
        raise ValueError("没有解析到有效 SRT 字幕。")
    return cues


def normalize_text(s):
    s = re.sub(r"<[^>]+>", "", s.lower())
    return "".join(ch for ch in s if ch.isalnum() or ('\u4e00' <= ch <= '\u9fff'))


def sample_cues(cues, limit=280):
    usable = [c for c in cues if len(normalize_text(c.text)) >= 4]
    if len(usable) <= limit:
        return usable
    step = (len(usable)-1)/(limit-1)
    return [usable[round(i*step)] for i in range(limit)]


def build_windows(segs, max_join=3):
    out = []
    for i in range(len(segs)):
        parts=[]
        for k in range(max_join):
            j=i+k
            if j >= len(segs): break
            parts.append(segs[j].text)
            norm=normalize_text(" ".join(parts))
            if len(norm) >= 2:
                out.append((norm, segs[i].start, segs[j].end))
    return out


def robust_affine(pairs):
    if len(pairs) < 4:
        raise ValueError("有效匹配点太少，无法可靠校准。")
    diffs=[y-x for x,y,s in pairs if s >= 70] or [y-x for x,y,s in pairs]
    b0=statistics.median(diffs)
    r0=[abs((y-x)-b0) for x,y,s in pairs]
    mad0=statistics.median(r0) if r0 else 0.0
    tol0=max(2.5,min(20.0,4.0*mad0+0.8))
    kept=[p for p in pairs if abs((p[1]-p[0])-b0) <= tol0]
    if len(kept)<4:
        kept=sorted(pairs,key=lambda p:abs((p[1]-p[0])-b0))[:max(4,min(20,len(pairs)))]

    def fit(data):
        xs=[p[0] for p in data]; ys=[p[1] for p in data]
        mx=sum(xs)/len(xs); my=sum(ys)/len(ys)
        den=sum((x-mx)**2 for x in xs)
        if den < 1e-9:
            return 1.0, statistics.median([y-x for x,y,_ in data])
        a=sum((x-mx)*(y-my) for x,y,_ in data)/den
        return a, my-a*mx

    a,b=fit(kept)
    if not (0.97 <= a <= 1.03):
        a=1.0; b=statistics.median([y-x for x,y,_ in kept])
    for _ in range(4):
        res=[abs(y-(a*x+b)) for x,y,s in kept]
        med=statistics.median(res) if res else 0.0
        tol=max(0.45,min(3.0,3.5*med+0.15))
        new=[p for p in kept if abs(p[1]-(a*p[0]+b)) <= tol]
        if len(new)<4 or len(new)==len(kept): break
        kept=new; a,b=fit(kept)
    errors=[abs(y-(a*x+b)) for x,y,s in kept]
    med_err=statistics.median(errors) if errors else 999
    mean_score=sum(s for _,_,s in kept)/len(kept)
    coverage=min(1.0,len(kept)/max(12,len(pairs)*0.4))
    conf=max(0.0,min(99.0,0.55*mean_score+30*coverage+15*max(0,1-med_err/2)))
    return a,b,kept,med_err,conf


def match_srt_asr(cues,segs):
    windows=build_windows(segs,3)
    choices=[w[0] for w in windows]
    sampled=sample_cues(cues,280)
    pairs=[]
    for n,c in enumerate(sampled):
        q=normalize_text(c.text)
        if len(q)<4: continue
        hit=process.extractOne(q,choices,scorer=fuzz.WRatio,score_cutoff=52)
        if hit:
            _,score,wi=hit
            _,st,en=windows[wi]
            sd=max(0.2,c.end-c.start); ad=max(0.2,en-st)
            ratio=min(sd,ad)/max(sd,ad)
            adj=float(score)*(0.82+0.18*ratio)
            if adj>=54:
                pairs.append(((c.start+c.end)/2,(st+en)/2,adj))
        if n%8==0:
            set_state(progress=65+int(25*n/max(1,len(sampled))),message=f"正在匹配字幕 {n+1}/{len(sampled)}")
    if not pairs:
        raise ValueError("没有找到足够的文本匹配。若视频语音和 SRT 不是同一种语言，请改用“自动生成字幕”。")
    return (*robust_affine(pairs),pairs)


LANG_STOPWORDS = {
    "en": {"the","and","is","are","to","of","in","that","this","it","for","you","we","with","on","as","be","have","not","but","from","was","were","will","can"},
    "de": {"der","die","das","und","ist","sind","zu","von","in","ich","nicht","mit","auf","für","ein","eine","wir","sie","es","den","dem","dass","auch","wie","aber"},
    "fr": {"le","la","les","de","des","du","et","est","sont","à","un","une","je","nous","vous","pas","pour","dans","que","qui","ce","cette","avec","mais"},
    "es": {"el","la","los","las","de","del","y","es","son","a","un","una","yo","nosotros","usted","no","para","en","que","con","por","como","pero","esta"},
}

def detect_text_language(text, fallback="en"):
    """Small dependency-free classifier for zh/en/de/fr/es subtitle snippets.

    It intentionally prefers the Whisper global language as fallback for short/ambiguous
    fragments, avoiding a source-only NLP package in the installer.
    """
    text=(text or "").strip()
    if not text:
        return fallback if fallback in SUPPORTED_LANGS else "en"
    han=sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    letters=sum(1 for c in text if c.isalpha())
    if han >= 2 or (letters and han/letters >= 0.18):
        return "zh"
    lower=text.lower()
    words=re.findall(r"[a-zA-ZÀ-ÿß]+", lower)
    if len(words) < 2:
        return fallback if fallback in SUPPORTED_LANGS else "en"
    scores={k:0.0 for k in ("en","de","fr","es")}
    for lang, vocab in LANG_STOPWORDS.items():
        scores[lang] += 2.0*sum(1 for w in words if w in vocab)
    # language-specific orthography; accents shared by fr/es get smaller weights
    scores["de"] += 2.5*sum(lower.count(ch) for ch in "äöüß")
    scores["es"] += 3.0*(lower.count("ñ")+lower.count("¿")+lower.count("¡"))
    scores["fr"] += 1.2*sum(lower.count(ch) for ch in "àâçèêëîïôùûüœ")
    scores["es"] += 0.8*sum(lower.count(ch) for ch in "áéíóú")
    # contractions are strong French signals
    scores["fr"] += 1.5*len(re.findall(r"\b(?:l|d|j|qu|c|n|s|m|t)'", lower))
    best=max(scores, key=scores.get)
    if scores[best] < 2.0:
        return fallback if fallback in SUPPORTED_LANGS else "en"
    return best


def _is_sentence_end(text):
    return bool(re.search(r"[.!?。！？…]['\"”’)]?$", text.strip()))


def _join_words(words):
    out=""
    for w in words:
        t=w[2]
        if not out:
            out=t.strip()
        elif t.startswith((",", ".", "!", "?", ";", ":", "'", "’", "。", "，", "！", "？", "；", "：")):
            out += t.strip()
        elif any('\u4e00' <= c <= '\u9fff' for c in t):
            out += t.strip()
        else:
            out += " " + t.strip()
    return out.strip()


def segments_to_cues(segs, fallback_lang="en"):
    cues=[]
    for seg in segs:
        words=[]
        for w in (seg.words or []):
            try:
                st=float(w.start); en=float(w.end); tx=str(w.word)
                if tx.strip(): words.append((st,en,tx))
            except Exception:
                pass
        if not words:
            tx=seg.text.strip()
            if tx:
                lang=detect_text_language(tx, fallback_lang)
                cues.append(Cue(len(cues)+1,seg.start,seg.end,tx,lang,tx))
            continue
        cur=[]
        cue_start=None
        last_end=None
        for w in words:
            st,en,tx=w
            if cue_start is None:
                cue_start=st
            trial=cur+[w]
            trial_text=_join_words(trial)
            dur=en-cue_start
            gap=(st-last_end) if last_end is not None else 0
            force_before = bool(cur) and (gap>0.85 or dur>6.4 or len(trial_text)>64)
            if force_before:
                text=_join_words(cur)
                lang=detect_text_language(text, fallback_lang)
                cues.append(Cue(len(cues)+1,cur[0][0],cur[-1][1],text,lang,text))
                cur=[w]; cue_start=st
            else:
                cur.append(w)
            last_end=en
            text_now=_join_words(cur)
            dur_now=cur[-1][1]-cur[0][0]
            if _is_sentence_end(text_now) and dur_now>=1.1 and len(text_now)>=10:
                lang=detect_text_language(text_now, fallback_lang)
                cues.append(Cue(len(cues)+1,cur[0][0],cur[-1][1],text_now,lang,text_now))
                cur=[]; cue_start=None; last_end=None
        if cur:
            text=_join_words(cur)
            lang=detect_text_language(text, fallback_lang)
            cues.append(Cue(len(cues)+1,cur[0][0],cur[-1][1],text,lang,text))

    # merge very tiny adjacent cues when they are close and same language
    merged=[]
    for c in cues:
        if merged and (c.end-c.start)<0.8 and c.start-merged[-1].end<0.35 and c.language==merged[-1].language:
            p=merged[-1]
            p.end=c.end
            sep="" if p.language=="zh" else " "
            p.text=(p.text+sep+c.text).strip()
            p.original_text=p.text
        else:
            c.idx=len(merged)+1
            merged.append(c)
    for i,c in enumerate(merged,1): c.idx=i
    return merged


def write_srt(cues,out):
    lines=[]
    for k,c in enumerate(cues,1):
        lines += [str(k),f"{sec_to_ts(c.start)} --> {sec_to_ts(c.end)}",c.text,""]
    Path(out).write_text("\n".join(lines),encoding="utf-8-sig")


def write_synced(cues,out,a,b):
    synced=[]
    for k,c in enumerate(cues,1):
        st=a*c.start+b; en=a*c.end+b
        if en <= st: en=st+max(0.2,c.end-c.start)
        synced.append(Cue(k,st,en,c.text,c.language,c.original_text))
    write_srt(synced,out)


def unique_output_from_srt(src):
    src=Path(src)
    out=src.with_name(src.stem+"_synced.srt")
    i=2
    while out.exists():
        out=src.with_name(f"{src.stem}_synced_{i}.srt"); i+=1
    return out


def unique_generated_output(video, lang_code, bilingual=False):
    src=Path(video)
    suffix = "bilingual" if bilingual else (lang_code if lang_code != "original" else "original")
    out=src.with_name(f"{src.stem}_{suffix}.srt")
    i=2
    while out.exists():
        out=src.with_name(f"{src.stem}_{suffix}_{i}.srt"); i+=1
    return out


def unique_mp4(video):
    src=Path(video)
    out=src.with_name(src.stem+"_subtitled.mp4")
    i=2
    while out.exists():
        out=src.with_name(f"{src.stem}_subtitled_{i}.mp4"); i+=1
    return out


def choose_file(kind):
    prompt = "选择 MP4 / 视频文件" if kind=="video" else "选择 SRT 字幕文件"
    script = f'POSIX path of (choose file with prompt "{prompt}")'
    p=subprocess.run(["/usr/bin/osascript","-e",script],capture_output=True,text=True)
    if p.returncode != 0:
        return ""
    path=p.stdout.strip()
    if kind=="video" and Path(path).suffix.lower() not in {".mp4",".m4v",".mov",".mkv",".webm",".avi"}:
        raise ValueError("请选择 MP4/MOV/MKV/WebM/AVI 视频文件。")
    if kind=="srt" and Path(path).suffix.lower() != ".srt":
        raise ValueError("请选择 .srt 字幕文件。")
    return path


def load_whisper(model_size):
    return WhisperModel(model_size,device="cpu",compute_type="int8")


def transcribe_video(video, model_size, lang, mixed, need_words, progress_start=10, progress_span=50):
    set_state(progress=progress_start,message=f"正在加载 Whisper {model_size}…")
    model=load_whisper(model_size)
    language=None if lang=="auto" else lang
    kwargs=dict(
        language=language,
        vad_filter=True,
        beam_size=5,
        word_timestamps=need_words,
    )
    if mixed and lang=="auto":
        # faster-whisper multilingual mode can re-detect language across windows.
        # condition_on_previous_text=False avoids previous-language prompt leakage.
        kwargs.update(multilingual=True, condition_on_previous_text=False)
    seg_iter,info=model.transcribe(video,**kwargs)
    segs=[]; dur=max(1.0,float(getattr(info,"duration",1.0) or 1.0))
    for s in seg_iter:
        text=s.text.strip()
        if text:
            segs.append(ASRSeg(float(s.start),float(s.end),text,getattr(s,"words",None)))
        set_state(progress=progress_start+int(progress_span*min(1.0,float(s.end)/dur)),
                  message=f"语音识别中：{float(s.end)/60:.1f} / {dur/60:.1f} 分钟")
    if len(segs)<1:
        raise ValueError("没有识别到有效语音。")
    return segs, info, dur


def align_worker(model_size,lang,mixed):
    global CUES_CACHE, RESULT_CACHE
    try:
        st=get_state(); video=st["video"]; srt=st["srt"]
        cues=parse_srt(srt); CUES_CACHE=cues
        set_state(progress=5,message=f"已解析 {len(cues)} 条字幕")
        segs,info,dur=transcribe_video(video,model_size,lang,mixed,False,10,52)
        set_state(progress=64,message=f"识别完成（{len(segs)} 段），开始匹配 SRT…")
        a,b,kept,med_err,conf,pairs=match_srt_asr(cues,segs)
        drift_1h=(a-1.0)*3600
        kind="基本属于固定时间偏移" if abs(drift_1h)<0.35 else "检测到时间轴漂移（不仅是常数偏移）"
        result={
            "mode":"align","a":a,"b":b,"drift_1h":drift_1h,"matched":len(kept),"candidates":len(pairs),
            "median_error":med_err,"confidence":conf,"language":getattr(info,"language","unknown"),
            "segments":len(segs),"duration":dur,"kind":kind
        }
        RESULT_CACHE=result
        set_state(running=False,progress=100,message="自动校准完成",result=result,error="")
    except Exception as e:
        set_state(running=False,message="自动校准失败",error=str(e))


def _download_file(url, dest, label):
    """Download with macOS system curl so Keychain trust works even if Python CA is broken."""
    dest=Path(dest); dest.parent.mkdir(parents=True, exist_ok=True)
    tmp=dest.with_suffix(dest.suffix+".part")
    set_state(message=f"首次翻译：正在下载 {label}")
    cmd=["/usr/bin/curl","--fail","--location","--retry","3","--retry-all-errors",
         "--connect-timeout","20","--output",str(tmp),url]
    try:
        p=subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or "curl 下载失败").strip()[-1200:])
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise RuntimeError("下载结果为空")
        tmp.replace(dest)
    except Exception:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
        raise


def _model_files_ok(model_dir):
    model_dir=Path(model_dir)
    required=("config.json","model.bin","shared_vocabulary.json","source.spm","target.spm")
    return all((model_dir/x).is_file() and (model_dir/x).stat().st_size>0 for x in required)


def ensure_translation_model(fr,to):
    if fr==to:
        return None
    key=(fr,to)
    if key not in TRANSLATION_REPOS:
        raise RuntimeError(f"没有配置离线翻译模型：{fr} → {to}")
    with TRANSLATION_LOCK:
        if key in TRANSLATION_CACHE:
            return TRANSLATION_CACHE[key]
        repo=TRANSLATION_REPOS[key]
        model_dir=TRANSLATION_MODEL_DIR / f"{fr}-{to}"
        model_dir.mkdir(parents=True,exist_ok=True)
        files=("config.json","model.bin","shared_vocabulary.json","source.spm","target.spm")
        if not _model_files_ok(model_dir):
            set_state(message=f"首次使用 {fr}→{to}：下载离线翻译模型（只需一次）…")
            for fn in files:
                dest=model_dir/fn
                if dest.is_file() and dest.stat().st_size>0:
                    continue
                url=f"https://huggingface.co/{repo}/resolve/main/{fn}?download=true"
                _download_file(url,dest,f"{fr}→{to} / {fn}")
        if not ctranslate2.contains_model(str(model_dir)):
            raise RuntimeError(f"翻译模型 {fr}→{to} 文件不完整；请删除 {model_dir} 后重试")
        try:
            translator=ctranslate2.Translator(str(model_dir),device="cpu",compute_type="int8")
            src_sp=spm.SentencePieceProcessor(model_file=str(model_dir/"source.spm"))
            tgt_sp=spm.SentencePieceProcessor(model_file=str(model_dir/"target.spm"))
        except Exception as e:
            raise RuntimeError(f"翻译模型 {fr}→{to} 加载失败：{e}") from e
        TRANSLATION_CACHE[key]=(translator,src_sp,tgt_sp)
        return TRANSLATION_CACHE[key]


def _translate_direct(text,fr,to):
    if fr==to or not text.strip(): return text
    translator,src_sp,tgt_sp=ensure_translation_model(fr,to)
    pieces=src_sp.encode(text,out_type=str)
    if not pieces: return text
    max_len=max(32,min(256,len(pieces)*4+24))
    result=translator.translate_batch([pieces],beam_size=3,max_decoding_length=max_len)
    if not result or not result[0].hypotheses:
        return text
    out=tgt_sp.decode(result[0].hypotheses[0]).strip()
    return out or text


def translate_text(text,src,target):
    if src==target: return text
    if src not in SUPPORTED_LANGS: src="en"
    if target not in SUPPORTED_LANGS: return text
    key=(src,target,text.strip())
    with TRANSLATION_LOCK:
        cached=TEXT_TRANSLATION_CACHE.get(key)
    if cached is not None:
        return cached
    if src!="en" and target!="en":
        mid=_translate_direct(text,src,"en")
        out=_translate_direct(mid,"en",target)
    else:
        out=_translate_direct(text,src,target)
    with TRANSLATION_LOCK:
        if len(TEXT_TRANSLATION_CACHE)>5000:
            TEXT_TRANSLATION_CACHE.clear()
        TEXT_TRANSLATION_CACHE[key]=out
    return out


def translate_cues(cues,target,bilingual):
    if target=="original":
        return cues
    needed=[]
    for c in cues:
        src=c.language if c.language in SUPPORTED_LANGS else "en"
        if src==target: continue
        if src!="en" and target!="en":
            needed.extend([(src,"en"),("en",target)])
        else:
            needed.append((src,target))
    seen=set()
    for pair in needed:
        if pair in seen: continue
        seen.add(pair); ensure_translation_model(*pair)

    out=[]
    total=max(1,len(cues))
    for i,c in enumerate(cues):
        src=c.language if c.language in SUPPORTED_LANGS else "en"
        original=c.original_text or c.text
        translated=translate_text(original,src,target) if src!=target else original
        if bilingual and translated.strip()!=original.strip():
            text=original.strip()+"\n"+translated.strip()
        else:
            text=translated.strip()
        out.append(Cue(i+1,c.start,c.end,text,target if not bilingual else src,original))
        if i%8==0:
            set_state(progress=75+int(20*(i+1)/total),message=f"正在翻译字幕 {i+1}/{len(cues)}")
    return out


def generate_worker(model_size,lang,mixed,target,bilingual):
    global CUES_CACHE, RESULT_CACHE
    try:
        video=get_state()["video"]
        segs,info,dur=transcribe_video(video,model_size,lang,mixed,True,5,64)
        set_state(progress=70,message="正在按时间戳切分字幕…")
        cues=segments_to_cues(segs, getattr(info,"language","en"))
        if not cues:
            raise ValueError("没有生成有效字幕。")
        counts={}
        for c in cues:
            counts[c.language]=counts.get(c.language,0)+1
        set_state(progress=74,message=f"已生成 {len(cues)} 条原文字幕")
        final_cues=translate_cues(cues,target,bilingual)
        CUES_CACHE=final_cues
        langs_sorted=sorted(counts.items(), key=lambda x:-x[1])
        lang_summary=", ".join(f"{SUPPORTED_LANGS.get(k,k)} {v}" for k,v in langs_sorted)
        result={
            "mode":"generate","segments":len(segs),"cues":len(final_cues),"duration":dur,
            "detected_language":getattr(info,"language","unknown"),"language_counts":counts,
            "language_summary":lang_summary,"target":target,"bilingual":bool(bilingual),
            "mixed":bool(mixed),"model":model_size
        }
        RESULT_CACHE=result
        set_state(running=False,progress=100,message="字幕生成完成",result=result,error="")
    except Exception as e:
        set_state(running=False,message="字幕生成失败",error=str(e))


def _run_ffmpeg_attempt(cmd, duration, phase_label):
    tail=[]
    proc=subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, bufsize=1, universal_newlines=True)
    assert proc.stdout is not None
    for raw in proc.stdout:
        line=raw.strip()
        if line:
            tail.append(line)
            if len(tail)>40: tail=tail[-40:]
        if line.startswith("out_time_ms="):
            try:
                sec=float(line.split("=",1)[1])/1_000_000.0
                pct=max(1,min(99,int(100*sec/max(1.0,duration))))
                set_state(mux_progress=pct,mux_message=f"{phase_label}：{pct}%")
            except Exception:
                pass
        elif line == "progress=end":
            set_state(mux_progress=100,mux_message=f"{phase_label}：完成")
    rc=proc.wait()
    return rc==0, "\n".join(tail[-20:])


def _ffmpeg_cmd(ffmpeg, video, subtitle, out, video_mode, audio_mode, title="Subtitles", lang="und"):
    cmd=[ffmpeg,"-hide_banner","-loglevel","error","-y",
         "-i",video,"-i",subtitle,
         "-map","0:v:0","-map","0:a?","-map","1:0",
         "-map_metadata","0","-map_chapters","0"]
    if video_mode == "copy": cmd += ["-c:v","copy"]
    else: cmd += ["-c:v","libx264","-preset","medium","-crf","18"]
    if audio_mode == "copy": cmd += ["-c:a","copy"]
    else: cmd += ["-c:a","aac","-b:a","192k"]
    cmd += ["-c:s","mov_text",
            "-metadata:s:s:0",f"title={title}",
            "-metadata:s:s:0",f"language={lang}",
            "-disposition:s:0","default",
            "-movflags","+faststart",
            "-progress","pipe:1","-nostats",str(out)]
    return cmd


def mux_worker():
    tmp_path=None
    try:
        st=get_state(); video=st.get("video",""); result=st.get("result")
        if not result or CUES_CACHE is None:
            raise ValueError("还没有可用于封装的字幕结果。")
        if not video or not os.path.isfile(video):
            raise ValueError("原视频文件不存在。")
        fd,tmp_path=tempfile.mkstemp(prefix="autosubtitle_",suffix=".srt"); os.close(fd)
        if result.get("mode")=="align":
            write_synced(CUES_CACHE,tmp_path,result["a"],result["b"])
            title="Synced Subtitles"; lang="und"
        else:
            write_srt(CUES_CACHE,tmp_path)
            target=result.get("target","original")
            title=("Bilingual Subtitles" if result.get("bilingual") else f"{SUPPORTED_LANGS.get(target,'Original')} Subtitles")
            lang=target if target in SUPPORTED_LANGS else "und"
        out=unique_mp4(video); ffmpeg=get_ffmpeg_exe(); duration=float(result.get("duration",1.0) or 1.0)
        attempts=[
            ("无损封装", "copy", "copy", "正在无损封装字幕（视频和音频不重编码）…"),
            ("转换音频并封装", "copy", "aac", "原音频不适合 MP4，正在只转换音频…"),
            ("兼容模式转码", "h264", "aac", "视频编码不适合 MP4，正在转换为 H.264/AAC…"),
        ]
        last_error=""; ok=False
        for phase,vmode,amode,msg in attempts:
            try: Path(out).unlink(missing_ok=True)
            except Exception: pass
            set_state(mux_progress=1,mux_message=msg)
            cmd=_ffmpeg_cmd(ffmpeg,video,tmp_path,out,vmode,amode,title,lang)
            ok,last_error=_run_ffmpeg_attempt(cmd,duration,phase)
            if ok: break
        if not ok:
            raise RuntimeError("生成 MP4 失败。FFmpeg 最后输出：\n"+last_error)
        set_state(mux_running=False,mux_progress=100,mux_message="带字幕 MP4 已导出",
                  mp4_exported=str(out),message=f"已导出带字幕 MP4：{out.name}",error="")
        subprocess.Popen(['/usr/bin/open','-R',str(out)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except Exception as e:
        set_state(mux_running=False,mux_message="MP4 导出失败",error=str(e))
    finally:
        if tmp_path:
            try: Path(tmp_path).unlink(missing_ok=True)
            except Exception: pass



def _online_format_selector(is_live_hint=False):
    # Online playback requires one muxed URL. Prefer HTTP/HLS formats that
    # already contain both audio and video; this avoids downloading the whole
    # file before playback. DASH-only split A/V is reported clearly below.
    if is_live_hint:
        return "best[protocol*=m3u8][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best"
    return "best[protocol^=http][vcodec!=none][acodec!=none]/best[protocol*=m3u8][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best"


def detect_browser_sessions():
    """Return browser profiles that yt-dlp can *attempt* to read on macOS.

    This intentionally does not export or persist cookies. A detected browser
    means a local profile exists, not that a particular website is logged in.
    """
    home=Path.home()
    checks=[
        ("chrome","Chrome",[
            home/"Library/Application Support/Google/Chrome",
            Path("/Applications/Google Chrome.app")]),
        ("safari","Safari",[
            home/"Library/Safari",
            home/"Library/Containers/com.apple.Safari",
            Path("/Applications/Safari.app")]),
        ("edge","Edge",[
            home/"Library/Application Support/Microsoft Edge",
            Path("/Applications/Microsoft Edge.app")]),
        ("firefox","Firefox",[
            home/"Library/Application Support/Firefox/Profiles",
            Path("/Applications/Firefox.app")]),
    ]
    out=[]
    for key,label,paths in checks:
        present=any(x.exists() for x in paths)
        out.append({"id":key,"label":label,"available":bool(present),
                    "status":"可尝试读取本机登录状态" if present else "未检测到本机配置"})
    return out


def _cookie_tuple(browser):
    # yt-dlp Python API expects (browser, profile, keyring, container).
    return (browser, None, None, None)


def _classify_online_error(err):
    text=str(err or "")
    low=text.lower()
    if any(x in low for x in ["sign in to confirm", "not a bot", "login required", "authentication", "cookies-from-browser", "confirm your age", "age-restricted"]):
        return "AUTH", "网站需要登录/真人验证。请在浏览器中正常登录后，让软件使用该浏览器会话。"
    if any(x in low for x in ["could not copy chrome cookie", "cookie database", "keychain", "decrypt cookie", "cookies could not", "failed to decrypt"]):
        return "COOKIE_ACCESS", "浏览器会话存在，但 macOS 暂未允许读取。可关闭浏览器后重试，或在系统隐私设置中允许 Terminal/Python 访问相关数据。"
    if "drm" in low or "widevine" in low or "fairplay" in low:
        return "DRM", "检测到受 DRM/访问控制保护的媒体；本软件不会绕过 DRM。"
    if any(x in low for x in ["not available in your country", "geo", "geographic restriction"]):
        return "GEO", "该媒体存在地区限制；请在你有权访问的地区/网络环境下播放。"
    if any(x in low for x in ["unsupported url", "no suitable extractor", "unsupported site"]):
        return "UNSUPPORTED", "yt-dlp 当前没有适配这个页面。若页面本身能播放，可尝试 Browser Companion 在原网页叠加字幕。"
    if any(x in low for x in ["timed out", "timeout", "network is unreachable", "temporary failure", "connection reset", "ssl"]):
        return "NETWORK", "网络或证书连接失败。请检查网络后重试。"
    if any(x in low for x in ["requested format is not available", "separate audio", "requested_formats"]):
        return "FORMAT", "该页面当前只提供分离音视频格式，内置在线播放器暂不能直接使用；可尝试原网页 Browser Companion。"
    return "UNKNOWN", "在线视频解析失败。可运行 diagnose_online.command 查看浏览器会话与 yt-dlp 状态。"


def _normalize_info(info):
    if info and info.get("_type") == "playlist":
        entries=[e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("这个链接没有解析到可播放视频。")
        info=entries[0]
    if not info:
        raise RuntimeError("无法解析在线视频。")
    media_url=info.get("url")
    if not media_url:
        req=info.get("requested_downloads") or []
        if len(req)==1:
            media_url=req[0].get("url")
    if not media_url:
        # If yt-dlp resolved split video/audio, we deliberately do not pretend
        # the browser can play a single stream. The in-page Companion remains a
        # good fallback because the original site already has a working player.
        if info.get("requested_formats") or len(info.get("requested_downloads") or [])>1:
            raise RuntimeError("该站点当前只提供分离音视频（DASH）格式；请使用 Browser Companion 在原网页叠加字幕，或先合法保存为本地视频。")
        raise RuntimeError("没有解析到可直接播放的单一媒体流。")
    protocol=str(info.get("protocol") or "")
    ext=str(info.get("ext") or "")
    return {
        "url":media_url,
        "headers":dict(info.get("http_headers") or {}),
        "protocol":protocol,
        "ext":ext,
        "title":str(info.get("title") or "在线视频"),
        "duration":float(info.get("duration") or 0.0),
        "is_live":bool(info.get("is_live") or info.get("live_status") == "is_live"),
    }


def _is_http_media_hint(url):
    try:
        u=urlparse(str(url or "").strip())
    except Exception:
        return False
    if u.scheme not in {"http","https"}:
        return False
    low=(u.path+"?"+u.query).lower()
    return any(x in low for x in (".m3u8",".mpd",".mp4",".m4v",".webm","manifest","playlist"))


def _media_hint_headers(referer="", user_agent=""):
    h={}
    if user_agent:
        h["User-Agent"]=str(user_agent)[:800]
    if referer and str(referer).startswith(("http://","https://")):
        h["Referer"]=str(referer)
        try:
            u=urlparse(referer)
            h["Origin"]=f"{u.scheme}://{u.netloc}"
        except Exception:
            pass
    return h


def _probe_direct_media(url, headers=None, timeout=14):
    """Confirm a browser-discovered media hint is readable by FFmpeg.

    This is deliberately a short audio probe. It does not persist media and it
    does not attempt to bypass DRM or access controls. A failed probe simply
    falls back to normal page/frame resolution.
    """
    ffmpeg=get_ffmpeg_exe()
    cmd=[ffmpeg,"-hide_banner","-loglevel","error"]
    hblob=_ffmpeg_header_blob(headers or {})
    if hblob:
        cmd += ["-headers",hblob]
    cmd += ["-i",url,"-map","0:a:0","-t","0.35","-f","null","-"]
    try:
        cp=subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=timeout)
        if cp.returncode==0:
            return True,""
        err=(cp.stderr or b"").decode("utf-8","replace")[-900:]
        return False,err
    except subprocess.TimeoutExpired:
        return False,"媒体探测超时"
    except Exception as e:
        return False,str(e)


def _direct_media_record(media_hint, referer="", user_agent="", title="", duration=0.0):
    u=urlparse(media_hint)
    path=(u.path or "").lower()
    if ".m3u8" in path or "m3u8" in (u.query or "").lower():
        protocol="m3u8_native"; ext="m3u8"
    elif ".mpd" in path or "mpd" in (u.query or "").lower():
        protocol="http_dash_segments"; ext="mpd"
    else:
        protocol="https" if u.scheme=="https" else "http"
        ext=Path(path).suffix.lstrip(".") or "mp4"
    return {
        "url":media_hint,
        "headers":_media_hint_headers(referer,user_agent),
        "protocol":protocol,
        "ext":ext,
        "title":str(title or "浏览器播放器"),
        "duration":max(0.0,float(duration or 0.0)),
        "is_live":False,
        "session_browser":"",
        "source_kind":"browser_media_hint",
    }


def resolve_online_media(page_url, session_mode="auto", media_hint="", referer="", user_agent="", top_url="", hint_duration=0.0, hint_title=""):
    """Resolve audio/video for online subtitle generation.

    v7.6 adds a browser-playback integration layer. Browser Companion can run
    inside cross-origin iframe players and report a non-DRM HTTP media hint
    observed by the browser. We first verify that hint with FFmpeg. If it is not
    usable, we fall back to yt-dlp on the iframe page and then the top page.

    session_mode: auto | anonymous | chrome | edge | firefox | safari
    Browser cookies are passed directly to yt-dlp for each attempt; no cookie
    file is written by AutoSubtitleSync.
    """
    sessions=detect_browser_sessions()
    available=[x["id"] for x in sessions if x["available"]]
    set_state(browser_sessions=sessions, online_phase="FETCH_STREAM", online_phase_label="解析播放器")

    if session_mode not in {"auto","anonymous","chrome","safari","edge","firefox"}:
        session_mode="auto"
    attempts=[]

    # 1) Browser-observed direct media hint (m3u8/mpd/mp4). We only use it if
    # FFmpeg can read audio normally. blob:/mediasource URLs never reach here.
    media_hint=str(media_hint or "").strip()
    if _is_http_media_hint(media_hint):
        headers=_media_hint_headers(referer or page_url,user_agent)
        set_state(online_phase="FETCH_STREAM",online_phase_label="验证浏览器播放器媒体")
        ok,probe_err=_probe_direct_media(media_hint,headers)
        if ok:
            attempts.append({"method":"浏览器播放器媒体线索","ok":True})
            set_state(online_resolve_attempts=attempts,online_error_kind="",online_error_hint="")
            return _direct_media_record(media_hint,referer or page_url,user_agent,hint_title,hint_duration)
        attempts.append({"method":"浏览器播放器媒体线索","ok":False,"kind":"MEDIA_HINT","error":str(probe_err)[:500]})

    if session_mode=="anonymous":
        order=[None]
    elif session_mode=="auto":
        order=[None]+[b for b in ("chrome","edge","firefox","safari") if b in available]
    else:
        order=[session_mode]

    pages=[]
    for label,u in (("播放器 iframe",page_url),("外层网页",top_url)):
        u=str(u or "").strip()
        if u.startswith(("http://","https://")) and all(u!=x[1] for x in pages):
            pages.append((label,u))
    if not pages:
        raise RuntimeError("浏览器没有提供可解析的 HTTP/HTTPS 播放器地址。")

    last_err=None
    for page_label,url in pages:
        for browser in order:
            label=(f"{page_label} · 公开访问" if browser is None else f"{page_label} · {browser.title()} 会话")
            set_state(online_phase="CHECK_SESSION" if browser else "FETCH_STREAM",
                      online_phase_label=f"尝试 {label}", online_session_browser=browser or "")
            opts={"quiet":True,"no_warnings":True,"noplaylist":True,
                  "format":_online_format_selector(False),"socket_timeout":25,
                  "cachedir":False}
            if referer:
                opts["http_headers"]={"Referer":referer,"User-Agent":user_agent} if user_agent else {"Referer":referer}
            if browser:
                opts["cookiesfrombrowser"]=_cookie_tuple(browser)
            try:
                with YoutubeDL(opts) as ydl:
                    info=ydl.extract_info(url, download=False)
                media=_normalize_info(info)
                media["session_browser"]=browser or ""
                media["source_kind"]="browser_session" if browser else "anonymous"
                attempts.append({"method":label,"ok":True})
                set_state(online_resolve_attempts=attempts, online_session_browser=browser or "",
                          online_error_kind="", online_error_hint="")
                return media
            except Exception as e:
                last_err=e
                kind,hint=_classify_online_error(e)
                attempts.append({"method":label,"ok":False,"kind":kind,"error":str(e)[:500]})
                set_state(online_resolve_attempts=attempts, online_error_kind=kind,
                          online_error_hint=hint, online_session_browser=browser or "")
                continue

    kind,hint=_classify_online_error(last_err)
    if media_hint and not _is_http_media_hint(media_hint):
        hint = "浏览器已检测到播放器，但媒体入口是 blob/MediaSource 或暂未暴露普通 HTTP 媒体地址。请先让视频实际播放几秒再重试；若页面使用 DRM，本软件不会绕过。"
    attempted=" → ".join(x["method"] for x in attempts) or "无"
    raise RuntimeError(f"{hint}\n\n已尝试：{attempted}\n技术信息：{str(last_err)[:700]}")

def _ffmpeg_header_blob(headers):
    out=[]
    for k,v in (headers or {}).items():
        if v and str(k).lower() in {"user-agent","referer","origin","cookie","authorization","accept-language"}:
            out.append(f"{k}: {v}\\r\\n")
    return "".join(out)


def _online_append(cue):
    # Backward-compatible helper used outside semantic rebuilds.
    global ONLINE_REVISION
    with ONLINE_LOCK:
        cue.idx=len(ONLINE_CUES)+1
        ONLINE_CUES.append(cue)
        ONLINE_REVISION += 1
        count=len(ONLINE_CUES)
        rev=ONLINE_REVISION
    set_state(online_count=count, online_revision=rev)


def online_cues_snapshot():
    with ONLINE_LOCK:
        return {
            "revision": ONLINE_REVISION,
            "cues": [{"idx":c.idx,"start":c.start,"end":c.end,"text":c.text,
                      "original":c.original_text,"language":c.language}
                     for c in ONLINE_CUES]
        }

def companion_sync_snapshot(seq, known_revision=-1):
    """Snapshot used by the in-page Browser Companion overlay.

    The userscript polls this localhost endpoint.  A sequence id prevents an old
    browser tab from accidentally rendering captions produced for a newer tab.
    Cues are only returned when the semantic timeline revision changes.
    """
    st=get_state()
    current=int(st.get("companion_seq") or 0)
    if int(seq or 0) != current or current <= 0:
        return {"ok":False,"stale":True,"seq":current}
    snap=online_cues_snapshot()
    out={
        "ok":True, "stale":False, "seq":current,
        "running":bool(st.get("online_running")),
        "done":bool(st.get("online_done")),
        "error":str(st.get("online_error") or st.get("error") or ""),
        "message":str(st.get("online_message") or ""),
        "processed_time":float(st.get("online_processed_time") or 0.0),
        "stable_through":float(st.get("online_stable_through") or 0.0),
        "lookahead":float(st.get("online_lookahead") or 90.0),
        "start_at":float(st.get("online_start_at") or 0.0),
        "is_live":bool(st.get("online_is_live")),
        "audio_mode":bool(st.get("online_audio_mode")),
        "revision":int(snap.get("revision") or 0),
        "count":len(snap.get("cues") or []),
        "title":str(st.get("online_title") or st.get("companion_title") or "")
    }
    if int(known_revision) != out["revision"]:
        out["cues"]=snap.get("cues") or []
    return out


def _stream_translate(text, fallback_lang, target, bilingual):
    src=detect_text_language(text, fallback_lang if fallback_lang in SUPPORTED_LANGS else "en")
    if target=="original" or src==target:
        return text,src
    translated=translate_text(text,src,target)
    if bilingual and translated.strip()!=text.strip():
        return text.strip()+"\\n"+translated.strip(),src
    return translated.strip(),target


def _merge_raw_window(segs, window_start, replace_from):
    """Replace the mutable ASR tail with a newly decoded overlapping window."""
    global ONLINE_RAW
    fresh=[]
    for seg in segs:
        text=(seg.text or "").strip()
        if not text:
            continue
        gs=window_start+float(seg.start); ge=window_start+float(seg.end)
        if ge<=replace_from-0.25:
            continue
        fresh.append(ASRSeg(gs,ge,text,None))
    with ONLINE_LOCK:
        kept=[r for r in ONLINE_RAW if r.end<=replace_from]
        merged=kept[:]
        for r in fresh:
            if merged and r.start < merged[-1].end-0.35:
                # If Whisper moved a boundary slightly, prefer the newer segmentation.
                if r.end <= merged[-1].end+0.20 and fuzz.ratio(normalize_text(r.text), normalize_text(merged[-1].text)) >= 72:
                    continue
                r.start=max(r.start, merged[-1].end+0.01)
            if r.end>r.start:
                merged.append(r)
        ONLINE_RAW=merged


def _ends_sentence(text):
    return bool(re.search(r'[.!?。！？…]["\'”’）)】]*$', (text or '').strip()))


def _looks_continuation(text):
    t=(text or '').strip()
    if not t:
        return False
    # English/German/French/Spanish continuation words and lowercase starts are useful
    # weak signals; Chinese relies mainly on punctuation and pause structure.
    first=t[0]
    if first.isalpha() and first.islower():
        return True
    low=t.lower()
    prefixes=(
        'and ','but ','because ','so ','which ','that ','while ','although ','however ',
        'und ','aber ','weil ','dass ','während ','obwohl ','donc ','mais ','parce ',
        'et ','que ','y ','pero ','porque ','aunque ','entonces '
    )
    return low.startswith(prefixes)


def _semantic_sentence_groups(raw, stable_through, processed_time):
    """Re-segment ASR fragments using future transcript as mutable semantic context.

    The whole decoded future transcript participates in boundary decisions. Only groups
    whose end is before stable_through are exposed as stable captions; the newest tail
    remains mutable and is re-decoded/re-segmented on every pass.
    """
    if not raw:
        return []
    groups=[]; cur=None
    for i,r in enumerate(raw):
        tx=(r.text or '').strip()
        if not tx:
            continue
        if cur is None:
            cur=[r.start,r.end,tx]
            continue
        gap=max(0.0,r.start-cur[1])
        cur_dur=cur[1]-cur[0]
        combined_len=len(cur[2])+1+len(tx)
        terminal=_ends_sentence(cur[2])
        continuation=_looks_continuation(tx)
        # Future-aware boundary policy: punctuation + an independent next phrase is a
        # strong boundary; absent punctuation, short pauses and continuation signals
        # keep clauses together. Hard caps prevent unreadably huge subtitle cards.
        hard_break = gap>=1.35 or cur_dur>=17.0 or combined_len>=190
        soft_break = terminal and gap>=0.06
        clause_break = (gap>=0.72 and cur_dur>=5.0 and not continuation)
        if hard_break or soft_break or clause_break:
            groups.append(cur); cur=[r.start,r.end,tx]
        else:
            cur[1]=r.end
            joiner='' if (cur[2] and ord(cur[2][-1])>127 and tx and ord(tx[0])>127) else ' '
            cur[2]=(cur[2].rstrip()+joiner+tx.lstrip()).strip()
    if cur is not None:
        groups.append(cur)

    # Only expose sentence groups that are safely behind the mutable tail. The groups
    # are rebuilt repeatedly, so captions the viewer has not reached yet can improve as
    # up to ~1–2 minutes of future transcript arrives.
    stable=[]
    for g in groups:
        if g[1] <= stable_through+0.02:
            stable.append(g)
    return stable


def _rebuild_semantic_cues(fallback, target, bilingual, processed_time, mutable_tail):
    """Rebuild the stable subtitle timeline from the latest future-aware transcript."""
    global ONLINE_CUES, ONLINE_REVISION
    stable_through=max(0.0, processed_time-mutable_tail)
    with ONLINE_LOCK:
        raw=list(ONLINE_RAW)
    groups=_semantic_sentence_groups(raw,stable_through,processed_time)
    rebuilt=[]
    for gs,ge,tx in groups:
        shown,shown_lang=_stream_translate(tx,fallback,target,bilingual)
        rebuilt.append(Cue(len(rebuilt)+1,gs,ge,shown,shown_lang,tx))
    with ONLINE_LOCK:
        # Avoid revision churn if the semantic timeline did not materially change.
        old_sig=[(round(c.start,2),round(c.end,2),c.text) for c in ONLINE_CUES]
        new_sig=[(round(c.start,2),round(c.end,2),c.text) for c in rebuilt]
        if old_sig!=new_sig:
            ONLINE_CUES=rebuilt
            ONLINE_REVISION += 1
        rev=ONLINE_REVISION; count=len(ONLINE_CUES)
    set_state(online_count=count, online_revision=rev, online_stable_through=stable_through)
    return stable_through


def online_worker(page_url, model_size, lang, mixed, target, bilingual, lookahead, start_at=0.0, session_mode="auto", media_hint="", referer="", user_agent="", top_url="", hint_duration=0.0, hint_title=""):
    global ONLINE_PROC, RESULT_CACHE, CUES_CACHE, ONLINE_RAW
    proc=None
    try:
        set_state(online_message="正在解析在线视频链接…", online_error="", online_done=False, online_count=0,
                  online_phase="FETCH_STREAM",online_phase_label="解析视频",online_session_mode=session_mode,
                  online_error_kind="",online_error_hint="",online_resolve_attempts=[])
        media=resolve_online_media(page_url,session_mode,media_hint,referer,user_agent,top_url,hint_duration,hint_title)
        is_live=bool(media.get("is_live"))
        duration=float(media.get("duration") or 0.0)
        start_at=max(0.0,float(start_at or 0.0))
        if is_live:
            start_at=0.0
        elif duration>0:
            start_at=min(start_at,max(0.0,duration-1.0))
        # When the browser hands off from the middle of a sentence, decode a short
        # pre-roll so ASR has the preceding clause/context. The local player still
        # begins at start_at; only recognition starts slightly earlier.
        decode_start=(0.0 if is_live else max(0.0,start_at-15.0))
        ONLINE_MEDIA.clear(); ONLINE_MEDIA.update(media)
        protocol=(media.get("protocol") or "").lower(); ext=(media.get("ext") or "").lower()
        is_hls=("m3u8" in protocol) or ext in {"m3u8","m3u"}
        player_url=media["url"] if is_hls else "/api/online/media"
        set_state(online_title=media["title"],online_player_url=player_url,online_is_live=media["is_live"],online_duration=media["duration"],online_start_at=start_at,
                  online_message="媒体已解析，正在加载 Whisper…",online_phase="BUFFERING",online_phase_label="建立 Semantic Look-ahead",
                  online_session_browser=media.get("session_browser", ""))
        model=load_whisper(model_size)
        ffmpeg=get_ffmpeg_exe()
        cmd=[ffmpeg,"-hide_banner","-loglevel","error"]
        hblob=_ffmpeg_header_blob(media.get("headers"))
        if hblob:
            cmd += ["-headers",hblob]
        if decode_start>0 and not is_live:
            cmd += ["-ss",f"{decode_start:.3f}"]
        cmd += ["-i",media["url"],"-vn","-ac","1","-ar","16000","-f","s16le","pipe:1"]
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        ONLINE_PROC=proc
        if proc.stdout is None:
            raise RuntimeError("无法读取在线视频音频流。")

        rate=16000; bytes_per_sec=rate*2
        # VOD is decoded in large sequential blocks so the ASR can run faster than
        # playback. Live media uses a much smaller block because future audio does not exist.
        chunk_sec=30.0 if not is_live else 8.0
        overlap_sec=6.0 if not is_live else 2.0
        step_sec=chunk_sec-overlap_sec
        chunk_bytes=int(chunk_sec*bytes_per_sec)
        step_bytes=int(step_sec*bytes_per_sec)
        read_bytes=int(1.0*bytes_per_sec)
        buf=bytearray(); buf_start=(decode_start if not is_live else 0.0); fallback="en"; eof=False
        mutable_tail=12.0 if not is_live else 3.0
        lookahead_target=float(lookahead if not is_live else 3.0)
        set_state(online_lookahead=lookahead_target,online_processed_time=decode_start,online_stable_through=decode_start,
                  online_semantic_tail=mutable_tail,
                  online_message=(f"Semantic Look-ahead 蓄水中 · 目标领先 {lookahead_target/60:.1f} 分钟" if not is_live else "直播字幕已启动 · 使用约 3 秒稳定缓冲"))

        while not ONLINE_STOP.is_set():
            # Fill one inference chunk. FFmpeg is intentionally not throttled with -re,
            # so VOD can be decoded and recognized ahead of the player.
            while len(buf)<chunk_bytes and not eof and not ONLINE_STOP.is_set():
                block=proc.stdout.read(read_bytes)
                if not block:
                    eof=True; break
                buf.extend(block)
            if ONLINE_STOP.is_set():
                break
            if not buf:
                break
            if len(buf)<int(2.0*bytes_per_sec) and eof:
                break

            use_len=min(len(buf),chunk_bytes)
            audio=np.frombuffer(bytes(buf[:use_len]),dtype=np.int16).astype(np.float32)/32768.0
            window_start=buf_start
            window_dur=len(audio)/rate
            language=None if lang=="auto" else lang
            kwargs=dict(language=language,vad_filter=True,beam_size=1,word_timestamps=False,condition_on_previous_text=False)
            if mixed and lang=="auto":
                kwargs["multilingual"]=True
            seg_iter,info=model.transcribe(audio,**kwargs)
            segs=list(seg_iter)
            fallback=getattr(info,"language",fallback) or fallback

            # The first overlap portion was already represented by the previous chunk.
            replace_from=window_start if window_start<=0.01 else window_start+overlap_sec*0.45
            _merge_raw_window(segs,window_start,replace_from)
            processed_time=window_start+window_dur
            stable_through=_rebuild_semantic_cues(fallback,target,bilingual,processed_time,mutable_tail)

            phase="READY" if (is_live or stable_through-start_at>=lookahead_target) else "BUFFERING"
            set_state(online_processed_time=processed_time, online_phase=phase,
                      online_phase_label=("字幕缓冲就绪" if phase=="READY" else "建立 Semantic Look-ahead"),
                      online_message=(f"Semantic Look-ahead · 已识别到 {processed_time/60:.1f} 分钟 · 稳定字幕到 {stable_through/60:.1f} 分钟 · {len(ONLINE_CUES)} 条" if not is_live else f"直播识别中 · {len(ONLINE_CUES)} 条字幕"))

            if eof:
                break
            consume=min(step_bytes,len(buf))
            del buf[:consume]
            buf_start += consume/bytes_per_sec

        # Rebuild once at EOF with no mutable tail so the final sentence is not held back.
        if not ONLINE_STOP.is_set():
            processed=float(get_state().get("online_processed_time") or 0.0)
            _rebuild_semantic_cues(fallback,target,bilingual,processed,0.0)
        set_state(online_message="在线字幕已停止" if ONLINE_STOP.is_set() else "在线视频识别完成",online_running=False,online_done=True,
                  online_phase=("IDLE" if ONLINE_STOP.is_set() else "COMPLETE"),online_phase_label=("已停止" if ONLINE_STOP.is_set() else "识别完成"),
                  online_stable_through=max(float(get_state().get("online_stable_through") or 0.0),float(media.get("duration") or 0.0) if not ONLINE_STOP.is_set() else 0.0))
        with ONLINE_LOCK:
            final=[Cue(i+1,c.start,c.end,c.text,c.language,c.original_text) for i,c in enumerate(ONLINE_CUES)]
        CUES_CACHE=final
        result={"mode":"online","cues":len(final),"duration":media.get("duration",0.0),"model":model_size,"target":target,"bilingual":bool(bilingual),"title":media.get("title","在线视频"),"lookahead":float(lookahead)}
        RESULT_CACHE=result; set_state(result=result)
    except Exception as e:
        kind,hint=_classify_online_error(e)
        set_state(online_running=False,online_done=False,online_error=str(e),online_message="在线视频解析/识别失败",error=str(e),
                  online_phase="ERROR",online_phase_label="解析失败",online_error_kind=kind,online_error_hint=hint)
    finally:
        ONLINE_PROC=None
        if proc:
            try:
                if proc.poll() is None: proc.terminate()
            except Exception: pass


def start_online(page_url, model_size, lang, mixed, target, bilingual, lookahead=90.0, start_at=0.0, session_mode="auto", media_hint="", referer="", user_agent="", top_url="", hint_duration=0.0, hint_title=""):
    global RESULT_CACHE, CUES_CACHE, ONLINE_PROC, ONLINE_RAW, ONLINE_REVISION
    if not page_url.startswith(("http://","https://")):
        raise ValueError("请输入以 http:// 或 https:// 开头的视频网址。")
    ONLINE_STOP.set()
    if ONLINE_PROC:
        try: ONLINE_PROC.terminate()
        except Exception: pass
    time.sleep(0.05); ONLINE_STOP.clear()
    with ONLINE_LOCK:
        ONLINE_CUES.clear(); ONLINE_RAW.clear(); ONLINE_REVISION=0
    CUES_CACHE=None; RESULT_CACHE=None
    start_at=max(0.0,float(start_at or 0.0))
    set_state(online_running=True,online_audio_mode=False,online_url=page_url,online_title="",online_message="准备在线字幕…",online_player_url="",online_is_live=False,online_count=0,online_error="",online_done=False,online_duration=0.0,online_processed_time=start_at,online_stable_through=start_at,online_start_at=start_at,online_lookahead=float(lookahead),online_revision=0,result=None,error="",exported="",
              online_phase="INIT",online_phase_label="准备任务",online_session_mode=session_mode,online_session_browser="",
              online_error_kind="",online_error_hint="",online_resolve_attempts=[],browser_sessions=detect_browser_sessions())
    threading.Thread(target=online_worker,args=(page_url,model_size,lang,mixed,target,bilingual,float(lookahead),start_at,session_mode,media_hint,referer,user_agent,top_url,hint_duration,hint_title),daemon=True).start()


def stop_online():
    global ONLINE_PROC
    ONLINE_STOP.set()
    if ONLINE_PROC:
        try: ONLINE_PROC.terminate()
        except Exception: pass


def export_online_srt():
    with ONLINE_LOCK:
        cues=[Cue(i+1,c.start,c.end,c.text,c.language,c.original_text) for i,c in enumerate(ONLINE_CUES)]
    if not cues:
        raise ValueError("目前还没有可导出的在线字幕。")
    title=re.sub(r"[^\\w\\- .()\\[\\]一-龥]+","_",ONLINE_MEDIA.get("title") or "online_video").strip()[:80] or "online_video"
    folder=Path.home()/"Downloads"; out=folder/f"{title}_stream_subtitles.srt"; i=2
    while out.exists():
        out=folder/f"{title}_stream_subtitles_{i}.srt"; i+=1
    write_srt(cues,out); return out


def proxy_online_media(handler):
    url=ONLINE_MEDIA.get("url") or ""
    if not url:
        handler.send_error(404); return
    headers=dict(ONLINE_MEDIA.get("headers") or {})
    rng=handler.headers.get("Range")
    if rng: headers["Range"]=rng
    req=urllib.request.Request(url,headers=headers,method="GET")
    try:
        with urllib.request.urlopen(req,timeout=30) as resp:
            handler.send_response(getattr(resp,"status",200))
            for k in ("Content-Type","Content-Length","Content-Range","Accept-Ranges","Last-Modified","ETag"):
                v=resp.headers.get(k)
                if v: handler.send_header(k,v)
            handler.send_header("Cache-Control","no-store"); handler.end_headers()
            while True:
                chunk=resp.read(256*1024)
                if not chunk: break
                try: handler.wfile.write(chunk)
                except (BrokenPipeError,ConnectionResetError): break
    except urllib.error.HTTPError as e:
        handler.send_error(e.code,str(e))
    except Exception as e:
        handler.send_error(502,str(e))
HTML=r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Auto Subtitle Sync v8.2</title>
<style>
:root{--bg:#f5f6f8;--text:#16181d;--muted:#6c727f;--line:#e7e9ee;--primary:#2563eb;--danger:#b42318;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif;color:var(--text)}*{box-sizing:border-box}body{margin:0;background:var(--bg)}button,select,input{font:inherit}.shell{max-width:1120px;margin:auto;padding:32px 24px 56px}.top{display:flex;justify-content:space-between;margin-bottom:24px}.brand{display:flex;gap:13px;align-items:center}.logo{width:44px;height:44px;border-radius:13px;background:#111827;color:white;display:grid;place-items:center;font-weight:800}.brand h1{margin:0 0 4px;font-size:25px}.sub{font-size:13px;color:var(--muted)}.ver{font-size:12px;color:#667085;background:white;border:1px solid var(--line);border-radius:999px;padding:6px 10px;height:max-content}.grid{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:18px}.card{background:white;border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:16px;box-shadow:0 1px 2px #10182808}.head{display:flex;gap:10px;margin-bottom:15px}.step{width:26px;height:26px;border-radius:8px;background:#eef3ff;color:#315bc9;display:grid;place-items:center;font-size:12px;font-weight:700}.h{font-size:15px;font-weight:700}.d{font-size:12px;color:var(--muted);margin-top:3px}.tabs{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;background:#f1f2f4;padding:4px;border-radius:11px;margin-bottom:16px}.tab{border:0;background:transparent;color:#626874;padding:9px;border-radius:8px;font-weight:650;cursor:pointer}.tab.on{background:white;color:#111827;box-shadow:0 1px 4px #11182716}.row{display:grid;grid-template-columns:92px minmax(0,1fr) auto;gap:12px;align-items:center;padding:10px 0}.label{font-size:13px;font-weight:650}.path,.url{min-width:0;color:#535a67;background:#f8f9fb;border:1px solid #eceef2;border-radius:9px;padding:10px 11px;font-size:13px}.path{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.url{width:100%;outline:none}.btn{border:0;border-radius:9px;padding:9px 13px;cursor:pointer;font-size:13px;font-weight:650}.secondary{background:#f1f3f6;color:#252a33}.primary{background:var(--primary);color:white}.danger{background:#fff0ee;color:var(--danger)}.btn:disabled{opacity:.42;cursor:default}.settings{display:grid;grid-template-columns:1fr 1fr;gap:14px}.field{display:flex;flex-direction:column;gap:7px}.fl{font-size:12.5px;font-weight:650}.note{font-size:11.5px;color:#8a909b;line-height:1.4}select{width:100%;border:1px solid #dfe2e8;background:white;border-radius:9px;padding:9px 10px}.toggle{display:flex;justify-content:space-between;align-items:center;border:1px solid #eceef2;background:#fafbfc;border-radius:11px;padding:12px}.toggle b{display:block;font-size:13px}.toggle span{font-size:11.5px;color:var(--muted)}.switch{position:relative;width:38px;height:22px}.switch input{opacity:0}.slider{position:absolute;inset:0;background:#cfd4dc;border-radius:999px}.slider:before{content:"";position:absolute;width:16px;height:16px;left:3px;top:3px;background:white;border-radius:50%;transition:.18s}.switch input:checked+.slider{background:var(--primary)}.switch input:checked+.slider:before{transform:translateX(16px)}.actions{display:flex;gap:9px;flex-wrap:wrap}.progress{height:7px;background:#eef0f3;border-radius:999px;overflow:hidden;margin-top:15px}.bar{height:100%;background:var(--primary);width:0}.status{display:flex;justify-content:space-between;color:#707784;font-size:12px;margin-top:8px}.side-title{font-size:13px;font-weight:700;margin-bottom:12px}.empty{font-size:12.5px;color:#9096a1;line-height:1.6}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px}.metric{background:#f8f9fb;border:1px solid #eceef2;border-radius:10px;padding:10px}.metric small{display:block;color:#858c98;margin-bottom:4px}.metric b{font-size:13px}.hidden{display:none!important}.advanced{margin-top:13px;border-top:1px solid #f0f1f3;padding-top:12px}details summary{cursor:pointer;font-size:12.5px;color:#676e7b;font-weight:600}.details{margin-top:9px;background:#f8f9fb;border:1px solid #eceef2;border-radius:10px;padding:11px;font-size:12px;color:#656c78;line-height:1.6}.player-card{padding:14px}.video-wrap{position:relative;background:#080b10;border-radius:13px;overflow:hidden;aspect-ratio:16/9}.video-wrap video{width:100%;height:100%;display:block;background:#000}.caption{position:absolute;left:7%;right:7%;bottom:6%;text-align:center;color:white;font-size:20px;font-weight:650;line-height:1.35;text-shadow:0 2px 5px #000,0 0 10px #000;background:#0006;border-radius:8px;padding:7px 10px;white-space:pre-line;pointer-events:none}.online-meta{display:flex;justify-content:space-between;gap:10px;margin-top:10px;color:#757c88;font-size:12px}.buffer-pill{display:inline-flex;align-items:center;gap:6px;background:#f3f6fb;border:1px solid #e3e9f3;border-radius:999px;padding:4px 8px}.dot{width:7px;height:7px;border-radius:50%;background:#98a2b3}.dot.ready{background:#12b76a}.dot.wait{background:#f79009}.transcript{margin-top:12px;max-height:180px;overflow:auto;border-top:1px solid #eef0f3}.cue{display:grid;grid-template-columns:56px 1fr;gap:8px;padding:8px 2px;border-bottom:1px solid #f1f2f4;font-size:12px}.cue-time{color:#8b929e}.cue-text{white-space:pre-line;line-height:1.45}.footer{text-align:center;color:#969ca7;font-size:11.5px;margin-top:20px}.session-panel{margin-top:10px;padding:11px 12px;border:1px solid #eceef2;background:#fafbfc;border-radius:11px}.session-line{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:#626a77}.session-chip{display:inline-flex;align-items:center;gap:5px;padding:4px 7px;background:white;border:1px solid #e4e7ec;border-radius:999px}.session-chip .mini{width:6px;height:6px;border-radius:50%;background:#12b76a}.session-chip.off .mini{background:#c5cad2}.errorbox{margin-top:12px;padding:12px;border-radius:10px;background:#fff5f4;border:1px solid #ffd9d5;color:#9f2a20;font-size:12px;line-height:1.55;white-space:pre-line}.phase{font-weight:650;color:#475467}@media(max-width:850px){.grid{grid-template-columns:1fr}.settings{grid-template-columns:1fr}.caption{font-size:17px}.row{grid-template-columns:72px minmax(0,1fr) auto}}
</style></head><body><div class="shell"><div class="top"><div class="brand"><div class="logo">AS</div><div><h1>Auto Subtitle Sync</h1><div class="sub">本地字幕 · 翻译 · 校准 · Look-ahead 在线字幕</div></div></div><div class="ver">v8.2 · 三种抓音</div></div><div class="grid"><main>
<section class="card"><div class="head"><div class="step">1</div><div><div class="h">选择任务</div><div class="d">本地视频与在线链接使用同一套识别、翻译和时间轴系统。</div></div></div><div class="tabs"><button id="tabGen" class="tab on" onclick="setMode('generate')">生成字幕</button><button id="tabAlign" class="tab" onclick="setMode('align')">校准 SRT</button><button id="tabOnline" class="tab" onclick="setMode('online')">在线字幕</button></div><div id="localInput"><div class="row"><div class="label">视频</div><div id="video" class="path">尚未选择</div><button class="btn secondary" onclick="pick('video')">选择</button></div><div id="srtRow" class="row hidden"><div class="label">字幕</div><div id="srt" class="path">尚未选择</div><button class="btn secondary" onclick="pick('srt')">选择</button></div></div><div id="onlineInput" class="hidden"><div class="row"><div class="label">视频网址</div><input id="onlineUrl" class="url" placeholder="粘贴公开或你有权访问的视频链接"><button class="btn secondary" onclick="pasteUrl()">粘贴</button></div><div class="row"><div class="label">访问会话</div><select id="sessionMode"><option value="auto" selected>自动 · 公开访问失败后尝试浏览器</option><option value="anonymous">仅公开访问</option><option value="chrome">Chrome</option><option value="safari">Safari</option><option value="edge">Edge</option><option value="firefox">Firefox</option></select><button class="btn secondary" onclick="refreshSessions()">检测</button></div><div class="session-panel"><div class="session-line"><span class="phase">本机浏览器</span><span id="browserSessions">正在检测…</span></div><div class="note" style="margin-top:6px">“可尝试”只表示检测到本机浏览器配置；软件不会导出或保存 cookies。需要登录的网站请先在浏览器中正常登录。</div></div></div></section>
<section class="card"><div class="head"><div class="step">2</div><div><div class="h">处理设置</div><div class="d">在线模式建议 Base 或 Small，以保证识别速度跟得上播放。</div></div></div><div class="settings"><label class="field"><span class="fl">识别模型</span><select id="model"><option value="tiny">Tiny · 最快</option><option value="base">Base · 流式推荐</option><option value="small" selected>Small · 推荐</option><option value="medium">Medium · 更准确</option><option value="large-v3">Large v3 · 最高质量</option></select><span class="note">在线模式模型越大，延迟越高。</span></label><label class="field"><span class="fl">音频语言</span><select id="lang"><option value="auto" selected>自动检测</option><option value="zh">中文</option><option value="en">English</option><option value="de">Deutsch</option><option value="fr">Français</option><option value="es">Español</option></select><span class="note">多语言视频保持自动检测。</span></label><label id="targetField" class="field"><span class="fl">输出字幕</span><select id="target" onchange="syncBilingual()"><option value="original">保持原语言</option><option value="zh" selected>中文</option><option value="en">English</option><option value="de">Deutsch</option><option value="fr">Français</option><option value="es">Español</option></select><span class="note">不同语言时会实时翻译。</span></label><label id="lookaheadField" class="field hidden"><span class="fl">Semantic Look-ahead</span><select id="lookahead"><option value="30">30 秒 · 快速</option><option value="60">60 秒 · 均衡</option><option value="90" selected>90 秒 · 推荐</option><option value="120">120 秒 · 高质量</option><option value="180">180 秒 · 极致</option></select><span class="note">VOD 先识别未来 1–2 分钟文本，再利用后文确认断句与翻译。</span></label><div class="field"><div class="toggle"><div><b>混合语言识别</b><span>不同段落自动切换语言</span></div><label class="switch"><input id="mixed" type="checkbox" checked><span class="slider"></span></label></div><div id="bilingualRow" class="toggle"><div><b>双语字幕</b><span>原文 + 翻译同时显示</span></div><label class="switch"><input id="bilingual" type="checkbox"><span class="slider"></span></label></div></div></div><div class="advanced"><details><summary>高级说明</summary><div class="details"><ul><li>VOD 默认使用 Semantic Look-ahead：后台提前识别 90 秒左右的未来文本，并持续修订尚未播放的断句；直播只能使用短稳定缓冲。</li><li>混合语言最适合“德文一段、英文一段”；同一句内频繁切换仍可能识别错。</li><li>需要登录的网站可使用本机浏览器会话；不会导出 Cookie，也不会绕过 DRM、付费墙或访问控制。</li></ul></div></details></div></section>
<section class="card"><div class="head"><div class="step">3</div><div><div class="h">运行与导出</div><div id="runDesc" class="d">处理完成后可导出 SRT 或带字幕 MP4。</div></div></div><div class="actions"><button id="run" class="btn primary" onclick="startTask()">开始生成字幕</button><button id="stopOnline" class="btn danger hidden" onclick="stopOnline()">停止在线字幕</button><button id="export" class="btn secondary" onclick="exportSrt()" disabled>导出 SRT</button><button id="exportMp4" class="btn secondary" onclick="exportMp4()" disabled>导出字幕 MP4</button></div><div class="progress"><div id="bar" class="bar"></div></div><div class="status"><span id="msg">请选择视频</span><span id="pct">0%</span></div><div id="onlineErrorBox" class="errorbox hidden"></div></section>
<section id="onlinePlayerCard" class="card player-card hidden"><div class="video-wrap"><video id="onlineVideo" controls playsinline preload="metadata"></video><div id="caption" class="caption"></div></div><div class="online-meta"><span id="onlineTitle">等待解析视频</span><span class="buffer-pill"><span id="bufferDot" class="dot"></span><span id="onlineBuffer">等待 Look-ahead</span></span><span id="onlineCount">0 条字幕</span></div><div id="transcript" class="transcript"></div></section></main><aside><section class="card"><div class="side-title">处理结果</div><div id="result" class="empty">完成一次任务后，这里会显示摘要。</div></section><section class="card"><div class="side-title">浏览器 Companion</div><div class="empty">在原视频网站直接显示本机生成的 Semantic Look-ahead 字幕；无需切换播放器。</div><div style="margin-top:10px"><a href="/companion.user.js" style="display:inline-block;text-decoration:none;background:#f1f3f6;color:#252a33;border-radius:9px;padding:9px 12px;font-size:12px;font-weight:650">安装 Tampermonkey 脚本</a></div></section><section class="card"><div class="side-title">实时字幕（音频桥）</div><div class="empty">不用粘贴链接：直接抓取网页播放器的声音，边播边出字幕（约慢几秒）。需要 Chrome / Edge + Tampermonkey。</div><div style="margin-top:10px"><a href="/bridge.user.js" style="display:inline-block;text-decoration:none;background:#2563eb;color:#fff;border-radius:9px;padding:9px 12px;font-size:12px;font-weight:650">安装实时字幕脚本</a></div></section><section class="card"><div class="side-title">在线模式</div><div class="empty">VOD 使用 Semantic Look-ahead 蓄水池：先积累未来文本，再播放。未来字幕会持续修订；领先量低于低水位时自动缓冲，恢复目标水位后继续。</div></section></aside></div><div class="footer">模型与字幕处理在本机完成；在线视频内容从原始网站传输。</div></div>
<script>
const E=id=>document.getElementById(id);let mode='generate',onlineCues=[],lastPlayerUrl='',onlineState={},wantedPlay=false,autoPauseAction=false,lastCompanionSeq=0,pendingStartAt=0,lastAppliedStart=-1;async function api(url,opt){let r=await fetch(url,opt);let x=await r.json();if(!r.ok)throw new Error(x.error||'操作失败');return x}function setMode(m){mode=m;E('tabGen').classList.toggle('on',m==='generate');E('tabAlign').classList.toggle('on',m==='align');E('tabOnline').classList.toggle('on',m==='online');E('localInput').classList.toggle('hidden',m==='online');E('onlineInput').classList.toggle('hidden',m!=='online');E('srtRow').classList.toggle('hidden',m!=='align');E('targetField').classList.toggle('hidden',m==='align');E('bilingualRow').classList.toggle('hidden',m==='align');E('onlinePlayerCard').classList.toggle('hidden',m!=='online');E('lookaheadField').classList.toggle('hidden',m!=='online');E('exportMp4').classList.toggle('hidden',m==='online');E('stopOnline').classList.toggle('hidden',m!=='online');E('run').textContent=m==='generate'?'开始生成字幕':m==='align'?'开始校准字幕':'开始在线字幕';E('runDesc').textContent=m==='online'?'解析视频、建立浏览器会话，并用 Semantic Look-ahead 生成字幕。':'处理完成后可导出 SRT 或带字幕 MP4。';syncBilingual();refresh()}function syncBilingual(){let ok=mode!=='align'&&E('target').value!=='original';E('bilingual').disabled=!ok;if(!ok)E('bilingual').checked=false;E('bilingualRow').style.opacity=ok?'1':'.55'}async function pasteUrl(){try{E('onlineUrl').value=await navigator.clipboard.readText()}catch(e){}}async function refreshSessions(){try{let x=await api('/api/browser-sessions');let arr=x.sessions||[];E('browserSessions').innerHTML=arr.map(b=>`<span class="session-chip ${b.available?'':'off'}"><span class="mini"></span>${b.label} · ${b.available?'可尝试':'未检测'}</span>`).join(' ')}catch(e){E('browserSessions').textContent='检测失败'}}async function pick(k){try{await api('/api/pick?kind='+k);refresh()}catch(e){alert(e.message)}}function opts(){return {model:E('model').value,lang:E('lang').value,mixed:E('mixed').checked,target:E('target').value,bilingual:E('bilingual').checked,lookahead:Number(E('lookahead').value||90),session_mode:E('sessionMode')?E('sessionMode').value:'auto'}}async function startTask(){try{if(mode==='online'){onlineCues=[];onlineRevision=-1;lastPlayerUrl='';wantedPlay=false;onlineState={};E('transcript').innerHTML='';await api('/api/online/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:E('onlineUrl').value.trim(),...opts()})})}else{await api('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,...opts()})})}refresh()}catch(e){alert(e.message)}}async function stopOnline(){try{await api('/api/online/stop',{method:'POST'});refresh()}catch(e){alert(e.message)}}async function exportSrt(){try{let x=mode==='online'?await api('/api/online/export',{method:'POST'}):await api('/api/export',{method:'POST'});alert('已导出：\n'+x.path)}catch(e){alert(e.message)}}async function exportMp4(){try{await api('/api/export-mp4',{method:'POST'});refresh()}catch(e){alert(e.message)}}function langName(c){return {zh:'中文',en:'English',de:'Deutsch',fr:'Français',es:'Español',original:'原语言'}[c]||c||'未知'}function metric(k,v){return `<div class="metric"><small>${k}</small><b>${v}</b></div>`}function fmtResult(r){if(!r)return '<div class="empty">完成一次任务后，这里会显示摘要。</div>';if(r.mode==='online')return `<div class="metrics">${metric('状态','在线字幕完成')}${metric('字幕条数',r.cues)}${metric('输出语言',langName(r.target))}${metric('识别模型',r.model)}</div>`;if(r.mode==='generate')return `<div class="metrics">${metric('状态','字幕已生成')}${metric('字幕条数',r.cues)}${metric('识别语言',r.language_summary||langName(r.detected_language))}${metric('输出语言',langName(r.target))}</div>`;return `<div class="metrics">${metric('状态','校准完成')}${metric('固定偏移',(r.b>=0?'+':'')+r.b.toFixed(3)+' 秒')}${metric('置信度',r.confidence.toFixed(1)+'%')}</div>`}function esc(s){return String(s||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}let onlineRevision=-1;async function pullCues(){if(mode!=='online')return;try{let x=await api('/api/online/cues');if(Number(x.revision)===onlineRevision)return;onlineRevision=Number(x.revision||0);onlineCues=x.cues||[];let tail=onlineCues.slice(-8);E('transcript').innerHTML=tail.map(c=>`<div class="cue"><div class="cue-time">${Math.floor(c.start/60)}:${String(Math.floor(c.start%60)).padStart(2,'0')}</div><div class="cue-text">${esc(c.text)}</div></div>`).join('');E('transcript').scrollTop=E('transcript').scrollHeight}catch(e){}}function manageLookahead(){if(mode!=='online')return;let v=E('onlineVideo');let s=onlineState||{};let isLive=!!s.online_is_live;if(isLive){E('onlineBuffer').textContent='直播 · 稳定缓冲';E('bufferDot').className='dot ready';return}let target=Number(s.online_lookahead||E('lookahead').value||90),stable=Number(s.online_stable_through||0),t=Number(v.currentTime||0),lead=Math.max(0,stable-t),done=!!s.online_done;let minLead=Math.max(8,target*0.50);let ready=done||lead>=target;let leadText=lead>=60?(lead/60).toFixed(1)+' min ahead':lead.toFixed(0)+'s ahead';E('onlineBuffer').textContent=(done?'已完成 · ':ready?'语义缓冲就绪 · ':'蓄水中 · ')+leadText;E('bufferDot').className='dot '+(ready?'ready':'wait');if(wantedPlay&&!done){if(!v.paused&&lead<minLead){autoPauseAction=true;v.pause();setTimeout(()=>autoPauseAction=false,0)}else if(v.paused&&lead>=target){autoPauseAction=true;let p=v.play();if(p&&p.finally)p.finally(()=>setTimeout(()=>autoPauseAction=false,0));else setTimeout(()=>autoPauseAction=false,0)}}}
function updateCaption(){if(mode!=='online')return;let t=E('onlineVideo').currentTime||0,c='';for(let i=onlineCues.length-1;i>=0;i--){let q=onlineCues[i];if(q.start<=t&&t<=q.end+.25){c=q.text;break}if(q.start<t-8)break}E('caption').textContent=c}async function refresh(){
  try{
    let s=await api('/api/state');
    onlineState=s;
    if(Number(s.companion_seq||0)>lastCompanionSeq){
      lastCompanionSeq=Number(s.companion_seq||0);
      if(mode!=='online')setMode('online');
      E('onlineUrl').value=s.companion_url||s.online_url||'';
      pendingStartAt=Number(s.companion_start_time||s.online_start_at||0);
      lastAppliedStart=-1;
    }
    E('video').textContent=s.video||'尚未选择';
    E('srt').textContent=s.srt||'尚未选择';
    let p=s.progress||0;
    if(mode==='online'){
      let ph=s.online_phase||'IDLE';
      p=ph==='ERROR'?0:ph==='INIT'?5:ph==='CHECK_SESSION'?10:ph==='FETCH_STREAM'?18:ph==='BUFFERING'?45:ph==='READY'?75:ph==='COMPLETE'?100:(s.online_running?35:0);
    }
    E('bar').style.width=p+'%';
    E('pct').textContent=mode==='online'?(s.online_phase_label||p+'%'):p+'%';
    E('msg').textContent=mode==='online'?(s.online_message||'粘贴在线视频链接'):(s.message||'');
    if(E('onlineErrorBox')){
      let bad=mode==='online'&&s.online_phase==='ERROR';
      E('onlineErrorBox').classList.toggle('hidden',!bad);
      if(bad){
        E('onlineErrorBox').textContent=(s.online_error_hint||'在线视频解析失败')+
          (s.online_error_kind?'\\n错误类型：'+s.online_error_kind:'')+
          (s.online_session_browser?'\\n最后尝试会话：'+s.online_session_browser:'');
      }
    }
    E('run').disabled=mode==='online'?!!s.online_running:!!s.running||!!s.mux_running;
    E('stopOnline').disabled=!s.online_running;
    E('export').disabled=mode==='online'?(s.online_count||0)<1:!s.result||!!s.running||!!s.mux_running;
    E('exportMp4').disabled=!s.result||!!s.running||!!s.mux_running;
    E('result').innerHTML=fmtResult(s.result);
    if(mode==='online'){
      E('onlineTitle').textContent=s.online_title||'等待解析视频';
      E('onlineCount').textContent=(s.online_count||0)+' 条字幕';
      if(s.online_player_url&&s.online_player_url!==lastPlayerUrl){
        lastPlayerUrl=s.online_player_url;
        pendingStartAt=Number(s.online_start_at||0);
        lastAppliedStart=-1;
        E('onlineVideo').src=s.online_player_url;
        E('onlineVideo').load();
      }
      manageLookahead();
    }
  }catch(e){}
}
E('onlineVideo').addEventListener('loadedmetadata',()=>{if(pendingStartAt>0&&lastAppliedStart!==pendingStartAt){try{E('onlineVideo').currentTime=pendingStartAt;lastAppliedStart=pendingStartAt}catch(e){}}});E('onlineVideo').addEventListener('play',()=>{if(mode==='online'&&!autoPauseAction){wantedPlay=true;manageLookahead()}});E('onlineVideo').addEventListener('pause',()=>{if(mode==='online'&&!autoPauseAction){wantedPlay=false}});E('onlineVideo').addEventListener('seeking',()=>{if(mode==='online'&&wantedPlay)manageLookahead()});setInterval(refresh,700);setInterval(pullCues,600);setInterval(updateCaption,120);setInterval(manageLookahead,250);setMode('generate');refreshSessions();
</script></body></html>
'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send_json(self,obj,status=200):
        data=json.dumps(obj,ensure_ascii=False).encode('utf-8')
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        u=urlparse(self.path)
        if u.path=='/':
            data=HTML.encode('utf-8'); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
        if u.path=='/api/companion/ping':
            self.send_json({'ok':True,'app':'AutoSubtitleSync','version':'8.2','port':self.server.server_port}); return
        if u.path=='/api/companion/sync':
            q=parse_qs(u.query)
            try: seq=int(q.get('seq',['0'])[0] or 0)
            except Exception: seq=0
            try: rev=int(q.get('revision',['-1'])[0] or -1)
            except Exception: rev=-1
            self.send_json(companion_sync_snapshot(seq,rev)); return
        if u.path=='/bridge.user.js':
            try:
                data=Path(__file__).with_name('BrowserAudioBridge.user.js').read_bytes()
                self.send_response(200); self.send_header('Content-Type','text/javascript; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
            except Exception as e: self.send_json({'error':str(e)},500)
            return
        if u.path=='/companion.user.js':
            try:
                data=Path(__file__).with_name('BrowserCompanion.user.js').read_bytes()
                self.send_response(200); self.send_header('Content-Type','text/javascript; charset=utf-8'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
            except Exception as e: self.send_json({'error':str(e)},500)
            return
        if u.path=='/api/state': self.send_json(get_state()); return
        if u.path=='/api/browser-sessions': self.send_json({'sessions':detect_browser_sessions()}); return
        if u.path=='/api/online/media': proxy_online_media(self); return
        if u.path=='/api/online/cues':
            self.send_json(online_cues_snapshot()); return
        if u.path=='/api/pick':
            kind=parse_qs(u.query).get('kind',[''])[0]
            if kind not in {'video','srt'}: self.send_json({'error':'参数错误'},400); return
            try:
                path=choose_file(kind)
                if path: set_state(**{kind:path},error='',exported='',mp4_exported='',mux_progress=0,mux_message='',result=None,progress=0)
                self.send_json({'path':path})
            except Exception as e: self.send_json({'error':str(e)},400)
            return
        self.send_error(404)
    def do_POST(self):
        global RESULT_CACHE
        u=urlparse(self.path)
        if u.path=='/api/browser-audio/push':
            n=int(self.headers.get('Content-Length','0') or 0)
            data=self.rfile.read(n) if n else b''
            if data:
                push_pcm(data)
                STATE['browser_audio_received']=True
                STATE['browser_audio_bytes']=int(STATE.get('browser_audio_bytes',0))+len(data)
                STATE['browser_audio_at']=time.time()
            try:
                st=__import__('browser_audio_bridge').snapshot()
            except Exception:
                st={'queue':0,'chunks':AUDIO_STATS.get('chunks',0),'bytes':AUDIO_STATS.get('bytes',0),'dropped':0}
            self.send_json({'ok':True,'bytes':len(data),'queue_chunks':st.get('chunks',0),
                            'queue_depth':st.get('queue',0),'dropped':st.get('dropped',0)})
            return
        if u.path=='/api/browser-audio/open':
            if audio_stream is None:
                self.send_json({'error':'audio_stream 模块缺失（请确认 audio_stream.py 与 server.py 同目录）'},500); return
            n=int(self.headers.get('Content-Length','0') or 0); body=self.rfile.read(n) if n else b'{}'
            try: opt=json.loads(body or b'{}')
            except Exception: opt={}
            try:
                self.send_json(audio_stream.start_audio_session(opt))
            except Exception as e:
                self.send_json({'error':str(e)},400)
            return
        if u.path=='/api/system-audio/devices':
            if audio_stream is None:
                self.send_json({'error':'audio_stream 模块缺失'},500); return
            try:
                self.send_json(audio_stream.list_capture_devices())
            except Exception as e:
                self.send_json({'error':str(e)},500)
            return
        if u.path=='/api/system-audio/open':
            if audio_stream is None:
                self.send_json({'error':'audio_stream 模块缺失'},500); return
            n=int(self.headers.get('Content-Length','0') or 0); body=self.rfile.read(n) if n else b'{}'
            try: opt=json.loads(body or b'{}')
            except Exception: opt={}
            try:
                self.send_json(audio_stream.start_system_audio(opt))
            except Exception as e:
                self.send_json({'error':str(e)},400)
            return
        if u.path=='/api/system-audio/stop':
            if audio_stream is not None:
                try: audio_stream.stop_audio_session()
                except Exception: pass
            self.send_json({'ok':True}); return
        if u.path=='/api/browser-audio/stop':
            if audio_stream is not None:
                try: audio_stream.stop_audio_session()
                except Exception: pass
            self.send_json({'ok':True}); return
        if u.path=='/api/companion/open':
            if get_state().get('online_running'):
                self.send_json({'error':'已有在线字幕任务正在运行。请先停止当前任务。'},409); return
            n=int(self.headers.get('Content-Length','0') or 0); body=self.rfile.read(n) if n else b'{}'
            try: opt=json.loads(body or b'{}')
            except Exception: opt={}
            url=str(opt.get('url','')).strip(); title=str(opt.get('title','')).strip()[:200]; start_at=max(0.0,float(opt.get('current_time',0) or 0))
            media_hint=str(opt.get('media_url','') or '').strip(); referer=str(opt.get('referer','') or url).strip(); user_agent=str(opt.get('user_agent','') or '').strip(); top_url=str(opt.get('top_url','') or '').strip(); hint_duration=max(0.0,float(opt.get('duration',0) or 0))
            model=opt.get('model','small'); lang=opt.get('lang','auto'); mixed=bool(opt.get('mixed',True)); target=opt.get('target','zh'); bilingual=bool(opt.get('bilingual',False)); lookahead=float(opt.get('lookahead',90) or 90); session_mode=str(opt.get('session_mode','auto') or 'auto')
            # v8-patch: audio-bridge mode — no URL resolution; captions come from the
            # PCM stream the browser pushes to /api/browser-audio/push.
            if bool(opt.get('audio_mode',False)) and audio_stream is not None:
                try:
                    info=audio_stream.start_audio_session({'model':model,'lang':lang,'mixed':mixed,
                        'target':target,'bilingual':bilingual,'t0':start_at,'title':title,'url':url,
                        'window':opt.get('window',18),'step':opt.get('step',6),'tail':opt.get('tail',3.0)})
                    ui=f'http://127.0.0.1:{self.server.server_port}/?from=companion&seq={info.get("seq")}'
                    self.send_json({'ok':True,'ui_url':ui,'seq':info.get('seq'),'start_at':start_at,'audio_mode':True}); return
                except Exception as e:
                    self.send_json({'error':str(e)},400); return
            if not url.startswith(('http://','https://')):
                if top_url.startswith(('http://','https://')):
                    url=top_url
                elif _is_http_media_hint(media_hint):
                    url=media_hint
                else:
                    self.send_json({'error':'播放器 frame 没有可解析的 HTTP/HTTPS 地址或媒体入口。请先让视频实际加载几秒。'},400); return
            if model not in {'tiny','base','small','medium','large-v3'}: model='small'
            if lang not in {'auto','zh','en','fr','es','de'}: lang='auto'
            if target not in {'original','zh','en','fr','es','de'}: target='zh'
            lookahead=max(30.0,min(180.0,lookahead))
            seq=int(get_state().get('companion_seq') or 0)+1
            set_state(companion_seq=seq,companion_url=url,companion_title=title,companion_start_time=start_at,companion_received=True)
            try:
                start_online(url,model,lang,mixed,target,bilingual,lookahead,start_at,session_mode,media_hint,referer,user_agent,top_url,hint_duration,title)
                ui=f'http://127.0.0.1:{self.server.server_port}/?from=companion&seq={seq}'
                self.send_json({'ok':True,'ui_url':ui,'seq':seq,'start_at':start_at})
            except Exception as e: self.send_json({'error':str(e)},400)
            return
        if u.path=='/api/online/start':
            if get_state().get('online_running'): self.send_json({'error':'在线字幕正在运行'},409); return
            n=int(self.headers.get('Content-Length','0') or 0); body=self.rfile.read(n) if n else b'{}'
            try: opt=json.loads(body or b'{}')
            except Exception: opt={}
            url=str(opt.get('url','')).strip(); model=opt.get('model','small'); lang=opt.get('lang','auto'); mixed=bool(opt.get('mixed',True)); target=opt.get('target','zh'); bilingual=bool(opt.get('bilingual',False)); lookahead=float(opt.get('lookahead',90) or 90); start_at=max(0.0,float(opt.get('start_at',0) or 0)); session_mode=str(opt.get('session_mode','auto') or 'auto')
            if model not in {'tiny','base','small','medium','large-v3'}: model='small'
            if lang not in {'auto','zh','en','fr','es','de'}: lang='auto'
            if target not in {'original','zh','en','fr','es','de'}: target='zh'
            lookahead=max(30.0,min(180.0,lookahead))
            try: start_online(url,model,lang,mixed,target,bilingual,lookahead,start_at,session_mode); self.send_json({'ok':True})
            except Exception as e: self.send_json({'error':str(e)},400)
            return
        if u.path=='/api/online/stop': stop_online(); self.send_json({'ok':True}); return
        if u.path=='/api/online/export':
            try:
                out=export_online_srt(); subprocess.Popen(['/usr/bin/open','-R',str(out)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); self.send_json({'path':str(out)})
            except Exception as e: self.send_json({'error':str(e)},400)
            return
        if u.path=='/api/start':
            st=get_state()
            if st['running']: self.send_json({'error':'正在处理中'},409); return
            if not st['video'] or not os.path.isfile(st['video']): self.send_json({'error':'请先选择视频文件'},400); return
            n=int(self.headers.get('Content-Length','0') or 0); body=self.rfile.read(n) if n else b'{}'
            try: opt=json.loads(body or b'{}')
            except Exception: opt={}
            mode=opt.get('mode','generate'); model=opt.get('model','small'); lang=opt.get('lang','auto'); mixed=bool(opt.get('mixed',True))
            target=opt.get('target','zh'); bilingual=bool(opt.get('bilingual',False))
            if mode not in {'generate','align'}: mode='generate'
            if model not in {'tiny','base','small','medium','large-v3'}: model='small'
            if lang not in {'auto','zh','en','fr','es','de'}: lang='auto'
            if target not in {'original','zh','en','fr','es','de'}: target='zh'
            if mode=='align' and (not st['srt'] or not os.path.isfile(st['srt'])):
                self.send_json({'error':'校准模式请先选择 SRT 文件'},400); return
            RESULT_CACHE=None
            set_state(mode=mode,running=True,progress=1,message='开始处理…',result=None,error='',exported='',mp4_exported='',mux_progress=0,mux_message='')
            if mode=='generate':
                threading.Thread(target=generate_worker,args=(model,lang,mixed,target,bilingual),daemon=True).start()
            else:
                threading.Thread(target=align_worker,args=(model,lang,mixed),daemon=True).start()
            self.send_json({'ok':True}); return
        if u.path=='/api/export':
            st=get_state(); r=st.get('result')
            if not r or CUES_CACHE is None: self.send_json({'error':'还没有可导出的字幕结果'},400); return
            try:
                if r.get('mode')=='align':
                    out=unique_output_from_srt(st['srt']); write_synced(CUES_CACHE,out,r['a'],r['b'])
                else:
                    out=unique_generated_output(st['video'],r.get('target','original'),r.get('bilingual',False)); write_srt(CUES_CACHE,out)
                set_state(exported=str(out),message=f'已导出：{out.name}')
                subprocess.Popen(['/usr/bin/open','-R',str(out)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                self.send_json({'path':str(out)})
            except Exception as e: self.send_json({'error':str(e)},500)
            return
        if u.path=='/api/export-mp4':
            st=get_state()
            if st.get('mux_running'): self.send_json({'error':'正在导出 MP4'},409); return
            if not st.get('result') or CUES_CACHE is None: self.send_json({'error':'请先完成字幕处理'},400); return
            set_state(mux_running=True,mux_progress=1,mux_message='准备生成带字幕 MP4…',error='',mp4_exported='')
            threading.Thread(target=mux_worker,daemon=True).start(); self.send_json({'ok':True}); return
        self.send_error(404)


def choose_port():
    import socket
    for p in range(8765,8786):
        s=socket.socket()
        try: s.bind(('127.0.0.1',p)); s.close(); return p
        except OSError: s.close()
    return 0

if __name__=='__main__':
    port=choose_port()
    if not port:
        print('无法找到可用的本地端口。'); sys.exit(1)
    url=f'http://127.0.0.1:{port}/'
    print('\nAuto Subtitle Sync v8.2 · 三种抓音 + Iframe Bridge 已启动')
    print('本地地址：',url)
    print('这个窗口可以最小化，但处理期间不要关闭。\n')
    threading.Timer(0.8,lambda:webbrowser.open(url)).start()
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
