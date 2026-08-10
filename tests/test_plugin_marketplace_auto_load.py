# -*- coding: utf-8 -*-
"""plugin-marketplace 自动加载回归测试

覆盖场景：
- 打开插件市场卡片后，worker 线程快速完成（缓存命中）并立即被
  deleteLater 销毁时，market_fetched 信号携带的线上插件数据必须
  被合并进 _plugin_data（不能因 sender() 解析为 None 被丢弃）。
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


def _wait_until(pred, timeout=5.0, interval=0.02):
    """轮询等待谓词成立（pump 事件循环）"""
    deadline = time.time() + timeout
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        if pred():
            return True
        time.sleep(interval)
    return False


def _make_fake_source():
    return {
        "name": "fake-market",
        "source": {"source": "url", "url": "https://example.invalid/marketplace.json"},
        "auto_update": True,
        "builtin": True,
    }


def _make_fake_market_data():
    return {
        "name": "fake-market",
        "description": "fake",
        "plugins": [
            {"name": "fake-plugin-a", "version": "1.0.0", "description": "a"},
            {"name": "fake-plugin-b", "version": "2.0.0", "description": "b"},
        ],
    }


def test_open_card_auto_loads_remote_plugins(monkeypatch):
    """打开卡片自动拉取：worker 快速完成 + deleteLater 销毁后，线上插件仍须进入 _plugin_data"""
    from ui.cards import MarketplaceCard
    from ui.marketplace_manager import MarketplaceSourceManager

    # 固定市场源 + 固定拉取结果（模拟缓存命中：瞬时返回，不等网络）
    monkeypatch.setattr(MarketplaceSourceManager, "get_sources", lambda self: [_make_fake_source()])
    monkeypatch.setattr(
        MarketplaceSourceManager,
        "fetch_marketplace",
        lambda self, src, force=False: _make_fake_market_data(),
    )

    card = MarketplaceCard()
    card.show_card()  # 触发 show_card → _load_timer → _start_load → _async_refresh

    # 等待 worker 完成且数据合并（修复前：数据被 sender 检查丢弃，永远为 0）
    ok = _wait_until(lambda: len(card._plugin_data) > 0)
    assert ok, (
        "打开插件市场后线上插件数据未进入 _plugin_data（worker 快速完成时 sender() 解析为 None 被误判为旧 worker 丢弃）"
    )

    names = {p.get("name") for p in card._plugin_data}
    assert {"fake-plugin-a", "fake-plugin-b"} <= names, f"线上插件缺失，实际: {sorted(names)}"


def test_refresh_force_also_loads(monkeypatch):
    """手动刷新（force=True）路径同样能合并数据"""
    from ui.cards import MarketplaceCard
    from ui.marketplace_manager import MarketplaceSourceManager

    monkeypatch.setattr(MarketplaceSourceManager, "get_sources", lambda self: [_make_fake_source()])
    monkeypatch.setattr(
        MarketplaceSourceManager,
        "fetch_marketplace",
        lambda self, src, force=False: _make_fake_market_data(),
    )

    card = MarketplaceCard()
    card.show_card()

    ok = _wait_until(lambda: len(card._plugin_data) > 0)
    assert ok, "打开后自动加载失败"

    # 再次手动刷新（force=True），worker 重建，数据应再次合并且不丢
    card._on_refresh()
    ok = _wait_until(lambda: len(card._plugin_data) >= 2)
    assert ok, "手动刷新后线上插件数据丢失"

    card._worker_thread.quit()
