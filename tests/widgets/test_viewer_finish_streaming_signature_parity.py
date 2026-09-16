# -*- coding: utf-8 -*-
"""viewer.finish_streaming 签名对齐回归（P0 崩溃）

现场：用户点推荐问题即崩 ——
    _append_user_message → MessageCard.finish_streaming
    → self.viewer.finish_streaming(keep_dock=..., immediate=...)
    → TypeError: PlainTextViewer.finish_streaming() got an unexpected
       keyword argument 'immediate'

根因：兄弟团队 T11 错峰链给 CodeWebViewer.finish_streaming 加了 immediate
参数并在 MessageCard 无条件透传，但 viewer 是鸭子类型多实现
（CodeWebViewer / PlainTextViewer / MarkdownBlockViewer + 池化复用），
漏改任一个即在该实现被选中时崩（PlainTextViewer=用户卡片/Qt 降级路径，
MarkdownBlockViewer=纯 Qt 灰度通道）。

守护方式：直接以 MessageCard 的真实调用形式（两个关键字参数全给）对每个
viewer 实现类做一次签名绑定检查（inspect.Signature.bind，不构造 Qt 对象、
不触发渲染），漏参数立即红。
"""

import inspect
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.widgets.markdown_block_viewer import MarkdownBlockViewer  # noqa: E402
from app.widgets.message_card import CodeWebViewer, MessageCard, PlainTextViewer  # noqa: E402

# MessageCard.finish_streaming 实际透传给 viewer 的参数名（缺一即 TypeError）
CALLER_KWARGS = ("keep_dock", "immediate")

VIEWER_CLASSES = [CodeWebViewer, PlainTextViewer, MarkdownBlockViewer]


@pytest.mark.parametrize("cls", VIEWER_CLASSES, ids=lambda c: c.__name__)
def test_viewer_signature_binds(cls):
    """精确校验：bind(self, keep_dock=…, immediate=…) 必须成功（崩溃的最小复现）

    用 Signature.bind 而非真实调用：不构造 Qt 对象、不触发渲染，
    且不依赖 self 的具体类型（传 None 仅占位）。
    """
    sig = inspect.signature(cls.finish_streaming)
    try:
        sig.bind(None, **{k: True for k in CALLER_KWARGS})
    except TypeError as e:
        pytest.fail(
            f"{cls.__name__}.finish_streaming 不接受 MessageCard 的调用形式{CALLER_KWARGS} → 用户实机 TypeError: {e}"
        )


def test_caller_passes_both_kwargs():
    """反向锁：调用点确实无条件透传两个参数（改动透传方式时本测试提醒更新清单）"""
    src = inspect.getsource(MessageCard.finish_streaming)
    assert "finish_streaming(keep_dock=" in src and "immediate=immediate)" in src, (
        "MessageCard.finish_streaming 的 viewer 透传形式已变，请同步更新 CALLER_KWARGS 与 viewer 实现签名清单"
    )
