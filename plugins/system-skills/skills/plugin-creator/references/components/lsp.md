---
description: LSP（语言服务器）开发指南——文件位置、最小模板、关键约束、排障与样例
---

# LSP（语言服务器）组件开发

### 文件位置

```
<plugin>/.lsp.json
```

### 模板

```json
{
    "servers": [
        {
            "language": "python",
            "command": "pyright-langserver",
            "args": ["--stdio"],
            "description": "Python 语言服务器"
        }
    ]
}
```

### 参考

- 完整规范：[docs/lsp.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/lsp.md)
- 系统案例：`plugins/system-mcp/.lsp.json`

---
