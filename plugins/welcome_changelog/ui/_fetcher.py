# -*- coding: utf-8 -*-
"""欢迎卡片「更新」tab — GitHub Releases 后台拉取 + 异步刷新派发。

线程模型：
- ``ChangelogFetcher`` 继承 ``QThread``，在子线程跑 ``httpx`` 拉取；
- 完成后 ``emit finished`` / ``error`` 到 ``_Bridge``（主线程 QObject），
  PyQt 跨线程自动 ``QueuedConnection``，所有缓存写与 ``UIEventBus.publish``
  都在**主线程**执行，避免 MessageCard 子线程触碰 PyQt 控件。

模块级单例：
- ``_changelog_cache``：进程内缓存 ``{releases, fetched_at, etag}``
- ``_bridge``：主线程 QObject 桥接器（懒构造，需 Qt 事件循环）
- ``_active_fetcher``：当前在跑的 fetcher（避免并发）
"""

from __future__ import annotations

import threading
import time
from html import escape
from typing import List, Optional

from loguru import logger
from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal

from app.core.ui_event_bus import EV_WELCOME_TAB_REFRESHED, UIEventBus

_REPO = "martin98-afk/DriFox"
_CACHE_TTL = 3600  # 1h
_MAX_RELEASES = 20
_PLUGIN_NAME = "welcome_changelog"
_MODE_KEY = "changelog"

_cache: dict = {}  # {releases, fetched_at, etag, last_error}
_cache_lock = threading.Lock()
_active_fetcher: Optional["ChangelogFetcher"] = None
_fetcher_lock = threading.Lock()
_bridge: Optional["_Bridge"] = None
_bridge_lock = threading.Lock()


def _get_bridge() -> "_Bridge":
    """懒构造主线程桥接器（首次调用需在 Qt 主线程上下文内）。"""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = _Bridge()
        return _bridge


def _publish_refresh() -> None:
    """主线程调用：派发刷新事件，MessageCard 订阅后对当前 mode 重渲染 body。"""
    try:
        UIEventBus.get_instance().publish(
            EV_WELCOME_TAB_REFRESHED,
            mode_key=_MODE_KEY,
            plugin_name=_PLUGIN_NAME,
        )
    except Exception as e:
        logger.warning(f"[{_PLUGIN_NAME}] 派发刷新事件失败: {e}")


def get_cached_state() -> dict:
    """返回缓存当前状态供 render_func 决策。

    Returns:
        dict 含以下键：
          - ``releases``: list | None（命中有效缓存时为 list，否则 None）
          - ``last_error``: str | None（最近一次 fetcher 错误信息）
          - ``loading``: bool（fetcher 是否正在跑）
    """
    with _cache_lock:
        releases = None
        if _cache and (time.time() - _cache.get("fetched_at", 0)) < _CACHE_TTL:
            releases = list(_cache.get("releases") or [])
        last_error = _cache.get("last_error")
    with _fetcher_lock:
        loading = _active_fetcher is not None and _active_fetcher.isRunning()
    return {"releases": releases, "last_error": last_error, "loading": loading}


def get_cached_releases() -> Optional[list]:
    """命中有效缓存返回 releases 列表；未命中 / 已过期返回 None。

    兼容旧调用方；新代码优先用 ``get_cached_state()`` 获取完整状态。
    """
    return get_cached_state()["releases"]


def start_fetch() -> None:
    """触发后台拉取（幂等：缓存有效 → 派发刷新；fetcher 在跑 → 跳过）。"""
    cached = get_cached_releases()
    if cached is not None:
        _publish_refresh()
        return
    _ensure_fetcher()


def _ensure_fetcher() -> None:
    """启动 fetcher；同一时刻只允许一个活动实例。"""
    global _active_fetcher
    with _fetcher_lock:
        if _active_fetcher is not None and _active_fetcher.isRunning():
            return
        etag = _cache.get("etag", "")
        fetcher = ChangelogFetcher(etag=etag)
        bridge = _get_bridge()
        # 跨线程 QueuedConnection：fetcher 在子线程 emit，bridge slot 跑在主线程
        fetcher.finished.connect(bridge.on_finished, Qt.AutoConnection)
        fetcher.error.connect(bridge.on_error, Qt.AutoConnection)
        _active_fetcher = fetcher
    fetcher.start()


def _on_finished_main(payload: list) -> None:
    """主线程槽：fetcher 成功回调。"""
    global _active_fetcher
    with _fetcher_lock:
        _active_fetcher = None
    try:
        if isinstance(payload, list) and payload and isinstance(payload[0], dict) and "releases" in payload[0]:
            data = payload[0]
            new_releases = data.get("releases") or []
            new_etag = data.get("etag", "")
            with _cache_lock:
                old_tags = [r.get("tag_name", "") for r in _cache.get("releases", [])]
                new_tags = [r.get("tag_name", "") for r in new_releases]
                if old_tags and old_tags == new_tags:
                    # tag 未变（GitHub etag 重生成等）→ 仅刷新 etag/fetched_at，不派发
                    _cache["etag"] = new_etag or _cache.get("etag", "")
                    _cache["fetched_at"] = time.time()
                    return
                _cache["releases"] = new_releases
                _cache["etag"] = new_etag
                _cache["fetched_at"] = time.time()
        else:
            # 304 等空 payload：缓存仍新鲜，无需重渲染
            return
    except Exception as e:
        logger.warning(f"[{_PLUGIN_NAME}] 处理 fetcher 结果失败: {e}")
        return
    _publish_refresh()


def _on_error_main(msg: str) -> None:
    """主线程槽：fetcher 错误回调。错误也派发刷新，让卡片展示错误占位。"""
    global _active_fetcher
    with _fetcher_lock:
        _active_fetcher = None
    logger.warning(f"[{_PLUGIN_NAME}] 拉取失败: {msg}")
    with _cache_lock:
        _cache["last_error"] = msg
        # 失败不更新 fetched_at，避免 TTL 内反复拉取；下次手动重试需 clear_cache_for_tests
    _publish_refresh()


def clear_cache_for_tests() -> None:
    """测试钩子：重置缓存 + 活动 fetcher + bridge。"""
    global _active_fetcher, _bridge
    with _cache_lock:
        _cache.clear()
    with _fetcher_lock:
        _active_fetcher = None
    with _bridge_lock:
        _bridge = None


class _Bridge(QObject):
    """主线程 QObject 桥接器。

    PyQt 跨线程自动 ``QueuedConnection``：fetcher 子线程 ``emit`` →
    本对象的 slot 跑在主线程，所有缓存写与 ``UIEventBus.publish`` 都安全。
    """

    def on_finished(self, payload: list) -> None:
        _on_finished_main(payload)

    def on_error(self, msg: str) -> None:
        _on_error_main(msg)


class ChangelogFetcher(QThread):
    """QThread 后台拉 GitHub Releases；emit finished(list) / error(str)。"""

    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, etag: str = "", parent=None):
        super().__init__(parent)
        self._etag = etag

    def run(self) -> None:
        try:
            import httpx
        except ImportError:
            self.error.emit("缺少 httpx 依赖")
            return
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._etag:
            headers["If-None-Match"] = self._etag
        url = f"https://api.github.com/repos/{_REPO}/releases?per_page=5"
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0)) as client:
                resp = client.get(url, headers=headers)
        except Exception as e:
            self.error.emit(f"网络错误：{e}")
            return
        if resp.status_code == 304:
            self.finished.emit([])
            return
        if resp.status_code != 200:
            self.error.emit(f"GitHub API {resp.status_code}：{resp.text[:120]}")
            return
        try:
            data = resp.json()
        except Exception as e:
            self.error.emit(f"解析失败：{e}")
            return
        new_etag = resp.headers.get("ETag", "")
        releases: List[dict] = []
        # 线程私有 markdown 实例（与 message_card 同源经验：全局 md 非线程安全，
        # 共享会偶发 0.4% 表格串扰 / 渲染失败）
        try:
            from markdown import Markdown

            md = Markdown(
                extensions=["fenced_code", "nl2br", "tables"],
                output_format="html5",
                safe=False,
            )
        except ImportError:
            md = None
        for item in data:
            body_md = item.get("body") or ""
            if md is not None:
                try:
                    body_html = md.convert(body_md)
                except Exception:
                    body_html = escape(body_md).replace("\n", "<br>")
                md.reset()
            else:
                body_html = escape(body_md).replace("\n", "<br>")
            releases.append(
                {
                    "tag_name": item.get("tag_name", ""),
                    "name": item.get("name", ""),
                    "body_html": body_html,
                    "published_at": item.get("published_at", ""),
                    "html_url": item.get("html_url", ""),
                }
            )
        self.finished.emit([{"releases": releases, "etag": new_etag}])