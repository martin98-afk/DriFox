# -*- coding: utf-8 -*-
"""更新链路代理解析：配置 → httpx / requests 参数

四种模式：
- direct：强制直连。必须显式 trust_env=False —— httpx 的 proxy=None
  **不**生效，仍会走环境变量代理（实测 0.28.1）
- system：交给库默认（trust_env=True），读环境变量 + 系统代理设置
- prefix：URL 改写（前缀 + 原 URL），连接参数强制直连
- http  ：转发全部请求到指定 HTTP 代理

设计约束（来自 docs/superpowers/specs/2026-09-14-update-download-proxy-design.md）：
- 加速前缀**只作用于下载链路**。实测 8 个主流 GitHub 加速站无一真代理
  api.github.com（返回 200 的是自家首页 HTML），检查更新必须直连。
- 不支持 SOCKS5：socksio / PySocks 均未安装，填 socks5:// 必然失败，
  在校验层提前拦下并说明。
- 不做自动故障回退：静默直连会让用户误以为代理生效，实际在 38KB/s 上爬。
"""

from __future__ import annotations

import re

from loguru import logger

# 模式常量（与 config.py 的 OptionsValidator 取值一致）
MODE_DIRECT = "direct"
MODE_SYSTEM = "system"
MODE_PREFIX = "prefix"
MODE_HTTP = "http"

# 连通性测试目标：release 小资产（287B 纯文本哈希），体量小、判定快
# 不探 api.github.com —— prefix 模式本就不代理 API 域，探它必然失败并误导用户
_TEST_URL = "https://github.com/martin98-afk/DriFox/releases/download/v0.6.0/SHA256SUMS"
# 实测：网络受限时 ConnectTimeout 要 21s 才落地，测试须给足时间否则误报为地址错误
_TEST_TIMEOUT = 25.0


def _cfg():
    """延迟取配置单例（避免模块导入期的 Qt 依赖）"""
    from app.utils.config import Settings

    return Settings.get_instance()


def _mode() -> str:
    try:
        return str(_cfg().update_proxy_mode.value or MODE_DIRECT)
    except Exception:
        return MODE_DIRECT


def _prefix_addr() -> str:
    try:
        return str(_cfg().update_proxy_prefix.value or "").strip()
    except Exception:
        return ""


def _http_addr() -> str:
    try:
        return str(_cfg().update_proxy_url.value or "").strip()
    except Exception:
        return ""


# ── URL 改写 ──


def rewrite_url(url: str) -> str:
    """prefix 模式拼接前缀；其余模式原样返回。

    仅下载链路调用 —— 检查更新走直连，不经过这里。
    """
    if _mode() != MODE_PREFIX:
        return url
    base = _prefix_addr().rstrip("/")
    if not base:
        # 选了 prefix 但没填地址：退化为直连而不是拼出坏 URL
        return url
    return f"{base}/{url.lstrip('/')}"


# ── 连接参数 ──


def httpx_kwargs() -> dict:
    """给 httpx.AsyncClient(**kwargs)；语义与 apply_to_session 保持一致"""
    mode = _mode()
    if mode == MODE_SYSTEM:
        # 库默认 trust_env=True，读环境变量 + 系统代理设置
        return {}
    if mode == MODE_HTTP:
        addr = _http_addr()
        if addr:
            return {"proxy": addr}
        # 没填地址 → 退化为直连
    # direct / prefix / http(空地址)：显式直连
    return {"trust_env": False}


def apply_to_session(session) -> None:
    """给 requests.Session 应用 trust_env / proxies"""
    mode = _mode()
    if mode == MODE_SYSTEM:
        session.trust_env = True
        session.proxies = {}
        return
    if mode == MODE_HTTP:
        addr = _http_addr()
        if addr:
            session.trust_env = False
            session.proxies = {"http": addr, "https": addr}
            return
    # direct / prefix / http(空地址)：显式直连
    session.trust_env = False
    session.proxies = {}


# ── 校验 ──


def validate(mode: str, addr: str) -> tuple[bool, str]:
    """地址格式校验（不发起网络请求）"""
    addr = (addr or "").strip()
    if not addr:
        return False, "地址不能为空"
    low = addr.lower()
    if low.startswith("socks"):
        return False, "不支持 SOCKS5 代理，请填 http:// 或 https:// 地址"
    if mode in (MODE_PREFIX, MODE_HTTP):
        if not re.match(r"^https?://", low):
            return False, "地址必须以 http:// 或 https:// 开头"
        if mode == MODE_HTTP:
            m = re.match(r"^https?://([^:/]+)(?::(\d+))?", addr)
            if not m or not m.group(2):
                return False, "代理地址必须包含端口，如 http://127.0.0.1:7890"
        return True, ""
    # direct / system 不需要地址
    return True, ""


# ── 系统代理探测（UI 展示用）──


def _read_system_proxy() -> dict:
    """读系统代理设置；返回 {"enable": bool, "server": str}

    经 urllib 统一入口：Windows 读注册表、macOS 读 SystemConfiguration、
    其余读环境变量。httpx 与 requests 内部走的是同一函数，所以这里的
    探测结果与实际请求行为一致。
    """
    result = {"enable": False, "server": ""}
    try:
        from urllib.request import getproxies

        proxies = getproxies() or {}
        # 环境变量代理没有「开关」概念，有值即启用
        server = proxies.get("https") or proxies.get("http") or ""
        if server:
            result["enable"] = True
            result["server"] = server
    except Exception as e:
        logger.debug(f"[UpdateProxy] 读取系统代理失败: {e}")

    if not result["server"]:
        # Windows 上 ProxyEnable=0 时 urllib 会返回空 dict，但注册表里可能
        # 残留 ProxyServer。用户需要知道「填了地址但开关关着」这个状态。
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            try:
                enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except FileNotFoundError:
                enable = 0
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except FileNotFoundError:
                server = ""
            winreg.CloseKey(key)
            result["enable"] = bool(enable) and bool(server)
            result["server"] = str(server or "")
        except Exception:
            pass
    return result


def probe_system_proxy() -> str:
    """UI 用：探测系统代理当前状态，返回人类可读描述"""
    info = _read_system_proxy()
    server = info.get("server") or ""
    if not server:
        return "系统未设置代理"
    if info.get("enable"):
        return f"系统代理已启用（{server}）"
    return f"系统代理未启用（已填 {server}，但开关关着）"


# ── 连通性测试 ──


def _looks_like_text_content(body: bytes) -> bool:
    """判据：响应是纯文本哈希（非 HTML）

    加速站会对不支持的路径返回 200 + 自家首页 HTML，只判状态码会得出
    「测试通过、实际不可用」。所以必须看内容形态。
    """
    if not body or len(body) > 4096:
        return False
    head = body[:64].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html") or head.startswith(b"<head"):
        return False
    # SHA256SUMS 是「64 位十六进制 + 空格 + 文件名」的纯文本
    return b"<" not in body[:32]


def test_connection(mode: str, addr: str, url: str = "") -> tuple[bool, str]:
    """用给定配置（不落盘）发起一次真实请求，返回 (是否通过, 提示)

    探测目标是 release 小资产而非安装包本体：SHA256SUMS 只有 287 字节，
    毫秒级；安装包 93MB 起步，点一下测试要等几分钟。
    """
    ok, msg = validate(mode, addr)
    if not ok:
        return False, msg

    import time

    import httpx

    target = url or _TEST_URL
    if mode == MODE_PREFIX:
        base = (addr or "").strip().rstrip("/")
        target = f"{base}/{target.lstrip('/')}"
        kwargs = {"trust_env": False}
    elif mode == MODE_HTTP:
        kwargs = {"proxy": (addr or "").strip()}
    else:
        kwargs = {"trust_env": False}

    start = time.time()
    try:
        with httpx.Client(timeout=_TEST_TIMEOUT, follow_redirects=True, **kwargs) as client:
            resp = client.get(target, headers={"User-Agent": "DriFox-update-probe"})
        ms = int((time.time() - start) * 1000)
        if resp.status_code != 200:
            return False, f"失败: HTTP {resp.status_code}（{ms} ms）"
        if not _looks_like_text_content(resp.content):
            return False, (
                f"失败: 返回内容不是预期的文件（{len(resp.content)}B，疑似加速站首页），该前缀可能未代理此路径"
            )
        return True, f"通过（{ms} ms）"
    except Exception as e:
        ms = int((time.time() - start) * 1000)
        return False, f"失败: {type(e).__name__} {e}（{ms} ms）"
