# -*- coding: utf-8 -*-
"""
回归测试：Gitee token 设备本地独立 + 失效判定。

历史 bug（旧架构）：配置同步把含 Gitee token 段的 app.config 上传云端，多台
设备共用同一份 refresh_token；任何一台刷新即旋转，其他设备的旧 RT 立即失效
→ invalid_grant → 误清绑。

现行架构：token 是**设备本地独立凭证**，云端 app.config 不含 token
（上传剔除 / 下载合并保留本地）。跨设备不再共享 RT，不存在轮换互踩；
本地 RT 失效只可能因用户在 Gitee 官网撤销授权。

本测试验证：
  1. recover_token_from_cloud：从本地磁盘重载 token 后重试刷新
  2. _refresh_local_and_upload：TOKEN_REVOKED 时先恢复，失败才清绑
  3. 网络异常保留绑定，不清绑
"""

import sys
import time
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.gateway.test_gitee_token_refresh import _FakeSettings  # noqa: E402

# ── 测试夹具 ──────────────────────────────────────────────

@pytest.fixture
def tmp_settings(tmp_path):
    """给被测模块用的临时 Settings（带真实 ConfigItem + toDict）"""
    cfg_file = tmp_path / "app.config"

    class _Fake(_FakeSettings):
        def toDict(self):
            return {
                "Gitee": {
                    "Bound": self.gitee_bound.value,
                    "UserToken": self.gitee_user_token.value,
                    "UserRefreshToken": self.gitee_user_refresh_token.value,
                    "TokenExpiresAt": self.gitee_token_expires_at.value,
                    "UserOwner": self.gitee_user_owner.value,
                    "UserRepo": self.gitee_user_repo.value,
                }
            }

    fake = _Fake(cfg_file)
    with mock.patch("app.utils.config.Settings.get_instance", return_value=fake):
        yield fake, cfg_file

def _seed(fake: _FakeSettings, *, local_rt: str = "R_local"):
    """设置「已绑定 + access_token 将过期」状态"""
    fake.gitee_bound.value = True
    fake.gitee_user_token.value = "AT_local"
    fake.gitee_user_refresh_token.value = local_rt
    fake.gitee_token_expires_at.value = time.time() + 30  # 提前 60s 刷新窗口内
    fake.gitee_user_owner.value = "test_owner"

# ── 1. recover_token_from_cloud：本地重载后重试刷新 ─────────

def test_recover_token_from_cloud_unbound_returns_false(tmp_settings):
    """未绑定：直接返回 False，不去重载磁盘 token"""
    from app.core.sync import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    svc = cs_mod.ConfigSyncService.get_instance()
    with (
        mock.patch.object(gitee_mod.GiteeOAuthBackend, "is_bound", return_value=False),
        mock.patch.object(svc, "_load_gitee_token_from_disk") as m_load,
    ):
        assert svc.recover_token_from_cloud() is False
    m_load.assert_not_called()

def test_recover_token_from_cloud_reload_then_refresh_ok(tmp_settings):
    """已绑定：重载磁盘 token 后刷新成功 → True"""
    from app.core.sync import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    fake, _ = tmp_settings
    _seed(fake)

    svc = cs_mod.ConfigSyncService.get_instance()
    with (
        mock.patch.object(gitee_mod.GiteeOAuthBackend, "is_bound", return_value=True),
        mock.patch.object(
            gitee_mod.GiteeOAuthBackend, "_ensure_valid_token", return_value=("AT_new", "")
        ),
        mock.patch.object(svc, "_load_gitee_token_from_disk") as m_load,
    ):
        assert svc.recover_token_from_cloud() is True
    m_load.assert_called_once()

def test_recover_token_from_cloud_reload_then_refresh_fail(tmp_settings):
    """已绑定但重载后刷新仍失败（官网撤权）→ False，交由调用方清绑"""
    from app.core.sync import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    fake, _ = tmp_settings
    _seed(fake)

    svc = cs_mod.ConfigSyncService.get_instance()
    with (
        mock.patch.object(gitee_mod.GiteeOAuthBackend, "is_bound", return_value=True),
        mock.patch.object(
            gitee_mod.GiteeOAuthBackend,
            "_ensure_valid_token",
            return_value=(None, "TOKEN_REVOKED::invalid_grant"),
        ),
        mock.patch.object(svc, "_load_gitee_token_from_disk") as m_load,
    ):
        assert svc.recover_token_from_cloud() is False
    m_load.assert_called_once()

# ── 2. _refresh_local_and_upload：失效时先本地恢复 ────────

def test_refresh_local_and_upload_revoked_recovers(tmp_settings):
    """本地刷新 TOKEN_REVOKED 且本地重载恢复成功 → 不清绑，syncDone(True)"""
    from app.core.sync import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    fake, _ = tmp_settings
    _seed(fake, local_rt="R_local")

    svc = cs_mod.ConfigSyncService.get_instance()
    svc._state = "idle"
    svc._initial_sync_completed = False

    results: list = []

    def _on_done(success, msg):
        results.append((success, msg))

    svc.syncDone.connect(_on_done)

    with (
        mock.patch.object(
            gitee_mod.GiteeOAuthBackend,
            "_ensure_valid_token",
            return_value=(None, "token 已过期且刷新失败：TOKEN_REVOKED::invalid_grant"),
        ),
        mock.patch.object(svc, "recover_token_from_cloud", return_value=True) as m_recover,
    ):
        svc._refresh_local_and_upload()

    m_recover.assert_called_once()
    # ★ 关键断言：恢复成功不清绑
    assert fake.gitee_bound.value is True, "恢复成功后不应清绑"
    assert fake.gitee_user_refresh_token.value == "R_local", "恢复后本地 RT 不应被清空"
    assert results and results[0][0] is True, f"应发成功信号, got={results}"
    assert svc._initial_sync_completed is True, "恢复成功应标记初始同步完成"

def test_refresh_local_and_upload_revoked_clears_when_no_cloud(tmp_settings):
    """本地刷新 TOKEN_REVOKED 且本地恢复也失败 → 清绑 + syncDone(False, 已失效)"""
    from app.core.sync import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    fake, _ = tmp_settings
    _seed(fake, local_rt="R_local")

    svc = cs_mod.ConfigSyncService.get_instance()
    svc._state = "idle"

    results: list = []

    def _on_done(success, msg):
        results.append((success, msg))

    svc.syncDone.connect(_on_done)

    with (
        mock.patch.object(
            gitee_mod.GiteeOAuthBackend,
            "_ensure_valid_token",
            return_value=(None, "token 已过期且刷新失败：TOKEN_REVOKED::invalid_grant"),
        ),
        mock.patch.object(svc, "recover_token_from_cloud", return_value=False),
    ):
        svc._refresh_local_and_upload()

    # ★ 本地恢复也失败 → 真失效，才清绑
    assert fake.gitee_bound.value is False, "恢复失败后才允许清绑"
    assert fake.gitee_user_token.value == "", "access_token 应被清空"
    assert fake.gitee_user_refresh_token.value == "", "refresh_token 应被清空"
    assert results and results[0][0] is False, f"应发失败信号, got={results}"
    assert "已失效" in results[0][1], f"失败消息应含'已失效', got={results[0][1]}"

def test_refresh_local_and_upload_network_error_keeps_binding(tmp_settings):
    """网络异常（非 TOKEN_REVOKED）→ 保留绑定，不触发恢复"""
    from app.core.sync import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    fake, _ = tmp_settings
    _seed(fake, local_rt="R_local")

    svc = cs_mod.ConfigSyncService.get_instance()
    svc._state = "idle"

    with (
        mock.patch.object(
            gitee_mod.GiteeOAuthBackend,
            "_ensure_valid_token",
            return_value=(None, "token 已过期且刷新失败：刷新 Token 网络异常：timeout"),
        ),
        mock.patch.object(svc, "recover_token_from_cloud") as m_recover,
    ):
        svc._refresh_local_and_upload()

    m_recover.assert_not_called(), "网络异常不应走恢复（非 RT 失效）"
    assert fake.gitee_bound.value is True, "网络异常不应清绑"
    assert svc._state == "error", "网络异常应保持 error 状态（稍后重试）"
