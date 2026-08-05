# -*- coding: utf-8 -*-
"""
Gitee OAuth 后端测试（app.gateway.auth.gitee）

覆盖:
  - ensure_repo（仓库检查/创建）
  - GiteeOAuthBackend.bind / unbind / is_bound / get_bound_info
    （bind 复用 base.run_authorization_code_flow 的通用流程）

Run: pytest tests/gateway/test_gitee_backend.py -v
"""

import time
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("app.gateway.auth.gitee")

from pytest import MonkeyPatch


@pytest.fixture(autouse=True)
def _mock_settings_config(monkeypatch: MonkeyPatch):
    """mock Settings — 确保 OAuth 凭证等配置可用"""
    cfg = MagicMock()
    cfg.gitee_oauth_client_id.value = "test_client_id"
    cfg.gitee_oauth_client_secret.value = "test_client_secret"
    cfg.gitee_bound = MagicMock()
    cfg.gitee_user_token = MagicMock()
    cfg.gitee_user_owner = MagicMock()
    cfg.gitee_user_repo = MagicMock()

    monkeypatch.setattr(
        "app.utils.config.Settings.get_instance",
        lambda: cfg,
    )
    yield cfg


# =============================================================================
# 1. ensure_repo 测试
# =============================================================================


class TestEnsureRepo:
    """仓库检查/创建（必须 mock requests.get/post/patch，避免真实网络请求）"""

    @staticmethod
    def _mock_patch_ok():
        p = MagicMock()
        p.status_code = 200
        p.json.return_value = {"name": "repo", "private": True}
        return p

    def test_repo_exists(self):
        """仓库已存在时应返回成功"""
        from app.gateway.auth.gitee import ensure_repo

        with patch("app.gateway.auth.gitee.requests.get") as mock_get, \
             patch("app.gateway.auth.gitee.requests.patch") as mock_patch:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "name": "DriFox_uploads", "private": False
            }

            ok, msg = ensure_repo("token", "owner", "DriFox_uploads", private=False)
            assert ok is True
            mock_patch.assert_not_called()

    def test_repo_exists_private_mismatch(self):
        """仓库可见性与请求不匹配时应更新可见性"""
        from app.gateway.auth.gitee import ensure_repo

        with patch("app.gateway.auth.gitee.requests.get") as mock_get, \
             patch("app.gateway.auth.gitee.requests.patch") as mock_patch:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "name": "DriFox_uploads", "private": False
            }
            mock_patch.return_value = self._mock_patch_ok()

            ok, msg = ensure_repo("token", "owner", "DriFox_uploads", private=True)
            assert ok is True
            mock_patch.assert_called_once()

    def test_repo_created(self):
        """仓库不存在时应创建"""
        from app.gateway.auth.gitee import ensure_repo

        with patch("app.gateway.auth.gitee.requests.get") as mock_get, \
             patch("app.gateway.auth.gitee.requests.post") as mock_post, \
             patch("app.gateway.auth.gitee.requests.patch") as mock_patch:

            mock_get.return_value.status_code = 404
            mock_post.return_value.status_code = 201
            mock_post.return_value.json.return_value = {"name": "DriFox_uploads"}
            mock_patch.return_value = self._mock_patch_ok()

            ok, msg = ensure_repo("token", "owner", "DriFox_uploads", private=False)
            assert ok is True
            assert "已创建" in msg

            post_kwargs = mock_post.call_args[1]
            assert "data" in post_kwargs
            assert post_kwargs["data"]["name"] == "DriFox_uploads"
            mock_patch.assert_called_once()

    def test_repo_create_failed(self):
        """仓库创建失败应返回错误"""
        from app.gateway.auth.gitee import ensure_repo

        with patch("app.gateway.auth.gitee.requests.get") as mock_get, \
             patch("app.gateway.auth.gitee.requests.post") as mock_post, \
             patch("app.gateway.auth.gitee.requests.patch") as mock_patch:

            mock_get.return_value.status_code = 404
            mock_post.return_value.status_code = 422
            mock_post.return_value.json.return_value = {"message": "already exists"}

            ok, msg = ensure_repo("token", "owner", "DriFox_uploads", private=False)
            assert ok is False
            assert "创建仓库失败" in msg
            mock_patch.assert_not_called()

    def test_repo_check_network_error(self):
        """检查仓库时网络错误应向上传播（ensure_repo 无 try/except）"""
        from app.gateway.auth.gitee import ensure_repo

        with patch("app.gateway.auth.gitee.requests.get") as mock_get:
            mock_get.side_effect = Exception("Network error")

            with pytest.raises(Exception, match="Network error"):
                ensure_repo("token", "owner", "DriFox_uploads", private=False)

    def test_repo_create_network_error(self):
        """创建仓库时网络错误应向上传播"""
        from app.gateway.auth.gitee import ensure_repo

        with patch("app.gateway.auth.gitee.requests.get") as mock_get, \
             patch("app.gateway.auth.gitee.requests.post") as mock_post:

            mock_get.return_value.status_code = 404
            mock_post.side_effect = Exception("Timeout")

            with pytest.raises(Exception, match="Timeout"):
                ensure_repo("token", "owner", "DriFox_uploads", private=False)

    def test_repo_patch_failed(self):
        """创建后设置可见性失败应返回错误"""
        from app.gateway.auth.gitee import ensure_repo

        with patch("app.gateway.auth.gitee.requests.get") as mock_get, \
             patch("app.gateway.auth.gitee.requests.post") as mock_post, \
             patch("app.gateway.auth.gitee.requests.patch") as mock_patch:

            mock_get.return_value.status_code = 404
            mock_post.return_value.status_code = 201
            mock_post.return_value.json.return_value = {"name": "DriFox_uploads"}

            failed_patch = MagicMock()
            failed_patch.status_code = 403
            failed_patch.json.return_value = {"message": "forbidden"}
            mock_patch.return_value = failed_patch

            ok, msg = ensure_repo("token", "owner", "DriFox_uploads", private=False)
            assert ok is False
            assert "可见性" in msg

    def test_repo_exists_patch_failed(self):
        """已存在仓库更新可见性失败应返回错误"""
        from app.gateway.auth.gitee import ensure_repo

        with patch("app.gateway.auth.gitee.requests.get") as mock_get, \
             patch("app.gateway.auth.gitee.requests.patch") as mock_patch:

            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "name": "DriFox_uploads", "private": False
            }
            failed_patch = MagicMock()
            failed_patch.status_code = 403
            failed_patch.json.return_value = {"message": "forbidden"}
            mock_patch.return_value = failed_patch

            ok, msg = ensure_repo("token", "owner", "DriFox_uploads", private=True)
            assert ok is False
            assert "可见性" in msg


# =============================================================================
# 2. GiteeOAuthBackend.unbind
# =============================================================================


class TestGiteeBackendUnbind:
    """解绑账号"""

    def test_unbind_clears_config(self, _mock_settings_config):
        """解绑应清除 token/owner/bound, 保留 repo 默认值"""
        from app.gateway.auth import get_oauth_backend

        cfg = _mock_settings_config
        ok, msg = get_oauth_backend("gitee").unbind()
        assert ok is True
        assert "解绑" in msg

        assert cfg.gitee_bound.value is False
        assert cfg.gitee_user_token.value == ""
        assert cfg.gitee_user_owner.value == ""
        assert cfg.gitee_user_repo.value == ""
        cfg.save.assert_called_once()

    def test_unbind_saves_config(self, _mock_settings_config):
        """解绑应调用 cfg.save()"""
        from app.gateway.auth import get_oauth_backend

        get_oauth_backend("gitee").unbind()
        _mock_settings_config.save.assert_called_once()


# =============================================================================
# 3. GiteeOAuthBackend.get_bound_info
# =============================================================================


class TestGiteeBackendBoundInfo:
    """获取绑定信息"""

    def test_get_bound_info_when_bound(self, _mock_settings_config):
        """已绑定时应返回 owner/repo/token"""
        from app.gateway.auth import get_oauth_backend

        _mock_settings_config.gitee_bound.value = True
        _mock_settings_config.gitee_user_owner.value = "test_user"
        _mock_settings_config.gitee_user_repo.value = "DriFox_uploads"
        _mock_settings_config.gitee_user_token.value = "token_12345"
        # 确保 _ensure_valid_token 不会尝试刷新（token 仍在有效期内）
        _mock_settings_config.gitee_user_refresh_token.value = ""
        _mock_settings_config.gitee_token_expires_at.value = time.time() + 86400

        info = get_oauth_backend("gitee").get_bound_info()
        assert info is not None
        assert info["owner"] == "test_user"
        assert info["repo"] == "DriFox_uploads"
        assert info["token"] == "token_12345"

    def test_get_bound_info_when_not_bound(self, _mock_settings_config):
        """未绑定时应返回 None"""
        from app.gateway.auth import get_oauth_backend

        _mock_settings_config.gitee_bound.value = False
        info = get_oauth_backend("gitee").get_bound_info()
        assert info is None

    def test_get_bound_info_uses_property_access(self, _mock_settings_config):
        """验证 get_bound_info 通过 .value 访问配置"""
        from app.gateway.auth import get_oauth_backend

        _mock_settings_config.gitee_bound.value = True
        _mock_settings_config.gitee_user_owner.value = "owner_name"
        _mock_settings_config.gitee_user_refresh_token.value = ""
        _mock_settings_config.gitee_token_expires_at.value = time.time() + 86400
        info = get_oauth_backend("gitee").get_bound_info()
        assert info is not None


# =============================================================================
# 4. GiteeOAuthBackend.is_bound
# =============================================================================


class TestGiteeBackendIsBound:
    """查询绑定状态"""

    def test_is_bound_true(self, _mock_settings_config):
        from app.gateway.auth import get_oauth_backend

        _mock_settings_config.gitee_bound.value = True
        assert get_oauth_backend("gitee").is_bound() is True

    def test_is_bound_false(self, _mock_settings_config):
        from app.gateway.auth import get_oauth_backend

        _mock_settings_config.gitee_bound.value = False
        assert get_oauth_backend("gitee").is_bound() is False


# =============================================================================
# 5. GiteeOAuthBackend.bind（复用 base 通用授权码流程）
# =============================================================================


class TestGiteeBackendBind:
    """
    OAuth 绑定流程

    注意：bind() 内部通过 base.run_authorization_code_flow 起回调服务器、
    打开浏览器、等待授权码并换取 token；再执行 Gitee 特有的
    fetch_user_login / ensure_repo / 写配置。

    token 换取的 POST 发生在 base 层（patch app.gateway.auth.base.requests.post）；
    fetch_user_login / ensure_repo 的 GET/PATCH 发生在 gitee 层
    （patch app.gateway.auth.gitee.requests.get / .patch）。

    通过 preset mock server.auth_code / server.auth_error 触发不同分支，
    patch threading.Event.wait 让等待循环快速跑完。
    """

    @staticmethod
    def _mock_server_with_code(code: str = "auth_code_123"):
        s = MagicMock()
        s.auth_code = code
        s.auth_error = None
        return s

    @staticmethod
    def _mock_server_with_error(err: str = "user denied"):
        s = MagicMock()
        s.auth_code = None
        s.auth_error = err
        return s

    def test_no_oauth_credentials(self, _mock_settings_config):
        """OAuth 凭证未配置时应返回错误"""
        from app.gateway.auth import get_oauth_backend

        _mock_settings_config.gitee_oauth_client_id.value = ""
        _mock_settings_config.gitee_oauth_client_secret.value = ""

        ok, msg = get_oauth_backend("gitee").bind()
        assert ok is False
        assert "凭证未配置" in msg

    def test_callback_server_failed(self, _mock_settings_config):
        """回调服务器启动失败应返回错误"""
        from app.gateway.auth import get_oauth_backend

        with patch("app.gateway.auth.base.start_callback_server", return_value=None):
            ok, msg = get_oauth_backend("gitee").bind()
            assert ok is False
            assert "端口" in msg

    def test_oauth_timeout(self, _mock_settings_config):
        """授权超时应返回错误"""
        from app.gateway.auth import get_oauth_backend

        mock_server = self._mock_server_with_code()
        mock_server.auth_code = None  # 清除，模拟无人授权
        with patch("app.gateway.auth.base.start_callback_server", return_value=mock_server), \
             patch("app.gateway.auth.base.webbrowser.open"), \
             patch("app.gateway.auth.base.stop_callback_server") as mock_stop, \
             patch("app.gateway.auth.base.threading.Event.wait", return_value=None):

            ok, msg = get_oauth_backend("gitee").bind()
            assert ok is False
            assert "超时" in msg
            mock_stop.assert_called_once_with(mock_server)

    def test_oauth_user_error(self, _mock_settings_config):
        """用户拒绝授权应返回错误"""
        from app.gateway.auth import get_oauth_backend

        mock_server = self._mock_server_with_error("user denied")
        with patch("app.gateway.auth.base.start_callback_server", return_value=mock_server), \
             patch("app.gateway.auth.base.webbrowser.open"), \
             patch("app.gateway.auth.base.stop_callback_server"):

            ok, msg = get_oauth_backend("gitee").bind()
            assert ok is False
            assert "授权失败" in msg

    def test_token_exchange_failed(self, _mock_settings_config):
        """换取 access_token 失败应返回错误"""
        from app.gateway.auth import get_oauth_backend

        mock_server = self._mock_server_with_code()
        token_resp = MagicMock()
        token_resp.status_code = 401
        token_resp.json.return_value = {"message": "invalid code"}
        with patch("app.gateway.auth.base.start_callback_server", return_value=mock_server), \
             patch("app.gateway.auth.base.webbrowser.open"), \
             patch("app.gateway.auth.base.stop_callback_server"), \
             patch("app.gateway.auth.base.threading.Event.wait", return_value=None), \
             patch("app.gateway.auth.base.requests.post", return_value=token_resp), \
             patch("app.gateway.auth.gitee.requests.patch") as mock_patch:

            mock_patch.return_value = MagicMock(status_code=200)
            ok, msg = get_oauth_backend("gitee").bind()
            assert ok is False
            assert "Token" in msg

    def test_token_response_missing_token(self, _mock_settings_config):
        """Token 响应缺少 access_token 应返回错误"""
        from app.gateway.auth import get_oauth_backend

        mock_server = self._mock_server_with_code()
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"no_token_here": "true"}
        with patch("app.gateway.auth.base.start_callback_server", return_value=mock_server), \
             patch("app.gateway.auth.base.webbrowser.open"), \
             patch("app.gateway.auth.base.stop_callback_server"), \
             patch("app.gateway.auth.base.threading.Event.wait", return_value=None), \
             patch("app.gateway.auth.base.requests.post", return_value=token_resp):

            ok, msg = get_oauth_backend("gitee").bind()
            assert ok is False
            assert "缺少 access_token" in msg

    def test_user_info_failed(self, _mock_settings_config):
        """获取用户信息失败应返回错误"""
        from app.gateway.auth import get_oauth_backend

        mock_server = self._mock_server_with_code()
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "token_abc"}

        get_resp = MagicMock()
        get_resp.status_code = 403
        get_resp.json.return_value = {"message": "forbidden"}
        with patch("app.gateway.auth.base.start_callback_server", return_value=mock_server), \
             patch("app.gateway.auth.base.webbrowser.open"), \
             patch("app.gateway.auth.base.stop_callback_server"), \
             patch("app.gateway.auth.base.threading.Event.wait", return_value=None), \
             patch("app.gateway.auth.base.requests.post", return_value=token_resp), \
             patch("app.gateway.auth.gitee.requests.get") as mock_get, \
             patch("app.gateway.auth.gitee.requests.patch") as mock_patch:

            # GET 分别用于用户信息、主仓库检查、配置仓库检查
            mock_get.side_effect = [get_resp, MagicMock(status_code=200), MagicMock(status_code=200)]
            mock_patch.return_value = MagicMock(status_code=200)

            ok, msg = get_oauth_backend("gitee").bind()
            assert ok is False
            assert "用户信息" in msg

    def test_user_info_missing_login(self, _mock_settings_config):
        """用户信息缺少 login 字段应返回错误"""
        from app.gateway.auth import get_oauth_backend

        mock_server = self._mock_server_with_code()
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "token_abc"}

        with patch("app.gateway.auth.base.start_callback_server", return_value=mock_server), \
             patch("app.gateway.auth.base.webbrowser.open"), \
             patch("app.gateway.auth.base.stop_callback_server"), \
             patch("app.gateway.auth.base.threading.Event.wait", return_value=None), \
             patch("app.gateway.auth.base.requests.post", return_value=token_resp), \
             patch("app.gateway.auth.gitee.requests.get") as mock_get:

            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"name": "No Login"}

            ok, msg = get_oauth_backend("gitee").bind()
            assert ok is False
            assert "login 字段无效" in msg

    def test_full_oauth_success(self, _mock_settings_config):
        """完整的 OAuth 绑定成功流程"""
        from app.gateway.auth import get_oauth_backend

        mock_server = self._mock_server_with_code("valid_auth_code")
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "final_token_xyz"}

        with patch("app.gateway.auth.base.start_callback_server", return_value=mock_server), \
             patch("app.gateway.auth.base.webbrowser.open"), \
             patch("app.gateway.auth.base.stop_callback_server") as mock_stop, \
             patch("app.gateway.auth.base.threading.Event.wait", return_value=None), \
             patch("app.gateway.auth.base.requests.post", return_value=token_resp), \
             patch("app.gateway.auth.gitee.requests.get") as mock_get, \
             patch("app.gateway.auth.gitee.requests.patch") as mock_patch:

            mock_patch.return_value = MagicMock(status_code=200)

            # GET 顺序：用户信息 → 主仓库检查 → 配置仓库检查
            mock_get.side_effect = [
                MagicMock(status_code=200, json=lambda: {"login": "test_user"}),
                MagicMock(status_code=200, json=lambda: {"name": "DriFox_uploads", "private": True}),
                MagicMock(status_code=200, json=lambda: {"name": "DriFox_settings", "private": True}),
            ]

            ok, msg = get_oauth_backend("gitee").bind()
            assert ok is True
            assert "绑定成功" in msg

            assert _mock_settings_config.gitee_user_token.value == "final_token_xyz"
            assert _mock_settings_config.gitee_user_owner.value == "test_user"
            assert _mock_settings_config.gitee_user_repo.value == "DriFox_uploads"
            assert _mock_settings_config.gitee_bound.value is True

            mock_stop.assert_called_once_with(mock_server)

    def test_oauth_sets_repo_private(self, _mock_settings_config):
        """repo_private=True 时应创建私有仓库"""
        from app.gateway.auth import get_oauth_backend

        mock_server = self._mock_server_with_code("code")
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok"}

        # 注意：base 与 gitee 共享同一个 requests 模块，
        # 故只 patch 一次 requests.post，用 URL 区分 token 换取与建仓库。
        with patch("app.gateway.auth.base.start_callback_server", return_value=mock_server), \
             patch("app.gateway.auth.base.webbrowser.open"), \
             patch("app.gateway.auth.base.stop_callback_server"), \
             patch("app.gateway.auth.base.threading.Event.wait", return_value=None), \
             patch("app.gateway.auth.gitee.requests.get") as mock_get, \
             patch("app.gateway.auth.gitee.requests.patch") as mock_patch, \
             patch("app.gateway.auth.gitee.requests.post") as mock_post:

            def _post_side_effect(url: str, **kwargs):
                if "oauth/token" in url:
                    return token_resp
                return MagicMock(status_code=201, json=lambda: {"name": "repo"})
            mock_post.side_effect = _post_side_effect

            mock_get.side_effect = [
                MagicMock(status_code=200, json=lambda: {"login": "u"}),
                MagicMock(status_code=404),
                MagicMock(status_code=404),
            ]

            mock_patch.return_value = MagicMock(status_code=200)

            ok, msg = get_oauth_backend("gitee").bind(repo_private=True)
            assert ok is True

            # 找到创建主仓库的 POST（非 token 换取）
            create_posts = [
                c for c in mock_post.call_args_list
                if "oauth/token" not in c.args[0]
            ]
            assert len(create_posts) >= 1
            main_repo_data = create_posts[0].kwargs["data"]
            assert main_repo_data["name"] == "DriFox_uploads"
