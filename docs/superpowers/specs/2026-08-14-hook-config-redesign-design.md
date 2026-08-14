# Hook 配置存储层重设计

日期: 2026-08-14
状态: 已批准
范围: 只重设计配置存储层（不改加载/匹配/执行引擎）

## 背景与问题

排查发现 hook 开关「莫名开启/状态错乱」的根因链：

1. **统一覆盖层 + id 不稳定**：所有 hook 的 enabled 存 `hook_states.json`，按 `hook.id` 索引。
   源文件无 id 的 hook 每次启动生成新 uuid4；`_persist_hook_ids_to_file` 存在 id 分配错位 bug，
   导致 `_hook_states` 里的 id 匹配不上 → `_apply_hook_state` 不生效 → hook 恢复源文件默认
   （32/43 源文件无 enabled 字段 → 默认 True = 开启）。
2. **gitee 多端同步放大**：hook_states.json 被 config_sync 同步到多端，各端插件集不同 →
   幽灵 id（12/33 个 id 在本机源文件找不到）→ 状态错位。
3. **多实例快照竞态**：backend 的 HookManager 实例与 settings popup 的 HookManager 实例
   各持独立 `_hook_states` 快照，`_save_hook_states()` 整文件覆盖 → 后保存者用旧快照覆盖
   先保存者的修改。
4. **热重载失效**：`reload_hooks_config` 先写 `_config_watchers[config_file]` 再调
   `register_hooks_from_json`，命中去重检查 `return 0` → 热重载永远不生效。

## 设计原则

**每个 hook 的配置归属其来源文件**。插件 hook 直接读写源 hooks.json；系统 hook 保留覆盖层
（软件更新不丢用户修改）。

## 存储模型（双轨制）

| hook 来源 | enabled 开关 | 内容编辑 | 加载 |
|---|---|---|---|
| 插件 hook（`.drifox/plugins/*/`，`is_system_plugin=False`） | 写回源文件 `enabled` 字段 | 写回源文件 | 直接读源文件，不经过覆盖层 |
| user-custom hook（`.drifox/plugins/user-custom/`） | 写回源文件 | 写回源文件 | 直接读源文件 |
| 系统 hook（`plugins/system/`，`is_system_plugin=True`） | `hook_states.json`（保留现状） | `_overrides`（保留现状） | 源文件默认 + 覆盖层应用 |

**判定标准**：`hook.is_system_plugin`（注册时由 `plugin.is_system` 注入，已有字段）。

## 修改点（全部在 app/core/hook_manager.py）

| # | 位置 | 改动 |
|---|---|---|
| 1 | `toggle_hook_by_id` | 非系统 hook：写源文件 enabled + 不再写 `_hook_states`；系统 hook 保留现状 |
| 2 | `edit_hook_by_id` | 非系统 hook：写源文件（替代 `_overrides`）；系统 hook 保留现状 |
| 3 | `_apply_hook_state` / `_apply_hook_overrides` | 仅对系统 hook 生效（注册时判断 is_system_plugin） |
| 4 | `_persist_hook_ids_to_file` | 修复 id 分配错位 bug（mem_ids 按 config_file 过滤后按文件顺序对齐） |
| 4b | `_save_hook_to_file_by_id` / `_find_hook_fields` | **覆盖式写入，禁止追加**（见 §2.1 防重复约束） |
| 5 | 迁移逻辑（新） | 启动注册完成后：hook_states 中非系统条目 → 写回源文件 enabled → 删除条目；幽灵 id → 删除 |
| 6 | `_hook_states` / `_hook_overrides` | 改类级共享（与 `_shared_hooks` 一致），消除双实例快照竞态 |
| 7 | `reload_hooks_config` | 修复热重载失效：先清 `_config_watchers` 再注册 |

### §2.1 覆盖式写入防重复约束（历史教训）

历史 bug：写回源文件时没有覆盖，而是在文件尾部追加新 rule → hook 重复。

约束：
- **匹配顺序**：`_find_hook_fields` 优先按 `hook_id` 精确匹配；id 匹配不到时，才按
  `(event, matcher, command/url/function/prompt)` 唯一键兜底；两者都匹配不到 → 记日志 +
  返回失败，**绝不 append 新条目**（append 只允许 `_add_hook` 新建场景）。
- **事件移动**（matcher + new_event_name 变更）：目标事件若已存在同 matcher 的 rule →
  **合并进该 rule**，禁止新建 rule；旧位置移除后校验不残留。
- **写前防重**：写入前扫描目标文件，若存在同 id 或同 command 的重复条目 → 先清理重复再写。
- **失败可见**：匹配不到时返回 False 并在 UI 提示保存失败（不再静默 return 假装成功）。

## 数据流示例

**用户关掉插件 ponytail 的 hook**：

```
UI Switch → toggle_hook_by_id("ponytail_pre_user_message", False)
  → hook.enabled = False（内存共享对象）
  → 写回 .drifox/plugins/ponytail/hooks/hooks.json 的 enabled: false
  → 不写 hook_states.json
重启/多端同步 → 源文件自带 enabled:false → 状态稳定不丢
```

**用户编辑系统 builtin_global_contract**：

```
保存 → edit_hook_by_id → 写 _overrides（现状保留）
重启 → 源文件默认 + overrides 应用 → 编辑不丢
```

## 迁移流程（启动时一次性，注册完成后执行）

```
遍历 _hook_states：
  在 _hooks 中按 id 找 hook：
    - hook 存在 且 非系统 → 写回源文件 enabled = state → 从 _hook_states 删除
    - hook 存在 且 系统   → 保留
    - hook 不存在（幽灵） → 从 _hook_states 删除
保存 _hook_states
```

单条失败：跳过该条，保留在 hook_states，下次重试。

## 错误处理

- 写源文件失败（权限/只读）：记日志，保留内存状态，不崩溃。
- 源文件被外部改动（gitee 同步）：以文件内容为准；覆盖层只对系统 hook 生效。

## 测试点

1. 插件 hook toggle → 源文件 enabled 变化 + hook_states.json 无此条目
2. 系统 hook toggle → hook_states.json 变化 + 源文件不变
3. 幽灵 id 启动清理
4. 双实例（backend + settings）toggle 后互不覆盖
5. 热重载生效（改源文件 → check_and_reload 重新注册）
6. **防重复**：编辑已有 hook → 源文件条目被覆盖，不新增重复 rule（含事件移动场景：
   目标事件已有同 matcher rule 时合并不新建）
7. **防误更新**：同 command 多 hook 时，按 id 精确更新目标，不误更新第一条
8. **失败可见**：源文件找不到目标 hook 时返回失败 + UI 提示，不静默

## 不做的事

- 不改加载/匹配/执行引擎（HookMatchRule.matches / trigger_event / 执行管线）
- 不改 UI 层（hook_setting_card.py 的读写路径不变，仍走 toggle_hook_by_id / edit_hook_by_id）
- 不做插件更新时 hooks.json 合并（接受插件更新覆盖源文件的现状，用户已确认）
