#!/bin/zsh
set -u
SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1
if [[ ! -x ".venv/bin/python" ]]; then
  echo "尚未安装运行环境。请先打开 AutoSubtitleSync.command。"
  read -k 1 '?按任意键关闭...'
  exit 1
fi
CERT_PATH=""
[[ -f "$SCRIPT_DIR/.ssl_cert_path" ]] && CERT_PATH="$(cat "$SCRIPT_DIR/.ssl_cert_path" 2>/dev/null || true)"
if [[ -n "$CERT_PATH" && -f "$CERT_PATH" ]]; then export SSL_CERT_FILE="$CERT_PATH" REQUESTS_CA_BUNDLE="$CERT_PATH"; fi
"$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/diagnose_online.py"
printf '\n'
read -k 1 '?按任意键关闭...'
