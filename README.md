# llmmon

llmmon is a terminal monitor for a local model running on a Mac. The display is drawn on the machine where you run the command. The machine that runs the model only samples, and sends one line of JSON every 0.2 seconds. Keys and redraws stay local, so the session does not depend on a full-screen SSH connection.

![llmmon](docs/screenshot.png)

The monitor needs no extra Python packages. The Python 3 that ships with macOS is enough.

## Setup

On the Mac where you want the display:

```bash
git clone https://github.com/Linus-Shyu/llmmon.git
cd llmmon
./install.sh user@192.168.1.20
llmmon
```

Replace `user@192.168.1.20` with the Mac that runs the model. The first connection has to succeed with a public key. `ssh user@host` should open a shell without asking for a password.

`./install.sh` does three things:

1. Copies `llmmon` to `~/bin/llmmon` on this Mac.
2. Copies the same script to `/opt/homebrew/bin/llmmon` on the remote Mac. If that directory is not writable, it uses `~/bin/llmmon` on the remote Mac.
3. Writes the address to `~/.config/llmmon/host`, so later you can run `llmmon` with no arguments.

If `~/bin` is not on `PATH`, the script prints the line to add to `~/.zshrc`:

```bash
export PATH="$HOME/bin:$PATH"
```

Open a new terminal after changing `PATH`.

To watch a model on the same Mac:

```bash
./install.sh
llmmon --local
```

> **Note:** If the display stays on `connecting`, confirm that `ssh user@host` works without a password, then run `./install.sh user@host` again. The remote Mac needs an executable `llmmon`. GSSAPI is already disabled. A hand-written `ssh` command should include `-o GSSAPIAuthentication=no -o PreferredAuthentications=publickey`. Sampling does not use `powermetrics` and does not need `sudo`.

## Command-line usage

```bash
llmmon                         # host from ~/.config/llmmon/host
llmmon user@mac                # host for this run
llmmon --local                 # this Mac
llmmon --once                  # print one status line and exit
llmmon user@mac --once
llmmon --help
```

The host is taken from the command line, then from `LLMMON_HOST`, then from `~/.config/llmmon/host`. The file is one address per run. Lines that start with `#` are comments.

| Key | Action |
| --- | --- |
| `q` | Quit |
| `b` | Ask the model for a short completion and report decode / prefill tok/s |
| space | Pause the display. Press again to resume |

The colors are fixed. Titles are amber, numbers and sparklines are cyan, and usage bars go from green to amber to red as they fill.

- **MODEL**: loaded model, parameter count, quantization, context length, Metal memory, and whether the model is kept resident
- **CPU**: total use, performance and efficiency cores, 1/5/15-minute load, uptime, a sparkline, and each core
- **MEM**: memory in use, a stacked bar (app, wired, compressed, free), active, inactive, and swap
- **DISK / NET**: the data volume, and throughput on the network interface
- **RUN**: resident memory, CPU, and thread count of the Ollama process

If `~/.config/fastfetch/linus_art.txt` exists, those lines are drawn above the title. Without that file, the display starts at the title.

## Models

`setup-coder` reads the unified memory of a Mac and installs a Qwen2.5-Coder build that fits, through Ollama, under the name `coder`. It then connects [OpenCode](https://opencode.ai) so the model can edit files in the current directory and run terminal commands. Smaller models sometimes emit a tool call as plain JSON. `minicode-proxy.py` turns that JSON into a normal tool call.

```bash
./setup-coder                  # model and tools on this Mac
./setup-coder user@mac         # model on that Mac, tools on this one
./setup-coder --dry-run        # print the choice, and do not download
```

`setup-coder` needs Homebrew. It installs Ollama if it is missing, and installs OpenCode with `brew install anomalyco/tap/opencode-v2` if `opencode` is missing. An existing `~/.config/opencode/opencode.json` is left as it is.

| Memory | Model | Context |
| --- | --- | --- |
| under 12 GB | Qwen2.5-Coder 3B | 8192 |
| 12–23 GB | Qwen2.5-Coder 7B | 16384 |
| 24–47 GB | Qwen2.5-Coder 14B | 8192 |
| 48 GB and above | Qwen2.5-Coder 32B | 16384 |

On a 12–23 GB machine the 7B model is the one that fits and still leaves room for the system and a 16384-token context. A 14B or 32B model on that machine is pushed into swap.

After installation, change to a project directory and run:

```bash
minicode
```

`minicode` is a terminal session, not an application you open from the Finder. It asks before it edits a file or runs a command. Leave it with `Ctrl+C`.

> **Note:** Apple's Terminal quits while drawing this screen. The crash is `EXC_ARM_PAC_FAIL` in CoreText's font fallback. When Warp is installed, `minicode` opens the session there. Otherwise, run it from iTerm or Warp.

![minicode](docs/minicode.png)

A 7B model on an entry-level Apple Silicon Mac produces about twenty tokens per second. It is suitable for changing one file or running one command. It often gets a change wrong when the task spans a large repository.

The monitor reaches the remote Mac over SSH. It does not require Ollama to listen on the LAN. Leave Ollama on `127.0.0.1:11434`.

If this Mac already runs Ollama, forward the remote server to `11435` so you do not replace the local server on `11434`:

```bash
ssh -f -N -L 11435:127.0.0.1:11434 user@192.168.1.20
```

The OpenAI-compatible endpoint is:

```text
http://127.0.0.1:11435/v1
```

Use the name Ollama has for the model, for example `coder`.

To talk to the model in a remote shell:

```bash
ssh -t user@192.168.1.20 'export PATH=/opt/homebrew/bin:$PATH; ollama run coder'
```

## Running without a display

The remote Mac can have its display unplugged, as long as the user stays logged in. Metal on Apple Silicon still runs in that state.

Do not log out. Do not restart the machine while no display is attached. With FileVault, a restart waits for the disk to be unlocked at the machine before SSH returns.

To keep the Mac awake after the display is unplugged, install a LaunchAgent on the remote user account, for example `~/Library/LaunchAgents/com.user.keepawake.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.user.keepawake</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-ims</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
```

Then:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.keepawake.plist
```

`setup-coder` sets the following on the Homebrew Ollama LaunchAgent when that file exists. `llmmon` itself does not change Ollama's configuration. To keep the model resident without `setup-coder`, set the same variables and bootstrap the service again:

```text
OLLAMA_KEEP_ALIVE=-1
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_NUM_PARALLEL=1
OLLAMA_HOST=127.0.0.1:11434
```

## License

llmmon is released under the MIT License. See [LICENSE](LICENSE) for further details.
