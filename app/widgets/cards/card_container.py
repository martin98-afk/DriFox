# -*- coding: utf-8 -*-
from typing import Dict, Optional

from loguru import logger
from PyQt5.QtCore import Qt, QEasingCurve, QEvent, QPropertyAnimation, QTimer, pyqtSignal
from PyQt5.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from app.utils.design_tokens import Colors
from app.widgets.cards.card_manager import CardManager, ContainerType


class CardContainer(QWidget):
    """通用卡片容器 - 每个容器只管理一个位置的卡片

    功能：
    - 管理同位置多张卡片的显示/隐藏
    - 单卡片互斥：同一时间只显示一张
    - 动态展开/收起容器（放开/限制最大高度，让 Qt 自然布局）
    - 只响应自己容器内的卡片事件
    """

    # 展开时放开最大高度限制，让 Qt 布局系统自动计算合适高度
    _EXPAND_MAX = 16777215

    # 停靠区展开后的轴向最小尺寸：
    # 展开动画结束后释放轴向 max 并锁定此最小尺寸，
    # 使外层 QSplitter 可自由拖拽调整占比，且不会被拖成 1px 细条。
    # 横向停靠区（LEFT/RIGHT）用 300 而非依赖 minimumSizeHint：
    # Qt 对复杂卡片的 minimumSizeHint 常返回远小于内容实际需求的宽度
    # （实测 file-tree=206、context=260），且 file-tree 在 dock 内 sizeHint=300、
    # context-usage-stats 硬编码 setMinimumWidth(300)，固定下限 300 对齐
    # 这些真实卡片的内容下限，避免窗口缩小时卡片被压扁/裁切。
    _DOCK_MIN_H = 300  # 横向停靠区（LEFT/RIGHT）最小宽
    _DOCK_MIN_V = 80  # 纵向停靠区（TOP/BOTTOM in splitter）最小高
    # 纵向停靠区（TOP/BOTTOM）首次展开时强制占对话区（vdock splitter）的
    # 最小比例，避免卡片天然尺寸过小（内容未测量 / 空卡片 / 异步加载）时
    # 容器被压成极矮细条。用户已手动拖拽调过比例（_dock_last_size>0）后
    # 不再覆盖其设定。左右栏为横向轴、卡片天然较宽，沿用最小宽即可。
    _DOCK_DEFAULT_RATIO_V = 0.30

    # 卡片可通过 setProperty(NO_ANIMATION_PROP, True) 声明不参与容器的展开/折叠动画
    # 适用场景：卡片自带 resize / 拖拽 等会持续触发 heightChanged 的交互，
    # 容器动画会与这些交互产生约一个动画时长的高度延迟 / 抖动。
    # 声明后，容器高度会直接 snap 到目标值，不再走 QPropertyAnimation。
    NO_ANIMATION_PROP = "noContainerAnimation"

    # ── 覆盖层模式信号 ──
    # 当容器处于覆盖层模式（overlay_mode）且卡片显隐状态变化时发射，
    # 携带 bool 参数：True=有可见卡片（需展示覆盖层），False=无可见卡片（需隐藏覆盖层）
    overlayStateChanged = pyqtSignal(bool)

    def __init__(self, container_type: ContainerType):
        super().__init__()
        self._container_type = container_type
        # 横向容器（LEFT/RIGHT 停靠区）：沿宽度轴折叠/展开；纵向容器沿高度轴
        self._horizontal = container_type in (ContainerType.LEFT, ContainerType.RIGHT)
        self._cards: Dict[str, QWidget] = {}
        self._card_manager: Optional[CardManager] = None
        self._window_id: Optional[str] = None  # 多窗口隔离
        self._expand_timer: Optional[QTimer] = None  # 防抖展开定时器
        self._expand_animation: Optional[QPropertyAnimation] = None  # 展开/折叠动画
        self._expand_retry_count = 0  # 实例变量：防止 _do_expand 无限重试（每个容器独立计数）
        # 最近一次展开算出的目标轴向尺寸（已含首次最小占比）。
        # 停靠模式下 splitter 按 sizeHint 分配空间（可能远小于目标），
        # 展开完成时由 _restore_dock_size 用此值显式 setSizes，避免占比被丢弃。
        self._last_expand_target = 0
        # ── 停靠模式（enable_dock_mode 开启）──
        # 容器位于 QSplitter 中：展开后释放轴向 max、占比交给 splitter 拖拽；
        # 折叠时记忆当前尺寸并显式归还空间；重开时恢复上次拖出的占比。
        self._dock_splitter = None  # 宿主 QSplitter（None = 普通布局模式）
        self._dock_last_size = 0  # 折叠前记忆的轴向尺寸（重开恢复用）
        # ── 覆盖层模式 ──
        # 容器不在 splitter 中展开/折叠，而是作为 QStackedWidget 的覆盖层。
        # 卡片显隐时直接 show/hide 容器，发射 overlayStateChanged 信号供外部切换 stack。
        self._overlay_mode = False
        self._setup_ui()

    # ── 轴向抽象：纵向容器操作高度，横向容器操作宽度 ──

    def _axis_max(self) -> int:
        return self.maximumWidth() if self._horizontal else self.maximumHeight()

    def _set_axis_max(self, value: int):
        if self._horizontal:
            self.setMaximumWidth(value)
        else:
            self.setMaximumHeight(value)

    def _set_axis_min(self, value: int):
        if self._horizontal:
            self.setMinimumWidth(value)
        else:
            self.setMinimumHeight(value)

    def _axis_current(self) -> int:
        return self.width() if self._horizontal else self.height()

    def _axis_natural(self) -> int:
        """layout 计算出的自然尺寸（轴向）"""
        if self._horizontal:
            return max(self._layout.sizeHint().width(), self._layout.minimumSize().width())
        return max(self._layout.sizeHint().height(), self._layout.minimumSize().height())

    def _axis_property(self) -> bytes:
        return b"maximumWidth" if self._horizontal else b"maximumHeight"

    def _setup_ui(self):
        """初始化UI"""
        if self._horizontal:
            self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Expanding)
            self.setMinimumWidth(0)
            self.setMaximumWidth(0)  # 默认折叠
        else:
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            self.setMinimumHeight(0)
            self.setMaximumHeight(0)  # 默认折叠

        self._layout = QVBoxLayout(self)
        if self._horizontal:
            self._layout.setContentsMargins(4, 0, 4, 0)  # 左右停靠区：横向 padding
        else:
            self._layout.setContentsMargins(0, 0, 8, 0)  # 右 padding 8px，让卡片内容不贴右边缘
        self._layout.setSpacing(0)

        # 应用主题背景
        self._apply_background_style()

    def _apply_background_style(self):
        """应用主题背景 + 边框

        使用主题的 card_bg 作为容器背景色，border 作为边框色。
        顶部 8px 圆角，底部直角（与下方输入框/页面边缘融合）。
        覆写此方法可定制子类的背景样式。
        """
        Colors.refresh()
        bg = Colors.CARD_BG.format(alpha=232)
        if self._overlay_mode:
            # 覆盖层模式：四角圆角独立面板视觉 + 较实背景，与对话区形成明确边界
            self.setStyleSheet(f"""
                CardContainer {{
                    background: {Colors.CARD_BG.format(alpha=246)};
                    border: 1px solid {Colors.BORDER};
                    border-radius: 8px;
                }}
            """)
            return
        if self._horizontal or self._dock_splitter is not None:
            # 停靠区（左右容器 / 启用停靠模式的上下容器）：
            # 四角圆角独立面板视觉 + 更实的背景，与对话区形成明确边界
            self.setStyleSheet(f"""
                CardContainer {{
                    background: {Colors.CARD_BG.format(alpha=246)};
                    border: 1px solid {Colors.BORDER};
                    border-radius: 8px;
                }}
            """)
            return
        self.setStyleSheet(f"""
            CardContainer {{
                background: {bg};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }}
        """)

    def refresh_style(self):
        """响应主题切换：刷新容器背景 + 边框

        主题切换时由 main_widget 的全局 refresh 链调用。
        """
        self._apply_background_style()

    @property
    def container_type(self) -> ContainerType:
        return self._container_type

    def bind_card_manager(self, card_manager: CardManager, window_id: str):
        """绑定 CardManager（多窗口隔离）"""
        self._card_manager = card_manager
        self._window_id = window_id

    # ── 停靠模式（QSplitter 协作协议） ──

    def enable_dock_mode(self, splitter):
        """启用停靠模式：容器位于 QSplitter 中，占比可拖拽调整

        协作协议（与 _do_expand / 折叠路径配合）：
        1. 展开动画结束 → 释放轴向 max、锁定停靠最小尺寸，宽/高交给 splitter；
        2. 用户拖拽 → 容器尺寸变化不回弹（已展开分支早退）；
        3. 折叠 → 先记忆当前尺寸（_dock_last_size），动画收到 0 后 hide()
           并显式把空间归还给 splitter 邻居（_release_dock_space）；
        4. 重新展开 → show() 后动画展开，完成时若有记忆尺寸则恢复上次占比。
        """
        self._dock_splitter = splitter
        # 停靠面板样式（四角圆角独立面板，与对话区边界更清晰）
        self._apply_background_style()
        # 折叠态直接隐藏：让 splitter handle 一并消失，避免 0 宽容器留下拖拽缝
        if self._axis_max() == 0:
            self.hide()

    def set_overlay_mode(self, enabled: bool):
        """启用覆盖层模式：容器作为 QStackedWidget 覆盖层，不参与 splitter 展开/折叠

        覆盖模式下：
        - 卡片显示时容器直接 show() 并放开轴向 max（填满覆盖层空间）
        - 卡片全部隐藏时容器 hide() 并锁定轴向 max=0
        - 发射 overlayStateChanged 信号供外部（TabManagerWindow）切换 QStackedWidget 页面
        """
        self._overlay_mode = enabled
        if enabled:
            self._set_axis_max(0)
            self._set_axis_min(0)
            self.hide()
            # 覆盖模式使用四角圆角独立面板样式
            self._apply_background_style()

    def _dock_min(self) -> int:
        return self._DOCK_MIN_H if self._horizontal else self._DOCK_MIN_V

    def _visible_cards_min_axis(self) -> int:
        """当前可见卡片的最小轴向尺寸（横向取宽、纵向取高），至少 0

        停靠模式展开时用于提升 min 锁：窗口缩小时 QSplitter 优先保证
        卡片内容完整可见的最小尺寸，对话区最后被压缩（可被遮挡）。
        与 _schedule_expand 一致用 not isHidden() 判定可见性。

        同时取 minimumSizeHint() 与 minimumSize() 的轴向最大值：两者是
        独立属性——硬编码 setMinimumWidth/Height 的卡片（如 context-usage-
        stats setMinimumWidth(300)、tool_control setMinimumHeight(250)）
        只有 minimumSize 生效，minimumSizeHint 可能是布局算出的更小值；
        minimumSizeHint 无布局时返回无效尺寸 (-1,-1)，max() 自然兜底。
        """
        axis_min = 0
        for w in self._cards.values():
            if w.isHidden():
                continue
            hint = w.minimumSizeHint()
            if self._horizontal:
                value = max(hint.width(), w.minimumWidth())
            else:
                value = max(hint.height(), w.minimumHeight())
            if value > axis_min:
                axis_min = value
        return axis_min

    def _splitter_index(self) -> int:
        if self._dock_splitter is None:
            return -1
        return self._dock_splitter.indexOf(self)

    def _remember_dock_size(self):
        """折叠前记忆当前轴向尺寸，供下次展开恢复占比"""
        cur = self._axis_current()
        if cur >= self._dock_min():
            self._dock_last_size = cur

    def _restore_dock_size(self, target: int):
        """展开完成后：把占比交还给外层 QSplitter（显式 setSizes）

        target 已是含最小占比下限的最终目标尺寸。setSizes 是一次性覆盖，
        可能被后续异步布局重排冲掉；真正的稳定兜底来自 _release_to_splitter
        锁定的 minimumHeight（见下），此处仅做精确归位。
        """
        sp = self._dock_splitter
        idx = self._splitter_index()
        if sp is None or idx < 0:
            return
        if target < self._dock_min():
            return
        sizes = sp.sizes()
        if idx >= len(sizes):
            return
        delta = target - sizes[idx]
        if delta == 0:
            return
        # 从最大的邻居窗格里借空间，保持其余窗格不动
        donor = max(
            (i for i in range(len(sizes)) if i != idx),
            key=lambda i: sizes[i],
            default=-1,
        )
        if donor < 0 or sizes[donor] - delta < 0:
            return
        sizes[idx] = target
        sizes[donor] -= delta
        sp.setSizes(sizes)

    def _release_dock_space(self):
        """折叠完成后：hide + 显式把空间归还给 splitter 邻居

        QSplitter 不会因子控件 maximumWidth/Height 变 0 而自动重排，
        必须 setSizes 归还，否则内容区右侧留白（“关闭后不恢复”问题根因）。
        """
        self.hide()
        sp = self._dock_splitter
        idx = self._splitter_index()
        if sp is None or idx < 0:
            return
        sizes = sp.sizes()
        if idx >= len(sizes) or sizes[idx] == 0:
            return
        freed = sizes[idx]
        sizes[idx] = 0
        # 归还给最大的邻居（通常是内容区）
        donor = max(
            (i for i in range(len(sizes)) if i != idx),
            key=lambda i: sizes[i],
            default=-1,
        )
        if donor >= 0:
            sizes[donor] += freed
        sp.setSizes(sizes)

    def resizeEvent(self, event):
        """窗口缩小时联动宿主 splitter：溢出则把 dock 压回最小尺寸

        在停靠模式下覆盖：QSplitter 压缩空间时优先压 stretch 大的窗格
        （对话区），压到 0 后若仍溢出（如用户曾把 dock 拖大），QSplitter
        会保持 dock 当前尺寸宁可溢出——导致右侧 dock 超出窗口被裁切。
        此处延迟到 resize 完成后检查溢出，显式把各 dock 压回最小尺寸、
        对话区让位吸收剩余空间。
        """
        super().resizeEvent(event)
        if self._dock_splitter is not None and not self._overlay_mode:
            # 延迟到 resize 传播链结束（self 尺寸已更新）再检查，避免重入
            QTimer.singleShot(0, self._ensure_splitter_fits)

    def _ensure_splitter_fits(self):
        """splitter 总尺寸溢出可视区时，把 dock 分项压回各自最小尺寸

        只处理展开态/可见 dock。非 dock 窗格（对话区）先压到其轴向最小值
        （外层已把 minimumWidth 让位为 0），再压 dock 到轴向最小值。
        QSplitter.sizes() 不包含 handle 宽度，可视区 = width - handle 总宽。
        """
        sp = self._dock_splitter
        if sp is None or sp.width() <= 0:
            return
        sizes = sp.sizes()
        handle_total = sp.handleWidth() * max(0, sp.count() - 1)
        avail = sp.width() - handle_total
        total = sum(sizes)
        if total <= avail:
            return
        overflow = total - avail

        horizontal = sp.orientation() == Qt.Horizontal
        new_sizes = list(sizes)

        # 1) 先压非 dock 窗格（对话区）到其最小轴
        for i in range(sp.count()):
            w = sp.widget(i)
            if w is None or isinstance(w, CardContainer):
                continue
            m = w.minimumWidth() if horizontal else w.minimumHeight()
            cut = min(new_sizes[i] - m, overflow)
            if cut > 0:
                new_sizes[i] -= cut
                overflow -= cut
        # 2) 再压 dock 窗格到各自轴向最小值（含自身：self 的 resize 传播链
        #    已完成，setSizes 触发的新 resize 由幂等检查与 singleShot 防抖）
        for i in range(sp.count()):
            w = sp.widget(i)
            if not isinstance(w, CardContainer):
                continue
            m = w.minimumWidth() if horizontal else w.minimumHeight()
            cut = min(new_sizes[i] - m, overflow)
            if cut > 0:
                new_sizes[i] -= cut
                overflow -= cut
        if new_sizes != sizes:
            sp.setSizes(new_sizes)

    def showEvent(self, event):
        """容器从隐藏变为可见时，如果有可见卡片则重新展开

        关键场景：Tab模式下，非活跃Tab触发了提问卡片后，切换到该Tab时
        容器需要重新展开（之前因父窗口隐藏，_do_expand 未正确展开）。
        """
        super().showEvent(event)
        # 延迟到当前 show 事件处理完成后再展开（此时布局已激活）
        # not isHidden()：容器自身隐藏期间被 setVisible(True) 的卡片
        # isVisible()/WA_WState_Visible 均为 False，但 isHidden() 能正确反映意图
        if any(not w.isHidden() for w in self._cards.values()):
            QTimer.singleShot(0, self._schedule_expand)

    def _is_expanded(self) -> bool:
        """容器是否已展开"""
        return self._axis_max() >= self._EXPAND_MAX

    def _should_skip_animation(self) -> bool:
        """当前可见卡片中是否存在声明跳过容器动画的卡片

        用于 resize 等高频高度变化场景，避免容器动画造成视觉延迟。
        """
        for w in self._cards.values():
            if w.isVisible() and w.property(self.NO_ANIMATION_PROP):
                return True
        return False

    def _on_card_shown(self, card_id: str):
        """某张卡片被显示"""
        if card_id not in self._cards:
            return
        self._schedule_expand()

    def _on_card_hidden(self, card_id: str):
        """某张卡片被隐藏"""
        if card_id not in self._cards:
            return
        self._schedule_expand()

    def _schedule_expand(self, source: str = ""):
        """防抖调度：有可见卡片则展开，否则折叠

        注意：不在"已展开"时早 return。
        子卡片（如 CommandCard 切 detail 模式）可能在已展开容器内
        通过 setFixedHeight 改变自身高度，需要容器重新跑 _do_expand
        以动画到新高度。_do_expand 内部会用 sizeHint 差异判断避免无谓动画。

        性能优化：source="resize" 时走 timer 路径防抖，避免布局级联中
        连续触发 _do_expand（每次都会 activate 布局 → 级联 Resize → 再触发）。
        卡片 show/hide 事件仍立即展开，防止父容器高度为 0 时卡片不可见。
        """
        # 窗口拖拽过程中跳过容器展开/折叠，防止布局级联干扰
        try:
            from app.utils.window_drag_state import any_window_dragging

            if any_window_dragging:
                return
        except ImportError:
            pass
        # not isHidden()：兼容停靠模式下容器自身处于 hide() 状态的场景
        has_visible = any(not w.isHidden() for w in self._cards.values())
        # 取消上次未执行的防抖
        if self._expand_timer:
            self._expand_timer.stop()
        else:
            self._expand_timer = QTimer(self)
            self._expand_timer.setSingleShot(True)
            self._expand_timer.setInterval(0)
            self._expand_timer.timeout.connect(self._do_expand)

        if has_visible:
            if source == "resize":
                # Resize 事件防抖：走 timer 路径，连续 Resize 只触发一次展开
                self._expand_timer.start()
            else:
                # 卡片 show/hide：立即展开，防止父容器高度为 0 时卡片不可见
                self._do_expand()
        else:
            # 无可见卡片：通过 timer 折叠（延迟一点没关系）
            self._expand_timer.start()

    def _do_expand(self):
        """执行展开/折叠动画（200ms 缓动，OutCubic）

        若当前唯一可见卡片声明了 NO_ANIMATION_PROP（例如自带 resize 的 todo 卡片），
        则跳过动画、直接 snap 到目标高度，避免容器高度在拖拽过程中滞后于卡片内容。
        """
        has_visible = any(not w.isHidden() for w in self._cards.values())
        skip_anim = self._should_skip_animation()

        # ── 覆盖层模式：不操作 splitter，直接 show/hide + 发射信号 ──
        if self._overlay_mode:
            if has_visible:
                self._set_axis_max(self._EXPAND_MAX)
                self._set_axis_min(0)
                self.show()
            else:
                self._set_axis_max(0)
                self._set_axis_min(0)
                self.hide()
            self._layout.activate()
            self.overlayStateChanged.emit(has_visible)
            return

        # 停靠模式：折叠态容器是 hide() 的，展开前必须先 show()
        # （否则动画属性变化对不可见 widget 无视觉效果，splitter 也不给它分配空间）
        if has_visible and self._dock_splitter is not None and self.isHidden():
            self.show()

        # 取消进行中的动画，避免叠加造成跳变
        if self._expand_animation is not None and self._expand_animation.state() == QPropertyAnimation.Running:
            self._expand_animation.stop()
            try:
                self._expand_animation.finished.disconnect()
            except TypeError, RuntimeError:
                pass

        # 解除轴向最小尺寸限制，确保折叠动画能跑到 0
        self._set_axis_min(0)

        if has_visible:
            # ── 停靠区已展开（轴向 max 已释放、尺寸由外层 QSplitter 控制）──
            # 卡片内容变化：仅恢复一个较小的绝对最小锁，保证不缩成 1px 细条，
            # 同时允许用户把槽位拖小（双向拖拽）。默认占比（30%）由"首次展开
            # 的 setSizes 归位"保证，不在此用 minimum 钳死，否则手柄只能单向拖。
            # 小窗口优先：锁 max(_dock_min, 可见卡片最小轴)，卡片内容变化后
            # 锁不降级，窗口缩小时仍优先保证卡片完整可见。
            if self._dock_splitter is not None and self._axis_max() >= self._EXPAND_MAX and self._axis_current() > 0:
                self._set_axis_min(max(self._dock_min(), self._visible_cards_min_axis()))
                return

            # ── 展开：snap 或动画到 layout 算出的自然尺寸（轴向） ──
            # 先放开轴向 max，让 layout 算出"展开后该有多大"
            self._set_axis_max(self._EXPAND_MAX)
            # 强制激活布局：修复 setUpdatesEnabled(False) 期间填充内容后，
            # layout.sizeHint() 可能返回过期缓存值（输入框隐藏但卡片不显示，
            # resize 窗口后恢复正常）。activate() 确保子 widget 的最新内容
            # 被计入容器尺寸。
            self._layout.activate()

            natural_h = self._axis_natural()
            if natural_h <= 0:
                # 兜底：layout 没算出高度，尝试强制子控件 adjustSize + 父级布局激活后重测
                for w in self._cards.values():
                    if w.isVisible():
                        try:
                            w.adjustSize()
                        except RuntimeError:
                            pass
                self._layout.activate()
                # 🛠️ 父级布局强制激活（仅在此重测路径中），让容器 height()
                # 反映正确的分配；主展开路径不激活父级，以保留动画触发时机。
                p = self.parent()
                pl = p.layout() if hasattr(p, "layout") else None
                if pl is not None:
                    pl.invalidate()
                    pl.activate()
                natural_h = self._axis_natural()

                if natural_h <= 0:
                    # 仍然为 0：渐进重试，让 Qt 在下一轮事件循环完成测量
                    self._expand_retry_count += 1
                    if self._expand_retry_count >= 2:
                        # 重试 2 次仍为 0：不锁为 0，保留 EXPAND_MAX 让父级
                        # 在后续 layout pass 中通过 sizeHint 自行分配尺寸。
                        self._expand_retry_count = 0
                        self._set_axis_max(self._EXPAND_MAX)
                        return
                    # singleShot(1) 而非 0：给 Qt 一个完整事件循环完成测量
                    QTimer.singleShot(1, self._do_expand)
                    return

            self._expand_retry_count = 0
            # ⚡ 读 current_h 前不激活父级布局 — 父级此刻仍用过期尺寸（如 0），
            # current_h ≈ 0 而 natural_h ≈ 200，差值巨大 → 触发平滑展开动画。
            # 父级布局在动画中通过轴向 max 属性的 updateGeometry 级联刷新。
            current_h = self._axis_current()

            # 停靠模式：计算展开目标 + 最小占比下限（首次/二次都生效）
            on_expand_done = None
            if self._dock_splitter is not None:
                # 纵向停靠区（TOP/BOTTOM）强制至少占对话区 _DOCK_DEFAULT_RATIO_V：
                # ① 首次展开，卡片天然尺寸过小（内容未测量/空卡片/异步加载）会被压成细条；
                # ② 更关键：二次及以后，用户拖拽/折叠时记忆的占比 _dock_last_size 也
                #    可能偏小，若不兜底就会缩回细条。因此无论是否拖拽过，都以下限
                #    max(记忆占比, 最小比) 钳制，保证不存在"小于最小占比"的细条。
                # 横向停靠区（LEFT/RIGHT）卡片天然较宽，沿用最小宽、尊重记忆占比即可。
                chat_h = self._dock_splitter.height()
                ratio_floor = int(chat_h * self._DOCK_DEFAULT_RATIO_V) if (not self._horizontal and chat_h > 0) else 0
                if self._horizontal:
                    target = self._dock_last_size if self._dock_last_size > natural_h else natural_h
                    # 小窗口优先：min 锁 = max(停靠最小宽, 可见卡片最小宽)，
                    # 窗口缩小时 splitter 优先保证卡片内容完整可见，对话区最后被压
                    min_floor = max(self._dock_min(), self._visible_cards_min_axis())
                else:
                    if self._dock_last_size > 0:
                        target = max(self._dock_last_size, ratio_floor)
                    else:
                        target = max(natural_h, ratio_floor)
                    # 持久下限用较小的绝对值（_dock_min），保证不缩成 1px 细条即可；
                    # 默认占比（30%）由 target 经 setSizes 一次性归位保证。若把
                    # minimum 钳到 30%，QSplitter 会禁止把手柄拖回更小，导致"单向拖"。
                    # 小窗口优先：min 锁再叠加可见卡片最小高，窗口缩小时卡片内容
                    # 不被压缩（对话区最后被压矮）。
                    min_floor = max(self._dock_min(), self._visible_cards_min_axis())

                def _release_to_splitter():
                    # minimum 钳制是稳定兜底：splitter 按 sizeHint 分配空间，纵向卡片
                    # sizeHint 常因异步加载变化，单次 setSizes 会被后续重排冲掉；用
                    # minimum 锁死下限后，无论 sizeHint 怎么变，splitter 都必须给到
                    # 至少该高度，首次/二次及以后都不会缩回细条。
                    self._set_axis_min(min_floor)
                    self._set_axis_max(self._EXPAND_MAX)
                    self._restore_dock_size(target)

                on_expand_done = _release_to_splitter
                # 记录本次展开目标（已含最小占比），供 _restore_dock_size 精确 setSizes
                self._last_expand_target = target
                natural_h = target
            else:
                self._last_expand_target = natural_h
                # 非停靠（普通布局）展开：完成后锁 min=自然高度，窗口缩小时
                # 卡片内容不被压缩；下次 _do_expand 开头 _set_axis_min(0) 解锁，
                # 动态高度变化（heightChanged → _schedule_expand）不会被卡死。
                on_expand_done = lambda: self._set_axis_min(natural_h)

            # 尺寸差异 < 2px 跳过动画，避免列表过滤/模式切换时无谓抖动
            if abs(natural_h - current_h) < 2:
                if on_expand_done is not None:
                    on_expand_done()
                return
            if skip_anim:
                self._set_axis_max(natural_h)
                if on_expand_done is not None:
                    on_expand_done()
                return
            self._animate_height(current_h, natural_h, on_finished=on_expand_done)
        else:
            # ── 折叠：snap 或动画到 0，结束后锁定轴向 max=0 ──
            # 停靠模式：折叠前记忆当前占比（重开时恢复）
            if self._dock_splitter is not None:
                self._remember_dock_size()

            def _on_collapsed():
                self._set_axis_max(0)
                if self._dock_splitter is not None:
                    self._release_dock_space()

            # 折叠前轴向 max 可能是 _EXPAND_MAX 或动画中间值，确保放开以读取真实尺寸
            if self._axis_max() < self._EXPAND_MAX:
                self._set_axis_max(self._EXPAND_MAX)
            current_h = self._axis_current()
            if current_h <= 0 or skip_anim:
                _on_collapsed()
                return
            self._animate_height(current_h, 0, on_finished=_on_collapsed)

    def _animate_height(self, start_h: int, end_h: int, on_finished=None):
        """用 QPropertyAnimation 动画轴向 max 属性（纵向 maximumHeight / 横向 maximumWidth），200ms OutCubic

        性能优化：复用 _expand_animation 对象，避免每次展开/折叠都创建新动画实例。
        动画已停止时：直接重置 start/end 值并 restart。
        动画运行中：stop 后被 _do_expand 截获并取消，不会进入此方法。
        """
        anim = self._expand_animation
        if anim is None:
            anim = QPropertyAnimation(self, self._axis_property())
            anim.setDuration(200)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            self._expand_animation = anim

        # 断开上次的 on_finished 回调（避免重复连接）
        try:
            anim.finished.disconnect()
        except TypeError, RuntimeError:
            pass

        anim.setStartValue(start_h)
        anim.setEndValue(end_h)

        if on_finished is not None:

            def _on_done():
                try:
                    on_finished()
                finally:
                    try:
                        if self._expand_animation is not None:
                            self._expand_animation.finished.disconnect(_on_done)
                    except TypeError, RuntimeError:
                        pass

            anim.finished.connect(_on_done)
        anim.start()

    def add_card(self, card_id: str, card_widget: QWidget):
        """添加卡片到容器，并注册专属回调"""
        self._cards[card_id] = card_widget
        self._layout.addWidget(card_widget)
        # 纵向停靠区（TOP/BOTTOM）：让卡片纵向填满槽位，使"可见卡片高度 = 槽位高度"。
        # 否则 Fixed/Preferred 的卡片在放大后的槽位里只占天然高度，视觉上仍是"细条"。
        # 横向停靠区保持卡片自带策略。
        if not self._horizontal:
            sp = card_widget.sizePolicy()
            card_widget.setSizePolicy(sp.horizontalPolicy(), QSizePolicy.Minimum)
        card_widget.setVisible(False)
        card_widget.installEventFilter(self)

        # 注册此卡片专属的回调（传入 window_id 用于多窗口隔离）
        if self._card_manager and self._window_id:
            self._card_manager.on_card_shown(self._window_id, card_id, self._on_card_shown)
            self._card_manager.on_card_hidden(self._window_id, card_id, self._on_card_hidden)

        # 连接卡片内部高度变化信号 → 容器重新展开（支持拖拽、自适应等动态高度）
        if hasattr(card_widget, "heightChanged"):
            card_widget.heightChanged.connect(self._schedule_expand)

    def remove_card(self, card_id: str):
        """从容器移除卡片"""
        if card_id not in self._cards:
            return

        widget = self._cards[card_id]
        widget.removeEventFilter(self)
        self._layout.removeWidget(widget)
        del self._cards[card_id]

        if len(self._cards) == 0:
            self._set_axis_max(0)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize and obj in self._cards.values():
            # 动画运行时，卡片 Resize 是动画自身级联副作用（容器撑大 → 卡片撑大），
            # 若触发 _schedule_expand → _do_expand 会取消进行中的动画并重启，
            # 形成"动画→Resize→打断→重启→Resize→..."的自激循环，导致高度抖动。
            # 跳过 Resize，让当前动画完成后再处理最终的尺寸。
            if self._expand_animation is not None and self._expand_animation.state() == QPropertyAnimation.Running:
                return False
            self._schedule_expand(source="resize")
        return super().eventFilter(obj, event)


class TopCardContainer(CardContainer):
    def __init__(self):
        super().__init__(ContainerType.TOP)


class BottomCardContainer(CardContainer):
    """下方卡片容器 - 底部直角设计，与输入框视觉融合"""

    def __init__(self):
        super().__init__(ContainerType.BOTTOM)

    def _apply_background_style(self):
        """底部容器背景：8px 上圆角 + 底部直角，与输入框视觉拼接"""
        Colors.refresh()
        bg = Colors.CARD_BG.format(alpha=232)
        self.setStyleSheet(f"""
            BottomCardContainer {{
                background: {bg};
                border: 1px solid {Colors.BORDER};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }}
        """)

    def add_card(self, card_id: str, card_widget: QWidget):
        """添加卡片并修正底部圆角，使其与下方输入框视觉融合"""
        # 修正卡片底部圆角为直角
        card_widget.setProperty("bottomCard", True)
        card_widget.style().unpolish(card_widget)
        card_widget.style().polish(card_widget)
        super().add_card(card_id, card_widget)
