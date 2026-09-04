# -*- coding: utf-8 -*-
"""
系统工具插件 — 工具搜索与中转执行（schema 懒加载）

tool_search：按关键词/精确名搜索全部已注册工具（含被 schema 档位裁剪的与
MCP 工具），返回完整定义（name + description + parameters）。

tool_execute：中转执行搜到的工具。为什么需要：API 层只允许模型调用本次
请求 tools 列表里的工具，未注入 schema 的工具模型发不出调用（实测确认），
必须经本工具中转。执行时走 registry impl / MCP call_tool_sync，与直接
调用同一条分发链；全局 deny 策略的工具中转也拒绝（安全网保留）。

对齐 CodeBuddy ToolSearchTool（queries 全文搜索 + tool_names 精确查找 +
top_k 截断）；其中转执行即 CodeBuddy 的 DeferExecuteTool。
"""
import json

from app.tools.result import ToolResult
from app.tools.registry import ToolRegistry

GROUP_TOOL_SEARCH = "工具搜索"

# 输出字符上限（对齐 CodeBuddy 默认 30k，DriFox schema 体量取 20k 足够）
_MAX_OUTPUT_CHARS = 20000
_SELF_NAME = "tool_search"


def _as_str_list(v) -> list:
    """LLM 可能传单个字符串或 JSON 字符串，统一成 list[str]。"""
    if v is None:
        return []
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return []
        if v.startswith("["):
            try:
                v = json.loads(v)
            except Exception:
                return [v]
        return [v] if isinstance(v, str) else [str(x) for x in v]
    return [str(x) for x in v if str(x).strip()]


def _clamp_top_k(v) -> int:
    try:
        k = int(v)
    except (TypeError, ValueError):
        return 5
    return max(1, min(20, k))


def _score_fields(fields: list, query: str) -> int:
    """对单个工具的字段集合按 query 打分（子串匹配，大小写不敏感）。"""
    ql = query.lower().strip()
    if not ql:
        return 0
    best = 0
    for kind, text in fields:
        t = (text or "").lower()
        if not t:
            continue
        if kind == "name":
            best = max(best, 100 if t == ql else (80 if ql in t else 0))
        elif kind == "alias":
            best = max(best, 85 if t == ql else (70 if ql in t else 0))
        elif kind == "cn":
            best = max(best, 60 if ql in t else 0)
        elif kind == "group":
            best = max(best, 40 if ql in t else 0)
        elif kind == "desc":
            best = max(best, 20 if ql in t else 0)
    return best


def _best_score(fields: list, queries: list) -> int:
    """多 query 取最大；单 query 内空格分词 AND（全部词命中才计分）。"""
    best = 0
    for q in queries:
        tokens = [t for t in q.lower().split() if t]
        if not tokens:
            continue
        scores = [_score_fields(fields, t) for t in tokens]
        if all(s > 0 for s in scores):
            best = max(best, min(scores))
    return best


def _search_registry(queries: list) -> list:
    """搜索 registry 工具，返回 [(score, schema)] 按 score 降序。"""
    scored = []
    for r in ToolRegistry.get_instance().list():
        if r.name in (_SELF_NAME, "tool_execute"):
            continue
        fields = [
            ("name", r.name),
            ("alias", r.name.replace("_", " ")),
            ("cn", r.cn_name),
            ("group", r.group),
            ("desc", r.description),
        ] + [("alias", a) for a in (r.aliases or [])]
        s = _best_score(fields, queries)
        if s > 0:
            scored.append((s, r.schema))
    scored.sort(key=lambda t: -t[0])
    return scored


def _search_mcp(services: dict, queries: list) -> list:
    """搜索 MCP 工具 schema（services["mcp"] 不可用时静默为空）。"""
    mcp = services.get("mcp") if isinstance(services, dict) else None
    if mcp is None:
        return []
    try:
        schemas = mcp.get_tool_schemas() or []
    except Exception:
        return []
    scored = []
    for schema in schemas:
        fn = schema.get("function", {}) or {}
        name = fn.get("name", "")
        if not name:
            continue
        bare = name.split("__", 2)[-1] if name.startswith("mcp__") else name
        fields = [("name", name), ("name", bare), ("desc", fn.get("description", ""))]
        s = _best_score(fields, queries)
        if s > 0:
            scored.append((s, schema))
    scored.sort(key=lambda t: -t[0])
    return scored


def _fname(schema: dict) -> str:
    return (schema.get("function", {}) or {}).get("name", "")


def _format_entry(schema: dict) -> list:
    fn = schema.get("function", {}) or {}
    lines = [f"## {fn.get('name', '?')}", (fn.get("description") or "（无描述）").strip()]
    params = fn.get("parameters")
    if params:
        lines += ["", "Parameters:", "```json", json.dumps(params, ensure_ascii=False, indent=2), "```"]
    return lines


def _search_impl(tool_ctx=None, **kw):
    queries = _as_str_list(kw.get("queries"))
    tool_names = _as_str_list(kw.get("tool_names"))
    top_k = _clamp_top_k(kw.get("top_k"))
    if not queries and not tool_names:
        return ToolResult(
            True,
            content=(
                '请提供搜索条件：queries（关键词数组，中英文均可，如 ["文件搜索", "file search"]）'
                '或 tool_names（精确工具名数组）。'
            ),
        )
    services = (tool_ctx or {}).get("services") or {}
    results: list = []
    candidates: list = []

    # 精确查找优先（registry → 别名 → MCP）
    if tool_names:
        reg = ToolRegistry.get_instance()
        mcp_all = _search_mcp(services, [""])
        mcp_by_name = {_fname(s): s for s in mcp_all}
        for raw in tool_names:
            name = raw.strip()
            if not name:
                continue
            hit = None
            if name != _SELF_NAME and reg.has(name):
                hit = reg.get(name).schema
            else:
                for target in reg.list():
                    if name.lower() in [a.lower() for a in (target.aliases or [])]:
                        hit = target.schema
                        break
            if hit is None:
                hit = mcp_by_name.get(name)
            if hit is not None and _fname(hit) not in {_fname(x) for x in results}:
                results.append(hit)
                continue
            # 精确未命中 → 单条搜索兜底
            fallback = (_search_registry([name]) + _search_mcp(services, [name]))[:1]
            if fallback and _fname(fallback[0][1]) not in {_fname(x) for x in results}:
                results.append(fallback[0][1])
            else:
                candidates.append(name)

    # 关键词搜索
    if queries:
        scored = _search_registry(queries) + _search_mcp(services, queries)
        have = {_fname(x) for x in results}
        matched = []
        for _s, schema in scored:
            fname = _fname(schema)
            if fname == _SELF_NAME or fname in have:
                continue
            have.add(fname)
            matched.append(schema)
        results.extend(matched[:top_k])
        candidates.extend(_fname(x) for x in matched[top_k:])

    if not results:
        return ToolResult(
            True,
            content="没有找到匹配的工具。换关键词试试（中英文各写一份效果更好），或改用 tool_names 精确查找。",
        )

    lines: list = [f"找到 {len(results)} 个工具（完整定义如下；当前工具列表里没有它们，请用 tool_execute(tool_name=工具名, arguments=参数对象) 中转调用）："]
    for schema in results:
        lines += ["", *_format_entry(schema)]
    shown = [c for c in candidates if c][:10]
    if shown:
        lines += [
            "",
            f"其他候选（共 {len(candidates)} 个，可用 tool_names 获取完整定义）：",
            *[f"- {c}" for c in shown],
        ]
    content = "\n".join(lines)
    if len(content) > _MAX_OUTPUT_CHARS:
        content = content[:_MAX_OUTPUT_CHARS] + "\n... [输出超长已截断，请用更精确的 queries 或 tool_names 缩小范围]"
    return ToolResult(True, content=content)


def _truncate(content: str) -> str:
    if len(content) <= _MAX_OUTPUT_CHARS:
        return content
    return content[:_MAX_OUTPUT_CHARS] + "\n... [输出超长已截断]"


def _execute_impl(tool_ctx=None, **kw):
    """中转执行：调用 tool_search 搜到但未注入 schema 的工具。"""
    import inspect

    name = str(kw.get("tool_name") or "").strip()
    args = kw.get("arguments")
    if args is None or args == "":
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except Exception:
            return ToolResult(False, error="arguments 必须是 JSON 对象（或对象字符串）")
    if not isinstance(args, dict):
        return ToolResult(False, error="arguments 必须是对象（键值对形式）")
    if not name:
        return ToolResult(True, content="请提供 tool_name（精确工具名）与 arguments（参数对象，无参传 {}）。")
    if name in (_SELF_NAME, "tool_execute"):
        return ToolResult(True, content="不能中转调用元工具自身。")

    services = (tool_ctx or {}).get("services") or {}

    # 安全网：用户在权限卡片显式关掉的工具（toggles=False 且 deny 策略），中转也拒绝
    try:
        from app.core.tool_permission_controller import resolve_tool_off_policy
        from app.utils.config import Settings

        settings = Settings.get_instance()
        toggles = dict(settings.tool_toggles.value or {})
        if toggles.get(name, True) is False:
            policies = dict(settings.tool_permission_policy.value or {})
            behavior = settings.tool_off_behavior.value or "deny"
            if resolve_tool_off_policy(name, None, policies, behavior) == "deny":
                return ToolResult(True, content=f"工具 {name} 已被用户禁用（deny 策略），不能中转执行。")
    except Exception:
        pass  # 检查失败不阻塞执行（与执行层兜底口径一致）

    reg = ToolRegistry.get_instance()
    if reg.has(name):
        entry = reg.get(name)
        impl = entry.impl
        if impl is None:
            return ToolResult(False, error=f"工具 {name} 没有可执行的实现。")
        try:
            has_ctx = "tool_ctx" in inspect.signature(impl).parameters
            result = impl(tool_ctx=tool_ctx, **args) if has_ctx else impl(**args)
        except TypeError as e:
            return ToolResult(False, error=f"参数不匹配：{e}。请用 tool_search 核对该工具的 Parameters 定义。")
        except Exception as e:
            return ToolResult(False, error=f"工具 {name} 执行失败: {e}")
    elif name.startswith("mcp__"):
        mcp = services.get("mcp")
        if mcp is None:
            return ToolResult(False, error="MCP 服务不可用，无法中转执行 MCP 工具。")
        result = mcp.call_tool_sync(name, args)
    else:
        return ToolResult(
            True,
            content=f"没有找到工具 {name}。先用 tool_search 搜索（queries 关键词或 tool_names 精确名）确认工具存在及参数。",
        )

    if isinstance(result, ToolResult):
        if result.success and isinstance(result.content, str):
            result.content = _truncate(result.content)
        return result
    return ToolResult(True, content=_truncate(str(result)))


_EXECUTE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tool_execute",
        "description": (
            "中转执行工具：调用通过 tool_search 搜到、但不在你当前工具列表里的工具。"
            "API 只允许调用本次请求 tools 列表里的工具，所以未注入 schema 的工具必须经本工具中转。"
            "用法：tool_name 给精确工具名（含别名、MCP 全名 mcp__服务__工具），"
            "arguments 给参数对象（按 tool_search 返回的 Parameters 传，无参传 {}）。"
            "已在工具列表里的工具直接调用，不要经本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "description": "要执行的工具名（精确名，含别名；MCP 用全名）"},
                "arguments": {"type": "object", "description": "工具参数对象（按其 schema 的 Parameters 传，无参传 {}）"},
            },
            "required": ["tool_name"],
        },
    },
}


_SCHEMA = {
    "type": "function",
    "function": {
        "name": _SELF_NAME,
        "description": (
            "按名称或描述搜索可用工具，返回工具的完整定义（schema）。"
            "搜到后用 tool_execute(tool_name, arguments) 调用。"
            "当需要的能力不在你当前的工具列表里（被权限档位裁剪、或 MCP 工具未列出）时，"
            "先用本工具搜索：queries 给中英文关键词，tool_names 给精确工具名。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关键词数组（建议中英文各写一份，如 [\"文件搜索\", \"file search\"]）；多词任一命中即返回",
                },
                "tool_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "精确工具名数组（含别名；MCP 用全名 mcp__服务__工具）",
                },
                "top_k": {"type": "integer", "description": "返回完整定义的数量上限（默认 5，最大 20）"},
            },
        },
    },
}


def register(registry):
    """工具插件化注册入口（PluginToolLoader 调用）"""
    registry.register(
        _SELF_NAME,
        _SCHEMA,
        impl=_search_impl,
        danger="safe",
        icon="search",
        cn_name="搜索工具",
        group=GROUP_TOOL_SEARCH,
        description="搜索可用工具并返回完整 schema（含被档位裁剪的与 MCP 工具）",
        aliases=["工具搜索", "搜索工具", "tool search"],
    )
    registry.register(
        "tool_execute",
        _EXECUTE_SCHEMA,
        impl=_execute_impl,
        danger="safe",
        icon="工具",
        cn_name="调用工具",
        group=GROUP_TOOL_SEARCH,
        description="中转执行 tool_search 搜到的工具（未注入 schema 的工具经此调用）",
        aliases=["调用工具", "中转执行", "call tool"],
    )
