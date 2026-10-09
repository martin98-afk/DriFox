# -*- coding: utf-8 -*-
"""服务商预置卡片墙（P1-7 + 视觉改版）。

背景：原先「添加服务商」直接进入 831 行手写表单，用户要先在服务商下拉里
翻找、再手工填 URL / 认证方式 / 模型名。卡片墙把「选哪个服务商」前置成
**一眼可辨的一步**：每张卡是一个已声明的服务商（图标 + 名称 + 一句话说明），
点击即以预置参数进入编辑表单，用户只补 API_KEY。

视觉（对齐项目内卡片语言：插件市场插件卡 / 模型选择卡 ModelItem）：
- 每卡是独立 ``QFrame``：圆角 8 + 1px 边框 + 浅背景，hover 时边框转强调色
- 内部**横向分区**：左图标区（固定 30px）+ 右文字区（QVBox：名称 / host）
  —— 绝不叠放（早期版本图标压字）
- 名称超宽走 elide，全名与 host 进 tooltip
- 卡片本体 setFixedSize 固定 176×68（系统配置卡同款写法）：容器多宽 tile 都不跟，
  FlowLayout 自动换行出网格感

数据源：``ProviderRegistry``（providers 插件声明的 ``ProviderDef``），零硬编码。
分组复用 ``provider_setting_card._group_of``，配色复用 ``_GROUP_COLORS``。
"""

from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css
from app.widgets.flow_layout import FlowLayout

# 卡片尺寸：高度固定、宽度随内容自适应（下限 _CARD_W 防短名卡片过窄）。
# 不按容器推导——实测宽窗口下 tile 被拉成 230px 横条铺满。
_CARD_W = 176
_CARD_H = 68

# 图标区边长与图标边长（横向分区，不与文字重叠）
_ICON_BOX = 30
_ICON_SIZE = 22

# 布局节奏（组间留白此前近 100px，失衡）
_GROUP_HEADER_TOP = 16  # 非首组组头的上边距
_TILE_SPACING = 12  # 组内卡片间距
_GROUP_SPACING = 20  # 组间额外间距
_CUSTOM_SPACING = 20  # 「自定义」区与上面的组间距

# 「+ 自定义」入口的固定标识（点它走原手工表单）
CUSTOM_ENTRY = "__custom__"


def _provider_caption(api_url: str, family: str, group: str) -> str:
    """一句话说明：优先展示 api_url 的 host（用户能对上「哪个域名」），退化到分组名。"""
    host = ""
    try:
        from urllib.parse import urlparse

        host = urlparse(api_url).hostname or ""
    except Exception:
        host = ""
    if host:
        return host
    if family:
        return family
    return group


class _IconBox(QWidget):
    """图标容器：固定边长 + **浅底衬盘**，承载 ProviderIconWidget。

    独立成容器是为了保证**横向分区**（左图标 | 右文字）在布局上彻底分离。
    衬底：部分服务商图标本体是深色填充（如 OpenCode Zen/Go 的 ``#211E1E``）
    且插件未提供 ``icons_light/`` 浅色版 —— 浅色主题下直接摆在白卡上就是个
    黑方块。统一垫一层随主题的浅底圆角，深/浅主题都不会出现「纯黑块」。
    """

    def __init__(self, provider_name: str, parent=None):
        super().__init__(parent)
        self.provider_name = provider_name
        self.setFixedSize(_ICON_BOX, _ICON_BOX)
        # ⚠ 普通 QWidget（非 QFrame）必须开 WA_StyledBackground，否则
        # `setStyleSheet("background-color: ...")` 不生效（衬底失效 → 深色图标
        # 仍显示为黑块）。实测：不开无背景，开了才有。
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._apply_style()
        if provider_name == CUSTOM_ENTRY:
            return
        from app.widgets.cards.settings.provider_setting_card import ProviderIconWidget

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._icon = ProviderIconWidget(provider_name, _ICON_SIZE)
        layout.addWidget(self._icon, 0, Qt.AlignCenter)

    def _apply_style(self):
        """衬盘样式（主题 token，refresh_style 时重建）"""
        Colors.refresh()
        self.setStyleSheet(
            f"""
            background-color: {Colors.HOVER_BG};
            border-radius: 6px;
            """
        )

    def refresh_style(self):
        """主题切换：重刷衬盘与内部图标"""
        self._apply_style()
        icon = getattr(self, "_icon", None)
        if icon is not None and hasattr(icon, "refresh_style"):
            try:
                icon.refresh_style()
            except RuntimeError:
                pass


class ProviderPickerTile(QFrame):
    """单个预置服务商卡（图标 + 名称 + 一句话）。"""

    picked = pyqtSignal(str)  # provider_name（或 CUSTOM_ENTRY）

    def __init__(self, provider_name: str, caption: str, parent=None):
        super().__init__(parent)
        self.provider_name = provider_name
        # 高度固定、宽度自适应内容（QLabel 全文自然宽度撑开）：固定 176px 会截断
        # 「阿里云 (DashScope)」「SiliconFlow (硅基流动)」这类长名（用户实测）
        self.setFixedHeight(_CARD_H)
        self.setMinimumWidth(_CARD_W)
        self.setCursor(Qt.PointingHandCursor)
        self._apply_style()

        # 横向分区：左图标 | 右文字（绝不用绝对定位叠放）
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.iconBox = _IconBox(provider_name, self)
        layout.addWidget(self.iconBox, 0, Qt.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        full_name = provider_name if provider_name != CUSTOM_ENTRY else "自定义服务商"
        self.nameLabel = QLabel(full_name, self)
        text_col.addWidget(self.nameLabel)

        self.captionLabel = QLabel(caption, self)
        text_col.addWidget(self.captionLabel)

        # 文字样式在建完控件后统一施加（含主题 token）
        self._apply_text_style()

        text_col.addStretch(1)
        layout.addLayout(text_col, 1)

        # tooltip 只留卡片本体一处：全局 setToolTip patch 会给每个调用装一个
        # 自绘悬浮气泡，子控件再各设一份 → hover 文字区时双气泡重叠（用户实测）
        self.setToolTip(f"{full_name}\n{caption}" if caption else full_name)

    def _apply_style(self):
        """卡片样式（内嵌主题 token，refresh_style 时需重建）"""
        Colors.refresh()
        self.setStyleSheet(
            f"""
            ProviderPickerTile {{
                background-color: {Colors.CARD_BG.format(alpha=200)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
            ProviderPickerTile:hover {{
                background-color: {Colors.HOVER_BG};
                border: 1px solid {Colors.SYSTEM_ACCENT};
            }}
            """
        )

    def _apply_text_style(self):
        """文字样式（单独重建，避免被卡片 QSS 的透明规则干扰）"""
        Colors.refresh()
        self.nameLabel.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {font_size_css(13)} font-weight: 600; "
            f"{get_font_family_css()}; background: transparent; border: none;"
        )
        self.captionLabel.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {font_size_css(11)}; {get_font_family_css()}; "
            f"background: transparent; border: none;"
        )

    def refresh_style(self):
        """主题切换：重刷卡片/文字/图标（主题 token 全在样式串里）"""
        self._apply_style()
        self._apply_text_style()
        self.iconBox.refresh_style()

    def mousePressEvent(self, event):
        self.picked.emit(self.provider_name)
        super().mousePressEvent(event)


class ProviderPickerCard(QWidget):
    """服务商预置卡片墙内容（装进 BaseSettingsCard 壳）。

    分组顺序与设置卡列表一致（OAuth → Coding Plan → 本地 → API），
    末尾固定一个「自定义服务商」入口（走原手工表单）。
    """

    providerPicked = pyqtSignal(str)  # provider_name；CUSTOM_ENTRY 表示自定义
    customPicked = pyqtSignal()  # 选「自定义服务商」

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._tiles: List[ProviderPickerTile] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(0)
        self._layout = layout
        self._build()

    def _build(self):
        from app.plugins.registries.provider_registry import ProviderRegistry
        from app.widgets.cards.settings.provider_setting_card import _GROUP_COLORS, _GROUP_ORDER, _group_of

        try:
            providers = ProviderRegistry.get_instance().all()
        except Exception:
            providers = []

        # 按分组归集（保持注册表返回序）
        # ⚠ 必须用 ProviderDef 的真实字段构造伪 info 喂给 _group_of：「本地」组的
        # 判据是「认证方式 none 或 api_url 含 localhost」，而插件声明的这两项都在
        # ProviderDef 上（auth_type / api_url）—— 传空 dict 会让本地类服务商
        # （Ollama / LM Studio）永远落不进「本地」组。
        grouped: Dict[str, list] = {}
        for p in providers:
            pseudo_info = {
                "认证方式": getattr(p, "auth_type", "") or "",
                "API_URL": getattr(p, "api_url", "") or "",
            }
            group = _group_of(p.name, pseudo_info)
            grouped.setdefault(group, []).append(p)

        first_group = True
        for group in _GROUP_ORDER:
            items = grouped.get(group)
            if not items:
                continue
            self._add_group_header(group, len(items), _GROUP_COLORS[group], first=first_group)
            first_group = False
            self._add_flow_row(
                [
                    (
                        p.name,
                        _provider_caption(getattr(p, "api_url", ""), getattr(p, "family", ""), group),
                    )
                    for p in items
                ]
            )

        # 「+ 自定义」入口（固定末位）
        self._layout.addSpacing(_CUSTOM_SPACING)
        self._add_group_header("其他", 1, Colors.TEXT_MUTED, first=False)
        self._add_flow_row([(CUSTOM_ENTRY, "手动填写全部参数")])

    def _add_flow_row(self, entries: List[tuple]) -> None:
        """一行 FlowLayout（卡片固定尺寸，按可用宽度自动换行）"""
        row = QWidget(self)
        row.setStyleSheet("background: transparent;")
        flow = FlowLayout(row, spacing=_TILE_SPACING, margins=0)
        for provider_name, caption in entries:
            tile = ProviderPickerTile(provider_name, caption, row)
            tile.picked.connect(self._on_tile_picked)
            flow.addWidget(tile)
            self._tiles.append(tile)
        self._layout.addWidget(row)

    def _add_group_header(self, group: str, count: int, color: str, first: bool) -> None:
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(4, 0 if first else _GROUP_HEADER_TOP, 4, 6)
        layout.setSpacing(8)
        anchor = QWidget(header)
        anchor.setFixedSize(3, 12)
        anchor.setStyleSheet(f"background: {color}; border: none; border-radius: 1px;")
        layout.addWidget(anchor)
        label = QLabel(f"{group}（{count}）", header)
        label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; {font_size_css(12)} font-weight: 600; {get_font_family_css()}")
        layout.addWidget(label)
        layout.addStretch(1)
        if not first:
            self._layout.addSpacing(max(0, _GROUP_SPACING - _GROUP_HEADER_TOP))
        self._layout.addWidget(header)

    def _on_tile_picked(self, provider_name: str):
        if provider_name == CUSTOM_ENTRY:
            self.customPicked.emit()
        else:
            self.providerPicked.emit(provider_name)

    def tiles(self) -> List[ProviderPickerTile]:
        """已构建的卡片（测试用）"""
        return list(self._tiles)

    def provider_names(self) -> List[str]:
        return [t.provider_name for t in self._tiles]


def preset_provider_summary(provider_name: str) -> Optional[Dict[str, str]]:
    """给编辑表单用的预置参数摘要（URL / 认证方式 / 默认模型）。

    取值全部走 `provider_ui_meta` 基座（插件声明优先），与编辑卡其余部分同源。
    """
    from app.plugins.registries.provider_registry import ProviderRegistry
    from app.utils.provider_ui_meta import get_auth_type, get_preset_urls

    try:
        p = ProviderRegistry.get_instance().get(provider_name)
    except Exception:
        p = None
    if p is None:
        return None
    urls = get_preset_urls(provider_name)
    return {
        "API_URL": urls[0] if urls else "",
        "认证方式": get_auth_type(provider_name),
        "模型名称": str(getattr(p, "default_model", "") or ""),
    }
