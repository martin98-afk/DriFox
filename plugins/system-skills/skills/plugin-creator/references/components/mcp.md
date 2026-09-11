---
description: MCP（服务器配置）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# MCP（服务器配置）组件开发

### 文件位置

```
<plugin>/.mcp.json
```

### 模板

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

### 参考

- 完整规范：[docs/mcp.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/mcp.md)
- 系统案例：`plugins/system-mcp/.mcp.json`

---
