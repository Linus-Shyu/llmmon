#!/bin/sh
# Open an outbound Cloudflare tunnel on the Mac that runs the model.
# The other Mac then uses the printed https addresses. No inbound SSH.
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
uid=$(id -u)
agents="$HOME/Library/LaunchAgents"
logs="$HOME/Library/Logs/llmmon"
config="$HOME/.cloudflared/config.yml"
mkdir -p "$agents" "$logs" "$HOME/.config/llmmon" "$HOME/.config/minicode" "$HOME/bin"

if [ -f "$ROOT/llmmon" ]; then
  cp "$ROOT/llmmon" "$HOME/bin/llmmon"
  chmod 755 "$HOME/bin/llmmon"
fi
if [ ! -x "$HOME/bin/llmmon" ]; then
  echo "找不到 llmmon。在仓库目录里运行 ./tunnel.sh。" >&2
  exit 1
fi

if [ ! -s "$HOME/.config/llmmon/token" ]; then
  openssl rand -hex 24 > "$HOME/.config/llmmon/token"
  chmod 600 "$HOME/.config/llmmon/token"
fi
token=$(awk 'NF && $1 !~ /^#/ { print; exit }' "$HOME/.config/llmmon/token")

write_plist() {
  path=$1
  label=$2
  body=$3
  cat > "$path" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
$body
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
EOF
}

bootstrap_agent() {
  label=$1
  plist=$2
  launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$uid" "$plist"
}

write_plist "$agents/com.llmmon.http.plist" com.llmmon.http "  <key>ProgramArguments</key>
  <array>
    <string>$HOME/bin/llmmon</string>
    <string>--serve-http</string>
  </array>
  <key>StandardOutPath</key>
  <string>$logs/http.log</string>
  <key>StandardErrorPath</key>
  <string>$logs/http.log</string>"
bootstrap_agent com.llmmon.http "$agents/com.llmmon.http.plist"
echo "监控接口已开在 127.0.0.1:11437"

if ! command -v cloudflared >/dev/null 2>&1; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "需要 Homebrew 才能安装 cloudflared。" >&2
    exit 1
  fi
  brew install cloudflared
fi
cf=$(command -v cloudflared)

named=0
ollama_named=""
monitor_named=""
if [ -f "$config" ] && grep -q '^tunnel:' "$config"; then
  if curl -sf -m 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    named=1
    ollama_named=$(awk '/hostname:/ {h=$NF} /service:/ && /11434/ && h {print h; h=""}' "$config" | head -1)
    monitor_named=$(awk '/hostname:/ {h=$NF} /service:/ && /11437/ && h {print h; h=""}' "$config" | head -1)
    write_plist "$agents/com.llmmon.cloudflared.plist" com.llmmon.cloudflared "  <key>ProgramArguments</key>
  <array>
    <string>$cf</string>
    <string>tunnel</string>
    <string>--no-autoupdate</string>
    <string>--config</string>
    <string>$config</string>
    <string>run</string>
  </array>
  <key>StandardOutPath</key>
  <string>$logs/cloudflared.log</string>
  <key>StandardErrorPath</key>
  <string>$logs/cloudflared.log</string>"
    bootstrap_agent com.llmmon.cloudflared "$agents/com.llmmon.cloudflared.plist"
    echo "已用现有的命名隧道： $config"
  else
    echo "这台 Mac 没有在跑 Ollama，跳过已有的命名隧道，避免抢走另一台机器的域名。"
  fi
fi

start_quick() {
  label=$1
  port=$2
  log=$logs/$label.log
  : > "$log"
  write_plist "$agents/$label.plist" "$label" "  <key>ProgramArguments</key>
  <array>
    <string>$cf</string>
    <string>tunnel</string>
    <string>--no-autoupdate</string>
    <string>--protocol</string>
    <string>http2</string>
    <string>--url</string>
    <string>http://127.0.0.1:$port</string>
  </array>
  <key>StandardOutPath</key>
  <string>$log</string>
  <key>StandardErrorPath</key>
  <string>$log</string>"
  bootstrap_agent "$label" "$agents/$label.plist"
  i=0
  while [ "$i" -lt 40 ]; do
    url=$(grep -oE 'https://[A-Za-z0-9-]+\.trycloudflare\.com' "$log" 2>/dev/null | tail -1 || true)
    if [ -n "$url" ]; then
      printf '%s\n' "$url"
      return 0
    fi
    i=$((i + 1))
    sleep 0.5
  done
  echo "临时隧道没有给出地址。看 $log" >&2
  return 1
}

monitor_url=""
ollama_url=""
if [ -n "$monitor_named" ]; then
  monitor_url="https://$monitor_named"
else
  echo "正在为监控开临时隧道……"
  monitor_url=$(start_quick com.llmmon.tunnel-monitor 11437)
fi
if [ -n "$ollama_named" ]; then
  ollama_url="https://$ollama_named"
elif curl -sf -m 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  echo "正在为模型开临时隧道……"
  ollama_url=$(start_quick com.llmmon.tunnel-ollama 11434)
else
  echo "本机 11434 没有 Ollama，模型隧道先不开。"
fi

if [ -n "$monitor_url" ]; then
  printf '%s\n' "$monitor_url" > "$HOME/.config/llmmon/url"
fi
if [ -n "$ollama_url" ]; then
  printf '%s\n' "$ollama_url" > "$HOME/.config/minicode/upstream"
fi

echo
echo "隧道已开。在另一台 Mac 上粘贴这一行："
if [ -n "$monitor_url" ] && [ -n "$ollama_url" ]; then
  echo "curl -fsSL https://raw.githubusercontent.com/Linus-Shyu/llmmon/main/install.sh | sh -s -- --url $monitor_url --token $token --upstream $ollama_url"
elif [ -n "$monitor_url" ]; then
  echo "curl -fsSL https://raw.githubusercontent.com/Linus-Shyu/llmmon/main/install.sh | sh -s -- --url $monitor_url --token $token"
fi
if [ "$named" -eq 0 ]; then
  echo "临时地址在 cloudflared 重启后会变。变了之后再跑一次 ./tunnel.sh，把新的一行装到另一台 Mac。"
fi
echo "这两个地址在公网上。监控有 token。模型地址没有登录。"
