# llmmon

在你面前这台 Mac 的 Terminal 里，实时看另一台 Mac 上的本地模型。

画面画在你运行 `llmmon` 的机器上。远端只采样，每 0.2 秒发一行 JSON。键盘和刷新都不走整屏 SSH，所以按键是本地的。

不需要额外 Python 包。macOS 自带的 Python 3 就够。

## 装好就能用

在你平时敲命令的那台 Mac 上：

```sh
git clone https://github.com/Linus-Shyu/llmmon.git
cd llmmon
./install.sh user@192.168.1.20
llmmon
```

把 `user@192.168.1.20` 换成跑模型的那台 Mac。第一次 SSH 要能免密登录（公钥）。安装脚本会：

1. 把 `llmmon` 放到本机 `~/bin/llmmon`
2. 把同一份脚本放到远端的 `/opt/homebrew/bin/llmmon`（没有 Homebrew 就放到远端 `~/bin/llmmon`）
3. 把地址写进 `~/.config/llmmon/host`，之后直接打 `llmmon` 即可

如果 `~/bin` 不在 PATH 里，脚本会提示你加一行到 `~/.zshrc`：

```sh
export PATH="$HOME/bin:$PATH"
```

## 只看本机

模型就在这台 Mac 上时：

```sh
./install.sh
llmmon --local
```

## 按键

| 键 | 作用 |
| --- | --- |
| `q` | 退出 |
| `b` | 让远端模型生成一小段代码，测 decode / prefill tok/s |
| 空格 | 暂停画面，再按一次继续 |

## 画面

配色是固定的：标题和分区是琥珀色，数字和曲线是青色，用量条按占用从绿到琥珀再到红。

- **MODEL**：当前加载的模型、参数量、量化、上下文、Metal 显存、是否常驻
- **CPU**：总占用、性能核 / 能效核、1/5/15 分钟负载、开机时长、曲线、每颗核
- **MEM**：已用内存、分层条（应用 / 接线 / 压缩 / 空闲）、active、inactive、swap
- **DISK / NET**：数据卷占用，以及网卡上下行
- **RUN**：Ollama 进程的驻留内存、CPU、线程数

有 `~/.config/fastfetch/linus_art.txt` 时，字标会画在最上面。没有这个文件就只显示标题。

## 用这台模型写代码

监控走 SSH，不依赖 Ollama 是否对局域网开放。建议 Ollama 只听本机 `127.0.0.1:11434`。

在你这台 Mac 上开一条隧道（本机如果已经有 Ollama，不要占 11434）：

```sh
ssh -f -N -L 11435:127.0.0.1:11434 user@192.168.1.20
```

OpenAI 兼容接口：

```text
http://127.0.0.1:11435/v1
```

模型名用 Ollama 里的名字，例如 `coder`。

想直接在终端里聊天：

```sh
ssh -t user@192.168.1.20 'export PATH=/opt/homebrew/bin:$PATH; ollama run coder'
```

## 不接显示器

可以拔掉远端的显示器，人留在登录状态。Apple Silicon 的 Metal 在这种情况下仍然工作。

不要注销，也不要在没接显示器时重启。开了 FileVault 的话，重启后要在本机解锁磁盘，SSH 才会回来。

防止拔掉显示器后系统睡眠，在远端用户目录放一个 LaunchAgent，路径例如 `~/Library/LaunchAgents/com.user.keepawake.plist`：

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

然后：

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.keepawake.plist
```

让模型常驻内存，可以在 Ollama 的 LaunchAgent 里加上：

```text
OLLAMA_KEEP_ALIVE=-1
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
OLLAMA_HOST=127.0.0.1:11434
```

改完后重新 bootstrap 那个服务。`llmmon` 本身不改 Ollama 配置。

## 配置

优先级：命令行 `user@host`，然后是环境变量 `LLMMON_HOST`，然后是 `~/.config/llmmon/host`。文件里一行地址，`#` 开头是注释。

```sh
llmmon --help
llmmon --once          # 打一行状态
llmmon user@mac --once
```

## 常见问题

**打开后停在 connecting。** 先 `ssh user@host` 确认免密能进。远端要有可执行的 `llmmon`，再跑一次 `./install.sh user@host`。

**SSH 要卡好几秒。** 脚本已经关掉了 GSSAPI。如果自己手写 `ssh`，加上 `-o GSSAPIAuthentication=no -o PreferredAuthentications=publickey`。

**本机也装了 Ollama，隧道连错机器。** 转发到 `11435`，不要覆盖本机的 `11434`。

**采样很慢或要输密码。** 监控不使用 `powermetrics`，也不需要 sudo。
