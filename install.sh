#!/bin/sh
# Install llmmon on this Mac, and on the Mac that runs the model.
# Usage: ./install.sh [user@host]
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
mkdir -p "${HOME}/bin" "${HOME}/.config/llmmon"
cp "$ROOT/llmmon" "${HOME}/bin/llmmon"
chmod 755 "${HOME}/bin/llmmon"

HOST="${1:-}"
if [ -z "$HOST" ] && [ -f "${HOME}/.config/llmmon/host" ]; then
  HOST=$(awk 'NF && $1 !~ /^#/ { print; exit }' "${HOME}/.config/llmmon/host")
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
else
  echo "installed on this Mac only"
  echo "to watch another Mac:  ./install.sh user@host"
fi

case ":$PATH:" in
  *":${HOME}/bin:"*) ;;
  *)
    echo "add this to ~/.zshrc, then open a new Terminal window:"
    echo "  export PATH=\"\$HOME/bin:\$PATH\""
    ;;
esac

echo "start:  llmmon"
