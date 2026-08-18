# -*- coding: utf-8 -*-
"""回归测试：精选 tab 在无缓存/远端未到达时不得把「全部」插件塞进一个框

复现背景：
- 打开插件市场（默认进入「精选」探索页）
- 清掉市场缓存（~/.drifox/cache/marketplaces/）或远端尚未到达
  → _all_plugins == []
- _render_local_installed() 用本地 extras 触发 _render_plugins([]) → _rebuild_explore()
- 历史实现：view_plugins = list(self._all_plugins) + self._build_local_extra_plugins()
  本地 extras（系统/手动安装/禁用）默认 categories=[] → 全部进入兜底"全部插件"分组
- 用户在精选 tab 看到"全部插件"分组里塞满了本地插件，误以为这是精选内容

修复：
- _rebuild_explore 只用 self._all_plugins（市场数据），不再混入本地 extras
- 精选数据为空时直接 return，不渲染任何分组
- _apply_browse_mode("featured") 在 _all_plugins 为空时切到空状态占位，
  提示「精选数据加载中…」，而不是显示空白或兜底分组
"""

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


def _app():
    from PyQt5.QtWidgets import QApplication

    return QApplication.instance()


def _pump(seconds=0.3):
    app = _app()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.01)


def _new_card(monkeypatch):
    """构造卡片：禁用所有市场源（_all_plugins 永远为空），模拟「无缓存/远端未到」

    Patch MarketplaceSourceManager.get_sources 返回空列表 → 不会拉取任何
    远程市场数据 → _all_plugins 始终保持 __init__ 时的空列表。
    """
    if str(PLUGIN_MARKETPLACE) not in sys.path:
        sys.path.insert(0, str(PLUGIN_MARKETPLACE))
    from ui.cards import MarketplaceCard
    from ui.installer import PluginInstaller
    from ui.marketplace_manager import MarketplaceSourceManager

    # 无市场源 → 远程永不返回
    monkeypatch.setattr(MarketplaceSourceManager, "get_sources", lambda self: [])
    monkeypatch.setattr(MarketplaceSourceManager, "fetch_marketplace", lambda self, src, force=False: {"name": "", "plugins": []})
    # 本地状态：模拟「装了 5 个本地插件但无 categories」（即会被旧实现兜底的场景）
    fake_local = {
        "local-plugin-a": "enabled",
        "local-plugin-b": "enabled",
        "local-plugin-c": "disabled",
        "local-plugin-d": "manual",
        "local-plugin-e": "enabled",
    }
    monkeypatch.setattr(PluginInstaller, "get_installed_map", lambda self, use_cache=True: {k: "1.0.0" for k in fake_local})
    monkeypatch.setattr(PluginInstaller, "get_status_map", lambda self, use_cache=True: fake_local)
    # 标记本地插件路径不存在（manifest 读不到 → categories=[]，命中旧实现兜底路径）
    import ui.cards as _cards_mod

    monkeypatch.setattr(
        _cards_mod._PluginRow,
        "_find_local_plugin_path",
        lambda name: None,
    )

    return MarketplaceCard()


def test_rebuild_explore_empty_market_data_no_fallback_section(monkeypatch):
    """精选 tab + _all_plugins=[] + 本地有插件 → _rebuild_explore 不渲染任何分组

    修复前：本地 extras 无 categories → 兜底渲染"全部插件"分组
    修复后：view_plugins 只看 _all_plugins → 空数据直接 return，_explore_sections 为空
    """
    card = _new_card(monkeypatch)
    card.show()

    # 直接调用 _rebuild_explore（_all_plugins 初始为空 → 模拟"无缓存/远端未到"）
    assert card._all_plugins == [], "前置条件：_all_plugins 应为空"
    card._rebuild_explore()

    # 核心断言：精选页不得有任何分组（修复前会有"全部插件"兜底分组）
    assert card._explore_sections == [], (
        f"_rebuild_explore 不应渲染任何分组，但 _explore_sections={len(card._explore_sections)} "
        f"（修复前会把本地 extras 兜底成'全部插件'分组）"
    )


def test_apply_browse_mode_featured_empty_data_shows_placeholder(monkeypatch):
    """精选 tab + _all_plugins=[] → _apply_browse_mode 切到空状态占位（不切到 _explore_scroll）

    修复前：featured 模式无条件切到 _explore_scroll → 用户看到空白或兜底"全部插件"框
    修复后：_all_plugins 为空时切到 _empty_label 并设置"精选数据加载中…"文案
    """
    card = _new_card(monkeypatch)
    card.show()

    assert card._all_plugins == [], "前置条件：_all_plugins 应为空"

    # 模拟"先离开精选 tab 再回来"——确保 currentItemChanged 信号会触发
    # （直接 setCurrentItem('featured') 在初始已是 featured 时不触发信号）
    card._filter_bar.setCurrentItem("all")
    _pump(0.2)
    card._filter_bar.setCurrentItem("featured")
    # setCurrentItem 触发 currentItemChanged → _on_filter_changed → _apply_browse_mode
    _pump(0.2)

    # 核心断言：content_stack 当前展示的是空状态占位（不是 _explore_scroll）
    current = card._content_stack.currentWidget()
    assert current is card._empty_label, (
        f"精选 tab 数据为空时应展示空状态占位，实际展示 {type(current).__name__}"
    )
    # 空状态文案应提示"加载中"（不是普通的"暂无可用插件"或"没有匹配的插件"）
    assert "加载中" in card._empty_label.text(), (
        f"空状态文案应含'加载中'，实际：{card._empty_label.text()}"
    )


def test_apply_browse_mode_featured_with_data_shows_explore(monkeypatch):
    """精选 tab + _all_plugins 有数据 → _apply_browse_mode 切到 _explore_scroll 正常渲染

    反向断言：修复不破坏正常流程——数据到位后精选页应正常展示探索视图。
    """
    from ui.cards import MarketplaceCard
    from ui.marketplace_manager import MarketplaceSourceManager
    from ui.installer import PluginInstaller

    if str(PLUGIN_MARKETPLACE) not in sys.path:
        sys.path.insert(0, str(PLUGIN_MARKETPLACE))

    # Patch：无市场源（避免 worker 拉取），但手动注入 _all_plugins
    monkeypatch.setattr(MarketplaceSourceManager, "get_sources", lambda self: [])
    monkeypatch.setattr(MarketplaceSourceManager, "fetch_marketplace", lambda self, src, force=False: {"name": "", "plugins": []})
    # _fill_explore_section 需要 _installed_set / _version_map / _status_map（_row_state 内部用）
    monkeypatch.setattr(PluginInstaller, "get_installed_map", lambda self, use_cache=True: {})
    monkeypatch.setattr(PluginInstaller, "get_status_map", lambda self, use_cache=True: {})

    card = MarketplaceCard()
    card.show()
    # 手动注入市场数据（含分类）→ _all_plugins 非空
    card._all_plugins = [
        {"name": f"market-plugin-{i}", "version": "1.0.0", "categories": ["tool"]}
        for i in range(10)
    ]
    # 初始化 _fill_explore_section 依赖的 row_state 缓存
    inst_map = PluginInstaller.get_installed_map(None)
    card._installed_set = set(inst_map)
    card._version_map = inst_map
    card._status_map = PluginInstaller.get_status_map(None)
    card._rebuild_explore()
    _pump(0.2)

    assert card._explore_sections, "前置条件：_all_plugins 有数据时应构建精选分组"

    # 切换到精选 tab（先切走再切回，确保信号触发）
    card._filter_bar.setCurrentItem("all")
    _pump(0.2)
    card._filter_bar.setCurrentItem("featured")
    _pump(0.2)

    # 核心断言：content_stack 当前展示的是 _explore_scroll（不是空状态）
    current = card._content_stack.currentWidget()
    assert current is card._explore_scroll, (
        f"精选 tab 数据非空时应展示 _explore_scroll，实际展示 {type(current).__name__}"
    )


def test_full_flow_no_market_data_does_not_dump_all_into_featured(monkeypatch):
    """端到端：show_card → 远端未到 → 切到精选 tab → 不显示兜底"全部插件"框

    完整重现用户场景：
    1. 新建卡片（无市场源 → 远端永不返回）
    2. 调 show_card()（_render_local_installed 会在 300ms 后触发）
    3. 等 _render_local_installed 走完（_all_plugins 被置空 → _render_plugins([]) → _rebuild_explore）
    4. 此时切到精选 tab，验证 _explore_sections 为空，且 content_stack 展示空状态
    """
    card = _new_card(monkeypatch)
    card.show()
    card.show_card()

    # 等 300ms 首屏合并窗口 + _render_local_installed 走完
    deadline = time.time() + 4
    while time.time() < deadline:
        _pump(0.05)
        # _render_local_installed 走完后 _initial_view_done 为 True
        if card._initial_view_done:
            break
    assert card._initial_view_done, "首屏渲染未完成（_render_local_installed 未触发）"

    # 兜底断言：_render_local_installed 把 _all_plugins 置空（前置）
    assert card._all_plugins == [], (
        f"无市场源时 _all_plugins 应为空，实际 {len(card._all_plugins)} 个"
    )

    # 核心断言 1：_rebuild_explore 没有兜底分组
    assert card._explore_sections == [], (
        f"精选页不应渲染兜底'全部插件'分组，但 _explore_sections={len(card._explore_sections)}"
    )

    # 核心断言 2：show_card 默认就在精选 tab，且数据为空 → 展示空状态占位
    current = card._content_stack.currentWidget()
    assert current is card._empty_label, (
        f"show_card 后精选 tab + 数据为空 → 应展示空状态占位，实际 {type(current).__name__}"
    )


def test_filter_bar_other_tabs_unaffected(monkeypatch):
    """切到其他 tab（all/installed/uninstalled/updates）时不受精选 tab 修复影响

    修复不破坏"全部"tab 的列表渲染：列表 tab 仍按 _render_plugins 全量重建，
    本地 extras 仍按 _build_local_extra_plugins() 并入 _matched（这是"全部"tab
    的预期行为，与精选 tab 不同）。
    """
    from ui.cards import MarketplaceCard
    from ui.marketplace_manager import MarketplaceSourceManager
    from ui.installer import PluginInstaller

    if str(PLUGIN_MARKETPLACE) not in sys.path:
        sys.path.insert(0, str(PLUGIN_MARKETPLACE))

    monkeypatch.setattr(MarketplaceSourceManager, "get_sources", lambda self: [])
    monkeypatch.setattr(MarketplaceSourceManager, "fetch_marketplace", lambda self, src, force=False: {"name": "", "plugins": []})
    monkeypatch.setattr(PluginInstaller, "get_installed_map", lambda self, use_cache=True: {})
    monkeypatch.setattr(PluginInstaller, "get_status_map", lambda self, use_cache=True: {})

    card = MarketplaceCard()
    card.show()
    # 手动注入市场数据 + 本地 extras
    card._plugin_data = [
        {"name": f"market-plugin-{i}", "version": "1.0.0", "categories": ["tool"]}
        for i in range(10)
    ]
    card._all_plugins = list(card._plugin_data)
    card._installed_set = set()
    card._version_map = {}
    card._status_map = {}
    card._render_plugins(card._plugin_data)
    _pump(0.2)

    # 切到"全部"tab
    card._filter_bar.setCurrentItem("all")
    _pump(0.2)

    # 列表 tab 应展示 _scroll（不是空状态）→ _scroll 有渲染的内容
    current = card._content_stack.currentWidget()
    assert current is card._scroll, (
        f"全部 tab 应展示 _scroll，实际展示 {type(current).__name__}"
    )
    # 列表有渲染的行（_row_map 非空）→ 列表路径未受影响
    assert card._row_map, "全部 tab 列表行未渲染"


# ──────────────────────────────────────────────────────────────────────
# B2：端到端——远端数据到达后精选页自动从「加载中…」占位切到正常视图
# ──────────────────────────────────────────────────────────────────────


def _make_market_plugins(n, market_name="fake", categories=("tool",)):
    """构造 mock 市场数据（每个插件带 categories，确保有分组可渲染）"""
    return [
        {
            "name": f"{market_name}-plugin-{i:03d}",
            "version": "1.2.3",
            "description": "测试插件",
            "categories": [categories[i % len(categories)]],
        }
        for i in range(n)
    ]


def _new_card_with_mock_worker(monkeypatch):
    """构造卡片 + 桩 worker：模拟远端市场源有数据且 worker 完整走信号链路

    - get_sources 返回一个源（让 worker 不直接走 all_done 跳过）
    - _MarketFetchWorker.run 被替换为 fake_run：直接 emit market_fetched
      + all_done（信号跨线程由 Qt 自动 queued 到主线程，与真实路径一致）
    - 不依赖真实网络 / 真实 MarketplaceSourceManager.fetch_marketplace
    - 模拟「15 个市场插件」：前 8 个已装 + 7 个未装；已装插件 local_ver=0.9.0
      比 remote 的 1.2.3 旧 → updates tab 也有匹配。确保 4 个列表 tab 都能
      在 _render_plugins 后得到非空 _matched → _apply_browse_mode 切到 _scroll。

    返回 MarketplaceCard（已 show）
    """
    if str(PLUGIN_MARKETPLACE) not in sys.path:
        sys.path.insert(0, str(PLUGIN_MARKETPLACE))
    import ui.cards as _cards_mod
    from ui.cards import MarketplaceCard
    from ui.installer import PluginInstaller
    from ui.marketplace_manager import MarketplaceSourceManager

    # 一个市场源即可（让 worker.run 不走「无源 → 直接 all_done」分支）
    fake_src = {"name": "fake", "source": {"source": "url", "url": "x"}}
    monkeypatch.setattr(MarketplaceSourceManager, "get_sources", lambda self: [fake_src])
    # 即便 fake_run 不调用 fetch_marketplace，也兜一下
    monkeypatch.setattr(
        MarketplaceSourceManager,
        "fetch_marketplace",
        lambda self, src, force=False: {"name": src["name"], "plugins": _make_market_plugins(15)},
    )
    # 桩化 worker.run：直接 emit market_fetched + all_done（不进入网络）
    def fake_run(self):
        data = {
            "name": "fake",
            "plugins": _make_market_plugins(15),
            "_marketplace": "fake",
        }
        self.market_fetched.emit(data, self._gen)
        self.all_done.emit(self._gen)

    monkeypatch.setattr(_cards_mod._MarketFetchWorker, "run", fake_run)

    # 本地已装 8 个（local_ver=0.9.0 < remote 1.2.3 → updates tab 匹配）；
    # 未装 7 个。状态全 enabled。这样 4 个列表 tab（all/installed/
    # uninstalled/updates）的 _matched 都非空。
    installed_names = {f"fake-plugin-{i:03d}" for i in range(8)}
    installed_map = {n: "0.9.0" for n in installed_names}
    status_map = {n: "enabled" for n in installed_names}
    monkeypatch.setattr(PluginInstaller, "get_installed_map", lambda self, use_cache=True: installed_map)
    monkeypatch.setattr(PluginInstaller, "get_status_map", lambda self, use_cache=True: status_map)

    card = MarketplaceCard()
    # 在 show 前预热 _installed_set/_version_map/_status_map（_render_plugins 内部依赖）
    card._installed_set = set(installed_map)
    card._version_map = dict(installed_map)
    card._status_map = dict(status_map)
    card.show()
    return card


def test_remote_arrival_switches_featured_from_placeholder_to_explore(monkeypatch):
    """B2 端到端：远端到达 → 精选页从 _empty_label 自动切到 _explore_scroll

    完整链路：
      show_card() → 50ms 后 _start_load → _async_refresh 启动 worker 线程
      → fake worker.run emit market_fetched（queued 到主线程）
      → _on_market_fetched → _merge_market_data → _schedule_render (80ms debounce)
      → _flush_render（被 _initial_view_done=False 拦截）+ 300ms _render_initial_view
      → _render_plugins → _apply_browse_mode(self._current_filter)
      → _content_stack 切到 _explore_scroll（不再卡在 _empty_label）

    防止 worker 回调链（market_fetched → _merge_market_data → _render_plugins →
    _apply_browse_mode）任一环节断裂导致用户永久卡在「精选数据加载中…」占位。
    """
    card = _new_card_with_mock_worker(monkeypatch)

    # ── 第一阶段：show_card 后立即断言 → 精选 tab 切到 _empty_label 占位 ──
    # show_card 同步调 _apply_browse_mode("featured")（_all_plugins=[] → _empty_label）。
    # pump(0.05) 让 50ms _start_load timer 触发并启动 worker 线程（fake_run 已 emit
    # market_fetched + all_done queued 到主线程，pump 时主线程消费这两个 queued
    # signal → _merge_market_data 填充 _plugin_data）。但 80ms debounce timer +
    # 300ms _render_initial_view 都还**未到期** → _render_plugins 未触发 →
    # _all_plugins 仍为 []、_content_stack 仍为 _empty_label。
    card.show_card()
    _pump(0.05)
    assert card._content_stack.currentWidget() is card._empty_label, (
        f"前置失败：精选 tab 数据未到达时应展示 _empty_label 占位，实际 "
        f"{type(card._content_stack.currentWidget()).__name__}"
    )
    assert card._all_plugins == [], (
        f"前置失败：_all_plugins 应仍为空（worker 回调链尚未触发 _render_plugins），"
        f"实际 {len(card._all_plugins)} 个"
    )

    # ── 第二阶段：等 80ms debounce + 300ms _render_initial_view 触发
    # → _render_plugins 填充 _all_plugins → _apply_browse_mode 切到 _explore_scroll ──
    # 总耗时上限：80ms debounce + 300ms initial render ≈ 400ms。给 3s 上限防环境慢。
    deadline = time.time() + 3
    while time.time() < deadline:
        _pump(0.05)
        if card._content_stack.currentWidget() is card._explore_scroll:
            break

    # 核心断言：远端数据到达后 _content_stack 从 _empty_label 切到 _explore_scroll
    current = card._content_stack.currentWidget()
    assert current is card._explore_scroll, (
        f"远端到达后精选页应自动切到 _explore_scroll，实际 "
        f"{type(current).__name__}（worker 回调链可能断裂，用户将永久卡在加载占位）"
    )

    # 兜底断言：_all_plugins 被填充 + 精选分组已构建
    assert card._all_plugins, (
        f"远端到达后 _all_plugins 应被填充，实际 {len(card._all_plugins)} 个插件"
    )
    assert card._explore_sections, (
        f"_rebuild_explore 应构建至少一个精选分组，实际 0 个（_all_plugins={len(card._all_plugins)}）"
    )


@pytest.mark.parametrize("target_tab", ["all", "installed", "uninstalled", "updates"])
def test_remote_arrival_other_tabs_switch_to_scroll(monkeypatch, target_tab):
    """B2 端到端参数化：远端到达 → 列表 tab 从占位切到 _scroll（_apply_browse_mode 不被精选修复影响）

    review 低优建议：参数化 4 个列表 tab（all/installed/uninstalled/updates）
    验证远端数据到达后非精选 tab 也走正常渲染路径（_scroll + _row_map）。

    测试时序：
      show_card() → 启动 worker + _render_initial_view timer（默认 featured tab）
      → 等 worker emit + 80ms debounce + 300ms _render_initial_view
      → _all_plugins 填充 + featured tab 显示 _explore_scroll
      → 切到目标 tab → _on_filter_changed → _render_plugins(self._plugin_data)
      → _apply_browse_mode(self._current_filter=target_tab)
      → 若 _matched 非空则切到 _scroll + _row_map 被填充
    """
    card = _new_card_with_mock_worker(monkeypatch)

    # 启动 show_card（启动 worker + _render_initial_view timer，默认 featured tab）
    card.show_card()

    # 等首屏渲染完成：worker emit + 80ms debounce + 300ms _render_initial_view
    deadline = time.time() + 3
    while time.time() < deadline:
        _pump(0.05)
        if card._all_plugins and card._initial_view_done:
            break
    assert card._all_plugins, (
        f"show_card 后 _all_plugins 应被远端数据填充，实际 {len(card._all_plugins)} 个"
    )

    # 切到目标 tab（_on_filter_changed → _apply_browse_mode + _render_plugins）
    card._filter_bar.setCurrentItem(target_tab)
    _pump(0.2)

    # 核心断言 1：_all_plugins 已填充（远端到达）
    assert card._all_plugins, (
        f"远端到达后 _all_plugins 应被填充，实际 {len(card._all_plugins)} 个"
    )

    # 核心断言 2：列表 tab 切到 _scroll（不是 _empty_label）
    # _plugin_matches 的过滤依赖 mock 的 _installed_set：
    #   - "all" → 全部 15 个匹配（_matched=15）
    #   - "installed" → 已装的 8 个匹配（_matched=8）
    #   - "uninstalled" → 未装的 7 个匹配（_matched=7）
    #   - "updates" → 已装且有更新的 8 个匹配（_matched=8）
    # 4 个 tab 的 _matched 都非空 → _apply_browse_mode 切到 _scroll。
    current = card._content_stack.currentWidget()
    assert current is card._scroll, (
        f"{target_tab} tab 数据到达后应切到 _scroll，实际 "
        f"{type(current).__name__}（_matched={len(card._matched)}）"
    )

    # 核心断言 3：列表行已渲染（_row_map 非空）
    assert card._row_map, (
        f"{target_tab} tab 列表行未渲染（远端到达后未走列表渲染路径）"
    )