# -*- coding: utf-8 -*-
"""服务商列表搜索框回归测试（P0-1）

覆盖：
1. 命中显示名（用户填的「配置名称」）过滤；
2. 命中 provider_name（插件声明的服务商名）过滤；
3. 无匹配 → 空态提示显示、行全部消失；
4. 清空关键词 → 恢复全部行与组头。

补充锁定：
- 150ms 防抖（输入后立即重建视为回归：列表会随每次按键全量重建）；
- 搜索框与空态标签在重建后仍留在 viewLayout（常驻控件被 takeAt 摘掉的回归）。
"""

import pytest
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QLabel

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


@pytest.fixture()
def card(monkeypatch, qapp):
    """构造带 4 条服务商配置的列表卡（注册表 + qconfig 隔离）"""
    from qfluentwidgets import ConfigItem

    from app.widgets.cards.settings import provider_setting_card as mod

    providers = {
        "cid_ds": {"provider_name": "DeepSeek", "name": "我的深度求索", "模型名称": "deepseek-chat"},
        "cid_cb": {"provider_name": "CodeBuddy", "模型名称": "auto"},
        "cid_zp": {"provider_name": "智谱AI", "模型名称": "glm-5.3"},
        "cid_ol": {"provider_name": "Ollama", "API_URL": "http://localhost:11434/v1"},
    }
    reg = ProviderRegistry()
    for p in (
        ProviderDef(name="DeepSeek"),
        ProviderDef(name="CodeBuddy", capabilities={"login_hook": lambda: None}),
        ProviderDef(name="智谱AI", coding_plan_fetcher=lambda c: None),
        ProviderDef(name="Ollama"),
    ):
        reg.register(p, source="plugin:test")
    monkeypatch.setattr(ProviderRegistry, "_instance", reg)
    monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))

    cfg_item = ConfigItem("Test", "SavedProviders", {})
    default_item = ConfigItem("Test", "SelectedModel", "")
    orig_get = mod.qconfig.get

    def _fake_get(item):
        if item is cfg_item:
            return providers
        if item is default_item:
            return ""
        return orig_get(item)  # qfluentwidgets 内部 item 必须透传

    monkeypatch.setattr(mod.qconfig, "get", _fake_get)
    monkeypatch.setattr(mod.qconfig, "set", lambda *a, **k: None)

    card = mod.ProviderListSettingCard(
        icon=QIcon(),
        configItem=cfg_item,
        defaultProviderItem=default_item,
        title="已保存的服务商",
    )
    monkeypatch.setattr(mod.qconfig, "get", orig_get)
    return card


def _visible_names(card) -> list[str]:
    """当前布局中处于展示态的服务商显示名

    ⚠️ 用 not isHidden() 而非 isVisible()：测试不 show 顶层窗口，
    isVisible() 在整个父链未显示时恒为 False，区分不出「被过滤掉」与「窗体未显示」。
    """
    from app.widgets.cards.settings.provider_setting_card import ProviderItem

    names = []
    for i in range(card.viewLayout.count()):
        w = card.viewLayout.itemAt(i).widget()
        if isinstance(w, ProviderItem) and not w.isHidden():
            names.append(w.nameLabel.text())
    return names


def _search(card, text: str):
    """输入关键词并立即触发防抖到期（跳过 150ms 等待）"""
    card._search_bar.setText(text)
    card._search_timer.stop()
    card._apply_filter()


def qtbot_wait(timer, timeout_ms: int = 600):
    """真实等 QTimer 到期：跑事件循环直到定时器停表（超时则断言失败）"""
    from PyQt5.QtCore import QEventLoop, QTimer
    from PyQt5.QtWidgets import QApplication

    loop = QEventLoop()
    timer.timeout.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec_()
    # 让 timeout 槽（含 Qt 队列投递）跑完
    QApplication.processEvents()
    assert not timer.isActive(), "定时器未在超时前停表"


class TestSearchFilter:
    def test_match_display_name(self, card):
        """命中显示名：输入「深度」应只剩 DeepSeek 那条（其 name 为「我的深度求索」）"""
        _search(card, "深度")
        assert _visible_names(card) == ["我的深度求索"]

    def test_match_provider_name(self, card):
        """命中 provider_name：输入「zhipu」不中，输入「智谱」应只剩智谱AI"""
        _search(card, "智谱")
        assert _visible_names(card) == ["智谱AI"]

    def test_match_model_name(self, card):
        """命中模型名称：输入「glm」只剩智谱AI（模型名 glm-5.3）"""
        _search(card, "glm")
        assert _visible_names(card) == ["智谱AI"]

    def test_no_match_shows_empty_state(self, card):
        """无匹配：行全部消失 + 空态提示可见"""
        _search(card, "zzz-not-exist")
        assert _visible_names(card) == []
        assert card._empty_label is not None
        assert not card._empty_label.isHidden()
        assert "zzz-not-exist" in card._empty_label.text()

    def test_clear_keyword_restores_all(self, card):
        """清空关键词：全部行恢复，空态消失，组头重新出现"""
        _search(card, "zzz-not-exist")
        assert _visible_names(card) == []

        _search(card, "")
        assert sorted(_visible_names(card)) == sorted(["我的深度求索", "CodeBuddy", "智谱AI", "Ollama"])
        assert card._empty_label is not None and card._empty_label.isHidden()
        assert len(card._group_headers) == 4  # OAuth / Coding Plan / 本地 / API


class TestSearchInfrastructure:
    def test_debounce_interval_is_150ms(self, card):
        """防抖间隔固定 150ms（与技能卡同款）；输入不立即重建"""
        assert card._search_timer.interval() == 150
        assert card._search_timer.isSingleShot()

        # 输入后立即读取：防抖未到期前不应重建
        card._search_bar.setText("智谱")
        assert sorted(_visible_names(card)) == sorted(["我的深度求索", "CodeBuddy", "智谱AI", "Ollama"])

    def test_debounce_fires_exactly_once(self, card):
        """到期后恰好触发一次 _apply_filter（不是零次，也不是每次按键都跑）

        计时方式：直接数 `_search_timer.timeout` 的发射次数。定时器在建卡时已把
        `_apply_filter` 绑成槽，monkeypatch 实例属性对已连接的 bound method 无效。
        """
        fired = []
        card._search_timer.timeout.connect(lambda: fired.append(1))

        # 连打三个字符：定时器被反复重启，到期后只应触发一次
        card._search_bar.setText("智")
        card._search_bar.setText("智谱")
        card._search_bar.setText("智谱AI")
        assert fired == [], "防抖期间不得触发重建"

        qtbot_wait(card._search_timer, 600)
        assert len(fired) == 1, f"到期后应恰好触发一次，实际 {len(fired)} 次"
        assert _visible_names(card) == ["智谱AI"], "过滤结果应生效"

    def test_empty_label_text_switches_by_context(self, card):
        """R1-3：无关键词时文案是「暂无服务商」，有词时才是「未找到匹配…」"""
        _search(card, "zzz")
        assert "未找到匹配「zzz」" in card._empty_label.text()

        # 清空关键词 + 清空服务商列表 → 空态文案不应出现空引号
        card.providers = {}
        _search(card, "")
        assert not card._empty_label.isHidden()
        assert card._empty_label.text() == "暂无服务商", f"实际 {card._empty_label.text()!r}"

    def test_search_bar_survives_rebuild(self, card):
        """回归：搜索框不得在重建中被摘出布局（否则用户无法继续输入）"""
        _search(card, "智谱")
        assert card._search_bar.parent() is not None
        assert any(card.viewLayout.itemAt(i).widget() is card._search_bar for i in range(card.viewLayout.count())), (
            "搜索框必须仍是 viewLayout 成员"
        )

    def test_empty_label_survives_rebuild(self, card):
        """回归：空态标签重建后仍可复用，不得被 deleteLater"""
        _search(card, "zzz")
        assert not card._empty_label.isHidden()
        _search(card, "")
        assert card._empty_label.isHidden()
        _search(card, "zzz")
        assert not card._empty_label.isHidden()
