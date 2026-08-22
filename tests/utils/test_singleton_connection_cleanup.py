# -*- coding: utf-8 -*-
"""_singleton_connections 清理骨架测试（#4.9 Commit 4 配套）"""
import gc

from app.core.tool_permission_controller import ToolPermissionController


def test_copy_state_from_does_not_copy_singleton_connections():
    """copy_state_from 必须不复制 _singleton_connections 字段（防双连接泄漏）。

    验证：
    1. 构造时 __init__ 已通过 _reg_sig 注册 ConfigSync.settingsRestored（1 条）
    2. copy_state_from 调用后，dst 的 _singleton_connections 长度不变（不复制）
    3. 不持有对源窗口 signal 的连接引用（无重复 receiver）
    """
    src = ToolPermissionController()
    dst = ToolPermissionController()
    # 构造时各持有 1 条自己的连接（ConfigSync.settingsRestored → _on_config_synced）
    src_len_before = len(src._singleton_connections)
    dst_len_before = len(dst._singleton_connections)
    assert src_len_before >= 1
    assert dst_len_before >= 1
    assert src_len_before == dst_len_before  # 同类，初始化挂载相同

    dst.copy_state_from(src)

    # ★ 关键断言：copy_state_from 不得把 src 的连接复制到 dst
    assert len(dst._singleton_connections) == dst_len_before, (
        f"copy_state_from 复制了 _singleton_connections: "
        f"dst {dst_len_before} → {len(dst._singleton_connections)}"
    )
    # src 自身不受影响
    assert len(src._singleton_connections) == src_len_before

    # 显式删除源对象 + gc.collect，验证不产生泄漏
    del src
    gc.collect()
    assert len(dst._singleton_connections) == dst_len_before