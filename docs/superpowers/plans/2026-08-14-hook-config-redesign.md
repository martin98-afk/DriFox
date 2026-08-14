# Hook 配置存储层重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 hook 开关/编辑从统一覆盖层（hook_states.json）改为双轨制：插件 hook 写回源文件，系统 hook 保留覆盖层；修复 id 错位、幽灵状态、多实例竞态、热重载失效。

**Architecture:** 只改 `app/core/hook_manager.py` 的配置存储层。判定标准 `hook.is_system_plugin`（注册时由 `plugin.is_system` 注入）。非系统 hook 的 enabled/编辑直接写回 `hook.config_file` 指向的源 hooks.json（覆盖式、禁止追加）；系统 hook 维持现有 `_hook_states`/`_hook_overrides` 持久化。`_hook_states`/`_hook_overrides` 改为类级共享消除双实例竞态。启动时执行一次性迁移：hook_states 中非系统条目写回源文件后删除，幽灵 id 直接删除。

**Tech Stack:** Python 3.14 / PyQt5 / pytest 9.1 / loguru

## Global Constraints

- 判定标准：`hook.is_system_plugin == True` → 系统 hook（走覆盖层）；否则写回源文件
- 写回必须**覆盖**，禁止追加（历史教训：追加导致 hook 重复）
- 匹配不到目标 hook 时返回失败 + UI 提示，不静默假装成功
- 迁移只执行一次，单条失败跳过保留在 hook_states 下次重试
- 不改加载/匹配/执行引擎（matches / trigger_event / 执行管线）
- 不改 UI 层读写路径（仍走 toggle_hook_by_id / edit_hook_by_id）
- 提交规范：`feat|fix: scope - summary`
- 不碰任务外代码（no_unrelated_refactor）

---

### Task 1: `_hook_states` / `_hook_overrides` 改类级共享（消除双实例竞态）

**Files:**
- Modify: `app/core/hook_manager.py`（`__init__` ~L797-805、类属性区 ~L960-966）

**Interfaces:**
- Consumes: 无（基础设施改造）
- Produces: 类级共享 `_shared_hook_states: Dict[str, bool]`、`_shared_hook_overrides: Dict[str, Dict[str, Any]]`；实例属性 `self._hook_states`、`self._hook_overrides` 指向共享字典（与 `_shared_hooks` 模式一致）

- [ ] **Step 1: 写失败测试**

```python
# tests/core/test_hook_config_storage.py
# -*- coding: utf-8 -*-
"""Hook 配置存储层双轨制回归测试"""
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


def test_hook_states_shared_across_instances():
    """两个实例必须共享同一 _hook_states 快照（消除双实例覆盖竞态）"""
    a = HookManager()
    b = HookManager()
    a._hook_states["test_id"] = False
    assert b._hook_states.get("test_id") is False, "实例 B 必须看到实例 A 的修改"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/core/test_hook_config_storage.py::test_hook_states_shared_across_instances -v`
Expected: FAIL（实例 B 看不到 A 的修改，因为当前 `_hook_states` 是实例级）

- [ ] **Step 3: 实现类级共享**

在 `hook_manager.py` 类属性区（`_shared_hooks` 附近）添加：

```python
    # 跨窗口共享的 hook 开关状态（类级共享，避免多实例各自快照互相覆盖）
    _shared_hook_states: Dict[str, bool] = {}
    _shared_hook_overrides: Dict[str, Dict[str, Any]] = {}
```

`__init__` 中两处改为指向共享字典：

```python
        # hook 开关持久化（所有 hook 共用，不受插件源文件限制）
        self._hook_states: Dict[str, bool] = HookManager._shared_hook_states
        if not self._hook_states:
            self._hook_states.update(self._load_hook_states())

        # hook 内容覆盖持久化（系统 hook 编辑覆盖，与 hook_states 共享同一文件）
        self._hook_overrides: Dict[str, Dict[str, Any]] = HookManager._shared_hook_overrides
        if not self._hook_overrides:
            self._hook_overrides.update(self._load_hook_overrides())
```

注意：`_load_hook_states()` 返回 `{}` 时空 dict 会被共享——用 `if not` 守卫保证只加载一次磁盘。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/core/test_hook_config_storage.py::test_hook_states_shared_across_instances -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/hook_manager.py tests/core/test_hook_config_storage.py
git commit -m "refactor: share hook states across HookManager instances to prevent snapshot overwrite race"
```

---

### Task 2: 非系统 hook 的 toggle 写回源文件（不写 hook_states）

**Files:**
- Modify: `app/core/hook_manager.py`（`toggle_hook_by_id` ~L2193-2230）

**Interfaces:**
- Consumes: `_is_user_custom_hook(hook)`（现有）、`_save_hook_to_file_by_id(hook, new_data)`（现有，Task 3 会修）
- Produces: `_should_write_to_source(hook) -> bool` — 判定 hook 是否应写回源文件（`not hook.is_system_plugin`）

- [ ] **Step 1: 写失败测试**

```python
def _make_hook_manager_with_file(tmp_path):
    """构造带临时 hooks.json 的 HookManager"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(json.dumps({
        "hooks": {
            "PreUserMessage": [
                {"matcher": "", "hooks": [{"id": "plugin_hook_1", "type": "command", "command": "echo hi"}]}
            ]
        }
    }, ensure_ascii=False), encoding="utf-8")
    hm = HookManager()
    # 注册（绕过去重：直接调内部注册）
    count = hm.register_hooks_from_json(
        "test-plugin", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file)
    )
    assert count == 1
    return hm, hooks_file


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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/core/test_hook_config_storage.py::test_plugin_hook_toggle_writes_source_file -v`
Expected: FAIL（当前 toggle 只写 `_hook_states`，源文件无 enabled，且 states 有该 id）

- [ ] **Step 3: 实现**

在 `toggle_hook_by_id` 中，把持久化分支从 `_is_user_custom_hook` 改为双轨：

```python
    def toggle_hook_by_id(self, hook_id: str, enabled: bool) -> bool:
        result = self._find_hook_by_id(hook_id)
        if result is None:
            logger.warning(f"[HookManager] Hook {hook_id} not found")
            return False

        event_name, rule_idx, hook_idx, hook = result
        hook.enabled = enabled

        if hook.is_system_plugin:
            # 系统 hook：保留覆盖层持久化
            self._hook_states[hook.id] = enabled
            self._save_hook_states()
        else:
            # 非系统 hook：写回源文件（覆盖式）
            if hook.config_file:
                ok = self._save_hook_to_file_by_id(hook, {"enabled": enabled})
                if not ok:
                    logger.error(f"[HookManager] Failed to persist toggle for {hook_id} to {hook.config_file}")
            # 清理覆盖层残留（迁移兜底：若旧数据里还有该 id）
            if hook.id in self._hook_states:
                del self._hook_states[hook.id]
                self._save_hook_states()

        logger.info(f"[HookManager] Toggled hook {hook_id} enabled={enabled}")
        return True
```

注意：Task 3 会把 `_save_hook_to_file_by_id` 改为返回 bool 并覆盖式写入，此处在 Task 3 前先依赖现有签名（返回 None），`ok = ...` 在 Task 3 后可收紧为 bool 判断。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/core/test_hook_config_storage.py::test_plugin_hook_toggle_writes_source_file -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/hook_manager.py tests/core/test_hook_config_storage.py
git commit -m "feat: write plugin hook toggle state back to source hooks.json"
```

---

### Task 3: 覆盖式写入防重复（`_find_hook_fields` + `_save_hook_to_file_by_id`）

**Files:**
- Modify: `app/core/hook_manager.py`（`_find_hook_fields` ~L1965、`_save_hook_to_file_by_id` ~L2005）

**Interfaces:**
- Consumes: `Hook` 对象（含 id/config_file/command 等）
- Produces: `_save_hook_to_file_by_id(hook, new_data) -> bool` — 成功 True / 失败 False（匹配不到或写失败）；`_find_hook_fields(hook, config) -> Optional[tuple]` 优先 id 精确匹配

- [ ] **Step 1: 写失败测试**

```python
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
    hooks_file.write_text(json.dumps({
        "hooks": {
            "PreUserMessage": [
                {"matcher": "tool:bash", "hooks": [{"id": "h1", "type": "command", "command": "echo a"}]}
            ],
            "PostUserMessage": [
                {"matcher": "tool:bash", "hooks": [{"id": "h2", "type": "command", "command": "echo b"}]}
            ]
        }
    }, ensure_ascii=False), encoding="utf-8")
    hm = HookManager()
    hm.register_hooks_from_json("p", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file))
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
    hm.register_hooks_from_json("p", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file))
    # 伪造一个内存中不存在于文件的 hook（动态注册）
    from app.core.hook_manager import Hook, HookMatchRule
    ghost = Hook(id="ghost_1", type="command", command="echo x", config_file=str(hooks_file))
    rule = HookMatchRule(matcher="", hooks=[ghost])
    hm._hooks.setdefault("PreUserMessage", []).append(rule)
    ok = hm._save_hook_to_file_by_id(ghost, {"command": "echo y"})
    assert ok is False, "源文件无此 hook 必须返回 False"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/core/test_hook_config_storage.py -v`
Expected: 至少 `test_edit_hook_not_found_returns_false` FAIL（当前返回 None 不返回 False）

- [ ] **Step 3: 实现**

重写 `_find_hook_fields`（优先 id，id 匹配不到时按 (event, matcher, command 唯一键) 兜底）：

```python
    def _find_hook_fields(self, hook: Hook, config: dict) -> Optional[tuple]:
        """在配置中查找 hook 所在位置，返回 (event_name, rule_idx, hook_idx) 或 None

        匹配策略（防误更新）：
        1. 优先按 id 精确匹配（同 command 多 hook 场景不会误更新第一条）
        2. id 匹配不到时，按 (matcher, command/url/function/prompt) 唯一键兜底
        """
        raw_hooks = config.get("hooks", config)
        target_cmd = hook.command or hook.url or hook.function or hook.prompt or ""
        target_matcher = ""
        # 从内存规则里找该 hook 的 matcher
        for event_name, rules in self._hooks.items():
            for rule in rules:
                for h in rule.hooks:
                    if h.id == hook.id:
                        target_matcher = rule.matcher or ""
                        break
                if target_matcher:
                    break
            if target_matcher:
                break

        for event_name, rules in raw_hooks.items():
            for rule_idx, rule in enumerate(rules):
                hooks_list = rule.get("hooks", [])
                for hook_idx, h in enumerate(hooks_list):
                    hook_id = h.get("id", "") or ""
                    if hook_id and hook_id == hook.id:
                        return (event_name, rule_idx, hook_idx)
        # id 兜底失败 → 唯一键兜底（matcher + command）
        if target_cmd:
            for event_name, rules in raw_hooks.items():
                for rule_idx, rule in enumerate(rules):
                    hooks_list = rule.get("hooks", [])
                    for hook_idx, h in enumerate(hooks_list):
                        h_cmd = h.get("command", "") or h.get("url", "") or h.get("function", "") or h.get("prompt", "")
                        if h_cmd == target_cmd:
                            return (event_name, rule_idx, hook_idx)
        return None
```

重写 `_save_hook_to_file_by_id` 为覆盖式 + 返回 bool + 事件移动合并：

```python
    def _save_hook_to_file_by_id(self, hook: Hook, new_data: dict = None) -> bool:
        """通过 hook_id 覆盖式保存到源文件（禁止追加，防重复）

        Returns:
            True 成功 / False 匹配不到或写失败
        """
        if not hook.config_file or not os.path.exists(hook.config_file):
            return False
        try:
            with open(hook.config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            location = self._find_hook_fields(hook, config)
            if location is None:
                logger.warning(f"[HookManager] Hook {hook.id} not found in {hook.config_file}")
                return False

            event_name, rule_idx, hook_idx = location
            raw_hooks = config.get("hooks", config)
            target_rule = raw_hooks[event_name][rule_idx]
            target_hooks = target_rule.get("hooks", [])

            hook_entry = target_hooks[hook_idx]
            if new_data:
                for key in ("enabled", "command", "url", "function"):
                    if key in new_data:
                        hook_entry[key] = new_data[key]

                # 事件移动：合并进目标事件同 matcher rule，禁止新建重复 rule
                if "matcher" in new_data and "new_event_name" in new_data:
                    new_event = new_data["new_event_name"]
                    new_matcher = new_data["matcher"] or ""
                    if new_event not in raw_hooks:
                        raw_hooks[new_event] = []
                    # 查找目标事件中同 matcher 的 rule
                    matched_rule = None
                    for r in raw_hooks[new_event]:
                        if (r.get("matcher") or "") == new_matcher:
                            matched_rule = r
                            break
                    if matched_rule:
                        matched_rule.setdefault("hooks", []).append(hook_entry)
                    else:
                        raw_hooks[new_event].append({"matcher": new_matcher or "", "hooks": [hook_entry]})
                    # 从旧位置移除
                    target_hooks.pop(hook_idx)
                    if not target_rule.get("hooks"):
                        raw_hooks[event_name].pop(rule_idx)
                    if not raw_hooks.get(event_name):
                        del raw_hooks[event_name]

                for key in [
                    "type", "cwd", "add_output_to_context", "skill_root", "timeout", "retry",
                    "conditions", "headers", "allowedEnvVars", "function_args",
                    "commandWindows", "statusMessage", "prompt",
                ]:
                    if key in new_data:
                        hook_entry[key] = new_data[key]

            hook_entry["id"] = hook.id
            # 写前防重：清理同 id 重复条目（防御历史遗留）
            self._dedupe_hook_entries(raw_hooks)

            with open(hook.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            logger.debug(f"[HookManager] Saved hook {hook.id} to {hook.config_file}")
            return True
        except Exception as e:
            logger.error(f"[HookManager] Failed to save hook {hook.id}: {e}")
            return False

    @staticmethod
    def _dedupe_hook_entries(raw_hooks: dict):
        """写前防重：同一 rule 内同 id 的 hook 条目只保留第一个"""
        for rules in raw_hooks.values():
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                hooks = rule.get("hooks")
                if not isinstance(hooks, list):
                    continue
                seen: set = set()
                deduped = []
                for h in hooks:
                    hid = h.get("id", "")
                    if hid and hid in seen:
                        continue
                    if hid:
                        seen.add(hid)
                    deduped.append(h)
                rule["hooks"] = deduped
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/core/test_hook_config_storage.py -v`
Expected: 全部 PASS（含 Task 2 的测试——`_save_hook_to_file_by_id` 返回 bool 后 Task 2 的 `ok = ...` 判断生效）

- [ ] **Step 5: 全量 hook 相关测试回归**

Run: `pytest tests/test_stop_hook_matcher.py tests/test_f1_hook_async_semantics.py tests/test_hook_message_cleanup.py -v`
Expected: PASS（未破坏既有行为）

- [ ] **Step 6: 提交**

```bash
git add app/core/hook_manager.py tests/core/test_hook_config_storage.py
git commit -m "fix: overwrite-only hook save with id-precise matching and merge-on-move"
```

---

### Task 4: 非系统 hook 的 edit 写回源文件（不写 _overrides）

**Files:**
- Modify: `app/core/hook_manager.py`（`edit_hook_by_id` ~L2090-2180）

**Interfaces:**
- Consumes: `_save_hook_to_file_by_id`（Task 3 改造后返回 bool）
- Produces: 无新接口

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/core/test_hook_config_storage.py::test_plugin_hook_edit_writes_source_not_overrides -v`
Expected: FAIL（当前非 user-custom 的非系统 hook 走 `_hook_overrides` 分支）

- [ ] **Step 3: 实现**

修改 `edit_hook_by_id` 末尾的持久化分支（现 `if self._is_user_custom_hook(hook): ... else: overrides`）：

```python
        # 持久化到源文件（非系统 hook）
        if not hook.is_system_plugin:
            ok = self._save_hook_to_file_by_id(hook, new_data)
            if not ok:
                logger.error(f"[HookManager] Failed to persist edit for {hook_id}")
            # 清理覆盖层残留（迁移兜底）
            if hook.id in self._hook_overrides:
                del self._hook_overrides[hook.id]
                self._save_hook_states()
        else:
            # 系统 hook：持久化到 _hook_overrides（与 hook_states 共享同一文件）
            override_fields = {
                "type", "command", "url", "function", "prompt", "cwd",
                "add_output_to_context", "timeout", "retry", "commandWindows",
                "statusMessage", "function_args", "matcher",
            }
            overrides = {k: v for k, v in new_data.items() if k in override_fields}
            if overrides:
                self._hook_overrides[hook.id] = overrides
                self._save_hook_states()
```

注意：`edit_hook_by_id` 中事件变更分支（`if event_changed:`）里已有的 `_save_hook_to_file_by_id(hook, new_data)` 调用（user-custom 限定）也改为 `if not hook.is_system_plugin` 条件执行；同事件 matcher 更新分支同样处理。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/core/test_hook_config_storage.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/hook_manager.py tests/core/test_hook_config_storage.py
git commit -m "feat: write plugin hook edits back to source file instead of overrides layer"
```

---

### Task 5: `_apply_hook_state` / `_apply_hook_overrides` 仅对系统 hook 生效

**Files:**
- Modify: `app/core/hook_manager.py`（`register_hooks_from_json` 恢复段 ~L1005-1015、`_apply_hook_state` ~L830、`_apply_hook_overrides` ~L840）

**Interfaces:**
- Consumes: `hook.is_system_plugin`
- Produces: 无新接口

- [ ] **Step 1: 写失败测试**

```python
def test_apply_state_only_for_system_hook(tmp_path):
    """注册时：非系统 hook 不应用 _hook_states 覆盖（状态以源文件为准）"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(json.dumps({
        "hooks": {
            "PreUserMessage": [
                {"matcher": "", "hooks": [{"id": "plugin_1", "type": "command", "command": "echo a"}]}
            ]
        }
    }, ensure_ascii=False), encoding="utf-8")
    hm = HookManager()
    # 预置覆盖层状态（模拟旧数据残留）
    hm._hook_states["plugin_1"] = False
    hm.register_hooks_from_json("p", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file))
    hook = hm._hooks["PreUserMessage"][0].hooks[0]
    # 源文件无 enabled → 默认 True，且非系统 hook 不被覆盖层强制改 False
    assert hook.enabled is True
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/core/test_hook_config_storage.py::test_apply_state_only_for_system_hook -v`
Expected: FAIL（当前 `_apply_hook_state` 对所有 hook 生效 → enabled=False）

- [ ] **Step 3: 实现**

`register_hooks_from_json` 恢复段改为按 `is_system_plugin` 分支：

```python
        # 从持久化的状态恢复已注册 hook 的开关和内容覆盖（仅系统 hook）
        if count > 0:
            for event_name, rules in raw_hooks.items():
                if event_name not in self._hooks:
                    continue
                for rule in self._hooks[event_name]:
                    for hook in rule.hooks:
                        if not hook.is_system_plugin:
                            continue
                        if self._hook_states:
                            self._apply_hook_state(hook)
                        if self._hook_overrides:
                            self._apply_hook_overrides(hook, rule)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/core/test_hook_config_storage.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/hook_manager.py tests/core/test_hook_config_storage.py
git commit -m "fix: apply persisted hook state/overrides only to system hooks"
```

---

### Task 6: `_persist_hook_ids_to_file` id 分配错位修复

**Files:**
- Modify: `app/core/hook_manager.py`（`_persist_hook_ids_to_file` ~L1018-1065）

**Interfaces:**
- Consumes: `self._hooks`（内存注册表）
- Produces: 无新接口；保证源文件无 id 的 hook 分配到的 id 与内存一致且稳定

- [ ] **Step 1: 写失败测试**

```python
def test_persist_ids_aligned_with_file_order(tmp_path):
    """id 分配必须按文件顺序对齐，不因内存注册顺序错位"""
    hooks_file = tmp_path / "hooks.json"
    # 文件里两个 hook 都无 id，且命令不同
    hooks_file.write_text(json.dumps({
        "hooks": {
            "PreUserMessage": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "echo first"},
                    {"type": "command", "command": "echo second"},
                ]}
            ]
        }
    }, ensure_ascii=False), encoding="utf-8")
    hm = HookManager()
    hm.register_hooks_from_json("p", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file))
    # 注册后源文件应有两个不同 id（persist 已写回）
    data = json.loads(hooks_file.read_text(encoding="utf-8"))
    entries = data["hooks"]["PreUserMessage"][0]["hooks"]
    assert len(entries) == 2
    id1, id2 = entries[0]["id"], entries[1]["id"]
    assert id1 and id2 and id1 != id2
    # 再次注册（模拟重启）：id 必须保持稳定不漂移
    hm2 = HookManager()
    hm2.register_hooks_from_json("p", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file))
    hook_map = {h.command: h.id for h in hm2._hooks["PreUserMessage"][0].hooks}
    assert hook_map["echo first"] == id1
    assert hook_map["echo second"] == id2
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/core/test_hook_config_storage.py::test_persist_ids_aligned_with_file_order -v`
Expected: 视现状通过或失败；若通过则此测试作为回归保护。核心验证点：id 稳定不漂移。

- [ ] **Step 3: 实现**

`_persist_hook_ids_to_file` 的 mem_ids 收集与分配改为按 config_file + 文件顺序对齐：

```python
    def _persist_hook_ids_to_file(self, config_file: str):
        if not config_file or not os.path.exists(config_file):
            return
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            raw_hooks = config.get("hooks", config)
            modified = False

            # 收集属于该 config_file 的内存 hook id，按 (event, rule_idx, hook_idx) 记录
            config_file_norm = os.path.normpath(config_file)
            mem_ids: Dict[str, List[str]] = {}  # event_name -> [hook_id, ...]（按文件内顺序）
            for event_name, rules in self._hooks.items():
                for rule in rules:
                    for hook in rule.hooks:
                        if hook.config_file and os.path.normpath(hook.config_file) == config_file_norm:
                            if event_name not in mem_ids:
                                mem_ids[event_name] = []
                            mem_ids[event_name].append(hook.id)

            # 按文件内 hook 顺序逐个分配 id（无 id 才分配）
            for event_name, rules in raw_hooks.items():
                if event_name not in mem_ids:
                    continue
                ids_iter = iter(mem_ids[event_name])
                for rule in rules:
                    for h in rule.get("hooks", []):
                        if not h.get("id"):
                            try:
                                h["id"] = next(ids_iter)
                                modified = True
                            except StopIteration:
                                logger.warning(
                                    f"[HookManager] No in-memory id for hook in {event_name}: {h.get('command', '')[:40]}"
                                )
                        # 有 id 的 hook 不消费 ids_iter（避免错位）
```

关键修复：旧实现用 `idx` 全局计数（含已有 id 的 hook 也计数）导致错位；新实现**只有无 id 的 hook 才消费 id 迭代器**，天然对齐。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/core/test_hook_config_storage.py::test_persist_ids_aligned_with_file_order -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/hook_manager.py tests/core/test_hook_config_storage.py
git commit -m "fix: align hook id persistence with file order to prevent id drift"
```

---

### Task 7: 启动迁移（非系统条目写回源文件 + 幽灵 id 清理）

**Files:**
- Modify: `app/core/hook_manager.py`（新方法 + `register_hooks_from_json` 恢复段尾部调用）

**Interfaces:**
- Consumes: `_hooks`（注册完成后）、`_hook_states`、`_save_hook_to_file_by_id`、`_save_hook_states`
- Produces: `migrate_legacy_hook_states() -> int` — 迁移条数；在注册完成后调用（backend/agent 加载完所有 hooks 后）

- [ ] **Step 1: 写失败测试**

```python
def test_migrate_legacy_states_writes_source_and_cleans_ghost(tmp_path):
    """迁移：非系统条目写回源文件 + 幽灵 id 删除，系统条目保留"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(json.dumps({
        "hooks": {
            "PreUserMessage": [
                {"matcher": "", "hooks": [{"id": "plugin_1", "type": "command", "command": "echo a"}]}
            ]
        }
    }, ensure_ascii=False), encoding="utf-8")
    hm = HookManager()
    hm.register_hooks_from_json("p", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file))
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/core/test_hook_config_storage.py::test_migrate_legacy_states_writes_source_and_cleans_ghost -v`
Expected: FAIL（AttributeError: 无 migrate_legacy_hook_states）

- [ ] **Step 3: 实现**

新增方法：

```python
    def migrate_legacy_hook_states(self) -> int:
        """一次性迁移旧覆盖层数据（注册完成后调用）

        规则：
        - 非系统 hook 条目 → 写回源文件 enabled → 从 _hook_states 删除
        - 幽灵 id（内存中找不到 hook）→ 直接删除
        - 系统 hook 条目 → 保留
        单条失败跳过，下次启动重试。

        Returns:
            处理的条数
        """
        if not self._hook_states:
            return 0
        processed = 0
        dirty = False
        for hook_id, enabled in list(self._hook_states.items()):
            found = self._find_hook_by_id(hook_id)
            if found is None:
                # 幽灵 id
                del self._hook_states[hook_id]
                dirty = True
                processed += 1
                continue
            _, _, _, hook = found
            if hook.is_system_plugin:
                continue  # 系统 hook 保留
            # 非系统 hook：写回源文件
            if hook.config_file and os.path.exists(hook.config_file):
                ok = self._save_hook_to_file_by_id(hook, {"enabled": enabled})
                if ok:
                    del self._hook_states[hook_id]
                    dirty = True
                    processed += 1
                else:
                    logger.warning(f"[HookManager] Migration failed for {hook_id}, will retry next start")
            else:
                del self._hook_states[hook_id]
                dirty = True
                processed += 1
        if dirty:
            self._save_hook_states()
        if processed:
            logger.info(f"[HookManager] Migrated {processed} legacy hook states")
        return processed
```

在 `register_hooks_from_json` 恢复段尾部调用（仅当本次注册后做一次迁移）：

```python
        # 旧覆盖层数据一次性迁移（非系统条目写回源文件，幽灵清理）
        self.migrate_legacy_hook_states()
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/core/test_hook_config_storage.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/hook_manager.py tests/core/test_hook_config_storage.py
git commit -m "feat: one-time migration of legacy hook states to source files"
```

---

### Task 8: `reload_hooks_config` 热重载失效修复

**Files:**
- Modify: `app/core/hook_manager.py`（`reload_hooks_config` ~L1177-1215）

**Interfaces:**
- Consumes: `register_hooks_from_json`、`_config_watchers`
- Produces: 无新接口；热重载重新生效

- [ ] **Step 1: 写失败测试**

```python
def test_reload_hooks_config_actually_reloads(tmp_path, monkeypatch):
    """热重载必须真正重新注册（不被去重拦截）"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(json.dumps({
        "hooks": {"PreUserMessage": [{"matcher": "", "hooks": [{"id": "h1", "type": "command", "command": "echo v1"}]}]}
    }), encoding="utf-8")
    hm = HookManager()
    hm._config_file = str(hooks_file)
    hm.register_hooks_from_json("user-custom", str(tmp_path), json.loads(hooks_file.read_text(encoding="utf-8")), str(hooks_file))
    assert len(hm._hooks.get("PreUserMessage", [])) == 1

    # 修改文件内容
    hooks_file.write_text(json.dumps({
        "hooks": {"PreUserMessage": [{"matcher": "", "hooks": [{"id": "h1", "type": "command", "command": "echo v2"}]}]}
    }), encoding="utf-8")
    # 模拟 mtime 前进
    import os as _os
    future = _os.path.getmtime(hooks_file) + 5
    _os.utime(hooks_file, (future, future))

    ok = hm.check_and_reload()
    assert ok is True, "热重载应成功"
    # 重新注册后 hook 应来自新内容（v2）
    hook = hm._hooks["PreUserMessage"][0].hooks[0]
    assert hook.command == "echo v2"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/core/test_hook_config_storage.py::test_reload_hooks_config_actually_reloads -v`
Expected: FAIL（当前 reload 被去重 return 0 → ok=False 或 hook 未更新）

- [ ] **Step 3: 实现**

`reload_hooks_config` 中先清除 watcher 再注册：

```python
    def reload_hooks_config(self, config_file: str = None) -> bool:
        config_file = config_file or self._config_file
        if not config_file or not os.path.exists(config_file):
            return False

        try:
            HookWorker._clear_relative_func_cache()
            current_mtime = os.path.getmtime(config_file)
            last_mtime = self._config_watchers.get(config_file, 0)

            if current_mtime <= last_mtime:
                return False  # 文件未修改

            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            # 先清除去重缓存，再注册（否则 register 被 _config_watchers 拦截 return 0）
            if config_file in self._config_watchers:
                del self._config_watchers[config_file]

            self.register_hooks_from_json("user-custom", "", config, config_file)
            # 注册成功后更新监控时间（register 内部已更新，但此处兜底）
            self._config_watchers[config_file] = current_mtime

            logger.info(f"[HookManager] Hot reloaded hooks from {config_file}")
            return True
        except Exception as e:
            logger.error(f"[HookManager] Failed to reload hooks: {e}")
            return False
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/core/test_hook_config_storage.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 全量 hook 相关测试回归**

Run: `pytest tests/test_stop_hook_matcher.py tests/test_f1_hook_async_semantics.py tests/test_hook_message_cleanup.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/core/hook_manager.py tests/core/test_hook_config_storage.py
git commit -m "fix: clear config watcher before hot reload re-registration"
```

---

### Task 9: 全量回归 + 清理

**Files:**
- Modify: 无（验证 + 收尾）

- [ ] **Step 1: 全量 hook 相关测试**

Run: `pytest tests/core/test_hook_config_storage.py tests/test_stop_hook_matcher.py tests/test_f1_hook_async_semantics.py tests/test_hook_message_cleanup.py tests/test_gitee_multi_device_recover.py -v`
Expected: 全部 PASS

- [ ] **Step 2: lint 检查**

Run: `python -m ruff check app/core/hook_manager.py tests/core/test_hook_config_storage.py`
Expected: 无错误（或仅既有告警）

- [ ] **Step 3: 手动冒烟验证（可选，有 GUI 环境时）**

启动应用 → 设置 → Hook 管理：
1. 关闭一个插件 hook → 检查 `.drifox/plugins/<name>/hooks/hooks.json` 出现 `enabled: false`
2. 关闭一个系统 hook → 检查 `hook_states.json` 出现该 id
3. 重启应用 → 插件 hook 开关保持关闭
4. 编辑插件 hook command → 源文件被覆盖不重复

- [ ] **Step 4: 更新 state.json 坑点**

```bash
python plugins/system/skills/drifox-dev/scripts/state_manager.py pitfall \
  --module hook_manager \
  --symptom "hook 开关莫名变化/幽灵状态/双实例互相覆盖/热重载失效" \
  --cause "统一覆盖层按 id 索引，id 漂移+多端同步+多实例快照竞态" \
  --fix "双轨制：插件 hook 写回源文件，系统 hook 保留覆盖层；id 按文件顺序对齐；类级共享状态"
```

- [ ] **Step 5: 提交（如有文档变更）**

```bash
git add -A
git commit -m "chore: hook config storage redesign regression pass"  # 无变更则跳过
```

---

## Self-Review

**Spec 覆盖检查：**
- §2 修改点 1（toggle 写源文件）→ Task 2 ✓
- §2 修改点 2（edit 写源文件）→ Task 4 ✓
- §2 修改点 3（apply 仅系统）→ Task 5 ✓
- §2 修改点 4（persist id 对齐）→ Task 6 ✓
- §2 修改点 4b（覆盖式写入防重复）→ Task 3 ✓
- §2 修改点 5（迁移）→ Task 7 ✓
- §2 修改点 6（类级共享）→ Task 1 ✓
- §2 修改点 7（热重载修复）→ Task 8 ✓
- §2.1 防重复约束（匹配顺序/合并/写前防重/失败可见）→ Task 3 ✓
- 测试点 1-8 → Task 2/4/5/7/3/8 对应 ✓

**类型一致性：**
- `_save_hook_to_file_by_id` 返回 bool：Task 2 先依赖（`ok = ...`），Task 3 实现返回 bool，Task 4/7 消费 ✓
- `migrate_legacy_hook_states` 在 Task 7 定义，Task 7 内部调用 ✓
- `_dedupe_hook_entries` 在 Task 3 定义并调用 ✓
- 类级共享 `_shared_hook_states`/`_shared_hook_overrides`：Task 1 定义，Task 2/4/5/7 消费 ✓
