# -*- coding: utf-8 -*-
import re
import threading
from typing import Any, Dict, List, Optional

import orjson as json
from loguru import logger

# ========== consolidate_messages 脏标记缓存 ==========
_consolidate_cache_local = threading.local()


def _get_consolidate_cache() -> dict:
    cache = getattr(_consolidate_cache_local, "cache", None)
    if cache is None:
        cache = {"key": None, "result": None}
        _consolidate_cache_local.cache = cache
    return cache


def _set_consolidate_cache(list_id: int, list_len: int, result: list):
    cache = _get_consolidate_cache()
    cache["key"] = (list_id, list_len)
    cache["result"] = result


# ==========

VALID_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}

# 渲染敏感标记（按长度降序排列，避免部分匹配）
_SENSITIVE_MARKERS = [
    "",
    "</think>",
    "</tool>",
    "<tool>",
    "```",
]

# 性能优化：预编译正则表达式用于一次性替换所有敏感标记
_SANITIZE_PATTERN = re.compile('|'.join(re.escape(marker) for marker in _SENSITIVE_MARKERS))


def _sanitize_rendering_string(text: str) -> str:
    """
    清理字符串中的渲染敏感标记。
    在字符串进入渲染流程前调用，防止标记被错误解析。

    注意：只清理完整的工具块标记，不要清理参数中的子串！

    性能优化：使用预编译的正则表达式一次性替换所有标记。
    """
    if not text or not isinstance(text, str):
        return str(text) if text is not None else ""

    return _SANITIZE_PATTERN.sub("", text)


def _has_image_content(content: Any) -> bool:
    """检查内容中是否包含图片块"""
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "image_url":
                    return True
                # Anthropic 格式
                if block.get("type") == "input_image":
                    return True
                if block.get("type") == "image":
                    return True
    return False


def _extract_content_for_api(content: Any, supports_vision: bool = True) -> Any:
    """
    提取适合 API 调用的内容格式。
    纯文本返回 str，含图片块返回 list。

    Args:
        content: 消息内容
        supports_vision: 当前模型是否支持视觉输入。若为 False，
            图片块将被替换为 [图片] 文本占位符。

    Returns:
        str 或 list，保持图片块的原样传递
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # 是否有图片块？
        has_image = any(
            isinstance(b, dict) and b.get("type") in ("image_url", "input_image", "image")
            for b in content
        )
        if not has_image:
            # 纯文本块，合并为字符串
            return _extract_text_content(content)
        # 含图片块但不支持视觉 → 将图片块替换为 [图片] 文本
        if not supports_vision:
            return _strip_image_blocks(content)
        # 支持视觉，返回完整的 multimodal list
        return _clean_multimodal_blocks(content)
    return str(content)


def _strip_image_blocks(content: list) -> str:
    """将含图片块的内容列表转为纯文本，图片块替换为 [图片] 占位符。

    用于不支持视觉输入的模型：发送请求前过滤掉 image_url/input_image/image 块。
    """
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = str(block.get("text", ""))
            if text:
                parts.append(text)
        elif btype in ("image_url", "input_image", "image"):
            parts.append("[图片]")
    return "\n".join(parts) if parts else ""


def _clean_multimodal_blocks(blocks: List[Dict]) -> List[Dict]:
    """
    清理 multimodal 内容块列表，去掉空文本块。
    """
    cleaned = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = str(block.get("text", ""))
            if text:
                cleaned.append({"type": "text", "text": text})
        elif btype in ("image_url", "input_image", "image"):
            cleaned.append(dict(block))
        else:
            # 其他类型保留
            cleaned.append(dict(block))
    return cleaned


def _sanitize_tool_args(args: Any) -> Any:
    """
    递归清理工具参数中的渲染敏感标记。
    """
    if args is None:
        return {}

    if isinstance(args, dict):
        return {k: _sanitize_tool_args(v) for k, v in args.items()}

    if isinstance(args, list):
        return [_sanitize_tool_args(item) for item in args]

    if isinstance(args, str):
        return _sanitize_rendering_string(args)

    return args


def _sanitize_result(result: Any) -> str:
    """
    清理工具结果中的渲染敏感标记。
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return _sanitize_rendering_string(result)
    return str(result)


def make_text_block(text: Any) -> Dict[str, Any]:
    return {
        "type": "text",
        "text": str(text or ""),
    }


def make_tool_result_block(
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        result: Any = None,
        success: bool = True,
        tool_call_id: Optional[str] = None,
        diff: Optional[str] = None,
        echarts: Optional[str] = None,
) -> Dict[str, Any]:
    # 检测是否为子智能体任务（task tool）
    is_subagent = str(tool_name).lower() == "task"

    block = {
        "type": "tool_result",
        "name": str(tool_name or "tool"),
        "arguments": _sanitize_tool_args(arguments),
        "result": _sanitize_rendering_string("") if result is None else _sanitize_rendering_string(str(result)),
        "success": bool(success),
        "is_subagent": is_subagent,  # 标记是否为子智能体结果
    }
    if tool_call_id:
        block["tool_call_id"] = str(tool_call_id)
    if diff:
        block["diff"] = diff
    if echarts:
        block["echarts"] = echarts
    return block


def ensure_content_blocks(content: Any) -> List[Dict[str, Any]]:
    """
    将任意格式的内容转换为标准 blocks 列表。

    支持类型：text, reasoning, tool_result, image_url, input_image, image

    性能优化：简化类型检查逻辑，减少重复代码。
    """
    if content is None:
        return []

    if isinstance(content, list):
        blocks: List[Dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text":
                    text = str(item.get("text", ""))
                    if text:
                        blocks.append({"type": "text", "text": text})
                elif item_type in ("image_url", "input_image", "image"):
                    # 图片块直接透传
                    blocks.append(dict(item))
                elif item_type == "reasoning":
                    reasoning_content = str(item.get("content", "") or "")
                    blocks.append({"type": "reasoning", "content": reasoning_content})
                elif item_type == "tool_result":
                    blocks.append(
                        make_tool_result_block(
                            tool_name=item.get("name", "tool"),
                            arguments=item.get("arguments", {}),
                            result=item.get("result", ""),
                            success=item.get("success", True),
                            tool_call_id=item.get("tool_call_id"),
                            diff=item.get("diff"),
                            echarts=item.get("echarts"),
                        )
                    )
                elif item_type == "custom":
                    # 自定义内容块：{ "type": "custom", "custom_type": "<type>", "data": {...} }
                    custom_type = item.get("custom_type", "")
                    data = item.get("data", {}) or {}
                    if custom_type:
                        blocks.append({
                            "type": "custom",
                            "custom_type": custom_type,
                            "data": data,
                        })
                else:
                    # 其他类型也当作文本处理
                    text = str(item.get("text", ""))
                    if text:
                        blocks.append({"type": "text", "text": text})
            elif item is not None:
                text = str(item)
                if text:
                    blocks.append({"type": "text", "text": text})
        return blocks

    text = str(content or "")
    return [make_text_block(text)] if text else []


def build_assistant_content(
        text: Any = "",
        tool_results: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    text_value = str(text or "")
    if text_value:
        blocks.append(make_text_block(text_value))

    for item in tool_results or []:
        if not isinstance(item, dict):
            continue
        blocks.append(
            make_tool_result_block(
                tool_name=item.get("name", "tool"),
                arguments=item.get("arguments", {}),
                result=item.get("result", item.get("content", "")),
                success=item.get("success", True),
                tool_call_id=item.get("tool_call_id"),
                diff=item.get("diff"),
            )
        )

    return blocks


def append_text_block(content: Any, text: Any) -> List[Dict[str, Any]]:
    text_value = str(text or "")
    if not text_value:
        return ensure_content_blocks(content)

    # 性能优化：如果 content 已是 list 且末尾为 text block，就地追加避免重建列表
    # 流式输出时高频调用，避免每次复制全部 block
    if isinstance(content, list) and content and isinstance(content[-1], dict) and content[-1].get("type") == "text":
        content[-1]["text"] = str(content[-1].get("text", "")) + text_value
        return content

    blocks = ensure_content_blocks(content)
    blocks.append(make_text_block(text_value))
    return blocks


def content_to_text(content: Any, include_tool_results: bool = False) -> str:
    if isinstance(content, str):
        return content

    texts: List[str] = []
    for block in ensure_content_blocks(content):
        block_type = block.get("type")
        if block_type == "text":
            text = str(block.get("text", ""))
            if text:
                texts.append(text)
        elif block_type in ("image_url", "input_image", "image"):
            # 图片块转为文本占位符
            continue
        elif include_tool_results and block_type == "tool_result":
            name = str(block.get("name", "tool"))
            result = str(block.get("result", ""))
            snippet = result[:500]
            texts.append(f"[tool:{name}] {snippet}")
    return "\n\n".join(part for part in texts if part).strip()


def content_to_markdown(content: Any) -> str:
    if isinstance(content, str):
        return content

    parts: List[str] = []
    # 性能优化：content 已为 list 时跳过 ensure_content_blocks 二次拷贝
    blocks = content if isinstance(content, list) else ensure_content_blocks(content)
    for block in blocks:
        block_type = block.get("type")
        if block_type == "custom":
            # 调用注册渲染器获取 HTML
            from app.core.ui_plugin_registry import UIPluginRegistry
            custom_type = block.get("custom_type", "")
            data = block.get("data", {}) or {}
            try:
                registry = UIPluginRegistry.get_instance()
                renderer = registry.get_content_renderer(custom_type)
            except Exception:
                renderer = None
            if renderer:
                try:
                    html = renderer.render_func(data, None)
                    # 用 <div class="custom-block"> 包裹，方便后续处理
                    parts.append(
                        f'<div class="custom-block" data-type="{custom_type}">\n{html}\n</div>'
                    )
                except Exception as e:
                    from loguru import logger
                    logger.error(
                        f"[content_to_markdown] 渲染自定义块失败 {custom_type}: {e}"
                    )
                    parts.append(f"[自定义内容块 {custom_type} 渲染失败]")
            else:
                parts.append(f"[自定义内容块: {custom_type}]")
        elif block_type == "reasoning":
            # 思考内容：输出为 <think> 标签，由渲染器 _inject_think_cards 处理
            reasoning_content = str(block.get("content", "") or "")
            if reasoning_content:
                parts.append(f"<think>{reasoning_content}</think>")
        elif block_type in ("image_url", "input_image", "image"):
            # 图片块转为 markdown 图片引用
            image_url = ""
            if block_type == "image_url":
                image_data = block.get("image_url", {}) or {}
                image_url = str(image_data.get("url", ""))
            if image_url:
                if image_url.startswith("data:image"):
                    parts.append("![image](uploaded_image)")
                else:
                    parts.append(f"![image]({image_url})")
            else:
                parts.append("[图片]")
        elif block_type == "text":
            text = str(block.get("text", ""))
            if text:
                parts.append(text)
        elif block_type == "tool_result":
            # 直接从 block 中提取关键参数，避免 JSON 序列化问题
            args = block.get("arguments", {}) or {}

            # 生成安全的参数字符串表示
            if isinstance(args, dict) and args:
                # 按 value 类型排序：字符串优先显示（如 path），复杂类型（list/dict）放后面
                # 这样即使 JSON 被截断，关键短字段如 path 也不会丢失
                sorted_items = sorted(args.items(), key=lambda x: (0 if isinstance(x[1], str) else 1, len(str(x[1]))))
                args_parts = []
                for k, v in sorted_items:
                    if isinstance(v, str):
                        if len(v) > 200:
                            # 截断长字符串但保留 JSON 合法性
                            truncated = v[:200].replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
                            truncated = _sanitize_result(truncated)
                            args_parts.append(f'"{k}": "{truncated}..."')
                        else:
                            # 短字符串完整保留（如 path）
                            safe_v = v.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
                            safe_v = _sanitize_result(safe_v)
                            args_parts.append(f'"{k}": "{safe_v}"')
                    else:
                        # 非字符串类型（list/dict）：序列化后智能截断
                        try:
                            serialized = json.dumps(v).decode('utf-8')
                        except (AttributeError, TypeError):
                            serialized = str(v)
                        # question 工具的 questions 字段是核心展示数据，不截断
                        is_question_args = block.get("name") == "question" and k == "questions"
                        if len(serialized) > 300 and not is_question_args:
                            # 过长的 list/dict 只保留前100字符作为预览 + 省略标记
                            preview = serialized[:100].replace('\\', '\\\\\\').replace('"', '\\"').replace('\n', '\\n')
                            preview = _sanitize_result(preview)
                            args_parts.append(f'"{k}": "{preview}..."')
                        else:
                            args_parts.append(f'"{k}": {_sanitize_result(serialized)}')
                args_json = "{" + ", ".join(args_parts) + "}"
            else:
                args_json = "{}"

            # 处理 result：清理可能影响渲染的标签
            result_raw = str(block.get("result", ""))
            # question 工具的回答可能包含多个问题，不截断
            if block.get("name") == "question":
                result_escaped = _sanitize_result(result_raw)
            else:
                result_escaped = _sanitize_result(result_raw)[:300]

            success = bool(block.get("success", True))
            tool_call_id = block.get("tool_call_id", "")

            # 读取 diff 字段（用于 inline diff 展示）
            diff_raw = block.get("diff", "") or ""
            if diff_raw:
                # diff 多行内容，直接嵌入
                diff_escaped = _sanitize_result(str(diff_raw))

            # 读取 echarts 字段（用于 DAG 图展示）
            echarts_raw = block.get("echarts", "") or ""

            tool_lines = [
                "<tool>",
                f"name: {block.get('name', 'tool')}",
                f"args: {args_json}",
                f"result: {result_escaped}",
            ]
            if diff_raw:
                tool_lines.append("diff:")
                tool_lines.append(diff_escaped)
            tool_lines.append(f"success: {success}")
            # 保留 tool_call_id 用于差异对比功能
            if tool_call_id:
                tool_lines.append(f"tool_call_id: {tool_call_id}")
            # echarts 图表：嵌入 tool 块内部，由 _render_tool_block_content 渲染
            if echarts_raw:
                tool_lines.append("echarts:")
                tool_lines.append(echarts_raw)
            tool_lines.append("</tool>")
            parts.append("\n".join(tool_lines))
    return "\n\n".join(part for part in parts if part).strip()


def extract_tool_result_blocks(content: Any) -> List[Dict[str, Any]]:
    return [
        dict(block)
        for block in ensure_content_blocks(content)
        if block.get("type") == "tool_result"
    ]


def dedupe_tool_result_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for block in blocks or []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        key = (
            block.get("tool_call_id"),
            block.get("name"),
            json.dumps(
                block.get("arguments", {}) or {}, option=json.OPT_SORT_KEYS
            ).decode("utf-8"),
            block.get("result", ""),
            bool(block.get("success", True)),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(
            make_tool_result_block(
                tool_name=block.get("name", "tool"),
                arguments=block.get("arguments", {}),
                result=block.get("result", ""),
                success=block.get("success", True),
                tool_call_id=block.get("tool_call_id"),
                diff=block.get("diff"),
            )
        )
    return deduped


def normalize_tool_call(tool_call: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(tool_call, dict):
        return None

    function = tool_call.get("function", {}) or {}
    function_name = str(function.get("name", "") or "").strip()
    function_arguments = function.get("arguments", "{}")
    if isinstance(function_arguments, dict):
        function_arguments = json.dumps(function_arguments).decode("utf-8")
    else:
        function_arguments = str(function_arguments or "{}")

    try:
        parsed_arguments = json.loads(function_arguments)
    except Exception:
        parsed_arguments = {}

    if not isinstance(parsed_arguments, dict):
        parsed_arguments = {}

    normalized = {
        "id": str(tool_call.get("id", "") or ""),
        "type": str(tool_call.get("type", "function") or "function"),
        "function": {
            "name": function_name,
            "arguments": json.dumps(parsed_arguments).decode("utf-8"),
        },
    }
    return normalized


def normalize_message(message: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        return None

    role = str(message.get("role", "") or "").strip()
    if role not in VALID_MESSAGE_ROLES:
        return None

    normalized: Dict[str, Any] = {"role": role}

    if message.get("timestamp"):
        normalized["timestamp"] = str(message.get("timestamp"))

    if role == "assistant":
        content = content_to_text(message.get("content", ""))
        if content:
            normalized["content"] = content
        tool_calls = [
            item
            for item in (
                normalize_tool_call(tool_call)
                for tool_call in (message.get("tool_calls") or [])
            )
            if item
        ]
        if tool_calls:
            normalized["tool_calls"] = tool_calls
        # DeepSeek V4 thinking mode: 保留 reasoning_content
        reasoning = message.get("reasoning_content")
        if reasoning:
            normalized["reasoning_content"] = str(reasoning)
        if message.get("round_id"):
            normalized["round_id"] = str(message.get("round_id"))
        if message.get("model_name"):
            normalized["model_name"] = str(message.get("model_name"))
        if message.get("provider_name"):
            normalized["provider_name"] = str(message.get("provider_name"))
        if message.get("elapsed") is not None:
            normalized["elapsed"] = float(message["elapsed"])
        if isinstance(message.get("token_usage"), dict):
            normalized["token_usage"] = dict(message["token_usage"])
        # 保留 _hook_event 标记，确保能通过 save/load 持久化
        if "_hook_event" in message:
            normalized["_hook_event"] = message["_hook_event"]
        else:
            # 迁移旧数据：无 _hook_event 但内容格式匹配的 → 自动补上标记
            _fill_hook_event = _check_hook_content(normalized.get("content", ""))
            if _fill_hook_event is not None:
                normalized["_hook_event"] = _fill_hook_event
                # 用原消息更新持久化（直接回写 message 引用）
                message["_hook_event"] = _fill_hook_event

        if not normalized.get("content") and not normalized.get("tool_calls") and not normalized.get(
                "reasoning_content"):
            return None
        return normalized

    if role == "tool":
        tool_call_id = str(message.get("tool_call_id", "") or "").strip()
        if not tool_call_id:
            return None
        normalized["tool_call_id"] = tool_call_id
        normalized["content"] = content_to_text(message.get("content", ""))
        normalized["name"] = str(message.get("name", "tool") or "tool")
        normalized["arguments"] = message.get("arguments", {})
        normalized["success"] = bool(message.get("success", True))
        if message.get("round_id"):
            normalized["round_id"] = str(message.get("round_id"))
        if message.get("diff"):
            normalized["diff"] = str(message.get("diff"))
        if message.get("anchors"):
            normalized["anchors"] = str(message.get("anchors"))
        if message.get("echarts"):
            normalized["echarts"] = str(message.get("echarts"))
        return normalized

    raw_content = message.get("content", "")
    # 用户消息：如果含图片块，保留原始 list 格式；否则转为文本
    if role == "user":
        if isinstance(raw_content, list) and _has_image_content(raw_content):
            normalized["content"] = _clean_multimodal_blocks(raw_content)
        else:
            normalized["content"] = content_to_text(raw_content)
        params = message.get("params")
        normalized["params"] = dict(params) if isinstance(params, dict) else {}
    else:
        normalized["content"] = content_to_text(raw_content)

    if message.get("model_name"):
        normalized["model_name"] = str(message.get("model_name"))
    # 保留 _hook_event 标记，确保能通过 save/load 持久化
    if "_hook_event" in message:
        normalized["_hook_event"] = message["_hook_event"]
    else:
        # 迁移旧数据：为 assistant 和 user 角色的 hook 格式消息补上 _hook_event
        _fill_hook_event = _check_hook_content(normalized.get("content", ""))
        if _fill_hook_event is not None:
            normalized["_hook_event"] = _fill_hook_event
            message["_hook_event"] = _fill_hook_event
    return normalized


def consolidate_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    保持消息列表平坦，不再合并。
    每个 assistant 消息只包含自己的内容和 tool_calls。
    每个 tool 结果独立为一条 tool 消息。

    使用脏标记缓存：调用方传入同一个列表对象且长度未变时不重复计算。
    消息列表只追加（长度增加）或整体替换（id 变化），不原地修改内容，此策略安全。
    """
    # 脏标记缓存：key=(id, len)，仅对非空列表有效
    # 消息从不原地修改内容，只追加或整体替换，此策略正确
    if messages is not None:
        cache_key = (id(messages), len(messages))
        _cache = _get_consolidate_cache()
        if _cache["key"] == cache_key:
            return _cache["result"]

    normalized: List[Dict[str, Any]] = []
    for message in messages or []:
        item = normalize_message(message)
        if item:
            normalized.append(item)

    if messages is not None:
        _set_consolidate_cache(id(messages), len(messages), normalized)

    return normalized


def get_user_round_ranges(messages: List[Dict[str, Any]]) -> List[tuple[int, int]]:
    """
    计算每个 user round 的起止索引。

    Round 范围向前扩展：包含 user 消息之前的所有 hook 消息
    （PreUserMessage、PreToolUse 等除 SessionStart 外的 hook），
    删除 round 时这些 hook 会被一起删除，避免重复堆叠。

    SessionStart 视为会话级上下文，不纳入任何 round 范围，
    删除首个 round 时不会连带删除。

    关键设计：先计算每个 round 的 start（向前回溯 hook），
    再用下一个 round 的 start 作为本 round 的 end，
    避免 hook 消息同时属于两个 round。
    """
    canonical_messages = consolidate_messages(messages or [])
    user_indices = [
        idx for idx, msg in enumerate(canonical_messages) if msg.get("role") == "user"
    ]

    # 第一遍：计算每个 round 的 start
    round_starts: List[int] = []
    for pos, start_idx in enumerate(user_indices):
        # 向前回溯起点：上一个 user 之后，或会话开头
        prev_boundary = user_indices[pos - 1] + 1 if pos > 0 else 0
        round_start = start_idx
        for j in range(start_idx - 1, prev_boundary - 1, -1):
            msg = canonical_messages[j]
            if msg.get("role") == "user":
                break  # 遇到上一个 user，停止
            hook_event = msg.get("_hook_event")
            if hook_event and hook_event != "SessionStart":
                round_start = j  # 扩展起点到本 hook
            elif hook_event == "SessionStart":
                # SessionStart 是会话级，跳过不扩展
                continue
            else:
                # 非 hook 消息，停止向前回溯
                break
        round_starts.append(round_start)

    # 第二遍：end = 下一个 round 的 start（最后一个 round 则是消息总数）
    ranges: List[tuple[int, int]] = []
    for i, start in enumerate(round_starts):
        end = round_starts[i + 1] if i + 1 < len(round_starts) else len(canonical_messages)
        ranges.append((start, end))
    return ranges


# 预编译 hook 内容格式正则（模块级复用，避免重复编译）
_HOOK_CONTENT_PATTERN = re.compile(
    r'<system-reminder>\s*<([a-z0-9-]+-hook)>.*?</\1>\s*</system-reminder>',
    re.DOTALL
)


def _check_hook_content(content: Any) -> Optional[str]:
    """检查内容是否为 hook 格式，若是则返回提取的 event_name

    用于迁移旧数据：当消息没有 _hook_event 字段但内容匹配 hook 格式时，
    自动推断出 event_name 并补上 _hook_event。

    Args:
        content: 消息内容（str 或 list）

    Returns:
        event_name 字符串（如 "pre-user-message-hook"），
        或 None（不匹配 hook 格式）
    """
    if isinstance(content, str):
        m = _HOOK_CONTENT_PATTERN.search(content)
        if m:
            tag = m.group(1)  # e.g., "pre-user-message-hook"
            return tag
        return None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", ""))
                m = _HOOK_CONTENT_PATTERN.search(text)
                if m:
                    tag = m.group(1)  # e.g., "pre-user-message-hook"
                    return tag
        return None
    return None


def _is_hook_message(msg: Dict[str, Any]) -> bool:
    """判断消息是否为 hook 内部通知消息

    两层判断：
    1. 首选 _hook_event 字段（精确标记，新消息都有）
    2. 兜底内容格式匹配（<system-reminder><xxx-hook>...</xxx-hook></system-reminder>）
       用于旧数据或字段丢失的极端情况

    Returns:
        True 表示是 hook 消息，应跳过渲染
    """
    if msg.get("_hook_event"):
        return True
    # 兜底：检查内容格式
    content = msg.get("content", "")
    if isinstance(content, str) and _HOOK_CONTENT_PATTERN.search(content):
        return True
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", ""))
                if _HOOK_CONTENT_PATTERN.search(text):
                    return True
    return False


def group_messages_for_display(
        messages: List[Dict[str, Any]],
) -> List[List[Dict[str, Any]]]:
    canonical_messages = consolidate_messages(messages or [])
    batches: List[List[Dict[str, Any]]] = []
    current_batch: List[Dict[str, Any]] = []

    for msg in canonical_messages:
        role = msg.get("role")
        if role == "system":
            continue
        # 跳过 hook 内部通知消息（如 SessionStart、PostToolUse 等），
        # 不显示为消息卡片。
        if _is_hook_message(msg):
            continue
        if role == "user":
            if current_batch:
                batches.append(current_batch)
                current_batch = []
            batches.append([msg])
            continue
        current_batch.append(msg)

    if current_batch:
        batches.append(current_batch)
    return batches


def to_api_message(message: Dict[str, Any], supports_vision: bool = True) -> Dict[str, Any]:
    """
    将内部消息格式转换为标准API请求格式。
    用于发送给API的消息构建。

    Args:
        message: 内部消息字典
        supports_vision: 当前模型是否支持视觉输入。若为 False，
            图片块将被替换为 [图片] 文本占位符，避免不支持视觉的模型报 400 错误。

    支持 multimodal 内容（含 image_url 块的列表）。
    """
    normalized_message = normalize_message(message)
    if not normalized_message:
        return {}

    role = normalized_message.get("role")
    if role == "system":
        return {
            "role": "system",
            "content": _extract_text_content(normalized_message.get("content", "")),
        }
    elif role == "user":
        raw_content = normalized_message.get("content", "")
        api_content = _extract_content_for_api(raw_content, supports_vision=supports_vision)
        api_msg = {
            "role": "user",
            "content": api_content,
        }
        # 如果 content 是 str 且有 params → 拼合上下文
        params = normalized_message.get("params", {})
        if params and isinstance(api_content, str):
            context_parts = [
                str(value[1])
                for value in params.values()
                if isinstance(value, (list, tuple)) and len(value) > 1
            ]
            combined = "\n\n".join(part for part in context_parts if part)
            if combined:
                api_msg["content"] = (
                    combined + "\n\n" + api_content
                    if api_content
                    else combined
                )
        return api_msg
    elif role == "assistant":
        api_msg: Dict[str, Any] = {
            "role": "assistant",
        }
        text = _extract_text_content(normalized_message.get("content", ""))
        if text:
            api_msg["content"] = text
        tool_calls = normalized_message.get("tool_calls")
        if tool_calls:
            api_msg["tool_calls"] = tool_calls
        # DeepSeek V4 thinking mode: 传递 reasoning_content
        reasoning = normalized_message.get("reasoning_content")
        if reasoning:
            api_msg["reasoning_content"] = reasoning
        # 确保 content 或 tool_calls 存在，避免 API 报 "content or tool_calls must be set"
        if "content" not in api_msg and "tool_calls" not in api_msg:
            api_msg["content"] = ""
        return api_msg
    elif role == "tool":
        raw_content = normalized_message.get("content", "")
        # tool 结果支持 multimodal（如图片 base64 描述）
        tool_content = _extract_content_for_api(raw_content)
        # tool 消息的 content 必须是字符串（OpenAI 协议要求）
        # 如果有 image_url 块，将其转换为文本描述
        if isinstance(tool_content, list):
            tool_text_parts = []
            for block in tool_content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        tool_text_parts.append(str(block.get("text", "")))
                    elif block.get("type") == "image_url":
                        # tool 结果中的图片转为文本描述
                        tool_text_parts.append("[Image: base64 data]")
            tool_content = "\n".join(tool_text_parts)
        return {
            "role": "tool",
            "tool_call_id": str(normalized_message.get("tool_call_id", "")),
            "name": str(normalized_message.get("name", "")),
            "content": str(tool_content or ""),
        }
    return {
        "role": role,
        "content": _extract_text_content(normalized_message.get("content", "")),
    }


def messages_to_api(messages: List[Dict[str, Any]], supports_vision: bool = True) -> List[Dict[str, Any]]:
    """将内部消息列表转换为标准API请求格式列表。

    Args:
        messages: 内部消息列表
        supports_vision: 当前模型是否支持视觉输入。若为 False，
            图片块将被替换为 [图片] 文本占位符。
    """
    api_messages: List[Dict[str, Any]] = []
    for message in messages:
        api_message = to_api_message(message, supports_vision=supports_vision)
        if api_message:
            if api_message.get("role") == "user" and not api_message.get("content"):
                continue
            api_messages.append(api_message)
    return api_messages


def _extract_text_content(content: Any) -> str:
    """从复杂内容中提取纯文本"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    txt = str(block.get("text", ""))
                    if txt:
                        parts.append(txt)
        return " ".join(parts)
    return str(content)
