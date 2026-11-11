# -*- coding: utf-8 -*-
"""回归：服务商改 API_KEY 后配置 id 变化，当前对话的模型选择不得被重置。

根因：config_id = sha256(API_URL + "\\x00" + API_KEY)[:8]（provider_profile），
改 key 必然换 id（旧条目删除、按新 hash 落新条目）。原
`GlobalCardController._on_provider_edit_saved` 的迁移判定写成
`saved_providers.get(new_config_id) is provider_info`，而 apply_provider_save
落盘时按 merge 语义新建了 dict —— 该判定恒 False，迁移静默失效：窗口
`_current_provider_name` 与全局 `llm_selected_model` 都停在旧 id，随后
`_load_model_configs` 判定「当前选择已失效」，回退到服务商列表第一项。

覆盖：
1. controller 保存回调把窗口选择 + 全局默认模型迁到新 id；
2. 与本次编辑无关的窗口/默认选择不受影响；
3. 全链路：迁移后 `_load_model_configs` 保持原模型；未迁移的旧 id 仍走
   「失效回退首项」兜底（语义不被破坏）。
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.modelmeta.provider_profile import compute_provider_config_id

API_URL = "https://api.test.example"
OLD_KEY = "sk-old"
NEW_KEY = "sk-new"
MODEL = "model-x"


class _Item:
    """最小 ConfigItem：保存链只读 .value"""

    def __init__(self, value):
        self.value = value


class _FakeCfg:
    """只实现保存链路用到的配置语义，不触真实 app.config"""

    def __init__(self, providers: dict, selected: str = ""):
        self.llm_saved_providers = _Item(providers)
        self.llm_selected_model = _Item(selected)
        # 是否调用 save=True（落盘意图）在这里不是断言重点，记录即可
        self.writes = []

    def set(self, item, value, save=False):
        item.value = value
        self.writes.append((item, value, save))


class _FakeWin:
    def __init__(self, provider: str, model: str):
        self._current_provider_name = provider
        self._current_model_name = model
        self._load_calls = 0

    def _load_model_configs(self):
        self._load_calls += 1


def _entry(key: str) -> dict:
    """构造一条服务商配置（config_id 由 (URL, KEY) hash 决定）"""
    cid = compute_provider_config_id({"API_URL": API_URL, "API_KEY": key})
    return {
        "config_id": cid,
        "provider_name": "TestProvider",
        "name": "TestProvider",
        "API_URL": API_URL,
        "API_KEY": key,
        "模型名称": MODEL,
        "模型列表": [MODEL],
    }


@pytest.fixture()
def cc(_qapp, monkeypatch):
    """最小可用的 GlobalCardController：CardManager / Settings 全部打桩"""
    from unittest.mock import MagicMock

    from app.utils.config import Settings
    from app.widgets.cards.global_card_controller import GlobalCardController

    monkeypatch.setattr("app.widgets.cards.card_manager.CardManager.get_instance", lambda: MagicMock())
    monkeypatch.setattr(Settings, "get_instance", classmethod(lambda cls: _FakeCfg({})))
    monkeypatch.setattr(
        "qfluentwidgets.InfoBar.success", lambda *a, **kw: None, raising=False
    )

    from PySide6.QtWidgets import QWidget

    controller = GlobalCardController(QWidget(), MagicMock())
    controller._close_edit_card = lambda card_id: None
    return controller


@pytest.fixture(scope="module")
def _qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    return app


def test_key_change_migrates_window_selection(cc):
    """改 key → 窗口当前服务商迁到新 id，模型名保持不变"""
    entry = _entry(OLD_KEY)
    old_id = entry["config_id"]
    new_id = compute_provider_config_id({"API_URL": API_URL, "API_KEY": NEW_KEY})
    assert old_id != new_id, "前提：(URL, KEY) 变化必须换 id"

    cc.cfg = _FakeCfg({old_id: dict(entry)}, selected=old_id)
    win = _FakeWin(old_id, MODEL)
    cc._all_windows = lambda: [win]

    cc._on_provider_edit_saved("TestProvider", dict(entry, API_KEY=NEW_KEY), is_new=False)

    saved = cc.cfg.llm_saved_providers.value
    assert new_id in saved and old_id not in saved, "旧条目应被新 hash 条目取代"
    assert win._current_provider_name == new_id, "窗口选中服务商必须跟随新 id，否则被重置首项"
    assert win._current_model_name == MODEL, "模型名不应被改动"
    assert cc.cfg.llm_selected_model.value == new_id, "全局默认模型也要跟随新 id（重启后不丢）"
    assert win._load_calls == 1, "保存后应广播一次配置重载"


def test_end_to_end_save_then_reload_keeps_model(cc):
    """保存 → 配置重载全链路：模型选择保持（修复前后的分水岭）

    controller 末尾会真的调一次 `_load_model_configs`，用真实方法验证
    「迁移后的新 id 能命中配置 → 不走回退首项分支」。
    """
    entry = _entry(OLD_KEY)
    old_id = entry["config_id"]
    new_id = compute_provider_config_id({"API_URL": API_URL, "API_KEY": NEW_KEY})
    # 前面摆一条别的服务商：_load_model_configs 的失效兑底会回退到它（列表首项），
    # 这样才分得清「保持原模型」与「回退首项」两个结果。
    first = {
        "config_id": "first-id",
        "provider_name": "FirstProvider",
        "API_URL": "https://api.first.example",
        "API_KEY": "sk-first",
        "模型名称": "model-first",
        "模型列表": ["model-first"],
    }
    cfg = _FakeCfg({"first-id": dict(first), old_id: dict(entry)}, selected=old_id)
    inst = _make_load_stub(cfg, old_id, MODEL)

    cc.cfg = cfg
    cc._all_windows = lambda: [inst]

    cc._on_provider_edit_saved("TestProvider", dict(entry, API_KEY=NEW_KEY), is_new=False)

    assert inst._current_provider_name == new_id, "保存后窗口选择应已迁到新 id"
    assert (inst._current_provider_name, inst._current_model_name) == (new_id, MODEL), (
        "重载后模型选择不得被重置"
    )


def test_other_provider_selection_untouched(cc):
    """编辑 A 服务商不影响选中 B 的窗口与全局默认"""
    entry = _entry(OLD_KEY)
    old_id = entry["config_id"]
    other = {
        "config_id": "other-id",
        "provider_name": "OtherProvider",
        "name": "OtherProvider",
        "API_URL": "https://api.other.example",
        "API_KEY": "sk-other",
        "模型名称": "model-y",
        "模型列表": ["model-y"],
    }
    cc.cfg = _FakeCfg({old_id: dict(entry), "other-id": dict(other)}, selected="other-id")
    win = _FakeWin("other-id", "model-y")
    cc._all_windows = lambda: [win]

    cc._on_provider_edit_saved("TestProvider", dict(entry, API_KEY=NEW_KEY), is_new=False)

    assert win._current_provider_name == "other-id"
    assert win._current_model_name == "model-y"
    assert cc.cfg.llm_selected_model.value == "other-id"


# ══════════════════════════════════════════════════════════════════
# 全链路：_load_model_configs 保持/回退语义
# ══════════════════════════════════════════════════════════════════


def _make_load_stub(cfg, provider: str, model: str, valid: dict | None = None):
    """OpenAIChatToolWindow.__new__ 桩，只跑 _load_model_configs 主逻辑"""
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    inst._is_destroyed = False
    inst._valid_configs = dict(valid or {})
    inst._current_provider_name = provider
    inst._current_model_name = model
    inst._pending_model_restore = None
    inst._user_manually_selected_model = True
    inst.cfg = cfg
    inst._update_model_selector_btn = lambda: None
    inst._refresh_context_usage_indicator = lambda: None
    return inst


def test_load_model_configs_keeps_migrated_selection():
    """迁移后的新 id 在配置里 → 保持当前模型（这是本次修复的落点）"""
    entry = _entry(OLD_KEY)
    new_id = compute_provider_config_id({"API_URL": API_URL, "API_KEY": NEW_KEY})
    cfg = _FakeCfg({new_id: dict(entry, config_id=new_id, API_KEY=NEW_KEY)}, selected=new_id)

    inst = _make_load_stub(cfg, new_id, MODEL)
    inst._load_model_configs()

    assert (inst._current_provider_name, inst._current_model_name) == (new_id, MODEL)


def test_load_model_configs_stale_id_still_falls_back():
    """对照：真正失效的旧 id（服务商被删）仍回退列表首项 —— 兜底语义保留"""
    first_id = compute_provider_config_id({"API_URL": API_URL, "API_KEY": NEW_KEY})
    second = {
        "config_id": "second-id",
        "provider_name": "SecondProvider",
        "API_URL": "https://api.second.example",
        "API_KEY": "sk-second",
        "模型名称": "model-z",
        "模型列表": ["model-z"],
    }
    cfg = _FakeCfg(
        {
            first_id: dict(_entry(NEW_KEY), config_id=first_id),
            "second-id": second,
        },
        selected="",
    )

    inst = _make_load_stub(cfg, "deleted-id", MODEL)
    inst._load_model_configs()

    assert inst._current_provider_name == first_id, "选择失效时应回退配置列表第一项"


# ══════════════════════════════════════════════════════════════
# id 漂移治愈：旧 id 消失但服务商还在（改 key / 解锁迁移 / 云同步）
# ══════════════════════════════════════════════════════════════


def test_load_model_configs_heals_drifted_id_by_provider_name():
    """旧 id 不在配置里，但旧快照的 provider_name 唯一命中新条目 → 跟随新 id 并保留模型"""
    old_entry = _entry(OLD_KEY)  # provider_name=TestProvider, name=TestProvider
    new_id = compute_provider_config_id({"API_URL": API_URL, "API_KEY": NEW_KEY})
    other = {
        "config_id": "other-id",
        "provider_name": "OtherProvider",
        "API_URL": "https://api.other.example",
        "API_KEY": "sk-other",
        "模型名称": "model-y",
        "模型列表": ["model-y"],
    }
    cfg = _FakeCfg(
        {
            "other-id": dict(other),
            new_id: dict(old_entry, config_id=new_id, API_KEY=NEW_KEY),
        },
        selected="",
    )

    stub = _make_load_stub(cfg, old_entry["config_id"], MODEL, valid={old_entry["config_id"]: old_entry})
    stub._load_model_configs()

    assert (stub._current_provider_name, stub._current_model_name) == (new_id, MODEL), (
        "同一服务商换 id 后应跟随新 id 并保留原模型，而不是回退首项"
    )


def test_load_model_configs_ambiguous_provider_falls_back():
    """同名服务商多条且无法用配置名收窄 → 不猜，仍走回退首项"""
    first = {
        "config_id": "first-id",
        "provider_name": "DupProvider",
        "name": "Dup-A",
        "API_URL": "https://api.a.example",
        "API_KEY": "sk-a",
        "模型名称": "model-a",
        "模型列表": ["model-a"],
    }
    second = {
        "config_id": "second-id",
        "provider_name": "DupProvider",
        "name": "Dup-B",
        "API_URL": "https://api.b.example",
        "API_KEY": "sk-b",
        "模型名称": "model-b",
        "模型列表": ["model-b"],
    }
    cfg = _FakeCfg({"first-id": dict(first), "second-id": dict(second)}, selected="")
    old_info = {
        "config_id": "gone-id",
        "provider_name": "DupProvider",
        "name": "",
        "API_URL": "https://api.gone.example",
        "API_KEY": "sk-gone",
        "模型名称": MODEL,
    }

    stub = _make_load_stub(cfg, "gone-id", MODEL, valid={"gone-id": old_info})
    stub._load_model_configs()

    assert stub._current_provider_name == "first-id", "多匹配不明确时不得猜，回退首项"
