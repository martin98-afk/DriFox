---
description: 管理子智能体任务和默认模型
type: function
shortcut: Ctrl+Shift+A
argument-hint:
  "[--detail]": "显示子智能体详细日志面板"
  "[--model=]": "设置子智能体默认模型"
  "[--reset]": "清空子智能体默认模型设置"
  "[--create=]": "进入创建子智能体工作流，根据描述自动生成智能体 md 文件"
mutex_groups:
  mode: ["--detail", "--model=", "--reset", "--create="]
prompt_sections:
  --create=: "create"
---

<!-- section:create -->
## 任务：创建 DriFox 子智能体

请根据以下描述，创建一个新的 DriFox 子智能体 md 文件。

### 用户需求描述
$ARGUMENTS

### 输出要求

使用 `write` 工具创建文件，目标目录：`~/.drifox/plugins/user-custom/agents/<agent-name>.md`

文件名 `<agent-name>` 请根据描述自动生成，规则：
- 使用英文小写 + 连字符（kebab-case）
- 2-4 个单词，如 `code-reviewer`、`api-doc-generator`
- 清晰表达智能体核心功能

### 角色模板选择

根据用户描述判断智能体的主要职责，从以下三套模板中选最合适的。

---

#### 模板 A — 只读探索型（Read-Only Analysis）

**适用场景**：代码审查、架构分析、依赖分析、Bug 复现分析。**只能读，不能改任何文件。**

```yaml
---
description: <必填，何时触发 + 功能描述>
mode: subagent
hidden: false
temperature: 0.2
steps: 50
tools:
  - Read
  - Glob
  - Grep
---
```

- 不能执行 shell 命令
- 不能写/编辑任何文件
- 适合 `Review`、`Explore`、`Audit` 等职责

---

#### 模板 B — 读写实现型（Read-Write Implementation）

**适用场景**：功能开发、Bug 修复、代码重构、测试编写。**可以读写文件，可执行有限 shell 命令。**

```yaml
---
description: <必填，何时触发 + 功能描述>
mode: subagent
hidden: false
temperature: 0.2
steps: 100
tools:
  - Read
  - Glob
  - Grep
  - Write
  - Edit
  - multi_edit
  - Bash
---
```

- `Bash` 仅限项目构建/测试命令（`npm test`、`pytest` 等），不允许任意 shell
- 适合 `Build`、`Fix`、`Refactor`、`Test` 等职责

---

#### 模板 C — 规划分析型（Planning & Design）

**适用场景**：架构设计、方案对比、需求分析、技术调研。**可以读文件、可以提问，不能改动代码。**

```yaml
---
description: <必填，何时触发 + 功能描述>
mode: subagent
hidden: false
temperature: 0.2
steps: 30
tools:
  - Read
  - Glob
  - Grep
  - Question
---
```

- 允许 `Question` 向主智能体提问（获取额外上下文）
- 不能写/编辑文件，不能执行 shell
- 适合 `Plan`、`Design`、`Research`、`Analyze` 等职责

---

### 角色模板速查

| 维度 | A: 只读探索 | B: 读写实现 | C: 规划分析 |
|------|-----------|-----------|-----------|
| Read/Glob/Grep | ✅ | ✅ | ✅ |
| Write/Edit/multi_edit | ❌ | ✅ | ❌ |
| Bash | ❌ | ✅（仅构建命令） | ❌ |
| Question | ❌ | ❌ | ✅ |
| subagent_para/dag | ❌ | ❌ | ❌ |
| todowrite/todoread | ❌ | ❌ | ❌ |
| steps | 50 | 100 | 30 |

**所有模板共同的禁区**（自动处理，无需手动添加）：
- 不允许调用子智能体（`subagent_para`、`subagent_dag`）
- 不允许操作待办（`todowrite`、`todoread`）
- 不允许无限步骤（steps 有上限）

---

### 提示词正文规范

在 frontmatter 的 `---` 之后，用 Markdown 编写智能体的系统提示词，结构建议：

```markdown
# Role
你是一个 [角色描述]

## Core Responsibilities
1. ...
2. ...

## Workflow
### Step 1: ...
### Step 2: ...

## Quality Standards
- ...

## Output Format
...
```

### 参考范例

| 角色模板 | 参考现有智能体 |
|---------|---------------|
| A: 只读探索 | `plugins/system/agents/explore.md` — 只读代码探索 |
| B: 读写实现 | `plugins/system/agents/build.md` — 编码实现 |
| C: 规划分析 | `plugins/system/agents/plan.md` — 规划分析（含 Question 权限） |

### 注意事项
1. 文件创建到 `.drifox/plugins/user-custom/agents/` 后，watchfiles 会在 1-3 秒内自动热加载，无需手动重载
2. description 会被截断到 300 字符注入主智能体提示词，请在前 300 字符内传达核心信息，可含 `<example>` 标记增强触发精度
3. mode 说明：`subagent` = 仅作子智能体；`primary` = 仅作主智能体；`all` = 两者皆可
4. 提示词正文建议 500-2000 字，结构清晰，指令明确
5. 如果用户描述不够具体，请基于常识合理推断并继续执行，不要提问
6. 如果用户描述模糊无法判断角色模板，默认使用模板 A（只读探索）——安全优先于便利
<!-- end -->
