# -*- coding: utf-8 -*-
"""
HookManager - Hooks 机制核心管理类 (增强版)
管理所有已注册的 Hooks，处理事件触发、匹配、异步执行

增强特性:
- 动态生命周期管理（热重载、enable/disable）
- 多种 Hook 类型（command、http、python function）
- 增强条件匹配（环境变量/文件类型/工具名多维度）
- 决策控制能力（block/continue）
- Skill 与 Hook 深度集成
"""

import json
import os
import re
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from uuid import uuid4

from loguru import logger
from PyQt5.QtCore import QObject, QRunnable, QThread, QThreadPool, pyqtSignal

# 常见脚本扩展名（用于从 hook command 中解析脚本路径，以确定 cwd）
_SCRIPT_EXTENSIONS = r"cmd|bat|ps1|sh|bash|py"

# 全局共享的并行执行器（延迟初始化，避免模块加载时创建线程）
_PARALLEL_EXECUTOR: Optional[ThreadPoolExecutor] = None
_PARALLEL_MAX_WORKERS = 4


def _get_parallel_executor() -> ThreadPoolExecutor:
    """获取全局共享的 ThreadPoolExecutor，用于并行执行 hook"""
    global _PARALLEL_EXECUTOR
    if _PARALLEL_EXECUTOR is None:
        _PARALLEL_EXECUTOR = ThreadPoolExecutor(
            max_workers=_PARALLEL_MAX_WORKERS,
            thread_name_prefix="hook_parallel",
        )
    return _PARALLEL_EXECUTOR


def shutdown_parallel_executor():
    """由应用退出路径调用，释放并行执行器资源"""
    global _PARALLEL_EXECUTOR
    if _PARALLEL_EXECUTOR is not None:
        _PARALLEL_EXECUTOR.shutdown(wait=False)
        _PARALLEL_EXECUTOR = None


def _is_ui_thread() -> bool:
    """检测当前是否运行在 Qt 主线程（UI 线程）上"""
    try:
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return False
        return QThread.currentThread() == app.thread()
    except ImportError:
        return False


def _parse_function_params(function_path: str) -> tuple:
    """从 Python hook 的函数路径中分离查询参数

    支持 URL 风格的参数内联写法：
        ".module:func?key1=val1&key2=val2" → (".module:func", {"key1": "val1", "key2": "val2"})
        ".module:func" → (".module:func", {})

    数值类参数自动转为 int/float，保持直观。

    Args:
        function_path: 原始函数路径（可能含 ? 参数）

    Returns:
        (clean_path: str, params: dict) — 清理后的路径和参数字典
    """
    if "?" not in function_path:
        return function_path, {}

    base_path, query = function_path.split("?", 1)
    params = {}
    if query:
        for pair in query.split("&"):
            if "=" in pair:
                key, val = pair.split("=", 1)
                key = key.strip()
                val = val.strip()
                # 自动类型转换：int → float → str
                try:
                    val = int(val) if "." not in val else float(val)
                except ValueError:
                    pass  # 保留字符串
                params[key] = val
    return base_path, params


class HookType(Enum):
    """Hook 类型"""

    COMMAND = "command"
    HTTP = "http"
    PYTHON = "python"
    PROMPT = "prompt"


class HookDecision(Enum):
    """Hook 决策结果"""

    CONTINUE = "continue"  # 继续执行
    BLOCK = "block"  # 阻止操作
    DEFER = "defer"  # 延迟执行


class HookConditionType(Enum):
    """条件类型"""

    ENV = "env"  # 环境变量条件，如 "env:DEBUG=true"
    FILE_PATTERN = "file"  # 文件模式匹配，如 "file:*.py"
    TOOL = "tool"  # 工具名匹配，如 "tool:bash"
    REGEX = "regex"  # 正则匹配，如 "regex:.*关键词.*"


@dataclass
class HookCondition:
    """单个条件配置"""

    type: str  # env, file, tool, regex
    pattern: str  # 条件模式

    @classmethod
    def from_dict(cls, d: dict) -> "HookCondition":
        return cls(type=d.get("type", "env"), pattern=d.get("pattern", ""))


@dataclass
class Hook:
    """
    单个 Hook 配置 (增强版)

    支持字段:
    - id: 唯一标识符
    - type: hook 类型 (command/http/python/prompt)
    - command: 执行命令 (command 类型)
    - url: HTTP 请求地址 (http 类型)
    - function: Python 函数路径 (python 类型)
    - prompt: 提示文本 (prompt 类型，直接插入到消息中)
    - cwd: 工作目录
    - add_output_to_context: 是否添加到上下文
    - skill_root: 所属技能根目录
    - enabled: 是否启用
    - timeout: 超时时间（秒）
    - retry: 重试次数
    - conditions: 执行条件列表
    """

    id: str = ""
    type: str = "command"
    command: str = ""
    cwd: Optional[str] = None
    add_output_to_context: bool = True
    skill_root: str = ""
    enabled: bool = True
    timeout: int = (
        30  # 默认超时 30s（W1：收紧自 300s，避免新建会话同步执行 hook 卡死 UI；hooks.json 显式 timeout 仍可覆盖）
    )
    retry: int = 0
    conditions: List[HookCondition] = field(default_factory=list)

    # HTTP 类型专用字段
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    allowed_env_vars: Optional[List[str]] = None

    # Python 类型专用字段
    function: Optional[str] = None
    function_args: Optional[Dict[str, Any]] = None

    # Prompt 类型专用字段
    prompt: Optional[str] = None

    # Windows 专用命令（Claude Code 插件兼容）
    commandWindows: Optional[str] = None
    # 状态信息（执行时显示的状态消息）
    statusMessage: Optional[str] = None

    # config_file: 所属的 hooks.json 配置文件路径（用于 UI 保存）
    config_file: Optional[str] = None

    # 是否来自系统内置插件（plugins/system/）。系统级 hook 在 UI 上禁止删除。
    # 该字段由 HookManager.register_hooks_from_json() 注入，不会写回源文件。
    is_system_plugin: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "Hook":
        conditions = [HookCondition.from_dict(c) for c in d.get("conditions", [])]
        hook_type = d.get("type", "command")
        # 根据类型规范化 command 值：优先取专用字段，再 fallback 到 command
        command = d.get("command", "")
        commandWindows = d.get("commandWindows") or d.get("command_windows", "")
        statusMessage = d.get("statusMessage") or d.get("status_message", "")
        if hook_type == "python":
            command = d.get("function") or command
        elif hook_type == "http":
            command = d.get("url") or command
        elif hook_type == "prompt":
            command = d.get("prompt") or command
        hook_id = d.get("id", "") or uuid4().hex
        return cls(
            id=hook_id,
            type=hook_type,
            command=command,
            commandWindows=commandWindows,
            statusMessage=statusMessage,
            cwd=d.get("cwd"),
            add_output_to_context=d.get("add_output_to_context", True),
            skill_root=d.get("skill_root", ""),
            enabled=d.get("enabled", True),
            timeout=d.get("timeout", 30),
            retry=d.get("retry", 0),
            conditions=conditions,
            url=d.get("url"),
            headers=d.get("headers"),
            allowed_env_vars=d.get("allowedEnvVars"),
            function=d.get("function"),
            function_args=d.get("function_args"),
            config_file=d.get("config_file"),
            prompt=d.get("prompt"),
            is_system_plugin=d.get("is_system_plugin", False),
        )

    def to_dict(self) -> dict:
        """转换为字典（用于序列化）"""
        result = {
            "id": self.id,
            "type": self.type,
            "command": self.command,
            "cwd": self.cwd,
            "add_output_to_context": self.add_output_to_context,
            "skill_root": self.skill_root,
            "enabled": self.enabled,
            "timeout": self.timeout,
            "retry": self.retry,
            "conditions": [{"type": c.type, "pattern": c.pattern} for c in self.conditions],
            "url": self.url,
            "headers": self.headers,
            "allowedEnvVars": self.allowed_env_vars,
            "function": self.function,
            "function_args": self.function_args,
            "config_file": self.config_file,
            "commandWindows": self.commandWindows,
            "statusMessage": self.statusMessage,
        }
        if self.prompt is not None:
            result["prompt"] = self.prompt
        return result


@dataclass
class HookMatchRule:
    """
    匹配规则，一个事件可以有多个匹配规则

    支持 matcher 类型:
    - "tool:xxx" - 工具名匹配
    - 普通正则表达式 - 匹配用户消息
    """

    matcher: Optional[str] = None
    hooks: List[Hook] = field(default_factory=list)
    # 归属 skill（注册时注入，作为 hook 来源标签的真相来源）。
    # 🛡️ 避免依赖 _skill_to_hooks 索引：索引会在热重载 insert/pop 后错位，
    # 而 rule 对象本身的位置无关，来源归属永远准确。
    skill_name: str = ""

    def __post_init__(self):
        if self.hooks is None:
            self.hooks = []

    def matches(self, context: Dict[str, Any]) -> bool:
        """检查规则是否匹配当前上下文"""
        if not self.matcher:
            return True

        event_name = context.get("event_name", "")

        # SessionStart 会话状态匹配
        # matcher 格式如 "startup|resume|clear|compact"，匹配 context["state"]
        # matcher="#team_member" 特例：仅当窗口是团队成员时触发（让 hooks.json 能为
        # 团队成员单独配置会话启动指引，避免普通会话看到团队相关提示）
        if event_name == "SessionStart":
            if self.matcher == "#team_member":
                return bool(context.get("is_team_member", False))
            session_state = context.get("state", "startup")
            states = [s.strip() for s in self.matcher.split("|")]
            return session_state in states

        # Stop 停止原因匹配（参考 SessionStart 的 state 模式）
        # matcher 格式如 "completed|cancelled|error"，匹配 context["reason"]
        # reason 字段由 chat_worker.py 在 3 处 Stop 触发点注入：
        #   - 正常完成 → "completed"
        #   - 用户取消 → "cancelled"
        #   - API 异常 → "error"
        # matcher="#team_member" 特例保持一致：仅团队成员窗口触发
        if event_name == "Stop":
            if self.matcher == "#team_member":
                return bool(context.get("is_team_member", False))
            stop_reason = context.get("reason", "completed")
            reasons = [r.strip() for r in self.matcher.split("|")]
            return stop_reason in reasons

        # BuildSystemPrompt 智能体角色匹配
        # matcher 格式如 "primary|subagent"，匹配 context["current_role"]
        if event_name == "BuildSystemPrompt":
            current_role = context.get("current_role", "primary")
            roles = [r.strip() for r in self.matcher.split("|")]
            return current_role in roles

        # 团队模式匹配：matcher="#team_member" 仅当窗口已加入团队时触发
        # 由 chat_worker._trigger_worker_hook 注入的 is_team_member 字段判断
        if self.matcher == "#team_member":
            return bool(context.get("is_team_member", False))

        # 工具名匹配（支持别名）
        if self.matcher.startswith("tool:"):
            pattern = self.matcher[5:]
            actual = context.get("tool_name", "")
            # 两边都用 ToolNameMapper 归一化（懒导入，避免触发 app.tools 全量加载）
            from app.tools.tool_name_mapper import ToolNameMapper

            return ToolNameMapper.to_native(pattern) == ToolNameMapper.to_native(actual)

        # 正则匹配用户消息
        message = context.get("message", "")
        try:
            if re.match(self.matcher, message):
                return True
        except re.error:
            pass

        # 对于 PostAssistantMessage/Stop 等事件，也尝试匹配 assistant 回复内容
        # context["response"] 是 DriFoxx 自有格式，context["assistant_response"] 是 Claude Code 兼容格式
        # 两个字段通常指向同一内容，取第一个非空值即可
        resp_text = context.get("assistant_response") or context.get("response", "")
        if resp_text:
            try:
                if re.match(self.matcher, resp_text):
                    return True
            except re.error:
                pass

        # 对于工具相关事件，也尝试匹配工具名（大小写不敏感）
        # 这样 "Write|Edit" 这样的 matcher 可以直接匹配工具名
        if event_name in ("PreToolUse", "PostToolUse"):
            tool_name = context.get("tool_name", "")
            if tool_name:
                try:
                    if re.match(self.matcher, tool_name, re.IGNORECASE):
                        return True
                except re.error:
                    pass

        return False

    @classmethod
    def from_dict(
        cls, d: dict, skill_root: str = "", config_file: str = "", is_system_plugin: bool = False
    ) -> "HookMatchRule":
        hooks = [Hook.from_dict(h) for h in d.get("hooks", [])]
        for h in hooks:
            h.skill_root = skill_root
            h.config_file = config_file  # 传递 config_file
            h.is_system_plugin = is_system_plugin  # 标记系统级 hook
        return cls(
            matcher=d.get("matcher"),
            hooks=hooks,
        )


class HookExecutionResult:
    """Hook 执行结果"""

    def __init__(
        self,
        success: bool,
        output: str = "",
        decision: str = "continue",
        status_message: str = "",
        add_to_context: bool = True,
    ):
        self.success = success
        self.output = output
        self.decision: HookDecision = HookDecision(decision) if decision else HookDecision.CONTINUE
        self.status_message = status_message
        # 标记此结果是否应注入到消息列表
        # 对应 Hook.add_output_to_context，由 _execute_hook 传递
        self.add_to_context = add_to_context


class HookWorkerSignals(QObject):
    """Worker 信号，用于执行完后回调"""

    finished = pyqtSignal(str, str, bool, str)  # event_name, output, success, status_message
    status_changed = pyqtSignal(str, str, bool)  # event_name, status_message, is_start


class HookWorker(QRunnable):
    """异步执行 Hook 的 Worker"""

    def __init__(
        self,
        hook: Hook,
        cwd: Optional[str],
        signals: HookWorkerSignals,
        event_name: str = "",
        context: Dict[str, Any] = None,
    ):
        super().__init__()
        self.hook = hook
        self.cwd = cwd
        self.signals = signals
        self.event_name = event_name
        self.context = context or {}

    @staticmethod
    def _run_command_sync(
        command: str,
        cwd: Optional[str] = None,
        timeout: int = 300,
        stdin_data: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> tuple:
        """
        统一的命令同步执行方法（公共提取，避免代码重复）。
        返回 (output, success, exit_code)。
        处理 echo 快捷方式、Windows 编码回退、路径分隔符转换。
        stdin_data: 可选的 stdin 输入（传递给脚本的 JSON 上下文）
        extra_env: 额外环境变量，合并到当前环境后传递给子进程
        """
        # echo 快捷方式 — 仅对纯单行 echo 有效，无任何命令分隔符
        # 排除换行/&&/||/;/&/| 等，避免截胡多命令（如 echo "msg"\nexit 2）
        if (
            command.startswith("echo ")
            and "\n" not in command
            and "&&" not in command
            and "||" not in command
            and ";" not in command
            and " & " not in command
            and " |" not in command
        ):
            output = command[5:].strip()
            if output.startswith('"') and output.endswith('"'):
                output = output[1:-1]
            elif output.startswith("'") and output.endswith("'"):
                output = output[1:-1]
            return output, True, 0

        # 修复路径分隔符问题：Unix / 转 Windows \
        if os.name == "nt":
            command = command.replace("/", "\\")
            # Windows cmd.exe 对 `subprocess.run(..., shell=True)` 传入的多行命令里 `\n`
            # 处理不可靠（实际只跑第一行）。统一转换为 `&` 分隔符确保 exit 2 等能真正执行。
            if "\n" in command:
                command = command.replace("\n", " & ")

        # 构造 subprocess 参数
        subprocess_kwargs = {
            "cwd": cwd,
            "shell": True,
            "capture_output": True,
            "text": True,
            "errors": "replace",
            "timeout": timeout,
            "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        }
        # 注入额外环境变量（Claude Code 兼容变量等），合并到当前环境
        if extra_env:
            merged = dict(os.environ)
            merged.update(extra_env)
            subprocess_kwargs["env"] = merged
        if stdin_data is not None:
            subprocess_kwargs["input"] = stdin_data
        else:
            subprocess_kwargs["stdin"] = subprocess.DEVNULL

        if os.name == "nt":
            import locale

            # Windows: cmd.exe 的 echo 输出编码跟随系统代码页（中文 Windows 通常 GBK/gb2312），
            # 用 UTF-8 解码会变乱码。优先用系统首选编码，与 cmd.exe 输出一致。
            preferred = locale.getpreferredencoding(False) or "gbk"
            if preferred.upper() in ("UTF-8", "UTF8"):
                enc = "utf-8"
            else:
                enc = preferred
            try:
                result = subprocess.run(command, encoding=enc, **subprocess_kwargs)
            except UnicodeDecodeError, LookupError:
                # 解码失败时回退到 UTF-8 with errors='replace'
                result = subprocess.run(command, encoding="utf-8", **subprocess_kwargs)
            exit_code = result.returncode
            if exit_code != 0:
                # exit code 2: Claude Code BLOCK 约定，用 stdout 作为 output
                if exit_code == 2:
                    return result.stdout or "", False, exit_code
                return result.stderr or f"Command failed with exit code {exit_code}", False, exit_code
            return result.stdout or "", True, exit_code
        else:
            result = subprocess.run(command, encoding="utf-8", **subprocess_kwargs)

        exit_code = result.returncode
        if exit_code != 0:
            # exit code 2: Claude Code BLOCK 约定，用 stdout 作为 output
            if exit_code == 2:
                return result.stdout or "", False, exit_code
            return result.stderr or f"Command failed with exit code {exit_code}", False, exit_code
        return result.stdout or "", True, exit_code

    def run(self):
        """执行命令，收集输出"""
        status_message = self.hook.statusMessage or ""

        # 注意：is_start=True 由 HookManager._execute_hook() 在主线程上直接发射，
        # 异步场景下 worker 不再重复发射 start 事件，避免 UI 收到两次开始回调。
        try:
            output = ""
            success = False

            if self.hook.type == HookType.COMMAND.value:
                output, success = self._execute_command()
            elif self.hook.type == HookType.HTTP.value:
                output, success = self._execute_http()
            elif self.hook.type == HookType.PYTHON.value:
                output, success = self._execute_python()
            elif self.hook.type == HookType.PROMPT.value:
                output, success = self._execute_prompt()
            else:
                output = f"Unknown hook type: {self.hook.type}"
                success = False

            # 发出执行结束状态
            if status_message:
                self.signals.status_changed.emit(self.event_name, status_message, False)

            self.signals.finished.emit(self.event_name, output, success, status_message)
        except Exception as e:
            logger.error(f"[HookWorker] Execution failed: {e}")
            if status_message:
                self.signals.status_changed.emit(self.event_name, status_message, False)
            self.signals.finished.emit(self.event_name, f"Error: {str(e)}", False, status_message)

    def _execute_prompt(self) -> tuple:
        """执行 prompt 类型：直接返回 prompt 文本"""
        prompt_text = self.hook.prompt or self.hook.command or ""
        return prompt_text, True

    def _execute_command(self) -> tuple:
        """执行命令（委托给公共静态方法），传递 context 作为 stdin"""
        import json as _json
        import os

        # Windows 上优先使用 commandWindows（Claude Code 插件兼容）
        effective_cmd = self.hook.command
        if os.name == "nt" and self.hook.commandWindows:
            effective_cmd = self.hook.commandWindows
        # 变量插值（含 ${CLAUDE_PLUGIN_ROOT} 等插件路径变量）
        effective_cmd = HookManager._interpolate_variables(effective_cmd, self.context)
        stdin_data = _json.dumps(self.context) if self.context else None
        # 注入 Claude Code 兼容环境变量（第三方插件依赖）
        extra_env = HookManager._build_claude_env(self.context)
        output, success, _ = HookWorker._run_command_sync(
            effective_cmd,
            self.cwd,
            self.hook.timeout,
            stdin_data=stdin_data,
            extra_env=extra_env,
        )
        return output, success

    def _execute_http(self) -> tuple:
        """执行 HTTP 请求"""
        try:
            import urllib.error
            import urllib.request

            url = self.hook.url
            headers = self.hook.headers or {}
            headers["Content-Type"] = "application/json"

            # 构建请求数据
            data = json.dumps(
                {
                    "event": self.event_name,
                    "context": self.context,
                }
            ).encode("utf-8")

            req = urllib.request.Request(url, data=data, headers=headers, method="POST")

            with urllib.request.urlopen(req, timeout=self.hook.timeout) as response:
                output = response.read().decode("utf-8")
                return output, True
        except Exception as e:
            return f"HTTP request failed: {str(e)}", False

    # 相对模块函数缓存：{(config_file, function_path): Callable}
    _relative_func_cache: Dict[str, Callable] = {}

    @classmethod
    def _clear_relative_func_cache(cls):
        """清除相对模块函数缓存（热重载时调用）"""
        cls._relative_func_cache.clear()

    @staticmethod
    def _import_relative_function(function_path: str, config_file: str) -> Optional[Callable]:
        """从相对模块路径导入函数（.module:func → <config_dir>/module.py 中的 func）

        缓存已导入的模块函数，避免每次 hook 触发都重复 exec_module。

        Args:
            function_path: 函数路径，如 .evolver_hook:hook_session_start
            config_file: hooks.json 的完整路径

        Returns:
            可调用的函数对象，失败返回 None
        """
        # 缓存键：(config_file, function_path) 确保不同 hooks.json 的同名模块不冲突
        cache_key = f"{config_file}::{function_path}"
        cached = HookWorker._relative_func_cache.get(cache_key)
        if cached is not None:
            return cached

        parts = function_path.rsplit(":", 1)
        if len(parts) != 2:
            return None

        module_path, func_name = parts
        if not module_path.startswith("."):
            return None  # 非相对路径，让调用方用标准方式处理

        # 标准化：去掉前导点，将点号转为路径分隔符
        relative_dotted = module_path.lstrip(".")
        relative_path = relative_dotted.replace(".", "/")
        py_file = f"{relative_path}.py"

        # 基于 hooks.json 所在目录解析
        config_dir = Path(config_file).resolve().parent
        abs_path = config_dir / py_file

        if not abs_path.exists():
            logger.error(f"[HookWorker] Relative module not found: {abs_path}")
            return None

        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                f"_hook_relative_{relative_dotted.replace('/', '_')}", str(abs_path)
            )
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            func = getattr(module, func_name, None)
            if callable(func):
                HookWorker._relative_func_cache[cache_key] = func
                return func
            return None
        except Exception as e:
            logger.error(f"[HookWorker] Failed to load relative module {abs_path}: {e}")
            return None

    def _execute_python(self) -> tuple:
        """执行 Python 函数"""
        try:
            if not self.hook.function:
                return "No function specified", False

            # 解析函数路径中的内联参数（?key=val 语法）
            clean_function, inline_params = _parse_function_params(self.hook.function)

            # 解析函数路径 (module.path:function_name)
            parts = clean_function.rsplit(":", 1)
            if len(parts) != 2:
                return f"Invalid function path: {self.hook.function}", False

            module_path, func_name = parts

            # 相对路径：基于 hooks.json 目录解析
            func = None
            if module_path.startswith(".") and self.hook.config_file:
                func = HookWorker._import_relative_function(clean_function, self.hook.config_file)

            # 标准路径：importlib.import_module
            if func is None:
                import importlib

                module = importlib.import_module(module_path)
                func = getattr(module, func_name, None)

            if not callable(func):
                return f"Function not found: {self.hook.function}", False

            # 执行函数（合并：内联参数 > function_args > event/context 标准参数）
            args = dict(inline_params)
            args.update(self.hook.function_args or {})
            args["event"] = self.event_name
            args["context"] = self.context

            result = func(**args)

            if isinstance(result, str):
                return result, True
            elif isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False), True
            else:
                return str(result), True
        except Exception as e:
            return f"Python function failed: {str(e)}", False


class HookManager:
    """
    Hook 管理器 (增强版)

    功能特性:
    - 动态生命周期管理（热重载、enable/disable）
    - 多种 Hook 类型（command、http、python）
    - 增强条件匹配
    - 决策控制能力
    - Skill 深度集成
    """

    # 允许执行 Python 函数的模块白名单
    SAFE_PYTHON_MODULES = {"app.hooks", "app.utils"}

    # 跨窗口共享的 hooks 注册数据（只加载一次，所有窗口复用）
    _shared_hooks: Dict[str, List[HookMatchRule]] = {}
    _shared_skill_to_hooks: Dict[str, List[tuple[str, int]]] = {}
    _shared_config_watchers: Dict[str, float] = {}
    _shared_registered_functions: Dict[str, Callable] = {}
    # 共享的 cwd 解析缓存
    _shared_cwd_resolve_cache: Dict[int, tuple] = {}
    # 跨窗口共享的 hook 开关状态（类级共享，避免多实例各自快照互相覆盖）
    _shared_hook_states: Dict[str, bool] = {}
    _shared_hook_overrides: Dict[str, Dict[str, Any]] = {}
    # 热重载顺序快照：{skill_name: {event_name: [(hook_id, owner_skill), ...]}}
    # unregister 前记录事件内完整 rule 顺序（用首个 hook.id 标识 rule），
    # 重新注册时按快照归并重建，精确恢复交错顺序（不依赖脆弱的索引计算）。
    _shared_restore_snapshots: Dict[str, Dict[str, List[tuple]]] = {}

    def __init__(self, thread_pool: Optional[QThreadPool] = None):
        # hooks 注册数据指向类级别的共享字典（所有窗口共用）
        self._hooks: Dict[str, List[HookMatchRule]] = HookManager._shared_hooks
        self._skill_to_hooks: Dict[str, List[tuple[str, int]]] = HookManager._shared_skill_to_hooks

        # 线程池
        self._thread_pool = thread_pool or QThreadPool.globalInstance()

        # 完成回调（每个窗口独立）(event_name, output, success, status_message)
        self._on_finished_callback: Optional[Callable[[str, str, bool, str], None]] = None

        # 决策回调（每个窗口独立）
        self._on_decision_callback: Optional[Callable[[str, HookDecision], None]] = None

        # 执行状态回调（每个窗口独立）(event_name, status_message, is_start)
        self._on_status_callback: Optional[Callable[[str, str, bool], None]] = None

        # 配置热重载监控（类级别共享）
        self._config_watchers: Dict[str, float] = HookManager._shared_config_watchers
        self._config_file: Optional[str] = None

        # 注册的 Python 函数（类级别共享）
        self._registered_functions: Dict[str, Callable] = HookManager._shared_registered_functions

        # cwd 解析缓存（类级别共享）
        self._cwd_resolve_cache: Dict[int, tuple] = HookManager._shared_cwd_resolve_cache
        self._CWD_CACHE_TTL = 30.0  # 30秒缓存

        # hook 开关持久化（所有 hook 共用，不受插件源文件限制）
        # 🛡️ 类级共享：backend 与 settings popup 各自创建 HookManager 实例时，
        # 若每个实例独立从磁盘加载快照，后保存者会用旧快照覆盖先保存者的修改
        # （开关莫名变化根因之一）。改为类级共享字典，只在首次实例化时加载磁盘。
        self._hook_states: Dict[str, bool] = HookManager._shared_hook_states
        if not self._hook_states:
            self._hook_states.update(self._load_hook_states())

        # hook 内容覆盖持久化（系统 hook 编辑覆盖，与 hook_states 共享同一文件）
        # 存储格式: {hook_id: {"command": "...", "statusMessage": "...", ...}}
        # 加载时覆盖系统插件源文件中的默认值，实现系统 hook 可编辑不丢失
        # 🛡️ 类级共享（同上，避免多实例互相覆盖）
        self._hook_overrides: Dict[str, Dict[str, Any]] = HookManager._shared_hook_overrides
        if not self._hook_overrides:
            self._hook_overrides.update(self._load_hook_overrides())

    @staticmethod
    def _get_hook_states_path() -> str:
        """获取 hook 状态持久化文件路径"""
        from app.utils.utils import get_app_data_dir

        data_dir = get_app_data_dir() / "plugins" / "user-custom" / "hooks"
        return str(data_dir / "hook_states.json")

    def _load_hook_states(self) -> Dict[str, bool]:
        """从磁盘加载所有 hook 的开关状态（过滤 _overrides 键）"""
        fp = self._get_hook_states_path()
        if not os.path.exists(fp):
            return {}
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if k != "_overrides" and isinstance(v, bool)}
        except Exception:
            return {}

    def _load_hook_overrides(self) -> Dict[str, Dict[str, Any]]:
        """从 hook_states.json 加载 hook 内容覆盖（系统 hook 编辑持久化）"""
        fp = self._get_hook_states_path()
        if not os.path.exists(fp):
            return {}
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("_overrides", {})
        except Exception:
            return {}

    def _save_hook_states(self):
        """将所有 hook 的开关状态 + 内容覆盖写入磁盘"""
        fp = self._get_hook_states_path()
        try:
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            # 合并状态和覆盖到同一文件
            data: dict = dict(self._hook_states)  # 复制状态
            if self._hook_overrides:
                data["_overrides"] = dict(self._hook_overrides)  # 追加覆盖
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[HookManager] Failed to save hook states: {e}")

    def _apply_hook_state(self, hook: Hook):
        """将持久化的开关状态应用到 hook 对象（如果存在）"""
        if hook.id in self._hook_states:
            hook.enabled = self._hook_states[hook.id]

    def _apply_hook_overrides(self, hook: Hook, rule: HookMatchRule = None):
        """将持久化的内容覆盖应用到 hook 对象（系统 hook 编辑持久化）

        从 _hook_overrides 中读取 hook.id 对应的覆盖字段，写入 Hook 对象属性。
        支持所有 edit_hook_by_id 能处理的字段，包括 matcher（写入 rule）。

        Args:
            hook: 要应用覆盖的 Hook 对象
            rule: 包含该 hook 的匹配规则（用于 matcher 覆盖）
        """
        overrides = self._hook_overrides.get(hook.id)
        if not overrides:
            return

        # ── 应用 matcher（写入 rule 级别） ──
        if "matcher" in overrides and rule is not None:
            rule.matcher = overrides["matcher"] or None

        # ── 应用 Hook 对象字段 ──
        field_mapping = {
            "type": "type",
            "command": "command",
            "url": "url",
            "function": "function",
            "prompt": "prompt",
            "cwd": "cwd",
            "add_output_to_context": "add_output_to_context",
            "timeout": "timeout",
            "retry": "retry",
            "commandWindows": "commandWindows",
            "statusMessage": "statusMessage",
            "function_args": "function_args",
        }
        for key, attr in field_mapping.items():
            if key in overrides:
                setattr(hook, attr, overrides[key])

    def _is_user_custom_hook(self, hook: Hook) -> bool:
        """判断 hook 是否属于 user-custom skill（可安全写回源文件）

        通过 _skill_to_hooks 查找 hook.id 所在的 skill_name，
        避免把非系统插件（如普通 skill 插件）的 hook 误判为自定义 hook。
        """
        # 先通过 config_file 路径快速判断
        if hook.config_file and "user-custom" in hook.config_file.replace("\\", "/"):
            return True
        # 兜底：按 rule.skill_name 判断（P021 修复：不依赖索引，索引可能错位）
        for event_name, rules in self._hooks.items():
            for rule in rules:
                if getattr(rule, "skill_name", "") != "user-custom":
                    continue
                for h in rule.hooks:
                    if h.id == hook.id:
                        return True
        return False

    def _get_effective_hook_dict(self, hook: Hook) -> dict:
        """获取 hook 的字典表示"""
        return hook.to_dict()

    def set_on_finished_callback(self, callback: Callable[[str, str, bool, str], None]):
        """设置 Hook 执行完成回调 (event_name, output, success, status_message)"""
        self._on_finished_callback = callback

    def set_on_status_callback(self, callback: Optional[Callable[[str, str, bool], None]]):
        """设置执行状态回调 (event_name, status_message, is_start)"""
        self._on_status_callback = callback

    def set_on_decision_callback(self, callback: Callable[[str, HookDecision], None]):
        """设置决策回调 (当 hook 返回 block/continue 等决策时调用)"""
        self._on_decision_callback = callback

    def register_function(self, name: str, func: Callable):
        """注册 Python 函数供 hooks 调用"""
        self._registered_functions[name] = func
        logger.debug(f"[HookManager] Registered function: {name}")

    def unregister_function(self, name: str):
        """注销 Python 函数"""
        if name in self._registered_functions:
            del self._registered_functions[name]
            logger.debug(f"[HookManager] Unregistered function: {name}")

    def register_hooks_from_json(
        self,
        skill_name: str,
        skill_root: str,
        hooks_config: Union[dict, str],
        config_file: str = None,
        is_system_plugin: bool = False,
    ) -> int:
        """
        从 JSON 加载 hooks 配置

        支持两种格式:
        1. 新格式 (带 hooks 数组):
           {"hooks": {"EventName": [{"matcher": "...", "hooks": [...]}]}}

        2. 旧格式 (简化):
           {"EventName": [{"command": "..."}]}

        注意：相同的 config_file 只注册一次，防止重复注册。
        """
        # 处理字符串路径
        if isinstance(hooks_config, str):
            config_file = hooks_config
            try:
                with open(hooks_config, "r", encoding="utf-8") as f:
                    hooks_config = json.load(f)
            except Exception as e:
                logger.error(f"[HookManager] Failed to load hooks from {hooks_config}: {e}")
                return 0

        # 去重：相同的 config_file 只注册一次
        if config_file and config_file in self._config_watchers:
            logger.debug(f"[HookManager] Skipping already loaded config: {config_file}")
            return 0

        # 保存配置文件的监控时间
        if config_file:
            self._config_file = config_file
            try:
                self._config_watchers[config_file] = os.path.getmtime(config_file)
            except OSError:
                pass

        # 检测配置格式
        raw_hooks = hooks_config.get("hooks", hooks_config)

        # 🛡️ 热重载顺序恢复：unregister 时记录的事件内顺序快照（hook.id 标识），
        # 重新注册后按快照归并重建，精确恢复交错顺序。
        # 不用旧索引方案：insert 恢复时的索引位移计算在多 skill 交错下必然出错，
        # 导致 _skill_to_hooks 错位 → UI 来源标签错乱（P021）。
        snapshot = HookManager._shared_restore_snapshots.pop(skill_name, None)

        count = 0
        for event_name, rules in raw_hooks.items():
            if event_name not in self._hooks:
                self._hooks[event_name] = []

            # 标准化规则格式
            if isinstance(rules, list):
                for rule_data in rules:
                    if isinstance(rule_data, str):
                        # 简化格式: 直接是命令
                        rule_data = {"hooks": [{"type": "command", "command": rule_data}]}
                    elif "command" in rule_data and "hooks" not in rule_data:
                        # 旧格式兼容
                        rule_data = {"hooks": [rule_data]}

                    match_rule = HookMatchRule.from_dict(
                        rule_data, skill_root, config_file, is_system_plugin=is_system_plugin
                    )
                    # 注入归属 skill（来源标签的真相来源）
                    match_rule.skill_name = skill_name
                    if match_rule.hooks:
                        # 统一 append：不手工 insert（顺序恢复由 _restore_rule_order 完成）
                        self._hooks[event_name].append(match_rule)
                        count += len(match_rule.hooks)
            else:
                logger.warning(f"[HookManager] Invalid rules format for {event_name}")

        logger.info(f"[HookManager] Registered {count} hooks for skill {skill_name}")

        # 持久化生成的 hook id 到源文件（确保下次启动 id 不变）
        # ⚠️ 必须在顺序恢复之前：此时内存顺序=文件顺序，id 对齐才可靠
        if count > 0 and config_file:
            self._persist_hook_ids_to_file(config_file)

        # 热重载顺序恢复（基于 hook.id 归并，不依赖索引）
        if count > 0 and snapshot:
            self._restore_rule_order(skill_name, snapshot)

        # 重建 _skill_to_hooks 索引（从 rule.skill_name 全量派生，保证与 _hooks 一致）
        self._rebuild_skill_to_hooks()

        # 从持久化的状态恢复已注册 hook 的开关和内容覆盖（仅系统 hook）
        # 非系统 hook 的状态以源文件为准（双轨制：插件 hook 写回源文件，不走覆盖层）
        if count > 0:
            for event_name, rules in raw_hooks.items():
                if event_name not in self._hooks:
                    continue
                for rule in self._hooks[event_name]:
                    for hook in rule.hooks:
                        if not hook.is_system_plugin:
                            continue
                        # 恢复开关状态（覆盖插件源文件中的默认值）
                        if self._hook_states:
                            self._apply_hook_state(hook)
                        # 应用内容覆盖（系统 hook 编辑持久化，如 command/prompt/statusMessage 等）
                        if self._hook_overrides:
                            self._apply_hook_overrides(hook, rule)

        return count

    def migrate_legacy_hook_states(self) -> int:
        """一次性迁移旧覆盖层数据（必须在所有 hooks 注册完成后调用）

        规则：
        - 非系统 hook 条目 → 写回源文件 enabled → 从 _hook_states 删除
        - 幽灵 id（内存中找不到 hook）→ 直接删除
        - 系统 hook 条目 → 保留
        单条失败跳过，下次启动重试。

        Returns:
            处理的条数
        """
        if not self._hook_states:
            return 0
        processed = 0
        dirty = False
        for hook_id, enabled in list(self._hook_states.items()):
            found = self._find_hook_by_id(hook_id)
            if found is None:
                # 幽灵 id（本机无此 hook：插件已删/多端同步残留）
                del self._hook_states[hook_id]
                dirty = True
                processed += 1
                continue
            _, _, _, hook = found
            if hook.is_system_plugin:
                continue  # 系统 hook 保留覆盖层
            # 非系统 hook：写回源文件
            if hook.config_file and os.path.exists(hook.config_file):
                ok = self._save_hook_to_file_by_id(hook, {"enabled": enabled})
                if ok:
                    del self._hook_states[hook_id]
                    dirty = True
                    processed += 1
                else:
                    logger.warning(f"[HookManager] Migration failed for {hook_id}, will retry next start")
            else:
                del self._hook_states[hook_id]
                dirty = True
                processed += 1
        if dirty:
            self._save_hook_states()
        if processed:
            logger.info(f"[HookManager] Migrated {processed} legacy hook states")
        return processed

    def _persist_hook_ids_to_file(self, config_file: str):
        """将内存中 hook 的 id 写回到 JSON 配置文件

        按 config_file 匹配内存中的 hook，而不是按全局位置索引。
        避免不同来源（如不同插件）的 hook 拿到相同的 id。
        """
        if not config_file or not os.path.exists(config_file):
            return
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            raw_hooks = config.get("hooks", config)
            modified = False

            # 收集所有属于这个 config_file 的 hook id，按 event 内顺序排列
            config_file_norm = os.path.normpath(config_file)
            mem_ids: Dict[str, List[str]] = {}  # event_name -> [hook_id, ...]
            for event_name, rules in self._hooks.items():
                for rule in rules:
                    for hook in rule.hooks:
                        if hook.config_file and os.path.normpath(hook.config_file) == config_file_norm:
                            if event_name not in mem_ids:
                                mem_ids[event_name] = []
                            mem_ids[event_name].append(hook.id)

            # 遍历文件中的 hook，按 event 内顺序逐个分配 id
            # 🛡️ 防重复：分配前检查候选 id 是否已被文件其他 hook 占用。
            # 正常注册（文件顺序=内存顺序）下 idx 天然对齐；文件被外部改动
            # （gitee 同步/手动编辑）导致顺序不一致时，跳过已占用 id 而非
            # 错配成重复 id（重复 id 会导致开关状态串台）。
            existing_ids = set()
            for _rules in raw_hooks.values():
                for _rule in _rules:
                    for _h in _rule.get("hooks", []):
                        if _h.get("id"):
                            existing_ids.add(_h["id"])

            for event_name, rules in raw_hooks.items():
                ids = mem_ids.get(event_name, [])
                idx = 0
                for rule in rules:
                    for h in rule.get("hooks", []):
                        if not h.get("id"):
                            # 从当前位置向后找第一个未被占用的内存 id
                            while idx < len(ids) and ids[idx] in existing_ids:
                                idx += 1
                            if idx < len(ids):
                                h["id"] = ids[idx]
                                existing_ids.add(ids[idx])
                                modified = True
                                idx += 1
                            else:
                                logger.warning(
                                    f"[HookManager] No available id for hook in {event_name}: "
                                    f"{h.get('command', '')[:40]}"
                                )
                        else:
                            # 有 id 的也算进序号计数（保持位置对应）
                            idx += 1

            if modified:
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                logger.debug(f"[HookManager] Persisted hook ids to {config_file}")
        except Exception as e:
            logger.error(f"[HookManager] Failed to persist hook ids to {config_file}: {e}")

    def unregister_skill_hooks(self, skill_name: str):
        """注销一个技能的所有 Hooks

        🛡️ 按归属删除：通过 rule.skill_name 定位（而非 _skill_to_hooks 索引），
        索引错位时不会误删其他 skill 的 rule。
        🛡️ 顺序快照：删除前记录各事件内完整 rule 顺序（hook.id 标识），
        供重新注册时精确恢复交错顺序（P021 修复，替代脆弱的索引恢复）。
        """
        if skill_name not in self._skill_to_hooks:
            return

        # 记录事件内顺序快照（供热重载恢复）：
        # {event_name: [(rule 首个 hook.id, owner_skill), ...]}
        snapshot: Dict[str, List[tuple]] = {}
        for event_name, rules in self._hooks.items():
            snapshot[event_name] = [
                (rule.hooks[0].id if rule.hooks else "", getattr(rule, "skill_name", "")) for rule in rules
            ]
        HookManager._shared_restore_snapshots[skill_name] = snapshot

        # 按归属过滤删除本 skill 的 rule（不依赖索引，索引错位也不误删）
        empty_events: List[str] = []
        for event_name, rules in self._hooks.items():
            kept = [r for r in rules if getattr(r, "skill_name", "") != skill_name]
            if kept:
                self._hooks[event_name] = kept
            else:
                empty_events.append(event_name)
        # 删除空事件（在遍历结束后统一删，避免迭代中修改字典）
        for event_name in empty_events:
            del self._hooks[event_name]

        # 全量重建索引（pop 后其他 skill 的索引位移由重建统一修正）
        del self._skill_to_hooks[skill_name]
        self._rebuild_skill_to_hooks()
        logger.debug(f"[HookManager] Unregistered all hooks for skill {skill_name}")

    def _rebuild_skill_to_hooks(self):
        """从 rule.skill_name 全量重建 _skill_to_hooks 索引

        _skill_to_hooks 是纯派生数据：任何 rule 增删/移动后调用本方法，
        保证索引与 _hooks 实际位置严格一致（P021 根因修复）。
        """
        new_index: Dict[str, List[tuple[str, int]]] = {}
        for event_name, rules in self._hooks.items():
            for rule_idx, rule in enumerate(rules):
                owner = getattr(rule, "skill_name", "")
                if owner:
                    new_index.setdefault(owner, []).append((event_name, rule_idx))
        self._skill_to_hooks.clear()
        self._skill_to_hooks.update(new_index)

    def _restore_rule_order(self, skill_name: str, snapshot: Dict[str, List[tuple]]):
        """按 unregister 时的事件内顺序快照重建 rule 顺序（精确恢复交错）

        用 rule 首个 hook.id 匹配归属（id 由 _persist_hook_ids_to_file 持久化，
        热重载后不变）。非本 skill 的 rule 保持相对顺序。
        """
        for event_name, order in snapshot.items():
            rules = self._hooks.get(event_name)
            if not rules:
                continue
            mine: List = []
            others: List = []
            for r in rules:
                (mine if getattr(r, "skill_name", "") == skill_name else others).append(r)
            mine_by_id = {r.hooks[0].id: r for r in mine if r.hooks}
            rebuilt: List = []
            for hook_id, owner in order:
                if owner == skill_name:
                    r = mine_by_id.pop(hook_id, None)
                    if r is not None:
                        rebuilt.append(r)
                    # id 不匹配（文件被外部改动）：跳过该位置，其余继续按序补位
                elif others:
                    rebuilt.append(others.pop(0))
            # 残余（新文件新增的 rule / 快照外新增的 rule）追加到末尾
            rebuilt.extend(mine_by_id.values())
            rebuilt.extend(others)
            self._hooks[event_name] = rebuilt

    def _clear_config_watcher(self, config_file: str):
        """清除配置去重缓存，允许同一文件用不同 skill_name 重新注册

        用于增量热更新：当 hooks 从旧 key 迁移到新 key 时，
        需要先清除 _config_watchers 中的条目，否则 register_hooks_from_json
        会因为去重检查而跳过新 key 的注册。
        """
        if config_file in self._config_watchers:
            del self._config_watchers[config_file]

    # ========== 动态生命周期管理 API ==========

    def enable_hook(self, skill_name: str, event_name: str, hook_index: int) -> bool:
        """启用指定的 Hook"""
        if event_name not in self._hooks:
            return False
        if hook_index >= len(self._hooks[event_name]):
            return False

        rule = self._hooks[event_name][hook_index]
        if hook_index < len(rule.hooks):
            rule.hooks[hook_index].enabled = True
            logger.info(f"[HookManager] Enabled hook: {event_name}[{hook_index}]")
            return True
        return False

    def disable_hook(self, skill_name: str, event_name: str, hook_index: int) -> bool:
        """禁用指定的 Hook"""
        if event_name not in self._hooks:
            return False
        if hook_index >= len(self._hooks[event_name]):
            return False

        rule = self._hooks[event_name][hook_index]
        if hook_index < len(rule.hooks):
            rule.hooks[hook_index].enabled = False
            logger.info(f"[HookManager] Disabled hook: {event_name}[{hook_index}]")
            return True
        return False

    def dynamic_register_hook(
        self, skill_name: str, event_name: str, hook: Union[Hook, dict], matcher: str = None
    ) -> int:
        """
        动态注册单个 Hook

        Args:
            skill_name: 所属技能名
            event_name: 事件名
            hook: Hook 配置
            matcher: 匹配规则 (可选)

        Returns:
            注册的 hook 索引，-1 表示失败
        """
        if isinstance(hook, dict):
            hook = Hook.from_dict(hook)

        if event_name not in self._hooks:
            self._hooks[event_name] = []

        rule = HookMatchRule(matcher=matcher, hooks=[hook])
        rule.skill_name = skill_name
        rule_index = len(self._hooks[event_name])
        self._hooks[event_name].append(rule)
        self._rebuild_skill_to_hooks()

        logger.info(f"[HookManager] Dynamically registered hook: {event_name} for {skill_name}")
        return rule_index

    def dynamic_unregister_hook(self, skill_name: str, event_name: str, hook_index: int) -> bool:
        """
        动态注销单个 Hook

        Args:
            skill_name: 所属技能名
            event_name: 事件名
            hook_index: Hook 索引

        Returns:
            是否成功
        """
        if event_name not in self._hooks:
            return False

        rules = self._hooks[event_name]
        if hook_index >= len(rules):
            return False

        rules.pop(hook_index)

        # 全量重建索引（pop 后位移由重建统一修正）
        self._rebuild_skill_to_hooks()

        logger.info(f"[HookManager] Dynamically unregistered hook: {event_name}[{hook_index}]")
        return True

    def reload_hooks_config(self, config_file: str = None) -> bool:
        """
        热重载 hooks 配置

        Args:
            config_file: 配置文件路径，默认使用上次加载的文件

        Returns:
            是否成功重载
        """
        config_file = config_file or self._config_file
        if not config_file or not os.path.exists(config_file):
            return False

        try:
            # 清除相对模块函数缓存，让热重载生效
            HookWorker._clear_relative_func_cache()
            current_mtime = os.path.getmtime(config_file)
            last_mtime = self._config_watchers.get(config_file, 0)

            if current_mtime <= last_mtime:
                return False  # 文件未修改

            # 重新加载配置
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            # 🛡️ 修复热重载失效：先清除去重缓存，再注册。
            # 旧逻辑先更新 _config_watchers 再调 register_hooks_from_json，
            # register 开头的去重检查 `config_file in self._config_watchers`
            # 必然命中 → return 0 → 热重载永远不生效。
            if config_file in self._config_watchers:
                del self._config_watchers[config_file]

            # 先注销旧的 user-custom hooks，避免重新注册后新旧 rule 并存
            self.unregister_skill_hooks("user-custom")

            # 重新注册 (保留技能注册，skill_name 统一用 "user-custom")
            self.register_hooks_from_json("user-custom", "", config, config_file)

            # 更新监控时间（注册成功后）
            self._config_watchers[config_file] = current_mtime

            logger.info(f"[HookManager] Hot reloaded hooks from {config_file}")
            return True
        except Exception as e:
            logger.error(f"[HookManager] Failed to reload hooks: {e}")
            return False

    def check_and_reload(self) -> bool:
        """检查配置是否变更，必要时热重载（返回是否成功重载）"""
        return self.reload_hooks_config()

    # ========== 条件匹配 ==========

    def _check_conditions(self, hook: Hook, context: Dict[str, Any]) -> bool:
        """检查 Hook 的执行条件是否满足"""
        if not hook.conditions:
            return True

        for condition in hook.conditions:
            if not self._evaluate_condition(condition, context):
                return False
        return True

    def _evaluate_condition(self, condition: HookCondition, context: Dict[str, Any]) -> bool:
        """评估单个条件"""
        cond_type = condition.type
        pattern = condition.pattern

        if cond_type == "env":
            # 环境变量条件，如 "DEBUG=true"
            if "=" in pattern:
                key, value = pattern.split("=", 1)
                return os.environ.get(key) == value
            return bool(os.environ.get(pattern))

        elif cond_type == "file":
            # 文件模式匹配：用 re.search 而非 re.match，允许模式匹配路径中任意位置。
            # 行为差异：对典型通配符模式（如 `*.py`、`*main*`）两者等价，因为 `*` → `.*` 的转换
            # 已使模式无锚定。仅当 pattern 直接以字面前缀（无通配符）开头时，re.search 会匹配
            # 路径中部，而 re.match 会不匹配。例如 `"src/"` 现在可匹配 `"foo/src/bar.py"`。
            current_file = context.get("file", "")
            if not current_file:
                return False
            try:
                return bool(re.search(pattern.replace("*", ".*"), current_file))
            except re.error:
                return False

        elif cond_type == "tool":
            # 工具名匹配
            tool_name = context.get("tool_name", "")
            return tool_name == pattern

        elif cond_type == "regex":
            # 正则匹配
            message = context.get("message", "")
            try:
                return bool(re.search(pattern, message))
            except re.error:
                return False

        return True

    # ========== 事件触发 ==========

    def trigger_event(
        self, event_name: str, context: Dict[str, Any] = None, current_message: str = "", trigger_async: bool = True
    ) -> List[HookExecutionResult]:
        """
        触发事件，执行所有匹配的 Hooks（支持同事件内 Hook 级并行）

        Args:
            event_name: 事件名
            context: 上下文信息
            current_message: 当前消息 (用于 matcher 匹配)
            trigger_async: 是否异步执行

        Returns:
            执行结果列表（按原始注册顺序返回）
        """
        context = context or {}
        context["message"] = current_message
        context["event_name"] = event_name
        context["timestamp"] = time.time()

        if event_name not in self._hooks:
            return []

        # Phase 1: 收集所有匹配的 hook（串行，仅做规则匹配，不执行实际 hook）
        all_hooks: List[Hook] = []
        for rule in self._hooks[event_name]:
            if not rule.matches(context):
                continue
            for hook in rule.hooks:
                if not hook.enabled:
                    continue
                if not self._check_conditions(hook, context):
                    logger.debug(f"[HookManager] Hook conditions not met: {event_name}")
                    continue
                all_hooks.append(hook)

        if not all_hooks:
            return []

        # 分离 PROMPT 类型（必须同步执行，保持注入顺序）和其他类型（可并行）
        n = len(all_hooks)
        prompt_indices = [i for i in range(n) if all_hooks[i].type == HookType.PROMPT.value]
        parallel_indices = [i for i in range(n) if all_hooks[i].type != HookType.PROMPT.value]

        # 预分配结果列表，保持原始顺序
        results: List[Optional[HookExecutionResult]] = [None] * n

        # Phase 2: PROMPT hook 同步执行（按顺序，输出需立即注入）
        for idx in prompt_indices:
            hook = all_hooks[idx]
            results[idx] = self._execute_hook(hook, context, trigger_async=False)

        # Phase 3: 非 PROMPT hook 并行执行
        if parallel_indices:
            parallel_hooks = [all_hooks[i] for i in parallel_indices]
            # 🛡️ W1：trigger_async=True（UI 线程 SessionStart）时，command/非注入型
            # hook 改走后台异步执行 + 回调回补，主线程不被外部进程/网络阻塞。
            parallel_results = self._execute_hooks_parallel(parallel_hooks, context, trigger_async=trigger_async)
            for orig_idx, result in zip(parallel_indices, parallel_results):
                results[orig_idx] = result

        # 兜底：填充因异常未能赋值的结果 slot（正常情况下 assert 不触发）
        for i, r in enumerate(results):
            if r is None:
                results[i] = HookExecutionResult(success=False, output="Unknown hook execution error")
        return results  # type: ignore[return-value]

    def _collect_futures(
        self,
        future_to_idx: Dict[Future, int],
        results: List[Optional[HookExecutionResult]],
    ):
        """等待 futures 完成并收集结果。

        UI 线程：QEventLoop 保活驱动事件循环（界面不冻结）+ 5 分钟兜底超时。
        其他线程：直接阻塞等待。
        末尾统一兜底填充因异常未能赋值的结果 slot。
        """
        if _is_ui_thread():
            self._wait_futures_ui_safe(future_to_idx, results)
        else:
            done, _ = futures_wait(future_to_idx)
            for future in done:
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    logger.error(f"[HookManager] Parallel hook failed: {e}")
                    results[idx] = HookExecutionResult(success=False, output=str(e))
        # 兜底：填充因异常未能赋值的结果 slot（正常情况下不触发）
        for i, r in enumerate(results):
            if r is None:
                results[i] = HookExecutionResult(success=False, output="Unknown parallel hook error")

    def _execute_hooks_parallel(
        self, hooks: List[Hook], context: Dict[str, Any], trigger_async: bool = False
    ) -> List[HookExecutionResult]:
        """并行执行多个 Hook（非 PROMPT 类型），返回结果列表（顺序与输入一致）

        每个 hook 获得独立的 context 浅拷贝，避免并发读写竞争。
        内部通过 ThreadPoolExecutor 调度，所有 hook 完成后统一返回。

        Args:
            trigger_async: True 时（如 UI 线程触发 SessionStart 预对话事件），
                command 类型及非注入型 http/python hook 改走后台异步执行
                （HookWorker + finished 回调回补注入，主线程不等待）；
                其余注入型 hook 仍线程池执行并等待。

        UI 线程安全：若当前在 UI 线程，等待期间会持续处理 Qt 事件循环，
        避免界面冻结。

        并发安全性说明：
        - 每个 hook 使用 dict(context) 浅拷贝，hook 之间无共享可变状态
        - self._registered_functions 是只读的（运行期不修改），并发安全
        - self._cwd_resolve_cache 由 Python GIL 保护单操作原子性，最坏情况缓存未命中重算
        - 回调 self._on_finished_callback 等使用 queue.Queue（线程安全）+ Qt 信号跨线程发射
        """
        n = len(hooks)
        if n == 0:
            return []
        if n == 1:
            hook = hooks[0]
            if trigger_async and self._can_async_execute(hook):
                # 🛡️ W1：单条可后台 hook（如 SessionStart 的 command）：后台异步
                # 执行，立即返回占位结果，输出由 finished 回调回补，主线程不阻塞。
                return [self._execute_hook(hook, dict(context), trigger_async=True)]
            if trigger_async:
                # 请求异步但该 hook 不可后台（http/python 注入型）：线程池执行 +
                # UI 线程事件循环保活等待（最长 hook.timeout，界面不冻结）。
                executor = _get_parallel_executor()
                future = executor.submit(self._execute_hook, hook, dict(context), trigger_async=False)
                results: List[Optional[HookExecutionResult]] = [None]
                self._collect_futures({future: 0}, results)
                return results  # type: ignore[return-value]
            # 非异步请求：保持历史语义，在调用线程同步执行（减少线程调度开销）
            return [self._execute_hook(hook, dict(context), trigger_async=False)]

        logger.debug(f"[HookManager] Executing {n} hooks in parallel")
        results: List[Optional[HookExecutionResult]] = [None] * n

        executor = _get_parallel_executor()
        future_to_idx = {}
        for i, hook in enumerate(hooks):
            if trigger_async and self._can_async_execute(hook):
                # 🛡️ W1：可后台 hook（command / 非注入型）直接异步，立即返回占位
                # 结果，输出由 finished 回调回补；其余 hook 线程池执行并等待。
                results[i] = self._execute_hook(hook, dict(context), trigger_async=True)
            else:
                # 浅拷贝 context：保证每个 hook 有自己的 context 副本，
                # _execute_hook 中的 context["skill_root"] = ... 等写入不影响其他 hook
                future = executor.submit(self._execute_hook, hook, dict(context), trigger_async=False)
                future_to_idx[future] = i

        if future_to_idx:
            self._collect_futures(future_to_idx, results)

        # 兜底：填充因异常未能赋值的结果 slot
        for i, r in enumerate(results):
            if r is None:
                results[i] = HookExecutionResult(success=False, output="Unknown parallel hook error")
        return results  # type: ignore[return-value]

    def _wait_futures_ui_safe(
        self,
        future_to_idx: Dict[Future, int],
        results: List[Optional[HookExecutionResult]],
    ):
        """在 UI 线程安全地等待 Future 完成：循环检查 + QEventLoop 保活

        不阻塞 UI 事件处理，用户操作（缩放/滚动/输入）仍然响应。
        内置 5 分钟超时保护，防止挂起 hook 无限阻塞。
        """
        from PyQt5.QtCore import QEventLoop, QTimer

        pending = set(future_to_idx.keys())
        check_interval = 50  # 每 50ms 检查一次
        timeout_ms = 300_000  # 5 分钟总超时
        elapsed = 0

        while pending:
            # 处理 Qt 事件 50ms，保持 UI 响应
            loop = QEventLoop()
            QTimer.singleShot(check_interval, loop.quit)
            loop.exec_()
            elapsed += check_interval

            # 超时保护：取消未完成 futures 并标记失败
            if elapsed >= timeout_ms:
                for future in pending:
                    idx = future_to_idx[future]
                    future.cancel()
                    results[idx] = HookExecutionResult(
                        success=False,
                        output="Parallel hook execution timed out",
                    )
                logger.warning("[HookManager] Parallel hook timeout, cancelled remaining futures")
                break

            # 检查哪些 future 已完成
            done = {f for f in pending if f.done()}
            for future in done:
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    logger.error(f"[HookManager] Parallel hook failed: {e}")
                    results[idx] = HookExecutionResult(success=False, output=str(e))
            pending -= done

    @staticmethod
    def _can_async_execute(hook: "Hook") -> bool:
        """hook 是否可后台异步执行

        - PROMPT 必须同步（输出文本需立即注入 prompt）
        - COMMAND 始终可异步（输出通过 finished 回调回补注入）
        - HTTP/PYTHON 仅非消息注入时异步（add_output_to_context=False），
          避免 PreToolUse 等场景异步丢 BLOCK 决策
        """
        return hook.type != HookType.PROMPT.value and (
            hook.type == HookType.COMMAND.value or not hook.add_output_to_context
        )

    def _execute_hook(self, hook: Hook, context: Dict[str, Any], trigger_async: bool = True) -> HookExecutionResult:
        """执行单个 Hook"""
        # cwd: 智能解析（显式设置 > 从命令脚本路径推导 > 默认项目根目录）
        cwd = self._resolve_command_cwd(hook, context)

        # 注入 skill_root 到 context，供异步 worker 也能解析 ${CLAUDE_PLUGIN_ROOT}
        context["skill_root"] = hook.skill_root

        # 变量替换
        # Windows 上优先使用 commandWindows（Claude Code 插件兼容）
        if os.name == "nt" and hook.commandWindows:
            effective_command = hook.commandWindows
        else:
            effective_command = hook.command
        command = self._interpolate_variables(effective_command, context)
        url = self._interpolate_variables(hook.url or "", context)

        # 异步执行条件：
        # - PROMPT 类型必须同步（输出文本需立即注入 prompt）
        # - COMMAND 类型始终支持异步（输出通过回调队列注入）
        # - HTTP/PYTHON 仅在不需要注入消息列表时异步（add_output_to_context=False）
        #   ⚠️ PreToolUse 场景：异步 HTTP/PYTHON hook 若返回 BLOCK 决策，
        #     将不会被同步处理（罕见情况，需确保此类 hook 保留 add_output_to_context=True）
        can_async = trigger_async and self._can_async_execute(hook)
        # 提取 status_message 供后续传播
        status_message = hook.statusMessage or ""

        if can_async:
            signals = HookWorkerSignals()
            worker = HookWorker(hook, cwd, signals, context.get("event_name", ""), context)

            # 连接完成回调（携带 status_message）
            if hook.add_output_to_context and self._on_finished_callback:
                # 🛡️ W1：异步 worker 完成回调加 __async__ 前缀，backend 据此区分
                # 同步路径（调用方直接注入 session.messages）与异步路径（需回补注入），
                # 避免预对话事件（SessionStart 等）输出丢失或 double inject。
                # 🛡️ F1(W1-R2)：事件名追加触发时的 session_id（若有），
                # backend 回补注入前校验当前会话一致，防止用户切换会话后
                # 异步输出注入到错误 session。
                _async_sid = context.get("session_id", "") or ""
                if _async_sid:
                    async_event = f"__async__:{context.get('event_name', '')}:{_async_sid}"
                else:
                    async_event = f"__async__:{context.get('event_name', '')}"
                signals.finished.connect(
                    lambda _ev, _out, _ok, _st, _ae=async_event: self._on_finished_callback(_ae, _out, _ok, _st)
                )

            # 连接状态回调（异步结束时触发 is_start=False）
            if status_message and self._on_status_callback:
                signals.status_changed.connect(
                    lambda evt, msg, start: (
                        self._on_status_callback(evt, msg, start) if self._on_status_callback else None
                    )
                )

            self._thread_pool.start(worker)

            # 发出执行开始状态
            if status_message and self._on_status_callback:
                self._on_status_callback(context.get("event_name", ""), status_message, True)

            logger.debug(
                f"[HookManager] Hook triggered (async): {context.get('event_name')} "
                f"(type={hook.type}, add_output_to_context={hook.add_output_to_context})"
            )

            return HookExecutionResult(success=True, output="", status_message=status_message)
        else:
            # 同步执行

            # 发出执行开始状态
            if status_message and self._on_status_callback:
                self._on_status_callback(context.get("event_name", ""), status_message, True)

            try:
                output = ""
                success = False
                _exit2_skip = False  # exit code 2 跳过标记（Claude Code 兼容）

                if hook.type == HookType.COMMAND.value:
                    extra_env = HookManager._build_claude_env(context)
                    output, success, exit_code = HookWorker._run_command_sync(
                        command,
                        cwd,
                        hook.timeout,
                        stdin_data=json.dumps(context),
                        extra_env=extra_env,
                    )
                    _exit2_skip = exit_code == 2

                elif hook.type == HookType.HTTP.value:
                    import urllib.error
                    import urllib.request

                    data = json.dumps({"event": context.get("event_name"), "context": context}).encode("utf-8")
                    headers = hook.headers or {}
                    headers["Content-Type"] = "application/json"

                    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

                    with urllib.request.urlopen(req, timeout=hook.timeout) as response:
                        output = response.read().decode("utf-8")
                        success = True

                elif hook.type == HookType.PROMPT.value:
                    output = hook.prompt or hook.command or ""
                    success = True

                elif hook.type == HookType.PYTHON.value:
                    if not hook.function:
                        output = "No function specified"
                        success = False
                    else:
                        # 解析函数路径中的内联参数（?key=val 语法）
                        clean_function, inline_params = _parse_function_params(hook.function)

                        # 注册函数表也用清理后的路径查找
                        if clean_function in self._registered_functions:
                            func = self._registered_functions[clean_function]
                            args = dict(inline_params)
                            args.update(hook.function_args or {})
                            args.update({"event": context.get("event_name"), "context": context})
                            result = func(**args)
                            output = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                            success = True
                        else:
                            # 尝试解析 Python 函数
                            try:
                                parts = clean_function.rsplit(":", 1)
                                if len(parts) != 2:
                                    output = f"Function not registered (invalid path): {hook.function}"
                                    success = False
                                else:
                                    module_path, func_name = parts
                                    func = None

                                    # 相对路径：基于 hooks.json 目录解析
                                    if module_path.startswith(".") and hook.config_file:
                                        func = HookWorker._import_relative_function(clean_function, hook.config_file)

                                    # 标准路径：importlib.import_module
                                    if func is None:
                                        import importlib

                                        module = importlib.import_module(module_path)
                                        func = getattr(module, func_name, None)

                                    if not callable(func):
                                        output = f"Function not found: {hook.function}"
                                        success = False
                                    else:
                                        args = dict(inline_params)
                                        args.update(hook.function_args or {})
                                        args["event"] = context.get("event_name")
                                        args["context"] = context
                                        result = func(**args)
                                        output = (
                                            result
                                            if isinstance(result, str)
                                            else json.dumps(result, ensure_ascii=False)
                                        )
                                        success = True
                            except Exception as e:
                                output = f"Python hook failed: {str(e)}"
                                success = False

                # 检查决策（支持 JSON decision 和 exit code 2 两种方式）
                decision = HookDecision.CONTINUE

                # 方式1: 检测 exit code 2（Claude Code 兼容：跳过工具执行）
                if _exit2_skip:
                    decision = HookDecision.BLOCK
                    success = True  # exit 2 是有效钩子响应，非执行失败
                    logger.info(f"[HookManager] Hook exit code 2 → BLOCK: {context.get('event_name')}")

                # 方式2: 解析 JSON 中的 decision 字段
                try:
                    output_data = json.loads(output)
                    if isinstance(output_data, dict):
                        decision_str = output_data.get("decision", "continue")
                        if decision_str in ["block", "continue", "defer"]:
                            decision = HookDecision(decision_str)
                        # 提取 output 字段覆盖外层 output（解决 json.dumps(ensure_ascii=True) 转义乱码）
                        if "output" in output_data:
                            output = str(output_data["output"])
                except json.JSONDecodeError:
                    pass

                # 触发决策回调
                if decision != HookDecision.CONTINUE and self._on_decision_callback:
                    self._on_decision_callback(context.get("event_name", ""), decision)

                # 发出执行结束状态
                if status_message and self._on_status_callback:
                    self._on_status_callback(context.get("event_name", ""), status_message, False)

                # 触发完成回调
                if hook.add_output_to_context and self._on_finished_callback:
                    callback_event = context.get("event_name", "")
                    # PROMPT 类型 hook 用前缀标记，backend 据此总是加入消息列表
                    if hook.type == HookType.PROMPT.value:
                        callback_event = f"__prompt__:{callback_event}"
                    self._on_finished_callback(callback_event, output, success, status_message)

                logger.info(f"[HookManager] Hook executed: {context.get('event_name')}")

                return HookExecutionResult(
                    success=success,
                    output=output,
                    decision=decision.value,
                    status_message=status_message,
                    add_to_context=hook.add_output_to_context,
                )

            except Exception as e:
                logger.error(f"[HookManager] Hook failed: {context.get('event_name')} - {e}")

                # 发出执行结束状态（失败）
                if status_message and self._on_status_callback:
                    self._on_status_callback(context.get("event_name", ""), status_message, False)

                if hook.add_output_to_context and self._on_finished_callback:
                    callback_event = context.get("event_name", "")
                    if hook.type == HookType.PROMPT.value:
                        callback_event = f"__prompt__:{callback_event}"
                    self._on_finished_callback(callback_event, f"Error: {str(e)}", False, status_message)
                return HookExecutionResult(success=False, output=str(e), status_message=status_message)

    def _resolve_command_cwd(self, hook: Hook, context: Dict[str, Any]) -> Optional[str]:
        """
        解析命令的工作目录。
        优先级：
        1. 显式设置的 cwd（配置文件中指定）
        2. 从命令中解析脚本文件路径，使用该文件所在目录
        3. None（使用 subprocess 默认 CWD=项目根目录）

        结果会缓存 30 秒（因为 hook.command 和 hook.skill_root 是静态的），
        避免每次事件触发都重复扫描磁盘。
        """
        # 1. 显式设置优先（不缓存，因为值已经是最终结果）
        if hook.cwd:
            logger.debug(f"[HookManager] Using explicit cwd: {hook.cwd}")
            return hook.cwd

        # 2. 检查缓存（key 基于 hook.command + hook.skill_root，两者都是静态的）
        import time

        cache_key = id(hook)
        cached = self._cwd_resolve_cache.get(cache_key)
        if cached and time.monotonic() - cached[1] < self._CWD_CACHE_TTL:
            return cached[0]

        command = hook.command
        if not command:
            self._cwd_resolve_cache[cache_key] = (None, time.monotonic())
            logger.debug("[HookManager] No command, returning None for cwd")
            return None

        # 匹配常见的脚本调用模式（扩展名常量定义在模块顶部）：
        # ./script, script.ext, bash script, cmd script, python script 等
        patterns = [
            rf"^\s*\.?/?([^\s]+\.({_SCRIPT_EXTENSIONS}))(?:\s|$)",  # 相对路径脚本
            rf"\s+([^\s/]+\.({_SCRIPT_EXTENSIONS}))(?:\s|$)",  # 空格后的脚本
            rf"\s+([^\s/]+/[^\s]+\.({_SCRIPT_EXTENSIONS}))(?:\s|$)",  # 带目录的脚本
        ]

        for pattern in patterns:
            match = re.search(pattern, command, re.IGNORECASE)
            if match:
                script_path = match.group(1)
                logger.debug(f"[HookManager] Script detected: {script_path}")

                # 搜索目录列表（支持 hooks 子目录）
                search_dirs = []
                if hook.skill_root:
                    search_dirs.append(hook.skill_root)
                    # 如果 skill_root 下有 hooks 子目录，也加入搜索
                    hooks_dir = os.path.join(hook.skill_root, "hooks")
                    if os.path.isdir(hooks_dir):
                        search_dirs.append(hooks_dir)
                search_dirs.append(os.getcwd())

                for base_dir in search_dirs:
                    full_path = os.path.join(base_dir, script_path)
                    full_path = os.path.normpath(full_path)
                    logger.debug(f"[HookManager] Checking: {full_path}")
                    if os.path.isfile(full_path):
                        resolved_cwd = os.path.dirname(full_path)
                        logger.debug(f"[HookManager] Found script, resolved cwd: {resolved_cwd}")
                        self._cwd_resolve_cache[cache_key] = (resolved_cwd, time.monotonic())
                        return resolved_cwd

                logger.debug("[HookManager] Script file not found in any search dir")
                self._cwd_resolve_cache[cache_key] = (None, time.monotonic())
                return None

        logger.debug("[HookManager] No script in command, returning None for cwd")
        self._cwd_resolve_cache[cache_key] = (None, time.monotonic())
        return None

    @staticmethod
    def _interpolate_variables(text: str, context: Dict[str, Any]) -> str:
        """变量替换"""
        if not text:
            return text

        # ── Claude Code 插件路径变量（用 skill_root 推导，优先于环境变量） ──
        skill_root = context.get("skill_root", "")
        plugin_root = ""
        if skill_root:
            plugin_root = str(Path(skill_root).parent) if Path(skill_root).name == "hooks" else skill_root
            # ${CLAUDE_PLUGIN_ROOT} 是环境变量风格，单独替换
            text = text.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root)

        variables = {
            "{skill_root}": skill_root,
            "{plugin_root}": plugin_root,
            "{project_root}": context.get("project_root", ""),
            "{message}": context.get("message", ""),
            "{file}": context.get("file", ""),
            "{tool_name}": context.get("tool_name", ""),
            "{event_name}": context.get("event_name", ""),
        }

        for var, value in variables.items():
            if value:
                text = text.replace(var, str(value))

        # 环境变量替换（CLAUDE_PLUGIN_ROOT 已在上面处理，不会走这里）
        text = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), text)
        text = re.sub(r"\$(\w+)", lambda m: os.environ.get(m.group(1), ""), text)

        return text

    @staticmethod
    def _build_claude_env(context: Dict[str, Any]) -> Dict[str, str]:
        """从 hook context 构建 Claude Code 兼容的环境变量

        第三方插件（如 agent-memory）依赖这些环境变量来识别项目、会话和代理身份。
        """
        env: Dict[str, str] = {}

        # CLAUDE_PLUGIN_ROOT: 插件根目录（用于查找 src/ 等依赖）
        skill_root = context.get("skill_root", "")
        if skill_root:
            plugin_root = str(Path(skill_root).parent) if Path(skill_root).name == "hooks" else skill_root
            env["CLAUDE_PLUGIN_ROOT"] = plugin_root

        # CLAUDE_PROJECT: 项目标识，记忆按项目分组
        project_root = context.get("project_root", "")
        if project_root:
            env["CLAUDE_PROJECT"] = os.path.basename(project_root.rstrip("/\\"))

        # CLAUDE_SESSION_ID: 会话标识，记忆按会话粒度检索
        session_id = context.get("session_id", "")
        if session_id:
            env["CLAUDE_SESSION_ID"] = session_id

        # CLAUDE_AGENT_ID: 代理身份标识
        agent_id = context.get("current_role", "primary")
        env["CLAUDE_AGENT_ID"] = agent_id

        return env

    def get_registered_events(self) -> List[str]:
        """获取所有已注册事件"""
        return list(self._hooks.keys())

    def get_hook_info(self, event_name: str) -> List[dict]:
        """获取指定事件的 Hook 信息"""
        if event_name not in self._hooks:
            return []

        info = []
        for rule in self._hooks[event_name]:
            for hook in rule.hooks:
                info.append(hook.to_dict())
        return info

    def export_config(self) -> dict:
        """导出当前配置（用于保存）"""
        hooks = {}
        for event_name, rules in self._hooks.items():
            rules_data = []
            for rule in rules:
                hooks_data = [h.to_dict() for h in rule.hooks]
                if hooks_data:
                    rules_data.append({"matcher": rule.matcher, "hooks": hooks_data})
            if rules_data:
                hooks[event_name] = rules_data
        return {"hooks": hooks}

    # ==================== UI 集成方法 ====================

    def get_all_hooks(self) -> Dict[str, List[dict]]:
        """获取所有已注册的 hooks，用于 UI 显示（覆写层的 enabled 优先）"""
        result = {}
        for event_name, rules in self._hooks.items():
            result[event_name] = []
            for rule in rules:
                for hook in rule.hooks:
                    hook_dict = self._get_effective_hook_dict(hook)
                    hook_dict["matcher"] = rule.matcher
                    result[event_name].append(hook_dict)
        return result

    def get_all_hooks_grouped(self) -> Dict[str, Dict[str, List[dict]]]:
        """
        获取所有已注册的 hooks，按 plugin/skill/user 分组

        每个 hook dict 包含 _source_type 和 _display_name 字段用于 UI 来源标签。

        Returns:
            {"plugin": {...}, "skill": {...}, "user": {...}}
        """
        # 先建立 hook_id -> (source_type, display_name) 的映射
        hook_source_map = self._build_hook_source_map()

        grouped = {"plugin": {}, "skill": {}, "user": {}}
        for event_name, rules in self._hooks.items():
            for rule in rules:
                for hook in rule.hooks:
                    hook_dict = self._get_effective_hook_dict(hook)
                    hook_dict["matcher"] = rule.matcher

                    # 来源信息
                    source_type, display_name = hook_source_map.get(hook.id, ("user", "自定义"))
                    hook_dict["_source_type"] = source_type
                    hook_dict["_display_name"] = display_name
                    # 系统级 hook 标记（来自 plugins/system/），UI 据此禁用删除按钮
                    hook_dict["_is_system_plugin"] = hook.is_system_plugin

                    if event_name not in grouped[source_type]:
                        grouped[source_type][event_name] = []
                    grouped[source_type][event_name].append(hook_dict)
        return grouped

    def _build_hook_source_map(self) -> Dict[str, tuple]:
        """构建 hook_id -> (source_type, display_name) 的映射

        🛡️ 基于 rule.skill_name（真相来源）而非 _skill_to_hooks 索引：
        索引可能在热重载后错位，导致来源标签错乱（P021）。
        """
        result = {}
        for event_name, rules in self._hooks.items():
            for rule in rules:
                owner = getattr(rule, "skill_name", "")
                if owner == "user-custom":
                    source_type, display_name = "user", "自定义"
                else:
                    source_type, display_name = "plugin", owner
                for hook in rule.hooks:
                    result[hook.id] = (source_type, display_name)
        return result

    def _find_hook_by_id(self, hook_id: str) -> Optional[tuple]:
        """通过 id 查找 hook，返回 (event_name, rule_index, hook_index, hook)"""
        for event_name, rules in self._hooks.items():
            for rule_idx, rule in enumerate(rules):
                for hook_idx, hook_obj in enumerate(rule.hooks):
                    if hook_obj.id == hook_id:
                        return (event_name, rule_idx, hook_idx, hook_obj)
        return None

    def _find_hook_fields(self, hook: Hook, config: dict) -> Optional[tuple]:
        """在配置中查找 hook 所在位置，返回 (event_name, rule_idx, hook_idx) 或 None

        匹配策略（防误更新）：
        1. 优先按 id 精确匹配（同 command 多 hook 场景不会误更新第一条）
        2. id 匹配不到时，按 command/url/function/prompt 唯一键兜底
        """
        raw_hooks = config.get("hooks", config)
        target_cmd = hook.command or hook.url or hook.function or hook.prompt or ""

        for event_name, rules in raw_hooks.items():
            for rule_idx, rule in enumerate(rules):
                hooks_list = rule.get("hooks", [])
                for hook_idx, h in enumerate(hooks_list):
                    hook_id = h.get("id", "") or ""
                    if hook_id and hook_id == hook.id:
                        return (event_name, rule_idx, hook_idx)
        # id 兜底失败 → 唯一键兜底（command/url/function/prompt）
        if target_cmd:
            for event_name, rules in raw_hooks.items():
                for rule_idx, rule in enumerate(rules):
                    hooks_list = rule.get("hooks", [])
                    for hook_idx, h in enumerate(hooks_list):
                        h_cmd = h.get("command", "") or h.get("url", "") or h.get("function", "") or h.get("prompt", "")
                        if h_cmd == target_cmd:
                            return (event_name, rule_idx, hook_idx)
        return None

    def _save_hook_to_file_by_id(self, hook: Hook, new_data: dict = None) -> bool:
        """通过 hook_id 覆盖式保存到源文件（禁止追加，防重复）

        Returns:
            True 成功 / False 匹配不到或写失败
        """
        if not hook.config_file or not os.path.exists(hook.config_file):
            return False

        try:
            with open(hook.config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            # 查找 hook 位置（优先 id 精确匹配，防误更新）
            location = self._find_hook_fields(hook, config)
            if location is None:
                logger.warning(f"[HookManager] Hook {hook.id} not found in {hook.config_file}")
                return False

            event_name, rule_idx, hook_idx = location
            raw_hooks = config.get("hooks", config)
            target_rule = raw_hooks[event_name][rule_idx]
            target_hooks = target_rule.get("hooks", [])

            # 合并更新数据
            hook_entry = target_hooks[hook_idx]
            if new_data:
                # 处理 enabled/command/url/function
                for key in ("enabled", "command", "url", "function"):
                    if key in new_data:
                        hook_entry[key] = new_data[key]

                # 处理 matcher 变更（移动到新事件）：合并进目标事件同 matcher rule，禁止新建重复 rule
                # 兼容两种调用方键名：UI 传 {"event": ..., "matcher": ...}，
                # 内部迁移路径传 {"new_event_name": ..., "matcher": ...}
                new_event = new_data.get("new_event_name") or new_data.get("event")
                if "matcher" in new_data and new_event:
                    new_event = str(new_event)
                    new_matcher = new_data["matcher"] or ""

                    if new_event not in raw_hooks:
                        raw_hooks[new_event] = []

                    # 查找目标事件中同 matcher 的 rule，存在则合并，否则新建
                    matched_rule = None
                    for r in raw_hooks[new_event]:
                        if (r.get("matcher") or "") == new_matcher:
                            matched_rule = r
                            break
                    if matched_rule is not None:
                        matched_rule.setdefault("hooks", []).append(hook_entry)
                    else:
                        raw_hooks[new_event].append({"matcher": new_matcher or "", "hooks": [hook_entry]})

                    # 从旧位置移除
                    target_hooks.pop(hook_idx)
                    if not target_rule.get("hooks"):
                        raw_hooks[event_name].pop(rule_idx)
                    if not raw_hooks.get(event_name):
                        del raw_hooks[event_name]

                # 处理其他字段
                for key in [
                    "type",
                    "cwd",
                    "add_output_to_context",
                    "skill_root",
                    "timeout",
                    "retry",
                    "conditions",
                    "headers",
                    "allowedEnvVars",
                    "function_args",
                    "commandWindows",
                    "statusMessage",
                    "prompt",
                ]:
                    if key in new_data:
                        hook_entry[key] = new_data[key]

            # 确保 id 字段存在
            hook_entry["id"] = hook.id

            # 写前防重：清理同 id 重复条目（防御历史遗留）
            self._dedupe_hook_entries(raw_hooks)

            with open(hook.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            logger.debug(f"[HookManager] Saved hook {hook.id} to {hook.config_file}")
            return True
        except Exception as e:
            logger.error(f"[HookManager] Failed to save hook {hook.id}: {e}")
            return False

    @staticmethod
    def _dedupe_hook_entries(raw_hooks: dict):
        """写前防重：同一 rule 内同 id 的 hook 条目只保留第一个"""
        for rules in raw_hooks.values():
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                hooks = rule.get("hooks")
                if not isinstance(hooks, list):
                    continue
                seen: set = set()
                deduped = []
                for h in hooks:
                    hid = h.get("id", "")
                    if hid and hid in seen:
                        continue
                    if hid:
                        seen.add(hid)
                    deduped.append(h)
                rule["hooks"] = deduped

    def edit_hook_by_id(self, hook_id: str, new_data: dict) -> bool:
        """
        通过 id 编辑 hook — 直接修改共享 Hook 对象并持久化到源文件

        字段变更（command/matcher/type 等）直接写入共享 Hook 对象的属性，
        如果是 user-custom hook，同时持久化到源配置文件。

        Args:
            hook_id: hook 唯一标识
            new_data: 要更新的字段（如 {"command": "new_cmd", "matcher": "tool:write"}）

        Returns:
            是否成功
        """
        result = self._find_hook_by_id(hook_id)
        if result is None:
            logger.warning(f"[HookManager] Hook {hook_id} not found")
            return False

        event_name, rule_idx, hook_idx, hook = result

        # ── 事件变更：修改共享 _hooks（位置是全局的） ──
        new_event = new_data.get("event", event_name)
        event_changed = new_event != event_name

        if event_changed:
            # 从旧事件移除 rule（如果只剩这个 hook）
            rule = self._hooks[event_name][rule_idx]
            rule.hooks.remove(hook)
            if not rule.hooks:
                self._hooks[event_name].pop(rule_idx)
            if not self._hooks[event_name]:
                del self._hooks[event_name]

            # 添加到新事件
            if new_event not in self._hooks:
                self._hooks[new_event] = []
            new_matcher = new_data.get("matcher", rule.matcher or "")
            matched_rule = None
            for r in self._hooks[new_event]:
                if (r.matcher or "") == new_matcher:
                    matched_rule = r
                    break
            if matched_rule:
                matched_rule.hooks.append(hook)
                # 目标 rule 无归属时继承原 rule 的（避免来源标签为空）
                if not getattr(matched_rule, "skill_name", ""):
                    matched_rule.skill_name = getattr(rule, "skill_name", "")
            else:
                new_rule = HookMatchRule(matcher=new_matcher or None, hooks=[hook])
                # 继承原 rule 的归属 skill（来源标签真相）
                new_rule.skill_name = getattr(rule, "skill_name", "")
                self._hooks[new_event].append(new_rule)

            # 全量重建索引（rule 移动/合并后位置统一修正，P021 修复）
            self._rebuild_skill_to_hooks()
            # 非系统 hook 的事件变更也持久化到源文件（全局生效）；系统 hook 走覆盖层
            if not hook.is_system_plugin:
                ok = self._save_hook_to_file_by_id(hook, new_data)
                if not ok:
                    logger.error(f"[HookManager] Failed to persist event move for {hook_id}")
        elif "matcher" in new_data:
            # 同事件内更新共享内存的 matcher，用于 trigger_event 初始匹配
            self._hooks[event_name][rule_idx].matcher = new_data["matcher"] or None

        # ── 内容字段直接写入 Hook 对象 ──
        # 对于 user-custom hook，同时持久化到源配置文件
        field_mapping = {
            "command": "command",
            "type": "type",
            "url": "url",
            "function": "function",
            "prompt": "prompt",
            "cwd": "cwd",
            "add_output_to_context": "add_output_to_context",
            "timeout": "timeout",
            "retry": "retry",
            "commandWindows": "commandWindows",
            "statusMessage": "statusMessage",
            "function_args": "function_args",
        }
        for key, attr in field_mapping.items():
            if key in new_data:
                setattr(hook, attr, new_data[key])

        # 双轨制持久化：
        # - 非系统 hook（插件/user-custom）：写回源文件（覆盖式），并清理覆盖层残留
        # - 系统 hook：持久化到 _hook_overrides（与 hook_states 共享同一文件），
        #   只存储内容字段，不存储 event/enabled（它们有独立的持久化路径）
        if not hook.is_system_plugin:
            ok = self._save_hook_to_file_by_id(hook, new_data)
            if not ok:
                logger.error(f"[HookManager] Failed to persist edit for {hook_id}")
            # 清理覆盖层残留（迁移兜底：若该 id 仍在 _hook_overrides 中）
            if hook.id in self._hook_overrides:
                del self._hook_overrides[hook.id]
                self._save_hook_states()
        else:
            override_fields = {
                "type",
                "command",
                "url",
                "function",
                "prompt",
                "cwd",
                "add_output_to_context",
                "timeout",
                "retry",
                "commandWindows",
                "statusMessage",
                "function_args",
                "matcher",
            }
            overrides = {k: v for k, v in new_data.items() if k in override_fields}
            if overrides:
                self._hook_overrides[hook.id] = overrides
                self._save_hook_states()

        logger.info(f"[HookManager] Edited hook {hook_id}")
        return True

    def toggle_hook_by_id(self, hook_id: str, enabled: bool) -> bool:
        """
        通过 id 切换 hook 启用状态

        直接修改共享 Hook 对象的 enabled 字段，
        如果是 user-custom hook，同时持久化到源配置文件。

        Args:
            hook_id: hook 唯一标识
            enabled: 是否启用

        Returns:
            是否成功
        """
        result = self._find_hook_by_id(hook_id)
        if result is None:
            logger.warning(f"[HookManager] Hook {hook_id} not found")
            return False

        event_name, rule_idx, hook_idx, hook = result

        hook.enabled = enabled

        # 双轨制持久化：
        # - 系统 hook（plugins/system/）：保留覆盖层（hook_states.json）
        # - 非系统 hook（插件/user-custom）：写回源文件 enabled 字段（覆盖式）
        #   并清理覆盖层残留（迁移兜底）
        if hook.is_system_plugin:
            self._hook_states[hook.id] = enabled
            self._save_hook_states()
        else:
            if hook.config_file:
                ok = self._save_hook_to_file_by_id(hook, {"enabled": enabled})
                if not ok:
                    logger.error(f"[HookManager] Failed to persist toggle for {hook_id} to {hook.config_file}")
            # 清理覆盖层残留（旧数据迁移兜底：若该 id 仍在 hook_states 中）
            if hook.id in self._hook_states:
                del self._hook_states[hook.id]
                self._save_hook_states()

        logger.info(f"[HookManager] Toggled hook {hook_id} enabled={enabled}")
        return True

    def delete_hook_by_id(self, hook_id: str) -> bool:
        """
        通过 id 删除 hook

        系统内置插件（plugins/system/）的 hook 不可删除。

        Args:
            hook_id: hook 唯一标识

        Returns:
            是否成功
        """
        result = self._find_hook_by_id(hook_id)
        if result is None:
            logger.warning(f"[HookManager] Hook {hook_id} not found")
            return False

        event_name, rule_idx, hook_idx, hook = result

        # 系统级 hook 拒绝删除
        if hook.is_system_plugin:
            logger.warning(f"[HookManager] Refused to delete system plugin hook {hook_id}")
            return False

        config_file = hook.config_file

        # 从内存中删除
        self._hooks[event_name][rule_idx].hooks.pop(hook_idx)
        # 如果规则空了，移除规则
        if not self._hooks[event_name][rule_idx].hooks:
            self._hooks[event_name].pop(rule_idx)
        # 如果事件空了，移除事件
        if not self._hooks.get(event_name):
            del self._hooks[event_name]

        # 从源文件删除（仅 user-custom hook 操作源文件）
        if self._is_user_custom_hook(hook) and config_file and os.path.exists(config_file):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)

                location = self._find_hook_fields(hook, config)
                if location:
                    ev, ri, hi = location
                    raw_hooks = config.get("hooks", config)
                    raw_hooks[ev][ri].get("hooks", []).pop(hi)
                    if not raw_hooks[ev][ri].get("hooks"):
                        raw_hooks[ev].pop(ri)
                    if not raw_hooks.get(ev):
                        del raw_hooks[ev]

                    with open(config_file, "w", encoding="utf-8") as f:
                        json.dump(config, f, indent=2, ensure_ascii=False)

                logger.debug(f"[HookManager] Deleted hook {hook_id} from {config_file}")
            except Exception as e:
                logger.error(f"[HookManager] Failed to delete hook {hook_id}: {e}")

        logger.info(f"[HookManager] Deleted hook {hook_id}")
        return True

    def set_hook_enabled(self, event_name: str, hook_index: int, enabled: bool):
        """设置 hook 启用状态（内部委托给 toggle_hook_by_id）"""
        if event_name not in self._hooks:
            return

        rules = self._hooks[event_name]
        hook_count = 0
        for rule in rules:
            for h in rule.hooks:
                if hook_count == hook_index:
                    self.toggle_hook_by_id(h.id, enabled)
                    return
                hook_count += 1

    def _save_hook_to_file(self, hook: Hook, event_name: str):
        """保存单个 hook 的状态到配置文件"""
        try:
            with open(hook.config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            # 递归查找并更新 hook
            self._update_hook_in_config(config, event_name, hook)

            with open(hook.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            logger.debug(f"[HookManager] Saved hook enabled={hook.enabled} to {hook.config_file}")
        except Exception as e:
            logger.error(f"[HookManager] Failed to save hook to {hook.config_file}: {e}")

    def _update_hook_in_config(self, config: dict, event_name: str, target_hook: Hook):
        """递归更新配置中的 hook enabled 状态"""
        raw_hooks = config.get("hooks", config)
        if event_name not in raw_hooks:
            return

        rules = raw_hooks[event_name]
        for rule in rules:
            hooks = rule.get("hooks", [])
            for h in hooks:
                # 通过 command 匹配（假设 command 是唯一的）
                if h.get("command") == target_hook.command:
                    h["enabled"] = target_hook.enabled
                    return

    def reload_global_hooks(self, config_file: str = None):
        """仅重新加载全局 hooks 配置，不影响 skill/agent hooks"""
        if config_file is None:
            config_file = self._config_file
        if not config_file or not os.path.exists(config_file):
            return

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)

            # 先注销旧的全局 hooks（统一用 "user-custom" skill_name）
            self.unregister_skill_hooks("user-custom")

            # 清除 _config_watchers 中的条目，避免去重检查拦截重新注册
            if config_file in self._config_watchers:
                del self._config_watchers[config_file]

            # 重新注册
            skill_root = str(Path(config_file).parent)
            self.register_hooks_from_json("user-custom", skill_root, config, config_file)
            logger.info(f"[HookManager] Reloaded global hooks from {config_file}")
        except Exception as e:
            logger.error(f"Failed to reload global hooks: {e}")

    def reload_all_plugin_hooks(self):
        """重新加载所有已启用插件的 hooks（不碰 user-custom 全局 hooks）

        用于卡片 _save_hooks() 后同步插件 hooks 的最新文件内容。
        """
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                return
            for plugin in pm.get_enabled_plugins():
                if plugin.name == "user-custom":
                    # user-custom 由 reload_global_hooks 单独管理，跳过
                    continue
                hooks_dir = plugin.path / "hooks"
                hooks_file = hooks_dir / "hooks.json"
                if not hooks_dir.exists() or not hooks_dir.is_dir():
                    continue
                # 先注销旧的，清除去重缓存
                self.unregister_skill_hooks(plugin.name)
                if hooks_file.exists():
                    self._clear_config_watcher(str(hooks_file))
                # 重新注册
                count = self.load_hooks_from_directory_flat(hooks_dir, skill_name=plugin.name)
                if count > 0:
                    logger.debug(f"[HookManager] Reloaded {count} hooks for plugin {plugin.name}")
        except Exception as e:
            logger.error(f"[HookManager] Failed to reload all plugin hooks: {e}")

    def load_hooks_from_directory(self, agents_dir: Path, is_system_plugin: bool = False) -> int:
        """从 agents_dir 子目录加载 hooks.json (agents/{name}/hooks/hooks.json)

        Args:
            agents_dir: agents 目录路径
            is_system_plugin: 是否来自系统内置插件（plugins/system/），标记的 hook 在 UI 上禁止删除
        """
        count = 0
        if not agents_dir.exists():
            return count

        for agent_dir in agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            hooks_file = agent_dir / "hooks" / "hooks.json"
            if hooks_file.exists():
                try:
                    with open(hooks_file, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    n = self.register_hooks_from_json(
                        agent_dir.name,
                        str(agent_dir.absolute()),
                        config,
                        str(hooks_file),
                        is_system_plugin=is_system_plugin,
                    )
                    count += n
                    if n > 0:
                        logger.info(f"[HookManager] Loaded {n} hooks from {agent_dir.name}")
                except Exception as e:
                    logger.error(f"[HookManager] Failed to load hooks from {hooks_file}: {e}")
        return count

    def load_hooks_from_directory_flat(
        self, dir_path: Path, skill_name: str = None, is_system_plugin: bool = False
    ) -> int:
        """从目录直接加载 hooks.json（插件顶层 hooks/ 目录）

        加载 {dir_path}/hooks.json 文件（如果有）。

        Args:
            dir_path: hooks 目录路径
            skill_name: 注册用的 skill 名称。为 None 时使用 dir_path.name（兼容旧调用）
            is_system_plugin: 是否来自系统内置插件（plugins/system/），标记的 hook 在 UI 上禁止删除
        """
        count = 0
        if not dir_path.exists() or not dir_path.is_dir():
            return count

        hooks_file = dir_path / "hooks.json"
        if not hooks_file.exists():
            return count

        try:
            with open(hooks_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            n = self.register_hooks_from_json(
                skill_name or dir_path.name,
                str(dir_path.absolute()),
                config,
                str(hooks_file),
                is_system_plugin=is_system_plugin,
            )
            count += n
            if n > 0:
                logger.info(f"[HookManager] Loaded {n} hooks from {dir_path.name}/hooks.json")
        except Exception as e:
            logger.error(f"[HookManager] Failed to load hooks from {hooks_file}: {e}")

        return count

    # [已移除] 旧技能 hooks 加载路径（load_hooks_from_skills / _load_skill_hooks_from_markdown / _parse_inline_hooks）
    # 所有 hooks 现在只从插件 hooks/ 目录加载
