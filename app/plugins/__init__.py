# -*- coding: utf-8 -*-
"""
插件子系统（app.plugins）— 插件体系完全集中的独立包。

职责划分（按职责分子目录）：
- managers/   ：插件生命周期管理（PluginManager）
- registries/ ：各类注册表（服务商 ProviderRegistry / UI 插件 UIPluginRegistry /
                套餐用量获取器 coding_plan_fetcher）
- loaders/    ：插件文件扫描与热重载（plugin_tool_loader / provider_loader）

设计动机（万物为插件）：插件相关代码不再散落在 app/core 与 app/tools，
统一收口到本包，新增插件组件类型（如 providers）时改动面清晰可控。
"""