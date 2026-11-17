# -*- coding: utf-8 -*-
"""I10 回归：finalize_stop 门面 timeout 透传。

主线程阻塞线收尾：backend/engines.base 门面默认值收紧为 400ms（原无参默认
落 executor 3000+1000ms，切历史会话主线程阻塞实测可达 4s）；后台路径显式
None 保持不限短时语义。executor 内部预算拆分逻辑不在本单范围（禁动）。

覆盖：
① backend.finalize_stop：默认透传 400 / 显式 None 透传 None / 显式值原样透传；
② BaseEngine.finalize_stop：默认 400 / 显式 None；
③ 源码守卫：main_widget 后台 finalize 路径必须显式 timeout_ms=None
   （防止未来重构吃掉显式参数、后台路径误吃 400 默认）。
"""

from unittest.mock import MagicMock, call
from types import SimpleNamespace


def _make_backend():
    """最小 ChatBackend（绕过 __init__，只注入 _chat_engine）。"""
    from app.core.conversation.backend import ChatBackend

    backend = ChatBackend.__new__(ChatBackend)
    engine = SimpleNamespace(finalize_stop=MagicMock(return_value=[]))
    backend._chat_engine = engine
    return backend, engine


def test_backend_default_passes_400():
    """门面无参调用 → engine 收到 timeout_ms=400（新默认，主线程安全预算）。"""
    backend, engine = _make_backend()
    assert backend.finalize_stop() == []
    engine.finalize_stop.assert_called_once_with(timeout_ms=400)


def test_backend_none_passthrough():
    """显式 timeout_ms=None → 原样透传（后台路径不限短时语义）。"""
    backend, engine = _make_backend()
    backend.finalize_stop(timeout_ms=None)
    engine.finalize_stop.assert_called_once_with(timeout_ms=None)


def test_backend_explicit_value_passthrough():
    """显式数值（既有调用点 main_widget 主线程热路径 400 等）→ 原样透传，向后兼容。"""
    backend, engine = _make_backend()
    backend.finalize_stop(timeout_ms=800)
    engine.finalize_stop.assert_called_once_with(timeout_ms=800)


def test_base_engine_default_and_none():
    """BaseEngine：默认 400 / 显式 None，均透传 conversation_executor。"""
    from app.core.engines.base import BaseEngine

    executor = SimpleNamespace(finalize_stop=MagicMock(return_value=[]))
    engine = BaseEngine(conversation_core=None, conversation_executor=executor)
    engine.finalize_stop()
    executor.finalize_stop.assert_called_once_with(timeout_ms=400)
    engine.finalize_stop(timeout_ms=None)
    assert executor.finalize_stop.call_args_list[-1] == call(timeout_ms=None)


def test_background_finalize_path_explicit_none():
    """源码守卫：后台 finalize 路径必须显式 timeout_ms=None。

    门面默认收紧后，后台线程（_launch_background_finalize）若被重构吃掉显式
    参数会误吃 400ms 默认 → 关窗时中断消息收集不完整。锁死显式 None。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "app" / "main_widget.py").read_text(
        encoding="utf-8"
    )
    assert "engine_ref.finalize_stop(timeout_ms=None)" in src, (
        "后台 finalize 必须显式 timeout_ms=None（= executor 历史默认 3000+1000），"
        "不得依赖门面默认值"
    )
    # 主线程热路径显式 400 的既有调用保持不动（向后兼容核对）
    assert "self.backend.finalize_stop(timeout_ms=400)" in src
