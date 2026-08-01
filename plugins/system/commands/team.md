---
description: 多窗口团队协作管理
type: function
argument-hint:
  '[--join=]': '加入团队（如 --join=build 或 --join 弹出选择）'
  '[--leave]': '离开团队恢复独立模式'
  '[--save=]': '保存当前活跃窗口的 agent 列表为命名模板'
  '[--load=]': '加载模板（不指定名称时列出可用模板）；支持 UI 枚举选择'
  '[--delete=]': '删除模板（不指定名称时列出可用模板）'
  '[--create=]': '进入创建团队模板工作流，根据描述自动生成团队模板 yaml 文件'
mutex_groups:
  action: ['--join=', '--leave', '--save=', '--load=', '--delete=', '--create=']
prompt_sections:
  --create=: "create"
---

## 用户可见行为

### 加载团队模板（`--load=<name>`）

加载模板时**完全新建独立窗口**，已有标签页一律不动（不切换 agent、不改标题、不重新入队）：
- 模板 N 个角色 → 全部通过新建窗口承载，已有窗口保持原样
- 新建窗口走延迟 join team（`_join_new_window_for_template`），确保 backend 初始化完成后立即加入团队
- 新建窗口标题保持默认/会话标题，**不**被 agent 名覆盖

### Tab 标题与胶囊

- 团队模式下，Tab 标题**始终保持会话标题**（让 Windows 任务栏能区分各窗口），agent 名不显示在 Tab 标题上
- Tab 胶囊（角色 pill）单独显示 agent 名，与标题无关
  - 即使新建窗口的会话标题是默认 "飘狐"，加入团队后也会立即显示胶囊（不依赖标题变化触发）
  - 退出团队时立即清除胶囊
- Tab 面板把同团队的所有标签页用分组框（`#teamGroup`）圈出，独立窗口在框外
  - 加入团队时自动入框、退出团队时自动出框

<!-- section:create -->
## 任务：创建 DriFox 团队模板

请根据以下描述，创建一个新的团队模板 yaml 文件。

### 用户需求描述
$ARGUMENTS

### 输出要求
使用 `write` 工具创建文件，目标目录：
| 用户安装插件（打包环境） | `~/.drifox/plugins/user-custom/team_templates/<name>.yaml`（用 home 目录展开） |

文件名 `<name>` 请根据描述自动生成，规则：
- 使用英文小写 + 连字符（kebab-case）
- 2-4 个单词，如 `code-review-team`、`full-stack-dev`
- 清晰表达团队核心用途

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

#### 角色名来源

`agent_name` 请从你的系统提示词中 `## Available Subagents` 节列出的子智能体名称中选取。所有子智能体都可作为团队成员角色。

#### 角色描述（description）要求

每个 `agent` 条目必须带 `description` 字段（**必填**），内容从 `## Available Subagents` 节中对应子智能体的描述提取：
- 描述该角色在团队中的职责定位（如：编码实现、规划分析、代码审查）
- 简洁一句话，10-40 字为宜
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
2. `leader` 必须存在且位于 `agents` 第一位；不要创建系统中不存在的角色
3. `agents` 中角色名不能重复（schema 校验会拒绝重复项）
4. description 会显示在模板列表界面，请简洁清晰
5. 每个 `agent` 条目的 `description` 角色描述为必填项，从 Available Subagents 节对应描述提取
<!-- end -->
