# -*- coding: utf-8 -*-
"""回归测试：服务商编辑卡「获取模型列表」的 models_hook 调用链

修复背景（2026-09）：CodeBuddy 走 capabilities["models_hook"] 自定义获取，
插件实现为 _fetch_models(config)，而 UI 侧 _do_fetch_thread 零参调用 →
TypeError 在线程内炸掉 → fetchSuccess/fetchFailed 信号都不发 →
按钮停在禁用态、无任何提示（用户表现为「一直卡住获取不到」）。

覆盖：
1. models_hook 收到当前表单值（API_KEY 等），不再零参调用。
2. hook 抛异常时发 fetchFailed(reason) 且按钮恢复可用（状态机不泄漏）。
3. hook 返回空列表时同样走 fetchFailed，不误报成功。
4. 失败原因透传到 InfoBar 文案（不再是一句笼统的「请检查配置」）。
"""

import sys
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    from PyQt5.QtCore import QCoreApplication

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture()
def card_with_hook(monkeypatch):
    """构造挂有 models_hook 的编辑卡（绕过真实服务商注册表）。"""
    from app.widgets.cards.settings import provider_edit_card as mod

    def _make(hook):
        provider = ProviderDef(name="测试服务商", capabilities={"models_hook": hook})
        reg = ProviderRegistry()
        reg.register(provider, source="plugin:test")
        monkeypatch.setattr(ProviderRegistry, "_instance", reg)
        monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
        # 无 API URL 时 provider_default_config 可能返回 None，卡片自身容错
        return mod.ProviderEditCard(provider_name="测试服务商", provider_info={}, is_new=False)

    return _make


def _wait_threads(card, timeout_ms=5000):
    """等后台获取线程把信号投递回主线程（queued connection 需事件循环）。"""
    from PyQt5.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    card.fetchSuccess.connect(loop.quit)
    card.fetchFailed.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec_()


def test_models_hook_receives_form_values(card_with_hook, monkeypatch):
    """models_hook 被调用时必须收到含 API_KEY 的 config（回归：零参调用 TypeError）"""
    captured = {}

    def hook(config):
        captured["config"] = config
        return ["model-a", "model-b"]

    card = card_with_hook(hook)
    card.apiKeyEdit.setText("sk-test-key")
    card._on_fetch_models()
    _wait_threads(card)

    assert "config" in captured, "hook 未被调用（签名不匹配会在此处暴露）"
    assert captured["config"].get("API_KEY") == "sk-test-key"
    assert "API_URL" in captured["config"]
    assert card.fetchBtn.isEnabled(), "成功后按钮应恢复可用"


def test_models_hook_exception_emits_failure_and_reenables_button(card_with_hook):
    """hook 抛异常：发 fetchFailed + 按钮恢复可用（回归：线程静默死亡导致卡死）"""

    def hook(config):
        raise RuntimeError("尚未登录：请先点击「自动登录」")

    card = card_with_hook(hook)
    card.apiKeyEdit.setText("rt-test")
    reason = {}
    card.fetchFailed.connect(lambda r: reason.setdefault("value", r))

    card._on_fetch_models()
    assert not card.fetchBtn.isEnabled(), "获取中按钮应禁用"
    _wait_threads(card)

    assert "value" in reason, "异常必须转成 fetchFailed 信号（否则线程静默死亡）"
    assert "尚未登录" in reason["value"], "失败原因应透传"
    assert card.fetchBtn.isEnabled(), "失败后按钮必须恢复可用，否则永久卡住"


def test_models_hook_empty_result_emits_failure(card_with_hook):
    """hook 返回空列表：走失败分支，不误报成功"""

    def hook(config):
        return []

    card = card_with_hook(hook)
    card.apiKeyEdit.setText("rt-test")
    reason = {}
    card.fetchFailed.connect(lambda r: reason.setdefault("value", r))

    card._on_fetch_models()
    _wait_threads(card)

    assert reason.get("value") == "", "空结果应发空原因"
    assert card.fetchBtn.isEnabled()


def test_models_hook_success_updates_combo(card_with_hook):
    """成功路径：模型列表写入下拉框"""

    def hook(config):
        return ["glm-5.3", "hy3"]

    card = card_with_hook(hook)
    card.apiKeyEdit.setText("rt-test")
    card._on_fetch_models()
    _wait_threads(card)

    items = [card.modelCombo.itemText(i) for i in range(card.modelCombo.count())]
    assert "glm-5.3" in items
    assert "hy3" in items


def test_on_fetch_failed_shows_reason(monkeypatch):
    """失败提示应带插件给出的原因（不再是一句笼统文案）"""
    from app.widgets.cards.settings import provider_edit_card as mod

    provider = ProviderDef(name="测试服务商", capabilities={"models_hook": lambda c: []})
    reg = ProviderRegistry()
    reg.register(provider, source="plugin:test")
    monkeypatch.setattr(ProviderRegistry, "_instance", reg)
    monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))

    card = mod.ProviderEditCard(provider_name="测试服务商", provider_info={}, is_new=False)
    shown = {}

    import qfluentwidgets

    def fake_error(title, content, **kwargs):
        shown["title"] = title
        shown["content"] = content

    monkeypatch.setattr(qfluentwidgets.InfoBar, "error", staticmethod(fake_error))
    card._on_fetch_failed("刷新失败: {'code': 401}")

    assert shown["content"] == "刷新失败: {'code': 401}"


def test_codebuddy_hook_signature_accepts_config():
    """插件侧契约：codebuddy._fetch_models 接受 config 参数（UI 已按此调用）"""
    import importlib.util
    import inspect
    from pathlib import Path

    path = Path("plugins/system-providers/providers/codebuddy.py")
    if not path.exists():
        pytest.skip("codebuddy provider 插件未安装")
    spec = importlib.util.spec_from_file_location("_cb_sig_check", path)
    src = path.read_text(encoding="utf-8").replace(
        "from app.plugins.registries.provider_registry import ProviderDef", "ProviderDef = None"
    )
    module = importlib.util.module_from_spec(spec)
    exec(compile(src, str(path), "exec"), module.__dict__)  # noqa: S102
    params = list(inspect.signature(module._fetch_models).parameters)
    assert params and params[0] == "config", "UI 侧按位置传入 config，首参名必须为 config"


# ────────────────────────────────────────────────────────────
# 模型列表编辑升级：增量合并 / 保留未知键 / 写入隐藏与别名
# ────────────────────────────────────────────────────────────


def test_fetch_preserves_manual_edits(card_with_hook):
    """拉取模型后，用户手改的内容必须保留（回归：旧实现 clear 覆盖）"""

    def hook(config):
        return ["glm-5", "hy3"]

    card = card_with_hook(hook)
    card.apiKeyEdit.setText("rt-test")
    # 用户手工加了一个自定义模型
    card._set_combo_models(["我手加的模型"], keep_current=False)

    card._on_fetch_models()
    _wait_threads(card)

    items = [card.modelCombo.itemText(i) for i in range(card.modelCombo.count())]
    assert "我手加的模型" in items, "拉取不应覆盖用户手改的条目"
    assert "glm-5" in items and "hy3" in items, "拉取的新模型应追加进来"


def test_fetch_does_not_duplicate_existing(card_with_hook):
    """重复拉取不产生重复条目"""

    def hook(config):
        return ["glm-5", "hy3"]

    card = card_with_hook(hook)
    card.apiKeyEdit.setText("rt-test")
    card._on_fetch_models()
    _wait_threads(card)
    card._on_fetch_models()
    _wait_threads(card)

    items = [card.modelCombo.itemText(i) for i in range(card.modelCombo.count())]
    assert items.count("glm-5") == 1
    assert items.count("hy3") == 1


def test_save_preserves_unknown_keys(card_with_hook):
    """保存时必须保留未在表单里体现的键（如模型能力覆盖）。

    回归：旧实现从零重建 provider_info，未携带的键全部丢失。
    """

    def hook(config):
        return []

    info = {
        "API_URL": "http://x",
        "API_KEY": "k",
        "模型名称": "glm-5",
        "模型能力覆盖": {"glm-5": {"支持思考": True}},
        "config_id": "abc",
    }
    card = card_with_hook(hook)
    card.provider_info = dict(info)
    card.apiKeyEdit.setText("k")
    captured = {}
    card.saved.connect(lambda name, payload: captured.update(payload))
    card._on_save()

    assert captured.get("模型能力覆盖") == {"glm-5": {"支持思考": True}}, "未知键在保存时必须保留"


def test_save_writes_hidden_and_aliases(card_with_hook):
    """保存时写入新增的隐藏/别名两个键"""

    def hook(config):
        return []

    info = {"API_URL": "http://x", "API_KEY": "k", "模型名称": "a", "模型列表": ["a", "b"]}
    card = card_with_hook(hook)
    card.provider_info = dict(info)
    card.apiKeyEdit.setText("k")
    # 模拟真实流程：展开编辑器 → 改数据 → 收起（收起时收集进 provider_info）
    card.modelListEditor.set_models(["a", "b"], hidden=["b"], aliases={"a": "甲"})
    card.modelListEditor.setVisible(True)
    card._collect_editor_result()
    captured = {}
    card.saved.connect(lambda name, payload: captured.update(payload))
    card._on_save()

    assert captured.get("模型隐藏") == ["b"]
    assert captured.get("模型别名") == {"a": "甲"}


def test_editor_collapse_persists_edits(card_with_hook):
    """收起编辑器时改动即持久到 provider_info（不必等到保存）"""

    def hook(config):
        return []

    card = card_with_hook(hook)
    card.provider_info = {"模型列表": ["a", "b"], "模型名称": "a"}
    card.modelListEditor.set_models(["a", "b"], hidden=["b"], aliases={"a": "甲"})
    card.modelListEditor.setVisible(True)
    card._on_manage_models()  # 收起

    assert not card.modelListEditor.isVisible()
    assert card.provider_info.get("模型隐藏") == ["b"]
    assert card.provider_info.get("模型别名") == {"a": "甲"}


def test_save_omits_empty_hidden_and_aliases(card_with_hook):
    """无隐藏/别名时不写入这两个键（保持配置干净）"""

    def hook(config):
        return []

    info = {"API_URL": "http://x", "API_KEY": "k", "模型名称": "a", "模型列表": ["a"]}
    card = card_with_hook(hook)
    card.provider_info = dict(info)
    card.apiKeyEdit.setText("k")
    captured = {}
    card.saved.connect(lambda name, payload: captured.update(payload))
    card._on_save()

    assert "模型隐藏" not in captured
    assert "模型别名" not in captured
