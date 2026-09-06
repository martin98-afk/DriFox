# -*- coding: utf-8 -*-
"""
工具注册表 — 工具插件化的核心（进程级单例）

职责：
- 注册 / 注销 / 枚举 / 查询工具
- 每个工具注册时携带完整元数据：schema、danger（危险/安全）、icon（SVG 图标名）、
  cn_name（中文显示名）、group（权限卡片分组）、description（权限卡片行描述）、
  aliases（Claude Code 风格别名）、impl（执行函数）
- 版本号单调递增：驱动 schema 缓存失效 + UI（权限卡片/渲染层）热刷新
- 热插拔并发安全：执行方调用前取 snapshot()，unregister 只移除映射，已持有引用不受影响

数据流（单一数据源）：
- get_builtin_tools_schema()   → registry.schemas()        （LLM 工具定义）
- render_helpers 图标/中文名    → registry 元数据            （消息卡片渲染）
- tool_control_card 分组/描述   → registry 元数据            （权限设置卡片）
- tool_classifier 危险分类      → registry.danger            （权限控制器）
- ToolNameMapper 别名          → registry aliases           （hook 上下文/命令解析）
- ToolExecutor 执行            → registry.impl               （工具分发）
"""

from __future__ import annotations

import copy
import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

# 工具危险级别（注册时必须显式声明）
DANGER_SAFE = "safe"
DANGER_DANGEROUS = "dangerous"

# 默认分组：未显式声明 group 的 safe 工具落入"安全操作"兜底组
GROUP_DEFAULT_SAFE = "安全操作"
GROUP_DEFAULT_DANGEROUS = "其他"

# 工具默认 fallback icon 名（插件未声明、registry 找不到、渲染层 except 兜底统一引用）
# 单一来源：修改此处即同步影响 dataclass 默认值 / register() 默认 / get_icon() 兜底 / render_helpers._get_tool_icon_name()
DEFAULT_FALLBACK_ICON = "工具"


@dataclass
class ToolRegistration:
    """单个工具的完整注册信息"""

    name: str
    schema: Dict[str, Any]
    impl: Optional[Callable] = None  # 执行函数（可选；内置工具可走 ToolExecutor 特殊分发）
    danger: str = DANGER_SAFE  # safe | dangerous（强制声明）
    icon: str = DEFAULT_FALLBACK_ICON  # SVG 图标文件名（不含扩展名，渲染层 fallback DEFAULT_FALLBACK_ICON）
    icon_dir: str = ""  # 插件自带深色图标目录（绝对路径；空 → 渲染回退主程序 qrc 资源）
    icon_dir_light: str = ""  # 插件自带浅色图标目录（主题感知；空 → 回退深色/qrc）
    cn_name: str = ""  # 中文显示名（空 → 渲染层回退原名）
    group: str = ""  # 权限卡片分组（空 → 按 danger 落入兜底组）
    description: str = ""  # 权限卡片行内描述
    source: str = "builtin"  # builtin | plugin:<name>
    team_only: bool = False  # 团队专用：仅团队成员可见（非成员从 schema 定义中过滤）
    render: Optional[Callable] = (
        None  # 工具完成框 body 渲染闭包：render(result, tool_name, tool_args, success) -> str|None
    )
    render_mode: str = ""  # 完成框渲染模式：""=默认折叠卡 / "inline"=紧凑单行(无body) / "expand"=完整卡无折叠(body始终展开) / "none"=不渲染完成框
    preview: Optional[Callable] = None  # 自然语言预览闭包：preview(tool_args) -> str（用于 inline 卡/折叠头参数预览）
    summarize: Optional[Callable] = (
        None  # 结果压缩摘要闭包：summarize(tool_name, tool_args: dict, tool_content: str) -> str（历史压缩用）
    )
    aliases: List[str] = field(default_factory=list)  # Claude Code 风格别名
    keep_in_content: bool = False  # 工具完成卡常驻消息正文（不迁入「工具与思考」折叠区）
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_plugin(self) -> bool:
        return self.source != "builtin"

    @property
    def display_group(self) -> str:
        """生效分组：显式 group 优先，否则按危险级别兜底"""
        if self.group:
            return self.group
        return GROUP_DEFAULT_DANGEROUS if self.danger == DANGER_DANGEROUS else GROUP_DEFAULT_SAFE

    @property
    def display_cn_name(self) -> str:
        return self.cn_name or self.name


def make_summarize_from_preview(preview_fn):
    """通用压缩摘要工厂：复用 preview 闭包生成「[工具名] 预览 (N chars)」"""

    def _summarize(tool_name, tool_args, tool_content):
        label = preview_fn(tool_args or {}) if preview_fn else ""
        content_len = len(tool_content or "")
        return f"[{tool_name}] {label} ({content_len:,} chars)"

    return _summarize


def _to_listener_ref(listener: Callable[[int], None]):
    """监听者引用归一化：bound method → (弱引用对象, 函数)；普通 callable 原样保留。

    bound method 每次访问都是新对象，无法直接弱引用；拆成 (obj, func) 后只弱引用
    obj——监听者对象销毁（如权限卡片窗口关闭）后引用自动失效，_notify_change 遍历
    时跳过并顺带清理，避免「已销毁 QWidget 持续接收变更回调」的 listener 泄漏
    （多窗口 × 高频热重载场景：listener 只增不删 → 变更遍历越来越慢 + 内存泄漏）。
    """
    self_ = getattr(listener, "__self__", None)
    func = getattr(listener, "__func__", None)
    if self_ is not None and func is not None:
        return (weakref.ref(self_), func)
    return listener


def _from_listener_ref(ref) -> Optional[Callable[[int], None]]:
    """解析存储的监听者引用；对象已销毁返回 None（调用方丢弃该引用）。"""
    if isinstance(ref, tuple):
        obj = ref[0]()
        if obj is None:
            return None
        return ref[1].__get__(obj)
    return ref


def _same_listener(a, b) -> bool:
    """判断两个监听者引用是否同一（用于 off_change 精确解除）。"""
    if isinstance(a, tuple) and isinstance(b, tuple):
        obj_a = a[0]()
        return obj_a is not None and obj_a is b[0]() and a[1] is b[1]
    return a is b or a == b


class ToolRegistry:
    """工具注册表（进程级单例）。version 号驱动缓存失效与热重载快照。"""

    _instance: Optional["ToolRegistry"] = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._tools: Dict[str, ToolRegistration] = {}
        self._lock = threading.RLock()
        self._version = 0
        self._listeners: List = []  # 变更监听（bound method 以 (weakref, func) 弱持有，见 _to_listener_ref）
        self._notify_suspend = 0  # 批量通知挂起计数（嵌套安全）
        self._notify_pending = False  # 挂起期间是否发生变更（恢复计数归零时补发一次）
        # schema 过滤器（owner → fn）：对话前按 owner 裁剪发给 LLM 的工具 schema。
        # fn(schemas, ctx) -> list；ctx 含 session_id。插件禁用/卸载时按 owner 清理。
        self._schema_filters: Dict[str, Callable] = {}

    # ========== 单例 ==========

    @classmethod
    def get_instance(cls) -> "ToolRegistry":
        """获取进程级单例"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（仅测试用）"""
        with cls._instance_lock:
            cls._instance = None

    # ========== 注册 / 注销 ==========

    def register(
        self,
        name: str,
        schema: Dict[str, Any],
        impl: Optional[Callable] = None,
        danger: Optional[str] = None,
        icon: str = "",
        icon_dir: str = "",
        icon_dir_light: str = "",
        cn_name: str = "",
        group: str = "",
        description: str = "",
        source: str = "builtin",
        aliases: Optional[List[str]] = None,
        team_only: bool = False,
        render: Optional[Callable] = None,
        render_mode: str = "",
        preview: Optional[Callable] = None,
        summarize: Optional[Callable] = None,
        metadata: Optional[Dict[str, Any]] = None,
        keep_in_content: bool = False,
        trusted: bool = False,
    ) -> bool:
        """注册工具。返回是否发生了变更（覆盖已存在视为变更）。

        - danger 强制声明：插件工具（source != builtin）未声明 danger 拒绝注册
        - source 白名单：builtin 源只能由内置种子流程（trusted=True）写入，
          防止插件伪装 builtin 绕过 danger 强制声明

        注意：当前全部工具经 _PluginRegistryProxy 强制 source="plugin:*"，
        source="builtin" 路径已无生产调用方（保留兼容 + 安全护栏，勿删）。
        """
        if not name or not isinstance(name, str):
            logger.warning(f"[ToolRegistry] 非法工具名: {name!r}")
            return False
        if source == "builtin" and not trusted:
            logger.warning(f"[ToolRegistry] 工具 {name} 声称 builtin 源但非可信流程，拒绝注册")
            return False
        if danger is None:
            if source != "builtin":
                logger.warning(f"[ToolRegistry] 插件工具 {name} 未声明 danger，拒绝注册（必须显式声明）")
                return False
            # builtin：按 tool_classifier 分类推断（registry 空时兜底 safe）
            danger = _classify_fallback(name)
        if danger not in (DANGER_SAFE, DANGER_DANGEROUS):
            logger.warning(f"[ToolRegistry] 工具 {name} danger 非法值 {danger!r}，拒绝注册")
            return False
        # A3：schema 规范化（warning 不拒载）——在副本上改，不污染调用方数据；
        # function.name 必须等于注册名、parameters 必须为 object、description 上限 2KB。
        safe_schema = copy.deepcopy(schema) if isinstance(schema, dict) else schema
        if isinstance(safe_schema, dict):
            fn = safe_schema.get("function")
            if isinstance(fn, dict):
                if fn.get("name") is not None and fn.get("name") != name:
                    logger.warning(
                        f"[ToolRegistry] [SchemaGuard] 工具 {name} function.name={fn.get('name')!r} "
                        f"与注册名不一致，已剔除修正为注册名"
                    )
                    fn["name"] = name
                params = fn.get("parameters")
                if not isinstance(params, dict) or params.get("type") != "object":
                    logger.warning(
                        f"[ToolRegistry] [SchemaGuard] 工具 {name} parameters 缺失或非 object，"
                        f"已剔除并置为空 object schema"
                    )
                    fn["parameters"] = {"type": "object", "properties": {}}
                desc = fn.get("description")
                if isinstance(desc, str) and len(desc) > 2048:
                    logger.warning(
                        f"[ToolRegistry] [SchemaGuard] 工具 {name} description 长度 {len(desc)} 超 2KB，已截断"
                    )
                    fn["description"] = desc[:2048]
        reg = ToolRegistration(
            name=name,
            schema=safe_schema,
            impl=impl,
            danger=danger,
            icon=icon or DEFAULT_FALLBACK_ICON,
            icon_dir=icon_dir,
            icon_dir_light=icon_dir_light,
            cn_name=cn_name,
            group=group,
            description=description,
            source=source,
            team_only=bool(team_only),
            render=render,
            render_mode=render_mode,
            preview=preview,
            summarize=summarize,
            aliases=list(aliases or []),
            keep_in_content=bool(keep_in_content),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._tools[name] = reg
            self._version += 1
        self._notify_change()
        return True

    def unregister(self, name: str) -> bool:
        """注销工具。返回是否发生了变更。"""
        with self._lock:
            if name not in self._tools:
                return False
            del self._tools[name]
            self._version += 1
        self._notify_change()
        return True

    def clear(self) -> None:
        """清空全部注册（测试用）"""
        with self._lock:
            if not self._tools:
                return
            self._tools.clear()
            self._version += 1
        self._notify_change()

    # ========== 查询 ==========

    def get(self, name: str) -> Optional[ToolRegistration]:
        with self._lock:
            return self._tools.get(name)

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._tools

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._tools.keys())

    def list(self) -> List[ToolRegistration]:
        """返回全部注册（副本，调用方可安全持有）"""
        with self._lock:
            return list(self._tools.values())

    def schemas(self) -> List[Dict[str, Any]]:
        """返回全部 schema 深拷贝（给 LLM 用）"""
        with self._lock:
            return [copy.deepcopy(r.schema) for r in self._tools.values()]

    def snapshot(self) -> Dict[str, ToolRegistration]:
        """执行快照：工具执行前取用，热重载（unregister）不影响已启动调用。"""
        with self._lock:
            return dict(self._tools)

    def version(self) -> int:
        with self._lock:
            return self._version

    # ========== 聚合查询（渲染层 / 权限层 / 别名层共用） ==========

    def get_meta(self, name: str) -> Dict[str, Any]:
        """获取单个工具的展示元数据（渲染层/权限卡片用）。未注册返回空 dict。"""
        reg = self.get(name)
        if reg is None:
            return {}
        return {
            "icon": reg.icon,
            "cn_name": reg.display_cn_name,
            "group": reg.display_group,
            "danger": reg.danger,
            "description": reg.description,
            "source": reg.source,
        }

    def get_icon(self, name: str) -> str:
        reg = self.get(name)
        return reg.icon if reg is not None else DEFAULT_FALLBACK_ICON

    def get_icon_dir(self, name: str) -> str:
        """获取工具插件的自带深色图标目录（空 = 无插件图标，渲染回退主程序资源）"""
        reg = self.get(name)
        return reg.icon_dir if reg is not None else ""

    def get_icon_dir_light(self, name: str) -> str:
        """获取工具插件的自带浅色图标目录（空 = 无浅色版，渲染回退深色/qrc）"""
        reg = self.get(name)
        return reg.icon_dir_light if reg is not None else ""

    def get_cn_name(self, name: str) -> str:
        reg = self.get(name)
        return reg.display_cn_name if reg is not None else ""

    def get_danger(self, name: str) -> str:
        """查询工具危险级别（未注册默认 safe）"""
        reg = self.get(name)
        return reg.danger if reg is not None else DANGER_SAFE

    def get_group(self, name: str) -> str:
        reg = self.get(name)
        return reg.display_group if reg is not None else GROUP_DEFAULT_SAFE

    def get_aliases(self, name: str) -> List[str]:
        reg = self.get(name)
        return list(reg.aliases) if reg is not None else []

    def is_team_only(self, name: str) -> bool:
        """工具是否团队专用（仅团队成员可见）"""
        reg = self.get(name)
        return reg.team_only if reg is not None else False

    def get_render(self, name: str):
        """获取工具完成框渲染闭包（未注册返回 None，渲染层回退默认）"""
        reg = self.get(name)
        return reg.render if reg is not None else None

    def get_render_mode(self, name: str) -> str:
        """获取工具完成框渲染模式（""=默认折叠卡 / inline=紧凑单行 / expand=无折叠展开 / none=不渲染）"""
        reg = self.get(name)
        return reg.render_mode if reg is not None else ""

    def get_preview(self, name: str):
        """获取工具自然语言预览闭包（未注册返回 None，渲染层回退 key=value 格式）"""
        reg = self.get(name)
        return reg.preview if reg is not None else None

    def get_summarize(self, name: str):
        """获取工具结果压缩摘要闭包（未注册返回 None，压缩器回退通用摘要）"""
        reg = self.get(name)
        return reg.summarize if reg is not None else None

    def is_protected(self, name: str) -> bool:
        """工具内容是否需完整保留（压缩时跳过裁剪，metadata["protect"]=True）"""
        reg = self.get(name)
        return bool(reg and reg.metadata and reg.metadata.get("protect"))

    def is_interactive(self, name: str) -> bool:
        """是否为交互式工具（UI 弹窗/人工介入，metadata["interactive"]=True）"""
        reg = self.get(name)
        return bool(reg and reg.metadata and reg.metadata.get("interactive"))

    def is_ui_managed(self, name: str) -> bool:
        """是否为专属 UI 工具（不创建通用流式块，由专属 UI 处理，metadata["ui_managed"]=True）"""
        reg = self.get(name)
        return bool(reg and reg.metadata and reg.metadata.get("ui_managed"))

    def permission_resolve_args(self, name: str, arguments: dict) -> tuple:
        """提取权限检查参数（统一消费点，避免各执行路径行为分叉）。

        返回 (mode, arg)：
        - ("task", first_agent)：metadata["permission_task"] → resolver.resolve_task(first_agent)
        - ("plain", arg)：metadata["permission_arg"] → resolver.resolve(name, arg)
        - ("plain", "")：未声明 → resolver.resolve(name)
        """
        reg = self.get(name)
        meta = (reg.metadata or {}) if reg is not None else {}
        arguments = arguments or {}
        if meta.get("permission_task"):
            tasks = arguments.get("tasks", [])
            first_agent = tasks[0].get("agent", "") if tasks and len(tasks) > 0 else ""
            return "task", first_agent
        arg_name = meta.get("permission_arg")
        if arg_name:
            return "plain", arguments.get(arg_name, "")
        return "plain", ""

    def team_only_tools(self) -> List[str]:
        """全部团队专用工具名（供 schema 过滤）"""
        with self._lock:
            return [n for n, r in self._tools.items() if r.team_only]

    def provides_image_tools(self) -> frozenset:
        """提供视觉内容（截图/图片读取）的工具名集合（metadata["provides_image"]=True）。

        供 chat_worker 视觉注入路径使用（替代硬编码 screenshot/read 判断）：
        工具结果可携带 image_data（协议 B）或可解析出本地图片路径（协议 A）时声明。
        """
        with self._lock:
            return frozenset(r.name for r in self._tools.values() if (r.metadata or {}).get("provides_image"))

    def group_map(self) -> Dict[str, List[ToolRegistration]]:
        """按展示分组聚合（权限卡片用）。保持注册顺序，组内危险工具在前。"""
        groups: Dict[str, List[ToolRegistration]] = {}
        with self._lock:
            for reg in self._tools.values():
                groups.setdefault(reg.display_group, []).append(reg)
        for tools in groups.values():
            tools.sort(key=lambda r: 0 if r.danger == DANGER_DANGEROUS else 1)
        return groups

    def tools_in_group(self, group: str) -> List[str]:
        """返回指定展示分组内的全部工具名（插件注册时声明的 group）。

        供主程序各消费点（文件写入组判定/审查白名单/统计）复用，
        避免 group 名散落为硬编码工具名集合。
        """
        with self._lock:
            return [n for n, r in self._tools.items() if r.group == group]

    def keep_in_content_tools(self) -> frozenset:
        """始终展示在正文的工具名集合（消息卡片正文/工具区分区用）。

        纯参数派生：注册时显式声明 keep_in_content=True（如 write/edit/multi_edit、
        subagent_para、question）。不做 group/语义标记隐式推断——
        语义键（interactive/subagent_task）另有消费点，借用会误触发其他流程。
        """
        with self._lock:
            return frozenset(r.name for r in self._tools.values() if r.keep_in_content)

    def dangerous_tools(self) -> List[str]:
        with self._lock:
            return [n for n, r in self._tools.items() if r.danger == DANGER_DANGEROUS]

    def safe_tools(self) -> List[str]:
        with self._lock:
            return [n for n, r in self._tools.items() if r.danger == DANGER_SAFE]

    # ========== 作用域过滤（按 agent 可用工具） ==========

    def filter_by_agent(self, agent_name: Optional[str], agent_tools: Optional[List[str]]) -> List[ToolRegistration]:
        """按 agent 作用域过滤：agent_tools 为 None 表示全量；否则仅返回白名单内工具。"""
        with self._lock:
            if agent_tools is None or not agent_tools:
                return list(self._tools.values())
            wanted = set(agent_tools)
            return [r for name, r in self._tools.items() if name in wanted]

    # ========== Schema 过滤器（对话前裁剪发给 LLM 的工具列表）==========

    def register_schema_filter(self, owner: str, fn: Callable) -> None:
        """注册 schema 过滤器（同 owner 覆盖）。

        fn(schemas: List[Dict], ctx: Dict) -> List[Dict]：返回裁剪后的 schema 列表
        （可原地过滤返回子序列，也可返回原列表表示不干预）。
        """
        if not callable(fn):
            raise TypeError("schema filter 必须可调用")
        with self._lock:
            self._schema_filters[owner] = fn

    def unregister_schema_filter(self, owner: str) -> None:
        """注销指定 owner 的过滤器（幂等；插件禁用/卸载时调用）。"""
        with self._lock:
            self._schema_filters.pop(owner, None)

    def apply_schema_filters(self, schemas: List[Dict[str, Any]], ctx: Optional[Dict[str, Any]] = None) -> List[Dict]:
        """依次应用全部过滤器；单个过滤器异常时跳过（不拖垮工具下发）。"""
        if not self._schema_filters:
            return schemas
        current = schemas
        for owner, fn in list(self._schema_filters.items()):
            try:
                result = fn(current, dict(ctx or {}))
                if isinstance(result, list):
                    current = result
            except Exception as e:
                logger.warning(f"[ToolRegistry] schema 过滤器 {owner} 执行失败，已跳过: {e}")
        return current

    # ========== 缓存失效钩子 ==========

    def on_change(self, listener: Callable[[int], None]) -> None:
        """注册变更监听（schema 缓存失效 / UI 刷新）。返回后立即以当前版本回调一次。

        bound method 监听者以弱引用持有：监听者对象销毁后引用自动失效，不会发生
        「已销毁对象持续接收回调」的泄漏；需要主动解除时调用 off_change。
        """
        with self._lock:
            self._listeners.append(_to_listener_ref(listener))
        try:
            listener(self._version)
        except Exception as e:
            logger.warning(f"[ToolRegistry] 变更监听回调失败: {e}")

    def off_change(self, listener: Callable[[int], None]) -> None:
        """解除变更监听（不存在 / 已随对象销毁自动失效时静默忽略）。"""
        target = _to_listener_ref(listener)
        with self._lock:
            self._listeners[:] = [r for r in self._listeners if not _same_listener(r, target)]

    @contextmanager
    def notify_batch(self):
        """批量变更上下文：期内多次 register/unregister 合并为一次变更通知。

        热重载全量重扫（先注销全部工具再全量重注册）会产生几十次变更，
        若逐次 notify 会让 UI 重建/缓存失效反复排队。批量上下文把通知
        压成一次：恢复计数归零时若有变更标记立即补发。可嵌套。

        用法::
            with registry.notify_batch():
                registry.register(...)
                registry.register(...)
        """
        self.suspend_notify()
        try:
            yield
        finally:
            self.resume_notify()

    def suspend_notify(self) -> None:
        """挂起变更通知（可嵌套，配合 resume_notify 使用）"""
        with self._lock:
            self._notify_suspend += 1

    def resume_notify(self) -> None:
        """恢复变更通知；挂起计数归零且期间发生过变更时补发一次。

        ★ 并发安全：flush 决策（清 pending + 版本/监听快照）与计数操作
        原子完成于锁内，回调在锁外执行——避免另一线程在回调前重新挂起
        导致"已清 pending 但通知被吞"的延迟语义错位。
        """
        with self._lock:
            if self._notify_suspend <= 0:
                return
            self._notify_suspend -= 1
            if self._notify_suspend != 0 or not self._notify_pending:
                return
            self._notify_pending = False
            version = self._version
            refs = list(self._listeners)
        self._dispatch_notify(version, refs)

    def _notify_change(self) -> None:
        with self._lock:
            if self._notify_suspend > 0:
                # 挂起中：只记标记，不发通知（resume 计数归零时补发一次）
                self._notify_pending = True
                return
            version = self._version
            refs = list(self._listeners)
        self._dispatch_notify(version, refs)

    def _dispatch_notify(self, version: int, refs: List) -> None:
        """锁外执行变更通知（回调不应持 registry 锁，避免阻塞 register/unregister）。"""
        dead_ids = set()
        for ref in refs:
            listener = _from_listener_ref(ref)
            if listener is None:
                dead_ids.add(id(ref))  # 监听者对象已销毁 → 跳过（下方统一清理）
                continue
            try:
                listener(version)
            except Exception as e:
                logger.warning(f"[ToolRegistry] 变更监听回调失败: {e}")
        # 顺带清理已销毁对象的弱引用，防止 list 无限增长（仅删除快照中已销毁者，
        # 保留期间新注册的监听者）
        if dead_ids:
            with self._lock:
                self._listeners[:] = [r for r in self._listeners if id(r) not in dead_ids]


def _classify_fallback(tool_name: str) -> str:
    """builtin 源未声明 danger 时的兜底分类（registry 空时无法查表，按危险关键词启发式）。

    [deprecated] 当前无生产调用方（全部工具经插件代理注册且强制声明 danger），
    仅保留供 builtin 种子路径/安全护栏使用。
    """
    dangerous_hint = (
        "write",
        "edit",
        "multi_edit",
        "bash",
        "exec",
        "kill",
        "remove",
        "delete",
        "upload",
        "mouse",
        "keyboard",
        "subagent_para",
        "todowrite",
        "stage_files",
        "bg_start",
        "bg_stop",
    )
    base = tool_name
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        base = parts[2] if len(parts) > 2 else tool_name
    return DANGER_DANGEROUS if base in dangerous_hint else DANGER_SAFE
