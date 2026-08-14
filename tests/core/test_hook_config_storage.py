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
    HookManager._shared_hooks = {}
    HookManager._shared_skill_to_hooks = {}
    HookManager._shared_config_watchers = {}
    HookManager._shared_registered_functions = {}
    HookManager._shared_cwd_resolve_cache = {}
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


# ──────────────────────────────────────────────
# Task 4: 非系统 hook edit 写回源文件
# ──────────────────────────────────────────────
def test_plugin_hook_edit_writes_source_not_overrides(tmp_path):
    """非系统 hook 编辑 → 写回源文件，_hook_overrides 不记录"""
    hm, hooks_file = _make_hook_manager_with_file(tmp_path)
    hook = hm._hooks["PreUserMessage"][0].hooks[0]
    ok = hm.edit_hook_by_id(hook.id, {"command": "echo edited", "statusMessage": "working"})
    assert ok is True
    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    entry = data["hooks"]["PreUserMessage"][0]["hooks"][0]
    assert entry["command"] == "echo edited"
    assert entry["statusMessage"] == "working"
    # 非系统 hook 不得写入 overrides
    assert hook.id not in hm._hook_overrides


# ──────────────────────────────────────────────
# Task 5: _apply_hook_state/_overrides 仅系统 hook
# ──────────────────────────────────────────────
def test_apply_state_only_for_system_hook(tmp_path):
    """注册时：非系统 hook 不应用 _hook_states 覆盖（状态以源文件为准）"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreUserMessage": [
                        {"matcher": "", "hooks": [{"id": "plugin_1", "type": "command", "command": "echo a"}]}
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    hm = HookManager()
    # 预置覆盖层状态（模拟旧数据残留）
    hm._hook_states["plugin_1"] = False
    hm.register_hooks_from_json(
        "p", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file)
    )
    hook = hm._hooks["PreUserMessage"][0].hooks[0]
    # 源文件无 enabled → 默认 True，且非系统 hook 不被覆盖层强制改 False
    assert hook.enabled is True


# ──────────────────────────────────────────────
# Task 6: _persist_hook_ids_to_file id 对齐
# ──────────────────────────────────────────────
def test_persist_ids_aligned_with_file_order(tmp_path):
    """id 分配必须按文件顺序对齐，不因内存注册顺序错位"""
    hooks_file = tmp_path / "hooks.json"
    # 文件里两个 hook 都无 id，且命令不同
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreUserMessage": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"type": "command", "command": "echo first"},
                                {"type": "command", "command": "echo second"},
                            ],
                        }
                    ]
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
    # 注册后源文件应有两个不同 id（persist 已写回）
    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    entries = data["hooks"]["PreUserMessage"][0]["hooks"]
    assert len(entries) == 2
    id1, id2 = entries[0]["id"], entries[1]["id"]
    assert id1 and id2 and id1 != id2
    # 再次注册（模拟重启）：id 必须保持稳定不漂移
    hm2 = HookManager()
    hm2.register_hooks_from_json(
        "p", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file)
    )
    hook_map = {h.command: h.id for h in hm2._hooks["PreUserMessage"][0].hooks}
    assert hook_map["echo first"] == id1
    assert hook_map["echo second"] == id2


def test_persist_ids_mixed_existing_and_missing(tmp_path):
    """混合场景：文件里部分 hook 已有 id、部分没有 → 无 id 的按文件顺序补齐，已有的不被改"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreUserMessage": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"id": "fixed_id_a", "type": "command", "command": "echo a"},
                                {"type": "command", "command": "echo b"},
                                {"type": "command", "command": "echo c"},
                            ],
                        }
                    ]
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
    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    entries = data["hooks"]["PreUserMessage"][0]["hooks"]
    assert entries[0]["id"] == "fixed_id_a", "已有 id 的 hook 不得被改写"
    id_b, id_c = entries[1]["id"], entries[2]["id"]
    assert id_b and id_c, "无 id 的 hook 必须补齐"
    assert id_b != id_c and id_b != "fixed_id_a" and id_c != "fixed_id_a"
    # 重启后 id 稳定
    hm2 = HookManager()
    hm2.register_hooks_from_json(
        "p", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file)
    )
    hook_map = {h.command: h.id for h in hm2._hooks["PreUserMessage"][0].hooks}
    assert hook_map["echo a"] == "fixed_id_a"
    assert hook_map["echo b"] == id_b
    assert hook_map["echo c"] == id_c


# ──────────────────────────────────────────────
# Task 7: 启动迁移（非系统写回 + 幽灵清理）
# ──────────────────────────────────────────────
def test_migrate_legacy_states_writes_source_and_cleans_ghost(tmp_path):
    """迁移：非系统条目写回源文件 + 幽灵 id 删除，系统条目保留"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreUserMessage": [
                        {"matcher": "", "hooks": [{"id": "plugin_1", "type": "command", "command": "echo a"}]}
                    ]
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
    # 预置旧数据：plugin_1=False（非系统）、ghost=True（幽灵）、sys_1=True（系统）
    hm._hook_states["plugin_1"] = False
    hm._hook_states["ghost_1"] = True
    hm._hook_states["sys_1"] = True
    # 造一个系统 hook（is_system_plugin=True）
    from app.core.hook_manager import Hook, HookMatchRule

    sys_hook = Hook(id="sys_1", type="command", command="echo sys", is_system_plugin=True)
    hm._hooks.setdefault("Stop", []).append(HookMatchRule(matcher="", hooks=[sys_hook]))

    migrated = hm.migrate_legacy_hook_states()
    # plugin_1 写回源文件
    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert data["hooks"]["PreUserMessage"][0]["hooks"][0]["enabled"] is False
    # ghost 删除，sys 保留
    assert "plugin_1" not in hm._hook_states
    assert "ghost_1" not in hm._hook_states
    assert hm._hook_states.get("sys_1") is True
    assert migrated >= 2  # plugin_1 迁移 + ghost 清理


# ──────────────────────────────────────────────
# Task 8: reload_hooks_config 热重载失效修复
# ──────────────────────────────────────────────
def test_reload_hooks_config_actually_reloads(tmp_path):
    """热重载必须真正重新注册（不被去重拦截）"""
    import os as _os

    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreUserMessage": [
                        {"matcher": "", "hooks": [{"id": "h1", "type": "command", "command": "echo v1"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    hm = HookManager()
    hm._config_file = str(hooks_file)
    hm.register_hooks_from_json(
        "user-custom",
        str(tmp_path),
        json.loads(hooks_file.read_text(encoding="utf-8")),
        str(hooks_file),
    )
    assert len(hm._hooks.get("PreUserMessage", [])) == 1

    # 修改文件内容
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreUserMessage": [
                        {"matcher": "", "hooks": [{"id": "h1", "type": "command", "command": "echo v2"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    # 模拟 mtime 前进
    future = _os.path.getmtime(hooks_file) + 5
    _os.utime(hooks_file, (future, future))

    ok = hm.check_and_reload()
    assert ok is True, "热重载应成功"
    # 重新注册后 hook 应来自新内容（v2）
    hook = hm._hooks["PreUserMessage"][0].hooks[0]
    assert hook.command == "echo v2"


# ──────────────────────────────────────────────
# 回归：unregister 后其他 skill 的分组索引不得错位
# ──────────────────────────────────────────────
def test_unregister_skill_hooks_keeps_other_skill_indices_aligned(tmp_path):
    """热重载一个插件后，其他插件的 hook 分组映射必须保持正确（不得掉进自定义组）"""
    sys_file = tmp_path / "sys.json"
    sys_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "", "hooks": [{"id": "sys_a", "type": "command", "command": "echo sa"}]},
                        {"matcher": "", "hooks": [{"id": "sys_b", "type": "command", "command": "echo sb"}]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    pa_file = tmp_path / "pa.json"
    pa_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "", "hooks": [{"id": "pa_hook", "type": "command", "command": "echo pa"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    hm = HookManager()
    hm.register_hooks_from_json(
        "system", str(tmp_path), json.loads(sys_file.read_text(encoding="utf-8")), str(sys_file), is_system_plugin=True
    )
    hm.register_hooks_from_json(
        "plugin-a", str(tmp_path), json.loads(pa_file.read_text(encoding="utf-8")), str(pa_file)
    )

    # 模拟热重载 system 插件（watcher 触发路径）
    hm.unregister_skill_hooks("system")
    if str(sys_file) in hm._config_watchers:
        del hm._config_watchers[str(sys_file)]
    hm.register_hooks_from_json(
        "system", str(tmp_path), json.loads(sys_file.read_text(encoding="utf-8")), str(sys_file), is_system_plugin=True
    )

    # plugin-a 的 hook 必须仍在 plugin 分组（不得掉进 user/自定义）
    grouped = hm.get_all_hooks_grouped()
    plugin_ids = {h["id"] for hooks in grouped["plugin"].values() for h in hooks}
    user_ids = {h["id"] for hooks in grouped["user"].values() for h in hooks}
    assert "pa_hook" in plugin_ids, f"pa_hook 必须保留在 plugin 分组，实际 user={user_ids}"
    assert "pa_hook" not in user_ids
