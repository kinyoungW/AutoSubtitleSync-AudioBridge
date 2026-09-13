#!/bin/zsh
set -u
SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1

printf '\n=== Auto Subtitle Sync v8.2 Audio Bridge ===\n'
printf 'macOS 本地字幕生成 / 翻译 / 校准 / MP4 封装\n\n'

if [[ ! -x ".venv/bin/python" ]]; then
  AUTOSUB_LAUNCHED_BY_MAIN=1 /bin/zsh "$SCRIPT_DIR/install_mac.command" || exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  printf '\n安装环境没有创建成功。\n'
  read -k 1 '?按任意键关闭...'
  exit 1
fi

CERT_PATH=""
if [[ -f "$SCRIPT_DIR/.ssl_cert_path" ]]; then
  CERT_PATH="$(cat "$SCRIPT_DIR/.ssl_cert_path" 2>/dev/null || true)"
fi
if [[ -z "$CERT_PATH" || ! -f "$CERT_PATH" ]]; then
  CERT_PATH="$("$SCRIPT_DIR/.venv/bin/python" -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
fi
if [[ -n "$CERT_PATH" && -f "$CERT_PATH" ]]; then
  export SSL_CERT_FILE="$CERT_PATH"
  export REQUESTS_CA_BUNDLE="$CERT_PATH"
  export PIP_CERT="$CERT_PATH"
fi

exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/server.py"
