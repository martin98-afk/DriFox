"""CodeWebViewer 复用池（方案 A：WebView 池化）。

为什么需要它
------------
既有 B4 层（``MainWidget._recycle_lru_batches`` / ``_unload_batch``）已经能把
离屏批次的 UI 卸载掉，把并发 WebEngine 页数压在配额内。但它卸载的方式是
``delete_widgets_from_layout(..., call_cleanup=True)`` —— **销毁**
``QWebEngineView``。用户上滚回去时，批次要重建卡片并重新创建一个
``CodeWebViewer``，代价是：

* 一次 Chromium renderer 进程/上下文初始化（项目注释记录的量级 100–500ms 主线程占用）；
* 骨架 HTML 重新注入 + 全量渲染；
* 视觉上闪一下（用户描述的「重新加载一下」）。

池化把这步改成「摘下来 → 放池里 → 需要时直接复用实例」，
**renderer 进程与已初始化的 WebContents 保留，只换父对象与内容**。

设计要点
--------
* **分桶**：welcome 卡片用 light 骨架（无 echarts CDN），与常规骨架不能混用，
  按 ``light`` 分成两个桶。
* **隐藏宿主**：归还的 viewer 挂到一个 ``WA_DontShowOnScreen`` 的宿主上，
  保持 native 资源存活但绝不映射到屏幕（避免幽灵窗口，这是项目里踩过的坑）。
* **异常自愈**：任何一步 RuntimeError / C++ 对象已删除，都丢弃该 viewer 并
  回退到「新建」；连续失败达到阈值则整池停用，绝不因为池化引入崩溃。
* **与 B4 协同**：池中 viewer 的 renderer 进程仍占用内存且随时会被复用，
  因此通过 :meth:`pids` 暴露给 ``_pid_still_in_use`` 护栏，避免被强回收误杀。
"""

import contextlib
from typing import Dict, List, Optional, Set

from PyQt5.QtCore import Qt

try:  # 仅用于剔除已销毁对象，缺失时退化为不清理
    import sip
except Exception:  # pragma: no cover
    sip = None

# 每个桶（light / full）保留的空闲 viewer 上限。
# 这是"常驻但不显示"的 Chromium 实例，直接占用内存，不宜过大；
# 4 个足以覆盖上下滚动时的一进一出。
MAX_IDLE_PER_BUCKET = 4

# 连续失败多少次后整池停用（防止在异常环境里反复做无效尝试）
MAX_CONSECUTIVE_FAILURES = 3

# 总开关：出现问题时可在运行时整体关闭，回退到"每次新建"的既有行为
_ENABLED = True


class WebViewPool:
    """``CodeWebViewer`` 实例复用池（进程级单例）。"""

    _instance: Optional["WebViewPool"] = None

    def __init__(self) -> None:
        self._idle: Dict[bool, List] = {True: [], False: []}
        self._host = None
        self._failures = 0
        self._stats: Dict[str, int] = {
            "acquire": 0,
            "hit": 0,
            "miss": 0,
            "release": 0,
            "rejected": 0,  # 归还时被判定不可复用
            "evicted": 0,  # 取出时被判定已失效 / 超配额销毁
        }

    @classmethod
    def get_instance(cls) -> "WebViewPool":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 开关与统计 ──────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return _ENABLED and self._failures < MAX_CONSECUTIVE_FAILURES

    @staticmethod
    def set_enabled(value: bool) -> None:
        """全局开关（供设置项 / 故障降级使用）。"""
        global _ENABLED
        _ENABLED = bool(value)

    @property
    def stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def size(self, light: bool = False) -> int:
        return len(self._idle.setdefault(bool(light), []))

    # ── 借出 / 归还 ─────────────────────────────────────────────────

    def acquire(self, light: bool = False):
        """从池里取一个可复用的 viewer；没有则返回 None（调用方新建）。"""
        if not self.enabled:
            return None
        self._stats["acquire"] += 1
        bucket = self._idle.setdefault(bool(light), [])
        while bucket:
            viewer = bucket.pop(0)
            if not self._is_usable(viewer):
                self._stats["evicted"] += 1
                self._safe_delete(viewer)
                continue
            self._failures = 0
            self._stats["hit"] += 1
            return viewer
        self._stats["miss"] += 1
        return None

    def release(self, viewer, light: bool = False) -> bool:
        """归还一个 viewer 到池。返回是否成功入池（失败则调用方负责销毁）。"""
        if viewer is None:
            return False
        if not self.enabled or not self._is_usable(viewer):
            self._stats["rejected"] += 1
            return False
        bucket = self._idle.setdefault(bool(light), [])
        try:
            host = self._ensure_host()
            if host is None:
                return False
            viewer.setParent(host)
            viewer.setUpdatesEnabled(False)
            # 复位为"干净实例"：清空文档与卡片相关状态，避免复用时残留旧内容
            viewer.reset_for_reuse()
        except RuntimeError:
            self._note_failure()
            self._stats["rejected"] += 1
            return False
        except Exception:
            # 兜底：任何非预期异常都不让池化影响主流程
            self._note_failure()
            self._stats["rejected"] += 1
            return False

        if len(bucket) >= MAX_IDLE_PER_BUCKET:
            oldest = bucket.pop(0)
            self._stats["evicted"] += 1
            self._safe_delete(oldest)
        bucket.append(viewer)
        self._stats["release"] += 1
        self._failures = 0
        return True

    def clear(self) -> None:
        """清空并销毁所有池中 viewer（退出 / 主题全局刷新等场景）。"""
        for bucket in self._idle.values():
            while bucket:
                self._safe_delete(bucket.pop(0))

    # ── 与 B4 强回收护栏的协同 ──────────────────────────────────────

    def pids(self) -> Set[int]:
        """池中 viewer 当前占用的 renderer PID 集合。

        B4 强回收在 kill 离屏 renderer 前会调用 ``_pid_still_in_use``。
        池中 viewer 虽然不属于任何卡片，但其 renderer 仍在用，
        必须被视为"在用"，否则会被 kill → 复用时出现白屏。
        """
        pids: Set[int] = set()
        for bucket in self._idle.values():
            for viewer in bucket:
                with contextlib.suppress(Exception):
                    pid = getattr(viewer, "_renderer_pid", 0) or 0
                    if pid > 0:
                        pids.add(pid)
        return pids

    # ── 内部 ────────────────────────────────────────────────────────

    def _ensure_host(self):
        """懒创建隐藏宿主：保持 native 资源存活，但绝不显示。"""
        if self._host is not None and self._is_alive(self._host):
            return self._host
        try:
            from PyQt5.QtWidgets import QWidget

            host = QWidget()
            # WA_DontShowOnScreen：窗口资源会被创建（native surface 保留），
            # 但不会映射到屏幕 —— 既不弹幽灵窗口，也不会因父链不可见
            # 导致 Chromium 弹出独立原生窗口。
            host.setAttribute(Qt.WA_DontShowOnScreen, True)
            host.setWindowFlags(Qt.Tool)
            host.resize(600, 400)  # 有效几何：避免 0 尺寸下 Chromium 视口塌陷
            self._host = host
            return host
        except Exception:
            self._note_failure()
            return None

    @staticmethod
    def _is_alive(widget) -> bool:
        if widget is None:
            return False
        if sip is not None:
            try:
                if sip.isdeleted(widget):
                    return False
            except Exception:
                return False
        return True

    def _is_usable(self, viewer) -> bool:
        """viewer 是否还能复用：未销毁、page 仍在、渲染上下文未丢失。"""
        if not self._is_alive(viewer):
            return False
        try:
            if viewer.page() is None:
                return False
            if getattr(viewer, "_context_lost", False):
                return False
        except RuntimeError:
            return False
        except Exception:
            return False
        return True

    @staticmethod
    def _safe_delete(viewer) -> None:
        with contextlib.suppress(Exception):
            viewer.setParent(None)
        with contextlib.suppress(Exception):
            viewer.deleteLater()

    def _note_failure(self) -> None:
        self._failures += 1
