# -*- coding: utf-8 -*-
"""
任务工具集 - 提供待办事项和技能管理功能

支持：
- 待办事项：add_todo, update_todo, delete_todo, todowrite, todoread
- 技能管理：load_skill, execute_skill, list_skills
- 批处理任务：task_execute_batch
"""
import os
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from app.tools.result import ToolResult
from app.utils.utils import list_skills_with_intro
from app.utils.utils import load_skill as utils_load_skill


class TaskTools:
    def __init__(self, owner):
        self._owner = owner
        self._todo_list: List[Dict] = []
        self._loaded_skills: Dict[str, str] = {}
        self._skill_workspaces: Dict[str, str] = {}
        self._sub_agent_manager = None
        self._set_stage_callback = None
        self._key_documents_repo = None  # 关键文档仓储
        self._current_project = "默认项目"  # 当前项目（由 set_current_project() 设置）

    @property
    def workdir(self) -> Path:
        return self._owner.workdir

    def _normalize_todos(self, todos: List[Dict]) -> List[Dict]:
        normalized: List[Dict] = []
        for item in todos or []:
            if not isinstance(item, dict):
                continue
            # Normalize keys and values to match UI expectations.
            lower_item = {str(k).lower(): v for k, v in item.items()}
            status = str(lower_item.get("status", "")).lower()
            priority = str(lower_item.get("priority", "medium")).lower()
            # content 字段兼容 description/text/name 等常见别名
            content = lower_item.get("content") or lower_item.get("description") or ""
            normalized.append(
                {
                    "id": lower_item.get("id"),
                    "content": content,
                    "status": status or "pending",
                    "priority": priority or "medium",
                }
            )
        return normalized

    def todo_write(self, todos: List[Dict]) -> ToolResult:
        try:
            self._todo_list = self._normalize_todos(todos)
            return ToolResult(True, content=f"Todo list updated: {len(todos)} items")
        except Exception as e:
            return ToolResult(False, error=f"Todo write error: {str(e)}")

    def todo_clear(self) -> None:
        self._todo_list = []

    def todo_read(self) -> ToolResult:
        try:
            if not self._todo_list:
                return ToolResult(True, content="No todos")

            lines = []
            for i, todo in enumerate(self._todo_list, 1):
                status = todo.get("status", "")
                if status == "completed":
                    status_icon = "✓"
                elif status == "in_progress":
                    status_icon = "▶"
                else:
                    status_icon = "○"
                content = todo.get("content", "")
                priority = todo.get("priority", "medium")
                lines.append(f"{i}. [{priority}] {status_icon} {content}")

            return ToolResult(True, content="\n".join(lines))
        except Exception as e:
            return ToolResult(False, error=f"Todo read error: {str(e)}")

    def subagent_para_execute(
            self, tasks: List[Dict], share_context: bool = False,
            session_id: str = "", sub_agent_manager=None
    ) -> ToolResult:
        """
        批量分发多个子智能体任务（并行执行）。

        Args:
            tasks: List[Dict], 每个任务包含:
                - agent: str 子智能体名称
                - description: str 任务描述
                - context: str (可选) 父任务上下文
            share_context: bool 是否共享主智能体上下文给子智能体
            session_id: 当前会话 ID（由 ToolExecutor 传入，用于会话隔离）
            sub_agent_manager: 子智能体管理器实例。传入时优先使用，解决多窗口闭包问题。

        Returns:
            ToolResult: success=True, content={"task_ids": [str], "status": "running"}
        """
        try:
            # 优先使用传入的 sub_agent_manager（多窗口隔离），回退到共享实例
            manager = sub_agent_manager or self._sub_agent_manager
            if not manager:
                return ToolResult(False, error="子智能体管理器未初始化")

            if not tasks:
                return ToolResult(False, error="任务列表为空")

            import uuid

            task_ids = []
            for task_item in tasks:
                agent = task_item.get("agent", "")
                description = task_item.get("description", "")
                context = task_item.get("context", "")

                if not agent or not description:
                    continue

                task_id = str(uuid.uuid4())
                manager.execute_task(
                    task_id=task_id,
                    agent_name=agent,
                    task_description=description,
                    parent_context=context or "",
                    on_finished=None,
                    on_error=None,
                    executor_ref=None,
                    share_context=share_context,
                    session_id=session_id,
                )
                task_ids.append(task_id)

            return ToolResult(
                True,
                content={
                    "task_ids": task_ids,
                    "status": "running",
                    "count": len(task_ids),
                },
            )

        except Exception as e:
            logger.error(f"[Task] subagent_para_execute exception: {e}")
            return ToolResult(False, error=f"批量任务启动失败: {str(e)}")

    def subagent_dag_execute(
            self, nodes: List[Dict], edges: List[Dict],
            session_id: str = "", sub_agent_manager=None
    ) -> ToolResult:
        """
        执行 DAG 工作流（同步）。按拓扑排序分批并行执行节点，
        下游节点自动获取上游节点结果。

        Args:
            nodes: List[Dict], 每个节点包含:
                - id: str 节点唯一标识
                - agent: str 子智能体名称
                - description: str 任务描述
                - context: str (可选) 额外上下文
            edges: List[Dict], 依赖关系 [{"from": "id1", "to": "id2"}]
            session_id: 当前会话 ID
            sub_agent_manager: 子智能体管理器实例

        Returns:
            ToolResult: success=True, content={"nodes": [{"id": str, "status": str, "result": str, "error": str}]}
        """
        manager = sub_agent_manager or self._sub_agent_manager
        if not manager:
            return ToolResult(False, error="子智能体管理器未初始化")

        if not nodes:
            return ToolResult(False, error="节点列表为空")

        try:
            # 委托给 SubAgentManager 执行 DAG
            return manager.execute_dag(
                nodes=nodes,
                edges=edges,
                session_id=session_id,
            )
        except Exception as e:
            logger.error(f"[Task] subagent_dag_execute exception: {e}")
            return ToolResult(False, error=f"DAG 工作流执行失败: {str(e)}")

    def subagent_status(self, task_ids: str = None, with_log: bool = False, with_result: bool = True,
                     session_id: str = "", sub_agent_manager=None) -> ToolResult:
        """
        查询任务状态。

        Args:
            task_ids: 任务ID列表，用逗号分隔。None或空=查询所有活跃任务
            with_log: 是否包含执行日志（默认 False）
            with_result: 是否包含执行结果（默认 True）
            session_id: 当前会话 ID（由 ToolExecutor 传入，用于会话隔离）
            sub_agent_manager: 子智能体管理器实例。传入时优先使用，解决多窗口闭包问题。

        Returns:
            ToolResult: success=True, content={"tasks": [{"task_id": str, "status": str, "agent": str, "result"?: str, "logs"?: [...]}]}
        """
        # 优先使用传入的 sub_agent_manager（多窗口隔离），回退到共享实例
        manager = sub_agent_manager or self._sub_agent_manager
        if not manager:
            return ToolResult(False, error="子智能体管理器未初始化")

        # 解析任务ID：支持字符串（逗号分隔）或列表格式
        if task_ids:
            if isinstance(task_ids, list):
                # 直接是列表
                id_list = [str(tid).strip() for tid in task_ids if tid]
            else:
                # 字符串格式：逗号分隔
                id_list = [tid.strip() for tid in str(task_ids).split(",") if tid.strip()]
            return manager.get_tasks_status_with_details(id_list, with_log, with_result, session_id=session_id)
        else:
            return manager.get_all_active_tasks_with_details(with_log, with_result, session_id=session_id)

    def load_skill(self, name: str) -> ToolResult:
        """加载指定技能"""
        success, content, workspace = utils_load_skill(name)
        
        if success:
            self._loaded_skills[name] = content
            self._skill_workspaces[name] = workspace
            return ToolResult(
                True,
                content=f"Skill loaded: {name}\n\nSkill workspace: {workspace}\n\n{content}",
            )
        else:
            return ToolResult(False, error=content)

    def list_skills(self) -> ToolResult:
        """获取所有技能列表"""
        try:
            content = list_skills_with_intro()
            return ToolResult(True, content=content)
        except Exception as e:
            return ToolResult(False, error=f"List skills error: {str(e)}")

    def scan_repo(self, path: str = None, max_depth: int = 2) -> ToolResult:
        import os as _os

        try:
            target_path = self._resolve_path(path) if path else self.workdir
            if not target_path.exists():
                return ToolResult(False, error=f"Path not found: {target_path}")

            try:
                scan_display = str(target_path.relative_to(self.workdir))
            except ValueError:
                scan_display = str(target_path)
            lines = [f"Repository scan: {scan_display}"]
            root_depth = len(target_path.parts)

            for root, dirs, files in _os.walk(target_path):
                rel_depth = len(Path(root).parts) - root_depth
                if rel_depth > max_depth:
                    dirs[:] = []
                    continue

                dirs[:] = [
                    d
                    for d in dirs
                    if d not in {'.mypy_cache', '.git', 'node_modules', '__pycache__', 'venv', '.venv',
                                 'dist', 'build', '.idea', '.vscode'}
                ]
                rel_root = Path(root).relative_to(target_path)
                display_root = "." if str(rel_root) == "." else str(rel_root)
                lines.append(f"\n[{display_root}]")

                sample_dirs = sorted(dirs)[:8]
                sample_files = sorted(files)[:12]
                if sample_dirs:
                    lines.append("dirs: " + ", ".join(sample_dirs))
                if sample_files:
                    lines.append("files: " + ", ".join(sample_files))

            return ToolResult(True, content="\n".join(lines[:200]))
        except Exception as e:
            return ToolResult(False, error=f"scan_repo error: {str(e)}")

    def stage_files(self, files: List[str]) -> ToolResult:
        try:
            staged = []
            for file_path in files or []:
                if not file_path:
                    continue
                resolved = self._resolve_path(file_path)
                try:
                    display = str(resolved.relative_to(self.workdir))
                except ValueError:
                    display = str(resolved)
                staged.append(display)
                
                # 自动关联到关键文档
                if self._key_documents_repo:
                    self._key_documents_repo.add(
                        self._current_project,
                        str(resolved),
                        added_by="stage_files"
                    )
            
            if not staged:
                return ToolResult(True, content="No files staged")
            
            # 添加关键文档关联提示
            if self._key_documents_repo:
                return ToolResult(True, content="Staged files:\n" + "\n".join(staged) + f"\n\n[已关联 {len(staged)} 个文件到项目「{self._current_project}」的关键文档]")
            
            return ToolResult(True, content="Staged files:\n" + "\n".join(staged))
        except Exception as e:
            return ToolResult(False, error=f"stage_files error: {str(e)}")

    def ask_question(
            self, questions: List[Dict] = None, **kwargs
    ) -> ToolResult:
        """向用户提问，支持一次问多个问题
        
        Args:
            questions: 问题列表，每个元素为 {"question": str, "options": [...], "multiple": bool}
                       其中 options 的每个元素为 {"label": str, "description": str}
        """
        # 向后兼容旧格式
        if not questions and "question" in kwargs:
            questions = [{
                "question": kwargs["question"],
                "options": kwargs.get("options", []),
                "multiple": kwargs.get("multiple", False),
            }]
        return ToolResult(
            True,
            content={
                "questions": questions or [],
                "type": "question",
            },
        )

    def _resolve_path(self, path: Optional[str]) -> Path:
        if not path:
            return self.workdir
        try:
            expanded = os.path.expandvars(path)
            if expanded != path:
                path = expanded
            p = Path(path)
            if p.is_absolute():
                return p.resolve()
            else:
                return (self.workdir / p).resolve()
        except (ValueError, OSError, RuntimeError) as e:
            logger.warning(f"[TaskTools] Failed to resolve path {path}: {e}")
            return self.workdir

    def reset_session_state(self):
        """Reset session-scoped state when switching sessions"""
        self._todo_list = []
        self._loaded_skills = {}
        self._skill_workspaces = {}
