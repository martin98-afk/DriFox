# -*- coding: utf-8 -*-
"""
工具插件共享状态 — 待办列表（主程序侧：插件热重载不触碰）

task_tools.py 的 _todo_list 从插件模块级移到此处：插件文件被编辑/保存触发
watcher 全量重扫时，task_tools 模块重新 exec 但本模块不受影响，
待办状态跨热重载保留（否则编辑一次插件文件用户待办就清空）。
"""
from typing import Dict, List

# 进程级待办状态（跨窗口共享；UI 通过 ToolResult.todos 字段联动）
_todo_list: List[Dict] = []


def get_todos() -> List[Dict]:
    """获取待办（副本）"""
    return list(_todo_list)


def set_todos(todos: List[Dict]) -> List[Dict]:
    """覆盖待办，返回归一化后的列表"""
    global _todo_list
    _todo_list = list(todos)
    return list(_todo_list)
