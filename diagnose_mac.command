#!/bin/zsh
set -u
SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1
printf '\n=== Auto Subtitle Sync v6.2 诊断 ===\n\n'
printf 'macOS: '; /usr/bin/sw_vers -productVersion 2>/dev/null || true
printf '架构: '; /usr/bin/uname -m
printf 'Shell: %s\n' "$SHELL"
for py in "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [[ -x "$py" ]]; then
    printf '\nPython: %s\n' "$py"
    "$py" -c 'import sys,ssl; print(sys.version.split()[0]); print(ssl.OPENSSL_VERSION); print(ssl.get_default_verify_paths())' 2>&1 || true
    "$py" - <<'PY' 2>&1 || true
import urllib.request
try:
    with urllib.request.urlopen('https://pypi.org/simple/pip/', timeout=10) as r:
        print('Python HTTPS: OK', r.status)
except Exception as e:
    print('Python HTTPS: FAIL', repr(e))
PY
  fi
done
printf '\nmacOS curl -> PyPI: '
if /usr/bin/curl -fsSI --connect-timeout 10 https://pypi.org/ >/dev/null 2>&1; then echo OK; else echo FAIL; fi
printf 'macOS curl -> Hugging Face: '
if /usr/bin/curl -fsSI --connect-timeout 10 https://huggingface.co/ >/dev/null 2>&1; then echo OK; else echo FAIL; fi
if [[ -f .ssl_cert_path ]]; then printf '\nSSL hint: '; cat .ssl_cert_path; fi
if [[ -x .venv/bin/python ]]; then
  printf '\nvenv: 存在\n'
  .venv/bin/python - <<'PY' 2>&1 || true
mods=['faster_whisper','ctranslate2','rapidfuzz','imageio_ffmpeg','sentencepiece','certifi']
for m in mods:
    try:
        __import__(m); print(m, 'OK')
    except Exception as e:
        print(m, 'FAIL', repr(e))
PY
else
  printf '\nvenv: 不存在\n'
fi
printf '\n=== 诊断结束 ===\n'
read -k 1 '?按任意键关闭...'
printf '\n'
