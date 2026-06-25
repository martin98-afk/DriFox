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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
from uuid import uuid4

from loguru import logger
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from app.tools.tool_name_mapper import ToolNameMapper


class HookType(Enum):
    """Hook 类型"""
    COMMAND = "command"
    HTTP = "http"
    PYTHON = "python"
    PROMPT = "prompt"


class HookDecision(Enum):
    """Hook 决策结果"""
    CONTINUE = "continue"      # 继续执行
    BLOCK = "block"           # 阻止操作
    DEFER = "defer"           # 延迟执行


class HookConditionType(Enum):
    """条件类型"""
    ENV = "env"                # 环境变量条件，如 "env:DEBUG=true"
    FILE_PATTERN = "file"      # 文件模式匹配，如 "file:*.py"
    TOOL = "tool"             # 工具名匹配，如 "tool:bash"
    REGEX = "regex"           # 正则匹配，如 "regex:.*关键词.*"


@dataclass
class HookCondition:
    """单个条件配置"""
    type: str                  # env, file, tool, regex
    pattern: str               # 条件模式

    @classmethod
    def from_dict(cls, d: dict) -> 'HookCondition':
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
    timeout: int = 300
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

    @classmethod
    def from_dict(cls, d: dict) -> 'Hook':
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
            timeout=d.get("timeout", 300),
            retry=d.get("retry", 0),
            conditions=conditions,
            url=d.get("url"),
            headers=d.get("headers"),
            allowed_env_vars=d.get("allowedEnvVars"),
            function=d.get("function"),
            function_args=d.get("function_args"),
            config_file=d.get("config_file"),
            prompt=d.get("prompt"),
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
        if event_name == "SessionStart":
            session_state = context.get("state", "startup")
            states = [s.strip() for s in self.matcher.split("|")]
            return session_state in states

        # 工具名匹配（支持别名）
        if self.matcher.startswith("tool:"):
            pattern = self.matcher[5:]
            actual = context.get("tool_name", "")
            # 两边都用 ToolNameMapper 归一化
            return (ToolNameMapper.to_native(pattern) ==
                    ToolNameMapper.to_native(actual))

        # 正则匹配用户消息
        message = context.get("message", "")
        try:
            if re.match(self.matcher, message):
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
    def from_dict(cls, d: dict, skill_root: str = "", config_file: str = "") -> 'HookMatchRule':
        hooks = [Hook.from_dict(h) for h in d.get("hooks", [])]
        for h in hooks:
            h.skill_root = skill_root
            h.config_file = config_file  # 传递 config_file
        return cls(
            matcher=d.get("matcher"),
            hooks=hooks,
        )


class HookExecutionResult:
    """Hook 执行结果"""
    def __init__(self, success: bool, output: str = "", decision: str = "continue"):
        self.success = success
        self.output = output
        self.decision: HookDecision = HookDecision(decision) if decision else HookDecision.CONTINUE


class HookWorkerSignals(QObject):
    """Worker 信号，用于执行完后回调"""
    finished = pyqtSignal(str, str, bool)  # event_name, output, success


class HookOverrideManager:
    """
    Hook 覆写层管理器
    用于覆盖 plugin/skill hooks 的 enabled 状态（不影响源文件）
    存储位置: ~/.drifox/plugins/user-custom/hooks_overrides.json
    格式: {"hook_id": {"enabled": bool}}
    """

    OVERRIDES_FILE = "hooks_overrides.json"

    def __init__(self):
        self._overrides: Dict[str, Dict[str, bool]] = {}
        self._storage_path: Optional[str] = None
        self._init_storage_path()
        self._load()

    def _init_storage_path(self):
        """初始化存储路径（与项目 get_app_data_dir 保持一致）"""
        from app.utils.utils import get_app_data_dir
        storage_dir = get_app_data_dir() / "plugins" / "user-custom"
        storage_dir.mkdir(parents=True, exist_ok=True)
        self._storage_path = str(storage_dir / self.OVERRIDES_FILE)

    def _load(self):
        """从文件加载覆写配置"""
        if not self._storage_path or not os.path.exists(self._storage_path):
            return
        try:
            with open(self._storage_path, 'r', encoding='utf-8') as f:
                self._overrides = json.load(f)
            logger.debug(f"[HookOverrideManager] Loaded {len(self._overrides)} overrides")
        except Exception as e:
            logger.error(f"[HookOverrideManager] Failed to load overrides: {e}")
            self._overrides = {}

    def _save(self):
        """保存覆写配置到文件"""
        if not self._storage_path:
            return
        try:
            with open(self._storage_path, 'w', encoding='utf-8') as f:
                json.dump(self._overrides, f, indent=2, ensure_ascii=False)
            logger.debug(f"[HookOverrideManager] Saved {len(self._overrides)} overrides")
        except Exception as e:
            logger.error(f"[HookOverrideManager] Failed to save overrides: {e}")

    def get_effective_enabled(self, hook_id: str, default: bool) -> bool:
        """获取 hook 的有效 enabled 状态（覆写优先）"""
        if hook_id in self._overrides:
            return self._overrides[hook_id].get("enabled", default)
        return default

    def set_hook_enabled(self, hook_id: str, enabled: bool):
        """设置 hook 的覆写 enabled 状态"""
        self._overrides[hook_id] = {"enabled": enabled}
        self._save()

    def remove_override(self, hook_id: str):
        """移除 hook 的覆写（恢复默认值）"""
        if hook_id in self._overrides:
            del self._overrides[hook_id]
            self._save()


class HookWorker(QRunnable):
    """异步执行 Hook 的 Worker"""
    def __init__(self, hook: Hook, cwd: Optional[str], signals: HookWorkerSignals,
                 event_name: str = "", context: Dict[str, Any] = None):
        super().__init__()
        self.hook = hook
        self.cwd = cwd
        self.signals = signals
        self.event_name = event_name
        self.context = context or {}

    @staticmethod
    def _run_command_sync(command: str, cwd: Optional[str] = None, timeout: int = 300,
                          stdin_data: Optional[str] = None) -> tuple:
        """
        统一的命令同步执行方法（公共提取，避免代码重复）。
        返回 (output, success, exit_code)。
        处理 echo 快捷方式、Windows 编码回退、路径分隔符转换。
        stdin_data: 可选的 stdin 输入（传递给脚本的 JSON 上下文）
        """
        # echo 快捷方式（不需要 stdin）
        if command.startswith("echo "):
            output = command[5:].strip()
            if output.startswith('"') and output.endswith('"'):
                output = output[1:-1]
            elif output.startswith("'") and output.endswith("'"):
                output = output[1:-1]
            return output, True, 0

        # 修复路径分隔符问题：Unix / 转 Windows \
        if os.name == 'nt':
            command = command.replace('/', '\\')

        # 构造 subprocess 参数
        subprocess_kwargs = {
            'cwd': cwd,
            'shell': True,
            'capture_output': True,
            'text': True,
            'errors': 'replace',
            'timeout': timeout,
            'creationflags': subprocess.CREATE_NO_WINDOW
            if os.name == 'nt'
            else 0,
        }
        if stdin_data is not None:
            subprocess_kwargs['input'] = stdin_data
        else:
            subprocess_kwargs['stdin'] = subprocess.DEVNULL

        if os.name == 'nt':
            import locale
            preferred = locale.getpreferredencoding(False) or ''
            if preferred.upper() not in ('UTF-8', 'UTF8'):
                try:
                    result = subprocess.run(
                        command, encoding='utf-8', **subprocess_kwargs
                    )
                except Exception:
                    result = subprocess.run(
                        command, encoding=preferred or 'gbk', **subprocess_kwargs
                    )
                exit_code = result.returncode
                if exit_code != 0:
                    # exit code 2: Claude Code BLOCK 约定，用 stdout 作为 output
                    if exit_code == 2:
                        return result.stdout or "", False, exit_code
                    return result.stderr or f"Command failed with exit code {exit_code}", False, exit_code
                return result.stdout or "", True, exit_code
            else:
                result = subprocess.run(
                    command, encoding='utf-8', **subprocess_kwargs
                )
        else:
            result = subprocess.run(
                command, encoding='utf-8', **subprocess_kwargs
            )

        exit_code = result.returncode
        if exit_code != 0:
            # exit code 2: Claude Code BLOCK 约定，用 stdout 作为 output
            if exit_code == 2:
                return result.stdout or "", False, exit_code
            return result.stderr or f"Command failed with exit code {exit_code}", False, exit_code
        return result.stdout or "", True, exit_code

    def run(self):
        """执行命令，收集输出"""
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

            self.signals.finished.emit(self.event_name, output, success)
        except Exception as e:
            logger.error(f"[HookWorker] Execution failed: {e}")
            self.signals.finished.emit(self.event_name, f"Error: {str(e)}", False)

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
        if os.name == 'nt' and self.hook.commandWindows:
            effective_cmd = self.hook.commandWindows
        stdin_data = _json.dumps(self.context) if self.context else None
        output, success, _ = HookWorker._run_command_sync(
            effective_cmd, self.cwd, self.hook.timeout,
            stdin_data=stdin_data
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
            data = json.dumps({
                "event": self.event_name,
                "context": self.context,
            }).encode('utf-8')

            req = urllib.request.Request(url, data=data, headers=headers, method='POST')

            with urllib.request.urlopen(req, timeout=self.hook.timeout) as response:
                output = response.read().decode('utf-8')
                return output, True
        except Exception as e:
            return f"HTTP request failed: {str(e)}", False

    @staticmethod
    def _import_relative_function(function_path: str, config_file: str) -> Optional[Callable]:
        """从相对模块路径导入函数（.module:func → <config_dir>/module.py 中的 func）

        Args:
            function_path: 函数路径，如 .evolver_hook:hook_session_start
            config_file: hooks.json 的完整路径

        Returns:
            可调用的函数对象，失败返回 None
        """
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
                f"_hook_relative_{relative_dotted.replace('/', '_')}",
                str(abs_path)
            )
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            func = getattr(module, func_name, None)
            return func if callable(func) else None
        except Exception as e:
            logger.error(f"[HookWorker] Failed to load relative module {abs_path}: {e}")
            return None

    def _execute_python(self) -> tuple:
        """执行 Python 函数"""
        try:
            if not self.hook.function:
                return "No function specified", False

            # 解析函数路径 (module.path:function_name)
            parts = self.hook.function.rsplit(":", 1)
            if len(parts) != 2:
                return f"Invalid function path: {self.hook.function}", False

            module_path, func_name = parts

            # 相对路径：基于 hooks.json 目录解析
            func = None
            if module_path.startswith(".") and self.hook.config_file:
                func = HookWorker._import_relative_function(
                    self.hook.function, self.hook.config_file
                )

            # 标准路径：importlib.import_module
            if func is None:
                import importlib
                module = importlib.import_module(module_path)
                func = getattr(module, func_name, None)

            if not callable(func):
                return f"Function not found: {self.hook.function}", False

            # 执行函数
            args = self.hook.function_args or {}
            args["event"] = self.event_name
            args["context"] = self.context

            result = func(**args)

            if isinstance(result, str):
                return result, True
            elif isinstance(result, dict):
                return json.dumps(result), True
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

    def __init__(self, thread_pool: Optional[QThreadPool] = None):
        # hooks 注册数据指向类级别的共享字典（所有窗口共用）
        self._hooks: Dict[str, List[HookMatchRule]] = HookManager._shared_hooks
        self._skill_to_hooks: Dict[str, List[tuple[str, int]]] = HookManager._shared_skill_to_hooks

        # 线程池
        self._thread_pool = thread_pool or QThreadPool.globalInstance()

        # 完成回调（每个窗口独立）
        self._on_finished_callback: Optional[Callable[[str, str, bool], None]] = None

        # 决策回调（每个窗口独立）
        self._on_decision_callback: Optional[Callable[[str, HookDecision], None]] = None

        # 配置热重载监控（类级别共享）
        self._config_watchers: Dict[str, float] = HookManager._shared_config_watchers
        self._config_file: Optional[str] = None

        # 注册的 Python 函数（类级别共享）
        self._registered_functions: Dict[str, Callable] = HookManager._shared_registered_functions

        # cwd 解析缓存（类级别共享）
        self._cwd_resolve_cache: Dict[int, tuple] = HookManager._shared_cwd_resolve_cache
        self._CWD_CACHE_TTL = 30.0  # 30秒缓存

        # 覆写层管理器
        self._override_manager = HookOverrideManager()

    def _is_user_custom_hook(self, hook: Hook) -> bool:
        """判断 hook 是否为用户自定义（非 plugin/skill）"""
        return "user-custom" in (hook.config_file or "") or not hook.config_file

    def _get_effective_hook_dict(self, hook: Hook) -> dict:
        """获取覆写后的 hook dict（覆写层的 enabled 优先）"""
        d = hook.to_dict()
        # user-custom 的 hook 不走覆写层（直接改源文件）
        if not self._is_user_custom_hook(hook):
            # plugin/skill hooks 走覆写层
            effective_enabled = self._override_manager.get_effective_enabled(
                hook.id, hook.enabled
            )
            d["enabled"] = effective_enabled
        return d

    def set_on_finished_callback(self, callback: Callable[[str, str, bool], None]):
        """设置 Hook 执行完成回调"""
        self._on_finished_callback = callback

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

    def register_hooks_from_json(self, skill_name: str, skill_root: str,
                                  hooks_config: Union[dict, str], config_file: str = None) -> int:
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
                with open(hooks_config, 'r', encoding='utf-8') as f:
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

                    match_rule = HookMatchRule.from_dict(rule_data, skill_root, config_file)
                    if match_rule.hooks:
                        rule_index = len(self._hooks[event_name])
                        self._hooks[event_name].append(match_rule)
                        count += len(match_rule.hooks)

                        if skill_name not in self._skill_to_hooks:
                            self._skill_to_hooks[skill_name] = []
                        self._skill_to_hooks[skill_name].append((event_name, rule_index))
            else:
                logger.warning(f"[HookManager] Invalid rules format for {event_name}")

        logger.info(f"[HookManager] Registered {count} hooks for skill {skill_name}")

        # 持久化生成的 hook id 到源文件（确保下次启动 id 不变）
        if count > 0 and config_file:
            self._persist_hook_ids_to_file(config_file)

        return count

    def _persist_hook_ids_to_file(self, config_file: str):
        """将内存中 hook 的 id 写回到 JSON 配置文件
        
        按 config_file 匹配内存中的 hook，而不是按全局位置索引。
        避免不同来源（如不同插件）的 hook 拿到相同的 id。
        """
        if not config_file or not os.path.exists(config_file):
            return
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
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
            for event_name, rules in raw_hooks.items():
                idx = 0
                for rule in rules:
                    for h in rule.get("hooks", []):
                        if not h.get("id"):
                            ids = mem_ids.get(event_name, [])
                            if idx < len(ids):
                                h["id"] = ids[idx]
                                modified = True
                            idx += 1
                        else:
                            # 有 id 的也算进序号计数（保持位置对应）
                            idx += 1

            if modified:
                with open(config_file, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                logger.debug(f"[HookManager] Persisted hook ids to {config_file}")
        except Exception as e:
            logger.error(f"[HookManager] Failed to persist hook ids to {config_file}: {e}")

    def unregister_skill_hooks(self, skill_name: str):
        """注销一个技能的所有 Hooks"""
        if skill_name not in self._skill_to_hooks:
            return

        for (event_name, rule_index) in reversed(self._skill_to_hooks[skill_name]):
            if event_name in self._hooks and rule_index < len(self._hooks[event_name]):
                self._hooks[event_name].pop(rule_index)

        del self._skill_to_hooks[skill_name]
        logger.debug(f"[HookManager] Unregistered all hooks for skill {skill_name}")

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

    def dynamic_register_hook(self, skill_name: str, event_name: str,
                             hook: Union[Hook, dict], matcher: str = None) -> int:
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
        rule_index = len(self._hooks[event_name])
        self._hooks[event_name].append(rule)

        if skill_name not in self._skill_to_hooks:
            self._skill_to_hooks[skill_name] = []
        self._skill_to_hooks[skill_name].append((event_name, rule_index))

        logger.info(f"[HookManager] Dynamically registered hook: {event_name} for {skill_name}")
        return rule_index

    def dynamic_unregister_hook(self, skill_name: str, event_name: str,
                               hook_index: int) -> bool:
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

        # 更新技能索引
        if skill_name in self._skill_to_hooks:
            self._skill_to_hooks[skill_name] = [
                (e, i - 1 if i > hook_index else i)
                for (e, i) in self._skill_to_hooks[skill_name]
                if i != hook_index
            ]

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
            current_mtime = os.path.getmtime(config_file)
            last_mtime = self._config_watchers.get(config_file, 0)

            if current_mtime <= last_mtime:
                return False  # 文件未修改

            # 重新加载配置
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 更新监控时间
            self._config_watchers[config_file] = current_mtime

            # 重新注册 (保留技能注册，skill_name 统一用 "user-custom")
            self.register_hooks_from_json("user-custom", "", config, config_file)

            logger.info(f"[HookManager] Hot reloaded hooks from {config_file}")
            return True
        except Exception as e:
            logger.error(f"[HookManager] Failed to reload hooks: {e}")
            return False

    def check_and_reload(self):
        """检查配置是否变更，必要时热重载"""
        self.reload_hooks_config()

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
            # 文件模式匹配
            current_file = context.get("file", "")
            if not current_file:
                return False
            try:
                return bool(re.match(pattern.replace("*", ".*"), current_file))
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

    def trigger_event(self, event_name: str, context: Dict[str, Any] = None,
                     current_message: str = "",
                     trigger_async: bool = True) -> List[HookExecutionResult]:
        """
        触发事件，执行所有匹配的 Hooks
        
        Args:
            event_name: 事件名
            context: 上下文信息
            current_message: 当前消息 (用于 matcher 匹配)
            trigger_async: 是否异步执行
        
        Returns:
            执行结果列表
        """
        context = context or {}
        context["message"] = current_message
        context["event_name"] = event_name
        context["timestamp"] = time.time()

        if event_name not in self._hooks:
            return []

        results = []
        for rule in self._hooks[event_name]:
            # 检查 matcher 条件
            if not rule.matches(context):
                continue

            for hook in rule.hooks:
                # 检查启用状态（走覆写层：plugin/skill 看 override，user-custom 看源文件）
                hook_enabled = (
                    hook.enabled if self._is_user_custom_hook(hook)
                    else self._override_manager.get_effective_enabled(hook.id, hook.enabled)
                )
                if not hook_enabled:
                    continue

                # 检查执行条件
                if not self._check_conditions(hook, context):
                    logger.debug(f"[HookManager] Hook conditions not met: {event_name}")
                    continue

                # 执行 hook
                result = self._execute_hook(hook, context, trigger_async)
                results.append(result)

        return results

    def _execute_hook(self, hook: Hook, context: Dict[str, Any],
                     trigger_async: bool = True) -> HookExecutionResult:
        """执行单个 Hook"""
        # cwd: 智能解析（显式设置 > 从命令脚本路径推导 > 默认项目根目录）
        cwd = self._resolve_command_cwd(hook, context)

        # 变量替换
        # Windows 上优先使用 commandWindows（Claude Code 插件兼容）
        if os.name == 'nt' and hook.commandWindows:
            effective_command = hook.commandWindows
        else:
            effective_command = hook.command
        command = self._interpolate_variables(effective_command, context)
        url = self._interpolate_variables(hook.url or "", context)

        if trigger_async and hook.type == HookType.COMMAND.value:
            signals = HookWorkerSignals()
            worker = HookWorker(hook, cwd, signals, context.get("event_name", ""), context)

            # 仅在 hook 声明 add_output_to_context=True 时才连接完成回调
            if hook.add_output_to_context and self._on_finished_callback:
                signals.finished.connect(self._on_finished_callback)

            self._thread_pool.start(worker)
            logger.debug(f"[HookManager] Hook triggered (async): {context.get('event_name')} "
                        f"(add_output_to_context={hook.add_output_to_context})")

            return HookExecutionResult(success=True, output="")
        else:
            # 同步执行
            try:
                output = ""
                success = False
                _exit2_skip = False  # exit code 2 跳过标记（Claude Code 兼容）

                if hook.type == HookType.COMMAND.value:
                    output, success, exit_code = HookWorker._run_command_sync(
                        command, cwd, hook.timeout,
                        stdin_data=json.dumps(context)
                    )
                    _exit2_skip = (exit_code == 2)

                elif hook.type == HookType.HTTP.value:
                    import urllib.error
                    import urllib.request

                    data = json.dumps({"event": context.get("event_name"), "context": context}).encode('utf-8')
                    headers = hook.headers or {}
                    headers["Content-Type"] = "application/json"

                    req = urllib.request.Request(url, data=data, headers=headers, method='POST')

                    with urllib.request.urlopen(req, timeout=hook.timeout) as response:
                        output = response.read().decode('utf-8')
                        success = True

                elif hook.type == HookType.PROMPT.value:
                    output = hook.prompt or hook.command or ""
                    success = True

                elif hook.type == HookType.PYTHON.value:
                    if not hook.function:
                        output = "No function specified"
                        success = False
                    elif hook.function in self._registered_functions:
                        func = self._registered_functions[hook.function]
                        args = hook.function_args or {}
                        args.update({"event": context.get("event_name"), "context": context})
                        result = func(**args)
                        output = result if isinstance(result, str) else json.dumps(result)
                        success = True
                    else:
                        # 尝试解析 Python 函数
                        try:
                            parts = hook.function.rsplit(":", 1)
                            if len(parts) != 2:
                                output = f"Function not registered (invalid path): {hook.function}"
                                success = False
                            else:
                                module_path, func_name = parts
                                func = None

                                # 相对路径：基于 hooks.json 目录解析
                                if module_path.startswith(".") and hook.config_file:
                                    func = HookWorker._import_relative_function(
                                        hook.function, hook.config_file
                                    )

                                # 标准路径：importlib.import_module
                                if func is None:
                                    import importlib
                                    module = importlib.import_module(module_path)
                                    func = getattr(module, func_name, None)

                                if not callable(func):
                                    output = f"Function not found: {hook.function}"
                                    success = False
                                else:
                                    args = hook.function_args or {}
                                    args["event"] = context.get("event_name")
                                    args["context"] = context
                                    result = func(**args)
                                    output = result if isinstance(result, str) else json.dumps(result)
                                    success = True
                        except Exception as e:
                            output = f"Python hook failed: {str(e)}"
                            success = False

                # 检查决策（支持 JSON decision 和 exit code 2 两种方式）
                decision = HookDecision.CONTINUE

                # 方式1: 检测 exit code 2（Claude Code 兼容：跳过工具执行）
                if _exit2_skip:
                    decision = HookDecision.BLOCK
                    logger.info(f"[HookManager] Hook exit code 2 → BLOCK: {context.get('event_name')}")

                # 方式2: 解析 JSON 中的 decision 字段
                try:
                    output_data = json.loads(output)
                    if isinstance(output_data, dict):
                        decision_str = output_data.get("decision", "continue")
                        if decision_str in ["block", "continue", "defer"]:
                            decision = HookDecision(decision_str)
                except json.JSONDecodeError:
                    pass

                # 触发决策回调
                if decision != HookDecision.CONTINUE and self._on_decision_callback:
                    self._on_decision_callback(context.get("event_name", ""), decision)

                # 触发完成回调
                if hook.add_output_to_context and self._on_finished_callback:
                    callback_event = context.get("event_name", "")
                    # PROMPT 类型 hook 用前缀标记，backend 据此总是加入消息列表
                    if hook.type == HookType.PROMPT.value:
                        callback_event = f"__prompt__:{callback_event}"
                    self._on_finished_callback(callback_event, output, success)

                logger.info(f"[HookManager] Hook executed: {context.get('event_name')}")

                return HookExecutionResult(success=success, output=output, decision=decision.value)

            except Exception as e:
                logger.error(f"[HookManager] Hook failed: {context.get('event_name')} - {e}")
                if hook.add_output_to_context and self._on_finished_callback:
                    callback_event = context.get("event_name", "")
                    if hook.type == HookType.PROMPT.value:
                        callback_event = f"__prompt__:{callback_event}"
                    self._on_finished_callback(callback_event, f"Error: {str(e)}", False)
                return HookExecutionResult(success=False, output=str(e))

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

        # 匹配常见的脚本调用模式：
        # ./script, script.ext, bash script, cmd script, python script 等
        patterns = [
            r'^\s*\.?/?([^\s]+\.(cmd|bat|ps1|sh|bash))\s',  # 相对路径脚本
            r'\s+([^\s/]+\.(cmd|bat|ps1|sh|bash))\s',       # 空格后的脚本
            r'\s+([^\s/]+/[^\s]+\.(cmd|bat|ps1|sh|bash))\s',  # 带目录的脚本
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
                    hooks_dir = os.path.join(hook.skill_root, 'hooks')
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

    def _interpolate_variables(self, text: str, context: Dict[str, Any]) -> str:
        """变量替换"""
        if not text:
            return text

        variables = {
            "{skill_root}": context.get("skill_root", ""),
            "{project_root}": context.get("project_root", ""),
            "{message}": context.get("message", ""),
            "{file}": context.get("file", ""),
            "{tool_name}": context.get("tool_name", ""),
            "{event_name}": context.get("event_name", ""),
        }

        for var, value in variables.items():
            if value:
                text = text.replace(var, str(value))

        # 环境变量替换
        text = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ""), text)
        text = re.sub(r'\$(\w+)', lambda m: os.environ.get(m.group(1), ""), text)

        return text

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
                    rules_data.append({
                        "matcher": rule.matcher,
                        "hooks": hooks_data
                    })
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

                    if event_name not in grouped[source_type]:
                        grouped[source_type][event_name] = []
                    grouped[source_type][event_name].append(hook_dict)
        return grouped

    def _build_hook_source_map(self) -> Dict[str, tuple]:
        """构建 hook_id -> (source_type, display_name) 的映射"""
        result = {}
        for skill_name, entries in self._skill_to_hooks.items():
            if skill_name == "user-custom":
                source_type, display_name = "user", "自定义"
            elif skill_name.startswith("__skill__"):
                source_type, display_name = "skill", skill_name.replace("__skill__", "")
            else:
                source_type, display_name = "plugin", skill_name

            for event_name, rule_idx in entries:
                if event_name not in self._hooks or rule_idx >= len(self._hooks[event_name]):
                    continue
                rule = self._hooks[event_name][rule_idx]
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
        """在配置中查找 hook 所在的路径字段，返回 (event_name, rule_idx, hook_idx) 或 None"""
        raw_hooks = config.get("hooks", config)
        for event_name, rules in raw_hooks.items():
            for rule_idx, rule in enumerate(rules):
                hooks_list = rule.get("hooks", [])
                for hook_idx, h in enumerate(hooks_list):
                    hook_id = h.get("id", "") or ""
                    hook_cmd = (
                        h.get("command", "") or h.get("url", "") or
                        h.get("function", "") or h.get("prompt", "")
                    )
                    target_cmd = (
                        hook.command or hook.url or hook.function or hook.prompt or ""
                    )
                    if hook_id == hook.id or hook_cmd == target_cmd:
                        return (event_name, rule_idx, hook_idx)
        return None

    def _update_skill_index_for_hook(self, hook: Hook, new_event_name: str, new_rule_idx: int):
        """更新 skill_to_hooks 索引（当 hook 移动到不同事件时）"""
        # 遍历所有 skill，找到引用该 hook 的
        old_event = None
        old_rule_idx = None
        found_skill_name = None
        for skill_name, entries in self._skill_to_hooks.items():
            for i, (evt, ridx) in enumerate(entries):
                if evt in self._hooks and ridx < len(self._hooks[evt]):
                    rule = self._hooks[evt][ridx]
                    for h in rule.hooks:
                        if h.id == hook.id:
                            old_event = evt
                            old_rule_idx = ridx
                            found_skill_name = skill_name
                            break
                if old_event:
                    break
            if old_event:
                break

        if old_event and found_skill_name and old_event != new_event_name:
            # 从旧位置移除
            self._skill_to_hooks[found_skill_name] = [
                (e, r) for (e, r) in self._skill_to_hooks[found_skill_name]
                if not (e == old_event and r == old_rule_idx)
            ]
            # 添加到新位置
            self._skill_to_hooks[found_skill_name].append((new_event_name, new_rule_idx))

    def _save_hook_to_file_by_id(self, hook: Hook, new_data: dict = None):
        """通过 hook_id 保存到源文件（支持事件/matcher 变更时的移动）"""
        if not hook.config_file or not os.path.exists(hook.config_file):
            return

        try:
            with open(hook.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 查找 hook 位置
            location = self._find_hook_fields(hook, config)
            if location is None:
                logger.warning(f"[HookManager] Hook {hook.id} not found in {hook.config_file}")
                return

            event_name, rule_idx, hook_idx = location
            raw_hooks = config.get("hooks", config)
            target_rule = raw_hooks[event_name][rule_idx]
            target_hooks = target_rule.get("hooks", [])

            # 合并更新数据
            hook_entry = target_hooks[hook_idx]
            if new_data:
                # 处理 enabled
                if "enabled" in new_data:
                    hook_entry["enabled"] = new_data["enabled"]
                # 处理 command/url/function
                if "command" in new_data:
                    hook_entry["command"] = new_data["command"]
                if "url" in new_data:
                    hook_entry["url"] = new_data["url"]
                if "function" in new_data:
                    hook_entry["function"] = new_data["function"]
                # 处理 matcher 变更（移动到新事件）
                if "matcher" in new_data and "new_event_name" in new_data:
                    new_event = new_data["new_event_name"]
                    new_matcher = new_data["matcher"]

                    # 创建新事件条目
                    if new_event not in raw_hooks:
                        raw_hooks[new_event] = []
                    raw_hooks[new_event].append({"matcher": new_matcher, "hooks": [hook_entry]})

                    # 从旧位置移除
                    target_hooks.pop(hook_idx)
                    if not target_rule.get("hooks"):
                        raw_hooks[event_name].pop(rule_idx)
                    if not raw_hooks.get(event_name):
                        del raw_hooks[event_name]

                    # 更新内存中的 matcher
                    hook.config_file = hook.config_file  # 保持不变

                # 处理其他字段
                for key in ["type", "cwd", "add_output_to_context", "skill_root",
                            "timeout", "retry", "conditions", "headers",
                            "allowedEnvVars", "function_args",
                            "commandWindows", "statusMessage"]:
                    if key in new_data:
                        hook_entry[key] = new_data[key]

            # 确保 id 字段存在
            hook_entry["id"] = hook.id

            with open(hook.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            logger.debug(f"[HookManager] Saved hook {hook.id} to {hook.config_file}")
        except Exception as e:
            logger.error(f"[HookManager] Failed to save hook {hook.id}: {e}")

    def edit_hook_by_id(self, hook_id: str, new_data: dict) -> bool:
        """
        通过 id 编辑 hook
        
        Args:
            hook_id: hook 唯一标识
            new_data: 要更新的字段（如 {"command": "new_cmd", "enabled": False}）
        
        Returns:
            是否成功
        """
        result = self._find_hook_by_id(hook_id)
        if result is None:
            logger.warning(f"[HookManager] Hook {hook_id} not found")
            return False

        event_name, rule_idx, hook_idx, hook = result

        # 更新内存中的 hook 对象
        if "command" in new_data:
            hook.command = new_data["command"]
        if "url" in new_data:
            hook.url = new_data["url"]
        if "function" in new_data:
            hook.function = new_data["function"]
        if "type" in new_data:
            hook.type = new_data["type"]
        if "enabled" in new_data:
            hook.enabled = new_data["enabled"]
        if "cwd" in new_data:
            hook.cwd = new_data["cwd"]
        if "timeout" in new_data:
            hook.timeout = new_data["timeout"]
        if "retry" in new_data:
            hook.retry = new_data["retry"]
        if "matcher" in new_data:
            # 更新内存中的 matcher
            self._hooks[event_name][rule_idx].matcher = new_data["matcher"]
        if "conditions" in new_data:
            hook.conditions = [HookCondition.from_dict(c) for c in new_data["conditions"]]
        if "headers" in new_data:
            hook.headers = new_data["headers"]
        if "allowedEnvVars" in new_data:
            hook.allowed_env_vars = new_data["allowedEnvVars"]
        if "function_args" in new_data:
            hook.function_args = new_data["function_args"]
        if "add_output_to_context" in new_data:
            hook.add_output_to_context = new_data["add_output_to_context"]
        if "commandWindows" in new_data:
            hook.commandWindows = new_data["commandWindows"]
        if "statusMessage" in new_data:
            hook.statusMessage = new_data["statusMessage"]

        # 事件变更：从当前事件移动到新事件
        new_event = new_data.get("event", event_name)
        if new_event != event_name:
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
            # 查找是否已有相同 matcher 的 rule
            matched_rule = None
            for r in self._hooks[new_event]:
                if (r.matcher or "") == new_matcher:
                    matched_rule = r
                    break
            if matched_rule:
                matched_rule.hooks.append(hook)
            else:
                new_rule = HookMatchRule(matcher=new_matcher or None, hooks=[hook])
                self._hooks[new_event].append(new_rule)

            # 找到新 rule 索引
            new_rule_idx = next(
                (i for i, r in enumerate(self._hooks[new_event]) if hook in r.hooks),
                0
            )
            self._update_skill_index_for_hook(hook, new_event, new_rule_idx)
        else:
            # 同事件内更新 matcher
            if "matcher" in new_data:
                self._hooks[event_name][rule_idx].matcher = new_data["matcher"] or None

        # 保存到源文件
        self._save_hook_to_file_by_id(hook, new_data)

        logger.info(f"[HookManager] Edited hook {hook_id}")
        return True

    def toggle_hook_by_id(self, hook_id: str, enabled: bool) -> bool:
        """
        通过 id 切换 hook 启用状态
        
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

        # 判断 hook 来源：user-custom 直接改源文件，plugin/skill 走覆写层
        if self._is_user_custom_hook(hook):
            # user-custom: 直接改内存 + 写回源文件
            hook.enabled = enabled
            self._save_hook_to_file_by_id(hook, {"enabled": enabled})
        else:
            # plugin/skill: 写覆写层
            self._override_manager.set_hook_enabled(hook_id, enabled)

        logger.info(f"[HookManager] Toggled hook {hook_id} enabled={enabled}")
        return True

    def delete_hook_by_id(self, hook_id: str) -> bool:
        """
        通过 id 删除 hook
        
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
        config_file = hook.config_file

        # 从内存中删除
        self._hooks[event_name][rule_idx].hooks.pop(hook_idx)
        # 如果规则空了，移除规则
        if not self._hooks[event_name][rule_idx].hooks:
            self._hooks[event_name].pop(rule_idx)
        # 如果事件空了，移除事件
        if not self._hooks.get(event_name):
            del self._hooks[event_name]

        # 从覆写层清除记录
        self._override_manager.remove_override(hook_id)

        # 从源文件删除
        if config_file and os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
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

                    with open(config_file, 'w', encoding='utf-8') as f:
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
            with open(hook.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 递归查找并更新 hook
            self._update_hook_in_config(config, event_name, hook)

            with open(hook.config_file, 'w', encoding='utf-8') as f:
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
            with open(config_file, 'r', encoding='utf-8') as f:
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
            from app.core.plugin_manager import PluginManager
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

    def load_hooks_from_directory(self, agents_dir: Path) -> int:
        """从 agents_dir 子目录加载 hooks.json (agents/{name}/hooks/hooks.json)"""
        count = 0
        if not agents_dir.exists():
            return count

        for agent_dir in agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            hooks_file = agent_dir / "hooks" / "hooks.json"
            if hooks_file.exists():
                try:
                    with open(hooks_file, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                    n = self.register_hooks_from_json(
                        agent_dir.name,
                        str(agent_dir.absolute()),
                        config,
                        str(hooks_file)
                    )
                    count += n
                    if n > 0:
                        logger.info(f"[HookManager] Loaded {n} hooks from {agent_dir.name}")
                except Exception as e:
                    logger.error(f"[HookManager] Failed to load hooks from {hooks_file}: {e}")
        return count

    def load_hooks_from_directory_flat(self, dir_path: Path, skill_name: str = None) -> int:
        """从目录直接加载 hooks.json（插件顶层 hooks/ 目录）

        加载 {dir_path}/hooks.json 文件（如果有）。

        Args:
            dir_path: hooks 目录路径
            skill_name: 注册用的 skill 名称。为 None 时使用 dir_path.name（兼容旧调用）
        """
        count = 0
        if not dir_path.exists() or not dir_path.is_dir():
            return count

        hooks_file = dir_path / "hooks.json"
        if not hooks_file.exists():
            return count

        try:
            with open(hooks_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            n = self.register_hooks_from_json(
                skill_name or dir_path.name,
                str(dir_path.absolute()),
                config,
                str(hooks_file)
            )
            count += n
            if n > 0:
                logger.info(f"[HookManager] Loaded {n} hooks from {dir_path.name}/hooks.json")
        except Exception as e:
            logger.error(f"[HookManager] Failed to load hooks from {hooks_file}: {e}")

        return count

    def load_hooks_from_skills(self, skills_dir: Path, force: bool = False) -> int:
        """从 skills_dir 加载 hooks.json 和 SKILL.md

        Args:
            skills_dir: skills 根目录
            force: 为 True 时强制重新加载（reload_agents 时调用）
        """
        count = 0
        if not skills_dir.exists():
            return count

        # reload_agents 时 force=True，全量重新加载
        if force:
            # 先注销该 skills 目录下的所有 hooks
            for skill_dir in skills_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                # 使用 __skill__ 前缀命名空间，避免与同名的插件 hooks 冲突
                # 例如插件 evolver 注册 skill_name="evolver"
                # 而技能 evolver 注册 skill_name="__skill__evolver"
                self.unregister_skill_hooks(f"__skill__{skill_dir.name}")

        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            # 使用 __skill__ 前缀，避免与插件 hooks 共享同一 skill_name
            skill_name = f"__skill__{skill_dir.name}"

            # 加载 hooks.json
            hooks_file = skill_dir / "hooks" / "hooks.json"
            if hooks_file.exists():
                try:
                    with open(hooks_file, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                    n = self.register_hooks_from_json(
                        skill_name,
                        str(skill_dir.absolute()),
                        config,
                        str(hooks_file)
                    )
                    count += n
                    if n > 0:
                        logger.info(f"[HookManager] Loaded {n} hooks from skill {skill_name}")
                except Exception as e:
                    logger.error(f"[HookManager] Failed to load hooks from skill {hooks_file}: {e}")

            # 加载 SKILL.md 中的 frontmatter hooks
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                try:
                    n = self._load_skill_hooks_from_markdown(skill_dir, skill_md)
                    count += n
                except Exception as e:
                    logger.error(f"[HookManager] Failed to load hooks from SKILL.md {skill_md}: {e}")

        return count

    def _load_skill_hooks_from_markdown(self, skill_dir: Path, md_file: Path) -> int:
        """从 SKILL.md frontmatter 加载 hooks 配置"""
        import re

        content = md_file.read_text(encoding='utf-8')

        # 解析 frontmatter
        if not content.startswith("---"):
            return 0

        parts = content.split("---", 2)
        if len(parts) < 3:
            return 0

        body = parts[2]
        # 使用 __skill__ 前缀，与 load_hooks_from_skills 保持一致
        skill_name = f"__skill__{skill_dir.name}"

        # 查找 <hooks> 块
        hooks_pattern = r'<hooks>(.*?)</hooks>'
        hooks_match = re.search(hooks_pattern, body, re.DOTALL)

        if not hooks_match:
            return 0

        hooks_text = hooks_match.group(1).strip()
        config = self._parse_inline_hooks(hooks_text)

        if config.get("hooks"):
            n = self.register_hooks_from_json(
                skill_name,
                str(skill_dir.absolute()),
                config,
                str(md_file)
            )
            if n > 0:
                logger.info(f"[HookManager] Loaded {n} hooks from SKILL.md of {skill_name}")
            return n
        return 0

    def _parse_inline_hooks(self, hooks_text: str) -> dict:
        """解析内联 hooks 文本格式"""

        config = {"hooks": {}}
        current_event = None
        current_rules = []

        for line in hooks_text.split('\n'):
            line = line.rstrip()
            if not line:
                continue

            # 检查是否是事件名行（不以空格开头）
            if line and not line[0].isspace():
                # 保存上一个事件的 hooks
                if current_event:
                    config["hooks"][current_event] = current_rules

                current_event = line.strip()
                current_rules = []
            else:
                # 是 hook 规则行
                if current_event is None:
                    continue

                # 解析简化的 hook 格式
                hook_data = {}
                parts = line.strip().lstrip('- ')
                if ':' in parts:
                    key, value = parts.split(':', 1)
                    hook_data[key.strip()] = value.strip()

                if hook_data:
                    current_rules.append(hook_data)

        # 保存最后一个事件
        if current_event:
            config["hooks"][current_event] = current_rules

        return config
