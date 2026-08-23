# -*- coding: utf-8 -*-
"""UIModule 契约 — UI 模块级扩展（替换整个区域实现）

三层灵活性模型：
- 条目级（ui_slots.Region/SlotEntry）：往既有区域加条目 —— 一期
- 模块级（本契约）：替换整个区域的装配实现 —— 二期
- 页面级（WorkspacePage，三期）：提供全新主页面

铁律：build 产物属性必须 setattr 挂回 host（与原 setup_ui 同名），
宿主类其余代码靠属性访问，属性名变更 = 破坏性重构，禁止。
"""

from typing import Any


class UIModule:
    """UI 装配模块基类 — build/teardown 生命周期由 UIComposition 驱动"""

    module_id: str = ""  # 子类必填：模块槽 ID（如 "chat_area"）

    def build(self, host: Any) -> None:
        """构建模块 UI。所有产物 setattr(host, <原属性名>, <widget>)。

        Args:
            host: 宿主窗口（OpenAIChatToolWindow / 实现 IWindowHost 的测试 stub）。
                  根布局经 host.layout() 获取（首个模块 build 前需已创建）。
        """
        raise NotImplementedError

    def teardown(self, host: Any) -> None:
        """销毁模块产物（默认空：Qt 父子树随窗口销毁；有外部资源才需实现）"""
