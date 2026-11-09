---
description: MCP（服务器配置）开发指南——文件位置、两种接入方式、关键约束、排障与样例
---

# MCP（服务器配置）组件开发

### 文件位置

```
<plugin>/.mcp.json
```

### 方式一：外部子进程 server

```json
{
    "servers": [
        {
            "name": "my-server",
            "command": "python",
            "args": ["-m", "my_mcp_server"],
            "env": {
                "API_KEY": "${API_KEY}"
            },
            "description": "MCP 服务器说明"
        }
    ]
}
```

约束：用户机器要有系统解释器且装了 `mcp`/`httpx`，打包版分发不可控 → 优先方式二。

### 方式二：进程内 gateway（2026-11 起）

工具 handler 留在宿主进程内执行，无需外部 python 子进程，打包版不依赖系统解释器。三步：

1. manifest `capabilities` 声明 `"mcp.register"`

2. UI 侧经 `caps_for` 取注册函数，登记 handler：

```python
from app.plugins.sdk import caps_for

register_mcp = caps_for("mcp.register", surface="ui")
register_mcp("browser", my_handler, tools=MCP_TOOLS)
```

- `my_handler(params_dict) -> {"text": str}`（可带 `"isError": bool`），gateway 负责包成 `CallToolResult`
- `tools` 是 `tools/list` 返回的 schema 列表
- 插件卸载时宿主按 plugin 自动注销，无需手动清理

3. `.mcp.json` 用 `type: http` + `${LOCAL_MCP}` 占位符：

```json
{
    "servers": [
        {
            "name": "browser",
            "type": "http",
            "url": "${LOCAL_MCP}/browser",
            "description": "进程内 browser 工具"
        }
    ]
}
```

- 宿主连接前把 `${LOCAL_MCP}` 替换为 gateway 运行时地址 `http://127.0.0.1:<port>/mcp`（端口 `:0` 由 OS 分配）
- `name` 必须与注册时的 server_name 一致（gateway 按路径尾段分发）

### 参考

- 能力清单与 gateway 规范：[docs/plugins/capabilities.md](../../../../../docs/plugins/capabilities.md)
- 完整规范：[docs/mcp.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/mcp.md)
- 系统案例：`plugins/system-mcp/.mcp.json`
