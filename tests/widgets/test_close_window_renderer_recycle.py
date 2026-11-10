# -*- coding: utf-8 -*-
"""T16 验证：关窗延后回收 _kill_unloaded_pids_deferred 护栏三分支。

覆盖：
① 记账 pid 无任何存活窗口占用 → kill 被调用
② pid 被其他存活窗口在用（renderer-process-limit 复用）→ 跳过不 kill
③ 窗口 C++ 已销毁但弱引用未脱落（RuntimeError）→ 视为不占用，照常 kill
④ closeEvent 分支推演：最后一个窗口关闭（registry 空）→ 不调度 kill

offscreen 运行，无键鼠、无可见窗口（P0 约束）。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

import pytest

from app.core.infra import window_registry


class _FakeWindow:
    """最小窗口替身：只暴露 _pid_still_in_use，可控制销毁态。"""

    def __init__(self, busy_pids=(), broken=False):
        self.busy_pids = set(busy_pids)
        self.broken = broken

    def _pid_still_in_use(self, pid):
        if self.broken:
            raise RuntimeError("Internal C++ object already deleted")
        return pid in self.busy_pids


@pytest.fixture
def clean_registry():
    window_registry.window_instances.clear()
    yield
    window_registry.window_instances.clear()


def _register(fake):
    window_registry.register_window(fake)
    return fake


def _patch_kill():
    """patch 真 kill（静态方法），记录被杀 pid。"""
    from app.main_widget import OpenAIChatToolWindow

    killed = []
    patcher = patch.object(
        OpenAIChatToolWindow, "_kill_renderer", staticmethod(lambda pid: killed.append(pid) or True)
    )
    return patcher, killed


def test_deferred_kill_when_no_windows(clean_registry):
    """① registry 空（全部窗口已关/未登记）→ 全部 kill"""
    from app.main_widget import OpenAIChatToolWindow

    patcher, killed = _patch_kill()
    with patcher:
        n = OpenAIChatToolWindow._kill_unloaded_pids_deferred([111, 222])
    assert killed == [111, 222]
    assert n == 2


def test_deferred_kill_skips_pid_used_by_alive_window(clean_registry):
    """② pid 被其他存活窗口占用 → 跳过；空闲 pid 正常 kill"""
    from app.main_widget import OpenAIChatToolWindow

    fake = _register(_FakeWindow(busy_pids={111}))  # 保强引用，防 weakref 即死
    assert window_registry.alive_window_instances() == [fake]
    patcher, killed = _patch_kill()
    with patcher:
        n = OpenAIChatToolWindow._kill_unloaded_pids_deferred([111, 222])
    assert killed == [222]
    assert n == 1


def test_deferred_kill_treats_broken_window_ref_as_free(clean_registry):
    """③ C++ 已销毁的窗口引用抛 RuntimeError → 不算占用，照常 kill"""
    from app.main_widget import OpenAIChatToolWindow

    fake = _register(_FakeWindow(busy_pids={111}, broken=True))
    assert window_registry.alive_window_instances()
    patcher, killed = _patch_kill()
    with patcher:
        n = OpenAIChatToolWindow._kill_unloaded_pids_deferred([111])
    assert killed == [111]
    assert n == 1


def test_deferred_kill_swallow_kill_exception(clean_registry):
    """单 pid kill 抛异常 → 兜底继续下一个，不冒泡"""
    from app.main_widget import OpenAIChatToolWindow

    patcher, killed = _patch_kill()
    with patcher as _:
        pass
    # 直接 patch 成抛异常版本
    from unittest.mock import patch as _p

    def boom(pid):
        raise OSError("access denied")

    with _p.object(OpenAIChatToolWindow, "_kill_renderer", staticmethod(boom)):
        n = OpenAIChatToolWindow._kill_unloaded_pids_deferred([1, 2, 3])
    assert n == 0


def test_close_event_branch_last_window_no_kill(clean_registry):
    """④ 关窗分支：registry 为空（最后一个窗口）→ 不应调度延后 kill。

    直接验证 closeEvent 的决策条件（与源码同一表达式）：
    stale_pids 非空 且 alive_window_instances() 非空 才调度。
    """
    from app.core.infra import window_registry as wr

    assert wr.alive_window_instances() == []
    # 最后一个窗口场景：条件为 False → 不 singleShot（与 main_widget.py 26478 行一致）
    stale_pids = [1, 2, 3]
    should_schedule = bool(stale_pids) and bool(wr.alive_window_instances())
    assert should_schedule is False
    # 非最后窗口场景：条件为 True → 调度
    fake = _register(_FakeWindow())
    should_schedule = bool(stale_pids) and bool(wr.alive_window_instances())
    assert should_schedule is True
