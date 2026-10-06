#!/bin/sh
# One command for the Mac that runs the model, and one command for the Mac that watches it.
#
#   curl -fsSL https://raw.githubusercontent.com/Linus-Shyu/llmmon/main/install.sh | sh -s -- --server
#   curl -fsSL https://raw.githubusercontent.com/Linus-Shyu/llmmon/main/install.sh | sh -s -- --url https://… --token … --upstream https://…
set -eu

fetch_checkout() {
  tmp=$(mktemp -d)
  echo "正在下载 llmmon……"
  curl -fsSL https://github.com/Linus-Shyu/llmmon/archive/refs/heads/main.tar.gz | tar -xz -C "$tmp"
  exec "$tmp/llmmon-main/install.sh" "$@"
}

if [ -f "$0" ]; then
  ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
else
  ROOT=""
fi
if [ -z "$ROOT" ] || [ ! -f "$ROOT/llmmon" ]; then
  fetch_checkout "$@"
fi

URL=""
TOKEN=""
UPSTREAM=""
SERVER=0
HOST=""
while [ $# -gt 0 ]; do
  case $1 in
    --server) SERVER=1; shift ;;
    --url) URL=${2:-}; shift 2 ;;
    --token) TOKEN=${2:-}; shift 2 ;;
    --upstream) UPSTREAM=${2:-}; shift 2 ;;
    -h|--help)
      cat << 'EOF'
跑模型的那台 Mac：

  curl -fsSL https://raw.githubusercontent.com/Linus-Shyu/llmmon/main/install.sh | sh -s -- --server

看画面、写代码的那台 Mac（地址由上面那条命令打印）：

  curl -fsSL https://raw.githubusercontent.com/Linus-Shyu/llmmon/main/install.sh | sh -s -- --url https://… --token … --upstream https://…

需要本机已安装 Homebrew。
EOF
      exit 0
      ;;
    *@*) HOST=$1; shift ;;
    *)
      echo "不认识的参数: $1" >&2
      echo "看用法: install.sh --help" >&2
      exit 2
      ;;
  esac
done

link_bin() {
  name=$1
  if [ -w /opt/homebrew/bin ]; then
    ln -sfn "$HOME/bin/$name" "/opt/homebrew/bin/$name"
  fi
  if [ -w /usr/local/bin ]; then
    ln -sfn "$HOME/bin/$name" "/usr/local/bin/$name"
  fi
}

if [ "$SERVER" -eq 1 ]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "先安装 Homebrew，然后再运行这一行：" >&2
    echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' >&2
    exit 1
  fi
  sh "$ROOT/setup-coder" --here
  exit 0
fi

mkdir -p "${HOME}/bin" "${HOME}/.config/llmmon" "${HOME}/.config/minicode"
cp "$ROOT/llmmon" "${HOME}/bin/llmmon"
chmod 755 "${HOME}/bin/llmmon"
link_bin llmmon

if [ -n "$URL" ]; then
  printf '%s\n' "$URL" > "${HOME}/.config/llmmon/url"
  if [ -n "$TOKEN" ]; then
    printf '%s\n' "$TOKEN" > "${HOME}/.config/llmmon/token"
    chmod 600 "${HOME}/.config/llmmon/token"
  fi
  if [ -n "$UPSTREAM" ]; then
    sh "$ROOT/setup-coder" --upstream "$UPSTREAM"
  fi
  echo "装好了。新开一个终端，运行:  llmmon"
  exit 0
fi

if [ -n "$HOST" ]; then
  printf '%s\n' "$HOST" > "${HOME}/.config/llmmon/host"
  echo "watching  $HOST"
  ssh -T \
    -o BatchMode=yes \
    -o ConnectTimeout=8 \
    -o GSSAPIAuthentication=no \
    -o PreferredAuthentications=publickey \
    -o LogLevel=ERROR \
    "$HOST" 'mkdir -p "$HOME/bin"
if [ -d /opt/homebrew/bin ] && [ -w /opt/homebrew/bin ]; then
  cat > /opt/homebrew/bin/llmmon
  chmod 755 /opt/homebrew/bin/llmmon
  echo "remote  /opt/homebrew/bin/llmmon"
else
  cat > "$HOME/bin/llmmon"
  chmod 755 "$HOME/bin/llmmon"
  echo "remote  $HOME/bin/llmmon"
fi' < "$ROOT/llmmon"
  echo "start:  llmmon"
  exit 0
fi

echo "跑模型的那台 Mac："
echo "  curl -fsSL https://raw.githubusercontent.com/Linus-Shyu/llmmon/main/install.sh | sh -s -- --server"
echo "看画面的那台 Mac 使用上面打印出来的那一行。"
