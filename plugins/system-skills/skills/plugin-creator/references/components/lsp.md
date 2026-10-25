---
description: LSP（语言服务器）开发指南——文件位置、真实 schema、启动门禁、排障与样例
---

# LSP（语言服务器）组件开发

### 文件位置

```
<plugin>/.lsp.json
```

### 真实 schema

**顶层直接是 `{服务器名: 配置}`，没有 `servers` 包装层。** 宿主解析器
（`app/core/lsp/lsp_config.py` 的 `load_lsp_configs`）遍历顶层 dict：
`for name, data in data.items()`。写成 `{"servers": [...]}` 会在
`LspManager` 报 `'list' object has no attribute 'get'`，LSP 列表为空。

```json
{
    "pyright": {
        "command": "node",
        "args": ["C:/path/to/langserver.index.js", "--stdio"],
        "extensionToLanguage": { "py": "python" },
        "installHint": "npm install -g pyright"
    }
}
```

### 字段全集（LspServerConfig.from_dict）

| 字段 | 默认 | 说明 |
|------|------|------|
| `command` | 服务器名 | 二进制名（须在 PATH）或绝对路径 |
| `args` | `[]` | 启动参数；含 shell 元字符（分号、管道符、反引号、`$(`）会被安全门禁直接拒启 |
| `extensionToLanguage` | `{}` | 扩展名 → 语言 id，key 不带点会自动补 |
| `initializationOptions` / `settings` / `env` | `{}` | 透传服务器 |
| `startupTimeout` | `10000` | 启动超时（毫秒） |
| `maxRestarts` | `3` | 崩溃最大重启次数 |
| `transport` | `stdio` | `stdio` 或 `socket` |
| `installHint` | `""` | 设置卡显示的安装提示命令 |
| `triggerDiagnostics` / `cliFallback` | `null` | 诊断触发 / CLI 兜底（进阶） |

### 启动门禁（app/core/tools/mcp_lsp_safety.py）

| 来源 | 判定 |
|------|------|
| 项目 `plugins/`（系统插件根） | `proceed` 直接放行 |
| 用户级 `~/.drifox/plugins` | 首启 `need_confirm`：设置 → LSP 卡该行显示「安全确认」按钮，放行一次即可（确认绑定 command+args 指纹，配置变更后需重新确认） |
| args 含 shell 元字符 | `denied` 拒启 |

### Windows 坑

`pyright-langserver` 等 npm 全局命令是 `.cmd` shim，进程直启
`CreateProcess` 找不到（WinError 2）。`command` 写 `node.exe` 绝对路径，
`args` 直跑包内 JS 入口（如 `pyright` 包的 `langserver.index.js`）。

### 参考

- 宿主实现：`app/core/lsp/lsp_config.py`（解析）、`lsp_manager.py`（注册/门禁）
- 市场文档：[docs/lsp.md](https://github.com/martin98-afk/drifox-plugins/blob/main/docs/lsp.md)（若与宿主源码冲突，以宿主为准）

---
