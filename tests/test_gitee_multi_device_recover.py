# -*- coding: utf-8 -*-
"""
回归测试：多设备 refresh_token rotation 互踩 → 误清绑修复。

历史 bug：
  Gitee 的 refresh_token 是单次轮换的（rotation）：任何一台设备用它刷新，
  旧 RT 立即作废并返回新 RT。DriFox 配置同步会把含 Gitee token 段的
  app.config 上传云端、其他设备下载覆盖本地 → 多台设备持有**同一个 RT**。
  设备 A 刷新后，设备 B 运行中 access_token 过期 → 用本地旧 RT 刷新 →
  invalid_grant → 被误判「真失效」→ 清绑 → 同步红点 + 图片上传失败。

修复：
  - ConfigSyncService.recover_token_from_cloud()：本地刷新失败（TOKEN_REVOKED）
    时先拉取云端最新 app.config 中的 RT 重试刷新，成功则收敛，失败才清绑。
  - ConfigSyncService._fetch_cloud_token_pair()：强制直读云端 Gitee token 段。
  - _refresh_local_and_upload()：失效分支先走云端恢复，不再立即清绑。

本测试验证：
  1. _fetch_cloud_token_pair 能从云端 app.config 提取新 RT 并写回本地
  2. recover_token_from_cloud 在「云端 RT 更新」时恢复成功并回传云端
  3. 云端 RT 与本地一致时不进入无效循环（返回 False）
  4. _refresh_local_and_upload 在 TOKEN_REVOKED 时先云端恢复，成功不清绑
  5. 云端恢复也失败时才清绑并提示重新绑定
"""

import base64
import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.test_gitee_token_refresh import _FakeSettings  # noqa: E402


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


def _cloud_config_content(rt: str, at: str = "AT_cloud") -> str:
    """构造云端 app.config 的 base64 内容（含 Gitee 段）"""
    data = {
        "Gitee": {
            "Bound": True,
            "UserToken": at,
            "UserRefreshToken": rt,
            "TokenExpiresAt": time.time() + 86400,
            "UserOwner": "test_owner",
            "UserRepo": "DriFox_uploads",
        },
        "UI": {"ThemeStyle": "dark"},
    }
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttpxClient:
    """支持 with 语法的假 httpx.Client（只实现 get）"""

    def __init__(self, get_resp):
        self._get_resp = get_resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        return self._get_resp


# ── 1. _fetch_cloud_token_pair：从云端提取新 RT 写回本地 ──


def test_fetch_cloud_token_pair_updates_local(tmp_settings):
    """云端 app.config 含新 RT 时，_fetch_cloud_token_pair 应写回 cfg + 磁盘"""
    from app.core import config_sync as cs_mod

    fake, cfg_file = tmp_settings
    _seed(fake, local_rt="R_local")
    fake.save()  # 磁盘已有旧值

    cloud_resp = _FakeResp(
        200,
        {"content": _cloud_config_content("R_cloud"), "sha": "abc123"},
    )
    fake_client = _FakeHttpxClient(cloud_resp)

    svc = cs_mod.ConfigSyncService.get_instance()
    with mock.patch("app.core.config_sync.httpx.Client", return_value=fake_client):
        ok = svc._fetch_cloud_token_pair()

    assert ok, "应从云端恢复 token 段"
    assert fake.gitee_user_refresh_token.value == "R_cloud", "内存 RT 应更新为云端值"
    assert fake.gitee_user_token.value == "AT_cloud", "内存 access_token 应更新为云端值"

    # 磁盘也必须更新（下次冷启动能读到）
    with open(cfg_file, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["Gitee"]["UserRefreshToken"] == "R_cloud", "磁盘 RT 应更新为云端值"


def test_fetch_cloud_token_pair_same_rt_returns_false(tmp_settings):
    """云端 RT 与本地一致 → 返回 False（防止无效循环）"""
    from app.core import config_sync as cs_mod

    fake, _ = tmp_settings
    _seed(fake, local_rt="R_same")

    cloud_resp = _FakeResp(200, {"content": _cloud_config_content("R_same")})
    fake_client = _FakeHttpxClient(cloud_resp)

    svc = cs_mod.ConfigSyncService.get_instance()
    with mock.patch("app.core.config_sync.httpx.Client", return_value=fake_client):
        ok = svc._fetch_cloud_token_pair()

    assert not ok, "云端 RT 与本地相同，不应进入无效恢复循环"


def test_fetch_cloud_token_pair_http_error_returns_false(tmp_settings):
    """云端读取失败（401/网络）→ 返回 False"""
    from app.core import config_sync as cs_mod

    fake, _ = tmp_settings
    _seed(fake)

    fake_client = _FakeHttpxClient(_FakeResp(401, {"message": "invalid_token"}))
    svc = cs_mod.ConfigSyncService.get_instance()
    with mock.patch("app.core.config_sync.httpx.Client", return_value=fake_client):
        ok = svc._fetch_cloud_token_pair()

    assert not ok, "云端 401 时不应恢复成功"


# ── 2. recover_token_from_cloud：云端有更新 RT 时恢复 ──────


def test_recover_token_from_cloud_success(tmp_settings):
    """本地 RT 失效 + 云端 RT 更新 → 恢复成功并回传云端"""
    from app.core import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    fake, _ = tmp_settings
    _seed(fake, local_rt="R_local")

    svc = cs_mod.ConfigSyncService.get_instance()
    svc._state = "idle"
    svc._token = "AT_local"
    svc._owner = "test_owner"
    svc._initial_sync_completed = True
    # 屏蔽上传/落盘副作用，聚焦编排
    svc._persist_and_upload_token = mock.Mock()

    # 第 1 次 fetch 已把云端 RT 写入 cfg → _ensure_valid_token 用云端 RT 刷新成功
    ensure_results = [("AT_new", "")]
    with (
        mock.patch.object(
            gitee_mod.GiteeOAuthBackend,
            "_ensure_valid_token",
            side_effect=ensure_results,
        ),
        mock.patch.object(svc, "_fetch_cloud_token_pair", return_value=True) as m_fetch,
    ):
        ok = svc.recover_token_from_cloud()

    assert ok, "云端有更新 RT 时应恢复成功"
    assert m_fetch.call_count == 1, "只应拉取一次云端（第一次刷新即成功）"
    svc._persist_and_upload_token.assert_called_once(), "恢复成功应回传云端"


def test_recover_token_from_cloud_all_revoked_returns_false(tmp_settings):
    """云端 RT 也失效（连刷 2 次 TOKEN_REVOKED）→ 返回 False（应清绑）"""
    from app.core import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    fake, _ = tmp_settings
    _seed(fake, local_rt="R_local")

    svc = cs_mod.ConfigSyncService.get_instance()
    svc._state = "idle"
    svc._persist_and_upload_token = mock.Mock()

    with (
        mock.patch.object(
            gitee_mod.GiteeOAuthBackend,
            "_ensure_valid_token",
            return_value=(None, "token 已过期且刷新失败：TOKEN_REVOKED::invalid_grant"),
        ),
        mock.patch.object(svc, "_fetch_cloud_token_pair", return_value=True) as m_fetch,
    ):
        ok = svc.recover_token_from_cloud()

    assert not ok, "云端 RT 也无效时应恢复失败"
    assert m_fetch.call_count == 2, "第 1 次刷新失败应重拉云端再试一次"
    svc._persist_and_upload_token.assert_not_called()


def test_recover_token_from_cloud_no_cloud_rt(tmp_settings):
    """云端无 RT（_fetch 返回 False）→ 直接失败"""
    from app.core import config_sync as cs_mod

    fake, _ = tmp_settings
    _seed(fake, local_rt="R_local")

    svc = cs_mod.ConfigSyncService.get_instance()
    svc._persist_and_upload_token = mock.Mock()

    with mock.patch.object(svc, "_fetch_cloud_token_pair", return_value=False):
        ok = svc.recover_token_from_cloud()

    assert not ok, "云端无 RT 时恢复失败"
    svc._persist_and_upload_token.assert_not_called()


# ── 3. _refresh_local_and_upload：失效时先云端恢复 ────────


def test_refresh_local_and_upload_revoked_recovers(tmp_settings):
    """本地刷新 TOKEN_REVOKED 且云端恢复成功 → 不清绑，syncDone(True)"""
    from app.core import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    fake, _ = tmp_settings
    _seed(fake, local_rt="R_local")

    svc = cs_mod.ConfigSyncService.get_instance()
    svc._state = "idle"
    svc._initial_sync_completed = False
    svc.recover_token_from_cloud = mock.Mock(return_value=True)

    results: list = []

    def _on_done(success, msg):
        results.append((success, msg))

    svc.syncDone.connect(_on_done)

    with mock.patch.object(
        gitee_mod.GiteeOAuthBackend,
        "_ensure_valid_token",
        return_value=(None, "token 已过期且刷新失败：TOKEN_REVOKED::invalid_grant"),
    ):
        svc._refresh_local_and_upload()

    svc.recover_token_from_cloud.assert_called_once(), "失效时应先尝试云端恢复"
    # ★ 关键断言：不清绑（多设备场景不该误判真失效）
    assert fake.gitee_bound.value is True, "云端恢复成功后不应清绑"
    assert fake.gitee_user_refresh_token.value == "R_local", "恢复后本地 RT 不应被清空"
    assert results and results[0][0] is True, f"应发成功信号, got={results}"
    assert svc._initial_sync_completed is True, "恢复成功应标记初始同步完成"


def test_refresh_local_and_upload_revoked_clears_when_no_cloud(tmp_settings):
    """本地刷新 TOKEN_REVOKED 且云端恢复失败 → 清绑 + syncDone(False, 已失效)"""
    from app.core import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    fake, _ = tmp_settings
    _seed(fake, local_rt="R_local")

    svc = cs_mod.ConfigSyncService.get_instance()
    svc._state = "idle"
    svc.recover_token_from_cloud = mock.Mock(return_value=False)

    results: list = []

    def _on_done(success, msg):
        results.append((success, msg))

    svc.syncDone.connect(_on_done)

    with mock.patch.object(
        gitee_mod.GiteeOAuthBackend,
        "_ensure_valid_token",
        return_value=(None, "token 已过期且刷新失败：TOKEN_REVOKED::invalid_grant"),
    ):
        svc._refresh_local_and_upload()

    # ★ 云端也恢复失败 → 真失效，才清绑
    assert fake.gitee_bound.value is False, "云端恢复失败后才允许清绑"
    assert fake.gitee_user_token.value == "", "access_token 应被清空"
    assert fake.gitee_user_refresh_token.value == "", "refresh_token 应被清空"
    assert results and results[0][0] is False, f"应发失败信号, got={results}"
    assert "已失效" in results[0][1], f"失败消息应含'已失效', got={results[0][1]}"


def test_refresh_local_and_upload_network_error_keeps_binding(tmp_settings):
    """网络异常（非 TOKEN_REVOKED）→ 保留绑定，不触发云端恢复"""
    from app.core import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    fake, _ = tmp_settings
    _seed(fake, local_rt="R_local")

    svc = cs_mod.ConfigSyncService.get_instance()
    svc._state = "idle"
    svc.recover_token_from_cloud = mock.Mock()

    with mock.patch.object(
        gitee_mod.GiteeOAuthBackend,
        "_ensure_valid_token",
        return_value=(None, "token 已过期且刷新失败：刷新 Token 网络异常：timeout"),
    ):
        svc._refresh_local_and_upload()

    svc.recover_token_from_cloud.assert_not_called(), "网络异常不应走云端恢复（非 RT 失效）"
    assert fake.gitee_bound.value is True, "网络异常不应清绑"
    assert svc._state == "error", "网络异常应保持 error 状态（稍后重试）"
