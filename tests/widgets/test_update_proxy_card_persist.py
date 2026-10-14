# -*- coding: utf-8 -*-
"""更新下载代理设置卡：写入必须落盘（回归测试）

背景（2026-10-14）：卡片三处写入曾用 `item.value = x` 直接赋值。qfluentwidgets
的 ConfigItem.value setter 只 emit valueChanged 信号、从不写盘；项目覆写的
`Settings.set` 又把 save 默认改成 False。结果内存生效、磁盘不变，重启后代理
方式回退。本测试锁住「卡片交互必须走 cfg.set(save=True) 落盘」。

隔离：conftest 全局 DRIFOX_DATA_DIR 已指向 tests/.drifox-test，Settings 落盘
文件即该目录下 app.config，无需额外隔离。
"""

import json
from pathlib import Path

import pytest

from app.utils import update_proxy
from app.utils.config import Settings
from app.widgets.cards.settings.update_proxy_card import UpdateProxyCard


@pytest.fixture
def card(qapp):
    """干净的设置卡：预重置三个配置项并强制落盘，避免前序状态串扰"""
    cfg = Settings.get_instance()
    cfg.set(cfg.update_proxy_mode, update_proxy.MODE_DIRECT, save=True)
    cfg.set(cfg.update_proxy_prefix, "https://ghfast.top/", save=True)
    cfg.set(cfg.update_proxy_url, "", save=True)
    return UpdateProxyCard()


def _disk_update_section() -> dict:
    cfg = Settings.get_instance()
    data = json.loads(Path(cfg.file).read_text(encoding="utf-8"))
    return data.get("Update", {})


def test_mode_selection_persists(card):
    """点选模式行后 ProxyMode 必须出现在磁盘配置里"""
    card._on_row_selected(update_proxy.MODE_HTTP)
    assert card.cfg.update_proxy_mode.value == update_proxy.MODE_HTTP
    assert _disk_update_section()["ProxyMode"] == update_proxy.MODE_HTTP

    card._on_row_selected(update_proxy.MODE_SYSTEM)
    assert _disk_update_section()["ProxyMode"] == update_proxy.MODE_SYSTEM


def test_http_addr_commit_persists(card):
    """手动代理地址提交后 ProxyUrl 必须落盘"""
    row = card._rows[update_proxy.MODE_HTTP]
    row.edit.setText("http://127.0.0.1:7890")
    card._on_addr_committed(update_proxy.MODE_HTTP)
    assert _disk_update_section()["ProxyUrl"] == "http://127.0.0.1:7890"


def test_addr_clear_persists_empty(card):
    """清空地址提交后 ProxyUrl 必须落盘为空串（同样不允许只改内存）"""
    row = card._rows[update_proxy.MODE_HTTP]
    row.edit.setText("http://127.0.0.1:7890")
    card._on_addr_committed(update_proxy.MODE_HTTP)

    row.edit.setText("")
    card._on_addr_committed(update_proxy.MODE_HTTP)
    assert _disk_update_section()["ProxyUrl"] == ""


def test_invalid_addr_not_persisted(card):
    """非法地址被拦截：磁盘保持旧值"""
    row = card._rows[update_proxy.MODE_HTTP]
    row.edit.setText("http://127.0.0.1")  # 缺端口，校验必拒
    card._on_addr_committed(update_proxy.MODE_HTTP)
    assert _disk_update_section().get("ProxyUrl", "") != "http://127.0.0.1"
