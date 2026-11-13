# system-im — 即时通讯网关

把 DriFox 接入即时通讯平台：接收消息、按 `chat_id` 共享会话、媒体落盘与上传、
IM 内交互提问、主动投递。**平台适配器不在本插件内**，由各 `gateway-*` 插件提供。

## 边界

| 归属 | 内容 |
|------|------|
| 宿主（契约层） | `app/gateway/base.py`（`MessageEvent` / `BasePlatformAdapter` / `SendResult` / `Platform` / `PlatformConfig` / `get_cache_dir`）、`app/gateway/group_history.py`、`GatewayPlatformRegistry`、能力池 |
| 本插件 | 平台管理器、会话映射、消息路由、媒体、文本分片、交互提问、网关服务（Qt 信号） |
| 各 `gateway-*` 插件 | 单个 IM 平台的 SDK 接入与协议收发，注册 `GatewayPlatformDef` |

新增一个 IM 平台 = 新增一个 `gateway-*` 插件。主程序与本插件都不改。

## 目录

```
system-im/
├── .drifox-plugin/plugin.json   manifest（type=system，components.ui）
├── core/                        网关内核（从 app/gateway/ 迁入）
│   ├── config.py                网关配置
│   ├── text_split.py            长文本分片
│   ├── media.py                 媒体下载/落盘/上传
│   ├── attachments.py           出站 Markdown 附件提取 + 平台入参归一
│   ├── interaction.py           IM 内交互提问
│   ├── session_manager.py       平台会话 ↔ DriFox 会话映射
│   ├── message_handler.py       入站消息路由
│   └── manager.py               平台管理器（装载/启停/收发/重连）
├── engine.py                    GatewayEngine（headless 对话驱动）
├── service.py                   GatewayService（Qt 信号 + 控制面）
└── ui/
    ├── __init__.py              服务注册 + 设置卡注册 / 卸载收尾
    ├── core_loader.py           插件自有模块加载器（固定模块名）
    └── settings_card.py         IM 网关设置卡
```

## 模块加载

`ui/` 由宿主以 `ui_plugin_system-im` 加载，插件根与 `core/` 不在其包内，
统一由 `ui/core_loader.py` 按文件路径加载，模块名固定并写入 `sys.modules`：

| 模块名 | 内容 |
|--------|------|
| `system_im_core` | `core/` 包（内部用相对 import） |
| `system_im_engine` | `engine.py` |
| `system_im_service` | `service.py` |

三个前缀在 `plugin.json` 的 `module_prefixes` 中声明，热重载由宿主按声明清理。

## 能力与服务面

manifest 声明的宿主能力：`engine.session`、`engine.session.resume`、
`session.persist`、`model.config`、`net.upload`、`ui.notify`。

插件向宿主与其他插件暴露的服务经 `WindowServiceRegistry` 全局槽（key =
`im_gateway`）：宿主侧统一走 `app/core/services/gateway_access.py`，
其他插件走能力池 `platform.control`（`start` / `stop` / `status` /
`set_platform_enabled` / `restart_platform`），都不 import 插件内部模块。

## 设置卡

设置 → 插件页「即时通讯网关」：

- **总开关**：启停整个 IM 服务；状态写插件配置 `service_enabled`，
  下次启动按此决定是否自动连接（缺省开启）
- **平台列表**：按 `GatewayPlatformRegistry` 实时列出所有平台插件，
  显示 在线 / 连接中 / 已关闭 / 未就绪 / 错误（带原因），
  每行可单独启停与重连；2s 轮询，卡片隐藏即停

新增 gateway-* 平台插件后列表自动多一行，本插件与设置卡都不需要改。
