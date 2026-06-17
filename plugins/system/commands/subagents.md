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
  --create=: |
    ## 任务：创建 DriFox 子智能体

    请根据以下描述，创建一个新的 DriFox 子智能体 md 文件。

    ### 用户需求描述
    $ARGUMENTS

    ### 输出要求

    使用 `write` 工具创建文件，目标目录：`.drifox/plugins/user-custom/agents/<agent-name>.md`

    文件名 `<agent-name>` 请根据描述自动生成，规则：
    - 使用英文小写 + 连字符（kebab-case）
    - 2-4 个单词，如 `code-reviewer`、`api-doc-generator`
    - 清晰表达智能体核心功能

    ### Frontmatter 规范（必须遵守）

    ```yaml
    ---
    description: <必填，包含触发条件和使用场景，可含 <example> 标记>
    mode: subagent
    hidden: false
    temperature: 0.2
    steps: 50
    permission:
      write: allow
      edit: allow
      multi_edit: allow
      bash: allow
      read: allow
      question: deny
      todowrite: deny
      todoread: deny
      subagent_para: deny
      subagent_dag: deny
      "*": allow
    ---
    ```

    关键字段说明：
    - **description**: 必须包含"何时触发"（如 "Use this agent when user asks to..."）+ 功能描述，可含 `<example>` 标记增强触发精度
    - **mode**: `subagent` = 仅作子智能体；`primary` = 仅作主智能体；`all` = 两者皆可
    - **permission**: 子智能体必须 deny: `question`, `subagent_para`, `subagent_dag`, `todowrite`, `todoread`
    - **tools** (可选): 白名单模式，如 `["Read", "Write", "Bash"]`，未列出工具自动拒绝；与 permission 互斥

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

    可参考以下现有智能体的结构：
    - `plugins/system/agents/explore.md` — 只读代码探索（Role + Hard Rules + Workflow）
    - `plugins/system/agents/build.md` — 编码实现（编码前思考 + 简洁优先 + 精准修改）
    - `plugins/system/agents/plan.md` — 规划分析（先 question 多步提问 + 强制调用 explore）

    ### 注意事项
    1. 文件创建到 `.drifox/plugins/user-custom/agents/` 后，watchfiles 会在 1-3 秒内自动热加载，无需手动重载
    2. description 会被截断到 300 字符注入主智能体提示词，请在前 300 字符内传达核心信息
    3. 提示词正文建议 500-2000 字，结构清晰，指令明确
    4. 如果用户描述不够具体，请基于常识合理推断并继续执行，不要提问
---