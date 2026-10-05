#!/bin/sh
# Keep the Mac that runs the model awake, and load coder after login or a restart.
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

uid=$(id -u)
agents="$HOME/Library/LaunchAgents"
mkdir -p "$agents" "$HOME/.config/llmmon"

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

if ! pgrep -x caffeinate >/dev/null 2>&1; then
  write_plist "$agents/com.llmmon.keepawake.plist" com.llmmon.keepawake "  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-ims</string>
  </array>
  <key>KeepAlive</key>
  <true/>
  <key>LimitLoadToSessionType</key>
  <array>
    <string>Aqua</string>
    <string>Background</string>
  </array>"
  bootstrap_agent com.llmmon.keepawake "$agents/com.llmmon.keepawake.plist"
  echo "sleep is held by caffeinate"
else
  echo "caffeinate is already holding sleep"
fi

cat > "$HOME/.config/llmmon/warmup.sh" << 'EOF'
#!/bin/sh
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"
i=0
while [ "$i" -lt 30 ]; do
  if curl -sf -m 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 1
done
if curl -sf -m 2 http://127.0.0.1:11434/api/ps 2>/dev/null | grep -q '"name":"coder'; then
  exit 0
fi
curl -sf -m 180 http://127.0.0.1:11434/api/generate \
  -H "Content-Type: application/json" \
  -d '{"model":"coder","prompt":"ok","stream":false,"keep_alive":-1,"options":{"num_predict":1}}' \
  >/dev/null 2>&1 || true
EOF
chmod 755 "$HOME/.config/llmmon/warmup.sh"

write_plist "$agents/com.llmmon.warmup.plist" com.llmmon.warmup "  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>$HOME/.config/llmmon/warmup.sh</string>
  </array>
  <key>StartInterval</key>
  <integer>300</integer>"
bootstrap_agent com.llmmon.warmup "$agents/com.llmmon.warmup.plist"
echo "coder reloads itself if Ollama comes back empty"

ollama_plist="$agents/homebrew.mxcl.ollama.plist"
if [ -f "$ollama_plist" ]; then
  host=$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:OLLAMA_HOST" "$ollama_plist" 2>/dev/null || true)
  if [ "$host" != "127.0.0.1:11434" ]; then
    /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OLLAMA_HOST 127.0.0.1:11434" "$ollama_plist"
    launchctl bootout "gui/$uid/homebrew.mxcl.ollama" >/dev/null 2>&1 || true
    sleep 1
    launchctl bootstrap "gui/$uid" "$ollama_plist" >/dev/null 2>&1 || \
      launchctl bootstrap "gui/$uid" "$ollama_plist"
    echo "Ollama now listens on 127.0.0.1:11434"
  else
    echo "Ollama already listens on 127.0.0.1:11434"
  fi
fi

if sudo -n pmset -a sleep 0 disksleep 0 displaysleep 0 powernap 0 autorestart 0 tcpkeepalive 1 womp 1 >/dev/null 2>&1; then
  echo "power settings updated"
else
  echo "power settings need an administrator password, run this on the Mac:"
  echo "  sudo pmset -a sleep 0 disksleep 0 displaysleep 0 powernap 0 autorestart 0 tcpkeepalive 1 womp 1"
fi

sh "$HOME/.config/llmmon/warmup.sh" || true
