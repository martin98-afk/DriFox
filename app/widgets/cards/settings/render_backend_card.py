# -*- coding: utf-8 -*-
"""渲染后端下拉卡：说明随所选档位变化。

基类的 OptionsSettingCard 只有一个共用 content，7 个档位挤在一行里说不清差别。
这里继承后把头部说明做成**跟着当前值走**的一行短描述，展开后的每个单选项再挂
一个 tooltip 写清「它是什么 / 什么时候用」。

描述刻意短（≤ 18 字）：它在 header 的 contentLabel 里，而 contentLabel 的
sizeHint 会沿布局链把设置弹窗顶宽（见 render_status_card 的同类坑）。
"""

from __future__ import annotations

from qfluentwidgets import FluentIcon, OptionsSettingCard, qconfig

# 档位 → 头部一行说明（短）。无 auto 档：它不检测机器、只是读人工放的标记文件。
DESCRIPTIONS = {
    "hardware": "真实显卡跑 D3D11，最快",
    "software": "CPU 模拟 D3D11，驱动崩时用",
    "software_gl": "Mesa 纯 CPU 光栅，最慢最稳",
    "vulkan": "换 Vulkan 路径，新显卡排障",
    "d3d9": "老系统 / 没有 D3D11 时兜底",
    "swiftshader": "Qt + Chromium 双侧 CPU 兜底",
}

# 档位 → 展开后 tooltip 详解（可以长）
TIPS = {
    "hardware": "Qt 走 ANGLE → D3D11，由真实显卡驱动执行。正常机器的首选，合成与滚动都交给 GPU。",
    "software": "Qt 仍走 ANGLE → D3D11，但 D3D11 设备换成 WARP（微软的 CPU 光栅器）。"
    "驱动崩溃但仍能用 D3D11 时的第一选择，比软件 GL 快。",
    "software_gl": "绕开 ANGLE / D3D11，Qt 用自带的 Mesa llvmpipe 走桌面 OpenGL。"
    "完全不碰显卡驱动，最慢也最稳；WARP 都不行时用它。",
    "vulkan": "QT_ANGLE_PLATFORM=vulkan。部分新显卡 / 新驱动上 Vulkan 路径比 D3D11 更稳。"
    "驱动 Vulkan 支持不全可能黑屏，只当排障试。",
    "d3d9": "QT_ANGLE_PLATFORM=d3d9。给老机器 / Win7 这类没有 D3D11 的系统兜底，Qt 5.15 里已边缘化。",
    "swiftshader": "Qt 侧走 WARP + Chromium 追加 --use-angle=swiftshader：双侧都绕开显卡驱动。"
    "Qt 的 GL 起不来、或 GPU 进程一开就崩时的双保险。",
}


class RenderBackendCard(OptionsSettingCard):
    """渲染后端选择卡：头部说明随档位变化 + 每档 tooltip 详解"""

    def __init__(self, configItem, texts, parent=None):
        super().__init__(configItem, FluentIcon.SPEED_HIGH, "渲染后端", content="", texts=texts, parent=parent)
        # 展开后的单选项挂 tooltip（基类只塞了 RadioButton，没有说明）
        for button in self.buttonGroup.buttons():
            button.setToolTip(TIPS.get(button.property(self.configName), ""))
        # setValue 会刷新头部说明；基类构造时已调过一次，这里再补一次确保文案就位
        self.setValue(qconfig.get(configItem))

    def setValue(self, value):
        """选中档位 → 同步单选项 + 头部说明"""
        super().setValue(value)
        self.card.setContent(DESCRIPTIONS.get(value, ""))
