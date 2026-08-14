# -*- coding: utf-8 -*-
"""Hook 配置存储层双轨制回归测试

覆盖设计规格 docs/superpowers/specs/2026-08-14-hook-config-redesign-design.md：
- 双轨制：插件 hook 写回源文件，系统 hook 保留覆盖层
- 覆盖式写入防重复（§2.1）
- id 对齐、幽灵清理、类级共享、热重载
"""
import json
import os
from pathlib import Path

import pytest

from app.core.hook_manager import HookManager


@pytest.fixture(autouse=True)
def _isolate_hook_states(monkeypatch, tmp_path):
    """隔离 hook_states.json 落盘路径，避免污染真实数据"""
    states_dir = tmp_path / "states"
    states_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        HookManager,
        "_get_hook_states_path",
        staticmethod(lambda: str(states_dir / "hook_states.json")),
    )
    # 重置类级共享状态，保证测试间隔离
    HookManager._shared_hook_states = {}
    HookManager._shared_hook_overrides = {}
    yield states_dir


def _make_hook_manager_with_file(tmp_path):
    """构造带临时 hooks.json 的 HookManager"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreUserMessage": [
                        {
                            "matcher": "",
                            "hooks": [{"id": "plugin_hook_1", "type": "command", "command": "echo hi"}],
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    hm = HookManager()
    count = hm.register_hooks_from_json(
        "test-plugin",
        str(tmp_path),
        json.loads(hooks_file.read_text(encoding="utf-8")),
        str(hooks_file),
    )
    assert count == 1
    return hm, hooks_file


# ──────────────────────────────────────────────
# Task 1: 类级共享（消除双实例快照竞态）
# ──────────────────────────────────────────────
def test_hook_states_shared_across_instances():
    """两个实例必须共享同一 _hook_states 快照（消除双实例覆盖竞态）"""
    a = HookManager()
    b = HookManager()
    a._hook_states["test_id"] = False
    assert b._hook_states.get("test_id") is False, "实例 B 必须看到实例 A 的修改"


# ──────────────────────────────────────────────
# Task 2: 非系统 hook toggle 写回源文件
# ──────────────────────────────────────────────
def test_plugin_hook_toggle_writes_source_file(tmp_path):
    """非系统 hook toggle → enabled 写回源文件，hook_states.json 不记录"""
    hm, hooks_file = _make_hook_manager_with_file(tmp_path)
    # 找到 hook 并 toggle
    hook = hm._hooks["PreUserMessage"][0].hooks[0]
    assert hook.is_system_plugin is False
    ok = hm.toggle_hook_by_id(hook.id, False)
    assert ok is True
    # 源文件必须写入 enabled: false
    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    entry = data["hooks"]["PreUserMessage"][0]["hooks"][0]
    assert entry.get("enabled") is False
    # hook_states.json 不得出现该 id
    states_fp = Path(HookManager._get_hook_states_path())
    if states_fp.exists():
        states = json.loads(states_fp.read_text(encoding="utf-8"))
        assert hook.id not in states


# ──────────────────────────────────────────────
# Task 3: 覆盖式写入防重复
# ──────────────────────────────────────────────
def test_edit_hook_does_not_duplicate_rule(tmp_path):
    """编辑已有 hook → 覆盖原条目，不新增重复 rule"""
    hm, hooks_file = _make_hook_manager_with_file(tmp_path)
    hook = hm._hooks["PreUserMessage"][0].hooks[0]
    # 编辑 command（覆盖）
    ok = hm.edit_hook_by_id(hook.id, {"command": "echo edited"})
    assert ok is True
    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    rules = data["hooks"]["PreUserMessage"]
    assert len(rules) == 1, "编辑不得新增 rule"
    entries = rules[0]["hooks"]
    assert len(entries) == 1, "编辑不得新增 hook 条目"
    assert entries[0]["command"] == "echo edited"
    assert entries[0]["id"] == hook.id


def test_edit_hook_move_event_merges_existing_matcher_rule(tmp_path):
    """hook 移动到目标事件：目标事件已有同 matcher rule → 合并，不新建"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreUserMessage": [
                        {"matcher": "tool:bash", "hooks": [{"id": "h1", "type": "command", "command": "echo a"}]}
                    ],
                    "PostUserMessage": [
                        {"matcher": "tool:bash", "hooks": [{"id": "h2", "type": "command", "command": "echo b"}]}
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    hm = HookManager()
    hm.register_hooks_from_json(
        "p", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file)
    )
    h1 = hm._hooks["PreUserMessage"][0].hooks[0]
    ok = hm.edit_hook_by_id(h1.id, {"event": "PostUserMessage", "matcher": "tool:bash"})
    assert ok is True
    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    post_rules = data["hooks"]["PostUserMessage"]
    # 目标事件应只有一个同 matcher rule，含两个 hook
    assert len(post_rules) == 1
    assert len(post_rules[0]["hooks"]) == 2


def test_edit_hook_not_found_returns_false(tmp_path):
    """源文件找不到目标 hook → 返回 False（失败可见），不静默"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(json.dumps({"hooks": {"PreUserMessage": []}}), encoding="utf-8")
    hm = HookManager()
    hm.register_hooks_from_json(
        "p", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file)
    )
    # 伪造一个内存中不存在于文件的 hook（动态注册）
    from app.core.hook_manager import Hook, HookMatchRule

    ghost = Hook(id="ghost_1", type="command", command="echo x", config_file=str(hooks_file))
    rule = HookMatchRule(matcher="", hooks=[ghost])
    hm._hooks.setdefault("PreUserMessage", []).append(rule)
    ok = hm._save_hook_to_file_by_id(ghost, {"command": "echo y"})
    assert ok is False, "源文件无此 hook 必须返回 False"
