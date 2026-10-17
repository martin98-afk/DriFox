# MCP OAuth 登录

DriFox 主程序的 MCP 客户端支持 OAuth 2.1 授权码流程（含 PKCE 与动态客户端注册），
用于连接 Notion、Figma、Linear 这类只接受 OAuth 的远程 MCP 服务器。

## 一、能力范围

| 能力 | 状态 | 说明 |
|---|---|---|
| Protected Resource Metadata 发现（RFC 9728 / SEP-985） | 支持 | 由 mcp SDK 实现 |
| Authorization Server Metadata 发现（RFC 8414） | 支持 | 新旧 URL 格式都尝试 |
| PKCE（S256） | 支持 | 由 mcp SDK 生成与校验 |
| 动态客户端注册 DCR（RFC 7591） | 支持 | 无预置 clientId 时自动注册 |
| 本地 HTTP 回调 | 支持 | `http://127.0.0.1:<随机端口>/callback` |
| token 自动刷新 | 支持 | SDK 在 401 时先试 refresh，失败才重授权 |
| 静态 bearer token | 支持 | 直接写 `.mcp.json` 的 `headers`，无需 OAuth |
| query / url 型凭证注入 | **不支持** | 见「已知缺口」 |

## 二、在插件中声明

`.mcp.json` 里给 server 加 `auth` 段：

```json
{
  "mcpServers": {
    "notion": {
      "type": "http",
      "url": "https://mcp.notion.com/mcp",
      "enabled": true,
      "auth": {
        "type": "oauth",
        "client_name": "DriFox",
        "scope": ""
      }
    }
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | 是 | 固定 `oauth`。其他值（`bearer` / `none`）走原有路径，不触发授权 |
| `client_name` | 否 | DCR 注册时显示的客户端名，缺省 `DriFox` |
| `scope` | 否 | 覆盖授权服务器的默认 scope，空串用其广告值 |

`auth` 段经 `PluginManager._build_mcp_entry` 原样透传，并在 `update_mcp_server`
写入时保留（已加入 `_MCP_PERSIST_FIELDS`），从 UI 编辑配置不会丢。

## 三、代码结构

```
app/core/mcp_oauth/
├── __init__.py         导出 McpOAuthManager
├── token_store.py      实现 mcp SDK 的 TokenStorage 协议（落盘）
├── callback_server.py  127.0.0.1 随机端口临时 HTTP server 收授权码
├── provider.py         组装 OAuthClientProvider + redirect/callback handler
└── manager.py          单例门面，对外同步接口
```

接线点只有两处：

- `app/tools/mcp_tools.py`：新增 `_get_oauth_provider(config)`，在
  `_lifespan_http` 里判断 `auth.type == "oauth"` 时把 provider 传给
  `streamablehttp_client(auth=...)`。非 OAuth 服务器走原 `headers` 路径，行为不变。
- `app/plugins/managers/plugin_manager.py`：`_build_mcp_entry` 透传 `auth` 字段。

### 为什么不自己实现 OAuth

`mcp` SDK 1.28.1 的 `OAuthClientProvider`（继承 `httpx.Auth`）已实现完整的
发现、PKCE、DCR、401 重授权与刷新。自建一套约 1200 行且要与 SDK 演进对抗，
收益为零。适配层只做三件事：实现 `TokenStorage` 协议、提供两个回调 handler、
把 provider 接进连接生命周期。

## 四、token 存储

位置：`<app_data_dir>/plugin_data/<plugin_name>/config.json`，键名
`_mcp_oauth_<server_name>`。

```json
{
  "_mcp_oauth_notion": {
    "token": {
      "access_token": "...",
      "token_type": "Bearer",
      "expires_in": 28800,
      "refresh_token": "...",
      "scope": "default"
    },
    "expires_at": 1760412497.0,
    "client_info": {
      "client_id": "...",
      "client_secret": "...",
      "token_endpoint_auth_method": "client_secret_basic",
      "redirect_uris": ["http://127.0.0.1:58665/callback"]
    }
  }
}
```

复用 `PluginConfigStore`，因此自带原子写（`.json.tmp` + `os.replace`）、
路径穿越防护（`_safe_name`）、损坏降级（改名 `.json.corrupt`）。

### 安全须知

**token 明文存储。** 能读该文件的进程即可取得 access token；备份或同步
`plugin_data/` 时 token 会随之流转。这是已知的取舍，不是疏漏。
若未来需要加固，优先考虑接系统密钥链（Windows Credential Manager），
但那会带来跨平台后端差异与迁移成本。

`expires_at` 是适配层额外记录的绝对过期时刻，供 UI 展示「是否已授权」与排查。
SDK 自己只用 `expires_in` 在内存里现算，不读这个字段。

## 五、授权流程

```
用户点「授权登录」
  → CallbackServer.start()  拿到随机端口，redirect_uri 确定
  → 改写 provider 的 client_metadata.redirect_uris（占位端口 → 实际端口）
  → SDK async_auth_flow 被驱动，首个 401 触发：
       PRM 发现 → OASM 发现 → DCR 注册 → 拼授权 URL
  → redirect_handler 调 webbrowser.open()
  → callback_handler 阻塞等待（默认 300s）
  → 浏览器跳 127.0.0.1:<port>/callback?code=..&state=..
  → 本地校验 state（SDK 内 secrets.compare_digest）
  → token 交换，PluginMcpTokenStore 落盘
  → CallbackServer.close()
```

### 异常路径

| 情况 | 表现 |
|---|---|
| 用户关掉浏览器不授权 | 等 300s 超时，`status=error`，提示「授权超时」 |
| 授权服务器返回 `error=access_denied` | 立即返回，`error_description` 透出 |
| 回调缺少 `code` | `error=missing_code` |
| 授权服务器不支持 DCR | SDK 抛 `OAuthRegistrationError`，提示需手动配置 clientId |
| token 交换失败 | SDK 抛 `OAuthTokenError`，透出 message |
| 重复点授权按钮 | 同 key 已有 pending 会话则复用，不新起回调 server |

## 六、SDK 兼容性补丁

`provider.py::_patch_token_auth` 修正 mcp SDK 的一个实现偏差：

RFC 6749 §2.3.1 规定 `client_secret_basic` 模式下客户端凭据只能出现在
`Authorization: Basic` header，请求体不得再带 `client_id`。SDK 的
`prepare_token_auth` 只从 body 删了 `client_secret`，`client_id` 仍留在 body。
严格实现的授权服务器（Notion）会返回：

```
400 {"error":"invalid_request",
     "error_description":"Client must not use multiple authentication methods"}
```

补丁包一层 `prepare_token_auth`，在检测到 basic 认证时把 `client_id` 也从
body 移除。`none` / `client_secret_post` 两种模式不干预。

若 SDK 升级修复了此问题，可移除该补丁；有回归测试
`test_basic_auth_strips_client_id_from_body` 守护。

## 七、已知缺口

**query / url 型凭证注入未实现。** HanaAgent 等客户端允许在声明里指定
「这个 key 拼到 URL query 参数」或「拼进 URL 路径」。DriFox 的 MCP 凭证通路
目前只有 `headers` 透传。高德、百度地图等国内服务多为 query 型，暂不可用。

**无授权等待 UI。** 当前 `authorize()` 是同步阻塞调用，授权期间无应用内反馈，
只开浏览器 + 写日志。若要加设置卡按钮与状态提示，需要额外接 UI 信号。

**阻塞调用线程。** `authorize()` 内部 `asyncio.run` + 回调等待，最长阻塞 300 秒。
放在 UI 线程会冻界面。接入设置卡时必须放工作线程。

## 八、复现联调

`.mcp.json` 被 `.gitignore` 第 53 行的 `*.json` 全局忽略（既有约定，所有插件的
`.mcp.json` 都不入库），因此演示插件不随仓库分发。复现步骤：

1. 建 `plugins/<任意插件名>/.drifox-plugin/plugin.json`，`components` 声明 `"mcp": true`
2. 建同名 `.mcp.json`，内容见第二节示例（url 用 `https://mcp.notion.com/mcp`）
3. 起一个脚本：`PluginManager().initialize(get_app_data_dir())` → `get_mcp_servers()`
   取到该 server → `McpOAuthManager.get_instance().authorize(plugin, name, url, auth)`
4. 浏览器完成 Notion 登录，回到脚本看 `session.status`

注意 `authorize()` 是同步阻塞调用（最长 300 秒），不要放在 UI 线程。

## 九、验证记录

2026-10-14 对 Notion 官方 MCP（`https://mcp.notion.com/mcp`）完成端到端联调：

- DCR 注册成功，`client_id=UrCjDc-wMmvaN_q4`，`token_endpoint_auth_method=client_secret_basic`
- 授权 URL 含 PKCE S256 与 `resource` 参数
- 本地回调收到 code 与 state
- token 交换成功，`expires_in=28800`，`scope=default`
- 用该 token 走 `MCPClientManager` 真实连接路径握手成功，发现 **55 个工具**
  （notion-search / notion-fetch / notion-create-pages 等）

回归测试：`tests/core/test_mcp_oauth.py`，36 个用例。
