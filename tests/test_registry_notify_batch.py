# -*- coding: utf-8 -*-
"""registry.notify_batch 批量通知合并测试（热重载全量重扫通知风暴优化）"""

import pytest

from app.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def fresh_registry():
    """每个测试前重置 registry（测试用）"""
    ToolRegistry.reset_instance()
    yield
    ToolRegistry.reset_instance()


def _register(reg: ToolRegistry, name: str):
    reg.register(
        name,
        {"type": "function", "function": {"name": name}},
        danger="safe",
        source="plugin:test",
    )


class TestNotifyBatch:
    """通知合并语义"""

    def test_batch_merges_multiple_changes_to_one_notify(self):
        reg = ToolRegistry.get_instance()
        notified = []
        reg.on_change(lambda v: notified.append(v))
        notified.clear()  # on_change 注册时立即回调一次，清掉

        with reg.notify_batch():
            for i in range(10):
                _register(reg, f"tool_{i}")

        # 批内 10 次注册只触发一次通知，且 version 为最终值
        assert len(notified) == 1
        assert notified[0] == reg.version()

    def test_nested_batch_flushes_only_on_outer_exit(self):
        reg = ToolRegistry.get_instance()
        notified = []
        reg.on_change(lambda v: notified.append(v))
        notified.clear()

        with reg.notify_batch():
            _register(reg, "inner_1")
            with reg.notify_batch():
                _register(reg, "inner_2")
            # 出内层 batch：不 flush
            assert notified == []
        # 出外层 batch：flush 一次
        assert len(notified) == 1

    def test_batch_flushes_on_exception(self):
        reg = ToolRegistry.get_instance()
        notified = []
        reg.on_change(lambda v: notified.append(v))
        notified.clear()

        with pytest.raises(RuntimeError):
            with reg.notify_batch():
                _register(reg, "boom")
                raise RuntimeError("boom")
        # finally 语义：异常路径仍补发一次通知
        assert len(notified) == 1

    def test_batch_without_changes_does_not_notify(self):
        reg = ToolRegistry.get_instance()
        notified = []
        reg.on_change(lambda v: notified.append(v))
        notified.clear()

        with reg.notify_batch():
            pass

        assert notified == []

    def test_normal_path_notify_unchanged(self):
        """非挂起路径：每次变更照常通知，且清理已销毁监听者的弱引用"""
        reg = ToolRegistry.get_instance()
        notified = []
        reg.on_change(lambda v: notified.append(v))
        notified.clear()

        _register(reg, "a")
        assert notified == [1]
        _register(reg, "b")
        assert notified == [1, 2]

        # bound method 监听者对象销毁后，不再收到通知（弱引用 + 顺带清理）
        class _Listener:
            def on_registry_changed(self, v):
                notified.append(v)

        listener = _Listener()
        reg.on_change(listener.on_registry_changed)  # 注册时立即回调一次 version=2
        _register(reg, "c")  # 两个监听者各收一次 version=3
        assert notified == [1, 2, 2, 3, 3]
        del listener
        import gc

        gc.collect()
        _register(reg, "d")
        assert notified == [1, 2, 2, 3, 3, 4]  # 仅 lambda 收到；bound 对象已销毁
        assert len(reg._listeners) == 1  # 死引用已从监听列表清理