# -*- coding: utf-8 -*-
"""
OAuth 抽象层测试

覆盖:
  - get_oauth_backend 工厂函数
  - OAuthBackend 基本接口约束
  - GiteeOAuthBackend 实现

Run: pytest tests/gateway/test_auth_abstraction.py -v
"""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("app.gateway.auth")
pytest.importorskip("app.gateway.auth.base")
pytest.importorskip("app.gateway.auth.gitee")


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def _mock_cfg(monkeypatch):
    """mock Settings — 默认未绑定，用于工厂测试"""
    cfg = MagicMock()
    cfg.gitee_bound = MagicMock()
    cfg.gitee_bound.value = False

    # gitee.is_bound() 通过 app.utils.config import Settings 读取
    monkeypatch.setattr(
        "app.utils.config.Settings.get_instance",
        lambda: cfg,
    )
    yield cfg


# =============================================================================
# 1. 工厂函数
# =============================================================================


class TestGetOAuthBackend:
    """get_oauth_backend 工厂"""

    def test_returns_gitee_by_name(self):
        """指定 name='gitee' 应返回 GiteeOAuthBackend"""
        from app.gateway.auth import get_oauth_backend
        from app.gateway.auth.gitee import GiteeOAuthBackend

        backend = get_oauth_backend("gitee")
        assert isinstance(backend, GiteeOAuthBackend)
        assert backend.name == "gitee"

    def test_raises_for_unknown_name(self):
        """不存在的平台名应抛出 ValueError"""
        from app.gateway.auth import get_oauth_backend

        with pytest.raises(ValueError, match="未知的 OAuth 平台"):
            get_oauth_backend("unknown_platform")

    def test_returns_first_bound_backend(self, _mock_cfg):
        """name='' 时遍历已注册平台，返回第一个 is_bound()==True 的后端"""
        from app.gateway.auth import get_oauth_backend
        from app.gateway.auth.gitee import GiteeOAuthBackend

        _mock_cfg.gitee_bound.value = True
        backend = get_oauth_backend()
        assert isinstance(backend, GiteeOAuthBackend)
        assert backend.name == "gitee"

    def test_raises_when_nothing_bound(self, _mock_cfg):
        """未绑定任何平台时应抛出 ValueError"""
        from app.gateway.auth import get_oauth_backend

        _mock_cfg.gitee_bound.value = False
        with pytest.raises(ValueError, match="未绑定"):
            get_oauth_backend()
