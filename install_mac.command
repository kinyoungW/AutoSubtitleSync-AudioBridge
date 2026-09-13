#!/bin/zsh
set -u
SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1

printf '\n=== Auto Subtitle Sync 智能安装器 v7.6 Iframe Bridge ===\n'
printf 'Browser Companion：原网页字幕 Overlay + Semantic Look-ahead。\n\n'

PY_VERSION="3.12.10"
PY_URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-macos11.pkg"
PY_PKG="${TMPDIR:-/tmp}/AutoSubtitleSync-Python-${PY_VERSION}.pkg"
PY_OFFICIAL="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
SSL_HINT_FILE="$SCRIPT_DIR/.ssl_cert_path"

pause_fail() {
  printf '\n❌ %s\n' "$1"
  read -k 1 '?按任意键关闭...'
  printf '\n'
  return 1
}

python_ok() {
  local py="$1"
  [[ -x "$py" ]] || return 1
  "$py" -c 'import sys,venv; raise SystemExit(0 if (3,9) <= sys.version_info[:2] <= (3,13) else 1)' >/dev/null 2>&1
}

find_python() {
  local candidates p found
  candidates=(
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.10/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.9/bin/python3"
    "/opt/homebrew/bin/python3.12" "/opt/homebrew/bin/python3.13" "/opt/homebrew/bin/python3.11" "/opt/homebrew/bin/python3.10" "/opt/homebrew/bin/python3.9"
    "/usr/local/bin/python3.12" "/usr/local/bin/python3.13" "/usr/local/bin/python3.11" "/usr/local/bin/python3.10" "/usr/local/bin/python3.9"
  )
  for p in "${candidates[@]}"; do
    if python_ok "$p"; then printf '%s' "$p"; return 0; fi
  done
  for p in python3.12 python3.13 python3.11 python3.10 python3.9 python3; do
    found="$(command -v "$p" 2>/dev/null || true)"
    if [[ -n "$found" ]] && python_ok "$found"; then printf '%s' "$found"; return 0; fi
  done
  return 1
}

install_official_python() {
  printf '没有找到可用 Python，准备安装 Python.org 官方 Python %s。\n' "$PY_VERSION"
  printf '正在自动下载 Universal2 安装包…\n'
  rm -f "$PY_PKG"
  if ! /usr/bin/curl --fail --location --retry 3 --retry-all-errors --progress-bar "$PY_URL" -o "$PY_PKG"; then
    pause_fail 'Python 自动下载失败，请检查网络后重新运行。'; return 1
  fi
  printf '\n正在验证安装包签名…\n'
  if ! /usr/sbin/pkgutil --check-signature "$PY_PKG" >/dev/null 2>&1; then
    rm -f "$PY_PKG"; pause_fail 'Python 安装包签名验证失败，已停止。'; return 1
  fi
  printf '签名验证通过。macOS 将请求一次管理员授权。\n\n'
  /usr/bin/osascript - "$PY_PKG" <<'APPLESCRIPT'
on run argv
    set pkgPath to item 1 of argv
    do shell script "/usr/sbin/installer -pkg " & quoted form of pkgPath & " -target /" with administrator privileges
end run
APPLESCRIPT
  local rc=$?
  if [[ $rc -ne 0 ]]; then pause_fail 'Python 安装被取消或失败。'; return 1; fi
  if ! python_ok "$PY_OFFICIAL"; then pause_fail 'Python 安装完成，但没有找到可运行的 Python 3.12。'; return 1; fi
  printf '\n✅ Python %s 已安装。\n' "$PY_VERSION"
  return 0
}

https_ok() {
  local py="$1" cert="${2:-}"
  if [[ -n "$cert" ]]; then
    SSL_CERT_FILE="$cert" REQUESTS_CA_BUNDLE="$cert" "$py" - <<'PY' >/dev/null 2>&1
import urllib.request
with urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=12) as r:
    assert 200 <= r.status < 400
PY
  else
    "$py" - <<'PY' >/dev/null 2>&1
import urllib.request
with urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=12) as r:
    assert 200 <= r.status < 400
PY
  fi
}

write_ssl_hint() {
  local cert="$1"
  printf '%s\n' "$cert" > "$SSL_HINT_FILE"
  chmod 600 "$SSL_HINT_FILE" 2>/dev/null || true
}

bootstrap_certifi_with_curl() {
  local py="$1"
  local tmpdir json meta wheel_url wheel_sha wheel_file got
  tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/autosub-certifi.XXXXXX")" || return 1
  json="$tmpdir/certifi.json"
  printf '正在通过 macOS 系统 curl 安全引导 CA 证书包…\n' >&2
  if ! /usr/bin/curl --fail --location --retry 3 --retry-all-errors --silent --show-error \
      "https://pypi.org/pypi/certifi/json" -o "$json"; then
    rm -rf "$tmpdir"; return 1
  fi
  meta="$("$py" - "$json" <<'PY'
import json,sys
j=json.load(open(sys.argv[1],encoding='utf-8'))
for f in j.get('urls',[]):
    if f.get('filename','').endswith('.whl') and f.get('packagetype')=='bdist_wheel':
        print(f['url']); print(f['digests']['sha256']); print(f['filename']); break
PY
)"
  wheel_url="$(printf '%s\n' "$meta" | sed -n '1p')"
  wheel_sha="$(printf '%s\n' "$meta" | sed -n '2p')"
  wheel_file="$tmpdir/$(printf '%s\n' "$meta" | sed -n '3p')"
  [[ -n "$wheel_url" && -n "$wheel_sha" ]] || { rm -rf "$tmpdir"; return 1; }
  if ! /usr/bin/curl --fail --location --retry 3 --retry-all-errors --silent --show-error "$wheel_url" -o "$wheel_file"; then
    rm -rf "$tmpdir"; return 1
  fi
  got="$(/usr/bin/shasum -a 256 "$wheel_file" | awk '{print $1}')"
  if [[ "$got" != "$wheel_sha" ]]; then
    printf 'certifi wheel SHA256 校验失败。\n'
    rm -rf "$tmpdir"; return 1
  fi
  if ! "$py" -m pip install --disable-pip-version-check --no-index "$wheel_file" >/dev/null; then
    rm -rf "$tmpdir"; return 1
  fi
  local cert
  cert="$("$py" -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
  rm -rf "$tmpdir"
  [[ -n "$cert" && -f "$cert" ]] || return 1
  printf '%s' "$cert"
}

build_system_ca_bundle() {
  local out="$SCRIPT_DIR/.macos_system_ca.pem"
  : > "$out" || return 1
  local keychain
  for keychain in "/System/Library/Keychains/SystemRootCertificates.keychain" "/Library/Keychains/System.keychain"; do
    if [[ -f "$keychain" ]]; then
      /usr/bin/security find-certificate -a -p "$keychain" >> "$out" 2>/dev/null || true
    fi
  done
  [[ -s "$out" ]] || { rm -f "$out"; return 1; }
  printf '%s' "$out"
}

repair_python_ssl() {
  local py="$1" ver cert_script cert base_cert system_bundle combined
  rm -f "$SSL_HINT_FILE"
  printf '\n检查 Python HTTPS/证书链…\n'
  if https_ok "$py"; then
    printf '✅ Python HTTPS 验证正常。\n'
    return 0
  fi

  printf '检测到 Python SSL 证书链未完成，开始自动修复。\n'
  ver="$("$py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  cert_script="/Applications/Python ${ver}/Install Certificates.command"
  if [[ -f "$cert_script" ]]; then
    printf '正在运行 Python.org 官方 Install Certificates.command…\n'
    /bin/sh "$cert_script" >/dev/null 2>&1 || true
    if https_ok "$py"; then
      printf '✅ 官方证书安装已修复 Python HTTPS。\n'
      return 0
    fi
  fi

  for base_cert in "/etc/ssl/cert.pem" "/private/etc/ssl/cert.pem"; do
    if [[ -f "$base_cert" ]] && https_ok "$py" "$base_cert"; then
      write_ssl_hint "$base_cert"
      printf '✅ 已改用 macOS CA bundle：%s\n' "$base_cert"
      return 0
    fi
  done

  cert="$(bootstrap_certifi_with_curl "$py" || true)"
  if [[ -n "$cert" ]] && https_ok "$py" "$cert"; then
    write_ssl_hint "$cert"
    printf '✅ 已通过系统 curl 引导 certifi，并修复 Python HTTPS。\n'
    return 0
  fi

  system_bundle="$(build_system_ca_bundle || true)"
  if [[ -n "$system_bundle" ]]; then
    combined="$SCRIPT_DIR/.combined_ca.pem"
    : > "$combined"
    [[ -n "$cert" && -f "$cert" ]] && cat "$cert" >> "$combined"
    cat "$system_bundle" >> "$combined"
    if https_ok "$py" "$combined"; then
      write_ssl_hint "$combined"
      printf '✅ 已使用 macOS 系统钥匙串证书修复 Python HTTPS。\n'
      return 0
    fi
  fi

  printf '\nSSL 诊断：\n'
  "$py" - <<'PY' || true
import ssl
print("OpenSSL:", ssl.OPENSSL_VERSION)
print("默认验证路径:", ssl.get_default_verify_paths())
PY
  return 1
}

run_with_ca() {
  local cert=""
  if [[ -f "$SSL_HINT_FILE" ]]; then cert="$(cat "$SSL_HINT_FILE" 2>/dev/null || true)"; fi
  if [[ -n "$cert" && -f "$cert" ]]; then
    SSL_CERT_FILE="$cert" REQUESTS_CA_BUNDLE="$cert" PIP_CERT="$cert" "$@"
  else
    "$@"
  fi
}

make_env() {
  local py="$1"
  printf '\n使用 Python：%s\n' "$py"
  "$py" -c 'import platform,sys; print("Python 版本：",sys.version.split()[0]); print("Mac 架构：",platform.machine()); print("macOS：",platform.mac_ver()[0])' || return 1

  repair_python_ssl "$py" || { printf '❌ 无法建立安全 HTTPS 证书链。\n'; return 1; }

  rm -rf .venv
  "$py" -m venv .venv || return 1

  printf '\n安装运行依赖（仅允许预编译 wheel，禁止本地编译）…\n'
  # venv 自带 pip 已足够；不再把升级 pip/setuptools 作为硬前置步骤。
  run_with_ca .venv/bin/python -m pip --version || return 1
  run_with_ca .venv/bin/python -m pip install --disable-pip-version-check --prefer-binary --only-binary=:all: -r requirements.txt || return 1

  # 如果没有特殊 CA 提示，安装后优先使用 certifi，保证 urllib/httpx/requests 行为一致。
  if [[ ! -f "$SSL_HINT_FILE" ]]; then
    local vcert
    vcert="$(.venv/bin/python -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
    if [[ -n "$vcert" && -f "$vcert" ]]; then write_ssl_hint "$vcert"; fi
  fi

  run_with_ca .venv/bin/python - <<'PY'
from rapidfuzz import fuzz
import ctranslate2, certifi
from faster_whisper import WhisperModel
from imageio_ffmpeg import get_ffmpeg_exe
import sentencepiece
import yt_dlp
print("FFmpeg：", get_ffmpeg_exe())
print("yt-dlp：", yt_dlp.version.__version__)
print("证书包：", certifi.where())
print("字幕语言识别：内置轻量分类器（无 langid / 无源码构建）")
print("离线翻译：CTranslate2 + SentencePiece")
print("CTranslate2：", ctranslate2.__version__)
print("依赖自检：通过")
PY
}

PYTHON_BIN="$(find_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  install_official_python || exit 1
  PYTHON_BIN="$PY_OFFICIAL"
fi

if ! make_env "$PYTHON_BIN"; then
  printf '\n当前 Python 环境初始化失败。\n'
  if [[ "$PYTHON_BIN" != "$PY_OFFICIAL" ]]; then
    printf '将改用 Python.org 官方 Python %s 再试一次。\n' "$PY_VERSION"
    if ! python_ok "$PY_OFFICIAL"; then install_official_python || exit 1; fi
    PYTHON_BIN="$PY_OFFICIAL"
    make_env "$PYTHON_BIN" || { pause_fail '官方 Python 环境仍初始化失败。请把最后 40 行发给我。'; exit 1; }
  else
    pause_fail '环境初始化失败。请把最后 40 行发给我。'; exit 1
  fi
fi

printf '\n✅ 安装完成。\n\n'

# 如果用户是直接双击 install_mac.command，则安装完成后直接启动主程序。
# 如果是由 AutoSubtitleSync.command 调用，则正常返回给启动器，由启动器继续启动。
if [[ "${AUTOSUB_LAUNCHED_BY_MAIN:-0}" != "1" ]]; then
  printf '检测到你是直接运行安装器；现在自动启动 Auto Subtitle Sync…\n\n'
  CERT_PATH=""
  if [[ -f "$SSL_HINT_FILE" ]]; then CERT_PATH="$(cat "$SSL_HINT_FILE" 2>/dev/null || true)"; fi
  if [[ -n "$CERT_PATH" && -f "$CERT_PATH" ]]; then
    export SSL_CERT_FILE="$CERT_PATH"
    export REQUESTS_CA_BUNDLE="$CERT_PATH"
    export PIP_CERT="$CERT_PATH"
  fi
  exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/server.py"
fi

printf '安装器已完成，返回启动器继续启动。\n'
exit 0
