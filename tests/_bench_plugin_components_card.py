# -*- coding: utf-8 -*-
"""临时诊断脚本（不进 CI）：测量插件组件卡的构建耗时分布

用法：
    QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe tests/_bench_plugin_components_card.py

关心里程碑：
1. PluginManager 初始化 + 插件扫描
2. 卡片整体创建（含 _rebuild → 挂载小节）
3. 单个小节 / 单个组件行 / 单个 SwitchButton 的构造成本
"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication([])

from app.utils.utils import get_app_data_dir  # noqa: E402


def bench(label, fn, *args, **kwargs):
    t = time.perf_counter()
    out = fn(*args, **kwargs)
    print(f"{label:<42} {time.perf_counter() - t:8.3f}s")
    return out


from app.plugins.managers.plugin_manager import PluginManager  # noqa: E402

pm = PluginManager.get_instance()
bench("PluginManager.initialize", pm.initialize, get_app_data_dir())

enabled = pm.get_enabled_plugins()
print(f"{'已启用插件':<40} {len(enabled):>8}")
comps = sum(len([c for c, v in p.components.items() if v]) for p in enabled)
print(f"{'组件总数（插件×组件）':<38} {comps:>8}")

from app.widgets.cards.settings import plugin_components_card as m  # noqa: E402
from app.widgets.cards.settings.plugin_components_card import (  # noqa: E402
    ComponentRow,
    PluginComponentsCard,
    PluginSectionWidget,
)

# 单个零件成本
first = enabled[0] if enabled else None
if first:
    comps0 = [c for c, v in first.components.items() if v]
    bench(f"ComponentRow ×{len(comps0)}", lambda: [ComponentRow(c, True) for c in comps0])
    bench("PluginSectionWidget ×1", PluginSectionWidget, first.name, "", True, comps0)

bench("PluginComponentsCard（整卡创建）", PluginComponentsCard)

# 单个零件成本（第二次，排除首次字体/样式初始化的一次性开销）
if first:
    comps0 = [c for c, v in first.components.items() if v]
    bench(f"ComponentRow ×{len(comps0)}（第二轮）", lambda: [ComponentRow(c, True) for c in comps0])

# 整个设置面板（用户感知的"初次打开卡"是这里）
try:
    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    bench("LLMSettingsCard（整个设置面板）", LLMSettingsCard)
except Exception as e:
    print(f"LLMSettingsCard 创建失败: {type(e).__name__}: {e}")
