# -*- coding: utf-8 -*-
"""MCP token 估算常驻 worker 回归测试（工单 F37 Step2）

锁定：
1. 单飞：密集触发（_token_calc_running 置位期）只入队一次、只算一次；
2. 类级 worker 复用：卡片重建（新实例触发）不重建线程，同一线程服务多卡；
3. sip 存活硬判：卡片 sip.delete 后残留 job 被跳过，无异常逃逸、无泄漏 emit；
4. finally 复位：正常路径 _token_calc_running 复位，deleted 卡不炸 worker。
"""

import os
import sys
import time
import weakref

import shiboken6 as sip
from PySide6.QtWidgets import QApplication, QWidget

from app.widgets.cards.settings.mcp_setting_card import MCPListSettingCard


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _FakeTokenCard(QWidget):
    """真实 QWidget（sip.isdeleted 可硬判）+ 动态挂计算依赖（mock 隔离重活）"""

    def __init__(self):
        super().__init__()
        from unittest.mock import MagicMock

        self._tokenCountReady = MagicMock()
        self._token_calc_running = False
        self._get_servers = MagicMock(return_value=[{"name": "a", "enabled": True}])
        self._get_mcp_manager = MagicMock(return_value=None)
        self.cfg = MagicMock()
        self.cfg.mcp_enabled.value = False


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_single_flight_dedup_and_finally_reset():
    """①入队置位 → 防重入生效 → worker 完成 emit 一次并复位标志"""
    _ensure_qapp()
    card = _FakeTokenCard()
    cls = MCPListSettingCard

    MCPListSettingCard._update_mcp_token_count(card)
    assert card._token_calc_running is True  # 入队即置位
    # 置位期密集触发：直接 return，不再入队
    MCPListSettingCard._update_mcp_token_count(card)
    MCPListSettingCard._update_mcp_token_count(card)
    assert card._get_servers.call_count == 0  # 主线程零计算（重活在 worker）

    ok = _wait_until(lambda: card._tokenCountReady.emit.call_count >= 1)
    assert ok, "worker 应完成计算并 emit"
    assert card._token_calc_running is False  # finally 复位
    args = card._tokenCountReady.emit.call_args
    assert args[0][0] == "1/1 · ~0 tokens"
    # 等第二轮不再追加（单飞：重复触发未入队）
    time.sleep(0.1)
    assert card._tokenCountReady.emit.call_count == 1


def test_worker_thread_reused_across_cards():
    """②新实例触发 → 同一线程对象服务（不重建，无泄漏）"""
    _ensure_qapp()
    cls = MCPListSettingCard
    card1 = _FakeTokenCard()
    MCPListSettingCard._update_mcp_token_count(card1)
    assert _wait_until(lambda: cls._token_worker_thread is not None and cls._token_worker_thread.is_alive())
    thread1 = cls._token_worker_thread

    assert _wait_until(lambda: card1._token_calc_running is False)

    card2 = _FakeTokenCard()
    MCPListSettingCard._update_mcp_token_count(card2)
    assert cls._token_worker_thread is thread1, "卡片重建不得重建 worker 线程"
    assert _wait_until(lambda: card2._tokenCountReady.emit.call_count == 1)
    assert card2._token_calc_running is False


def test_deleted_card_job_skipped_and_worker_survives():
    """③sip.delete 后残留 job 被硬判跳过：无异常逃逸、无 emit、worker 仍活"""
    _ensure_qapp()
    cls = MCPListSettingCard
    card = _FakeTokenCard()
    MCPListSettingCard._update_mcp_token_count(card)  # 确保 worker 已起
    assert _wait_until(lambda: cls._token_worker_thread is not None and cls._token_worker_thread.is_alive())
    assert _wait_until(lambda: card._token_calc_running is False)

    doomed = _FakeTokenCard()
    ref = weakref.ref(doomed)
    doomed._token_calc_running = True  # 模拟「job 在途时卡片被销毁」
    sip.delete(doomed)
    # 直接向队列投递已销毁卡的 job（绕过 _update_mcp_token_count 的入队路径）
    cls._token_calc_queue.put_nowait(ref)
    time.sleep(0.3)

    assert cls._token_worker_thread.is_alive(), "worker 不得被 deleted 卡击穿退出"
    assert doomed._tokenCountReady.emit.call_count == 0  # 空壳不得 emit
    assert ref() is not None  # wrapper 仍在（sip.delete 语义），仅 C++ 析构
    # worker 可继续服务后续正常卡
    alive_card = _FakeTokenCard()
    MCPListSettingCard._update_mcp_token_count(alive_card)
    assert _wait_until(lambda: alive_card._tokenCountReady.emit.call_count == 1)
