# -*- coding: utf-8 -*-
"""
PostToolUse Hook 函数 — 自动上下文压缩触发器

监测每次工具调用后的对话上下文使用比例，
当超过设定阈值时返回 JSON 触发信号，
由 tool_executor 检查后自动触发 /compact --clear。

支持通过 function_args 传递自定义参数：
    threshold: 触发比例 (0.0 ~ 1.0)，默认 0.80（80%）
"""

import json as _json


def hook(event: str, context: dict, threshold: float = 0.80) -> str:
    """检查上下文使用比例，超过阈值时返回自动压缩触发信号

    Args:
        event: 事件名称（PostToolUse）
        context: 由 tool_executor 预取的上下文，含：
            - token_count: int 当前对话已占用的 token 数
            - token_limit: int 当前模型的最大上下文窗口限制
            - token_ratio: float 当前使用比例 (token_count / token_limit)
            - tool_name: str 当前执行的工具名
            - tool_name_native: str 工具原生名
            - 以及其他 PostToolUse 标准字段
        threshold: 触发比例阈值（默认 0.80），可通过 function_args 覆盖

    Returns:
        JSON 字符串：
            {"auto_compact": true, "ratio": 0.85}
            或
            {"auto_compact": false, "ratio": 0.45, "reason": "未超过阈值"}
    """
    token_count = context.get("token_count", 0)
    token_limit = context.get("token_limit", 0)

    if token_count <= 0 or token_limit <= 0:
        return _json.dumps(
            {
                "auto_compact": False,
                "ratio": 0.0,
                "reason": "无法获取上下文使用量信息",
            }
        )

    ratio = token_count / token_limit

    if ratio >= threshold:
        return _json.dumps(
            {
                "auto_compact": True,
                "ratio": round(ratio, 4),
                "reason": f"上下文使用 {ratio:.1%} 超过阈值 {threshold:.0%}，触发自动压缩",
            }
        )

    return _json.dumps(
        {
            "auto_compact": False,
            "ratio": round(ratio, 4),
            "reason": f"上下文使用 {ratio:.1%} 未超过阈值 {threshold:.0%}",
        }
    )
