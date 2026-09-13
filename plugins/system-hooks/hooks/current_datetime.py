# -*- coding: utf-8 -*-
"""
PreUserMessage Hook 函数 — 向 LLM 上下文注入当前系统时间

所有 hook 数据由 backend 预取后通过 context 传入。

此函数替代了原 command 类型（powershell date 命令），
可同步执行，确保 hook 输出在 user 消息之前就已注入上下文。
"""

from datetime import datetime


def hook(event: str, context: dict) -> str:
    """返回当前系统时间字符串

    Args:
        event: 事件名称（PreUserMessage）
        context: 由 backend 预取的上下文，含：
            - message: str 当前用户消息内容
            - project_root: str 当前窗口工作目录

    Returns:
        格式化的系统时间字符串
    """
    now = datetime.now()
    return now.strftime('%Y-%m-%d %H:%M:%S')
