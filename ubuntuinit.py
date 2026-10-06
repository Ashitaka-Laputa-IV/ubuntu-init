#!/usr/bin/env python3
"""ubuntuinit —— 用一份简单清单 + 本脚本，像 brew 一样在 Ubuntu 上装软件。

用法:
    ./ubuntuinit.py list                 # 列出清单里的 App
    ./ubuntuinit.py <名字> [<名字>...]    # 安装指定 App
    ./ubuntuinit.py all                  # 按清单顺序全部安装
    选项:
      -n, --dry-run   只演示将执行的命令，不真正安装
      -f, --force     忽略"已安装则跳过"，强制重装
      --proxy [URL]   所有需要联网的下载都走代理（默认 http://127.0.0.1:7890）
      --no-proxy      完全不用代理

清单 apps.json 的格式是 "短名": "来源:参数"，来源取值:
    apt | snap | github | script | npm
加新软件只改 apps.json，无需改本脚本。

关于慢速源：apt/snap 走的是系统镜像（通常很快），永不代理；
真正需要联网下载的源（GitHub、脚本安装器、npm）会**先探测该源的速度**，
慢了才自动改走代理。阈值可用 UBUNTUINIT_SLOW_MBPS 调整（默认 1.0 MB/s）。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
    console = Console()
except ImportError:  # 没装 rich 也能跑，只是没有进度显示
    console = None

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(HERE, "apps.json")
API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
BAR_MIN_BYTES = 1_000_000
STATE_DIR = os.path.expanduser("~/.local/state/ubuntuinit")

PROXY_DEFAULT = "http://127.0.0.1:7890"
SLOW_MBPS = float(os.environ.get("UBUNTUINIT_SLOW_MBPS", "1.0"))
PROBE_BYTES = 800_000
PROBE_MAX_SECONDS = 5.0

SUDO_SOURCES = {"apt", "snap", "github", "deb"}    # 这些来源需要 root
CACHE_DIR = os.path.expanduser("~/.cache/ubuntuinit/releases")
CACHE_TTL = float(os.environ.get("UBUNTUINIT_CACHE_TTL", "21600"))  # 秒


class SkipApp(Exception):
    """单个 App 暂时无法完成（限流/网络/解析失败），跳过它继续装其它，而不中断整体。"""

_LATEST: dict[str, dict] = {}                   # 缓存 GitHub 最新 release
_DEB_URL: dict[str, str] = {}                   # 缓存解析出的 .deb 直链
_PROXY_DECISION: dict[str, bool] = {}       # host -> 是否走代理
_PROXY_FORCED: str | None = None            # --proxy 的值
_PROXY_DISABLED = False                     # --no-proxy


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def say(text: str) -> None:
    console.print(text) if console else print(text)


def info(text: str) -> None:
    say(f"[cyan]{text}[/]" if console else text)


def warn(text: str) -> None:
    say(f"[bold yellow]![/] {text}" if console else f"! {text}")


def skip(name: str) -> None:
    say(f"[dim]•[/] {name}  已安装，跳过" if console else f"• {name}  已安装，跳过")


def fail(text: str) -> None:
    if console:
        console.print(f"[bold red]✗[/] {text}")
    else:
        print(f"✗ {text}", file=sys.stderr)
    raise SystemExit(1)


def _tail(text: str, lines: int = 12) -> str:
    rows = [r for r in (text or "").splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


# --------------------------------------------------------------------------- #
# 网络：按源探测速度 / 决定是否走代理
# --------------------------------------------------------------------------- #
def _proxy_url() -> str:
    return _PROXY_FORCED or PROXY_DEFAULT


def _reachable(url: str) -> bool:
    p = urllib.parse.urlparse(url if "://" in url else "http://" + url)
    try:
        with socket.create_connection((p.hostname, p.port or 7890), timeout=0.6):
            return True
    except OSError:
        return False


def _open(req: urllib.request.Request, timeout: float = 30, proxy: str | None = None):
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def probe(url: str) -> float | None:
    """直连该源拉一小段，返回 MB/s；直连不通返回 None。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": "ubuntuinit",
                      "Range": f"bytes=0-{PROBE_BYTES - 1}"})
    start = time.monotonic()
    got = 0
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            while chunk := resp.read(65536):
                got += len(chunk)
                if got >= PROBE_BYTES or time.monotonic() - start > PROBE_MAX_SECONDS:
                    break
    except (OSError, urllib.error.URLError):
        return None
    dt = time.monotonic() - start
    return (got / dt / 1e6) if dt > 0 and got > 0 else None


def proxy_for(url: str) -> str | None:
    """针对某个下载源决定用不用代理（按 host 缓存结论）。"""
    host = urllib.parse.urlparse(url).netloc or url
    if _PROXY_DISABLED:
        return None
    proxy = _proxy_url()
    if _PROXY_FORCED:
        return proxy
    if host in _PROXY_DECISION:
        return proxy if _PROXY_DECISION[host] else None
    if not _reachable(proxy):
        _PROXY_DECISION[host] = False
        return None
    mbps = probe(url)
    if mbps is None:
        info(f"{host}: 直连不通 → 改走代理")
        _PROXY_DECISION[host] = True
    elif mbps < SLOW_MBPS:
        info(f"{host}: 直连 {mbps:.2f} MB/s → 改走代理")
        _PROXY_DECISION[host] = True
    else:
        info(f"{host}: 直连 {mbps:.2f} MB/s")
        _PROXY_DECISION[host] = False
    return proxy if _PROXY_DECISION[host] else None


def child_env(proxy: str | None) -> dict:
    env = os.environ.copy()
    if proxy:
        env.update({"http_proxy": proxy, "HTTP_PROXY": proxy,
                    "https_proxy": proxy, "HTTPS_PROXY": proxy,
                    "all_proxy": proxy, "ALL_PROXY": proxy})
    return env


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def detect_arch() -> str:
    try:
        out = subprocess.run(["dpkg", "--print-architecture"],
                             capture_output=True, text=True, check=True)
        if out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    return {"x86_64": "amd64", "aarch64": "arm64",
            "armv7l": "armhf"}.get(platform.machine(), platform.machine())


def _ok(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True).returncode == 0
    except OSError:
        return False


def dpkg_versions() -> list[str]:
    r = subprocess.run(["dpkg-query", "-W", "-f=${Version}\n"],
                       capture_output=True, text=True)
    return r.stdout.split()


def normalize_tag(tag: str) -> str:
    for prefix in ("release-", "v"):
        if tag.startswith(prefix):
            return tag[len(prefix):]
    return tag


def _cache_path(repo: str) -> str:
    return os.path.join(CACHE_DIR, repo.replace("/", "__") + ".json")


def _read_cache(repo: str):
    path = _cache_path(repo)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), time.time() - os.path.getmtime(path)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(repo: str, data: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(repo), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def github_latest(repo: str) -> dict:
    """取最新 release：内存缓存 → 磁盘缓存(TTL 内) → 直连 → 代理。"""
    if repo in _LATEST:
        return _LATEST[repo]

    cached = _read_cache(repo)
    if cached and cached[1] < CACHE_TTL:
        _LATEST[repo] = cached[0]
        return cached[0]

    headers = {"User-Agent": "ubuntuinit", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API_LATEST.format(repo=repo), headers=headers)

    attempts: list[str | None] = [None]
    if not _PROXY_DISABLED and _reachable(_proxy_url()):
        attempts.append(_proxy_url())       # 换出口 IP 可绕过按 IP 的限流

    err: Exception | None = None
    for proxy in attempts:
        try:
            with _open(req, proxy=proxy) as resp:
                data = json.load(resp)
            _write_cache(repo, data)
            _LATEST[repo] = data
            return data
        except urllib.error.HTTPError as e:
            err = e
            if e.code == 404:
                break
        except (OSError, urllib.error.URLError) as e:
            err = e

    if cached:                              # 用过期缓存兜底
        warn(f"{repo}: API 不可用，改用缓存的版本信息")
        _LATEST[repo] = cached[0]
        return cached[0]

    if isinstance(err, urllib.error.HTTPError) and err.code == 403:
        raise SkipApp(
            "GitHub API 限流（未登录每小时 60 次）；可设置 GITHUB_TOKEN，或稍后再试")
    if isinstance(err, urllib.error.HTTPError) and err.code == 404:
        raise SkipApp(f"仓库不存在或没有 release: {repo}")
    raise SkipApp(f"无法获取 {repo} 的 release 信息")


def _fetch_text(url: str, proxy: str | None = None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "ubuntuinit"})
    with _open(req, proxy=proxy) as resp:
        return resp.read().decode("utf-8", "ignore")


def _latest_in_pool(index_url: str, arch: str) -> str:
    """从一个目录索引里挑出版本号最大的 <arch> .deb，返回其完整 URL。"""
    try:
        html = _fetch_text(index_url)
    except (OSError, urllib.error.URLError):
        if _PROXY_DISABLED or not _reachable(_proxy_url()):
            raise SkipApp(f"无法访问 {index_url}")
        try:
            html = _fetch_text(index_url, _proxy_url())
        except (OSError, urllib.error.URLError):
            raise SkipApp(f"无法访问 {index_url}")

    files = [f for f in re.findall(r'href="([^"]+\.deb)"', html) if arch in f]
    if not files:
        raise SkipApp(f"{index_url} 里没有匹配 {arch} 的 .deb")

    def ver_key(f: str) -> tuple:
        m = re.search(r"_(\d+(?:\.\d+)*)-(\d+)_", f)
        return (tuple(int(x) for x in m.group(1).split(".")) + (int(m.group(2)),)
                if m else (0,))

    return urllib.parse.urljoin(index_url, max(files, key=ver_key))


def resolve_deb_url(arg: str) -> str:
    """arg 是 .deb 直链就原样返回；是目录 URL 就取最新版。"""
    if arg in _DEB_URL:
        return _DEB_URL[arg]
    path = urllib.parse.urlparse(arg).path
    url = arg if path.endswith(".deb") else _latest_in_pool(arg, detect_arch())
    _DEB_URL[arg] = url
    return url


def http_download(url: str, dest: str, desc: str, proxy: str | None = None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ubuntuinit"})
    with _open(req, proxy=proxy) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        show_bar = console is not None and total >= BAR_MIN_BYTES
        if not show_bar:
            while chunk := resp.read(65536):
                f.write(chunk)
            return
        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(desc, total=total)
            while chunk := resp.read(65536):
                f.write(chunk)
                progress.update(task, advance=len(chunk))


def run(cmd: list[str], dry: bool, label: str, proxy: str | None = None) -> None:
    """执行命令。有 rich 时用状态圈包住并隐藏输出，失败才回显。"""
    if dry:
        say("    [dim]$ " + " ".join(shlex.quote(c) for c in cmd) + "[/]"
            if console else "    $ " + " ".join(cmd))
        return

    if console is not None:
        with console.status(f"[cyan]{label}...[/]", spinner="dots"):
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  env=child_env(proxy))
        if proc.returncode != 0:
            detail = _tail(proc.stderr) or _tail(proc.stdout)
            fail(f"{label} 失败\n{detail}")
        return

    try:
        subprocess.run(cmd, check=True, env=child_env(proxy))
    except FileNotFoundError:
        fail(f"找不到命令: {cmd[0]}（是否已安装？）")
    except subprocess.CalledProcessError as e:
        fail(f"{label} 失败（退出码 {e.returncode}）")


# --------------------------------------------------------------------------- #
# sudo 预认证（否则隐藏输出后无法输入密码）
# --------------------------------------------------------------------------- #
def ensure_sudo(skip: bool) -> None:
    if skip or _ok(["sudo", "-n", "true"]):
        return
    info("需要管理员权限，请按提示输入密码")
    if subprocess.run(["sudo", "-v"]).returncode != 0:
        fail("sudo 认证失败")


# --------------------------------------------------------------------------- #
# "是否已安装" 判断
# --------------------------------------------------------------------------- #
def is_installed(source: str, name: str, arg: str) -> bool:
    if source == "apt":
        return all(_ok(["dpkg", "-s", p]) for p in shlex.split(arg))
    if source == "snap":
        return _ok(["snap", "list", shlex.split(arg)[0]])
    if source == "npm":
        prefix = os.path.expanduser("~/.local")
        return _ok(["npm", "ls", "-g", "--prefix", prefix,
                    "--depth=0", shlex.split(arg)[0]])
    if source == "github":
        token = normalize_tag(github_latest(arg).get("tag_name", ""))
        return bool(token) and any(token in v for v in dpkg_versions())
    if source == "deb":
        try:
            url = resolve_deb_url(arg)
        except SystemExit:
            return False
        pkg = os.path.basename(urllib.parse.urlparse(url).path).split("_")[0]
        return _ok(["dpkg", "-s", pkg])
    if source == "script":
        return os.path.exists(os.path.join(STATE_DIR, name))
    return False


def mark_installed(name: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    open(os.path.join(STATE_DIR, name), "w").close()


# --------------------------------------------------------------------------- #
# 各 source 后端
# --------------------------------------------------------------------------- #
def do_apt(name: str, arg: str, dry: bool) -> None:
    # 走系统镜像，永不代理
    run(["sudo", "apt", "install", "-y", *shlex.split(arg)], dry, f"安装 {name}")


def do_snap(name: str, arg: str, dry: bool) -> None:
    run(["sudo", "snap", "install", *shlex.split(arg)], dry, f"安装 {name}")


def do_npm(name: str, arg: str, dry: bool) -> None:
    prefix = os.path.expanduser("~/.local")
    proxy = None if dry else proxy_for("https://registry.npmjs.org/")
    run(["npm", "install", "-g", "--prefix", prefix, *shlex.split(arg)],
        dry, f"安装 {name}", proxy=proxy)


def do_script(name: str, arg: str, dry: bool) -> None:
    if dry:
        say(f"    [dim]$ bash <(下载并执行 {arg})[/]" if console
            else f"    $ bash <(下载并执行 {arg})")
        return
    proxy = proxy_for(arg)
    fd, path = tempfile.mkstemp(suffix=".sh", prefix="ubuntuinit-")
    os.close(fd)
    try:
        http_download(arg, path, f"下载 {name} 脚本", proxy=proxy)
        run(["bash", path], False, f"安装 {name}", proxy=proxy)
    finally:
        os.remove(path)


def do_github(name: str, arg: str, dry: bool) -> None:
    data = github_latest(arg)
    tag = data.get("tag_name", "?")
    arch = detect_arch()
    matches = [
        a["browser_download_url"]
        for a in data.get("assets", [])
        if a["name"].endswith(".deb") and arch in a["name"]
    ]
    if not matches:
        fail(f"{name}: {arg} ({tag}) 里没有匹配 {arch} 的 .deb")
    url = matches[0]
    if dry:
        say(f"    [dim]$ 下载并安装 {url}[/]" if console else f"    $ {url}")
        return
    proxy = proxy_for(url)
    fd, path = tempfile.mkstemp(suffix=".deb", prefix="ubuntuinit-")
    os.close(fd)
    try:
        http_download(url, path, f"下载 {name} {tag}", proxy=proxy)
        # 本地 .deb 安装，依赖走镜像，不用代理
        run(["sudo", "apt", "install", "-y", path], False, f"安装 {name}")
    finally:
        os.remove(path)


def do_deb(name: str, arg: str, dry: bool) -> None:
    """arg 是 .deb 直链，或一个存放 .deb 的目录 URL（取最新版）。"""
    url = resolve_deb_url(arg)
    if dry:
        say(f"    [dim]$ 下载并安装 {url}[/]" if console else f"    $ {url}")
        return
    proxy = proxy_for(url)
    fd, path = tempfile.mkstemp(suffix=".deb", prefix="ubuntuinit-")
    os.close(fd)
    try:
        http_download(url, path, f"下载 {name}", proxy=proxy)
        run(["sudo", "apt", "install", "-y", path], False, f"安装 {name}")
    finally:
        os.remove(path)


BACKENDS = {
    "apt": do_apt,
    "snap": do_snap,
    "npm": do_npm,
    "script": do_script,
    "github": do_github,
    "deb": do_deb,
}


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #
def load_registry() -> dict:
    if not os.path.exists(REGISTRY_PATH):
        fail(f"找不到清单: {REGISTRY_PATH}")
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


def parse_entry(name: str, value: str) -> tuple[str, str]:
    source, sep, arg = value.partition(":")
    if not sep or not arg.strip():
        fail(f"{name}: 格式应为 '来源:参数'，例如 'apt:git'")
    return source.strip(), arg.strip()


def print_list(registry: dict) -> None:
    width = max((len(n) for n in registry), default=0)
    say("可用 App：")
    for name, value in registry.items():
        say(f"    {name:<{width}}  {value}")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def install_one(name: str, value: str, dry: bool, force: bool) -> None:
    source, arg = parse_entry(name, value)
    backend = BACKENDS.get(source)
    if backend is None:
        fail(f"{name}: 暂不支持的来源 '{source}'")

    try:
        if not force and is_installed(source, name, arg):
            skip(name)
            return
        backend(name, arg, dry)
    except SkipApp as e:
        warn(f"{name} 跳过：{e}")
        return

    if source == "script" and not dry:
        mark_installed(name)

    suffix = "  [dim](dry-run)[/]" if (dry and console) else ("（dry-run）" if dry else "")
    say(f"[green]✓[/] {name}{suffix}" if console else f"✓ {name}{suffix}")


def main() -> None:
    global _PROXY_FORCED, _PROXY_DISABLED
    parser = argparse.ArgumentParser(
        prog="ubuntuinit.py",
        description="用 apps.json 清单在 Ubuntu 上装软件",
    )
    parser.add_argument("names", nargs="*", help="App 短名（可多个），或 all / list")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="只演示将执行的命令，不真正安装")
    parser.add_argument("-f", "--force", action="store_true",
                        help="忽略'已安装则跳过'，强制重装")
    parser.add_argument("--proxy", nargs="?", const=PROXY_DEFAULT, metavar="URL",
                        help=f"所有联网下载都走代理（默认 {PROXY_DEFAULT}）")
    parser.add_argument("--no-proxy", action="store_true", help="完全不用代理")
    args = parser.parse_args()

    registry = load_registry()

    if not args.names or args.names[0] == "list":
        print_list(registry)
        return

    if args.names[0] == "all":
        names = list(registry)          # 按 apps.json 里的顺序
    else:
        for n in args.names:
            if n not in registry:
                fail(f"清单里没有 '{n}'（用 list 查看）")
        names = args.names

    _PROXY_FORCED = args.proxy
    _PROXY_DISABLED = args.no_proxy

    need_sudo = any(parse_entry(n, registry[n])[0] in SUDO_SOURCES for n in names)
    ensure_sudo(args.dry_run or not need_sudo)

    for name in names:
        install_one(name, registry[name], args.dry_run, args.force)

    say("[green]全部完成。[/]" if console else "全部完成。")


if __name__ == "__main__":
    main()
