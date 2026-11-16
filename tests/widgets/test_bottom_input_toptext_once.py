# -*- coding: utf-8 -*-
"""bottom_input_area 重复 toPlainText 修复（#29）回归测试。

背景：SendableTextEdit._on_text_changed 每次触发 toPlainText() 全量快照
≥3 次（has_text / 历史比对 / placeholder 同步各一次）。敲键可忽略，粘贴
大 prompt 时单次 3×O(n)。修复：入口取一次 text 全函数复用；
_sync_placeholder_removals 改收可选 text 参数（默认 None 自取，其他
调用点兼容）。

覆盖：
1. 单次 _on_text_changed → toPlainText 恰好 1 次（改前 3 次）
2. placeholder 移除三重守卫回归：IME 组合不上报、程序化路径只校准基线、
   正常路径多重集差上报
3. 可选参数：不传 text 时自取一次
4. 历史比对复用 text：历史文本被修改 → 退出历史模式
"""

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.widgets.bottom_input_area import SendableTextEdit  # noqa: E402

class _Counter:
    """toPlainText 替身：计数调用次数，返回固定值。"""

    def __init__(self, value=""):
        self.n = 0
        self.value = value

    def __call__(self):
        self.n += 1
        return self.value

class _StubSendBtn:
    def __init__(self):
        self.calls = []

    def set_send_enabled(self, v):
        self.calls.append(v)

class _FakeSignal:
    """attachmentsRemoved 替身（object.__new__ 实例无 C++ 对象，不能连真信号）。"""

    def __init__(self):
        self.emitted = []

    def emit(self, payload):
        self.emitted.append(payload)

def _patch_fake_signal(monkeypatch):
    fake = _FakeSignal()
    monkeypatch.setattr(SendableTextEdit, "attachmentsRemoved", fake, raising=False)
    return fake

def _make_editor(counter):
    """最小状态集的 SendableTextEdit（跳过重构造，只喂 _on_text_changed 依赖）。"""
    ed = SendableTextEdit.__new__(SendableTextEdit)
    ed.toPlainText = counter  # 实例属性 shadow：绕开胶囊展开重写版
    ed.send_btn = _StubSendBtn()
    ed._is_stop_mode = False
    ed._initializing = False
    ed._adjust_calls = 0

    def _adjust():
        ed._adjust_calls += 1

    ed._adjust_height_to_content = _adjust
    ed._history_index = -1
    ed._history_list = []
    ed._setting_history_text = False
    ed._reset_calls = []

    def _reset(clear_attachments=False):
        ed._reset_calls.append(clear_attachments)

    ed._reset_history_mode = _reset
    ed._detail_syncs = 0

    def _detail():
        ed._detail_syncs += 1

    ed._schedule_detail_sync = _detail
    ed._sync_attachments_enabled = True
    ed._last_placeholder_names = []
    ed._syncing_attachments = False
    ed._ime_composing = False
    return ed

def test_single_toptext_call_per_text_changed(monkeypatch):
    """单次 _on_text_changed → toPlainText 恰好 1 次（改前 3 次）"""
    _patch_fake_signal(monkeypatch)
    c = _Counter("hello world")
    ed = _make_editor(c)
    ed._on_text_changed()
    assert c.n == 1, f"toPlainText 应只调 1 次，实际 {c.n}"
    assert ed.send_btn.calls == [True]
    assert ed._adjust_calls == 1

def test_placeholder_guard_ime_composing(monkeypatch):
    """守卫① IME 组合中：不上报删除、不校准快照（语义不变）"""
    fake = _patch_fake_signal(monkeypatch)
    ed = _make_editor(_Counter("plain text"))
    ed._last_placeholder_names = ["a.txt"]
    ed._ime_composing = True
    ed._on_text_changed()
    assert fake.emitted == []
    assert ed._last_placeholder_names == ["a.txt"], "IME 分支应提前 return，不动快照"

def test_placeholder_diff_emits_removed(monkeypatch):
    """正常路径：多重集差上报被删引用"""
    fake = _patch_fake_signal(monkeypatch)
    ed = _make_editor(_Counter("[[a.txt]] leftover"))
    ed._last_placeholder_names = ["a.txt", "b.txt"]
    ed._on_text_changed()
    assert fake.emitted == [["b.txt"]]
    assert ed._last_placeholder_names == ["a.txt"]

def test_placeholder_multiset_diff_same_name(monkeypatch):
    """两个同名占位符删一个：只报一个（多重集差，不是集合差）"""
    fake = _patch_fake_signal(monkeypatch)
    ed = _make_editor(_Counter("[[a.txt]]"))
    ed._last_placeholder_names = ["a.txt", "a.txt"]
    ed._on_text_changed()
    assert fake.emitted == [["a.txt"]]

def test_placeholder_programmatic_only_calibrates(monkeypatch):
    """守卫② 程序化路径（_setting_history_text）：只校准基线不上报"""
    fake = _patch_fake_signal(monkeypatch)
    ed = _make_editor(_Counter("restored text"))
    ed._last_placeholder_names = ["a.txt"]
    ed._setting_history_text = True
    ed._on_text_changed()
    assert fake.emitted == []
    assert ed._last_placeholder_names == []

def test_placeholder_disabled_short_circuit(monkeypatch):
    """守卫③ 总开关关闭：直接短路（计数不增长）"""
    fake = _patch_fake_signal(monkeypatch)
    c = _Counter("[[a.txt]]")
    ed = _make_editor(c)
    ed._sync_attachments_enabled = False
    ed._on_text_changed()
    assert fake.emitted == []
    assert c.n == 1  # 入口那一次；placeholder 同步被短路不再取

def test_optional_text_param_self_fetches(monkeypatch):
    """不传 text 直接调 _sync_placeholder_removals：自取一次（其他调用点兼容）"""
    fake = _patch_fake_signal(monkeypatch)
    c = _Counter("[[x.txt]]")
    ed = _make_editor(c)
    ed._sync_placeholder_removals()
    assert c.n == 1
    assert ed._last_placeholder_names == ["x.txt"]

def test_history_compare_reuses_text(monkeypatch):
    """历史比对复用 text：历史文本被修改 → 退出历史模式（clear_attachments=False）"""
    fake = _patch_fake_signal(monkeypatch)
    ed = _make_editor(_Counter("edited"))
    ed._history_index = 0
    ed._history_list = [{"text": "original"}]
    ed._on_text_changed()
    assert ed._reset_calls == [False]

def test_history_same_text_stays_in_mode(monkeypatch):
    """历史比对复用 text：内容一致不退出历史模式"""
    fake = _patch_fake_signal(monkeypatch)
    ed = _make_editor(_Counter("original"))
    ed._history_index = 0
    ed._history_list = [{"text": "original"}]
    ed._on_text_changed()
    assert ed._reset_calls == []
