# -*- coding: utf-8 -*-
"""能力目录 —— 插件能力池的唯一真相。

新增能力必须在此登记；未登记的能力只能在注册时传 experimental=True
（见 registry.register），并会在每次构建池时记 warning。

约束：
- ID 形如 ``<group>.<name>``，group 必须等于首段
- level 是审核语义，不参与运行时裁剪
- surfaces 声明该能力在哪些上下文可用（ui = 浮动卡/工作台页，tool = 工具插件）
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Optional, Set

from app.plugins.contracts.capabilities import CapabilityLevel, CapabilitySpec

_L, _M, _H, _N = (
    CapabilityLevel.LOW,
    CapabilityLevel.MEDIUM,
    CapabilityLevel.HIGH,
    CapabilityLevel.NONE,
)
_UI: FrozenSet[str] = frozenset({"ui"})
_TOOL: FrozenSet[str] = frozenset({"tool"})
_BOTH: FrozenSet[str] = frozenset({"ui", "tool"})


def _spec(
    cid: str,
    level: CapabilityLevel,
    summary: str,
    surfaces: FrozenSet[str],
    legacy: Iterable[str] = (),
) -> CapabilitySpec:
    return CapabilitySpec(
        id=cid,
        group=cid.split(".")[0],
        level=level,
        summary=summary,
        surfaces=surfaces,
        legacy_keys=tuple(legacy),
    )


CATALOG: Dict[str, CapabilitySpec] = {
    s.id: s
    for s in (
        # ── 模型 ──
        _spec("model.metadata", _L, "当前模型的 provider/名称/上下文长度/能力元数据", _BOTH),
        _spec("model.schema", _L, "指定 agent 视角的工具 schema", _BOTH, ["get_tools_schema"]),
        _spec(
            "model.config",
            _H,
            "完整模型配置（含已解密明文 API key）",
            _BOTH,
            ["get_model_config", "get_provider_config"],
        ),
        _spec(
            "provider.list",
            _L,
            "已配置服务商清单（id/名称/模型数，不含密钥与 URL）",
            _BOTH,
        ),
        _spec(
            "model.enumerate",
            _L,
            "指定服务商的可选模型列表（含视觉/思考能力标记）",
            _BOTH,
        ),
        # ── 会话 ──
        _spec("session.read", _M, "当前 session_id 与消息列表", _BOTH),
        _spec("session.write", _M, "消息写回当前会话（入库 + 刷 UI + 发事件）", _BOTH, ["save_messages_to_session"]),
        _spec("session.list", _M, "历史会话列表与元数据", _UI),
        _spec("session.manage", _H, "新建/切换/删除会话（破坏性）", _UI),
        _spec(
            "session.invalidate",
            _M,
            "失效所有窗口的会话 system_prompt 缓存（跨窗口副作用）",
            _UI,
        ),
        # ── 输入框 ──
        _spec("input.read", _M, "输入框文本与附件列表", _UI),
        _spec("input.write", _M, "写入输入框文本", _UI),
        # ── 智能体 ──
        _spec("agent.read", _L, "agent 列表/提示词/管理器", _BOTH, ["get_agent_manager", "get_agent_prompt"]),
        _spec("subagent.run", _H, "派生子智能体", _TOOL, ["subagent"]),
        # ── 工作目录 ──
        _spec("workdir.read", _L, "当前工作目录", _BOTH, ["get_workdir"]),
        _spec(
            "workdir.write",
            _M,
            "切换工作目录（缓存 + executor + 团队广播 + 事件）",
            _UI,
            ["set_workdir", "sync_working_directory"],
        ),
        # ── 外观 ──
        _spec("theme.read", _L, "主题 id/名称/配色/字体/字号", _UI),
        # ── 上下文 ──
        _spec("context.compact", _M, "上下文压缩器", _BOTH, ["get_compactor"]),
        # ── 工具 ──
        _spec("tool.read", _L, "工具注册表元数据查询", _BOTH),
        _spec("tool.inject", _H, "向 LLM 注入工具（改变模型可见行为）", _UI),
        _spec("tool.execute", _H, "执行任意工具", _BOTH, ["get_tool_executor"]),
        # ── 对话引擎 ──
        _spec("engine.session", _H, "创建对话引擎会话（消耗 token）", _UI, ["create_engine_session"]),
        _spec("engine.stack", _H, "对话执行栈工厂", _UI, ["conversation_stack"]),
        # ── 通讯平台 ──
        _spec("platform.send", _H, "向通讯平台发送消息（数据外发）", _UI, ["send_to_platform"]),
        _spec("platform.read", _M, "平台与会话列表", _UI, ["list_platform_sessions", "list_platforms"]),
        # ── 内部资源 ──
        _spec("mcp.call", _H, "MCP 工具调用", _TOOL, ["mcp"]),
        _spec("lsp.query", _M, "LSP 查询", _TOOL, ["lsp"]),
        _spec("net.upload", _H, "Gitee 上传（数据外发）", _TOOL, ["gitee"]),
        _spec("net.download", _M, "从 Gitee 绑定仓库下载文件到本地路径", _BOTH),
        _spec(
            "net.avatar",
            _L,
            "Gitee 账户头像（fetch 确保已缓存并返回路径，须后台线程；cached_path 纯本地查，主线程安全）",
            _UI,
        ),

        # ── 界面 ──
        _spec("ui.notify", _L, "弹 InfoBar 通知", _UI, ["notify"]),
        _spec("ui.card.show", _L, "显示浮动卡", _UI),
        _spec("ui.card.hide", _L, "隐藏浮动卡", _UI, ["hide_card"]),
        _spec("ui.exclusive", _M, "独占 UI 模式", _UI, ["enter_exclusive_ui_mode", "exit_exclusive_ui_mode"]),
        _spec("ui.dialog", _L, "文件/目录选择、确认框、单行输入（跟随主题）", _UI),
        _spec("ui.events", _L, "订阅/发布 UI 事件总线（主题切换/欢迎页刷新等）", _UI),
        _spec(
            "ui.artifacts.show",
            _M,
            "向消息卡片成果栏推送成果条目（图片/视频/HTML/文本/文件，跨窗口副作用）",
            _BOTH,
        ),
        # ── 对话流 ──
        _spec("stream.subscribe", _M, "订阅对话流事件（工具/流/错误/上下文/会话切换）", _UI),
        # ── 窗口 ──
        _spec("window.active", _L, "当前活跃聊天窗口", _UI),
        _spec("window.current", _L, "当前窗口的语义快照（backend/session/project/workdir）", _UI),
        _spec("window.host", _L, "宿主顶层窗口（弹窗/InfoBar 的合适 parent）", _UI),
        _spec("window.list", _L, "存活窗口 id 集合", _UI),
        _spec("window.events", _M, "订阅 tab 切换等窗口级事件", _UI),
        _spec("window.workbench", _M, "打开/刷新右侧工作台页", _UI),
        # ── 插件管理 ──
        _spec("plugin.read", _L, "已安装插件列表/元数据", _UI),
        _spec("plugin.control", _H, "重载/启停其他插件", _UI),
        # ── 命令 ──
        _spec("command.read", _L, "命令列表", _UI),
        _spec("command.reload", _M, "命令热重载", _UI),
        # ── 同步 ──
        _spec(
            "sync.register",
            _H,
            "注册本地目录进入云同步监控（目录内容将打包备份到云端，数据外发）",
            _UI,
        ),
        # ── 团队 ──
        _spec("team.read", _L, "团队成员/角色/运行状态", _BOTH),
        _spec("team.send", _M, "向团队成员派发任务邮件（写对方邮箱）", _TOOL),
        # ── 存储 ──
        _spec("storage.data_dir", _L, "插件持久化数据根目录（插件自拼 plugin_data/<name>/）", _BOTH),
        # ── 基础（恒放行） ──
        _spec("plugin.log", _N, "带 plugin=<name> 前缀的 logger", _BOTH),
        _spec("state.kv", _N, "窗口级 KV 存储（无系统权限）", _BOTH, ["window_state"]),
        # ── 进程级 ──
        _spec("cache.purge", _L, "清理主程序进程内 LRU 缓存（内存释放）", _UI),
        _spec("process.start", _H, "启动后台进程（argv 数组、无 shell；独立于宿主生命周期）", _BOTH),
        # ── 配置（按 key 读非敏感标量，不给整个 Settings 对象） ──
        _spec(
            "config.read",
            _M,
            "按 key 读主程序配置项（非敏感标量；不含密钥，密钥走 model.config）",
            _UI,
            ["get_setting"],
        ),
        # ── 逃生舱 ──
        _spec("host.raw", _H, "裸 main_widget / backend 对象引用", _UI),
    )
}

# 旧键 → 能力 ID（ctx["services"] / tool_ctx["services"] 的投影依据）
LEGACY_MAP: Dict[str, str] = {}
for _sid, _s in CATALOG.items():
    for _legacy in _s.legacy_keys:
        LEGACY_MAP[_legacy] = _sid

# 存量 manifest 里可能出现的扁平能力名（A3 时代的写法）→ 命名空间 ID
ALIASES: Dict[str, str] = {
    "lsp": "lsp.query",
    "mcp": "mcp.call",
    "gitee": "net.upload",
    "subagent": "subagent.run",
    "window_state": "state.kv",
    "notify": "ui.notify",
    "hide_card": "ui.card.hide",
}

# ui 投影可用的旧键
UI_LEGACY_KEYS = tuple(k for k, v in LEGACY_MAP.items() if "ui" in CATALOG[v].surfaces)


def get(cap_id: str) -> Optional[CapabilitySpec]:
    """按 ID 取能力定义；未登记返回 None。"""
    return CATALOG.get(cap_id)


def groups() -> Set[str]:
    return {s.group for s in CATALOG.values()}


def in_group(group: str) -> Set[str]:
    return {cid for cid, s in CATALOG.items() if s.group == group}


def expand(ids: Iterable[str]) -> FrozenSet[str]:
    """把 manifest 声明展开为能力 ID 集合（处理别名与通配）。

    未知 ID 原样保留（注册表里没有对应 provider，构建池时自然不可用）。
    """
    out: Set[str] = set()
    for raw in ids:
        token = str(raw).strip()
        if not token:
            continue
        token = ALIASES.get(token, token)
        if token == "*":
            out.update(CATALOG)
        elif token.endswith(".*"):
            out.update(in_group(token[:-2]))
        else:
            out.add(token)
    return frozenset(out)


def unknown_declared(raw: object) -> List[str]:
    """manifest capabilities 声明里的未知项（加载期语义校验用）。

    expand 对未知 ID 静默保留、池构建时不匹配任何 provider，插件侧表现为
    拿不到能力却看不到原因（typo 如 ``session.Read`` 最典型）。本函数把
    这类声明在加载期挑出来，由 PluginManager 记入 manifest_warnings。

    判定口径与 expand 严格一致：``*`` 合法；``group.*`` 检查 group 存在；
    其余 token 过 ALIASES 后必须命中 CATALOG。
    """
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        token = str(item).strip()
        if not token or token == "*":
            continue
        token = ALIASES.get(token, token)
        if token.endswith(".*"):
            if token[:-2] not in groups():
                out.append(token)
        elif token not in CATALOG:
            out.append(token)
    return out
