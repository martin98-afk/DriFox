# -*- coding: utf-8 -*-
"""测试云同步合并写盘与 Settings 内存重载之间的写盘保护。

背景（真实事故）：2026-11-16 09:30:40 合并结果已写盘（含云端独有服务商
CodeBuddy），09:31:01 插件迁移触发的 Settings.save() 用合并前的旧内存态整
文件覆盖回去，云端独有的服务商条目与 12 处配置值全部丢失，合并成果只存活
21 秒。本文件锁定这条保护：合并写盘后到主线程重载完成之前，save() 一律丢弃。
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class _FakeSettings:
    """最小 Settings 替身：只承载写盘保护标志与文件写入"""

    def __init__(self, path):
        self.file = path
        self._suppress_save_reason = ""
        self.disk_writes = []

    def save(self, content):
        """模拟 Settings.save()：真实实现里保护命中时直接 return"""
        suppress = getattr(self, "_suppress_save_reason", "")
        if suppress:
            return False
        self.disk_writes.append(content)
        self.file.write_text(content, encoding="utf-8")
        return True


@pytest.fixture()
def cfg(tmp_path):
    p = tmp_path / "app.config"
    p.write_text('{"merged": true}', encoding="utf-8")
    return _FakeSettings(p)


def test_save_is_dropped_while_protection_active(cfg):
    """保护期间 save() 不得写盘，否则覆盖合并结果"""
    cfg._suppress_save_reason = "sync-merge-pending-reload"
    assert cfg.save('{"stale": true}') is False
    assert cfg.file.read_text(encoding="utf-8") == '{"merged": true}'
    assert cfg.disk_writes == []


def test_save_works_after_protection_released(cfg):
    """保护解除后 save() 恢复正常：不能永久禁用落盘"""
    cfg._suppress_save_reason = "sync-merge-pending-reload"
    assert cfg.save('{"blocked": true}') is False

    cfg._suppress_save_reason = ""
    assert cfg.save('{"after": true}') is True
    assert cfg.file.read_text(encoding="utf-8") == '{"after": true}'


def test_suppress_save_context_restores_previous_state():
    """嵌套/中途异常都要正确恢复，不能把保护永久留在置位状态"""
    from contextlib import contextmanager

    class _S:
        def __init__(self):
            self._suppress_save_reason = ""

        @contextmanager
        def suppress_save(self, reason=""):
            prev = self._suppress_save_reason
            self._suppress_save_reason = reason or prev or "sync-merge"
            try:
                yield
            finally:
                self._suppress_save_reason = prev

    s = _S()
    with s.suppress_save("outer"):
        assert s._suppress_save_reason == "outer"
        with s.suppress_save("inner"):
            assert s._suppress_save_reason == "inner"
        assert s._suppress_save_reason == "outer"
    assert s._suppress_save_reason == ""

    # 异常路径同样恢复
    try:
        with s.suppress_save("boom"):
            raise RuntimeError("simulated")
    except RuntimeError:
        pass
    assert s._suppress_save_reason == ""


def test_protection_flag_survives_singleton_reinstancing():
    """标志在实例上而非类上：不同实例互不影响"""
    a = _FakeSettings(Path("a.config"))
    b = _FakeSettings(Path("b.config"))
    a._suppress_save_reason = "sync-merge-pending-reload"
    assert b._suppress_save_reason == ""


# ── 真实 Settings.save() 的守卫（非替身，确认真实代码路径生效）──


@pytest.fixture()
def real_settings(qapp, tmp_path, monkeypatch):
    """隔离的真实 Settings 实例"""
    from app.utils import secret_store as ss
    from app.utils.config import Settings

    try:
        _ = Settings.secret_mode.value
    except RuntimeError:
        pytest.skip("Qt 对象已被同会话内的其他测试销毁")

    monkeypatch.setattr(ss, "_WIN", False)
    monkeypatch.setattr(ss, "_load_keyring", lambda: None)
    ss.SecretStore._instance = None
    monkeypatch.setattr(Settings, "_migrate_saved_providers", staticmethod(lambda *a, **k: None))

    inst = Settings()
    inst.file = tmp_path / "app.config"
    inst._suppress_save_reason = ""
    yield inst
    inst._suppress_save_reason = ""
    ss.SecretStore._instance = None


def test_real_save_dropped_during_merge_protection(real_settings):
    """真实 Settings.save()：保护期间不得触碰磁盘"""
    real_settings.file.write_text('{"merged": true}', encoding="utf-8")
    before = real_settings.file.read_bytes()

    real_settings._suppress_save_reason = "sync-merge-pending-reload"
    real_settings.save()

    assert real_settings.file.read_bytes() == before, "保护期间 save() 覆盖了磁盘上的合并结果"


def test_real_save_resumes_after_release(real_settings):
    """真实 Settings.save()：解除保护后恢复写入"""
    real_settings.file.write_text("{}", encoding="utf-8")

    real_settings._suppress_save_reason = "sync-merge-pending-reload"
    real_settings.save()
    assert real_settings.file.read_text(encoding="utf-8") == "{}"

    real_settings._suppress_save_reason = ""
    real_settings.save()
    written = real_settings.file.read_text(encoding="utf-8")
    assert written.strip() not in ("", "{}"), "解除保护后 save() 仍未正常写盘"


def test_real_suppress_save_context_manager(real_settings):
    """真实 Settings.suppress_save()：块内丢弃、块外恢复"""
    real_settings.file.write_text('{"keep": true}', encoding="utf-8")
    before = real_settings.file.read_bytes()

    with real_settings.suppress_save("test-window"):
        assert real_settings._suppress_save_reason == "test-window"
        real_settings.save()
        assert real_settings.file.read_bytes() == before

    assert real_settings._suppress_save_reason == ""

