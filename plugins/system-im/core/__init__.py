# -*- coding: utf-8 -*-
"""system-im 网关内核（插件所有物）。

从宿主 ``app/gateway/`` 迁入的 IM 业务模块，按职责分层：

- ``config``            网关配置读取（平台启用状态、附件阈值等）
- ``text_split``        长文本分片
- ``media``             图片/文件下载、落盘与上传
- ``interaction``       IM 内交互提问（question 工具转选项消息）
- ``session_manager``   平台会话 ↔ DriFox 会话映射（按 chat_id 共享）
- ``message_handler``   入站消息路由
- ``manager``           平台管理器：适配器装载、启停、统一收发

依赖方向（硬约束）：

1. **宿主契约留在宿主**：``app.gateway.base``（MessageEvent / BasePlatformAdapter /
   SendResult / Platform / PlatformConfig / get_cache_dir）与
   ``app.gateway.group_history`` 属契约层，插件直接 import，不复制。
2. **平台适配器不在这里**：各 IM 平台的适配器留在各自的 ``gateway-*`` 插件，
   通过宿主 ``GatewayPlatformRegistry`` 注册，本插件只按 platform_id 装载。
3. 新增一个 IM 平台 = 新增一个 gateway-* 插件，不改主程序、不改本插件。
"""
