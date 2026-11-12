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
    def test_commit_dead_window_returns_false(self):
        """关页后晚归标题提交 → window_id 解析失败即静默返回，不触达 UI。"""
        from app.plugins.capabilities.providers import title_core

        original = title_core._resolve_window_by_id
        title_core._resolve_window_by_id = lambda wid: None
        try:
            commit = title_core._session_title_commit(object())
            assert commit("gone", "晚归标题") is False
        finally:
            title_core._resolve_window_by_id = original

    def test_plugin_result_checks_session_id_before_commit(self):
        """插件提交前复查 session_id：切会话 / 关页的晚归结果被丢弃。"""
        import importlib.util

        path = _REPO_ROOT / "plugins" / "system-title-generator" / "ui" / "generator.py"
        spec = importlib.util.spec_from_file_location("_tg_late_guard", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        committed = []
        pool = {
            "session.read": lambda window_id="": {"session_id": "other-session"},
            "session.title.commit": lambda wid, title: committed.append((wid, title)),
        }
        ctrl = module._TitleController(pool)
        ctrl._on_result("w-1", "s-1", "晚归标题")
        assert committed == []
