---
description: DriFox 命令文件格式规范
---

# DriFox 命令文件格式规范

DriFox 命令系统通过 `.md` 文件定义斜杠命令（如 `/plugin`、`/verify`），支持三种命令类型。本文档说明完整的文件格式与 `prompt_sections` 分段机制。

---

## 文件位置

| 来源 | 路径 | 优先级 |
|------|------|--------|
| 系统内置 | `plugins/system/commands/<name>.md` | 低（可被用户覆盖） |
| 用户插件 | `.drifox/plugins/<plugin>/commands/<name>.md` | 高（同名覆盖系统） |

文件名（不含 `.md`）即为命令名，如 `plugin.md` → `/plugin`。

---

## 基础结构

每个命令文件 = **YAML frontmatter** + **Markdown 正文**。

```markdown
---
description: 命令简短描述
type: prompt
prompt_sections:
  --quick: "quick"        # 参数→标记 ID 映射（短引用）
  --deep: "deep"
---

# 正文（common，始终发送）

正文是「始终发送」的公共提示词。
参数相关的段落用 `<!-- section:id -->` / `<!-- end -->` 标记。

<!-- section:quick -->
## 快速模式指令
...仅在传 --quick 时发给 AI ...
<!-- end -->

<!-- section:deep -->
## 深度模式指令
...仅在传 --deep 时发给 AI ...
<!-- end -->
```

---

## 核心设计

**body（`---` 以下的正交）就是 common，始终发送。**
**参数相关的段落用 HTML 注释标记在 body 内，系统按需过滤。**

### 装配逻辑

当用户传参时，系统从 body 中**移除不匹配的标记段**，只保留：
- 公共部分（所有标记段之外的内容）
- 匹配参数对应的 `<!-- section:id -->` 段

| 用户输入 | 发送给 AI 的内容 |
|----------|-----------------|
| `/verify`（无参数） | 完整 body（含所有标记段，注释对 LLM 不可见） |
| `/verify --tests` | 公共部分 + `--tests` 段（移除 `--build`/`--all` 段） |
| `/verify --tests --build` | 公共部分 + `--tests` 段（同 `mode` 组互斥，取第一个） |
| `/verify --quick --html` | 公共部分 + `--quick` + `--html` 段（不同组，叠加） |
| 无 `prompt_sections` 的旧命令 | 完整 body（100% 向后兼容） |

---

## Frontmatter 字段

### 必填

| 字段 | 类型 | 说明 |
|------|------|------|
| `description` | str | 命令描述，显示在命令卡片和 `/help` 中 |
| `type` | str | `prompt` / `function` / `agent` |

### 可选

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `argument-hint` | dict | 无 | 参数列表与说明，**建议用 `parameters` 替代** |
| `parameters` | list | 无 | 结构化参数定义（新格式，推荐） |
| `mutex_groups` | dict | 无 | 互斥组，同组参数只能选其一 |
| `prompt_sections` | dict | 无 | **参数→提示词分段映射**（核心） |
| `shortcut` | str | 无 | 快捷键，如 `Ctrl+Shift+N` |
| `allowed-tools` | list | 无 | 工具白名单 |
| `tools` | list/dict | 无 | 工具白名单（同 `allowed-tools`） |
| `permission` | dict | 无 | 权限配置（`deny` 模式） |
| `hidden` | bool | false | 设为 true 时不显示在命令卡片中 |

---

## 命令类型（type）

| 类型 | 说明 |
|------|------|
| `prompt` | 提示词替换命令。执行时 body + prompt_sections 替换用户输入，发送给 AI |
| `function` | 函数型命令。触发 Python 处理器，不发送给 AI |
| `agent` | 智能体命令。同 `prompt`，额外支持 `--subagent` 子智能体模式 |

---

## 参数定义（parameters）

### 数组格式（推荐）

```yaml
parameters:
  - name: "--quick"
    description: "快速模式"
    param_type: flag
    mutex: mode          # 互斥组名

  - name: "--save-to="
    description: "输出路径"
    param_type: value

  - name: "<query>"
    description: "搜索关键词"
    param_type: positional
```

### param_type

| 值 | 说明 | 示例输入 |
|----|------|---------|
| `flag` | 开关参数，无值 | `--quick` |
| `value` | 带值参数 | `--save-to=report.md` |
| `positional` | 位置参数 | 无前缀的文本 |

### 简化格式（argument-hint，兼容旧版）

```yaml
argument-hint:
  "[--quick]": "快速模式"
  "[--save-to=]": "输出路径"
  "[<query>]": "搜索主题"
```

`[]` → 可选参数，`=` 结尾 → 带值参数。

---

## 互斥组（mutex_groups）

同组参数互斥，只取第一个匹配的：

```yaml
mutex_groups:
  mode: ["--quick", "--thorough", "--deep"]
  output: ["--markdown", "--html"]
```

**效果**：
- `/v --tests --build` → 不同组 → **都追加**对应 sections
- `/v --quick --deep` → 同 `mode` 组 → **只追加 --quick** section

> `prompt_sections` 选择也遵循此互斥规则。

---

## prompt_sections：参数→提示词分段

### YAML 格式

`prompt_sections` 的值是**短引用字符串**，不是大段 Markdown：

```yaml
prompt_sections:
  --tests: "tests"      # 参数 → 标记 ID
  --build: "build"
  --all: "all"
```

### body 标记语法

在正文中，每段参数相关的内容用 `<!-- section:id -->` 和 `<!-- end -->` 包裹：

```markdown
公共内容（始终发送）……

<!-- section:tests -->
## 测试验证
测试流程指令……
<!-- end -->

<!-- section:build -->
## 构建验证
构建流程指令……
<!-- end -->
```

- `<!-- section:id -->` — 段开始，`id` 与 YAML 中引用的 ID 对应
- `<!-- end -->` — 段结束
- 标记之间的内容仅在对应的参数被传入时才发送给 AI
- 公共内容（标记之外）始终发送
- `<!-- ... -->` 是标准 HTML 注释，LLM 会忽略，即使全部发送也无副作用

### 完整示例

```markdown
---
description: 完成前验证工作
type: prompt
mutex_groups:
  mode: ["--tests", "--build", "--all"]
prompt_sections:
  --tests: "tests"
  --build: "build"
  --all: "all"
---

## 铁律

未经最新验证证据，不声称任何状态。

## 红牌 - 停止

任何暗示成功但未运行验证的措辞，都是红牌。

## 底部原则

验证没有捷径。

<!-- section:tests -->
### 测试验证
- 测试命令输出：0 失败
- 回归测试：红-绿-红-绿 循环
<!-- end -->

<!-- section:build -->
### 构建验证
- 构建命令退出码：0
- Linter 检查通过
<!-- end -->

<!-- section:all -->
### 全量验证
执行所有验证流程，保证输出完整。
<!-- end -->
```

### 跨组叠加示例

```markdown
---
mutex_groups:
  mode: ["--quick", "--deep"]
  report: ["--html"]
prompt_sections:
  --quick: "quick"
  --deep: "deep"
  --html: "html"
---

## 通用搜索约束
……

<!-- section:quick -->
### 快速模式
1 跳搜索……
<!-- end -->

<!-- section:deep -->
### 深度模式
5 跳搜索……
<!-- end -->

<!-- section:html -->
HTML 渲染模板……
<!-- end -->
```

| 输入 | 发送内容 |
|------|---------|
| `/r --quick` | 公共 + quick 段 |
| `/r --quick --html` | 公共 + quick + html 段（不同组） |
| `/r --deep --html` | 公共 + deep + html 段 |
| `/r --quick --deep --html` | 公共 + quick + html 段（mode 互斥） |

---

## 工具限制

```yaml
# 白名单
allowed-tools:
  - Read
  - Glob
  - Grep

# Deny 模式
permission:
  question: deny
  subagent_para: deny
```

系统自动在提示词第一行生成工具限制说明，无需手动编写。

---

## 完整参考

| 文件 | 类型 | 说明 |
|------|------|------|
| `plugin.md` | prompt | 多参数互斥命令 |
| `verify.md` | prompt | 最适合改为 prompt_sections 的例子 |
| `webresearch.md` | prompt | 834 行大型命令，分段收益最大 |
| `debug.md` | prompt | 多阶段流程 |
| `subagents.md` | function | 唯一已用 prompt_sections 的例子（`--create=`） |
| `new.md` | function | 函数型命令示例 |

---

> **设计原则**：body 放「无论什么情况都需要的」公共提示词，
> `<!-- section:id -->` 标记段放「仅当用户指定参数时才需要的」特定提示词。
> 不把大段 Markdown 塞进 YAML，保持 frontmatter 干净可读。
