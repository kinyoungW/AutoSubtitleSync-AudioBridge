"""Browser Audio Bridge — server-side consumer (v8 patch).

Drains PCM16 / 16 kHz / mono chunks that the browser bridge pushes to
/api/browser-audio/push and drives the existing Semantic Look-ahead pipeline
(ONLINE_RAW -> ONLINE_CUES), so the in-page Browser Companion overlay shows live
captions with no change to its transport.

Decoding strategy — disjoint windows (no overlap):
  * Whisper returns roughly one segment per short window, so a sliding window with
    overlap cannot be merged without either dropping or duplicating text. Every
    window here is decoded exactly once and appended, so no audio can be lost.
  * Cross-window context is carried with `initial_prompt` instead of audio overlap,
    which keeps sentence continuity at window edges.
  * Captions lag the speech by about (window + stability buffer) seconds; that is
    the intended live trade-off, and the overlay renders the newest stable caption.

Timeline: cue times are absolute player times. The browser reports `t0` (the
player's currentTime when capture started); audio offset = bytes / 32000.
"""
import queue, re, subprocess, sys, threading, time
import numpy as np

from browser_audio_bridge import pop_pcm, reset_stats, snapshot as audio_stats

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2
FIRST_WINDOW_SEC = 3.0      # short first window so captions start appearing quickly
LOOK_AHEAD_SEC = 0.6        # decode this far past the cut so Whisper finishes the last word
BACK_SEC = 0.25             # start the next window this far back, so trimmed words are re-heard
COMMIT_GUARD = 0.20         # only commit words that finish this long before the cut
MIN_TAIL_SEC = 1.2          # smallest partial window worth decoding
SILENCE_RMS = 0.0025        # below this the window is treated as silence (no decode)
PROMPT_CHARS = 180          # tail of committed text fed back as ASR context

_worker = None


def _srv():
    import server  # late import: server imports this module during startup
    return server


def _drain():
    while True:
        if not pop_pcm(timeout=0.02):
            return


# ---------------------------------------------------------------- PCM 音源
class BridgeSource:
    """PCM 来自浏览器音频桥（页面推送到 /api/browser-audio/push 的队列）。"""
    label = "浏览器音频"
    kind = "browser"

    def read(self, timeout=0.4):
        return pop_pcm(timeout=timeout)

    def close(self):
        pass


class PipeSource:
    """PCM 来自 ffmpeg 管道（系统声音 / 虚拟声卡 / 任意音频输入）。"""
    kind = "pipe"

    def __init__(self, cmd, label="系统声音", read_size=32768):
        self.cmd = list(cmd)
        self.label = label
        self.read_size = read_size
        self.proc = None
        self._q = queue.Queue(maxsize=80)
        self._eof = False
        self._err = []
        self.dropped = 0

    def start(self):
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()
        return self

    def _pump(self):
        try:
            while True:
                block = self.proc.stdout.read(self.read_size)
                if not block:
                    break
                try:
                    self._q.put(block, timeout=2.0)
                except Exception:
                    try:
                        self._q.get_nowait()          # 队列满：丢最旧块，保住"当前"
                    except Exception:
                        pass
                    self.dropped += 1
                    try:
                        self._q.put_nowait(block)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            self._eof = True

    def _drain_err(self):
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._err.append(line)
                    del self._err[:-20]
        except Exception:
            pass

    def read(self, timeout=0.4):
        try:
            return self._q.get(timeout=timeout)
        except Exception:
            return b"" if self._eof else None      # b"" = 音源结束；None = 暂时没有数据

    def error_text(self):
        return " / ".join(self._err[-4:])

    def close(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass


VIRTUAL_DEVICE_RE = re.compile(r"blackhole|loopback|soundflower|virtual|cable|aggregate|multi", re.I)


def list_capture_devices():
    """列出可用于「录电脑正在播放的声音」的音频输入设备。"""
    srv = _srv()
    if sys.platform != "darwin":
        return {"devices": [], "recommended": None, "platform": sys.platform,
                "hint": "自动列举音频设备目前只支持 macOS（avfoundation）。"}
    try:
        p = subprocess.run([srv.get_ffmpeg_exe(), "-hide_banner", "-f", "avfoundation",
                            "-list_devices", "true", "-i", ""],
                           capture_output=True, text=True, timeout=25)
        text = (p.stderr or "") + (p.stdout or "")
    except Exception as e:
        return {"devices": [], "recommended": None, "platform": sys.platform, "hint": f"调用 ffmpeg 失败：{e}"}
    devices, in_audio = [], False
    for line in text.splitlines():
        low = line.lower()
        if "video devices" in low:
            in_audio = False
            continue
        if "audio devices" in low:
            in_audio = True
            continue
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if m and in_audio:
            devices.append({"index": int(m.group(1)), "name": m.group(2).strip()})
    recommended = None
    for d in devices:
        if VIRTUAL_DEVICE_RE.search(d["name"]):
            recommended = d["index"]
            break
    if not devices:
        hint = "没有枚举到音频输入设备。请确认 ffmpeg 可用，并在「系统设置 → 隐私与安全性 → 麦克风」里允许终端 / 本程序。"
    elif recommended is None:
        hint = ("没有发现虚拟声卡。直接采麦克风只能录到环境声，听不到电脑播放的声音。"
                "想录「电脑正在播放的声音」，先装免费的 BlackHole（brew install blackhole-2ch），"
                "再到「音频 MIDI 设置」新建一个多输出设备（BlackHole + 你的扬声器）——"
                "这样你自己还能照常听到声音。装好后再点一次。")
    else:
        hint = ""
    return {"devices": devices, "recommended": recommended, "platform": sys.platform, "hint": hint}


def _capture_cmd(device, extra_input=None):
    ff = _srv().get_ffmpeg_exe()
    if extra_input:
        inp = list(extra_input)
    elif sys.platform == "darwin":
        inp = ["-f", "avfoundation", "-i", f":{int(device)}"]
    elif sys.platform.startswith("linux"):
        inp = ["-f", "pulse", "-i", str(device)]
    else:
        inp = ["-f", "dshow", "-i", f"audio={device}"]
    return [ff, "-hide_banner", "-loglevel", "error"] + inp + \
           ["-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]


def _norm_opts(opt):
    model = str(opt.get("model") or "small")
    if model not in {"tiny", "base", "small", "medium", "large-v3"}:
        model = "small"
    lang = str(opt.get("lang") or "auto")
    if lang not in {"auto", "zh", "en", "fr", "es", "de"}:
        lang = "auto"
    target = str(opt.get("target") or "zh")
    if target not in {"original", "zh", "en", "fr", "es", "de"}:
        target = "zh"
    return {"model": model, "lang": lang, "target": target,
            "bilingual": bool(opt.get("bilingual", False)),
            "mixed": bool(opt.get("mixed", True)),
            "t0": max(0.0, float(opt.get("t0", opt.get("current_time", 0)) or 0.0)),
            "title": str(opt.get("title") or "实时字幕")[:200],
            "url": str(opt.get("url") or ""),
            "window": max(3.0, min(20.0, float(opt.get("window", 6) or 6))),
            "tail": max(0.0, min(8.0, float(opt.get("tail", 1.5) or 1.5)))}


def _begin_session(o, source, session_mode, message):
    srv = _srv()
    global _worker
    srv.ONLINE_STOP.set()
    time.sleep(0.05)
    srv.ONLINE_STOP.clear()
    with srv.ONLINE_LOCK:
        srv.ONLINE_CUES.clear()
        srv.ONLINE_RAW.clear()
        srv.ONLINE_REVISION = 0
    seq = int(srv.get_state().get("companion_seq") or 0) + 1
    srv.set_state(
        companion_seq=seq, companion_received=True, companion_url=o["url"], companion_title=o["title"],
        companion_start_time=o["t0"],
        online_running=True, online_audio_mode=True, online_url=o["url"], online_title=o["title"],
        online_message=message, online_error="", online_done=False,
        online_count=0, online_revision=0, online_is_live=True, online_lookahead=o["tail"],
        online_processed_time=o["t0"], online_stable_through=o["t0"], online_start_at=o["t0"],
        online_phase="BUFFERING", online_phase_label="建立实时字幕",
        online_session_mode=session_mode, online_session_browser="",
        online_semantic_tail=o["tail"], online_duration=0.0, online_player_url="",
        online_error_kind="", online_error_hint="", online_resolve_attempts=[], error="",
        browser_audio_dropped=0, browser_audio_stats={}, online_source=source.label)
    _worker = threading.Thread(
        target=_audio_worker,
        args=(o["t0"], o["model"], o["lang"], o["mixed"], o["target"], o["bilingual"], o["window"], o["tail"], source),
        daemon=True)
    _worker.start()
    return {"ok": True, "seq": seq, "audio_mode": True, "start_at": o["t0"], "window": o["window"],
            "step": o["window"], "tail": o["tail"], "source": source.label}


def start_audio_session(opt):
    """浏览器音频桥：PCM 由网页推送到 /api/browser-audio/push。"""
    stop_audio_session(quiet=True)
    o = _norm_opts(opt)
    reset_stats()
    _drain()
    return _begin_session(o, BridgeSource(), "browser_audio", "音频通道已连接，正在加载 Whisper…")


def start_system_audio(opt):
    """系统声音：服务端用 ffmpeg 直接从音频输入设备录音（配虚拟声卡可录电脑播放的声音）。"""
    stop_audio_session(quiet=True)
    o = _norm_opts(opt)
    device = opt.get("device")
    extra = opt.get("input_args")
    if not extra:
        info = list_capture_devices()
        if device is None:
            device = info.get("recommended")
        if device is None:
            if not info.get("devices"):
                raise RuntimeError(info.get("hint") or "没有可用的音频输入设备。")
            device = info["devices"][0]["index"]
    cmd = _capture_cmd(device, extra)
    src = PipeSource(cmd, label="系统声音").start()
    time.sleep(0.6)
    if src.proc.poll() is not None:
        err = src.error_text()
        rc = src.proc.returncode
        src.close()
        raise RuntimeError(f"无法采集系统声音（ffmpeg 退出码 {rc}）：{err or '设备不可用或没有权限'}")
    return _begin_session(o, src, "system_audio", f"系统声音采集中（设备 {device}），正在加载 Whisper…")


def stop_audio_session(quiet=False):
    global _worker
    srv = _srv()
    srv.ONLINE_STOP.set()
    w = _worker
    if w is not None and w.is_alive() and not quiet:
        w.join(timeout=8.0)
    _worker = None
    return {"ok": True}


def _best_cut(buf, target_bytes, search_sec=1.5, frame_sec=0.02):
    """Pick a cut point near target_bytes that lands on the quietest 20 ms frame.

    Cutting a window in the middle of a word is what produces artefacts like
    "testing the brow | The audio bridge", so windows are aligned to pauses.
    """
    fr = int(frame_sec * BYTES_PER_SEC)
    lo = max(fr, target_bytes - int(search_sec * BYTES_PER_SEC))
    hi = len(buf) - fr
    if hi <= lo:
        return min(target_bytes, len(buf))
    arr = np.frombuffer(bytes(buf[lo:hi + fr]), dtype=np.int16).astype(np.float32) / 32768.0
    n = (len(arr) - fr) // fr
    if n <= 0:
        return min(target_bytes, len(buf))
    rms = [float(np.sqrt(np.mean(np.square(arr[i * fr:(i + 1) * fr])))) for i in range(n)]
    return lo + int(np.argmin(rms)) * fr + fr // 2


def _rms(audio):
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio))))


SENT_END = re.compile(r'[.!?。！？…]["\'”’）)】]*$')


def _split_sentences(seg, commit_until=None):
    """Split one Whisper segment into sentence-sized fragments using word times.

    Only words that finish before `commit_until` (absolute time) are committed; a
    word cut in half by the window boundary is left for the next window to re-hear.
    """
    words = list(getattr(seg, "words", None) or [])
    if commit_until is not None:
        words = [w for w in words if float(getattr(w, "end", 0) or 0) <= commit_until]
    if not words:
        return []
    out = []
    cur = []
    for w in words:
        txt = (getattr(w, "word", "") or "").strip()
        if not txt:
            continue
        cur.append(w)
        if SENT_END.search(txt) and len(" ".join((x.word or "").strip() for x in cur)) >= 2:
            out.append(cur); cur = []
    if cur:
        out.append(cur)
    frags = []
    for i, grp in enumerate(out):
        text = "".join((x.word or "") for x in grp).strip()
        if not text:
            continue
        st = float(getattr(grp[0], "start", seg.start) or seg.start)
        en = float(getattr(grp[-1], "end", seg.end) or seg.end)
        nxt = out[i + 1] if i + 1 < len(out) else None
        if nxt:  # leave a small gap so sentence groups split cleanly
            nxt_st = float(getattr(nxt[0], "start", en) or en)
            if nxt_st - en < 0.08:
                en = max(st, nxt_st - 0.08)
        frags.append((st, en, text))
    return frags


def _append_raw(segs, window_start, srv, commit_until=None):
    """Append one window's fragments to ONLINE_RAW (never removes earlier text)."""
    fresh = []
    for s in segs:
        text = (s.text or "").strip()
        if not text:
            continue
        pieces = _split_sentences(s, None if commit_until is None else commit_until - window_start) \
            if getattr(s, "words", None) else []
        if pieces:
            for st, en, tx in pieces:
                if en > st:
                    fresh.append(srv.ASRSeg(window_start + st, window_start + en, tx, None))
        elif commit_until is None or window_start + float(s.end) <= commit_until:
            fresh.append(srv.ASRSeg(window_start + float(s.start), window_start + float(s.end), text, None))
    if not fresh:
        return 0
    added = 0
    with srv.ONLINE_LOCK:
        merged = list(srv.ONLINE_RAW)
        for r in fresh:
            if merged and r.start < merged[-1].end - 0.35:
                same = (r.end <= merged[-1].end + 0.20 and
                        srv.fuzz.ratio(srv.normalize_text(r.text), srv.normalize_text(merged[-1].text)) >= 78)
                if same:
                    continue                       # repeated hallucination of the previous line
                r.start = max(r.start, merged[-1].end + 0.01)
            if r.end > r.start:
                merged.append(r)
                added += 1
        srv.ONLINE_RAW = merged
    return added


def _audio_worker(t0, model_size, lang, mixed, target, bilingual, window_sec, tail, source=None):
    source = source or BridgeSource()
    srv = _srv()
    fallback = "en"
    buf = bytearray()
    buf_start = float(t0)
    prompt = ""
    try:
        srv.set_state(online_message=f"正在加载 Whisper {model_size}…")
        model = srv.load_whisper(model_size)
        first = True
        last_data = time.time()
        srv.set_state(
            online_message=f"实时字幕运行中 · 输出 {target} · 预计延迟约 {window_sec + tail:.0f} 秒",
            online_phase="READY", online_phase_label="实时识别中")

        while not srv.ONLINE_STOP.is_set():
            chunk = source.read(timeout=0.4)
            if chunk == b"":
                break                       # 音源结束（ffmpeg 退出 / 设备断开）
            if chunk:
                buf.extend(chunk)
                last_data = time.time()

            need = int((FIRST_WINDOW_SEC if first else window_sec) * BYTES_PER_SEC)
            look = int(LOOK_AHEAD_SEC * BYTES_PER_SEC)
            have = len(buf)
            idle = (time.time() - last_data) > 1.5
            enough = have >= need + look
            if not enough and not (idle and have >= int(MIN_TAIL_SEC * BYTES_PER_SEC)):
                if idle and have < int(0.4 * BYTES_PER_SEC) and (time.time() - last_data) > 15:
                    srv.set_state(online_message="等待浏览器音频…（页面暂停或切换标签时会出现）")
                continue

            if first or have <= need:
                cut = min(have, need)
            else:
                cut = _best_cut(buf, need, search_sec=min(1.5, max(0.0, (have - need) / BYTES_PER_SEC + 1.0)))
            return_sec = 0.0 if idle and have <= need + look else BACK_SEC
            take = min(have, cut + look)
            commit_until = buf_start + cut / BYTES_PER_SEC - COMMIT_GUARD if cut + look < have else None
            audio = np.frombuffer(bytes(buf[:take]), dtype=np.int16).astype(np.float32) / 32768.0
            duration = len(audio) / SAMPLE_RATE
            processed = buf_start + duration

            if _rms(audio) >= SILENCE_RMS:
                kwargs = dict(language=None if lang == "auto" else lang, vad_filter=False,
                              beam_size=1, word_timestamps=True, condition_on_previous_text=False)
                if mixed and lang == "auto":
                    kwargs["multilingual"] = True
                if prompt:
                    kwargs["initial_prompt"] = prompt
                try:
                    seg_iter, info = model.transcribe(audio, **kwargs)
                    segs = list(seg_iter)
                    fallback = getattr(info, "language", fallback) or fallback
                    n = _append_raw(segs, buf_start, srv, commit_until)
                except Exception:
                    n = 0
                if n:
                    with srv.ONLINE_LOCK:
                        committed = [r.text for r in srv.ONLINE_RAW[-3:]]
                    prompt = " ".join(committed)[-PROMPT_CHARS:]
                srv.set_state(
                    online_message=f"实时识别中 · 已处理 {processed/60:.1f} 分钟 · {len(srv.ONLINE_CUES)} 条字幕")
            else:
                srv.set_state(
                    online_message=f"实时字幕运行中 · 静音中 · 已处理 {processed/60:.1f} 分钟")

            srv._rebuild_semantic_cues(fallback, target, bilingual, processed, tail)
            if getattr(source, "kind", "") == "browser":
                stats = audio_stats()
            else:
                stats = {"queue": 0, "dropped": getattr(source, "dropped", 0), "kind": getattr(source, "kind", "")}
            srv.set_state(online_processed_time=processed, online_is_live=True,
                          browser_audio_dropped=stats.get("dropped", 0), browser_audio_stats=stats)

            keep_back = int(return_sec * BYTES_PER_SEC)
            drop = max(0, cut - keep_back)
            if drop <= 0:
                drop = cut
            del buf[:drop]
            buf_start += drop / BYTES_PER_SEC
            first = False

        # ---- stop: flush the partial tail, then expose everything that is left
        processed = float(srv.get_state().get("online_processed_time") or buf_start)
        if len(buf) >= int(MIN_TAIL_SEC * BYTES_PER_SEC):
            audio = np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) / 32768.0
            if _rms(audio) >= SILENCE_RMS:
                kwargs = dict(language=None if lang == "auto" else lang, vad_filter=False,
                              beam_size=1, word_timestamps=False, condition_on_previous_text=False)
                if mixed and lang == "auto":
                    kwargs["multilingual"] = True
                if prompt:
                    kwargs["initial_prompt"] = prompt
                try:
                    seg_iter, info = model.transcribe(audio, **kwargs)
                    _append_raw(list(seg_iter), buf_start, srv)
                    fallback = getattr(info, "language", fallback) or fallback
                except Exception:
                    pass
            processed = buf_start + len(audio) / SAMPLE_RATE
        srv._rebuild_semantic_cues(fallback, target, bilingual, processed, 0.0)

        with srv.ONLINE_LOCK:
            final = [srv.Cue(i + 1, c.start, c.end, c.text, c.language, c.original_text)
                     for i, c in enumerate(srv.ONLINE_CUES)]
        base = "browser-audio-" + time.strftime("%Y%m%d-%H%M%S")
        result = {"mode": "online", "cues": len(final), "duration": processed,
                  "model": model_size, "target": target, "bilingual": bool(bilingual),
                  "title": srv.get_state().get("online_title") or "浏览器音频",
                  "a": target, "b": fallback, "audio_mode": True}
        srv.CUES_CACHE = final
        srv.RESULT_CACHE = result
        srv.set_state(online_running=False, online_done=True, online_stable_through=processed,
                      online_processed_time=processed, online_phase="IDLE", online_phase_label="已停止",
                      online_message=f"实时字幕已停止 · 共 {len(final)} 条",
                      video=base + ".mp4", srt=base + ".srt", result=result)
    except Exception as e:
        srv.set_state(online_running=False, online_done=False, online_error=str(e),
                      online_phase="ERROR", online_phase_label="音频字幕失败",
                      online_message="音频字幕失败", error=str(e))
    finally:
        try:
            source.close()
        except Exception:
            pass
