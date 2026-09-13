"""Browser Audio Bridge — PCM intake.

The browser bridge POSTs 0.5 s PCM16 / 16 kHz / mono chunks to
/api/browser-audio/push; audio_stream.py drains them from here and drives the
online caption pipeline.

If the consumer ever falls behind (very slow machine, big model), the OLDEST
chunk is discarded instead of refusing the newest one: live captions must stay
close to the current playback position, and STATS['dropped'] surfaces the loss.
"""
from queue import Queue
import threading
import time

AUDIO_QUEUE = Queue(maxsize=200)
STATS = {'chunks': 0, 'bytes': 0, 'dropped': 0, 'started': time.time()}
_LOCK = threading.Lock()


def push_pcm(data: bytes):
    if not data:
        return
    with _LOCK:
        try:
            AUDIO_QUEUE.put_nowait(data)
        except Exception:
            try:
                AUDIO_QUEUE.get_nowait()
                AUDIO_QUEUE.task_done()
            except Exception:
                pass
            try:
                AUDIO_QUEUE.put_nowait(data)
            except Exception:
                pass
            STATS['dropped'] += 1
        STATS['chunks'] += 1
        STATS['bytes'] += len(data)


def pop_pcm(timeout=0.5):
    try:
        return AUDIO_QUEUE.get(timeout=timeout)
    except Exception:
        return None


def queue_depth():
    return AUDIO_QUEUE.qsize()


def reset_stats():
    with _LOCK:
        try:
            while True:
                AUDIO_QUEUE.get_nowait()
        except Exception:
            pass
        STATS.update({'chunks': 0, 'bytes': 0, 'dropped': 0})


def snapshot():
    return {'queue': AUDIO_QUEUE.qsize(), 'chunks': STATS['chunks'],
            'bytes': STATS['bytes'], 'dropped': STATS['dropped']}
