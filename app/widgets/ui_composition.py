# -*- coding: utf-8 -*-
"""UIComposition — UI 模块装配器（Phase F）

按 module_ids 顺序构建 UIModule，调用 host.setattr 挂回产物。
单模块异常不中断其他模块的装配——记 warning 日志并报告 failed 状态。

宿主（OpenAIChatToolWindow / 测试 stub）根布局由 root_layout_factory 创建；
主程序路径下根 QVBoxLayout 已在 setup_ui 头部建好，传 `lambda h: None` 跳过根创建。
"""

from typing import Any, Callable, Dict, List, Optional

from loguru import logger


def compose(
    host: Any,
    module_ids: List[str],
    root_layout_factory: Optional[Callable[[Any], Any]] = None,
) -> Dict[str, Optional[str]]:
    """按 module_ids 顺序装配 UIModule 到 host。

    Args:
        host: 宿主窗口（实现 IWindowHost 协议或鸭子属性访问）
        module_ids: 待装配的 module_id 列表（按序）
        root_layout_factory: 可选根布局工厂；调用后返回值无要求，None 时跳过；
            返回值非 None 的场景：测试/自定义宿主无根布局时用此创建。
            主程序路径根布局已建好，传 `lambda h: None` 跳过。

    Returns:
        {module_id: 状态} 字典——"system" / 插件名 / "failed" / 缺失时 None
    """
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    if root_layout_factory is not None:
        try:
            root_layout_factory(host)
        except Exception as e:
            logger.warning(f"[UIComposition] root_layout_factory 失败: {e}")

    reg = UIPluginRegistry.get_instance()
    report: Dict[str, Optional[str]] = {}
    for module_id in module_ids:
        module = reg.get_ui_module(module_id)
        if module is None:
            report[module_id] = None
            continue
        # 推断胜者 plugin_name：取该 slot 中 priority 最大的 (按 tiebreaker: 后注册胜)
        slot = reg._ui_modules.get(module_id, [])
        if slot:
            _idx, winner = max(enumerate(slot), key=lambda x: (x[1][1], x[0]))
            report[module_id] = winner[0]
        else:
            report[module_id] = "unknown"
        try:
            module.build(host)
        except Exception as e:
            logger.error(f"[UIComposition] 模块 {module_id} build 失败: {e}")
            report[module_id] = "failed"
    return report
