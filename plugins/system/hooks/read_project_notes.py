# -*- coding: utf-8 -*-
"""
BuildSystemPrompt Hook 函数 — 从项目目录读取 AGENTS.md 注入系统提示词

不再依赖 context_builder 预取 notes 内容传参，改为直接从本地 {workdir}/AGENTS.md
读取，文件不存在时用默认模板创建。所有项目笔记相关逻辑集中在 hook 中处理。

"新建项目 → 首条消息 → BuildSystemPrompt hook → 读取/创建 AGENTS.md"，
不再在 backend/context_builder 代码中硬编码笔记内容。
"""

from pathlib import Path

from loguru import logger

INITIAL_TEMPLATE = """# 项目开发规范

本文件为 AI Agent 提供项目操作手册与约束清单，确保 Agent 行为可控、可复现。

---

## 1. 目标与边界

### 允许的操作
- **有关键文档存在时，优先以关键文档作为项目路径进行探索**
- 读取、修改顶层文档：`README.md`、`AGENTS.md`、`CONTRIBUTING.md` 等
- 读取、修改 `docs/`、`prompts/`、`skills/`、`tools/config/`、`tools/external/` 下的文档与代码
- 执行项目规定的 lint、检查、构建命令
- 新增/修改功能、修复问题
- 提交符合规范的 commit

### 禁止的操作
- 修改 `.github/workflows/` 中的 CI 配置（除非任务明确要求）
- 修改 `LICENSE`、`CODE_OF_CONDUCT.md`
- 在代码中硬编码密钥、Token 或敏感凭证
- 未经确认的大范围重构

### 敏感区域（禁止自动修改）
- `.github/workflows/*.yml` - CI/CD 配置
- `.env*` 文件（如存在）

---

## 2. 推荐执行路径

```bash
# 1. 拉取最新代码
git pull --rebase origin develop

# 2. 初始化依赖（如有需要）
# ... 项目特有命令

# 3. 运行 lint 检查
# ... 项目特有命令

# 4. 执行修改任务
# ...

# 5. 再次验证
# ... 项目特有检查命令

# 6. 提交变更
git add -A
git commit -m "feat|fix|docs|chore: scope - summary"
git push origin develop
```

---

## 3. 修改约束

### 架构原则
- 保持根目录扁平，避免巨石文件
- 遵循项目现有架构，不随意改动

### 禁止行为
- 禁止"顺手重构/大范围改动"除非任务明确要求
- 禁止删除现有测试用例（除非任务要求）
- 禁止在代码中硬编码敏感信息

---

## 4. 风格与质量标准

### 格式化工具
- 遵循项目现有代码风格
- 使用项目已有的格式化工具

### 命名约定
- 文档、注释、日志使用中文
- 代码符号统一英文且语义直白
- 文件名小写加中划线或下划线（遵循现有风格）

### 设计品味
- 优先消除分支与重复
- 函数单一职责且短小

---

## 5. 提交规范

遵循简化 Conventional Commits：
```
feat|fix|docs|chore|refactor|test: scope - summary
```

---

## 6. 强制同步规则

**任何功能/命令/配置/目录/工作流变化必须同步更新相关文档**

不确定的内容用 TODO 标注，不允许猜测。
"""


def hook(event: str, context: dict) -> str:
    """从本地 AGENTS.md 读取项目笔记并格式化为系统提示词

    Args:
        event: 事件名称（BuildSystemPrompt）
        context: 由 get_agent_system_prompt() 构建的上下文，含：
            - project_root: str 项目工作目录
            - project_name: str 项目名称

    Returns:
        格式化的项目笔记字符串，无项目时返回空字符串
    """
    workdir = context.get("project_root", "")
    project_name = context.get("project_name", "")

    if not workdir:
        # compaction/subagent/gateway 等无真实项目上下文的情况，跳过注入
        logger.warning(f"[ProjectNotesHook] 无项目工作目录，跳过注入项目笔记: project_name={project_name}")
        return INITIAL_TEMPLATE

    agents_path = Path(workdir) / "AGENTS.md"
    notes_content = ""

    if agents_path.exists():
        try:
            notes_content = agents_path.read_text(encoding="utf-8")
            logger.info(f"[ProjectNotesHook] 已读取项目笔记文件: {agents_path}")
        except Exception as e:
            logger.error(f"[ProjectNotesHook] 读取 {agents_path} 失败: {e}")
            notes_content = ""
    else:
        # 文件不存在 → 用默认模板创建（新建项目首次运行此路径）
        try:
            agents_path.parent.mkdir(parents=True, exist_ok=True)
            agents_path.write_text(INITIAL_TEMPLATE, encoding="utf-8")
            logger.info(f"[ProjectNotesHook] 已创建项目笔记文件: {agents_path}")
            notes_content = INITIAL_TEMPLATE
        except Exception as e:
            logger.error(f"[ProjectNotesHook] 创建 {agents_path} 失败: {e}")

    if notes_content:
        return f"## 项目笔记\n[当前项目: {project_name}]\n{notes_content}"
    else:
        return f"## 项目笔记\n[当前项目: {project_name}]\n（项目笔记为空）"
