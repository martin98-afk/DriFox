"""成果栏（插件 artifacts-manager）与 artifacts 字段驱动测试

覆盖：条目归一化 / kind 推断 / 去重 / 上限截断 / 空列表隐藏 / 坏数据容错 /
增量合并 / 历史回填 / ToolResult.artifacts 透传 / catalog 登记。
"""
from importlib import import_module

import pytest

from app.tools.result import ToolResult

_panel_mod = import_module("plugins.artifacts-manager.ui.artifact_panel")
InlineArtifactPanel = _panel_mod.InlineArtifactPanel



from app.tools.result import ToolResult


# ── ToolResult.artifacts 透传 ──


def test_tool_result_artifacts_passthrough():
    items = [{"path": "/tmp/a.png", "kind": "image"}]
    r = ToolResult(True, content="ok", artifacts=items)
    assert r.artifacts == items
    assert r.to_dict()["artifacts"] == items


def test_tool_result_artifacts_none_omitted():
    r = ToolResult(True, content="ok")
    assert r.artifacts is None
    assert "artifacts" not in r.to_dict()


def test_tool_result_artifacts_kwargs_only():
    """关键字参数形态（插件侧 _result_with_artifacts 用法）不炸"""
    r = ToolResult(success=True, content="ok", artifacts=[{"path": "p"}], data={"k": 1})
    assert r.data == {"k": 1}
    assert r.artifacts == [{"path": "p"}]


# ── 面板（需要 QApplication）──

pytest.importorskip("PyQt5.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def panel(qapp):
    p = InlineArtifactPanel()
    yield p
    p.deleteLater()


def _img_items(n, prefix="a"):
    return [{"path": f"/tmp/{prefix}{i}.png", "kind": "image", "name": f"{prefix}{i}.png"} for i in range(n)]


def test_panel_hidden_without_items(panel):
    panel.update_artifacts([])
    assert not panel.isVisible()


def test_panel_hidden_without_items_via_clear(panel):
    panel.update_artifacts(_img_items(2))
    panel.clear_artifacts()
    assert not panel.isVisible()
    assert panel._items == []


def test_panel_signature_skips_rebuild(panel):
    """同签名重复推送不重建（幂等）"""
    items = _img_items(2)
    panel.update_artifacts(items)
    n_items = panel._list_layout.count()
    panel.update_artifacts([dict(x) for x in items])  # 内容相同的新列表
    assert panel._list_layout.count() == n_items


def test_panel_dedupes_same_path(panel):
    """同路径重复条目去重保序"""
    items = [{"path": "/tmp/a.png"}, {"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}]
    panel.update_artifacts(items)
    assert len(panel._items) == 2


def test_panel_drops_invalid_items(panel):
    """坏条目（非 dict / 无 path）静默丢弃"""
    panel.update_artifacts(["not-a-dict", 42, {"nope": 1}, {"path": "/tmp/a.png"}])
    assert len(panel._items) == 1
    assert panel._items[0]["path"] == "/tmp/a.png"


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/x/a.png", "image"),
        ("/x/a.JPG", "image"),
        ("/x/a.svg", "image"),
        ("/x/a.mp4", "video"),
        ("/x/a.html", "html"),
        ("/x/a.md", "text"),
        ("/x/a.bin", "file"),
    ],
)
def test_panel_kind_from_ext(panel, path, expected):
    """kind 缺省按扩展名推断"""
    panel.update_artifacts([{"path": path}])
    assert panel._items[0]["kind"] == expected


def test_panel_kind_explicit_wins(panel):
    """显式 kind 优先于扩展名推断"""
    panel.update_artifacts([{"path": "/x/a.png", "kind": "video"}])
    assert panel._items[0]["kind"] == "video"

def test_panel_svg_thumb_decodes(panel, tmp_path):
    """SVG 走图片路径要真能解码出缩略图（Qt qsvg 插件在位）"""
    svg = tmp_path / "a.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
        '<rect width="120" height="80" fill="#3b82f6"/></svg>',
        encoding="utf-8",
    )
    pix = panel._load_thumb(str(svg))
    assert pix is not None and not pix.isNull()
    assert pix.width() <= _panel_mod._THUMB_W and pix.height() <= _panel_mod._THUMB_H

def test_panel_load_thumb_supports_strip_sizes(panel, tmp_path):
    """折叠方格 64×64：解码不越界（方向 C 统一方格）"""
    svg = tmp_path / "b.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90">'
        '<rect width="160" height="90" fill="#10b981"/></svg>',
        encoding="utf-8",
    )
    side = _panel_mod._COLLAPSED_THUMB
    pix = panel._load_thumb(str(svg), side, side)
    assert pix is not None and not pix.isNull()
    dpr = pix.devicePixelRatio()
    assert pix.width() == pix.height() == round(side * dpr), "输出必须是精确的方格物理尺寸"


def test_panel_overflow_capped(panel):
    """超上限截断 + 「还有 N 项」提示"""
    panel.update_artifacts(_img_items(25))
    assert len(panel._items) == 25
    # 可见行 = 上限 20 + 提示行；_clear_items 后 stretch 也占一个 layout 项
    widgets = [panel._list_layout.itemAt(i).widget() for i in range(panel._list_layout.count())]
    labels = [w for w in widgets if w is not None and w.metaObject().className() == "QLabel"]
    assert any("还有 5 项" in w.text() for w in labels), "应有溢出提示"


def test_panel_size_not_duplicated_on_repeat(panel):
    """重复推送不同 size 的同路径 → 视为新数据重建（文件可能已更新）"""
    panel.update_artifacts([{"path": "/tmp/a.png", "size": 1}])
    panel.update_artifacts([{"path": "/tmp/a.png", "size": 2}])
    assert len(panel._items) == 1
    assert panel._items[0]["size"] == 2


# ── catalog 登记 ──


def test_catalog_has_artifacts_show():
    from app.plugins.capabilities import catalog
    from app.plugins.contracts.capabilities import CapabilityLevel

    spec = catalog.get("ui.artifacts.show")
    assert spec is not None
    assert spec.level is CapabilityLevel.MEDIUM
    assert spec.surfaces == frozenset({"ui", "tool"})


def test_capability_registered_both_surfaces():
    """两 surface 都注册了 ui.artifacts.show（经捕获型 registry 验证）"""
    from app.plugins.capabilities.providers import tool_core, ui_core

    class CaptureRegistry:
        def __init__(self):
            self.calls = {}

        def register(self, cap_id, provider, **kwargs):
            self.calls[(cap_id, kwargs.get("surface", "ui"))] = provider

        def has(self, cap_id, surface):
            return (cap_id, surface) in self.calls

    reg = CaptureRegistry()
    ui_core.register(reg)
    tool_core.register(reg)
    assert reg.has("ui.artifacts.show", "ui")
    assert reg.has("ui.artifacts.show", "tool")


def test_tool_surface_show_without_window_returns_zero():
    """tool surface 无窗口时返回恒 0 空实现（不炸）

    ``_artifacts_show`` 是 ``tool_core.register`` 内的嵌套工厂（该模块惯例），
    经 register 捕获取用。
    """
    from app.plugins.capabilities.providers import tool_core

    holder = {}

    class CaptureRegistry:
        def register(self, cap_id, provider, **kwargs):
            if cap_id == "ui.artifacts.show":
                holder["provider"] = provider

    tool_core.register(CaptureRegistry())
    assert "provider" in holder, "ui.artifacts.show 未在 tool surface 注册"
    provider = holder["provider"](None)
    assert provider([{"path": "/tmp/a.png"}]) == 0
    assert provider([]) == 0


# ── ui surface provider（主线程直跑，不依赖窗口就返回 0）──


def test_ui_provider_bad_items_zero(qapp, monkeypatch):
    from app.plugins.capabilities.providers import ui_core

    provider = ui_core._ui_artifacts_show(object())  # host 无卡片属性
    assert provider([]) == 0
    assert provider("not-a-list") == 0


def test_ui_provider_delivers_on_main_thread(qapp):
    """主线程调用直接落到 push_inline_items（用假卡片验证）"""
    from app.plugins.capabilities.providers import ui_core

    delivered = []

    class FakeCard:
        def push_inline_items(self, panel_id, items):
            delivered.extend(items)
            return len(items)

    class FakeHost:
        _current_assistant_card = FakeCard()
        _tool_card_map = {}

    provider = ui_core._ui_artifacts_show(FakeHost())
    n = provider([{"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}])
    assert n == 2
    assert len(delivered) == 2


def test_ui_provider_call_id_targeting(qapp):
    """target="call" 经 _tool_card_map 定位原卡"""
    from app.plugins.capabilities.providers import ui_core

    delivered = []

    class FakeCard:
        def __init__(self, tag):
            self.tag = tag

        def push_inline_items(self, panel_id, items):
            delivered.append(self.tag)
            return len(items)

    old_card, origin_card = FakeCard("latest"), FakeCard("origin")
    host = type("H", (), {})()
    host._current_assistant_card = old_card
    host._tool_card_map = {"call-1": origin_card}

    provider = ui_core._ui_artifacts_show(host)
    provider([{"path": "/tmp/a.png"}], target="call", call_id="call-1")
    assert delivered == ["origin"], "应挂到 call_id 对应的发起卡，而非最新卡"


def test_ui_provider_missing_card_zero(qapp):
    from app.plugins.capabilities.providers import ui_core

    host = type("H", (), {})()  # 无 _current_assistant_card / _find_latest_assistant_card
    provider = ui_core._ui_artifacts_show(host)
    assert provider([{"path": "/tmp/a.png"}]) == 0


# ── 合并语义（面板 update_items 契约）──


def test_panel_update_items_merges_instead_of_replacing(panel):
    """同轮多次推送必须累积：整体替换会把先到的条目顶掉"""
    panel.update_items([{"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}])
    panel.update_items([{"path": "/tmp/c.png"}])
    assert [it["path"] for it in panel._items] == ["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"]


def test_panel_update_items_updates_same_path_in_place(panel):
    """同路径重复推送原位更新，不改变顺序"""
    panel.update_items([{"path": "/tmp/a.png", "size": 1}, {"path": "/tmp/b.png"}])
    panel.update_items([{"path": "/tmp/a.png", "size": 2}])
    assert [it["path"] for it in panel._items] == ["/tmp/a.png", "/tmp/b.png"]
    assert panel._items[0]["size"] == 2


def test_panel_update_items_ignores_empty_push(panel):
    """空列表不隐含清空；显式清理走 clear_artifacts"""
    panel.update_items([{"path": "/tmp/a.png"}])
    panel.update_items([])
    assert len(panel._items) == 1
    panel.clear_artifacts()
    assert panel._items == []


def test_panel_restore_items_is_idempotent(panel):
    """历史回填重复调用不产生重复行"""
    items = [{"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}]
    panel.restore_items(items)
    panel.restore_items(items)
    assert [it["path"] for it in panel._items] == ["/tmp/a.png", "/tmp/b.png"]


def test_plugin_history_loader_collects_tool_artifacts():
    """会话重载：从 batch 的 tool 消息收集成果（空 content 也要收）"""
    ui_mod = import_module("plugins.artifacts-manager.ui")

    batch = [
        {"role": "assistant", "content": "done"},
        {"role": "tool", "content": "ok", "artifacts": [{"path": "/tmp/a.png"}]},
        {"role": "tool", "content": "", "artifacts": [{"path": "/tmp/b.png"}]},
        {"role": "tool", "content": "no-artifacts"},
    ]
    items = ui_mod._load_history(batch, None)
    assert [it["path"] for it in items] == ["/tmp/a.png", "/tmp/b.png"]


# ── 折叠态缩略图带 / 溢出展开 / 拖拽 ──

def test_panel_collapsed_strip_lists_only_images(panel):
    panel.update_artifacts([{"path": "/tmp/a.png"}, {"path": "/tmp/b.png"}, {"path": "/tmp/c.html"}])
    widgets = [panel._strip_layout.itemAt(i).widget() for i in range(panel._strip_layout.count())]
    thumbs = [w for w in widgets if w is not None and w.metaObject().className() == "QLabel"]
    assert len(thumbs) == 2, "折叠带只放图片条目"
    assert not panel._thumb_strip.isHidden(), "折叠态缩略图带应可见"

def test_panel_collapsed_strip_hidden_after_expand(panel):
    panel.update_artifacts([{"path": "/tmp/a.png"}])
    panel._on_collapse_clicked()
    assert panel._thumb_strip.isHidden()

def test_panel_collapsed_strip_hides_when_only_files(panel):
    """纯文件成果不渲染图片画廊（无图片占位）"""
    panel.update_artifacts([{"path": "/tmp/report.md", "kind": "text", "name": "report.md"}])
    assert panel._thumb_strip.isHidden()

def test_panel_collapsed_strip_gallery_slots_and_overflow(panel):
    """单行方格画廊：420 宽 6 槽（含 +N 位），放得下不出 +N，摘要给类型分布"""
    from PyQt5.QtWidgets import QLabel

    items = _img_items(5) + [{"path": "/tmp/report.md", "kind": "text", "name": "report.md"}]
    panel.update_artifacts(items)
    panel.resize(420, 200)
    panel._rebuild_thumb_strip()

    widgets = [panel._strip_layout.itemAt(i).widget() for i in range(panel._strip_layout.count())]
    thumbs = [w for w in widgets if w is not None and w.objectName() == "artifactThumbnail"]
    overflow = [w for w in widgets if w is not None and w.objectName() == "artifactOverflow"]
    summary = panel._thumb_strip.findChild(QLabel, "artifactSummary")

    assert panel._collapsed_slot_count(7) == 6, "420 宽应为 6 槽（+N 占一格）"
    assert len(thumbs) == 5, "5 图 ≤ 6 槽时全部展示"
    assert overflow == [], "放得下时不得出现 +N"
    assert all(w.width() == 64 and w.height() == 64 for w in thumbs), "方格恒 64×64"
    assert summary is not None and summary.text() == "a0.png · 5 张图片 · 1 个文件"

def test_missing_image_thumb_shows_kind_fallback(panel):
    """文件缺失/清理后显示 IMG 徽标与缺失说明，而非无解释的 ✕"""
    panel.update_artifacts(_img_items(1))
    panel.show()
    label = next(
        w
        for w in (panel._strip_layout.itemAt(i).widget() for i in range(panel._strip_layout.count()))
        if w is not None and w.objectName() == "artifactThumbnail"
    )
    panel._apply_thumb(label, "/nonexistent/xx.png", 64, 64)
    assert label.text() == "IMG"
    assert "不存在" in label.toolTip()


def test_expand_collapse_without_motion_lands_final_state(panel, monkeypatch):
    """reduced-motion：跳过补间直落终值（列表放开限高、画廊回归），不悬在中间态"""
    monkeypatch.setattr(_panel_mod.Animations, "motion_enabled", lambda: False)
    panel.update_artifacts(_img_items(2))
    panel.show()

    panel._on_collapse_clicked()  # 展开
    assert panel._list_wrap.isVisible()
    assert panel._list_wrap.maximumHeight() == _panel_mod._QWIDGETSIZE_MAX
    assert panel._thumb_strip.isHidden()

    panel._on_collapse_clicked()  # 折叠回
    assert panel._list_wrap.isHidden()
    assert not panel._thumb_strip.isHidden()
    assert panel._list_wrap.maximumHeight() == _panel_mod._QWIDGETSIZE_MAX


def test_animating_sizehint_reports_clamped_height(qapp):
    """等高交接动画：动画中 sizeHint ≈ 折叠态占高（宿主补偿不跳变，标题不抖）"""
    from PyQt5.QtWidgets import QVBoxLayout, QWidget

    host = QWidget()
    host.resize(420, 400)
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    panel = InlineArtifactPanel()
    lay.addWidget(panel)
    host.show()
    try:
        panel.update_artifacts(_img_items(3))
        folded_h = panel.sizeHint().height()
        strip_h = panel._strip_visual_height()
        assert strip_h > 0, "折叠态画廊应有占高"

        panel._on_collapse_clicked()  # 展开：列表从画廊占高长出
        anim = panel._list_anim
        assert anim is not None and anim.state() == _panel_mod.QAbstractAnimation.Running
        assert int(anim.startValue()) == strip_h, "展开应从画廊占高起步（等高交接）"
        anim_h = panel.sizeHint().height()
        assert abs(anim_h - folded_h) <= 4, f"动画起点占高应与折叠态一致 anim={anim_h} folded={folded_h}"

        anim.stop()
        panel._on_list_anim_done(True)  # 模拟动画收尾：放开限高
        expanded_h = panel.sizeHint().height()
        assert expanded_h > folded_h + 10, "动画结束后 sizeHint 应回到全展开高"
    finally:
        panel.deleteLater()
        host.deleteLater()


def _strip_parts(panel):
    widgets = [panel._strip_layout.itemAt(i).widget() for i in range(panel._strip_layout.count())]
    thumbs = [w for w in widgets if w is not None and w.objectName() == "artifactThumbnail"]
    overflow = [w for w in widgets if w is not None and w.objectName() == "artifactOverflow"]
    return thumbs, overflow


def _set_strip_width(panel, width: int) -> None:
    """顶层窗口受 Qt 最小尺寸约束；直接缩画廊容器模拟任意可用宽"""
    panel._thumb_strip.resize(width, 100)
    assert panel._thumb_strip.width() == width


def _add_solid_png(path, w: int, h: int) -> None:
    from PyQt5.QtGui import QColor, QImage, QPainter

    img = QImage(w, h, QImage.Format_ARGB32)
    p = QPainter(img)
    p.fillRect(img.rect(), QColor(200, 60, 60))
    p.end()
    img.save(path)


def test_strip_slots_boundary_table(panel):
    """方向 C 边界表：+N 占格；放得下不溢出；窄栏只留计数入口；超宽 12 上限"""
    panel.update_artifacts(_img_items(20))
    _set_strip_width(panel, 420)
    panel._rebuild_thumb_strip()
    thumbs, overflow = _strip_parts(panel)
    assert panel._collapsed_slot_count(20) == 6, "420 宽 6 槽"
    assert len(thumbs) == 5 and overflow and overflow[0].text() == "+15"
    assert panel._collapsed_slot_count(6) == 6, "6 图恰好塞满 6 槽"
    assert panel._collapsed_slot_count(7) == 6

    panel.update_artifacts(_img_items(7))
    panel._rebuild_thumb_strip()
    thumbs, overflow = _strip_parts(panel)
    assert len(thumbs) == 5 and overflow and overflow[0].text() == "+2"

    # +6 哨兵：W=64 → (64+6)//70 == 1；漏掉 +6 会算成 0
    _set_strip_width(panel, 64)
    panel._rebuild_thumb_strip()
    thumbs, overflow = _strip_parts(panel)
    assert panel._collapsed_slot_count(7) == 1
    assert len(thumbs) == 0 and overflow and overflow[0].text() == "+7"

    # W=414 → (414+6)//70 == 6；漏掉 +6 会算成 5
    _set_strip_width(panel, 414)
    panel._rebuild_thumb_strip()
    thumbs, overflow = _strip_parts(panel)
    assert panel._collapsed_slot_count(7) == 6
    assert len(thumbs) == 5 and overflow and overflow[0].text() == "+2"

    panel.resize(150, 200)
    _set_strip_width(panel, 150)
    panel._rebuild_thumb_strip()
    thumbs, overflow = _strip_parts(panel)
    assert panel._collapsed_slot_count(7) == 2
    assert len(thumbs) == 1 and overflow and overflow[0].text() == "+6"

    panel.resize(60, 200)
    _set_strip_width(panel, 60)
    panel._rebuild_thumb_strip()
    thumbs, overflow = _strip_parts(panel)
    assert panel._collapsed_slot_count(7) == 0
    assert len(thumbs) == 0 and overflow and overflow[0].text() == "+7"

    panel.update_artifacts(_img_items(20))
    # W=1200 → (1206)//70 == 17 > 12：只有上限截断才能得到 12
    panel.resize(900, 200)
    _set_strip_width(panel, 1200)
    panel._rebuild_thumb_strip()
    thumbs, overflow = _strip_parts(panel)
    assert panel._collapsed_slot_count(20) == 12, "超宽按上限 12 截断"
    # +N 占格优先于「12 thumb」：12 槽溢出时为 11 thumb + “+9”
    assert len(thumbs) == 11 and overflow and overflow[0].text() == "+9"


def test_strip_single_image_shrinks_on_narrow_width(panel, monkeypatch):
    """单图特例：W<70 时仍显示 1 格并等比缩格（真实窗口缩不到 60，注入宽度测纯逻辑）"""
    panel.update_artifacts(_img_items(1))
    monkeypatch.setattr(panel._thumb_strip, "width", lambda: 60)
    panel._rebuild_thumb_strip()
    thumbs, overflow = _strip_parts(panel)
    assert len(thumbs) == 1 and overflow == []
    assert thumbs[0].width() == 60 and thumbs[0].height() == 60, "极窄栏等比缩格仍保持正方形"


def test_strip_thumb_covers_without_distortion(panel, tmp_path):
    """1:4 / 4:1 图都 cover 填充到 64×64 方格，不压扁（裁切而非拉伸）"""
    from PyQt5.QtGui import QColor, QImage, QPainter

    def make(path, w, h, split_horizontal):
        img = QImage(w, h, QImage.Format_RGB32)
        p = QPainter(img)
        p.fillRect(img.rect(), QColor(220, 40, 40))
        if split_horizontal:
            p.fillRect(0, 0, w // 2, h, QColor(40, 80, 220))
        else:
            p.fillRect(0, 0, w, h // 2, QColor(40, 80, 220))
        p.end()
        img.save(path)

    wide = str(tmp_path / "wide.png")
    tall = str(tmp_path / "tall.png")
    make(wide, 800, 200, True)
    make(tall, 100, 400, False)
    side = _panel_mod._COLLAPSED_THUMB
    for path, is_wide in ((wide, True), (tall, False)):
        pix = panel._load_thumb(path, side, side)
        assert pix is not None and not pix.isNull()
        dpr = pix.devicePixelRatio()
        assert pix.width() == pix.height() == round(side * dpr), "必须精确方格"
        img = pix.toImage()
        mid = int(round((side / 2) * dpr))
        if is_wide:
            assert img.pixelColor(mid, 1).alpha() == 255, "宽图 cover 后顶部中央必须不透明（fit 会留透明）"
        else:
            assert img.pixelColor(1, mid).alpha() == 255, "竖图 cover 后左侧中央必须不透明（fit 会留透明）"


def test_strip_thumb_rounded_corners(panel, tmp_path):
    """圆角必须真裁 pixmap：左上角透明、中心不透明（QSS border-radius 不裁）"""
    path = str(tmp_path / "solid.png")
    _add_solid_png(path, 200, 120)
    side = _panel_mod._COLLAPSED_THUMB
    pix = panel._load_thumb(path, side, side)
    dpr = pix.devicePixelRatio()
    img = pix.toImage()
    mid = int(round((side / 2) * dpr))
    assert img.pixelColor(0, 0).alpha() == 0, "左上角应被圆角裁掉"
    assert img.pixelColor(mid, mid).alpha() == 255, "中心保持不透明"

def test_strip_thumb_dpr_scales_decode(panel, tmp_path, monkeypatch):
    """DPR 路径：物理像素 = 逻辑尺寸 × DPR，pixmap 带回 devicePixelRatio"""
    path = str(tmp_path / "dpr.png")
    _add_solid_png(path, 200, 120)
    side = 64
    monkeypatch.setattr(panel, "_dpr", lambda: 2.0)
    pix = panel._load_thumb(path, side, side)
    assert pix is not None
    assert pix.devicePixelRatio() == 2.0
    assert pix.width() == pix.height() == round(side * 2)

def test_strip_blocks_are_fixed_squares(panel):
    """折叠态所有块（缩略图 + 溢出 +N）恒 64×64"""
    panel.update_artifacts(_img_items(8))
    _set_strip_width(panel, 420)
    panel._rebuild_thumb_strip()
    thumbs, overflow = _strip_parts(panel)
    assert len(thumbs) == 5 and overflow and overflow[0].text() == "+3"
    assert all(w.width() == 64 and w.height() == 64 for w in thumbs + overflow)

def test_panel_overflow_expand_all(panel):
    MAX_VISIBLE_ITEMS = _panel_mod.MAX_VISIBLE_ITEMS

    panel.update_artifacts(_img_items(25))
    rows = [panel._list_layout.itemAt(i).widget() for i in range(panel._list_layout.count())]
    assert len([w for w in rows if w is not None]) == MAX_VISIBLE_ITEMS + 1, "20 行 + 溢出提示"
    panel._expand_all()
    rows = [panel._list_layout.itemAt(i).widget() for i in range(panel._list_layout.count())]
    assert len([w for w in rows if w is not None]) == 25

def test_panel_make_drag_carries_file_url(panel, tmp_path):
    from PyQt5.QtWidgets import QFrame

    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    drag = panel._make_drag(str(f), QFrame())
    assert drag is not None
    from pathlib import Path

    assert [Path(u.toLocalFile()) for u in drag.mimeData().urls()] == [f]

def test_panel_make_drag_missing_file_returns_none(panel, tmp_path):
    from PyQt5.QtWidgets import QFrame

    assert panel._make_drag(str(tmp_path / "nope.png"), QFrame()) is None

# ── 落库归一化与历史恢复 ──

def test_normalize_message_keeps_artifacts():
    from app.core.conversation.message_content import normalize_message

    out = normalize_message(
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "gen",
            "content": "ok",
            "artifacts": [
                {
                    "path": "/tmp/a.png",
                    "kind": "image",
                    "width": 1024,
                    "height": 768,
                    "meta": {"duration": 3, "note": "x" * 500, "bad": ["list"]},
                },
                {"path": "", "kind": "image"},
                "not-a-dict",
            ],
        }
    )
    assert out is not None
    assert len(out["artifacts"]) == 1
    art = out["artifacts"][0]
    assert art["path"] == "/tmp/a.png"
    assert art["kind"] == "image"
    assert art["width"] == 1024
    assert len(art["meta"]["note"]) == 200, "meta 长字符串截断"
    assert "bad" not in art["meta"], "非标量 meta 丢弃"

def test_normalize_artifacts_capped():
    from app.core.conversation.message_content import normalize_artifact_items

    items = [{"path": f"/tmp/{i}.png"} for i in range(80)]
    assert len(normalize_artifact_items(items)) == 50

def test_kind_badge_shows_letter(qapp):
    """徽标必须有字：构造期漏 setText 会渲成空白色块"""
    _KindBadge = _panel_mod._KindBadge

    assert _KindBadge("text").text() == "TXT"
    assert _KindBadge("file").text() == "FILE"
    assert _KindBadge("image").text() == "IMG"
    assert _KindBadge("html").text() == "HTML"

def test_kind_meta_label_is_human_readable(qapp):
    """meta 行用可读类型词而非单字母缩写"""
    assert _panel_mod._KIND_LABEL["text"] == "文本"
    assert _panel_mod._KIND_LABEL["image"] == "图像"
    assert _panel_mod._KIND_LABEL["file"] == "文件"

def test_gallery_container_is_theme_styled(panel):
    """卡片层级：alpha250 + 派生边框对合成底 ≥1.5（review #6 裁定）"""
    from PyQt5.QtGui import QColor

    panel.update_artifacts(_img_items(2) + [{"path": "/tmp/report.md", "kind": "text"}])
    css = panel._thumb_strip.styleSheet()
    assert "border-radius" in css, "画廊容器应有圆角"
    assert ", 250)" in css, "内容容器 alpha 必须为 250（alpha110 会红）"
    assert "artifactSummary" in css or panel._summary_label.styleSheet(), "摘要行应有主题样式"

    derived = _panel_mod._derived_card_border()
    surface = _panel_mod._panel_card_surface()
    ratio = _panel_mod._contrast_ratio(QColor(derived), surface)
    assert ratio >= 1.5, f"派生边框对卡片合成底对比不足: {ratio:.3f}"

    panel.show()
    panel._on_collapse_clicked()  # 展开
    list_css = panel._list_wrap.styleSheet()
    assert "border-radius" in list_css, "展开列表应包进同款圆角卡片"
    assert "artifactListCard" in list_css
    assert ", 250)" in list_css

def test_derived_border_lifts_true_black(panel, monkeypatch):
    """true-black 现状 BORDER 不达标时必须派生更亮值（不改 design_tokens）"""
    from PyQt5.QtGui import QColor

    monkeypatch.setattr(_panel_mod.Colors, "refresh", lambda: None)
    monkeypatch.setattr(_panel_mod.Colors, "BORDER", "#2A2A2A")
    monkeypatch.setattr(_panel_mod.Colors, "CARD_BG", "rgba(18, 18, 18, {alpha})")
    monkeypatch.setattr(_panel_mod.Colors, "ASSISTANT_CARD_BG", "rgba(20, 20, 20, 150)")
    monkeypatch.setattr(_panel_mod.Colors, "CONTENT_BG", "#101010")
    monkeypatch.setattr("app.utils.theme_manager.theme_manager.is_light_theme", lambda *a, **k: False)

    surface = _panel_mod._panel_card_surface()
    derived = _panel_mod._derived_card_border()
    assert derived != "#2A2A2A", "不达标时不得原值返回"
    assert _panel_mod._contrast_ratio(QColor(derived), surface) >= 1.5

def test_plugin_registers_inline_panel(qapp):
    """插件把成果栏注册到卡片内嵌面板槽位"""
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    ui_mod = import_module("plugins.artifacts-manager.ui")
    ui_mod.register_ui(UIPluginRegistry.get_instance())
    info = UIPluginRegistry.get_instance().get_card_inline_panel("artifacts")
    assert info is not None
    assert info.widget_class is InlineArtifactPanel


def test_card_push_inline_items_delivers_to_panel(qapp):
    """宿主通用投递：ensure_inline_panel + update_items 落到插件面板"""
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
    from app.widgets.message_card import MessageCard

    ui_mod = import_module("plugins.artifacts-manager.ui")
    ui_mod.register_ui(UIPluginRegistry.get_instance())
    card = MessageCard(role="assistant")
    try:
        n = card.push_inline_items("artifacts", [{"path": "/tmp/a.png"}])
        assert n == 1
        panel = card.get_inline_panel("artifacts")
        assert panel is not None
        assert [it["path"] for it in panel._items] == ["/tmp/a.png"]
        card.push_inline_items("artifacts", [{"path": "/tmp/b.png"}])
        assert [it["path"] for it in panel._items] == ["/tmp/a.png", "/tmp/b.png"]
    finally:
        card.deleteLater()


def test_card_gallery_height_handoff(qapp, monkeypatch):
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QVBoxLayout, QWidget
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
    from app.widgets.message_card import MessageCard

    monkeypatch.setattr(_panel_mod.Animations, "motion_enabled", staticmethod(lambda: True))
    import_module("plugins.artifacts-manager.ui").register_ui(UIPluginRegistry.get_instance())
    host = QWidget()
    layout = QVBoxLayout(host)
    card = MessageCard(role="assistant")
    from PyQt5.QtCore import Qt
    layout.addWidget(card, 0, Qt.AlignTop)
    layout.addStretch()
    host.resize(680, 900)
    host.show()
    try:
        card.push_inline_items("artifacts", _img_items(4))
        panel = card.get_inline_panel("artifacts")
        QTest.qWait(25)
        strip = panel._strip_visual_height()
        folded = card.height()
        panel._on_collapse_clicked()
        QTest.qWait(55)
        target = int(panel._list_anim.endValue())
        mid = card.height()
        QTest.qWait(_panel_mod.Animations.ENTER_MS + 40)
        assert folded < mid < card.height()
        assert abs(panel._list_wrap.height() - target) <= 2, (target, panel._list_wrap.height())
        panel._on_collapse_clicked()
        assert int(panel._list_anim.endValue()) == strip, (strip, folded, mid, card.height())
        QTest.qWait(45)
        current = panel._list_wrap.maximumHeight()
        panel._on_collapse_clicked()
        assert int(panel._list_anim.startValue()) == current
        QTest.qWait(_panel_mod.Animations.ENTER_MS + 40)
        panel._on_collapse_clicked()
        QTest.qWait(_panel_mod.Animations.EXIT_MS + 50)
        assert card.height() == folded
        assert not panel._thumb_strip.isHidden()
    finally:
        host.close()
        host.deleteLater()
        qapp.processEvents()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
