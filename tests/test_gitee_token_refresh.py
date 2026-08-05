# -*- coding: utf-8 -*-
"""
回归测试：Gitee OAuth token 刷新必须收敛到单一入口。

历史 bug：
  - GiteeUploader / gitee_card 走 backend._ensure_valid_token（有锁 + 写盘）
  - ConfigSyncService._sync_token 自行调用 refresh_access_token（无锁 + 只写内存）
  - 两套入口对 Gitee refresh_token 的"单次使用 + 强制轮换"机制并发/时序竞争，
    导致内存新、磁盘旧；进程冷启动后从磁盘读到旧 refresh_token 必报
    "refresh_token 无效或已被撤销"。

修复：ConfigSyncService._sync_token 改为复用 backend._ensure_valid_token()，
     并通过 pause_upload() 抑制 watcher 防止写盘触发误上传。

本测试验证：
  1. 并发调用 _ensure_valid_token：只有一个线程真正命中 Gitee API（锁生效）
  2. 刷新成功后磁盘文件包含新的 refresh_token（持久化生效）
  3. ConfigSyncService._sync_token 走 _ensure_valid_token（不再自己刷）
"""

import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── 测试夹具 ──────────────────────────────────────────────


class _FakeSettings:
    """模拟 Settings 单例，把所有 ConfigItem.value 写到 tmpfile。"""

    def __init__(self, file_path: Path):
        self.file = file_path
        # 用真实 ConfigItem，避免 _ensure_valid_token 内部对 cfg.xxx.value 的访问失败
        from qfluentwidgets import ConfigItem
        from qfluentwidgets.common.config import BoolValidator

        self.gitee_bound = ConfigItem("Gitee", "Bound", True, BoolValidator())
        self.gitee_user_token = ConfigItem("Gitee", "UserToken", "")
        self.gitee_user_refresh_token = ConfigItem("Gitee", "UserRefreshToken", "")
        self.gitee_token_expires_at = ConfigItem("Gitee", "TokenExpiresAt", 0.0)
        self.gitee_user_owner = ConfigItem("Gitee", "UserOwner", "test_owner")
        self.gitee_user_repo = ConfigItem("Gitee", "UserRepo", "DriFox_uploads")
        self.gitee_oauth_client_id = ConfigItem("Gitee", "OAuthClientID", "fake_id")
        self.gitee_oauth_client_secret = ConfigItem("Gitee", "OAuthClientSecret", "fake_secret")
        self.gitee_token = ConfigItem("Gitee", "Token", "shared_token")
        self.gitee_owner = ConfigItem("Gitee", "Owner", "shared_owner")
        self.gitee_repo = ConfigItem("Gitee", "Repo", "shared_repo")
        self.gitee_path = ConfigItem("Gitee", "Path", "drifox")
        self.gitee_branch = ConfigItem("Gitee", "Branch", "master")

    # 模拟 cfg.save()：把当前所有 ConfigItem 写入 tmp 文件
    def save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "Gitee": {
                        "Bound": self.gitee_bound.value,
                        "UserToken": self.gitee_user_token.value,
                        "UserRefreshToken": self.gitee_user_refresh_token.value,
                        "TokenExpiresAt": self.gitee_token_expires_at.value,
                        "UserOwner": self.gitee_user_owner.value,
                        "UserRepo": self.gitee_user_repo.value,
                    }
                },
                f,
                ensure_ascii=False,
                indent=2,
            )


@pytest.fixture
def tmp_settings(tmp_path):
    """给被测模块用的临时 Settings（带真实 ConfigItem）"""
    cfg_file = tmp_path / "app.config"
    fake = _FakeSettings(cfg_file)
    with mock.patch("app.utils.config.Settings.get_instance", return_value=fake):
        yield fake, cfg_file


def _seed_bound_state(fake: _FakeSettings, *, expired: bool = True):
    """设置「已绑定 + token 过期」状态"""
    fake.gitee_user_token.value = "old_access_token"
    fake.gitee_user_refresh_token.value = "R0"
    fake.gitee_token_expires_at.value = (time.time() - 3600) if expired else (time.time() + 3600)
    fake.gitee_user_owner.value = "test_owner"


# ── 1. 锁生效：并发只触发一次真正的 Gitee 刷新 ────────────


def test_concurrent_refresh_only_one_gitee_call(tmp_settings):
    """N 个线程同时调 _ensure_valid_token，refresh_access_token 只该被调一次。"""
    from app.gateway.auth import gitee as gitee_mod

    fake, _ = tmp_settings
    _seed_bound_state(fake, expired=True)

    call_count = 0
    call_lock = threading.Lock()

    def fake_refresh(refresh_token, client_id, client_secret):
        nonlocal call_count
        with call_lock:
            call_count += 1
        # 模拟 Gitee 的 rotation：每次返回新 refresh_token
        return {
            "access_token": f"AT_{call_count}",
            "refresh_token": f"R{call_count + 1}",
            "expires_in": 86400,
        }, ""

    with mock.patch.object(gitee_mod, "refresh_access_token", side_effect=fake_refresh):
        backend = gitee_mod.GiteeOAuthBackend()
        results: list = []
        errors: list = []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait(timeout=2)
                tok, err = backend._ensure_valid_token()
                results.append((tok, err))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert not errors, f"线程异常: {errors}"
    assert all(tok for tok, _ in results), f"所有线程应拿到有效 token, results={results}"
    # ★ 关键断言：8 个并发请求只该 1 个真正打到 Gitee
    assert call_count == 1, (
        f"Gitee refresh 被调用 {call_count} 次（期望 1 次），refresh_token 已被 rotation {call_count} 次！"
    )


# ── 2. 持久化生效：刷新后磁盘文件包含新 refresh_token ──────


def test_refresh_persists_new_refresh_token_to_disk(tmp_settings):
    """刷新后磁盘 app.config 必须包含新的 refresh_token（下次冷启动能继续用）"""
    from app.gateway.auth import gitee as gitee_mod

    fake, cfg_file = tmp_settings
    _seed_bound_state(fake, expired=True)
    assert not cfg_file.exists(), "测试前置：磁盘不应预先存在"

    def fake_refresh(refresh_token, client_id, client_secret):
        return {
            "access_token": "AT_new",
            "refresh_token": "R_new",
            "expires_in": 86400,
        }, ""

    with mock.patch.object(gitee_mod, "refresh_access_token", side_effect=fake_refresh):
        backend = gitee_mod.GiteeOAuthBackend()
        tok, err = backend._ensure_valid_token()

    assert tok == "AT_new", f"刷新失败: {err}"
    assert cfg_file.exists(), "刷新成功后磁盘文件应已写入"

    with open(cfg_file, encoding="utf-8") as f:
        saved = json.load(f)
    gitee_saved = saved.get("Gitee", {})

    # ★ 关键断言：磁盘上的 refresh_token 必须是新的 R_new
    assert gitee_saved.get("UserRefreshToken") == "R_new", (
        f"磁盘上 refresh_token 仍是旧的！disk={gitee_saved.get('UserRefreshToken')!r}, "
        f"expected='R_new'。这是导致冷启动 'refresh_token 无效' 的根因。"
    )
    assert gitee_saved.get("UserToken") == "AT_new"
    assert gitee_saved.get("TokenExpiresAt", 0) > time.time() + 86000


# ── 3. ConfigSyncService 收敛到 _ensure_valid_token ────────


def test_config_sync_uses_backend_ensure_valid_token(tmp_settings):
    """ConfigSyncService._sync_token 必须复用 _ensure_valid_token，
    不能自己再调 refresh_access_token（否则双入口 bug 复发）。"""
    from app.core import config_sync as cs_mod
    from app.gateway.auth import gitee as gitee_mod

    fake, cfg_file = tmp_settings
    _seed_bound_state(fake, expired=True)

    # 准备一个 ConfigSyncService 实例
    svc = cs_mod.ConfigSyncService.get_instance()
    svc._state = "idle"  # 避免 _set_state 触发未知分支

    # mock 两个潜在入口：backend._ensure_valid_token 和原始 refresh_access_token
    refresh_called = {"backend_ensure": 0, "raw_refresh": 0}

    def fake_ensure(backend_self):
        refresh_called["backend_ensure"] += 1
        # 模拟一次成功刷新
        fake.gitee_user_token.value = "AT_from_ensure"
        fake.gitee_user_refresh_token.value = "R_from_ensure"
        fake.gitee_token_expires_at.value = time.time() + 86400
        return "AT_from_ensure", ""

    def should_not_call(*a, **kw):
        refresh_called["raw_refresh"] += 1
        return None, "不应该被调用"

    with (
        mock.patch.object(gitee_mod.GiteeOAuthBackend, "_ensure_valid_token", fake_ensure),
        mock.patch.object(gitee_mod, "refresh_access_token", side_effect=should_not_call),
    ):
        ok = svc._sync_token()

    assert ok, "_sync_token 应该成功"
    # ★ 关键断言：只走 backend._ensure_valid_token，没自己调 refresh_access_token
    assert refresh_called["backend_ensure"] == 1
    assert refresh_called["raw_refresh"] == 0, (
        "ConfigSyncService._sync_token 还在自行调用 refresh_access_token！双入口 bug 已复发，请检查 _sync_token 实现。"
    )
    # 抑制窗口应被设置（防止 watcher 触发误上传）
    assert svc._suppress_until > time.time(), "pause_upload() 未生效"


# ── 4. pause_upload() 正确设置抑制窗口 ────────────────────


def test_pause_upload_extends_suppress_window():
    """ConfigSyncService.pause_upload() 公开 API 应正确延长抑制窗口。"""
    from app.core import config_sync as cs_mod

    svc = cs_mod.ConfigSyncService.get_instance()
    svc._suppress_until = 0.0

    svc.pause_upload(3.0)
    assert 2.5 < (svc._suppress_until - time.time()) < 4.0, "3s 抑制窗口未正确设置"

    # 二次调用应取较大值（不会缩短）
    svc.pause_upload(10.0)
    assert (svc._suppress_until - time.time()) > 9.0, "应取较长抑制时间"

    svc.pause_upload(1.0)
    assert (svc._suppress_until - time.time()) > 9.0, "不应缩短已设置的抑制窗口"
