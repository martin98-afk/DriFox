# -*- coding: utf-8 -*-
"""
工具执行器模块 - 统一处理各种工具调用
"""

import json as _json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import orjson
from loguru import logger

from app.core.hook_manager import HookDecision
from app.core.message_content import content_to_text
from app.core.model_capabilities import resolve_context_limit
from app.core.token_estimator import count_messages_tokens
from app.tools.tool_name_mapper import ToolNameMapper

# 预编译正则表达式
_FILE_PREFIX_PATTERN = re.compile(r"^file:/{1,3}")

from app.core.lsp.lsp_manager import LspManager
from app.tools import BuiltinTools, ToolResult
from app.tools.file_tools import _resolve_path
from app.utils.config import Settings
from app.utils.file_operation_recorder import (
    FileOperationRecorder,
)


class ToolExecutor:
    """工具执行器 - 统一调度各种工具"""

    # 需要记录的文件操作
    _FILE_OPS_TO_TRACK = {"write", "edit", "multi_edit"}

    # 注意：BuiltinTools 不再跨窗口共享，每个窗口拥有独立实例
    # 确保工作目录（workdir）完全隔离，多窗口互不影响
    # MCPClientManager 本身已是全局单例，连接仍跨窗口共享

    def __init__(self, homepage=None, workdir: str = None, backend=None):
        self._homepage = homepage
        self._backend = backend  # ChatBackend 引用，用于访问 HookManager
        self._builtin_tools: Optional[BuiltinTools] = None
        # P002: 初始就用默认路径兜底，避免 self._workdir 与 _builtin_tools.workdir 不一致
        self._workdir = workdir or self._default_workdir()
        self._custom_tools: Dict[str, Callable] = {}
        self._session_id: Optional[str] = None
        self._call_id: Optional[str] = None

        # 文件操作记录器
        self._file_recorder: Optional[FileOperationRecorder] = None

        # 子智能体管理器（实例级，每个窗口独立，不共享给 BuiltinTools）
        self._sub_agent_manager = None

        # 线程安全锁：保护文件记录等非线程安全操作
        self._lock = threading.Lock()

        self._initialize_builtin_tools()

    def _initialize_builtin_tools(self):
        """初始化内置工具（每个窗口独立实例，不复用）"""
        # 使用已设置的 _workdir（用户可能已通过 set_workdir 或构造函数设置）
        # 仅当 _workdir 为 None 时才回退到默认路径
        workdir = self._workdir
        if not workdir:
            try:
                from app.utils.utils import resource_path

                workdir = resource_path("")
            except Exception:
                workdir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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
        清理窗口独有状态。

        清理当前窗口的 BuiltinTools（释放 MCP 引用计数），
        其他窗口不受影响。
        """
        # 清理文件操作记录器
        self._file_recorder = None

        # 清理会话上下文
        self._session_id = None
        self._call_id = None

        # 清理自定义工具
        self._custom_tools.clear()

        # 清理当前窗口的 BuiltinTools
        if self._builtin_tools:
            try:
                self._builtin_tools.cleanup()
            except Exception as e:
                logger.warning(f"[ToolExecutor] cleanup builtin_tools: {e}")
            self._builtin_tools = None

        # 释放 backend 引用（打破循环引用链）
        self._backend = None
        self._homepage = None

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

    def _record_file_operation_before(self, tool_name: str, args: dict, session_id: str = None, call_id: str = None):
        """
        在文件操作执行前记录备份信息

        Args:
            tool_name: 工具名称
            args: 工具参数
            session_id: 会话 ID（不传则使用 self._session_id）
            call_id: 调用 ID（不传则使用 self._call_id）

        Returns:
            str: 文件完整路径，用于后续的编辑后备份
        """
        if tool_name not in self._FILE_OPS_TO_TRACK:
            return None

        # 使用传入的值或回退到实例变量
        sid = session_id or self._session_id
        cid = call_id or self._call_id

        if not sid or not cid:
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
            path = _FILE_PREFIX_PATTERN.sub("", path)

        # 获取完整的文件路径
        if hasattr(self._builtin_tools, "_file_tools"):
            full_path = self._builtin_tools._file_tools._resolve_path(path)
        else:
            from pathlib import Path

            full_path = Path(path).resolve()

        # 记录操作（内部会处理文件不存在的情况）
        try:
            with self._lock:
                backup_path = self._file_recorder.record_operation(
                    session_id=sid, call_id=cid, tool_name=tool_name, file_path=str(full_path)
                )
            if backup_path:
                logger.info(f"[FileRecorder] 已备份: {full_path} -> {backup_path}")
            else:
                # 文件不存在（如新建文件 write_file），跳过记录是正常的
                logger.debug("[ToolExecutor] 文件操作记录返回 None")
        except Exception as e:
            # 记录失败不阻塞工具执行
            logger.warning(f"[ToolExecutor] 记录文件操作失败: {e}")

        return str(full_path)

    def _record_file_operation_after(
        self, tool_name: str, args: dict, file_path_before: str, session_id: str = None, call_id: str = None
    ):
        """
        在文件操作执行成功后备份编辑后的文件

        Args:
            tool_name: 工具名称
            args: 工具参数
            file_path_before: 执行前的文件路径
            session_id: 会话 ID（不传则使用 self._session_id）
            call_id: 调用 ID（不传则使用 self._call_id）
        """
        if not file_path_before:
            return

        if tool_name not in self._FILE_OPS_TO_TRACK:
            return

        sid = session_id or self._session_id
        cid = call_id or self._call_id

        if not sid or not cid:
            return

        if not self._file_recorder:
            return

        try:
            with self._lock:
                self._file_recorder.record_after_operation(
                    session_id=sid, call_id=cid, tool_name=tool_name, file_path=file_path_before
                )
        except Exception as e:
            # 记录失败不阻塞主流程
            logger.warning(f"[ToolExecutor] 编辑后备份失败: {e}")

    def _cleanup_backup_on_failure(self, session_id: str = None, call_id: str = None):
        """编辑失败时清理备份文件"""
        if not self._file_recorder:
            return
        sid = session_id or self._session_id
        cid = call_id or self._call_id
        if not sid or not cid:
            return

        try:
            with self._lock:
                self._file_recorder.cleanup_on_failure(sid, cid)
            logger.info(f"[ToolExecutor] 已清理失败操作的备份: session={sid}, call={cid}")
        except Exception as e:
            logger.warning(f"[ToolExecutor] 清理备份失败: {e}")

    # ========== PostToolUse hook 统一触发 ==========

    # Auto-compact 防重复触发冷却（秒）
    _AUTO_COMPACT_COOLDOWN = 30.0

    def _get_context_usage_info(self) -> tuple:
        """获取当前会话的 token 使用量和上下文限制

        从 session 的消息估算 token 数，从模型配置解析上下文窗口限制。

        Returns:
            (token_count, token_limit):
                token_count: 当前对话已占用的 token 数（估算值）
                token_limit: 当前模型的最大上下文窗口限制
                任一为 0 表示无法获取。
        """
        if not (self._backend and self._backend.session_manager):
            return 0, 0

        session = self._backend.get_current_session()
        if not session or not getattr(session, "messages", None):
            return 0, 0

        messages = session.messages
        if not messages:
            return 0, 0

        # 估算 token 数
        token_count = 0
        try:
            token_count = count_messages_tokens(messages)
        except Exception:
            pass

        # 解析上下文限制
        token_limit = 0
        try:
            getter = getattr(self._backend, "_get_model_config", None)
            if getter and callable(getter):
                llm_config = getter() or {}
                token_limit = resolve_context_limit(llm_config)
        except Exception:
            pass

        return token_count, token_limit

    def _trigger_post_tool_use(self, tool_name: str, args: dict, result=None) -> None:
        """触发 PostToolUse hook（统一方法，减少重复代码）

        Args:
            tool_name: 工具名
            args: 工具参数
            result: 工具执行结果（ToolResult 或 None），用于回填结果信息
        """
        if not (self._backend and self._backend.hook_manager):
            return

        # 获取 session_id
        local_session_id = ""
        if self._backend and self._backend.session_manager:
            session = self._backend.get_current_session()
            if session:
                local_session_id = session.session_id or ""

        # 创建标准化 tool_input 副本（兼容 Claude Code 字段命名）
        _normalized_input = dict(args)
        for _src, _dst in [
            ("newString", "new_string"),
            ("oldString", "old_string"),
            ("path", "file_path"),
            ("file", "file_path"),
        ]:
            if _src in _normalized_input and _dst not in _normalized_input:
                _normalized_input[_dst] = _normalized_input[_src]
            if _dst in _normalized_input and _src not in _normalized_input:
                _normalized_input[_src] = _normalized_input[_dst]
        for _edit in _normalized_input.get("edits", []):
            if isinstance(_edit, dict):
                if "newString" in _edit and "new_string" not in _edit:
                    _edit["new_string"] = _edit["newString"]
                if "oldString" in _edit and "old_string" not in _edit:
                    _edit["old_string"] = _edit["oldString"]

        context = {
            "project_root": self._workdir or os.getcwd(),
            # Claude Code 风格工具名（PascalCase），使第三方插件能通过
            # 'Edit|Write|MultiEdit' 等大小写敏感匹配识别工具
            "tool_name": ToolNameMapper.to_claude_style(tool_name),
            # 保留 DriFoxx 原生小写名（向后兼容）
            "tool_name_native": tool_name,
            "args": args,
            # Claude Code 兼容字段（字段名已标准化）
            "tool_input": _normalized_input,
            "session_id": local_session_id,
            # 【新增】让 hook 能识别当前执行角色（与 subagent_worker._build_hook_context 对齐）
            "current_role": "primary",
            "is_subagent_call": False,
        }

        # 注入工具结果信息（PostToolUse 最核心的增强）
        if result is not None:
            _MAX_RESULT_LEN = 100000  # 足够钩子脚本分析，避免 10MB+ 巨量结果撑爆 JSON
            context["result_success"] = result.success
            # 确保 content 为字符串，防止 dict/list 等非字符串类型切片报错
            # 例如 subagent_para 返回的 content 是 dict
            content = result.content
            if not isinstance(content, str):
                if content is not None:
                    content = orjson.dumps(content).decode("utf-8", errors="replace")
                else:
                    content = ""
            result_str = content or result.error or ""
            is_truncated = len(result_str) > _MAX_RESULT_LEN
            # DriFoxx 自有格式（向后兼容）
            context["result_content"] = result_str[:_MAX_RESULT_LEN]
            context["result_truncated"] = is_truncated
            # Claude Code 兼容格式：tool_result dict
            context["tool_result"] = {
                "success": result.success,
                "content": result_str[:_MAX_RESULT_LEN],
                "truncated": is_truncated,
                "has_error": bool(result.error),
                "error": result.error or "",
            }

        # 注入文件路径
        file_path = args.get("path") or args.get("file")
        if file_path:
            context["file"] = file_path

        # 获取最后一条用户消息（用于 hook matcher 匹配上下文）
        current_message_text = ""
        if self._backend.session_manager:
            session = self._backend.get_current_session()
            if session and hasattr(session, "messages"):
                for msg in reversed(session.messages):
                    if msg.get("role") == "user":
                        current_message_text = content_to_text(msg.get("content", ""))
                        break

        # ====== 注入上下文使用量信息（供 auto-compact hook 检测）======
        token_count, token_limit = self._get_context_usage_info()
        context["token_count"] = token_count
        context["token_limit"] = token_limit
        if token_count > 0 and token_limit > 0:
            context["token_ratio"] = token_count / token_limit
        else:
            context["token_ratio"] = 0.0
        # ============================================================

        # PostToolUse hook 同步执行，确保 hook 输出在 worker 构建消息序列前
        # 完成并注入到 session，使得 LLM 能感知 hook 的验证/处理结果
        results = self._backend.hook_manager.trigger_event(
            "PostToolUse",
            context=context,
            current_message=current_message_text,
            trigger_async=False,
        )

        # ====== 检查 PostToolUse hook 结果，触发 auto-compact ======
        self._check_auto_compact(results)
        # ============================================================

        # ====== PostToolUse hook 替换工具结果内容（hookSpecificOutput 约定）======
        # 适用场景：hook 需要对工具结果做脱敏/过滤/加密，再给大模型。
        #
        # 约定：PostToolUse hook 的返回值 JSON 中包含
        #   hookSpecificOutput.hookEventName == "PostToolUse"
        #   hookSpecificOutput.replaceContent == "替换后的内容"
        # 时才触发替换。普通文本输出或其他 JSON 输出不触发。
        #
        # 替换后清空 _hook_message_queue 防止双重注入（替换内容已反映在 result.content 中）。
        if result is not None and result.success and isinstance(result.content, str):
            _replaced = False
            for _r in results:
                if _r.success and _r.output:
                    try:
                        _data = _json.loads(_r.output)
                        if isinstance(_data, dict):
                            _hso = _data.get("hookSpecificOutput")
                            if (
                                isinstance(_hso, dict)
                                and _hso.get("hookEventName") == "PostToolUse"
                                and "replaceContent" in _hso
                            ):
                                result.content = str(_hso["replaceContent"])
                                _replaced = True
                                logger.debug(
                                    f"[ToolExecutor] PostToolUse hook 替换了 {tool_name}"
                                    f" 结果内容 ({len(result.content)} chars, replaceContent)"
                                )
                                break
                    except Exception:
                        pass  # 非 JSON 或无法解析 → 不触发替换
            if _replaced:
                # 清空 hook 队列：替换后的内容已在 result.content 中，
                # 队列中的副本会导致 worker 在下一轮循环中再次注入，造成双重注入。
                _q2 = getattr(self._backend, "_hook_message_queue", None)
                if _q2 is not None:
                    import queue as _queue2

                    while True:
                        try:
                            _q2.get_nowait()
                        except _queue2.Empty:
                            break
                    logger.debug(
                        "[ToolExecutor] PostToolUse 替换后已清空 _hook_message_queue，"
                        "防止 result.content 与队列消息双重注入"
                    )
        # ============================================================

    def _check_auto_compact(self, results: list) -> None:
        """检查 PostToolUse hook 结果中是否有 auto-compact 触发信号

        hook 函数的返回值（JSON）格式：
            {"auto_compact": true, "ratio": 0.85}

        Args:
            results: trigger_event 返回的 HookExecutionResult 列表
        """
        if not results or not self._backend:
            return

        for r in results:
            if not r.success or not r.output:
                continue
            try:
                data = _json.loads(r.output)
                if isinstance(data, dict) and data.get("auto_compact"):
                    ratio = float(data.get("ratio", 0.0))
                    # 交给后端处理（带冷却）
                    self._backend.request_auto_compact(ratio)
                    return  # 只触发一次
            except (_json.JSONDecodeError, ValueError, TypeError):
                pass

    # ========== 自动 LSP 诊断（文件编辑后） ==========

    def _try_auto_lsp_diagnose(self, tool_name: str, args: dict, result: "ToolResult") -> "ToolResult":
        """文件编辑成功后，若开启自动诊断则运行 LSP 诊断并追加到结果

        Args:
            tool_name: 工具名 (write/edit/multi_edit)
            args: 工具参数
            result: 原始工具结果

        Returns:
            追加了诊断信息的结果（或原始结果）
        """
        # 1. 检查自动诊断开关
        try:
            cfg = Settings.get_instance()
            if not cfg.lsp_auto_diagnose.value:
                return result
        except Exception as e:
            logger.debug(f"[ToolExecutor] 自动诊断开关读取失败: {e}")
            return result

        # 2. 获取文件路径并解析
        file_path = args.get("path")
        if not file_path:
            return result

        try:
            full_path = _resolve_path(self._builtin_tools.workdir, file_path)
        except Exception as e:
            logger.debug(f"[ToolExecutor] 自动诊断路径解析失败: {e}")
            return result

        # 3. 检查是否有对应的 LSP 客户端
        try:
            lsp_mgr = LspManager.get_instance()
            client = lsp_mgr.get_client_for_file(str(full_path))
            if not client:
                return result  # 无对应 LSP 服务器
        except Exception as e:
            logger.debug(f"[ToolExecutor] 自动诊断 LSP 路由失败: {e}")
            return result

        # 4. 运行快速诊断（跳过 LSP 协议握手，直接 CLI）
        try:
            diag_result = lsp_mgr.sync_quick_diagnostics(str(full_path), timeout=5.0)
        except Exception as e:
            logger.warning(f"[ToolExecutor] 自动 LSP 快速诊断失败: {e}")
            return result

        # 诊断文本追加到 result.content 末尾，使其自然通过消息持久化路径保留（
        # consolidate_messages → normalize_message 保留 content 字段），
        # 确保 LLM 在各轮次都能看到诊断结果，避免跨轮 content 变化导致 KV 缓存命中丢失。
        diag_msg = "[LSP 自动诊断] 未发现问题" if not diag_result else diag_result
        result.content = f"{result.content}\n\n{diag_msg}"

        logger.debug(f"[ToolExecutor] 自动 LSP 诊断完成: {tool_name} → {file_path}")
        return result

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
            logger.info("[ToolExecutor] Session messages getter attached to BuiltinTools")

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
        """获取当前工作目录（多窗口隔离：返回实例级值，非 DB 全局值）

        返回 None 表示用户未设置根目录（已清除）。
        上游调用方需自行处理 None 情况（如回退到 os.getcwd() 或""）。
        """
        return self._workdir if self._workdir else None

    def set_workdir(self, workdir: Optional[str]):
        """设置工作目录（None 或 "" 表示清除，不保留默认回退）

        清除时 _workdir 设为 None，get_workdir() 返回 None，
        下游代码据此判断"用户未设置根目录"。
        """
        if workdir:
            self._workdir = workdir
            if self._builtin_tools:
                self._builtin_tools.set_workdir(workdir)
        else:
            # 清除工作目录：设为 None，下游可据此判断"无根目录"
            self._workdir = None
            if self._builtin_tools:
                # BuiltinTools 需要一个有效路径（文件工具需要），
                # 回退到默认工作目录
                default_wd = self._default_workdir()
                self._builtin_tools.workdir = Path(default_wd)
        logger.info(f"[ToolExecutor] Workdir updated: {workdir or 'cleared'}")

    def _default_workdir(self) -> str:
        """获取默认工作目录（项目根 / exe 根，与 _initialize_builtin_tools 一致）

        规范化：用 Path 解析后转为 str，避免 Windows 路径带尾随反斜杠
        （如 'D:\\work\\DriFoxx\\' 导致 os.path.basename 返回空字符串）。
        """
        try:
            from app.utils.utils import resource_path

            return str(Path(resource_path("")).resolve())
        except Exception:
            return str(Path(__file__).resolve().parent.parent.parent)

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
        "todowrite": ["todos"],
        "todoread": [],
        "subagent_para": ["tasks"],
        "subagent_dag": ["nodes"],
        "task_wait": ["task_ids"],
        "subagent_status": [],
        "skill": ["name"],
        "list_skills": [],
        "question": ["questions"],
        "read_persisted_output": ["file_path"],
        "gitee_upload": ["local_path"],
        "upload_file": ["local_path"],
        # 桌面自动化 (mouse / keyboard / screenshot)
        "mouse": ["action"],
        "keyboard": ["action"],
        "screenshot": [],
        # LSP 工具
        "lsp": ["operation"],
    }

    def execute(self, tool_name: str, args: dict, call_id: str = None) -> ToolResult:
        """
        执行工具调用

        Args:
            tool_name: 工具名称
            args: 工具参数
            cancelled_ref: 取消标志引用 [bool]
            call_id: 工具调用 ID（可选，用于并行执行时隔离上下文；不传则使用 self._call_id）

        Returns:
            ToolResult: 执行结果
        """
        # 检查 ToolExecutor 是否仍然有效（API 模式下 UI 可能已关闭）
        if not self.is_valid():
            logger.warning("[ToolExecutor] ToolExecutor is invalid (UI may be closed)")
            return ToolResult(False, error="UI has been closed, tool execution unavailable")

        # 🛡️ 防御性补全 mcp__ 前缀：LLM 偶尔可能漏掉前缀
        # （如从 mcp_list_servers 返回的裸名、训练数据记忆的短名等），
        # 走完恢复后仍统一为可识别的完整名。
        tool_name = self._try_recover_mcp_prefix(tool_name) or tool_name

        # 🔒 线程安全：捕获当前 session_id/call_id 到局部变量（后续使用局部变量而非实例变量）
        with self._lock:
            local_session_id = self._session_id
            local_call_id = call_id or self._call_id

        logger.info(f"[ToolExecutor] Executing tool: {tool_name}, args: {args}")

        # Trigger PreToolUse hook（同步执行，支持跳过和输出回填）
        if self._backend and self._backend.hook_manager:
            # 创建标准化 tool_input 副本（兼容 Claude Code 字段命名）
            _normalized_input = dict(args)  # 浅拷贝，不污染原始 args
            for _src, _dst in [
                ("newString", "new_string"),
                ("oldString", "old_string"),
                ("path", "file_path"),
                ("file", "file_path"),
                ("content", "tool_input_content"),  # 额外别名（部分钩子用）
            ]:
                if _src in _normalized_input and _dst not in _normalized_input:
                    _normalized_input[_dst] = _normalized_input[_src]
                if _dst in _normalized_input and _src not in _normalized_input:
                    _normalized_input[_src] = _normalized_input[_dst]  # 双向兼容
            # MultiEdit edits 子项驼峰→蛇形
            for _edit in _normalized_input.get("edits", []):
                if isinstance(_edit, dict):
                    if "newString" in _edit and "new_string" not in _edit:
                        _edit["new_string"] = _edit["newString"]
                    if "oldString" in _edit and "old_string" not in _edit:
                        _edit["old_string"] = _edit["oldString"]

            context = {
                "project_root": self._workdir or os.getcwd(),
                # Claude Code 风格工具名（PascalCase），使第三方插件能通过
                # 'Edit|Write|MultiEdit' 等大小写敏感匹配识别工具
                "tool_name": ToolNameMapper.to_claude_style(tool_name),
                # 保留 DriFoxx 原生小写名（向后兼容）
                "tool_name_native": tool_name,
                # Claude Code 兼容字段（字段名已标准化）
                "tool_input": _normalized_input,
                "session_id": local_session_id,
                # 【新增】让 hook 能识别当前执行角色（与 subagent_worker._build_hook_context 对齐）
                "current_role": "primary",
                "is_subagent_call": False,
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
            # 完整的 args（DriFoxx 自有格式，向后兼容）
            context["args"] = args
            current_message_text = ""
            if self._backend.session_manager:
                session = self._backend.get_current_session()
                if session and hasattr(session, "messages"):
                    for msg in reversed(session.messages):
                        if msg.get("role") == "user":
                            current_message_text = content_to_text(msg.get("content", ""))
                            break

            # 同步执行 PreToolUse hooks
            results = self._backend.hook_manager.trigger_event(
                "PreToolUse",
                context=context,
                current_message=current_message_text,
                trigger_async=False,  # 关键：同步执行，才能检测 BLOCK 决策
            )

            # 检查是否有 hook 要求跳过工具执行（exit 2 或 JSON {"decision":"block"}）
            for result in results:
                if result.decision == HookDecision.BLOCK:
                    # Hook 要求跳过工具执行，将 hook 输出作为工具结果回填
                    logger.info(
                        f"[ToolExecutor] PreToolUse hook BLOCK: {tool_name}, output={result.output[:100] if result.output else 'empty'}"
                    )

                    # 🔧 Claude Code 语义对齐：BLOCK 时 hook 输出只通过 result.content 传递，
                    # 不同时走队列。避免 _inject_pending_pretool_messages() 和
                    # _build_response_message_sequence(tool_results) 双重注入。
                    # 清空队列中本应属于此 BLOCK hook 的输出（如果 add_output_to_context=True）。
                    _q = getattr(self._backend, "_pre_tool_message_queue", None)
                    if _q is not None:
                        import queue as _queue

                        while True:
                            try:
                                _q.get_nowait()
                            except _queue.Empty:
                                break

                    # 返回 hook 输出作为工具结果（单来源：result.content）
                    return ToolResult(
                        True,
                        content=result.output
                        or f"Tool '{tool_name}' was blocked by PreToolUse hook (exit code 2 / decision:block).",
                    )

        # 校验必需参数
        if tool_name in self.REQUIRED_ARGS:
            required = self.REQUIRED_ARGS[tool_name]
            missing = [p for p in required if not args.get(p)]
            if missing:
                return ToolResult(False, error=f"Missing required arguments: {missing}")

        # 文件操作前记录（用于撤销）
        file_path_before = self._record_file_operation_before(tool_name, args, local_session_id, local_call_id)

        if tool_name in self._custom_tools:
            try:
                result_data = self._custom_tools[tool_name](args)
                my_result = ToolResult(True, content=result_data)
                self._trigger_post_tool_use(tool_name, args, my_result)
                return my_result
            except Exception as e:
                my_result = ToolResult(False, error=f"Custom tool error: {str(e)}")
                self._trigger_post_tool_use(tool_name, args, my_result)
                return my_result

        # MCP 工具调用：工具名以 "mcp__" 开头
        if tool_name.startswith("mcp__") and self._builtin_tools:
            return self._execute_mcp_tool(tool_name, args)

        tool_map = {
            "read": lambda: self._builtin_tools.read_file(
                path=args.get("path"),  # 统一使用 path
                startline=int(args.get("startline")) if args.get("startline") is not None else 1,
                endline=int(args.get("endline")) if args.get("endline") is not None else None,
            ),
            "write": lambda: self._builtin_tools.write_file(path=args.get("path"), content=args.get("content", "")),
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
            "git_log": lambda: self._builtin_tools.git_log(args.get("path"), args.get("max_count", 10)),
            "git_diff": lambda: self._builtin_tools.git_diff(args.get("ref1"), args.get("ref2"), args.get("path")),
            "bash": lambda: self._builtin_tools.execute_bash(args.get("command", ""), args.get("timeout", 120)),
            # 后台任务工具
            "bg_start": lambda: self._builtin_tools.bg_start(args.get("command", ""), args.get("cwd")),
            "bg_stop": lambda: self._builtin_tools.bg_stop(args.get("task_id", "")),
            "bg_logs": lambda: self._builtin_tools.bg_logs(args.get("task_id", ""), args.get("lines", 100)),
            "bg_list": lambda: self._builtin_tools.bg_list(),
            "webfetch": lambda: self._builtin_tools.fetch_web(
                args.get("url", ""), args.get("format", "markdown"), args.get("max_chars", 26000)
            ),
            "websearch": lambda: self._builtin_tools.search_web(args.get("query", ""), args.get("num_results", 10)),
            "scan_repo": lambda: self._builtin_tools.scan_repo(args.get("path"), args.get("max_depth", 2)),
            "stage_files": lambda: self._builtin_tools.stage_files(args.get("files", [])),
            "get_diagnostics": lambda: self._builtin_tools.get_diagnostics(args.get("path", ""), args.get("language")),
            "summarize_changes": lambda: self._builtin_tools.summarize_changes(
                args.get("text", ""), args.get("limit", 1200)
            ),
            "todowrite": lambda: self._builtin_tools.todo_write(args.get("todos", [])),
            "todoread": lambda: self._builtin_tools.todo_read(),
            "subagent_para": lambda: (
                lambda tasks_val: self._builtin_tools.subagent_para_execute(
                    orjson.loads(tasks_val) if isinstance(tasks_val, str) else (tasks_val or []),
                    args.get("share_context", False),
                    session_id=local_session_id,
                    sub_agent_manager=self._sub_agent_manager,
                )
            )(args.get("tasks", [])),
            "subagent_status": lambda: self._builtin_tools.subagent_status(
                args.get("task_ids"),
                args.get("with_log", False),
                args.get("with_result", True),
                session_id=local_session_id,
                sub_agent_manager=self._sub_agent_manager,
            ),
            "subagent_dag": lambda: self._builtin_tools.subagent_dag_execute(
                args.get("nodes", []),
                args.get("edges", []),
                session_id=local_session_id,
                sub_agent_manager=self._sub_agent_manager,
            ),
            "skill": lambda: self._builtin_tools.load_skill(args.get("name", "")),
            "list_skills": lambda: self._builtin_tools.list_skills(),
            "question": lambda: self._builtin_tools.ask_question(
                args.get("questions"),
                **args,
            ),
            "mcp_list_servers": lambda: ToolResult(True, content=self._builtin_tools._mcp_manager.get_status()),
            "gitee_upload": lambda: self._builtin_tools.gitee_upload(
                local_path=args.get("local_path", ""),
            ),
            "upload_file": lambda: self._builtin_tools.gitee_upload(
                local_path=args.get("local_path", ""),
            ),
            "read_persisted_output": lambda: self._builtin_tools.read_persisted_output(
                file_path=args.get("file_path", "")
            ),
            # ========== 桌面自动化 (AutomationTools) ==========
            "mouse": lambda: self._builtin_tools.mouse(
                action=args.get("action", ""),
                x=int(args.get("x", 0) or 0),
                y=int(args.get("y", 0) or 0),
                button=args.get("button", "left"),
                clicks=int(args.get("clicks", 1) or 1),
                dx=int(args.get("dx", 0) or 0),
                dy=int(args.get("dy", -1) if args.get("dy") is not None else -1),
                duration=float(args.get("duration", 0) or 0),
            ),
            "keyboard": lambda: self._builtin_tools.keyboard(
                action=args.get("action", ""),
                text=args.get("text", "") or "",
                key=args.get("key", "") or "",
                keys=args.get("keys", "") or "",
            ),
            "screenshot": lambda: self._builtin_tools.screenshot(
                path=args.get("path", "") or "",
                region=(tuple(args.get("region", [])) if args.get("region") else None),
            ),
            # ========== LSP 工具 ==========
            "lsp": lambda: self._builtin_tools._lsp_tools.lsp(
                path=args.get("path", ""),
                operation=args.get("operation", "diagnostics"),
                line=int(args.get("line", 0) or 0),
                column=int(args.get("column", 0) or 0),
                language=args.get("language"),
            ),
            # ========== 团队协作工具 ==========
            "team_send_message": lambda: self._builtin_tools.team_send_message(
                to_agent=args.get("to_agent", ""),
                message=args.get("message", ""),
            ),
            "team_list_members": lambda: self._builtin_tools.team_list_members(),
            # ========== CodeGraph 代码智能 ==========
            "codegraph_explore": lambda: self._builtin_tools.codegraph_explore(
                query=args.get("query", ""),
                mode=args.get("mode", "explore"),
                depth=int(args.get("depth", 2) or 2),
                max_files=int(args.get("max_files", 50) or 50),
                kind=args.get("kind"),
                directory=args.get("directory"),
                limit=int(args.get("limit", 50) or 50),
                exact=args.get("exact", False),
            ),
        }

        # ========== 工具执行前的有效性检查 ==========
        # 在 UI 关闭场景下，即使方法开头检查通过，
        # lambda 执行期间 UI 可能被关闭，导致 BuiltinTools 访问崩溃
        if not self.is_valid():
            logger.warning("[ToolExecutor] ToolExecutor became invalid during hook phase")
            return ToolResult(False, error="UI has been closed, tool execution unavailable")

        executor = tool_map.get(tool_name)
        if executor:
            try:
                result = executor()
                # ========== 工具执行后的有效性检查（防御性）==========
                # 对于耗时操作（bash、subagent_batch），执行期间 UI 可能被关闭
                if not self.is_valid():
                    logger.warning(f"[ToolExecutor] ToolExecutor became invalid after tool execution: {tool_name}")
                    # 结果可能已被 UI 关闭中断，标记为不完整
                    if result and result.success:
                        result.success = False
                        result.content = (result.content or "") + "\n[警告: UI 在执行过程中已关闭]"
                # 文件操作成功后备份编辑后的文件（用于差异对比）
                if tool_name in self._FILE_OPS_TO_TRACK and result and result.success:
                    self._record_file_operation_after(
                        tool_name, args, file_path_before, local_session_id, local_call_id
                    )

                # 自动 LSP 诊断：文件编辑成功后，若开启则自动诊断
                if result and result.success and tool_name in ("write", "edit", "multi_edit"):
                    result = self._try_auto_lsp_diagnose(tool_name, args, result)

                # Trigger PostToolUse hook（统一方法，含结果回填）
                self._trigger_post_tool_use(tool_name, args, result)

                return result
            except Exception as e:
                # 文件编辑操作失败时清理备份
                if tool_name in self._FILE_OPS_TO_TRACK:
                    self._cleanup_backup_on_failure(local_session_id, local_call_id)
                err_result = ToolResult(False, error=f"Execution error: {str(e)}")
                self._trigger_post_tool_use(tool_name, args, err_result)
                return err_result

        return ToolResult(False, error=f"Unknown tool: {tool_name}")

    def _execute_mcp_tool(self, tool_name: str, args: dict) -> ToolResult:
        """执行 MCP 工具调用"""
        # 执行前检查 ToolExecutor 有效性
        if not self.is_valid():
            logger.warning(f"[ToolExecutor] ToolExecutor became invalid before MCP tool: {tool_name}")
            return ToolResult(False, error="UI has been closed, tool execution unavailable")

        # 获取 MCP Manager（单例，可能在 BuiltinTools 清理后仍存在）
        try:
            mcp_manager = self._builtin_tools._mcp_manager
        except AttributeError:
            logger.error("[ToolExecutor] _mcp_manager not accessible")
            return ToolResult(False, error="MCP 管理器不可用")

        if not mcp_manager.is_connected:
            return ToolResult(False, error="MCP 未连接，请先配置并连接 MCP 服务器")

        try:
            result = mcp_manager.call_tool_sync(tool_name, args)
        except TimeoutError as e:
            logger.error(f"[ToolExecutor] MCP tool '{tool_name}' timeout: {e}")
            err_result = ToolResult(False, error="MCP 工具调用超时，请稍后重试")
            self._trigger_post_tool_use(tool_name, args, err_result)
            return err_result
        except Exception as e:
            logger.error(f"[ToolExecutor] MCP tool '{tool_name}' raised exception: {e}")
            err_result = ToolResult(False, error=f"MCP 工具执行异常: {str(e)}")
            self._trigger_post_tool_use(tool_name, args, err_result)
            return err_result

        # 执行后检查 ToolExecutor 有效性（MCP 调用可能耗时较长）
        if not self.is_valid():
            logger.warning(f"[ToolExecutor] ToolExecutor became invalid after MCP tool: {tool_name}")
            # 不再尝试修改 result，避免访问已删除的 QObject 属性

        # Trigger PostToolUse hook for MCP tools
        self._trigger_post_tool_use(tool_name, args, result)

        return result

    def _try_recover_mcp_prefix(self, tool_name: str) -> Optional[str]:
        """
        防御性补全 MCP 工具名的 ``mcp__`` 前缀。

        LLM 偶尔会漏掉前缀（例如从 ``mcp_list_servers`` 拿到的裸名、
        历史对话记忆的短名）。在路由前尝试从已知 MCP 工具表中唯一性匹配，
        命中就补回完整名；无法唯一确定（如多个 server 同名）则返回 None，
        让调用走原来的失败路径，避免猜测。

        Args:
            tool_name: LLM 传来的原始工具名

        Returns:
            补全前缀后的工具名；不需要补全或无法唯一确定时返回 None
        """
        if not tool_name or not self._builtin_tools:
            return None
        # 已经有前缀的（可能是合法的非 MCP 工具），原样返回
        if tool_name.startswith("mcp__"):
            return tool_name
        try:
            mcp_manager = self._builtin_tools._mcp_manager
        except AttributeError:
            return None
        if not mcp_manager or not getattr(mcp_manager, "is_connected", False):
            return None

        prefix = getattr(mcp_manager, "TOOL_PREFIX", "mcp__")
        matches: List[str] = []
        for server_name, conn in mcp_manager._connections.items():
            if not getattr(conn, "session", None) or not getattr(conn, "enabled", True):
                continue
            for t in conn.tools:
                full = f"{prefix}{server_name}__{t.name}"
                # 接受两种入参形式：
                # 1) 'server__tool'（缺前缀但带 server）
                # 2) 'tool'（仅工具名，需在所有 server 中唯一）
                stripped = full[len(prefix) :]
                if tool_name == stripped or tool_name == t.name:
                    matches.append(full)

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(f"[ToolExecutor] MCP 前缀恢复歧义：'{tool_name}' 命中多个候选 {matches}，放弃补全")
        return None

    def set_sub_agent_manager(self, sub_agent_manager):
        """设置子智能体管理器（实例级 + 共享 BuiltinTools 回退）"""
        # 实例级引用：subagent_para/subagent_status lambda 使用此引用来路由到正确的窗口
        self._sub_agent_manager = sub_agent_manager
        # 共享 BuiltinTools 回退：供旧代码路径兼容
        if self._builtin_tools:
            self._builtin_tools._sub_agent_manager = sub_agent_manager
            self._builtin_tools._task_tools._sub_agent_manager = sub_agent_manager
            logger.info("[ToolExecutor] SubAgentManager attached to instance and BuiltinTools")
