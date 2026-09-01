# -*- coding: utf-8 -*-
"""test_assistant_card.py — 单列主页面冒烟测试。"""

import importlib.util
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("qfluentwidgets")

from PyQt5.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

_ROOT = Path(__file__).resolve().parents[3]
_UI_DIR = _ROOT / "plugins" / "assistant_hub" / "ui"

_pkg_name = "ui_plugin_assistant_hub"
if _pkg_name not in sys.modules:
    pkg = types.ModuleType(_pkg_name)
    pkg.__path__ = [str(_UI_DIR)]
    sys.modules[_pkg_name] = pkg

if "assistant_hub_manager" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "assistant_hub_manager", str(_ROOT / "plugins" / "assistant_hub" / "assistant_manager.py")
    )
    _mgr_mod = importlib.util.module_from_spec(_spec)
    sys.modules["assistant_hub_manager"] = _mgr_mod
    _spec.loader.exec_module(_mgr_mod)


def _load(name: str, file: str):
    full = f"{_pkg_name}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, str(_UI_DIR / file))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


for _f in ("assistant_avatar", "arc_stack", "overlays", "sections", "rename_dialog"):
    _load(_f, f"{_f}.py")
card_mod = _load("assistant_card", "assistant_card.py")


def test_card_construct_and_bind(tmp_path, monkeypatch):
    """构造主页面：创建助手 → 绑定编辑器 → 切换助手。"""
    mgr_mod = sys.modules["assistant_hub_manager"]
    mgr_mod.AssistantManager.reset_instance()
    mgr = mgr_mod.AssistantManager.get_instance(root_dir=str(tmp_path / "hub"))
    a = mgr.create("小狐")

    card = card_mod.AssistantCardWidget()
    # 空库自动 seed 3 个预设助手（build/hanako/pure）+ 测试新建 1 个
    assert len(card._stack._cards) == 4
    # 默认绑定主助手 build（seed 时 build 设为主助手，排序居首）
    assert card._active_aid == "build"
    assert card._name_label.text() == "Build"

    # 新建第二个 → 切换
    b = mgr.create("二号")
    card._reload_all(select_aid=b.id)
    assert card._active_aid == b.id
    assert card._name_label.text() == "二号"

    # 删除：删 a 后当前助手不变（仍绑 b）
    mgr.delete(a.id)
    card._reload_all(select_aid=b.id)
    assert card._active_aid == b.id
    assert mgr.has(b.id)


def test_card_persona_change(tmp_path):
    mgr_mod = sys.modules["assistant_hub_manager"]
    mgr_mod.AssistantManager.reset_instance()
    mgr = mgr_mod.AssistantManager.get_instance(root_dir=str(tmp_path / "hub2"))
    a = mgr.create("人格测试")
    card = card_mod.AssistantCardWidget()
    card._active_aid = a.id
    card._on_persona_change("hanako")
    assert mgr.get(a.id).yuan == "hanako"
    card._on_persona_change("none")
    assert mgr.get(a.id).yuan == "none"


def test_main_thread_call_dispatches_from_daemon_thread(tmp_path):
    """后台线程投递的 UI 回调必须真的落到主线程执行。

    QTimer.singleShot(0, fn) 在纯 Python daemon 线程里**永不触发** —— Qt 在
    调用方线程创建 QSingleShotTimer，而该线程没有 Qt 事件循环。后果：
    _dream_done() 不执行 → _dream_running 恒为 True → UI 卡在"Dream 整理中…"，
    且之后每次点击 Dream 都被 `_on_dream_run` 开头的 `_dream_running` 守卫吞掉。
    """
    mgr_mod = sys.modules["assistant_hub_manager"]
    mgr_mod.AssistantManager.reset_instance()
    mgr_mod.AssistantManager.get_instance(root_dir=str(tmp_path / "hub3"))
    card = card_mod.AssistantCardWidget()

    seen = {}
    main_tid = threading.get_ident()

    def _cb():
        seen["tid"] = threading.get_ident()

    t = threading.Thread(target=lambda: card._main_thread_call.emit(_cb), daemon=True)
    t.start()
    t.join()

    deadline = time.time() + 5
    while "tid" not in seen and time.time() < deadline:
        _APP.processEvents()
        time.sleep(0.01)

    assert seen.get("tid") == main_tid, "后台线程投递的回调未在主线程执行（UI 会永久卡住）"


def test_main_thread_call_swallows_callback_exception(tmp_path):
    """回调抛异常不能外溢到 Qt 事件循环（否则会打断主线程）。"""
    mgr_mod = sys.modules["assistant_hub_manager"]
    mgr_mod.AssistantManager.reset_instance()
    mgr_mod.AssistantManager.get_instance(root_dir=str(tmp_path / "hub4"))
    card = card_mod.AssistantCardWidget()
    # 直接调用槽函数（同步），异常应被吞掉
    card._run_on_main_thread(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    card._run_on_main_thread(None)  # 非 callable 也不炸


def test_worker_threads_do_not_use_single_shot():
    """守护：后台线程路径不得再出现 QTimer.singleShot（它不会触发）。

    文件里仅允许 1 处 —— `_reload_all` 的 `_do_bind`（主线程，用于延后一帧布局）。
    任何后台线程（Dream / 经验反思）里的 UI 回调都必须走 `_main_thread_call.emit()`。
    """
    import ast

    src = (_UI_DIR / "assistant_card.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 只统计真实调用节点（注释/文档里提到 QTimer.singleShot 不算）
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "singleShot"
    ]
    assert len(calls) == 1, (
        f"assistant_card.py 有 {len(calls)} 处 QTimer.singleShot 调用（应只有 1 处主线程用法）；"
        "后台线程里 QTimer.singleShot 永不触发，请改用 self._main_thread_call.emit(...)"
    )
