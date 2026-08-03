---
description: 多窗口团队协作管理
type: function
argument-hint:
  '[--load=]': '加载模板（不指定名称时列出可用模板）；支持 UI 枚举选择'
  '[--create=]': '进入创建团队模板工作流，根据描述自动生成团队模板 yaml 文件'
  '[--delete=]': '删除模板（不指定名称时列出可用模板）'
  '[--save=]': '保存当前活跃窗口的 agent 列表为命名模板'
  '[--join=]': '加入团队（如 --join=build 或 --join 弹出选择）'
  '[--leave]': '离开团队恢复独立模式'
mutex_groups:
  action: ['--join=', '--leave', '--save=', '--load=', '--delete=', '--create=']
prompt_sections:
  --create=: "create"
  --load=: "load_missing"
---

## 子智能体创建规范（公共，所有 section 共享）

> 以下规范被 `create` 与 `load_missing` 两个 section 共同引用，
> 置于 section 之外保证 `select_prompt` 过滤后始终保留（所有命令注入时都能看到）。

### 1. 完整骨架模板（写入 `~/.drifox/plugins/user-custom/agents/<role>.md`）

**Frontmatter**（变量替换 `<role>` 为 kebab-case 角色名，`<description>` 为 30-80 字角色定位）：

```yaml
---
description: <从用户描述推断的一句话角色定位，30-80 字>
mode: subagent
hidden: false
temperature: 0.3
steps: 50
permission:
  # 见下方「权限推导规则」
  "<按权限推导规则填充>"
---
```

**正文**：

```markdown
# Role
你是一个专业的 <角色中文名>，负责 <一句核心职责>。

# Core Capabilities
- <能力 1>
- <能力 2>
- <能力 3>

# Workflow
1. <接收输入>
2. <执行步骤>
3. <输出交付物>

# Hard Rules
- <红线 1>
- <红线 2>
- <红线 3>
```

### 2. 写文件前置步骤

先用 `bash` 工具确保目录存在：

```bash
mkdir -p ~/.drifox/plugins/user-custom/agents/
```

然后对每个确认新建的 role，用 `write` 工具写入完整骨架。

### 3. 权限推导规则（AI 自动判定，无需问用户）

按角色描述中的关键词匹配，4 个模板任选其一：

| 角色关键词（中文/英文） | permission 模板 | 典型场景 |
|---|---|---|
| 含 `测试`/`运行`/`部署`/`压测`/`runner`/`tester` | `bash: allow`、其余 `ask` | perf-tester, qa-runner |
| 含 `审查`/`分析`/`审计`/`只读`/`reviewer`/`analyzer`/`auditor` | `write`/`edit`/`multi_edit`: `deny`，其余 `*`: allow | perf-analyzer, security-auditor |
| 含 `写入`/`修改`/`迁移`/`重构`/`writer`/`migrator`/`refactorer` | `*: allow` | db-migrator, refactorer |
| 含 `协调`/`统筹`/`PM`/`scrum`/`master`/`coordinator` | `question`/`team_*`: `allow`，`write`/`edit`/`multi_edit`: `deny`，其余 `*`: allow | scrum-master, pm-bot |

**不匹配的兜底**（默认）：`write`/`edit`/`multi_edit`/`bash`: `ask`，`*: allow`（与 `plan.md` 模板一致，最保守安全）。

### 4. 文件路径约定

| 类型 | 路径 |
|---|---|
| 新建智能体 | `~/.drifox/plugins/user-custom/agents/<role>.md` |
| 团队模板 yaml | `~/.drifox/plugins/user-custom/team_templates/<name>.yaml` |

`<role>`：kebab-case，2-4 单词（与 Available Subagents 命名风格一致，如 `perf-tester`）。
`<name>`：kebab-case，2-4 单词，表达团队核心用途（如 `perf-test-team`）。

### 5. 新建智能体的加载时机（必须在写入后告知用户）

> ⚠️ 本次流程仅创建了 `.md` 文件，新角色**不会**自动加入当前会话的 `Available Subagents`。
> - 下次 `/team --load=<name>` 时若 watchfile 重载已完成，新角色立即可用
> - 若 watchfile 未触发（罕见），重启 DriFox 后可用
> - 当前会话内不能用 `subagent_para` 调起新角色

<!-- section:create -->
## 任务：创建 DriFox 团队模板

请根据以下描述，创建一个新的团队模板 yaml 文件。如果所需的角色不在 Available Subagents 中，**允许在 user-custom/agents/ 下新建智能体**（详见下方 A-F 规则与上方「子智能体创建规范」）。

### 用户需求描述
$ARGUMENTS

### 输出要求

#### A. 角色检测总则

1. **解析用户描述** → 提取所需角色列表（含 `leader` 必须为第一位）
2. **对比 `## Available Subagents` 节** → 把每个角色标记为：
   - ✅ 已有（Available Subagents 中存在）→ 直接进 yaml
   - ❌ 缺失（Available Subagents 中不存在）→ 进入步骤 B
3. 在聊天内输出"团队成员检测结果"表格（模板见步骤 F）

#### B. 缺失角色处理流程

对每个 ❌ 缺失角色，**逐个**用 `question` 工具弹窗确认：

```
question: 缺失智能体 "<role>"，是否在 user-custom/agents/ 新建？
  选项：
    1. 新建并写入完整骨架（推荐）
    2. 用已有角色 <existing_role> 替代
    3. 跳过，不创建
    4. 改名（输入新名字后重新比对）
```

- **选项 1**：按上方「子智能体创建规范」第 1/2 节，用 `write` 工具写入完整骨架，记录为 🆕 已创建
- **选项 2**：从 Available Subagents 中选最相似的 1-2 个作为替换，记录为 ✅ 已有（替代）
- **选项 3**：记录为 ⏭️ 已跳过，**不写入 yaml**
- **选项 4**：用户输入新名 → 回到步骤 A-2 重新比对 → 重复 B 流程

#### C. 失败与边界处理

| 场景 | 处理 |
|---|---|
| 用户跳过所有缺失角色 | 警告"团队不完整"，仍生成 yaml（不含跳过角色），提示用户后续手动补 |
| 角色名与已有冲突 | 用 question 弹窗提供候选改名（如 `<role>-2`、`my-<role>`），用户选其一或手动输入；新名不能与 Available Subagents 已有角色重名 |
| `mkdir` 失败 | 弹窗提示手动执行命令，提供复制粘贴命令 |
| `write` 失败 | 同上，附带失败原因 |
| 用户描述过短无法解析角色 | 用 question 反问"请补充团队核心职责"，最多 2 轮澄清后兜底（按通用 4 角色 `leader`+`plan`+`build`+`code-reviewer` 模板生成 yaml） |

#### D. 聊天内可视化（markdown 表格，仅 LLM 输出，无外部依赖）

**D-1. 检测阶段输出**（步骤 A 完成后立即输出）：

```markdown
## 团队成员检测结果

| 角色 | 状态 | 来源 | 说明 |
|---|---|---|---|
| leader | ✅ 已有 | Available Subagents | 团队 Leader |
| build | ✅ 已有 | Available Subagents | 编码实现 |
| perf-tester | ❌ 缺失 | 将新建 | 压测执行 |
| perf-analyzer | ❌ 缺失 | 将新建 | 压测分析 |
```

**D-2. 交付阶段输出**（yaml 写入完成后立即输出）：

```markdown
## 交付清单

| 角色 | 状态 | 路径 |
|---|---|---|
| leader | ✅ 已有 | — |
| build | ✅ 已有 | — |
| perf-tester | 🆕 已创建 | `~/.drifox/plugins/user-custom/agents/perf-tester.md` |
| perf-analyzer | ⏭️ 已跳过 | — |

**团队模板**：`~/.drifox/plugins/user-custom/team_templates/perf-test-team.yaml`
**下一步**：`/team --load=perf-test-team`
```

**状态 emoji 对照**：`✅` 已有 / `❌` 缺失（待确认）/ `🆕` 已创建 / `⏭️` 已跳过。

**重要提示**（必须在 D-2 之后追加告知用户，见上方「子智能体创建规范」第 5 节加载时机说明）。

### 模板格式规范

模板使用 YAML 格式，严格遵循以下结构：

```yaml
schema_version: 1
template_name: <与文件名 stem 一致>
description: <一句话描述团队用途>
agents:
  - agent_name: leader      # 固定第一位：团队 Leader，统筹任务拆解/分发/汇总
    description: <leader 的角色描述，从 Available Subagents 节中提取>
  - agent_name: <角色名2>   # 其余成员按需选择
    description: <该角色的描述>
  - agent_name: <角色名3>
    description: <该角色的描述>
  ...
```

#### 角色名来源（已扩展）

`agent_name` 来源**优先**从你的系统提示词中 `## Available Subagents` 节列出的子智能体名称中选取。

**如果某个所需角色不在 Available Subagents 中**，遵循上方「子智能体创建规范」在 `~/.drifox/plugins/user-custom/agents/` 创建该角色的智能体定义（仅用户确认后才创建）。

所有 Available Subagents 列出的子智能体 + 本流程新建的角色都可作为团队成员角色。

#### 角色描述（description）要求

每个 `agent` 条目必须带 `description` 字段（**必填**）：
- Available Subagents 中已有的角色 → 从 `## Available Subagents` 节对应描述提取
- 本流程新建的角色 → 从你刚才写入的 `.md` 的 `description` frontmatter 字段提取
- 注入团队上下文时每个成员只收到自己的角色描述，所以描述要能让成员明确自身职责

#### 固定 Leader 规则

- **必须**包含 `leader` 角色，且置于 `agents` 列表**第一位**，负责统筹管理（任务拆解、分发、进度监控、结果汇总）
- 其余成员**按需**选择，可 1 个或多个；无特殊需求时无需重复添加

#### 选择建议

- **标准开发团队**：`leader` + `plan` + `build` + `code-reviewer`（统筹→规划→编码→审查）
- **只读分析团队**：`leader` + `explore` + `code-reviewer`（统筹→探索→审查）
- 含 `leader` 在内 2-5 个角色为宜，按执行顺序排列
- 如果用户描述不够具体，请基于常识合理推断

### 注意事项
1. 文件创建到 `.drifox/plugins/user-custom/team_templates/` 后，可通过 `/team --load=<name>` 立即加载使用
2. `leader` 必须存在且位于 `agents` 第一位
3. `agents` 中角色名不能重复（schema 校验会拒绝重复项，包括与 Available Subagents 已有角色重名）
4. description 会显示在模板列表界面，请简洁清晰
5. 每个 `agent` 条目的 `description` 角色描述为必填项
6. 缺失角色的 `.md` 必须先写入（上方「子智能体创建规范」第 1/2 节），再写入 yaml，否则 yaml 引用未存在的角色
<!-- end -->

<!-- section:load_missing -->
## 任务：补全 /team --load 缺失的子智能体

用户执行 `/team --load=<name>` 时检测到模板 `<name>` 包含系统中不存在的角色。请逐个用 `question` 工具询问用户对每个缺失角色的处理方式，最后提示用户重新执行 `/team --load=<name>` 完成加载。

### 用户请求
$ARGUMENTS

- `--load=<name>`：要加载的模板名（与缺失角色来自同一模板）
- `缺失角色: <role_a>, <role_b>, ...`：从 `template.validate_agent_names(available_names)` 解析出的角色列表
- 模板名只是上下文回显，**真正关键的是缺失角色名单**，必须严格按 `缺失角色:` 后的逗号分隔列表逐个处理

### 操作流程

#### 1. 解析缺失角色
从 `$ARGUMENTS` 的 `缺失角色:` 段解析出每个角色名（如 `perf-tester`、`perf-analyzer`），形成有序列表。

#### 2. 逐个用 `question` 工具询问

```
question: 缺失智能体 "<role>"，是否在 user-custom/agents/ 新建？
  选项：
    1. 新建并写入完整骨架（推荐）
    2. 用已有角色 <existing_role> 替代
    3. 跳过，不创建
    4. 改名（输入新名字后重新比对）
```

- **选项 1**：按上方「子智能体创建规范」第 1/2 节，用 `write` 工具写入完整骨架到 `~/.drifox/plugins/user-custom/agents/<role>.md`，记录为 🆕 已创建
- **选项 2**：从 Available Subagents 中选最相似的 1-2 个作为替换，记录为 ✅ 已有（替代）
- **选项 3**：记录为 ⏭️ 已跳过，**不写入 yaml / .md**（跳过不是改名，用户接受该角色不在模板中）
- **选项 4**：用户输入新名 → 回到步骤 2 重新比对 → 重复本流程

#### 3. 失败与边界处理

| 场景 | 处理 |
|---|---|
| 用户跳过所有缺失角色 | 警告"模板无法加载（关键角色全部缺失）"，提示用户先手动新建后再 `/team --load=<name>` |
| 角色名与已有冲突 | 用 question 弹窗提供候选改名（如 `<role>-2`、`my-<role>`），用户选其一或手动输入 |
| `mkdir` 失败 | 弹窗提示手动执行命令，提供复制粘贴命令 |
| `write` 失败 | 同上，附带失败原因 |
| `缺失角色:` 段解析失败 | 用 question 反问"请提供缺失角色名清单（逗号分隔）"，最多 2 轮澄清后兜底（提示用户手动检查模板文件） |

#### 4. 补全完成提示

所有缺失角色处理完后，必须追加告知用户：

```markdown
## 补全完成

| 角色 | 状态 | 路径 |
|---|---|---|
| <role_a> | 🆕 已创建 | `~/.drifox/plugins/user-custom/agents/<role_a>.md` |
| <role_b> | ✅ 已有（替代） | — |
| <role_c> | ⏭️ 已跳过 | — |

**下一步**：再次执行 `/team --load=<name>` 完成加载（新建的角色已被 watchfile 重载）。
```

> ⚠️ 新建智能体的加载时机说明见上方「子智能体创建规范」第 5 节。
<!-- end -->
