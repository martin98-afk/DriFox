# -*- coding: utf-8 -*-
"""配置迁移基建（P0-7）回归测试

覆盖 Settings._run_migrations 的四条纪律：
1. 无变化不写盘（迁移项返回 False 时不得触发 save）；
2. locked 跳过（密码模式未解锁时不重算 config_id）；
3. 单项异常被隔离：不炸启动，且后续迁移仍继续执行；
4. 幂等：跑两次，第二次不写盘。

隔离手段：用 _FakeInstance 走纯逻辑（与 tests/utils/test_default_opencode_provider.py
同款），不依赖 Qt/Settings 单例；_MIGRATIONS 被 monkeypatch 成局部列表。
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.utils import config as config_mod
from app.utils.config import Settings


class _FakeConfigItem:
    def __init__(self, value):
        self.value = value


class _FakeInstance:
    """模拟 Settings 实例：只含迁移关心的字段，save() 计数"""

    def __init__(self, saved_providers=None, selected="", locked=False):
        self.llm_saved_providers = _FakeConfigItem(saved_providers if saved_providers is not None else {})
        self.llm_selected_model = _FakeConfigItem(selected)
        self.secrets_locked = locked
        self.save_calls = 0

    def save(self):
        self.save_calls += 1


def _migration_ready_providers():
    """已迁移形态：key 与 value.config_id 一致（走 apply_provider_save 应无变化）"""
    from app.core.modelmeta.provider_profile import compute_provider_config_id

    info = {
        "provider_name": "DeepSeek",
        "API_URL": "https://api.deepseek.com",
        "API_KEY": "sk-test",
    }
    cid = compute_provider_config_id(info)
    info["config_id"] = cid
    return {cid: info}


def _legacy_providers():
    """旧形态：key 是 provider_name，value 无 config_id"""
    return {
        "DeepSeek": {
            "provider_name": "DeepSeek",
            "API_URL": "https://api.deepseek.com",
            "API_KEY": "sk-test",
        }
    }


@pytest.fixture()
def isolated_migrations(monkeypatch):
    """把注册表换成可注入的局部列表（不影响真实启动链）"""
    table: list = []
    monkeypatch.setattr(config_mod, "_MIGRATIONS", table)
    return table


# ══════════════════════════════════════════════════════════════════
# 1. 已迁移不写盘
# ══════════════════════════════════════════════════════════════════


def test_already_migrated_does_not_save(monkeypatch):
    """配置已是新格式：迁移项返回 False，且不触发任何 save"""
    instance = _FakeInstance(saved_providers=_migration_ready_providers())
    saved_calls = {"n": 0}
    monkeypatch.setattr(instance, "save", lambda: saved_calls.__setitem__("n", saved_calls["n"] + 1))

    monkeypatch.setattr(config_mod, "_MIGRATIONS", [config_mod._migrate_saved_providers_entry])
    changed = Settings._run_migrations(instance)

    assert changed is False, "无变化时聚合结果应为 False"
    assert saved_calls["n"] == 0, "无变化时不得写盘"
    # 内存值原样保留
    assert instance.llm_saved_providers.value == _migration_ready_providers()


# ══════════════════════════════════════════════════════════════════
# 2. locked 跳过
# ══════════════════════════════════════════════════════════════════


def test_locked_skips_migration(monkeypatch):
    """密码模式未解锁：跳过 config_id 重算（密文 hash 会漂移），不写盘"""
    instance = _FakeInstance(saved_providers=_legacy_providers(), locked=True)
    saved_calls = {"n": 0}
    monkeypatch.setattr(instance, "save", lambda: saved_calls.__setitem__("n", saved_calls["n"] + 1))

    monkeypatch.setattr(config_mod, "_MIGRATIONS", [config_mod._migrate_saved_providers_entry])
    changed = Settings._run_migrations(instance)

    assert changed is False
    assert saved_calls["n"] == 0, "locked 期间不得写盘（否则会把密文 hash 漂移落盘）"
    # 旧形态原样保留（key 仍是 provider_name）
    assert list(instance.llm_saved_providers.value.keys()) == ["DeepSeek"]


def test_locked_flag_absent_treated_as_unlocked(monkeypatch):
    """实例无 secrets_locked 属性（老替身/非常规实例）→ 按未锁定处理，正常迁移"""

    class _NoFlag(_FakeInstance):
        def __init__(self, **kw):
            super().__init__(**kw)
            del self.secrets_locked

    instance = _NoFlag(saved_providers=_legacy_providers())
    monkeypatch.setattr(config_mod, "_MIGRATIONS", [config_mod._migrate_saved_providers_entry])
    assert Settings._run_migrations(instance) is True


# ══════════════════════════════════════════════════════════════════
# 3. 单项异常隔离
# ══════════════════════════════════════════════════════════════════


def test_exception_isolated_and_later_migrations_still_run(isolated_migrations):
    """某项抛异常 → 不炸启动；后续迁移照常执行，异常项的改动不计入 changed"""
    ran = []

    def _boom(instance):
        ran.append("boom")
        raise RuntimeError("模拟迁移失败")

    def _ok(instance):
        ran.append("ok")
        return True

    isolated_migrations.extend([_boom, _ok])

    changed = Settings._run_migrations(_FakeInstance())

    assert ran == ["boom", "ok"], "异常项之后的迁移必须继续执行"
    assert changed is True, "后续成功项的改动应计入聚合结果"
    assert isolated_migrations[0] is _boom  # 注册表本身不被破坏


def test_all_migrations_failing_returns_false(isolated_migrations):
    """全部迁移失败 → 聚合 False（不抛异常）"""

    def _boom(instance):
        raise ValueError("x")

    isolated_migrations.extend([_boom, _boom])
    assert Settings._run_migrations(_FakeInstance()) is False


# ══════════════════════════════════════════════════════════════════
# 4. 幂等
# ══════════════════════════════════════════════════════════════════


def test_migration_is_idempotent(monkeypatch):
    """跑两次：第一次迁移并写盘，第二次无变化不写盘"""
    instance = _FakeInstance(saved_providers=_legacy_providers())
    monkeypatch.setattr(config_mod, "_MIGRATIONS", [config_mod._migrate_saved_providers_entry])

    counter = {"n": 0}
    real_save = instance.save

    def _counting_save():
        counter["n"] += 1
        real_save()

    monkeypatch.setattr(instance, "save", _counting_save)

    first = Settings._run_migrations(instance)
    assert first is True, "旧格式应被迁移"
    assert counter["n"] == 1, "第一次应写盘一次"
    migrated = instance.llm_saved_providers.value
    # 迁移后形态：key == value.config_id
    assert all(k == v.get("config_id") for k, v in migrated.items())

    second = Settings._run_migrations(instance)
    assert second is False, "第二次无变化"
    assert counter["n"] == 1, "第二次不得再写盘"
    assert instance.llm_saved_providers.value == migrated


# ══════════════════════════════════════════════════════════════════
# 附加：注册表首项指向真实迁移（防误删 / 防指向空实现）
# ══════════════════════════════════════════════════════════════════


def test_registry_first_entry_points_to_real_migration():
    assert config_mod._MIGRATIONS, "注册表不得为空（服务商迁移必须仍在启动链上）"
    assert config_mod._MIGRATIONS[0] is config_mod._migrate_saved_providers_entry
    # 转发函数运行期解析类方法：monkeypatch 类方法后注册表条目同步失效（隔离有效）
    called = {"n": 0}
    original = Settings._migrate_saved_providers

    def _spy(instance):
        called["n"] += 1
        return False

    Settings._migrate_saved_providers = staticmethod(_spy)
    try:
        config_mod._MIGRATIONS[0](_FakeInstance())
    finally:
        Settings._migrate_saved_providers = original
    assert called["n"] == 1
