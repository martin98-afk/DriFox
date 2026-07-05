# -*- coding: utf-8 -*-
"""
Stop Hook 函数 — 在会话结束前让 AI 清理临时文件

行为：
- 正常完成（reason=completed）且为首次 Stop → 返回 block + 清理提示
- 二次 Stop（stop_hook_active=true，续命后的结束）→ 放过，不注入
- 取消/异常场景（reason=cancelled/error）→ 放过，不注入

避免在非续命场景下向消息列表注入无实际作用的提示词。
"""

import json


def hook(event: str, context: dict) -> str:
    """Stop 事件 hook：首次正常结束时 block 并要求 AI 清理垃圾文件

    Args:
        event: 事件名称（Stop）
        context: 上下文，含：
            - stop_hook_active: bool 续命标记
            - reason: str 停止原因（completed/cancelled/error）
            - last_assistant_message: str 最后一条 assistant 消息

    Returns:
        JSON 字符串，含 decision 和 output 字段
    """
    reason = context.get("reason", "")
    stop_hook_active = context.get("stop_hook_active", False)

    # 仅对正常完成 + 首次 Stop 做 block 续命清理
    if reason == "completed" and not stop_hook_active:
        return json.dumps(
            {
                "decision": "block",
                "output": (
                    "请清理本次会话中创建的所有临时文件和垃圾文件"
                    "（如下载的文件、生成的脚本、测试输出等），"
                    "使用 bash 工具删除它们。清理完成后回复「已完成清理」。"
                ),
            },
            ensure_ascii=False,
        )

    # 其他情况（二次 Stop / 取消 / 异常）：不注入任何内容
    return ""
