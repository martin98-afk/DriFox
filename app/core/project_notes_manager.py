# -*- coding: utf-8 -*-
"""
项目笔记文件管理器 - 基于 AGENTS.md

每个项目工作目录（含 worktree）独立存储一份 AGENTS.md：
- UI 编辑：实时去抖 300ms 保存
- AI 工具：edit_project_note 用 oldString/newString 替换
- LLM 上下文：从当前 workdir 的 AGENTS.md 注入
- 首次访问：从旧 SQLite 迁移内容，否则写入默认模板
"""

from pathlib import Path
from typing import Dict, Optional
from loguru import logger


# 默认模板（与原 SQLite get_or_create 一致）
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


# 记录已迁移的 (workdir, project)，避免重复从 SQLite 读取
_MIGRATED: set = set()


def get_notes_path(workdir: str) -> Optional[Path]:
    """获取项目笔记文件路径"""
    if not workdir:
        return None
    return Path(workdir) / "AGENTS.md"


def get_note(workdir: str, project: str) -> Optional[Dict]:
    """读取项目笔记，文件不存在则返回 None"""
    path = get_notes_path(workdir)
    if not path or not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        return {
            "project": project,
            "content": content,
            "path": str(path),
        }
    except Exception as e:
        logger.error(f"[ProjectNotes] 读取 {path} 失败: {e}")
        return None


def get_or_create_note(
    workdir: str,
    project: str,
    legacy_repo=None,
) -> Dict:
    """读取或创建项目笔记

    - 文件存在：直接读取
    - 文件不存在 + 未尝试迁移：从 SQLite 迁移，否者用默认模板
    - 文件不存在 + 已尝试迁移：用默认模板
    """
    path = get_notes_path(workdir)
    if not path:
        return {"project": project, "content": "", "path": ""}

    if not path.exists():
        if (workdir, project) not in _MIGRATED:
            _MIGRATED.add((workdir, project))
            content = _try_migrate(legacy_repo, project) or INITIAL_TEMPLATE
        else:
            content = INITIAL_TEMPLATE
        _write_file(path, content)

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"[ProjectNotes] 读取 {path} 失败: {e}")
        content = ""

    return {
        "project": project,
        "content": content,
        "path": str(path),
    }


def save_note(workdir: str, project: str, content: str) -> bool:
    """保存项目笔记（直接写文件）"""
    path = get_notes_path(workdir)
    if not path:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        logger.error(f"[ProjectNotes] 写入 {path} 失败: {e}")
        return False


def _try_migrate(legacy_repo, project: str) -> Optional[str]:
    """从旧 SQLite 读取一次性迁移内容"""
    if not legacy_repo:
        return None
    try:
        note = legacy_repo.get(project)
        if note and note.get("content"):
            return note["content"]
    except Exception as e:
        logger.warning(f"[ProjectNotes] SQLite 迁移失败 ({project}): {e}")
    return None


def _write_file(path: Path, content: str) -> bool:
    """原子写入文件"""
    try:
        path.write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        logger.error(f"[ProjectNotes] 写入 {path} 失败: {e}")
        return False
