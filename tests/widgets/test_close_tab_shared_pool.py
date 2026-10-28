# -*- coding: utf-8 -*-
"""关闭对话页不阻塞主线程（共享线程池契约）回归守卫

背景（2026-10-13 实测定位）：
「刚对话完关页卡 1 秒+」根因 = 标题生成任务（LLM HTTP 往返常 3-10s）
跑在窗口私有 QThreadPool 上：
  1. closeEvent 的 waitForDone(1000) 同步等满 1 秒（实测 1000.2ms，占同步段 94%）
  2. 池随窗口回收时析构再次同步等待任务跑完（实测按任务剩余时长阻塞）

修复 = 任务改跑应用级共享池（_get_gen_thread_pool），窗口不再持有池，
closeEvent 不再等待，晚归回调由 _is_destroyed 守卫丢弃。

本文件锁定三条契约：
1. closeEvent 不得含 waitForDone（防回退到同步等待）
2. 窗口构造的 _gen_thread_pool 与共享池同一实例；插件兼容读取面保留
3. 共享池懒建且全窗口单例（maxThreadCount=2 与原口径一致）
4. 晚归回调（关页后任务返回）由守卫静默丢弃，不触达已销毁 UI
"""
import ast
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _get_main_widget_src() -> str:
    return (_REPO_ROOT / "app" / "main_widget.py").read_text(encoding="utf-8")


def _get_class(name: str) -> ast.ClassDef:
    tree = ast.parse(_get_main_widget_src(), filename="main_widget.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"未找到类 {name}")


def _get_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"未找到方法 {cls.name}.{name}")


# ═══════════════════════════════════════════════════════════════
# 1. AST 守卫：closeEvent 不得含同步等待
# ═══════════════════════════════════════════════════════════════


class TestCloseEventNoSyncWait:
    def test_close_event_has_no_wait_for_done(self):
        """closeEvent 不得调用 waitForDone（同步等后台任务 = 关页卡顿主因回归）"""
        cls = _get_class("OpenAIChatToolWindow")
        close_event = _get_method(cls, "closeEvent")
        body_src = ast.unparse(close_event)
        assert "waitForDone" not in body_src, (
            "closeEvent 出现 waitForDone：关页会同步等待标题生成等后台任务（LLM HTTP "
            "往返常 3-10s），「刚对话完关页」卡顿将回归。后台任务应跑共享池并在"
            "回调侧用 _is_destroyed 守卫丢弃晚归结果。"
        )

    def test_close_event_has_no_gen_pool_wait(self):
        """closeEvent 不得访问 _gen_thread_pool 做等待类操作"""
        cls = _get_class("OpenAIChatToolWindow")
        close_event = _get_method(cls, "closeEvent")
        body_src = ast.unparse(close_event)
        # 允许注释提及（ast.unparse 不含注释），出现属性访问即视为等待路径嫌疑
        assert "_gen_thread_pool.wait" not in body_src
        assert "_gen_thread_pool.waitForDone" not in body_src


# ═══════════════════════════════════════════════════════════════
# 2. 模块级共享池：懒建 + 单例
# ═══════════════════════════════════════════════════════════════


class TestSharedPool:
    def test_get_gen_thread_pool_is_singleton(self):
        """_get_gen_thread_pool 两次调用返回同一实例（应用级单例）"""
        from app.main_widget import _get_gen_thread_pool

        p1 = _get_gen_thread_pool()
        p2 = _get_gen_thread_pool()
        assert p1 is p2

    def test_shared_pool_max_thread_count(self):
        """共享池 maxThreadCount=2（与原窗口私有池口径一致）"""
        from app.main_widget import _get_gen_thread_pool

        assert _get_gen_thread_pool().maxThreadCount() == 2

    def test_window_uses_shared_pool(self):
        """窗口构造不得自建 QThreadPool（AST：_gen_thread_pool 赋值右侧为共享池获取）"""
        cls = _get_class("OpenAIChatToolWindow")
        init = _get_method(cls, "__init__")
        assigns = [
            node
            for node in ast.walk(init)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Attribute) and t.attr == "_gen_thread_pool"
        ]
        assert assigns, "构造中未见 _gen_thread_pool 赋值（插件兼容读取面被移除？）"
        rhs_src = ast.unparse(assigns[0].value)
        assert "QThreadPool()" not in rhs_src, (
            "窗口仍自建 QThreadPool：私有池在关页时会双重等待"
            "（closeEvent waitForDone + 池析构等任务跑完），卡顿回归。"
        )
        assert "_get_gen_thread_pool" in rhs_src, "窗口应改用应用级共享池 _get_gen_thread_pool()"


# ═══════════════════════════════════════════════════════════════
# 3. 晚归回调守卫：关页后任务返回必须静默丢弃
# ═══════════════════════════════════════════════════════════════


class TestLateCallbackGuard:
    @pytest.mark.skipif(
        not pytest.importorskip("PySide6", reason="需要 PyQt5"),
        reason="需要 PyQt5",
    )
    def test_on_topic_summary_generated_destroyed_guard(self):
        """已销毁窗口收到晚归标题回调 → 守卫直接 return（不触达 UI 属性）"""
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        from app.main_widget import OpenAIChatToolWindow

        inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
        inst._is_destroyed = True
        # 裸实例访问其它属性会抛 RuntimeError（Qt 保护）；守卫在属性访问之前
        # return 即通过 —— 若守卫缺失，下方调用会抛 RuntimeError
        inst._on_topic_summary_generated({"topic_summary": "晚归标题"}, None)

    def test_thread_safe_callback_has_destroyed_guard_ast(self):
        """_maybe_generate_topic_summary 内的回调包装含 _is_destroyed 守卫"""
        cls = _get_class("OpenAIChatToolWindow")
        method = _get_method(cls, "_maybe_generate_topic_summary")
        body_src = ast.unparse(method)
        assert "_is_destroyed" in body_src, (
            "标题生成回调包装缺 _is_destroyed 守卫：任务在共享池上比窗口活得久，"
            "关页后回调对已销毁 QObject emit 会抛 RuntimeError（池线程内）。"
        )
        assert "RuntimeError" in body_src, "回调包装应捕获 emit 的 RuntimeError"
