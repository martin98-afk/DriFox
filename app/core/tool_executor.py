# -*- coding: utf-8 -*-
"""
工具执行器模块 - 统一处理各种工具调用
"""
import os
import orjson
import re
from pathlib import Path

from loguru import logger
from typing import Dict, Optional, Callable

from app.core.hook_manager import HookDecision

# 预编译正则表达式
_FILE_PREFIX_PATTERN = re.compile(r'^file:/{1,3}')

from app.tools import BuiltinTools, ToolResult
from app.utils.file_operation_recorder import (
    FileOperationRecorder,
)


class ToolExecutor:
    """工具执行器 - 统一调度各种工具"""

    # 需要记录的文件操作
    _FILE_OPS_TO_TRACK = {
        "write", "edit", "multi_edit"
    }

    def __init__(self, homepage=None, workdir: str = None, backend=None):
        self._homepage = homepage
        self._backend = backend  # ChatBackend 引用，用于访问 HookManager
        self._builtin_tools: Optional[BuiltinTools] = None
        self._workdir = workdir
        self._custom_tools: Dict[str, Callable] = {}
        self._session_id: Optional[str] = None
        self._call_id: Optional[str] = None

        # 文件操作记录器
        self._file_recorder: Optional[FileOperationRecorder] = None

        self._initialize_builtin_tools()

    def _initialize_builtin_tools(self):
        """初始化内置工具"""
        import os

        workdir = self._workdir
        try:
            from app.utils.utils import resource_path

            workdir = resource_path("")
        except Exception:
            workdir = os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
            )

        logger.info(f"[ToolExecutor] Initialized with workdir: {workdir}")
        self._builtin_tools = BuiltinTools(self._homepage, workdir)

    @property
    def builtin_tools(self) -> Optional[BuiltinTools]:
        return self._builtin_tools

    def is_valid(self) -> bool:
        """检查 ToolExecutor 是否仍然有效（UI 未关闭）"""
        if self._builtin_tools is None:
            return False
        # 检查 QObject 是否已被删除
        try:
            # 如果对象已删除，尝试访问 sip 会有异常
            from PyQt5 import sip
            if sip.isdeleted(self._builtin_tools):
                return False
        except Exception:
            pass
        return True

    def get_todos(self):
        """获取待办事项列表（返回副本）"""
        if self._builtin_tools:
            return self._builtin_tools.get_todos()
        return []

    def clear_todo_list(self):
        """清空待办事项列表"""
        if self._builtin_tools:
            self._builtin_tools.todo_clear()

    def reset_session_state(self):
        """Reset session-scoped state when switching sessions"""
        if self._builtin_tools:
            self._builtin_tools.reset_session_state()

    def cleanup(self):
        """
        彻底清理 ToolExecutor 的所有缓存，防止内存泄漏。
        应该在对话结束后或切换会话时调用。
        """
        # 清理 builtin_tools
        if self._builtin_tools:
            try:
                self._builtin_tools.cleanup()
            except Exception as e:
                logger.warning(f"[ToolExecutor] Failed to cleanup builtin_tools: {e}")
            self._builtin_tools = None

        # 清理文件操作记录器
        if self._file_recorder:
            self._file_recorder = None

        # 清理会话上下文
        self._session_id = None
        self._call_id = None

    def _init_file_recorder(self):
        """初始化文件操作记录器"""
        if self._file_recorder is None:
            self._file_recorder = FileOperationRecorder()

    def set_session_context(self, session_id: str, call_id: str = None):
        """
        设置会话上下文（用于文件操作记录）

        Args:
            session_id: 会话 ID
            call_id: 当前调用 ID（可选）
        """
        self._session_id = session_id
        self._call_id = call_id
        self._init_file_recorder()

    def set_call_id(self, call_id: str):
        """设置当前调用 ID"""
        self._call_id = call_id

    @property
    def file_recorder(self) -> Optional[FileOperationRecorder]:
        """获取文件操作记录器"""
        return self._file_recorder

    def _record_file_operation_before(self, tool_name: str, args: dict):
        """
        在文件操作执行前记录备份信息

        Args:
            tool_name: 工具名称
            args: 工具参数

        Returns:
            str: 文件完整路径，用于后续的编辑后备份
        """
        if tool_name not in self._FILE_OPS_TO_TRACK:
            return None

        if not self._session_id:
            return None

        if not self._call_id:
            return None

        if not self._file_recorder:
            return None

        # 获取文件路径
        path = args.get("path")
        if not path:
            return None

        logger.debug(f"[ToolExecutor] 准备记录文件操作: tool={tool_name}, path={path}")

        # 处理 URL 格式的文件路径 (如 file:/D:/xxx 或 file:///D:/xxx)
        if path.startswith("file:"):
            # 移除 file: 前缀，处理单斜杠或双斜杠
            path = _FILE_PREFIX_PATTERN.sub('', path)

        # 获取完整的文件路径
        if hasattr(self._builtin_tools, "_file_tools"):
            full_path = self._builtin_tools._file_tools._resolve_path(path)
        else:
            from pathlib import Path
            full_path = Path(path).resolve()

        # 记录操作（内部会处理文件不存在的情况）
        try:
            backup_path = self._file_recorder.record_operation(
                session_id=self._session_id,
                call_id=self._call_id,
                tool_name=tool_name,
                file_path=str(full_path)
            )
            if backup_path:
                logger.info(f"[FileRecorder] 已备份: {full_path} -> {backup_path}")
            else:
                # 文件不存在（如新建文件 write_file），跳过记录是正常的
                logger.debug(f"[ToolExecutor] 文件操作记录返回 None")
        except Exception as e:
            # 记录失败不阻塞工具执行
            logger.warning(f"[ToolExecutor] 记录文件操作失败: {e}")

        return str(full_path)

    def _record_file_operation_after(self, tool_name: str, args: dict, file_path_before: str):
        """
        在文件操作执行成功后备份编辑后的文件

        Args:
            tool_name: 工具名称
            args: 工具参数
            file_path_before: 执行前的文件路径
        """
        if not file_path_before:
            return

        if tool_name not in self._FILE_OPS_TO_TRACK:
            return

        if not self._session_id or not self._call_id:
            return

        if not self._file_recorder:
            return

        try:
            self._file_recorder.record_after_operation(
                session_id=self._session_id,
                call_id=self._call_id,
                tool_name=tool_name,
                file_path=file_path_before
            )
        except Exception as e:
            # 记录失败不阻塞主流程
            logger.warning(f"[ToolExecutor] 编辑后备份失败: {e}")

    def _cleanup_backup_on_failure(self):
        """编辑失败时清理备份文件"""
        if not self._file_recorder:
            return
        if not self._session_id or not self._call_id:
            return

        try:
            self._file_recorder.cleanup_on_failure(self._session_id, self._call_id)
            logger.info(f"[ToolExecutor] 已清理失败操作的备份: session={self._session_id}, call={self._call_id}")
        except Exception as e:
            logger.warning(f"[ToolExecutor] 清理备份失败: {e}")

    def set_memory_manager(self, memory_manager):
        if self._builtin_tools:
            self._builtin_tools.set_memory_manager(memory_manager)
            logger.info("[ToolExecutor] MemoryManager attached to BuiltinTools")

    def set_llm_config_getter(self, getter: Callable):
        if self._builtin_tools:
            self._builtin_tools.set_llm_config_getter(getter)
            logger.info("[ToolExecutor] LLM config getter attached to BuiltinTools")

    def set_session_messages_getter(self, getter: Callable):
        if self._builtin_tools:
            self._builtin_tools.set_session_messages_getter(getter)
            logger.info(
                "[ToolExecutor] Session messages getter attached to BuiltinTools"
            )

    def set_agent_manager(self, agent_manager):
        """设置 AgentManager 实例，用于动态生成工具 schema"""
        if self._builtin_tools:
            self._builtin_tools.set_agent_manager(agent_manager)

    def set_current_project(self, project: str):
        """设置当前项目（供 update_project_note 使用）"""
        if self._builtin_tools:
            self._builtin_tools.set_current_project(project)
            # 同步更新 TaskTools._current_project（stage_files 也用）
            if self._builtin_tools._task_tools:
                self._builtin_tools._task_tools._current_project = project
            logger.info(f"[ToolExecutor] set_current_project({project})")

    def get_workdir(self) -> Optional[str]:
        """获取当前工作目录（多窗口隔离：返回实例级值，非 DB 全局值）"""
        return self._workdir

    def set_workdir(self, workdir: Optional[str]):
        """设置工作目录（None 或 "" 表示恢复默认）"""
        self._workdir = workdir
        if self._builtin_tools:
            if workdir:
                self._builtin_tools.set_workdir(workdir)
            else:
                # 恢复默认：用 resource_path
                try:
                    from app.utils.utils import resource_path
                    default_wd = resource_path("")
                except Exception:
                    default_wd = str(Path(__file__).parent.parent.parent)
                self._builtin_tools.workdir = Path(default_wd)
            logger.info(f"[ToolExecutor] Workdir updated: {workdir or 'default'}")

    def set_key_documents_repo(self, repo, project: str = "默认项目"):
        """设置关键文档仓储和当前项目"""
        if self._builtin_tools and self._builtin_tools._task_tools:
            self._builtin_tools._task_tools._key_documents_repo = repo
            self._builtin_tools._task_tools._current_project = project
            logger.info(f"[ToolExecutor] KeyDocumentsRepo attached to TaskTools (project={project})")

    # 工具必需参数定义
    REQUIRED_ARGS = {
        "read": ["path"],
        "write": ["path", "content"],
        "edit": ["path", "oldString", "newString"],
        "multi_edit": ["path", "edits"],
        "grep": ["pattern"],
        "glob": ["pattern"],
        "bash": ["command"],
        # 后台任务工具
        "bg_start": ["command"],
        "bg_stop": ["task_id"],
        "bg_logs": ["task_id"],
        "bg_list": [],
        "webfetch": ["url"],
        "websearch": ["query"],
        "scan_repo": [],
        "stage_files": ["files"],
        "git_status": [],
        "git_log": [],
        "git_diff": [],
        "get_diagnostics": ["path"],
        "summarize_changes": ["text"],
        "edit_project_note": ["oldString", "newString"],
        "read_project_note": [],
        "todowrite": ["todos"],
        "todoread": [],
        "task_batch": ["tasks"],
        "task_wait": ["task_ids"],
        "task_status": [],
        "skill": ["name"],
        "list_skills": [],
        "question": ["questions"]
    }

    def execute(self, tool_name: str, args: dict, cancelled_ref: list = None) -> ToolResult:
        """
        执行工具调用

        Args:
            tool_name: 工具名称
            args: 工具参数
            cancelled_ref: 取消标志引用 [bool]

        Returns:
            ToolResult: 执行结果
        """
        # 检查 ToolExecutor 是否仍然有效（API 模式下 UI 可能已关闭）
        if not self.is_valid():
            logger.warning(f"[ToolExecutor] ToolExecutor is invalid (UI may be closed)")
            return ToolResult(False, error="UI has been closed, tool execution unavailable")

        logger.info(f"[ToolExecutor] Executing tool: {tool_name}, args: {args}")

        # Trigger PreToolUse hook（同步执行，支持跳过和输出回填）
        if self._backend and self._backend.hook_manager:
            import os
            context = {
                "project_root": self._workdir or os.getcwd(),
                "tool_name": tool_name,
            }
            file_path = args.get("path") or args.get("file")
            if file_path:
                context["file"] = file_path
            # 将工具参数注入 context，供 hook 脚本通过 stdin JSON 读取
            # webfetch 需要 url，websearch 需要 query，bash 需要 command
            if "url" in args:
                context["url"] = args["url"]
            if "query" in args:
                context["query"] = args["query"]
            if "command" in args:
                context["command"] = args["command"]
            if "format" in args:
                context["format"] = args["format"]
            # 也保留完整的 args 供高级脚本使用
            context["args"] = args
            current_message_text = ""
            if self._backend.session_manager:
                session = self._backend.get_current_session()
                if session and hasattr(session, 'messages'):
                    for msg in reversed(session.messages):
                        if msg.get('role') == 'user':
                            current_message_text = msg.get('content', '')
                            break
            
            # 同步执行 PreToolUse hooks
            results = self._backend.hook_manager.trigger_event(
                "PreToolUse",
                context=context,
                current_message=current_message_text,
                trigger_async=False   # 关键：同步执行，才能检测 BLOCK 决策
            )
            
            # 检查是否有 hook 要求跳过工具执行（exit 2 或 JSON {"decision":"block"}）
            for result in results:
                if result.decision == HookDecision.BLOCK:
                    # Hook 要求跳过工具执行，将 hook 输出作为工具结果回填
                    logger.info(f"[ToolExecutor] PreToolUse hook BLOCK: {tool_name}, output={result.output[:100] if result.output else 'empty'}")
                    
                    # 将 hook 输出注入到消息上下文（供 LLM 后续分析）
                    if result.output and self._backend:
                        hook_output_msg = f"<hook event=\"PreToolUse\">\n[BLOCKED] Tool '{tool_name}' was blocked by hook.\nHook output:\n{result.output}\n</hook>"
                        self._backend.message_received.emit({
                            "role": "assistant",
                            "content": hook_output_msg
                        })
                    
                    # 返回 hook 输出作为工具结果
                    return ToolResult(
                        True,
                        content=result.output or f"Tool '{tool_name}' was blocked by PreToolUse hook (exit code 2 / decision:block)."
                    )

        # 校验必需参数
        if tool_name in self.REQUIRED_ARGS:
            required = self.REQUIRED_ARGS[tool_name]
            missing = [p for p in required if not args.get(p)]
            if missing:
                return ToolResult(False, error=f"Missing required arguments: {missing}")

        # 对于耗时工具（如 grep, bash, webfetch, websearch），使用异步执行
        if tool_name == "grep":
            return self._execute_grep_async(args, cancelled_ref)
        elif tool_name == "webfetch":
            return self._execute_webfetch_async(args, cancelled_ref)
        elif tool_name == "websearch":
            return self._execute_websearch_async(args, cancelled_ref)

        # 文件操作前记录（用于撤销）
        file_path_before = self._record_file_operation_before(tool_name, args)

        if tool_name in self._custom_tools:
            try:
                result = self._custom_tools[tool_name](args)
                return ToolResult(True, content=result)
            except Exception as e:
                return ToolResult(False, error=f"Custom tool error: {str(e)}")

        # MCP 工具调用：工具名以 "mcp__" 开头
        if tool_name.startswith("mcp__") and self._builtin_tools:
            return self._execute_mcp_tool(tool_name, args)

        tool_map = {
            "read": lambda: self._builtin_tools.read_file(
                path=args.get("path"),  # 统一使用 path
                offset=int(args.get("offset")) if args.get("offset") is not None else 1,
                limit=int(args.get("limit")) if args.get("limit") is not None else 500
            ),
            "write": lambda: self._builtin_tools.write_file(
                path=args.get("path"), content=args.get("content", "")
            ),
            "edit": lambda: self._builtin_tools.edit_file(
                path=args.get("path"),
                oldString=args.get("oldString", ""),
                newString=args.get("newString", ""),
                replaceAll=args.get("replaceAll", False),
            ),
            "multi_edit": lambda: self._builtin_tools.multi_edit(
                path=args.get("path"),
                edits=args.get("edits", []),
            ),
            "grep": lambda: self._builtin_tools.grep_files(
                pattern=args.get("pattern"),
                path=args.get("path", ""),  # 默认当前路径
                include=args.get("include"),
            ),
            "glob": lambda: self._builtin_tools.glob_files(
                pattern=args.get("pattern"),
                path=args.get("path", ""),  # 默认当前路径
            ),
            "list": lambda: self._builtin_tools.list_directory(
                path=args.get("path", "")  # 默认当前路径
            ),
            "git_status": lambda: self._builtin_tools.git_status(args.get("path")),
            "git_log": lambda: self._builtin_tools.git_log(
                args.get("path"), args.get("max_count", 10)
            ),
            "git_diff": lambda: self._builtin_tools.git_diff(
                args.get("ref1"), args.get("ref2"), args.get("path")
            ),
            "bash": lambda: self._builtin_tools.execute_bash(
                args.get("command", ""), args.get("timeout", 120)
            ),
            # 后台任务工具
            "bg_start": lambda: self._builtin_tools.bg_start(
                args.get("command", ""),
                args.get("cwd")
            ),
            "bg_stop": lambda: self._builtin_tools.bg_stop(
                args.get("task_id", "")
            ),
            "bg_logs": lambda: self._builtin_tools.bg_logs(
                args.get("task_id", ""),
                args.get("lines", 100)
            ),
            "bg_list": lambda: self._builtin_tools.bg_list(),
            "webfetch": lambda: self._builtin_tools.fetch_web(
                args.get("url", ""), args.get("format", "markdown")
            ),
            "websearch": lambda: self._builtin_tools.search_web(
                args.get("query", ""), args.get("num_results", 10)
            ),
            "scan_repo": lambda: self._builtin_tools.scan_repo(
                args.get("path"), args.get("max_depth", 2)
            ),
            "stage_files": lambda: self._builtin_tools.stage_files(
                args.get("files", [])
            ),
            "get_diagnostics": lambda: self._builtin_tools.get_diagnostics(
                args.get("path", ""), args.get("language")
            ),
            "summarize_changes": lambda: self._builtin_tools.summarize_changes(
                args.get("text", ""), args.get("limit", 1200)
            ),
            "todowrite": lambda: self._builtin_tools.todo_write(args.get("todos", [])),
            "edit_project_note": lambda: self._builtin_tools.edit_project_note(
                oldString=args.get("oldString", ""),
                newString=args.get("newString", "")),
            "read_project_note": lambda: self._builtin_tools.read_project_note(
                args.get("offset", 1),
                args.get("limit", 500)),
            "todoread": lambda: self._builtin_tools.todo_read(),
            "task_batch": lambda: (
                lambda tasks_val: self._builtin_tools.task_execute_batch(
                    orjson.loads(tasks_val) if isinstance(tasks_val, str) else (tasks_val or []),
                    args.get("share_context", False),
                )
            )(args.get("tasks", [])),
            "task_status": lambda: self._builtin_tools.task_status(
                args.get("task_ids"),
                args.get("with_log", False),
                args.get("with_result", True),
            ),
            "skill": lambda: self._builtin_tools.load_skill(args.get("name", "")),
            "list_skills": lambda: self._builtin_tools.list_skills(),
            "question": lambda: self._builtin_tools.ask_question(
                args.get("questions"),
                **args,
            ),
            "mcp_list_servers": lambda: ToolResult(
                True,
                content=self._builtin_tools._mcp_manager.get_status()
            ),
        }

        # ========== 工具执行前的有效性检查 ==========
        # 在 UI 关闭场景下，即使方法开头检查通过，
        # lambda 执行期间 UI 可能被关闭，导致 BuiltinTools 访问崩溃
        if not self.is_valid():
            logger.warning(f"[ToolExecutor] ToolExecutor became invalid during hook phase")
            return ToolResult(False, error="UI has been closed, tool execution unavailable")

        executor = tool_map.get(tool_name)
        if executor:
            try:
                result = executor()
                # ========== 工具执行后的有效性检查（防御性）==========
                # 对于耗时操作（bash、task_batch），执行期间 UI 可能被关闭
                if not self.is_valid():
                    logger.warning(f"[ToolExecutor] ToolExecutor became invalid after tool execution: {tool_name}")
                    # 结果可能已被 UI 关闭中断，标记为不完整
                    if result and result.success:
                        result.success = False
                        result.content = (result.content or "") + "\n[警告: UI 在执行过程中已关闭]"
                # 文件操作成功后备份编辑后的文件（用于差异对比）
                if tool_name in self._FILE_OPS_TO_TRACK and result and result.success:
                    self._record_file_operation_after(tool_name, args, file_path_before)
                
                # Trigger PostToolUse hook
                if self._backend and self._backend.hook_manager:
                    import os
                    context = {
                        "project_root": self._workdir or os.getcwd(),
                        "tool_name": tool_name,
                    }
                    # Extract file path if available
                    file_path = args.get("path") or args.get("file")
                    if file_path:
                        context["file"] = file_path
                    
                    # Get last user message for matching
                    current_message_text = ""
                    if self._backend.session_manager:
                        session = self._backend.get_current_session()
                        if session and hasattr(session, 'messages'):
                            # Find last user message
                            for msg in reversed(session.messages):
                                if msg.get('role') == 'user':
                                    current_message_text = msg.get('content', '')
                                    break
                    
                    self._backend.hook_manager.trigger_event(
                        "PostToolUse",
                        context=context,
                        current_message=current_message_text
                    )
                
                return result
            except Exception as e:
                # 文件编辑操作失败时清理备份
                if tool_name in self._FILE_OPS_TO_TRACK:
                    self._cleanup_backup_on_failure()
                return ToolResult(False, error=f"Execution error: {str(e)}")

        return ToolResult(False, error=f"Unknown tool: {tool_name}")

    def _execute_mcp_tool(self, tool_name: str, args: dict) -> ToolResult:
        """执行 MCP 工具调用"""
        # 执行前检查 ToolExecutor 有效性
        if not self.is_valid():
            logger.warning(f"[ToolExecutor] ToolExecutor became invalid before MCP tool: {tool_name}")
            return ToolResult(False, error="UI has been closed, tool execution unavailable")

        mcp_manager = self._builtin_tools._mcp_manager

        if not mcp_manager.is_connected:
            return ToolResult(False, error="MCP 未连接，请先配置并连接 MCP 服务器")

        try:
            result = mcp_manager.call_tool_sync(tool_name, args)
        except Exception as e:
            logger.error(f"[ToolExecutor] MCP tool '{tool_name}' raised exception: {e}")
            return ToolResult(False, error=f"MCP 工具执行异常: {e}")

        # 执行后检查 ToolExecutor 有效性（MCP 调用可能耗时较长）
        if not self.is_valid():
            logger.warning(f"[ToolExecutor] ToolExecutor became invalid after MCP tool: {tool_name}")
            # 不再尝试修改 result，避免访问已删除的 QObject 属性

        return result

    def _execute_grep_async(self, args: dict, cancelled_ref: list = None) -> ToolResult:
        """
        异步执行 grep，使用子线程，完成后返回结果

        Args:
            args: 工具参数
            cancelled_ref: 取消标志引用 [bool]

        Returns:
            ToolResult: 执行结果
        """
        if not self._builtin_tools or not self._builtin_tools._file_tools:
            return ToolResult(False, error="FileTools not available")

        pattern = args.get("pattern", "")
        path = args.get("path", "")
        include = args.get("include")

        # 使用 FileTools 的异步接口
        result_holder = [None]
        finished = [False]

        def on_grep_done(result):
            result_holder[0] = result
            finished[0] = True

        # 启动异步 grep
        self._builtin_tools._file_tools.grep_files(
            pattern=pattern,
            path=path,
            include=include,
            callback=on_grep_done
        )

        # 使用定时器循环处理主线程事件，这样取消信号可以被处理
        def wait_for_result():
            from PyQt5.QtWidgets import QApplication
            QApplication.processEvents()

            if finished[0]:
                return

            # 检查取消标志
            if cancelled_ref is not None and cancelled_ref[0]:
                self._builtin_tools._file_tools.cancel()
                result_holder[0] = ToolResult(False, error="用户中止")
                finished[0] = True
                return

            # 继续等待
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(50, wait_for_result)

        wait_for_result()

        # 等待完成
        while not finished[0]:
            from PyQt5.QtWidgets import QApplication
            QApplication.processEvents()
            import time
            time.sleep(0.05)

        return result_holder[0] if result_holder[0] else ToolResult(False, error="Grep failed")

    def _execute_webfetch_async(self, args: dict, cancelled_ref: list = None) -> ToolResult:
        """异步执行网页抓取"""
        if not self._builtin_tools or not self._builtin_tools._web_tools:
            return ToolResult(False, error="WebTools not available")

        url = args.get("url", "")
        format = args.get("format", "markdown")
        max_chars = args.get("max_chars", 26000)

        result_holder = [None]
        finished = [False]

        def on_fetch_done(result):
            result_holder[0] = result
            finished[0] = True

        self._builtin_tools._web_tools.fetch_web(
            url=url, format=format, max_chars=max_chars,
            callback=on_fetch_done, cancelled_ref=cancelled_ref
        )

        def wait_for_result():
            from PyQt5.QtWidgets import QApplication
            QApplication.processEvents()
            if finished[0]: return
            if cancelled_ref is not None and cancelled_ref[0]:
                result_holder[0] = ToolResult(False, error="用户中止")
                finished[0] = True
                return
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(50, wait_for_result)

        wait_for_result()

        while not finished[0]:
            from PyQt5.QtWidgets import QApplication
            QApplication.processEvents()
            import time
            time.sleep(0.05)

        return result_holder[0] if result_holder[0] else ToolResult(False, error="WebFetch failed")

    def _execute_websearch_async(self, args: dict, cancelled_ref: list = None) -> ToolResult:
        """异步执行网络搜索"""
        if not self._builtin_tools or not self._builtin_tools._web_tools:
            return ToolResult(False, error="WebTools not available")

        query = args.get("query", "")
        num_results = args.get("num_results", 10)

        result_holder = [None]
        finished = [False]

        def on_search_done(result):
            result_holder[0] = result
            finished[0] = True

        self._builtin_tools._web_tools.search_web(
            query=query, num_results=num_results,
            callback=on_search_done, cancelled_ref=cancelled_ref
        )

        def wait_for_result():
            from PyQt5.QtWidgets import QApplication
            QApplication.processEvents()
            if finished[0]: return
            if cancelled_ref is not None and cancelled_ref[0]:
                result_holder[0] = ToolResult(False, error="用户中止")
                finished[0] = True
                return
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(50, wait_for_result)

        wait_for_result()

        while not finished[0]:
            from PyQt5.QtWidgets import QApplication
            QApplication.processEvents()
            import time
            time.sleep(0.05)

        return result_holder[0] if result_holder[0] else ToolResult(False, error="WebSearch failed")

    def set_sub_agent_manager(self, sub_agent_manager):
        """设置子智能体管理器"""
        if self._builtin_tools:
            self._builtin_tools._sub_agent_manager = sub_agent_manager
            self._builtin_tools._task_tools._sub_agent_manager = sub_agent_manager
            logger.info(
                "[ToolExecutor] SubAgentManager attached to BuiltinTools and TaskTools"
            )
