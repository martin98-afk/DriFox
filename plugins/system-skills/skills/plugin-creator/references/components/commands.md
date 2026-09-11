---
description: Commands（斜杠命令）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# Commands（斜杠命令）组件开发

### 文件位置

```
<plugin>/commands/<name>.md → 注册为 /<name>
```

### 最小模板

```markdown
---
description: 一句话说明命令做什么
type: prompt
parameters:
  - name: "--flag"
    description: "开关参数"
    param_type: flag
  - name: "--value="
    description: "带值参数"
    param_type: value
prompt_sections:
  --flag: "flag"
  --value: "value"
---

# /<name> 命令

你正在处理 `/<name>` 命令。用户参数：`$ARGUMENTS`

## 行为

1. 第一步做什么
2. 第二步做什么

<!-- section:flag -->
## Flag 模式
此段仅在使用 `--flag` 时追加……
<!-- end -->

<!-- section:value -->
## Value 模式
此段仅在使用 `--value=xxx` 时追加……
<!-- end -->
```

### 三种 type

| type | 说明 | 触发方式 |
|------|------|---------|
| `prompt` | 提示词替换，body + 选中段发送给 AI | 用户输入 `/xx` |
| `function` | 函数型，触发 Python 处理器，不发给 AI | 用户输入 `/xx` |
| `agent` | 同 prompt，额外支持 `--subagent` 模式 | 用户输入 `/xx` |

### 完整 frontmatter 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `description` | string | ✅ | 命令简介 |
| `type` | enum | ✅ | prompt / function / agent |
| `parameters` | list | 可选 | 结构化参数定义（推荐新格式） |
| `argument-hint` | dict | 可选 | 旧式参数提示（兼容） |
| `mutex_groups` | dict | 可选 | 互斥组，同组参数只能选其一 |
| `prompt_sections` | dict | 可选 | 参数→提示词分段映射 |
| `shortcut` | string | 可选 | 快捷键，如 `Ctrl+Shift+C` |
| `tools` | list | 可选 | 工具白名单 |
| `permission` | dict | 可选 | 权限配置（deny 模式） |
| `hidden` | bool | 可选 | true 时不显示在命令卡片 |

### 参数定义

```yaml
parameters:
  - name: "--quick"
    description: "快速模式"
    param_type: flag
    mutex: mode

  - name: "--save-to="
    description: "输出路径"
    param_type: value
    value_options:     # 自动补全候选值
      - json
      - yaml
      - toml

  - name: "<query>"
    description: "搜索关键词"
    param_type: positional
```

### 模板变量

| 变量 | 含义 |
|------|------|
| `$ARGUMENTS` | 用户在命令后输入的完整参数 |
| `$PLUGIN_NAME` | 当前插件名 |
| `$PLUGIN_DIR` | 插件根目录的绝对路径 |
| `$PROJECT_ROOT` | 当前工作项目根目录 |

### 参考

- 完整规范：[docs/commands.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/commands.md)
- 系统命令案例：`plugins/system-commands/commands/`
- 最小可运行样例：本技能 `examples/`（工具/配置/UI 骨架）

---
