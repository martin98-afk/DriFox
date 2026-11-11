# -*- coding: utf-8 -*-
"""GitHub 加速代理配置管理器

四种模式：
- prefix：前缀加速站，地址 + 原 URL 拼接（https://ghfast.top/）
- selfhost：自建代理服务（deno.dev 风格），地址 + 原 URL 拼接
- http：HTTP 正向代理（http://ip:port），httpx 走 proxy= 参数、
  git 走 -c http.proxy= 参数
- system：系统代理，行为同 http 但地址自动探测（Windows 注册表 /
  环境变量 / macOS scutil），无需手填；探测不到时自动直连

配置持久化到 .drifox6/plugins/user-custom/marketplaces/proxy.json
（随 user-custom 插件一起被云端备份/同步）。损坏/缺失视为未启用。
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import httpx
from loguru import logger


def _drifox_dir() -> Path:
    """获取应用数据目录（与 app.utils.utils.get_app_data_dir 保持一致，开发环境解析为绝对路径）

    开发环境: 仓库根/.drifox（基于 __file__ 定位，脱离可变 cwd）
    PyInstaller打包: ~/.drifox（用户 home 目录，可写）
    macOS .app: ~/Library/Application Support/Drifox/.drifox6

    注意：相对路径 Path(".drifox6") 会在每次文件操作时按当前 cwd 解析；
    Windows 原生 QFileDialog 会静默改变进程 cwd，导致相对路径被解析到
    错误目录而 FileNotFoundError。开发环境改为基于 __file__ 定位仓库根，
    返回绝对路径，彻底脱离 cwd 依赖。
    """
    if not hasattr(sys, "_MEIPASS") and not getattr(sys, "frozen", False):
        here = Path(__file__).resolve()
        root = here.parent
        for _ in range(8):
            if (root / ".git").exists():
                return root / ".drifox6"
        return here.parents[3] / ".drifox6"
    if sys.platform == "darwin":
        try:
            from AppKit import NSApplicationSupportDirectory, NSFileManager, NSUserDomainMask

            paths = NSFileManager.defaultManager().URLsForDirectory_inDomains_(
                NSApplicationSupportDirectory, NSUserDomainMask
            )
            if paths:
                app_support_path = paths[0].fileSystemRepresentation().decode("utf-8")
                app_support = Path(app_support_path) / "Drifox"
                app_support.mkdir(parents=True, exist_ok=True)
                return app_support / ".drifox6"
        except Exception:
            pass
    return Path.home() / ".drifox6"


# ── 系统代理探测 ──

_PROXY_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_PROXY_ADDR_RE = re.compile(r"^https?://([^:/@]+):(\d+)/?$")


def _normalize_proxy_addr(raw: str) -> Optional[str]:
    """规范化代理地址为 http://host:port；无法解析返回 None"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not _PROXY_SCHEME_RE.match(raw):
        raw = "http://" + raw
    return raw if _PROXY_ADDR_RE.match(raw) else None


def _win_registry_proxy() -> Optional[str]:
    """读 Windows 注册表系统代理（Clash/v2rayN 等工具「系统代理」开关写这里）"""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enable:
                return None
            raw, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            winreg.CloseKey(key)
    except OSError:
        return None
    raw = (raw or "").strip()
    if not raw:
        return None
    # 分协议形式："http=127.0.0.1:7890;https=127.0.0.1:7891;<local>"
    if "=" in raw:
        picked = None
        for part in raw.split(";"):
            part = part.strip()
            if not part or part.startswith("<"):
                continue
            proto, _, addr = part.partition("=")
            if proto.lower() in ("http", "https") and addr:
                picked = addr
                if proto.lower() == "https":  # 下载走 https，https 优先
                    break
        raw = picked or ""
    return _normalize_proxy_addr(raw)


def _mac_scutil_proxy() -> Optional[str]:
    """读 macOS 系统代理（scutil --proxy）"""
    try:
        out = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=3
        ).stdout
    except Exception:
        return None
    m = re.search(r"HTTPProxy\s*:\s*(\S+)", out)
    p = re.search(r"HTTPPort\s*:\s*(\d+)", out)
    if not (re.search(r"HTTPEnable\s*:\s*1", out) and m and p):
        return None
    return _normalize_proxy_addr(f"{m.group(1)}:{p.group(1)}")


def _env_proxy() -> Optional[str]:
    """读环境变量代理（HTTPS_PROXY > HTTP_PROXY > ALL_PROXY，大小写兼容）"""
    for name in (
        "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
    ):
        addr = _normalize_proxy_addr(os.environ.get(name, ""))
        if addr:
            return addr
    return None


def detect_system_proxy() -> Optional[str]:
    """探测系统全局代理地址，返回 http://host:port；未检测到返回 None

    优先级：Windows/macOS 系统代理（注册表 / scutil）> 环境变量。
    只认带端口的 HTTP(S) 代理；PAC/脚本代理不在探测范围。
    """
    if sys.platform == "win32":
        addr = _win_registry_proxy()
        if addr:
            return addr
    elif sys.platform == "darwin":
        addr = _mac_scutil_proxy()
        if addr:
            return addr
    return _env_proxy()


class ProxyConfig:
    """代理配置：读写 proxy.json + URL 改写 + httpx/git 参数构造"""

    HTTP_LIKE_MODES = ("http", "system")

    def __init__(self, file: Optional[Path] = None):
        self._file = file or (_drifox_dir() / "plugins" / "user-custom" / "marketplaces" / "proxy.json")
        self._enabled = False
        self._mode = "prefix"
        self._address = ""
        self._load()

    # ── 持久化 ──

    def _load(self):
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self._enabled = bool(data.get("enabled", False))
            self._mode = data.get("mode", "prefix")
            self._address = data.get("address", "")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"[Marketplace] 代理配置损坏，视为未启用: {e}")
            self._enabled = False
            self._mode = "prefix"
            self._address = ""

    def save(self, enabled: bool, mode: str, address: str) -> bool:
        """保存配置；地址非法返回 False 且不落盘"""
        ok, _ = self.validate(mode, address)
        if not ok:
            return False
        self._enabled = enabled
        self._mode = mode
        self._address = address.strip()
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(
                json.dumps(
                    {"enabled": self._enabled, "mode": self._mode, "address": self._address},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return True
        except Exception as e:
            logger.error(f"[Marketplace] 保存代理配置失败: {e}")
            return False

    # ── 查询 ──

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def address(self) -> str:
        return self._address

    # ── URL 改写与参数构造 ──

    def rewrite_url(self, url: str) -> str:
        """prefix/selfhost：地址 + 原 URL；http/system/未启用：原样返回"""
        if not self._enabled or self._mode in self.HTTP_LIKE_MODES:
            return url
        base = self._address.rstrip("/")
        if not base:
            return url
        return f"{base}/{url.lstrip('/')}"

    def http_proxy_address(self) -> str:
        """正向代理的实际地址：http/system 模式返回代理地址，其余返回 ''

        system 模式每次实时探测（注册表/环境变量，本地操作毫秒级），
        探测不到返回空串由调用方直连——系统代理关了就自动直连。
        """
        if not self._enabled:
            return ""
        if self._mode == "http":
            return self._address
        if self._mode == "system":
            return detect_system_proxy() or ""
        return ""

    def httpx_kwargs(self) -> dict:
        """http/system 模式且代理可用时返回 {'proxy': address}；其余 {}"""
        addr = self.http_proxy_address()
        if addr:
            return {"proxy": addr}
        return {}

    def git_clone_args(self, url: str) -> Tuple[str, list]:
        """返回 (改写后 URL, git 额外参数)

        http/system 模式：URL 不变，探测到地址时参数 ['-c', 'http.proxy={address}']，
        探测不到直连无参数；prefix/selfhost：URL 改写；github.com 仓库自动补 .git 后缀；
        未启用：原样返回，无参数（零开销）
        """
        if not self._enabled:
            return url, []
        if self._mode in self.HTTP_LIKE_MODES:
            addr = self.http_proxy_address()
            if addr:
                return url, ["-c", f"http.proxy={addr}"]
            return url, []
        return self._ensure_git_suffix(self.rewrite_url(url)), []

    @staticmethod
    def _ensure_git_suffix(url: str) -> str:
        """给 github.com 仓库 URL 自动补 .git 后缀

        marketplace.json 里 source.url 通常写 ``https://github.com/owner/repo``
        （不带 .git），某些加速站 + git 组合下 git 探测会失败（exit 12），
        加 .git 后探测更稳。带 .git 已有的、查询串、空 URL、ssh / git 协议
        不动。加速站后跟随的 github.com 路径同样处理（如
        ``https://ghfast.top/https://github.com/owner/repo``）。
        """
        if not url or url.endswith(".git"):
            return url
        # 拆 query / fragment，只对 path 部分加 .git
        suffix = ""
        for sep in ("?", "#"):
            if sep in url:
                idx = url.find(sep)
                suffix = url[idx:]
                url = url[:idx]
                break
        marker = "/github.com/"
        lower = url.lower()
        if marker not in lower:
            return url + suffix
        # 取 marker 之后的部分：owner/repo[/sub/...]
        idx = lower.find(marker)
        head = url[: idx + len(marker)]
        rest = url[idx + len(marker):]
        parts = rest.split("/", 2)
        # 只在 path 是 owner/repo（仓库根）时补 .git，已有 subdir 不补
        if len(parts) != 2 or not parts[0] or not parts[1] or "/" in parts[1]:
            return url + suffix
        parts[1] = parts[1] + ".git"
        return head + "/".join(parts) + suffix

    # ── 校验与连通性 ──

    @staticmethod
    def validate(mode: str, address: str) -> Tuple[bool, str]:
        """地址格式校验（不发起网络请求）；system 模式地址自动探测，不校验"""
        if mode == "system":
            return True, ""
        address = address.strip()
        if not address:
            return False, "地址不能为空"
        if mode in ("prefix", "selfhost"):
            if not re.match(r"^https?://", address):
                return False, "加速站地址必须以 http:// 或 https:// 开头"
        elif mode == "http":
            if not re.match(r"^https?://", address):
                return False, "HTTP 代理地址必须以 http:// 或 https:// 开头"
            m = re.match(r"^https?://([^:/]+)(?::(\d+))?", address)
            if not m or not m.group(2):
                return False, "HTTP 代理地址必须包含端口，如 http://127.0.0.1:7890"
        else:
            return False, f"未知模式: {mode}"
        return True, ""

    def test_connection(self, mode: str, address: str, url: Optional[str] = None) -> Tuple[bool, str]:
        """用给定配置（不落盘）发起一次真实请求，返回 (是否通过, 提示)

        默认探测点：git smart-HTTP 的 info/refs 端点（模拟 git clone 首请求），
        不是 raw.githubusercontent.com 上的 marketplace.json。某些加速站只代理
        raw 静态文件，不代理 github.com 的 git 协议请求，导致测试通过但实际
        git clone 失败 —— 用 info/refs 才能真实反映下载能力。

        GitHub info/refs 返回 200 + flush-packet（响应首4字节以 "00" 开头），
        如：``001e# service=git-upload-pack\\n0000``。除了 200 还验证响应
        真的是 git 协议，避免加速站把请求重定向到自家错误页（也返回 200）。
        """
        ok, msg = self.validate(mode, address)
        if not ok:
            return False, msg
        target = url or (
            "https://github.com/martin98-afk/drifox-plugins"
            "/info/refs?service=git-upload-pack"
        )
        addr = address
        if mode == "system":
            addr = detect_system_proxy() or ""
            if not addr:
                return False, "未检测到系统代理（请在代理工具中开启「系统代理」）"
        tmp = ProxyConfig.__new__(ProxyConfig)
        tmp._file = self._file
        tmp._enabled = True
        tmp._mode = "http" if mode == "system" else mode
        tmp._address = addr.strip()
        start = time.time()
        try:
            resp = httpx.get(
                tmp.rewrite_url(target),
                timeout=8,
                follow_redirects=True,
                headers={"User-Agent": "git/2.43.0", "Accept": "*/*"},
                **tmp.httpx_kwargs(),
            )
            resp.raise_for_status()
            # 验证响应是真正的 git 协议响应：flush-packet 首4字节以 "00" 开头
            preview = resp.content[:4]
            if not preview.startswith(b"00"):
                return False, (
                    f"失败: 返回非 git 协议响应（前4字节 {preview!r}），"
                    "加速站可能未代理 github.com"
                )
            ms = int((time.time() - start) * 1000)
            return True, f"通过（{ms} ms）"
        except Exception as e:
            return False, f"失败: {e}"


# ── 单例 ──

_instance: Optional[ProxyConfig] = None


def get_proxy_config() -> ProxyConfig:
    global _instance
    if _instance is None:
        _instance = ProxyConfig()
    return _instance
