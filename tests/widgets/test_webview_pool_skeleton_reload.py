# -*- coding: utf-8 -*-
"""回归测试：WebView 复用池取用侧必须重载骨架（复用卡片空白修复）

背景（用户现象：对话轮次变多后消息卡片内部全部空白）：
------------------------------------------------
B4 批次回收把 ``CodeWebViewer`` 归还复用池（``webview_pool.WebViewPool``）时，
``reset_for_reuse()`` 用 ``setHtml("", about:blank)`` 清空了骨架 HTML 与全部 JS，
但存在两个缺陷：

1. ``_is_js_ready`` 残留 ``True``（就绪态未随页面清空而失效）；
2. 取用侧（``MessageCard.ensure_rendered`` 的 pooled 分支）只做
   ``setParent`` + ``setUpdatesEnabled(True)``，**不重载骨架**。

后果：``set_content`` 误判 JS 可用，``runJavaScript("updateContent(...)")``
打在 about:blank 空页上（函数不存在，静默失败），且 ``contentReady``
（骨架 JS 的 ``pywebview_ready``）永不再触发，无任何兜底 → 卡片永久空白。

池子只有在 B4 虚拟滚动批次回收（渲染配额超限）后才进账 viewer，
因此现象与"对话轮次多了以后"强相关；且池是进程级全局的，
新开标签页的第一条回复也会取到被污染的 viewer。

修复：
1. 取用侧：重置 ``_is_js_ready`` 并调用 ``_load_skeleton()``（与新建等价）；
   ``set_content`` 因 JS 未就绪 defer，由 ``_on_js_ready`` 统一补渲。
2. ``reset_for_reuse()``：归还时同步 ``_is_js_ready = False``（防御）。

本测试验证（mock viewer，不依赖真实 Chromium）：
1. 从池中取出的 viewer 必须经历一次骨架重载，且 JS 就绪态被复位；
2. 归还侧 ``reset_for_reuse`` 会把残留的 ``_is_js_ready`` 清掉。

运行：
    python -m pytest tests/widgets/test_webview_pool_skeleton_reload.py -v
"""

import sys
from unittest.mock import MagicMock, patch

from PyQt5.QtWidgets import QApplication, QWidget

from app.widgets.message_card import MessageCard


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _FakePooledViewer(QWidget):
    """模拟"被归还过一次"的池中 viewer。

    - ``_is_js_ready`` 初始为 True：模拟 release 时残留的就绪态（污染源）；
    - ``_load_skeleton`` 被替换为计数桩：断言取用侧确实重载了骨架；
    - 其余未知属性（信号 / page 等）回退 MagicMock，吸收交互。
    """

    def __init__(self):
        super().__init__()
        self._is_js_ready = True
        self._light_skeleton = False
        self.skeleton_reload_count = 0

    def _load_skeleton(self):
        self.skeleton_reload_count += 1
        self._is_js_ready = False

    def _install_dialog_filter(self):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return MagicMock()


def test_pooled_viewer_must_reload_skeleton_on_acquire():
    """从池取出的 viewer 必须重载骨架并复位 JS 就绪态（核心回归）。"""
    _ensure_qapp()

    from app.widgets.webview_pool import WebViewPool

    WebViewPool.set_enabled(True)
    pool = WebViewPool.get_instance()
    pool.clear()

    fake = _FakePooledViewer()
    assert fake._is_js_ready is True
    # 归还进池（真实 release 链：_is_usable → reset_for_reuse → 入桶）
    assert pool.release(fake, light=False) is True

    card = MessageCard(role="assistant")
    card._pending_content = "你好"

    with (
        patch("app.widgets.message_card._qt_renderer_enabled", return_value=False),
        patch.object(MessageCard, "_is_effectively_visible", lambda self: True),
    ):
        card.ensure_rendered()

    try:
        assert card.viewer is fake, "应命中池中 viewer（复用路径）"
        assert card._lazy_rendered is True
        assert fake.skeleton_reload_count == 1, (
            "取用侧必须调用 _load_skeleton() 重载骨架（release 时 setHtml('') 已清空页面）"
        )
        assert fake._is_js_ready is False, (
            "复用时 JS 就绪态必须复位，否则 set_content 会把 runJavaScript "
            "打在空页上静默失败 → 卡片永久空白"
        )
    finally:
        pool.clear()


def test_reset_for_reuse_clears_stale_js_ready():
    """归还侧防御：reset_for_reuse 必须清掉残留的 _is_js_ready。"""
    _ensure_qapp()

    fake = _FakePooledViewer()
    # 模拟一张正常工作过的 viewer：JS 就绪 + 有稳定渲染状态
    fake._is_js_ready = True
    fake._stable_html = "<p>x</p>"
    fake._needs_full_render = False

    fake.reset_for_reuse()

    assert fake._is_js_ready is False, "页面已被 setHtml('') 清空，就绪态不得残留 True"
    assert fake._needs_full_render is True
    assert fake._stable_html == ""
