import os, sys, threading, time
sys.path.insert(0, '/tmp/autosub-v8/work_v8')
import server
from faster_whisper import WhisperModel
_M = WhisperModel(os.environ.get('TEST_MODEL_DIR') or '/tmp/ct2-base', device='cpu', compute_type='int8')
server.load_whisper = lambda size: _M
httpd = server.ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('AS_TEST_PORT') or 8767)), server.Handler)
print('SERVER_READY', flush=True)
httpd.serve_forever()
