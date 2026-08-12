# -*- coding: utf-8 -*-
"""GitHub 加速代理配置管理器

三种模式：
- prefix：前缀加速站，地址 + 原 URL 拼接（https://ghfast.top/）
- selfhost：自建代理服务（deno.dev 风格），地址 + 原 URL 拼接
- http：HTTP 正向代理（http://ip:port），httpx 走 proxy= 参数、
  git 走 -c http.proxy= 参数

配置持久化到 .drifox/plugins/user-custom/marketplaces/proxy.json
（随 user-custom 插件一起被云端备份/同步）。损坏/缺失视为未启用。
"""

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import httpx
from loguru import logger


def _drifox_dir() -> Path:
    """获取应用数据目录（与 app.utils.utils.get_app_data_dir 保持一致）"""
    if not hasattr(sys, "_MEIPASS") and not getattr(sys, "frozen", False):
        return Path(".drifox")
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
                return app_support / ".drifox"
        except Exception:
            pass
    return Path.home() / ".drifox"


class ProxyConfig:
    """代理配置：读写 proxy.json + URL 改写 + httpx/git 参数构造"""

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
        """prefix/selfhost：地址 + 原 URL；http/未启用：原样返回"""
        if not self._enabled or self._mode == "http":
            return url
        base = self._address.rstrip("/")
        if not base:
            return url
        return f"{base}/{url.lstrip('/')}"

    def httpx_kwargs(self) -> dict:
        """http 模式返回 {'proxy': address}；其他返回 {}"""
        if self._enabled and self._mode == "http":
            return {"proxy": self._address}
        return {}

    def git_clone_args(self, url: str) -> Tuple[str, list]:
        """返回 (改写后 URL, git 额外参数)

        http 模式：URL 不变，参数 ['-c', 'http.proxy={address}']
        prefix/selfhost：URL 改写；github.com 仓库自动补 .git 后缀
        未启用：原样返回，无参数（零开销）
        """
        if not self._enabled:
            return url, []
        if self._mode == "http":
            return url, ["-c", f"http.proxy={self._address}"]
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
        """地址格式校验（不发起网络请求）"""
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
        tmp = ProxyConfig.__new__(ProxyConfig)
        tmp._file = self._file
        tmp._enabled = True
        tmp._mode = mode
        tmp._address = address.strip()
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
                    f"✗ 失败: 返回非 git 协议响应（前4字节 {preview!r}），"
                    "加速站可能未代理 github.com"
                )
            ms = int((time.time() - start) * 1000)
            return True, f"✓ 通过（{ms} ms）"
        except Exception as e:
            return False, f"✗ 失败: {e}"


# ── 单例 ──

_instance: Optional[ProxyConfig] = None


def get_proxy_config() -> ProxyConfig:
    global _instance
    if _instance is None:
        _instance = ProxyConfig()
    return _instance
