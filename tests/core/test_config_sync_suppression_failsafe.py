# -*- coding: utf-8 -*-
"""config_sync save 写盘保护兜底与复位路径回归（实施单 #I5）。

覆盖完成标准：
- daemon 线程置保护后，兜底最终在主线程执行（Event/信号等待断言）
- 上传冲突链 app.config 合并成功后存在复位路径（_reloadSettings.emit）
- 释放逻辑清 reason 与时间戳；超窗场景不崩溃
- 既有下载链 finally 复位语义由全量 sync 回归另行覆盖
"""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.sync import config_sync as cs
from app.core.sync.config_sync import ConfigSyncService

@pytest.fixture
def svc(tmp_path, monkeypatch):
    """隔离到 tmp_path 的服务实例（模式对齐 test_config_sync_merge.py）"""
    from app.utils import info_bar_guard

    # 预热主线程投递桥（对齐真实启动序列 install_guard 的主线程预热）：
    # 兜底经 run_on_main_thread → post_to_main，桥未就绪时后台线程投递失败
    info_bar_guard._ensure_dispatcher()
    monkeypatch.setattr(cs, "get_app_data_dir", lambda: tmp_path)
    service = ConfigSyncService.get_instance()
    service._state = "idle"
    service._token = "token"
    service._owner = "owner"
    service._meta_recorder = None  # 隔离单例记账器，防跨用例污染
    service._content_hashes = {}
    service._merge_bases = set()
    service._device_tag = "testbox-abcd"
    service._suppress_until = 0.0
    yield service
    # 谁污染谁清理：置位残留会让后续用例 cfg.save() 静默不落盘
    service._meta_recorder = None
    try:
        from app.utils.config import Settings

        inst = Settings.get_instance()
        inst._suppress_save_reason = None
        inst._suppress_since = None
    except Exception:
        pass
    service._state = "disabled"

def _suppress(since_offset=0.0):
    from app.utils.config import Settings

    inst = Settings.get_instance()
    inst._suppress_save_reason = "sync-merge-pending-reload"
    inst._suppress_since = time.monotonic() - since_offset
    return inst

def test_failsafe_executes_on_main_thread_from_daemon(svc, monkeypatch, qtbot):
    """daemon 线程置保护 → 兜底最终在主线程执行并解除保护（Event 等待断言）"""
    monkeypatch.setattr(ConfigSyncService, "SUPPRESSION_FAILSAFE_MS", 50)
    inst = _suppress()
    assert inst._suppress_save_reason == "sync-merge-pending-reload"

    done = threading.Event()
    main_tid = threading.get_ident()
    seen = []
    orig = svc._release_save_suppression

    def spy():
        seen.append(threading.get_ident())
        orig()
        done.set()

    monkeypatch.setattr(svc, "_release_save_suppression", spy)

    t = threading.Thread(target=svc._arm_suppression_failsafe, daemon=True)
    t.start()
    t.join(2)
    qtbot.waitUntil(done.is_set, timeout=5000)
    assert seen == [main_tid], f"兜底执行线程 {seen}，应为主线程 {main_tid}"
    assert inst._suppress_save_reason == ""  # 兜底真解除（旧实现永不触发）

def test_upload_conflict_merge_emits_reload_for_app_config(svc, monkeypatch):
    """上传冲突链：app.config 合并成功 → _reloadSettings.emit（复位路径存在）"""
    calls = {"apply": 0, "emit": 0}
    monkeypatch.setattr(
        svc, "_apply_config_merge", lambda content, remote_authority=False: calls.__setitem__("apply", calls["apply"] + 1)
    )
    monkeypatch.setattr(svc, "_read_remote_bytes", lambda path, client=None: (b'{"System": {}}', "abc123"))
    monkeypatch.setattr(svc, "_has_merge_base", lambda unit: True)
    monkeypatch.setattr(svc, "_save_base_config", lambda data: None)
    monkeypatch.setattr(svc, "_flush_conflict_archive", lambda client=None: None)
    monkeypatch.setattr(svc, "_set_cached_remote_sha", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_save_sha_cache", lambda: None)
    svc._reloadSettings.connect(lambda: calls.__setitem__("emit", calls["emit"] + 1))

    sha = svc._resolve_unit_divergence("app.config", "/app.config", Path("unused"), MagicMock(), "live1")
    assert sha == "abc123"
    assert calls["apply"] == 1
    assert calls["emit"] == 1  # 上传链复位路径存在

def test_upload_conflict_merge_no_emit_for_non_app_config(svc, monkeypatch):
    """非 app.config 单元（目录/留档合并）不触发 Settings 重载"""
    calls = {"emit": 0}
    monkeypatch.setattr(svc, "_read_remote_bytes", lambda path, client=None: (b"zip-bytes", "sha9"))
    monkeypatch.setattr(svc, "_apply_dir_merge", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_flush_conflict_archive", lambda client=None: None)
    monkeypatch.setattr(svc, "_set_cached_remote_sha", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_save_sha_cache", lambda: None)
    svc._reloadSettings.connect(lambda: calls.__setitem__("emit", calls["emit"] + 1))

    # local_path 走目录分支（_apply_dir_merge 已 mock，路径本身不被真读）
    sha = svc._resolve_unit_divergence("user-custom", "/uc.zip", Path("."), MagicMock(), "live2")
    assert sha == "sha9"
    assert calls["emit"] == 0

def test_release_clears_reason_and_timestamp(svc):
    """释放清 reason 与时间戳（幂等，未置位时安全空转）"""
    inst = _suppress(since_offset=0.001)
    svc._release_save_suppression()
    assert inst._suppress_save_reason == ""
    assert inst._suppress_since is None
    # 幂等：再次调用不抛错
    svc._release_save_suppression()

def test_release_without_since_stamp_still_clears(svc):
    """旧格式残留（无时间戳字段）→ 仍解除 reason（向后兼容）"""
    from app.utils.config import Settings

    inst = Settings.get_instance()
    inst._suppress_save_reason = "sync-merge-pending-reload"
    if hasattr(inst, "_suppress_since"):
        del inst._suppress_since
    svc._release_save_suppression()
    assert inst._suppress_save_reason == ""
    # 清理测试期间动态删的属性痕迹（getattr 缺省路径已验证）
    inst._suppress_since = None
