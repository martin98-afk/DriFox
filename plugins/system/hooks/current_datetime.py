# -*- coding: utf-8 -*-
"""
PostUserMessage Hook 函数 — 向当前用户消息注入当前系统时间

所有 hook 数据由 backend 预取后通过 context 传入。

此函数替代了原 command 类型（powershell date 命令），
可同步执行，确保 PostUserMessage 在 worker 启动前完成并注入队列。
"""

from datetime import datetime


def hook(event: str, context: dict) -> str:
    """返回当前系统时间字符串

    Args:
        event: 事件名称（PostUserMessage）
        context: 由 backend 预取的上下文，含：
            - message: str 当前用户消息内容
            - project_root: str 当前窗口工作目录

    Returns:
        格式化的系统时间字符串
    """
    now = datetime.now()
    return f"当前系统时间：{now.strftime('%Y-%m-%d %H:%M:%S')}"
