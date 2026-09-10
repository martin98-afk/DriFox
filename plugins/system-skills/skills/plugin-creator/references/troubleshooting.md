---
description: 插件开发常见问题与解决方案（症状→原因→修法）
---

# 常见问题

> 统一格式：**症状 → 原因 → 修法**。承接自 SKILL.md §8（已瘦身至路由面）。

---

## Manifest 相关

### ❌ validate_plugins.py 报错「name does not match directory」

**症状**：验证报 name 不匹配；或插件装上后不出现在 `/plugin-marketplace`。

**原因**：`plugin.json` 的 `name` 字段与插件目录名不一致。

**修法**：
```jsonc
// 目录：plugins/my-cool-plugin/
// ❌ "name": "myCoolPlugin"
// ✅ "name": "my-cool-plugin"
```

### ❌ JSON Schema 校验失败

**症状**：`validate_plugins.py` 报 schema 校验错误。

**原因**：`plugin.json` 不符合 `schemas/plugin.schema.json`。

**修法**：
- 确认所有必填字段存在（name, description, version, components）
- 确认 `name` 符合 `^[a-z][a-z0-9-]{1,63}$`、`version` 符合 SemVer
- 确认 `components` 至少启用一个

### ❌ 「Components flag enabled but directory not found」

**症状**：验证报组件目录缺失。

**原因**：`components` 某个 flag 为 `true` 但没有对应目录/文件（如 `"commands": true` 却没有 `commands/`）。

**修法**：删除该 flag 或补齐对应目录。

---

## 运行时问题

### ❌ 插件不显示在 /plugin-marketplace

**症状**：装了插件但列表里没有。

**原因**（按概率排序）：
1. `plugin.json` 位置错误 → 应为 `<name>/.drifox-plugin/plugin.json`
2. `name` 与目录名不一致
3. JSON 语法错误
4. 插件放在不被扫描的目录

**修法**：
- 对照目录结构逐项检查
- 用 `python -c "import json; json.load(open('.drifox-plugin/plugin.json'))"` 测 JSON
- 重启 DriFox

### ❌ 工具注册被拒（registry 拒绝加载）

**症状**：`tools/*.py` 写完不生效，日志报注册失败。

**原因**：`registry.register(...)` 未显式声明 `danger` 参数——插件工具必须显式声明（safe/dangerous），未声明直接拒绝注册。

**修法**：
```python
# ❌ registry.register("my_tool", schema, impl=...)            # 缺 danger
# ✅ registry.register("my_tool", schema, impl=_impl, danger="safe")
```

### ❌ Providers 没被识别 / 互相覆盖

**症状**：服务商列表少了一个；或两个同名服务商只剩一个。

**原因**：
1. `providers/foo.py` 只定义了函数，没暴露 `register(registry)` → loader 不扫描
2. 两个 provider 用了相同 `name`（如都叫 "DeepSeek"）→ 后加载者覆盖先加载者
3. user 插件与 system 内置同名 → 覆盖是**预期行为**（user 优先），非 bug

**修法**：每个 `providers/*.py` 暴露 `register(registry)`；`ProviderDef.name` 保持唯一；刻意覆盖 system 内置时确认命名冲突是有意为之。

### ❌ 命令不显示 / 不触发

**症状**：`/xxx` 输入后无此命令。

**原因**：frontmatter 缺 `description` 或 `type`；文件名含大写或特殊字符；`components.commands` 未开。

**修法**：对照 `components.md §Commands` 检查。

### ❌ Hook 不触发

**症状**：事件发生但钩子函数没执行。

**原因**：`hooks.json` 格式错误；事件名拼写错误（大小写敏感）；函数名与 `hooks.json` 引用不匹配；Python 文件语法错误。

**修法**：
```bash
python -m py_compile hooks/<name>_hook.py
```

### ❌ UI 卡片空白 / 不显示

**症状**：卡片命令可输入但无窗口，或直接无命令。

**原因**：`ui/__init__.py` 缺失或无 `register_ui(registry)`；`components.ui` 未开；widget 构造签名不符。

**修法**：先按上述三点自检；仍不行 → **调用 `ui-plugin-creator` 技能**，提供详细症状。

### ❌ 插件内修改被升级覆盖

**症状**：改了 `plugins/` 下 system-* 系列内置插件，DriFox 更新后改动丢失或行为异常。

**原因**：system-* 是 DriFox 内置插件，不应手动修改。

**修法**：把需要的代码 fork 成自己的插件放到 `~/.drifox/plugins/<your-plugin>/`（用户插件根），基于它开发；system 插件保持原样。

### ❌ 改了代码但 version 没动

**症状**：市场/用户侧更新后拿到的还是旧版；CI 报版本不一致。

**原因**：每次修改后未更新 `plugin.json` 的 `version`。

**修法**：**每次修改后都更新 `version`**（SemVer：破坏性变更升 major / 新功能升 minor / 修复升 patch），再跑 generate。

---

## 发布问题

### ❌ PR 的 CI 失败

**症状**：提交 PR 后 GitHub Actions 红。

**原因**：`validate_plugins.py` 未通过或 `marketplace.json` 不一致。

**修法**（在 drifox-plugins 仓库 clone 中执行）：
```bash
python tools/validate_plugins.py
python tools/generate_marketplace.py
git add marketplace.json
git commit -m "chore: update marketplace.json"
```

### ❌ PR 被要求修改

**症状**：maintainer 或 bot 留下修改意见。

**常见原因**：`description` 过长（>200 字）；缺少 `README.md`；`version` 不合理；缺少 `license` 字段。

---

## 其他

### ❌ team_templates YAML 校验失败（TemplateError）

**症状**：`/team --load` 报 `TemplateError`，或模板不出现在 `/team` 列表。

**原因**：YAML 不合法——缺 `schema_version`（固定为 1）/`template_name`/非空 `agents`；`agents[].agent_name` 引用了不存在的 @角色；文件名含 `.`/`/`/反斜杠/`..`。

**修法**：
```yaml
schema_version: 1            # 固定为 1
template_name: my-team       # 建议与文件名一致
agents:                      # 非空；agent_name 必须引用已存在的 @角色
  - agent_name: build
```

### ❌ 不知道从何开始

**修法**：复制 [plugins/example-plugin/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin) 或本技能 `examples/` 目录下的最小骨架作为起点。

### ❌ 不知道该用哪种组件

**修法**：看 SKILL.md「触发与第一动作」的触发词表分流。

### ❌ 需要 UI 插件

**修法**：调用 `ui-plugin-creator` 技能，本技能不处理 UI 开发细节。
