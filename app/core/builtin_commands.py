# -*- coding: utf-8 -*-
"""
内置命令注册

从 main_widget.py 拆出的独立模块，集中管理所有内置命令的注册。
包含 function 命令（new/new-window/branch）和 prompt 命令（init/review/theme），
以及从 app/agents/ 目录加载智能体命令的逻辑。

用法：
    from app.core.builtin_commands import register_all_commands
    register_all_commands()
"""

from pathlib import Path

from loguru import logger

from app.core.command_manager import CommandManager


# ============================================================
# Prompt 常量
# ============================================================

_INIT_PROMPT = (
    "请分析此代码库并编写项目笔记，包含以下内容：\n"
    "1. 构建/lint/测试命令 - 特别是运行单个测试的方法\n"
    "2. 代码风格规范，包括导入、格式化、类型、命名约定、错误处理等\n\n"
    "你创建的文件将被提供给在此仓库中操作的 AI 编码智能体（如你自己）。内容约 150 行。\n"
    "如果有 Cursor 规则（在 .cursor/rules/ 或 .cursorrules 中）"
    "或 Copilot 规则（在 .github/copilot-instructions.md 中），请确保包含它们。\n\n"
    "如果已有项目笔记，请改进它。"
)

_REVIEW_PROMPT = """You are a code reviewer. Your job is to review code changes and provide actionable feedback.
---
## Determining What to Review
Based on the input provided, determine which type of review to perform:
1. **No arguments (default)**: Review all uncommitted changes
   - Run: `git diff` for unstaged changes
   - Run: `git diff --cached` for staged changes
   - Run: `git status --short` to identify untracked (net new) files
2. **Commit hash** (40-char SHA or short hash): Review that specific commit
   - Run: `git show <hash>`
3. **Branch name**: Compare current branch to the specified branch
   - Run: `git diff <branch>...HEAD`
4. **PR URL or number** (contains "github.com" or "pull" or looks like a PR number): Review the pull request
   - Run: `gh pr view <number>` to get PR context
   - Run: `gh pr diff <number>` to get the diff
Use best judgement when processing input.
---
## Gathering Context
**Diffs alone are not enough.** After getting the diff, read the entire file(s) being modified to understand the full context. Code that looks wrong in isolation may be correct given surrounding logic—and vice versa.
- Use the diff to identify which files changed
- Use `git status --short` to identify untracked files, then read their full contents
- Read the full file to understand existing patterns, control flow, and error handling
- Check for existing style guide or conventions files (CONVENTIONS.md, AGENTS.md, .editorconfig, etc.)
---
## What to Look For
**Bugs** - Your primary focus.
- Logic errors, off-by-one mistakes, incorrect conditionals
- If-else guards: missing guards, incorrect branching, unreachable code paths
- Edge cases: null/empty/undefined inputs, error conditions, race conditions
- Security issues: injection, auth bypass, data exposure
- Broken error handling that swallows failures, throws unexpectedly or returns error types that are not caught.
**Structure** - Does the code fit the codebase?
- Does it follow existing patterns and conventions?
- Are there established abstractions it should use but doesn't?
- Excessive nesting that could be flattened with early returns or extraction
**Performance** - Only flag if obviously problematic.
- O(n²) on unbounded data, N+1 queries, blocking I/O on hot paths
**Behavior Changes** - If a behavioral change is introduced, raise it (especially if it's possibly unintentional).
---
## Before You Flag Something
**Be certain.** If you're going to call something a bug, you need to be confident it actually is one.
- Only review the changes - do not review pre-existing code that wasn't modified
- Don't flag something as a bug if you're unsure - investigate first
- Don't invent hypothetical problems - if an edge case matters, explain the realistic scenario where it breaks
- If you need more context to be sure, use the tools below to get it
**Don't be a zealot about style.** When checking code against conventions:
- Verify the code is *actually* in violation. Don't complain about else statements if early returns are already being used correctly.
- Some "violations" are acceptable when they're the simplest option. A `let` statement is fine if the alternative is convoluted.
- Excessive nesting is a legitimate concern regardless of other style choices.
- Don't flag style preferences as issues unless they clearly violate established project conventions.
---
## Tools
Use these to inform your review:
- **Explore agent** - Find how existing code handles similar problems. Check patterns, conventions, and prior art before claiming something doesn't fit.
- **Exa Code Context** - Verify correct usage of libraries/APIs before flagging something as wrong.
- **Exa Web Search** - Research best practices if you're unsure about a pattern.
If you're uncertain about something and can't verify it with these tools, say "I'm not sure about X" rather than flagging it as a definite issue.
---
## Output
1. If there is a bug, be direct and clear about why it is a bug.
2. Clearly communicate severity of issues. Do not overstate severity.
3. Critiques should clearly and explicitly communicate the scenarios, environments, or inputs that are necessary for the bug to arise. The comment should immediately indicate that the issue's severity depends on these factors.
4. Your tone should be matter-of-fact and not accusatory or overly positive. It should read as a helpful AI assistant suggestion without sounding too much like a human reviewer.
5. Write so the reader can quickly understand the issue without reading too closely.
6. AVOID flattery, do not give any comments that are not helpful to the reader. Avoid phrasing like "Great job ...", "Thanks for ..."."""

_THEME_PROMPT = """你是 DriFox 的 AI 主题设计师。你的任务是基于当前软件的主题系统，生成一套全新的、视觉上和谐统一的深色主题颜色方案，并保存为用户主题。

DriFox 的主题系统基于 YAML 文件，每个主题包含 `name`、`id`、`window`、`background`、`colors` 这几个顶层字段。颜色值使用 hex (`#rrggbb`) 或 rgba 格式。

以下是完整的主题结构说明（==代表你需要生成的值）：

---
## 主题结构

基本信息
```yaml
name: 主题显示名（中文，如"紫罗兰"）
id: 主题唯一 ID（小写英文，如 "violet"）
```

### window（窗口渐变背景）
- `gradient_start` / `gradient_end`: 窗口左上到右下的线性渐变，两个 rgba(...,255) 颜色

### background（背景图片，可选）
**背景图片可自定义，也可使用内置默认图片。**

默认使用内置图片（无需创建文件）：
```yaml
background:
  chat_list:
    image: :/icons/fox_bg.png
    opacity: 0.1
    enabled: true
```

如果用户想自定义背景图片：
1. 将图片放入主题文件夹（相对路径引用）：
   ```
   ~/.drifox/themes/{theme_id}/
   ├── {theme_id}.yaml
   └── user_bg.png        # 你的背景图片
   ```
2. 在 YAML 中引用：
   ```yaml
   background:
     chat_list:
       image: user_bg.png
       opacity: 0.15      # 可调整透明度
       enabled: true
   ```
3. 如果不想用背景图片：`enabled: false`

### colors（颜色系统）

#### 1. 基础色（由 accent 主导整个色彩方向）
- `accent`: **核心强调色**，是整个主题的"灵魂色"。选择一个饱和度适中、有辨识度的颜色（如金色、青色、玫红、翠绿等），所有其它颜色围绕它派生
- `accent_warm`: 暖色强调，通常偏橙黄，与 accent 互补
- `border`: 边框色，比背景稍亮，通常比 accent 暗许多
- `border_accent`: 带强调色的边框，在 accent 的基础上降低饱和度/提亮
- `text_primary`: 主文字色，接近白色 `#f3f6fc` 风格
- `text_secondary`: 次要文字，白色带透明度 `rgba(..., ..., ..., 0.7x)`
- `text_muted`: 弱化文字，较暗的灰色 `#xxxxxx` hex
- `card_bg`: 卡片背景色 `rgba(r, g, b, 230)` 半透明
- `card_bg_solid`: 卡片实色背景 `rgba(r, g, b, 250)`
- `content_bg`: 内容区纯色背景 `#xxxxxx`
- `hover_bg`: 悬停高亮半透明层 `rgba(r, g, b, 0.12)` — r,g,b 使用 accent 的颜色
- `selected_bg`: 选中高亮半透明层 `rgba(r, g, b, 0.32)` — 同上但透明度更高
- `capsule_bg`: 胶囊（标签）背景 `rgba(r, g, b, 180)`
- `capsule_border`: 胶囊边框 `rgba(r, g, b, 200)`

#### 2. 用户 / AI 卡片色（区分对话双方）
- `user_card_bg`: 用户消息卡片半透明背景 `rgba(..., ..., ..., 150)` — 偏蓝色调
- `user_card_accent`: 用户卡片强调色（较亮的蓝色系）
- `user_card_text`: 用户卡片文字（亮白）
- `user_card_muted`: 用户卡片次要文字
- `assistant_card_bg`: AI 消息卡片半透明背景 `rgba(..., ..., ..., 150)` — 偏暖色调
- `assistant_card_accent`: AI 卡片强调色（暖色，如橙/金）
- `assistant_card_text`: AI 卡片文字（暖白）
- `assistant_card_muted`: AI 卡片次要文字

#### 3. 智能体按钮
- `agent_btn_text`: 默认文字色（偏灰）
- `agent_btn_text_active`: 激活文字色（使用 accent 或同类亮色）
- `agent_btn_bg_active`: 激活背景 `rgba(accent_r, accent_g, accent_b, 0.2)`
- `agent_btn_separator`: 分隔线 `rgba(r, g, b, 150)`

#### 4. 输入框
- `input_bg_start` / `input_bg_end`: 输入框渐变背景（默认状态），较深 rgba(..., ..., ..., 150)
- `input_focus_bg_start` / `input_focus_bg_end`: 聚焦状态渐变背景，稍亮 rgba(..., ..., ..., 220)
- `input_text`: 默认文字色
- `input_focus_text`: 聚焦文字色（略亮）
- `input_border`: 默认边框色（暗）
- `input_focus_border`: 聚焦边框色（使用 accent）
- `input_placeholder`: 占位文字色 `rgba(r, g, b, 0.4)`

#### 5. 实时交互卡片
- `realtime_border`: 边框（用 accent 或相近色）
- `realtime_accent`: 强调色（accent 的亮化版）
- `realtime_accent_warm`: 暖色强调
- `realtime_success`: 成功绿（保持 `#34d399` 或类似）
- `realtime_error`: 错误红（保持 `#f87171` 或类似）
- `realtime_bg`: 背景 `rgba(r, g, b, 242)`
- `realtime_text`: 文字
- `realtime_text_secondary`: 次要文字 `rgba(r, g, b, 0.7)`
- `realtime_tag_bg`: 标签背景 `rgba(r, g, b, 0.15)` — r,g,b 来自 realtime_accent
- `realtime_tag_border`: 标签边框 `rgba(r, g, b, 0.3)`

#### 6. 系统卡片
- `system_border`: 系统卡片边框色
- `system_accent`: 系统卡片强调色

#### 7. 发送按钮渐变
- `send_btn_start` / `send_btn_end`: 正常状态渐变（使用 accent 及其变体）
- `send_btn_hover_start` / `send_btn_hover_end`: 悬停状态渐变（略亮）

#### 8. 时间线
- `timeline_node`: 节点默认色 `#5A5A5A`
- `timeline_node_hover`: 节点悬停（用 accent 或亮色）
- `timeline_node_visible`: 可见节点（用亮绿色系或 accent）
- `timeline_node_selected`: 选中节点 `#FFA500`（通常保持橙色）
- `timeline_line`: 连线默认 `#3A3A3A`
- `timeline_line_progress`: 进度连线（用 visible 相同颜色）

#### 9. 上下文圆环
- `ring_normal`: 正常色（用 accent 或类似）
- `ring_warning`: 警告黄 `#f6c453`
- `ring_danger`: 危险红 `#ff6b6b`
- `ring_compacted`: 压缩紫 `#9b59b6`

#### 10. 分支标签
- `branch_label_bg`: `rgba(accent_r, accent_g, accent_b, 0.15)`
- `branch_label_border`: `rgba(accent_r, accent_g, accent_b, 0.3)`
- `window_bg`: `rgba(accent_r, accent_g, accent_b, 0.04)`

---

## 设计原则
1. **色彩统一**: 选择一个主色调（accent），所有颜色围绕它派生产生，确保视觉一致性
2. **用户卡片偏冷色调**（蓝色系），AI 卡片**偏暖色调**（橙/金色系）— 这个对比结构请保持
3. **深色主题**：整体保持深色背景、亮色文字，rgba 半透明值用于卡片分层
4. **可读性优先**: text_primary 接近白色，text_secondary 适当降低透明度，确保对比度足够
5. **和谐过渡**: 各颜色之间的过渡平滑，避免突兀的色块

## 输出要求

1. 生成一个完整的 YAML 主题文件，包含以上所有字段
2. 保存主题文件：放到用户主题目录，路径为 `~/.drifox/themes/{theme_id}/{theme_id}.yaml`
   - 先创建目录 `~/.drifox/themes/{theme_id}/`
   - 再写入 YAML 文件
   - 如果目录已存在，覆盖写入
3. 主题名称要有创意且有意义，围绕一个色彩主题（如"紫罗兰"、"琥珀光"、"极光绿"等）
4. 完成后告知用户：主题已保存，在设置中选择该主题即可生效"""


# ============================================================
# 注册主入口
# ============================================================

def register_all_commands():
    """注册所有内置命令：function 命令 + prompt 命令 + agents 目录智能体"""
    cmd_mgr = CommandManager.get_instance()

    # 先清空，避免重复注册
    for name in list(cmd_mgr.get_command_names()):
        cmd_mgr.unregister(name)

    # ---- function 命令 ----
    cmd_mgr.register("new", "function", description="新建会话")
    cmd_mgr.register("new-window", "function", description="新建窗口")
    cmd_mgr.register("branch", "function", description="新建分支窗口")
    cmd_mgr.register("compact", "function",
                     description="手动触发上下文压缩（调用子智能体压缩当前对话摘要）")

    # ---- prompt 命令 ----
    cmd_mgr.register("init", "prompt",
                     description="项目笔记初始化",
                     prompt_text=_INIT_PROMPT)
    cmd_mgr.register("review", "prompt",
                     description="审查更改代码",
                     prompt_text=_REVIEW_PROMPT)
    cmd_mgr.register("theme", "prompt",
                     description="生成新主题颜色",
                     prompt_text=_THEME_PROMPT)

    # ---- agents 目录智能体 ----
    _register_builtin_agents_as_commands(cmd_mgr)


# ============================================================
# agents 目录加载
# ============================================================

def _register_builtin_agents_as_commands(cmd_mgr: CommandManager):
    """从 app/agents 目录加载内置智能体并注册为命令"""
    import yaml

    agents_dir = Path(__file__).parent.parent / "agents"
    if not agents_dir.exists():
        return

    for md_file in agents_dir.glob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            if not content.startswith("---"):
                continue

            parts = content.split("---", 2)
            if len(parts) < 3:
                continue

            frontmatter = parts[1]
            body = parts[2].strip()

            meta = yaml.safe_load(frontmatter)
            if not meta:
                continue

            description = meta.get("description", "")

            cmd_mgr.register(
                name=md_file.stem,
                command_type="prompt",
                description=description,
                prompt_text=body,
            )
            logger.info(f"[BuiltinCommands] Registered agent command: /{md_file.stem}")

        except Exception as e:
            logger.error(f"[BuiltinCommands] Failed to load agent {md_file}: {e}")
