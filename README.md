# 我的极简 Ubuntu 配置指南

一份极简、带个人偏好的 Ubuntu 初始化指南，灵感来自 [Eugene Yan 的 Mac 配置指南](https://eugeneyan.com/writing/mac-setup/)。

Mac 上有 Homebrew Cask，`brew install --cask xxx` 一行搞定。Ubuntu 没有等价物：`apt` 只认软件源里的包，不认识 GitHub 上单独发布的 `.deb`，也不接受 URL。

所以这里放**两份东西**，凑成一个 "Ubuntu 版 Homebrew"：

- [`apps.json`](./apps.json) —— 清单：声明要装什么、怎么装；
- [`ubuntuinit.py`](./ubuntuinit.py) —— 安装器：读清单执行（Python 标准库；装了 rich 会显示下载进度条）。

## 用法

```bash
./ubuntuinit.py list              # 列出清单里的 App
./ubuntuinit.py git uv code       # 安装指定的几个
./ubuntuinit.py all               # 按清单顺序全部安装
./ubuntuinit.py flclash -n        # 只看会执行什么，不真正安装
./ubuntuinit.py all -f            # 强制重装（忽略"已装跳过"）
```

> 克隆后若脚本没有执行权限：`chmod +x ubuntuinit.py`。

## 慢速源与代理

**按源判断**，而不是一刀切：

- `apt` / `snap` 走系统镜像（通常很快）→ **永不代理**；
- 真正要联网下载的源（GitHub、脚本安装器、npm）→ **先探测该源的速度**，太慢或直连不通才自动改走代理。

代理默认是 `http://127.0.0.1:7890`（FlClash 默认端口），用前会先探端口是否在监听。

```bash
./ubuntuinit.py all --proxy                          # 联网下载强制走代理（apt/snap 除外）
./ubuntuinit.py all --proxy http://127.0.0.1:1080    # 指定别的代理
./ubuntuinit.py all --no-proxy                       # 完全不用代理
UBUNTUINIT_SLOW_MBPS=2 ./ubuntuinit.py all           # 阈值改成 2 MB/s（默认 1.0）
```

实测（本机）：GitHub 直连约 0.4 MB/s → 自动走代理；npm registry 直连近 0 → 走代理；apt 镜像不受影响。

## 清单 `apps.json`

格式就一行一条：`"短名": "来源:参数"`。

```json
{
  "git":            "apt:git",
  "curl":           "apt:curl",
  "uv":             "script:https://astral.sh/uv/install.sh",
  "node":           "apt:nodejs npm",
  "pnpm":           "npm:pnpm",
  "code":           "snap:code --classic",
  "github-desktop": "github:shiftkey/desktop",
  "flclash":        "github:chen08209/FlClash",
  "fish":           "apt:fish",
  "starship":       "apt:starship",
  "fzf":            "apt:fzf",
  "ripgrep":        "apt:ripgrep",
  "fd-find":        "apt:fd-find",
  "eza":            "apt:eza",
  "zoxide":         "apt:zoxide",
  "btop":           "apt:btop",
  "tmux":           "apt:tmux",
  "git-delta":      "apt:git-delta",
  "gh":             "apt:gh",
  "sqlite3":        "apt:sqlite3",
  "keepassxc":      "apt:keepassxc",
  "obsidian":       "snap:obsidian --classic",
  "fcitx5":         "apt:fcitx5 fcitx5-frontend-gtk3 fcitx5-frontend-gtk4 fcitx5-frontend-qt5 fcitx5-frontend-qt6",
  "rime":           "apt:fcitx5-rime",
  "fcitx5-config":  "apt:fcitx5-config-qt",
  "fonts-cjk":      "apt:fonts-noto-cjk"
}
```

**加新软件 = 加一行**，脚本不用动。`all` 按文件里的顺序执行，所以有先后依赖的（如 `node` 先于 `pnpm`）把顺序排好即可。

## 支持的来源

| 来源 | 干什么 | 例子 |
|---|---|---|
| `apt` | 官方源里的包 | `apt:git` |
| `snap` | snap 包 | `snap:code --classic` |
| `github` | 取 GitHub **最新 release** 里的 `.deb` 安装 | `github:shiftkey/desktop` |
| `script` | 跑官方安装脚本 | `script:https://astral.sh/uv/install.sh` |
| `npm` | `npm install -g` | `npm:pnpm` |
| `deb` | 直接下载 `.deb` 安装；给目录 URL 则自动取最新版 | `deb:https://.../chrome.deb` |

其中 `github` 会：调 API 取最新版 → 按架构（amd64 / arm64 / armhf）挑 `.deb` → 下载到临时文件 → `sudo apt install -y` 安装 → 删临时文件。用的是 `apt install ./x.deb` 而不是 `dpkg -i`，依赖会自动补齐。

## 进度显示

`.deb`、安装脚本这类长下载会用 [rich](https://github.com/Textualize/rich) 显示进度条（体积、速度、剩余时间）：

```
FlClash-0.8.99-linux-amd64.deb ━━━━━━━━━━━━━━━━ 47.3/47.3 MB 11.8 MB/s 0:00:00
```

```bash
sudo apt install python3-rich     # 装 rich（可选）
```

没装也能跑，只是退回纯文本百分比。

## 中文输入法（Fcitx5 + Rime 雾凇拼音）

先装框架、Rime 引擎、图形配置和 CJK 字体：

```bash
./ubuntuinit.py fcitx5 rime fcitx5-config fonts-cjk
```

然后**在你自己的终端**做一次性配置（这些不是装包，脚本不代劳）：

```bash
# 1) 设为默认输入法框架
im-config -n fcitx5

# 2) 让各程序用 fcitx5（Wayland/GNOME 推荐 environment.d）
mkdir -p ~/.config/environment.d
cat > ~/.config/environment.d/im.conf <<'EOF'
GTK_IM_MODULE=fcitx
QT_IM_MODULE=fcitx
XMODIFIERS=@im=fcitx
EOF

# 3) 部署雾凇拼音（rime-ice）；GitHub 慢的话前面加代理
git clone --depth=1 https://github.com/iDvel/rime-ice.git \
  ~/.local/share/fcitx5/rime

# 4) 注销重登（或重启），在 fcitx5-config-qt 里把「Rime」加进输入法列表
```

之后 `Ctrl+空格` 切换中英；Rime 里按 `Ctrl + 反引号` 打开方案选单。

> rime 用户目录常见为 `~/.local/share/fcitx5/rime`，部分版本是 `~/.config/fcitx5/rime`，方案没生效就换另一个。参考：[iDvel/rime-ice](https://github.com/iDvel/rime-ice)

## 注意

- `script` 会**执行网上下载的脚本**（如 uv 的官方安装脚本），属于跑第三方代码，请自行确认来源可信。
- **不要用 `sudo` 跑整个脚本**！用普通用户跑，需要 root 的步骤脚本内部会自己调 `sudo`。否则 `uv` 这类会装到 root 的家目录，你日常账号用不到。
- 需要 `sudo` 时会在开始**统一认证一次密码**（因为安装输出被隐藏，中途无法再输密码）。
- **已安装的会自动跳过**：`apt`/`snap`/`npm` 直接查系统，`github` 比对已装版本号，`script` 用状态文件记录。想强制重装加 `-f`。
- `apt`/`snap` 需要 `sudo`；`npm` 装到用户目录 `~/.local`，不需要 `sudo`。
- `fd-find` 装的命令叫 `fdfind`（Ubuntu 命名），可加 `alias fd=fdfind`。
- `uv` 装完要把 `~/.local/bin` 加进 PATH，**重开终端**才生效。
- GitHub 的 release 信息会缓存到 `~/.cache/ubuntuinit/releases`（默认 6 小时）；遇限流先换代理出口重试、再用缓存，仍不行就**跳过该 App 继续**，不中断整体。设 `GITHUB_TOKEN` 可提高限额。
- `chrome`/`edge` 通过官方 `.deb` 安装，其自带脚本会写入 Google/微软的 apt 源，之后可随 `apt upgrade` 一起更新。`dl.google.com` 在国内常不通，脚本会按源探测自动走代理。
- `wechat` 用腾讯官方 Linux 原生版（arm64 把 URL 换成 `WeChatLinux_arm64.deb`）；仓库/snap 里那个 `wechat` 是过时的网页包裹版，别用。若某个官方域名证书报错，换用 `dldir1.qq.com` 这个域名。
- `deb` 类首次安装后会记录真实包名，之后能正确"已装跳过"，不会重复下载上百 MB。

## 参考

- [Eugene Yan — My Minimal MacBook Pro Setup Guide](https://eugeneyan.com/writing/mac-setup/)
- [deb-get](https://github.com/ublue-os/deb-get)（apt 风格的第三方 deb 管理器，仅限其收录的 App）
