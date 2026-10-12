# -*- coding: utf-8 -*-
"""hover 抖动真机诊断：鼠标划过 AI 回复时「滚动条不动内容动」的证据采集。

用法：uv run python tests/debug/diag_hover_jitter.py
启动真实应用后，把鼠标划过 AI 回复卡片复现抖动，然后看终端输出。

打点：
  1. 滚动条 value/maximum 每次 valueChanged（滚动条是否真在动）
  2. 卡片 _apply_viewer_height / heightChanged（Qt 侧卡片高度是否变）
  3. 贴底四通道触发（_maintain_bottom_anchor / _do_scroll_to_bottom /
     _ensure_at_bottom / 高度回调）
  4. 每条记录附光标位置，对照「划过 AI 回复」的时刻

判定：
  - 日志显示 value/max 全静止 + 无 heightChanged，而肉眼看到内容动
    → 实锤 WebEngine 合成层（Chromium hover 重绘/亚像素舍入），Qt 侧无关
  - 日志显示有 heightChanged 但无 valueChanged → 卡片高度变了但没人补偿
  - 日志显示 valueChanged → 滚动条其实在动（用户感知偏差需修正）
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from loguru import logger

logger.remove()
logger.add(sys.stdout, level="INFO")

# 复用主入口的渲染环境（WebEngine 必须的属性/环境变量）
from app.utils.render_env import apply_render_env, default_config_path  # noqa: E402

apply_render_env(default_config_path())

from PyQt5.QtGui import QCursor  # noqa: E402

_t0 = time.perf_counter()


def _log(tag: str, detail: str = "") -> None:
    pos = QCursor.pos()
    logger.info(
        f"[jitter] +{(time.perf_counter() - _t0) * 1000:8.1f}ms cursor=({pos.x()},{pos.y()}) {tag} {detail}"
    )


def _patch_main_widget() -> None:
    import app.main_widget as mw

    orig_height_changed = mw.OpenAIChatToolWindow._on_message_card_height_changed
    orig_maintain = mw.OpenAIChatToolWindow._maintain_bottom_anchor
    orig_do_scroll = mw.OpenAIChatToolWindow._do_scroll_to_bottom
    orig_ensure = mw.OpenAIChatToolWindow._ensure_at_bottom

    def height_changed(self, _height: int):
        sender = self.sender()
        name = type(sender).__name__ if sender is not None else "?"
        delta = getattr(sender, "_last_height_delta", None)
        sb = self.chat_scroll_area.verticalScrollBar()
        _log(
            "heightChanged",
            f"card={name} delta={delta} value={sb.value()} max={sb.maximum()}",
        )
        return orig_height_changed(self, _height)

    def maintain(self):
        sb = self.chat_scroll_area.verticalScrollBar()
        _log("maintain_anchor", f"value={sb.value()} max={sb.maximum()}")
        return orig_maintain(self)

    def do_scroll(self):
        sb = self.chat_scroll_area.verticalScrollBar()
        _log("do_scroll_to_bottom", f"value={sb.value()} max={sb.maximum()}")
        return orig_do_scroll(self)

    def ensure(self, retries: int = 8):
        sb = self.chat_scroll_area.verticalScrollBar()
        _log("ensure_at_bottom", f"retries={retries} value={sb.value()} max={sb.maximum()}")
        return orig_ensure(self, retries)

    mw.OpenAIChatToolWindow._on_message_card_height_changed = height_changed
    mw.OpenAIChatToolWindow._maintain_bottom_anchor = maintain
    mw.OpenAIChatToolWindow._do_scroll_to_bottom = do_scroll
    mw.OpenAIChatToolWindow._ensure_at_bottom = ensure


def _patch_message_card() -> None:
    import app.widgets.message_card as mc

    orig_apply = mc.MessageCard._apply_viewer_height
    orig_commit = mc.MessageCard._commit_viewer_height

    def apply(self, value):
        h = max(40, int(value))
        cur = getattr(self, "_last_applied_viewer_height", None)
        if h != cur:
            _log("apply_viewer_height", f"role={self.role} {cur} -> {h}")
        return orig_apply(self, value)

    def commit(self, height: int):
        _log("commit_viewer_height", f"role={self.role} h={height}")
        return orig_commit(self, height)

    mc.MessageCard._apply_viewer_height = apply
    mc.MessageCard._commit_viewer_height = commit


def _install_value_watch(win_holder: dict) -> None:
    """窗口就绪后挂滚动条 valueChanged 监听（延迟到 chat_scroll_area 存在）。"""

    from PyQt5.QtCore import QTimer

    def try_hook():
        win = win_holder.get("win")
        if win is None or not hasattr(win, "chat_scroll_area"):
            QTimer.singleShot(500, try_hook)
            return
        sb = win.chat_scroll_area.verticalScrollBar()

        def on_value(v):
            _log("valueChanged", f"value={v} max={sb.maximum()}")

        sb.valueChanged.connect(on_value)

        def on_range(mn, mx):
            _log("rangeChanged", f"max {mn} -> {mx}")

        sb.rangeChanged.connect(on_range)
        app = win.window().windowHandle()
        dpr = app.devicePixelRatio() if app is not None else -1
        _log("hook_installed", f"滚动条监听就绪 dpr={dpr:.3f}")

    QTimer.singleShot(500, try_hook)


def main() -> None:
    _patch_main_widget()
    _patch_message_card()

    import app.main_widget as mw

    orig_init = mw.OpenAIChatToolWindow.__init__

    def init(self, *a, **kw):
        orig_init(self, *a, **kw)
        _install_value_watch({"win": self})

    mw.OpenAIChatToolWindow.__init__ = init

    _log("diag_start", "诊断钩子已装，复现抖动后 Ctrl+C 退出")
    import main as entry

    entry.main()


if __name__ == "__main__":
    main()
