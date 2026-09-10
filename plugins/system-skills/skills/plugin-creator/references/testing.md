---
description: 插件测试、验证与除错指南
---

# 测试与验证

> 承接自 SKILL.md §6（已瘦身至路由面）。

---

## 1. 本地热更新测试

DriFox 使用 **watchfiles** 实现插件热更新：

| 修改内容 | 更新方式 | 延迟 |
|---------|---------|------|
| commands/*.md | 自动生效 | 1-3 秒 |
| skills/*/SKILL.md | 自动生效 | 1-3 秒 |
| themes/*.yaml | 用 `/theme <name>` 切换 | 即时 |
| ui/ 卡片内容 | 自动重新加载 | 1-3 秒 |
| tools/*.py / providers/*.py | 自动热生效（watcher 扫描） | 1-3 秒 |
| hooks/*.py | 需重启 DriFox | — |
| .mcp.json / .lsp.json | 需重启 DriFox | — |
| plugin.json | 需重启 DriFox | — |

### 测试流程

```
① 修改插件文件
② 等待 1-3 秒 watchfiles 检测
③ 用对应功能测试：
   - Commands → 输入 /<command-name>
   - Agents → 输入 @<agent-name>
   - Skills → 触发场景让 AI 匹配
   - Themes → 输入 /theme <name>
   - Tools → AI 调用对应工具
   - UI → 输入 /<card-id> 或查看消息流
④ 用 /plugin-marketplace 查看插件状态（已安装/启用/禁用）
```

---

## 2. 完整验证（发布前必做）

clone 官方市场仓库，把你的插件放进去跑验证（四步）：

```bash
# 第一步：clone 官方市场仓库
git clone https://github.com/martin98-afk/drifox-plugins.git /tmp/dfp

# 第二步：把你的插件复制到仓库中
cp -r ~/.drifox/plugins/<name> /tmp/dfp/plugins/<name>
cd /tmp/dfp

# 第三步：在仓库中跑验证
python tools/validate_plugins.py
python tools/generate_marketplace.py

# 第四步：确认全部 OK 后，清掉暂存
rm -rf /tmp/dfp
```

### validate_plugins.py 检查项（十项）

- [x] plugin.json 存在且 JSON 合法
- [x] JSON Schema 校验通过（schemas/plugin.schema.json）
- [x] name 与目录名一致
- [x] version 符合 SemVer
- [x] description 不超过 200 字
- [x] 每个启用的 component 有对应文件
- [x] commands/*.md 有完整 frontmatter
- [x] skills/*/SKILL.md 有 frontmatter
- [x] providers/*.py 暴露 register(registry) 且 ProviderDef.name 唯一
- [x] team_templates/*.yaml 含 schema_version/template_name/非空 agents，且 agent_name 引用已存在的 @角色
- [x] hooks Python 文件可编译
- [x] marketplace.json 一致性

> ⚠️ `validate_plugins.py` 未通过 → 禁止提 PR（SKILL.md 硬停止第 6 条）。

---

## 3. Python 语法检查

```bash
# 检查本地开发中的插件（在 ~/.drifox/plugins/ 下）
python -m py_compile ~/.drifox/plugins/<name>/hooks/<name>_hook.py
python -m py_compile ~/.drifox/plugins/<name>/ui/__init__.py
python -m py_compile ~/.drifox/plugins/<name>/tools/*.py

# 或在 drifox-plugins 仓库 clone 中检查
python -m py_compile plugins/<name>/hooks/<name>_hook.py
```

> 注意：py_compile 会在同目录生成 `__pycache__/`，提交插件前删掉它。

---

## 4. 技能包自检脚本

本技能包自带结构自检脚本（校验 frontmatter、行数上限、references/evals 引用完整性）：

```bash
# 在本技能包目录（plugin-creator/）下执行
python scripts/check_skill_package.py .

# 对其他技能包（如 ui-plugin-creator）同样适用
python scripts/check_skill_package.py ../ui-plugin-creator

# 常用参数
#   --max-lines 200      覆盖 SKILL.md 行数上限（默认按技能包名取 200/230）
#   --check-only         只读校验，不写任何文件
```

退出码：`0`=通过 / `1`=结构错误 / `2`=警告（有警告但结构可过）。

---

## 5. 除错技巧

### 插件未加载

1. 检查 `plugin.json` 位置：`<plugin-name>/.drifox-plugin/plugin.json`
2. 检查 `name` 字段是否与目录名一致
3. 检查 JSON 语法是否合法
4. 重启 DriFox 后用 `/plugin-marketplace` 查看

### 命令不生效

1. 检查 `commands/*.md` 的 frontmatter
2. 检查 `description` 字段是否存在
3. 检查文件名是否 `^[a-z][a-z0-9-]*\.md$`
4. 检查 `plugin.json` 中 `components.commands: true`

### Hook 不触发

1. 检查 `hooks/hooks.json` 语法
2. 检查事件名是否拼写正确（区分大小写）
3. 检查函数引用路径是否正确
4. 用 `python -m py_compile` 检查语法
5. 重启 DriFox

### UI 卡片不显示

1. 检查 `ui/__init__.py` 是否存在
2. 检查是否定义了 `register_ui(registry)` 函数
3. 检查 `plugin.json` 中 `components.ui: true`
4. → 调用 `ui-plugin-creator` 技能

### 验证报错

- 仔细阅读 `validate_plugins.py` 的错误信息
- 对照 `schemas/plugin.schema.json` 检查 manifest
- 确认所有引用的目录和文件都存在

更多症状 → references/troubleshooting.md（症状→原因→修法速查）。
