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

3. ``setFixedHeight`` 钉死的高度（由 `_commit_viewer_height` 设置，长消息可达
   数千 px）在归还时未清 → 复用方在骨架就绪前顶着上一张消息的高度，
   表现为"巨高空白卡片"（而不是塌成最小高度）。

修复：
1. 取用侧：重置 ``_is_js_ready`` 并调用 ``_load_skeleton()``（与新建等价）；
   ``set_content`` 因 JS 未就绪 defer，由 ``_on_js_ready`` 统一补渲。
2. ``reset_for_reuse()``：归还时同步 ``_is_js_ready = False``（防御）。
3. 高度复位：归还侧与取用侧都把 viewer 高度复位到最小高度 40，
   由新内容首次 ``reportHeight`` 重新收敛。

本测试验证（mock viewer，不依赖真实 Chromium）：
1. 从池中取出的 viewer 必须经历一次骨架重载，且 JS 就绪态被复位；
2. 归还侧 ``reset_for_reuse`` 会把残留的 ``_is_js_ready`` 清掉；
3. 复用的 viewer 不得残留上一张卡片钉死的固定高度。

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

    - ``_is_js_ready`` 初始为 True：骨架已加载（release 后应保持 True）；
    - ``_load_skeleton`` 为计数桩：用于断言"骨架缺失时才重载、否则不重载"；
      重载后把 ``_is_js_ready`` 置 False（模拟等待 contentReady 的真实行为）；
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


def test_pooled_viewer_keeps_skeleton_and_clears_content():
    """复用不得重建文档：骨架保留 + 内容清空 + 高度复位（内存回归的核心）。

    2026-09-09：曾经在取用侧无条件 ``_load_skeleton()``（因为 release 用
    setHtml("") 把页面清了）。加载消息数很多的会话时批次反复卸载/重建，
    每次都新建一份 ~54KB 文档并重跑骨架 JS（setInterval/ResizeObserver/
    事件监听全套重注册），内存与 CPU 明显上涨。现改为"保留骨架、原地清内容"。
    """
    _ensure_qapp()

    from app.widgets.webview_pool import WebViewPool

    WebViewPool.set_enabled(True)
    pool = WebViewPool.get_instance()
    pool.clear()

    fake = _FakePooledViewer()
    assert fake._is_js_ready is True
    # 模拟上一张卡片的残留：钉死高度 + 旧内容
    fake.setMinimumHeight(40)
    fake.setFixedHeight(1600)
    fake._markdown_text = "上一张卡片的旧内容"
    fake._render_deferred = True
    # 归还进池（真实 release 链：_is_usable → reset_for_reuse → 入桶）
    assert pool.release(fake, light=False) is True
    # 骨架保留：JS 仍就绪，内容态被清空（否则复用后会拿旧文本多渲染一次）
    assert fake._is_js_ready is True, "release 必须保留骨架（不应 setHtml('') 清页）"
    assert fake._markdown_text == "", "旧内容必须清空，否则复用后 _on_js_ready 会多渲染一次"
    assert fake._render_deferred is False

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
        assert fake.skeleton_reload_count == 0, (
            "骨架仍在时不该重载（每次重载 = 新建文档 + 重跑骨架 JS，是内存回归根因）"
        )
        # 高度：上一张卡片钉死的 1600px 不得残留（用户现象：巨高空白卡片）
        assert fake.maximumHeight() == 40, (
            f"复用时必须复位高度，陈旧固定高度会撑出巨大空白卡片（当前 {fake.maximumHeight()}）"
        )
    finally:
        pool.clear()


def test_pooled_viewer_reloads_skeleton_when_missing():
    """骨架缺失（_is_js_ready=False）时必须补一次重载，否则 runJavaScript 打在空页上"""
    _ensure_qapp()

    from app.widgets.webview_pool import WebViewPool

    WebViewPool.set_enabled(True)
    pool = WebViewPool.get_instance()
    pool.clear()

    fake = _FakePooledViewer()
    fake._is_js_ready = False  # 骨架未就绪
    assert pool.release(fake, light=False) is True

    card = MessageCard(role="assistant")
    card._pending_content = "你好"

    with (
        patch("app.widgets.message_card._qt_renderer_enabled", return_value=False),
        patch.object(MessageCard, "_is_effectively_visible", lambda self: True),
    ):
        card.ensure_rendered()

    try:
        assert card.viewer is fake
        assert fake.skeleton_reload_count == 1, "骨架缺失时必须补一次 _load_skeleton()"
    finally:
        pool.clear()


def test_reset_for_reuse_keeps_skeleton_and_resets_state():
    """归还侧：保留骨架（JS 就绪态不变）+ 复位内容与高度。"""
    _ensure_qapp()

    fake = _FakePooledViewer()
    # 模拟一张正常工作过的 viewer：JS 就绪 + 有稳定渲染状态 + 已钉死高度
    fake._is_js_ready = True
    fake._stable_html = "<p>x</p>"
    fake._needs_full_render = False
    fake._markdown_text = "旧内容"
    fake.setFixedHeight(1600)

    fake.reset_for_reuse()

    # 骨架保留：不再 setHtml('') 清页
    assert fake._is_js_ready is True, "骨架保留时就绪态不得被清（否则复用要重建文档）"
    assert fake._needs_full_render is True
    assert fake._stable_html == ""
    assert fake._markdown_text == "", "旧内容必须清空（防复用后多一次全量渲染）"
    assert fake.maximumHeight() == 40, "归还时必须复位高度，否则复用时残留上一张卡片的巨高"
    assert fake.skeleton_reload_count == 0, "归还侧不得重载骨架"
