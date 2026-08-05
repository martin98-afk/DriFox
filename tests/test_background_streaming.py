# -*- coding: utf-8 -*-
"""BackgroundWorkerManager 单例和基本单元测试

⚠️ 遗留测试：引用的 app.core.workers.background_worker_manager 模块
从未存在于代码库（git 全历史无该模块文件，现有 9 个 worker 模块
chat_worker/subagent_worker/auto_loop_worker 等均无对应 API）。
T4-prep 已将其列为既有遗留 collection 错误（88F 基线之一）。
无法映射到现有模块 → 标记 skip，保留测试意图供未来功能实现时恢复。
"""

import pytest

pytest.importorskip("app.core.workers.background_worker_manager")

from app.core.workers.background_worker_manager import (  # noqa: E402,F401
    BackgroundWorkerManager,
    BackgroundWorkerRecord,
)


class TestBackgroundWorkerManager:
    """BackgroundWorkerManager 基本行为测试"""

    def test_singleton(self):
        """验证单例模式"""
        m1 = BackgroundWorkerManager.get_instance()
        m2 = BackgroundWorkerManager.get_instance()
        assert m1 is m2

    def test_no_active_workers_initially(self):
        """初始状态无活跃后台 worker"""
        m = BackgroundWorkerManager.get_instance()
        assert not m.has_active_workers()

    def test_streaming_sessions_empty_initially(self):
        """初始状态无流式会话"""
        m = BackgroundWorkerManager.get_instance()
        assert m.get_streaming_sessions() == []

    def test_get_record_none_for_unknown(self):
        """不存在的 session_id 返回 None"""
        m = BackgroundWorkerManager.get_instance()
        assert m.get_record("nonexistent") is None
