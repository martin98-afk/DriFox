# -*- coding: utf-8 -*-
"""#37 条 3（分支 B）：ConfigSync 启动链自动启用 / 幂等 / 卡片零线程。

核实结论：全仓 enable() 曾唯一由 GiteeCard/GiteeAccountRow._auto_enable_sync
调用，而卡片仅点设置时构造 → 已配置用户冷启动不点设置永不自动同步（缺陷）。
修法：main.py 首帧后 8s singleShot 调 auto_enable_if_bound；卡片共用该函数
幂等（_state 判重），构造期不再独自 enable。
"""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.sync import config_sync as cs


@pytest.fixture()
def fake_sync(monkeypatch):
    """隔离 ConfigSyncService 单例：桩化 _state / enable"""
    svc = SimpleNamespace(_state="disabled", enable=MagicMock())
    monkeypatch.setattr(cs.ConfigSyncService, "get_instance", staticmethod(lambda: svc))
    return svc


def test_bound_user_enables_on_startup(fake_sync, monkeypatch):
    """已配置用户：启动链调用 → enable(token, owner) 一次"""
    bound = {"token": "tk-123", "owner": "alice"}
    get_info = MagicMock(return_value=bound)
    monkeypatch.setattr("app.core.auth.get_oauth_backend", lambda p: SimpleNamespace(get_bound_info=get_info))
    assert cs.auto_enable_if_bound() is True
    fake_sync.enable.assert_called_once_with("tk-123", "alice")


def test_already_enabled_is_idempotent(fake_sync, monkeypatch):
    """幂等：_state 非 disabled → 不重复 enable、连 get_bound_info 都不调"""
    fake_sync._state = "idle"
    get_backend = MagicMock()
    monkeypatch.setattr("app.core.auth.get_oauth_backend", get_backend)
    assert cs.auto_enable_if_bound() is False
    get_backend.assert_not_called()
    fake_sync.enable.assert_not_called()


def test_unbound_user_zero_network(fake_sync, monkeypatch):
    """未配置用户：get_bound_info 首行守卫返回 None → 零 enable（零网络）"""
    get_info = MagicMock(return_value=None)
    monkeypatch.setattr("app.core.auth.get_oauth_backend", lambda p: SimpleNamespace(get_bound_info=get_info))
    assert cs.auto_enable_if_bound() is False
    get_info.assert_called_once()
    fake_sync.enable.assert_not_called()


def test_startup_hook_wired_in_main():
    """main.py 启动链挂点守卫：8s singleShot + auto_enable_if_bound"""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent.parent / "main.py").read_text(encoding="utf-8-sig")
    assert "QTimer.singleShot(8000, _auto_start_config_sync)" in src, "首帧后 8s 自动启用挂点缺失"
    assert "auto_enable_if_bound()" in src, "挂点必须调幂等公共函数"


def test_card_auto_enable_uses_shared_idempotent_entry():
    """卡片两类 _auto_enable_sync 必须走公共函数（构造/点设置不独自拉线程）"""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent.parent
        / "app"
        / "widgets"
        / "cards"
        / "settings"
        / "gitee_card.py"
    ).read_text(encoding="utf-8-sig")
    assert src.count("auto_enable_if_bound(self._sync_svc)") == 2, "两类 _auto_enable_sync 各经公共入口一次（传桩兼容实例）"
    assert "defer_ms" not in src, "60s 缓启降级方案已作废（分支 B）"
    assert "._sync_svc.enable(" not in src, "卡片不得再独自调 enable（双拉线程风险）"


def test_startup_hook_exception_is_nonfatal(fake_sync, monkeypatch):
    """token 后端异常：静默返 False，绝不阻塞启动链"""
    def _boom(p):
        raise RuntimeError("oauth backend unavailable")

    monkeypatch.setattr("app.core.auth.get_oauth_backend", _boom)
    assert cs.auto_enable_if_bound() is False
    fake_sync.enable.assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
