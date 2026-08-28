# -*- coding: utf-8 -*-
"""
性能回归测试（流式 + resize 卡顿）：消息卡片在 resize preview 期间停止 DOM 注入与级联 relayout。

(a) 对应瓶颈：大模型流式输出（append_chunk 每 ~80ms 一帧）期间，用户拖拽窗口边缘
    resize 时，消息卡片（CodeWebViewer）的 Chromium 浏览器持续累积 DOM 节点 +
    setFixedHeight 触发 Chromium 整页 relayout → ResizeObserver → reportHeight
    IPC → Python _stream_height_timer 80ms 防抖 → 又一轮 setFixedHeight 的循环。
    视觉上"resize 时内容很卡，要等很久才能响应新宽度"。

    修复三处：
      1. CodeWebViewer._append_text_incremental 在 isVisible()==False 时早返回
         （preview / 对话框 / 切 tab 隐藏期间停止 Chromium DOM 写入）。
      2. MessageCard._apply_viewer_height 在 self._resize_preview_mode=True 时
         只写 _pending_viewer_height 字段，不再调 viewer.setFixedHeight，也不再
         发 auto-scroll 的 runJavaScript——切断 setFixedHeight ↔ relayout
         ↔ reportHeight 的级联循环。
      3. MessageCard.set_resize_preview_mode(False) 退出 preview 时，把累积的
         _pending_viewer_height 一次性 setFixedHeight + 触发一次 reportHeight()，
         让 Chromium 首帧 paint 就按目标高度布局，避免二次 paint 抖动。

(b) 本测试未修改任何业务代码，仅静态分析：用 pathlib 读取 app/widgets/message_card.py
    源码文本 + re 匹配，不 import PySide6、不实例化任何 GUI 对象。

(c) 环境要求：pytest>=7 / Python3 / 对 app/ 源码有读权限 / 无需显示器 /
    无新三方依赖 / 跨平台 Windows 优先。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "app" / "widgets" / "message_card.py"


@pytest.fixture(scope="module")
def src_text() -> str:
    return SRC.read_text(encoding="utf-8")


def test_perf_append_text_incremental_skips_when_hidden(src_text: str):
    """流式增量 DOM 注入（_append_text_incremental）在 viewer 不可见时早返回。

    不可见期间 Chromium 停止累积 DOM，preview 退出后首帧 paint 不需重排整页。
    """
    # 取 CodeWebViewer._append_text_incremental 函数体
    start = src_text.find("def _append_text_incremental(self, text: str):")
    assert start != -1, "未找到 CodeWebViewer._append_text_incremental 定义"
    end = src_text.find("\n    def ", start + 1)
    body = src_text[start:end]

    # 必须有 isVisible() 守卫
    assert "self.isVisible()" in body, (
        "_append_text_incremental 必须含 isVisible() 守卫，否则 preview / 对话框 / "
        "切 tab 隐藏期间 Chromium 会持续累积 DOM"
    )
    # 守卫必须早返回
    assert "if not self.isVisible():" in body or "if not self._is_js_ready or not self.isVisible():" in body, (
        "_append_text_incremental 必须以 if not self.isVisible(): return 形式早返回"
    )


def test_perf_apply_viewer_height_skips_during_preview(src_text: str):
    """MessageCard._apply_viewer_height 在 _resize_preview_mode=True 时不调 setFixedHeight。

    关键防线：preview 期间 setFixedHeight 会触发 Chromium 整页 relayout →
    ResizeObserver → reportHeight → 又一轮 _stream_height_timer → setFixedHeight
    的循环，是流式 + resize 卡顿的根因之一。
    """
    start = src_text.find("def _apply_viewer_height(self, value):")
    assert start != -1, "未找到 MessageCard._apply_viewer_height 定义"
    end = src_text.find("\n    def ", start + 1)
    body = src_text[start:end]

    # 必须有 preview 守卫
    assert "if self._resize_preview_mode:" in body, (
        "_apply_viewer_height 必须含 _resize_preview_mode 守卫，否则 preview 期间 "
        "setFixedHeight → Chromium relayout → ResizeObserver → reportHeight 循环不断"
    )
    # 必须把目标高度写入累积字段而不是真调 setFixedHeight
    assert "_pending_viewer_height" in body, (
        "_apply_viewer_height 在 preview 模式必须把目标高度累积到 _pending_viewer_height，"
        "由 set_resize_preview_mode(False) 退出时一次性应用"
    )


def test_perf_preview_exit_applies_pending_height(src_text: str):
    """set_resize_preview_mode(False) 必须把累积的 _pending_viewer_height 一次性应用到 viewer。

    单一 setFixedHeight + reportHeight 触发让 Chromium 在首次 paint 时就按目标高度
    布局，避免首帧按旧高度重排再触发二次 paint 抖动。
    """
    start = src_text.find("def set_resize_preview_mode(self, enabled: bool):")
    assert start != -1, "未找到 MessageCard.set_resize_preview_mode 定义"
    end = src_text.find("\n    def ", start + 1)
    body = src_text[start:end]

    # 必须读取累积字段
    assert "getattr(self, \"_pending_viewer_height\", None)" in body, (
        "set_resize_preview_mode(False) 必须读取 _pending_viewer_height 累积值"
    )
    # 必须一次性 setFixedHeight
    assert "self.viewer.setFixedHeight(pending_h)" in body, (
        "set_resize_preview_mode(False) 必须一次性 setFixedHeight 应用累积高度"
    )
    # 必须强制一次高度上报
    assert "reportHeight()" in body, (
        "set_resize_preview_mode(False) 必须触发一次 reportHeight() 让 Chromium "
        "首帧 paint 按目标高度布局，避免二次 paint 抖动"
    )


def test_perf_pending_viewer_height_initialized(src_text: str):
    """_pending_viewer_height 必须在 MessageCard.__init__ 初始化（避免 getattr None 但写前缺字段）。"""
    # 找 MessageCard.__init__ 范围（_resize_preview_mode 字段附近）
    init_idx = src_text.find("self._resize_preview_mode = False")
    assert init_idx != -1, "未找到 MessageCard._resize_preview_mode 初始化"

    # 检查 _pending_viewer_height 在其前后 30 行内被初始化
    near = src_text[init_idx : init_idx + 1500]
    assert "self._pending_viewer_height" in near, (
        "_pending_viewer_height 必须在 MessageCard.__init__ 初始化（紧邻 _resize_preview_mode）"
    )
    assert ": Optional[int] = None" in near or "= None" in near, (
        "_pending_viewer_height 必须初始化为 None"
    )


def test_perf_cleanup_resets_pending_height(src_text: str):
    """MessageCard.cleanup 必须清理 _pending_viewer_height，防止 deleteLater 后 stale 引用。"""
    # MessageCard.cleanup 含"停止所有定时器"+ _anim_timer/_height_anim/_elapsed_timer 三定时器，
    # 与 CodeWebViewer.cleanup / PlainTextViewer.cleanup 区别开
    cleanup_idx = src_text.find(
        "self._anim_timer,\n            self._height_anim,\n            self._elapsed_timer,"
    )
    assert cleanup_idx != -1, "未找到 MessageCard.cleanup（_anim_timer 三件套）"

    # 检查 _pending_viewer_height 在 cleanup 中被重置
    cleanup_body = src_text[cleanup_idx : cleanup_idx + 8000]
    assert "self._pending_viewer_height = None" in cleanup_body, (
        "MessageCard.cleanup 必须重置 _pending_viewer_height，防止 deleteLater 后 stale 引用"
    )