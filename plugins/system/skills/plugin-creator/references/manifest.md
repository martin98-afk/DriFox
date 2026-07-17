---
description: plugin.json 字段定義、校驗規則與完整示例
---

# Plugin Manifest 參考

> `plugin.json` 是插件的「身份證」，DriFox 識別與加載插件的唯一入口。

---

## 位置與命名

```
<plugin-name>/.drifox-plugin/plugin.json
```

路徑**必須**是 `<plugin-name>/.drifox-plugin/plugin.json`，不能換名。

---

## Schema 校驗

所有合法 manifest 必須通過 [schemas/plugin.schema.json](https://github.com/martin98-afk/drifox-plugins/blob/main/schemas/plugin.schema.json) 校驗：

```bash
# 在 drifox-plugins 的本地 clone 中執行
python tools/validate_plugins.py
```

> 沒有本地 clone？先 `git clone https://github.com/martin98-afk/drifox-plugins.git`

---

## 字段總表

### 必填字段

| 字段 | 類型 | 說明 |
|------|------|------|
| `name` | string | 插件名，必須與目錄名一致，小寫 kebab-case `^[a-z][a-z0-9-]{1,63}$` |
| `description` | string | 一句話說明（≤200 字） |
| `version` | string | SemVer 2.0，如 `"1.0.0"` |
| `components` | object | 啟用的組件清單（至少啟用一個） |

### 選填字段

| 字段 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `author` | string \| object | 無 | 作者，string 或 `{"name":"x","email":"x@y.z","url":"..."}` |
| `homepage` | string (URI) | 無 | 插件主頁 |
| `repository` | string (URI) | 無 | 源代碼倉庫 |
| `license` | string | `"GPL-3.0-or-later"` | SPDX 標識符 |
| `type` | enum | `"user"` | `"user"`（用戶插件）\| `"system"`（系統級，需簽名） |
| `keywords` | string[] | 無 | 檢索關鍵詞 |
| `drifox` | object | 無 | DriFox 兼容性聲明 |
| `dependencies` | object | 無 | 插件間依賴 |

### `components` 子字段

```json
"components": {
  "commands": true,    // commands/<name>.md → 斜杠命令
  "agents": true,      // agents/<name>.md → @<name> 智能體
  "skills": true,      // skills/<name>/SKILL.md → AI 技能
  "themes": true,      // themes/<name>/*.yaml → 主題方案
  "hooks": true,       // hooks/hooks.json + <plugin>_hook.py
  "mcp": true,         // .mcp.json → MCP 伺服器
  "lsp": true,         // .lsp.json → LSP 語言伺服器
  "ui": true           // ui/__init__.py → UI 組件
}
```

每個 `true` 的 flag 必須有對應目錄/文件（否則 `validate_plugins.py` 報錯）。

### `drifox` 兼容性

```json
"drifox": {
  "min_version": "0.5.0",
  "max_version": "1.x",
  "events": ["SessionStart", "PostToolUse"]
}
```

### `dependencies` 依賴

```json
"dependencies": {
  "evolver": ">=1.0.0",
  "code-review": "^2.1.0"
}
```

---

## 完整示例

```json
{
    "name": "my-plugin",
    "description": "一句話描述插件功能",
    "version": "0.1.0",
    "author": {
        "name": "Your Name",
        "email": "your@email.com"
    },
    "homepage": "https://github.com/your/repo",
    "license": "MIT",
    "type": "user",
    "keywords": ["drifox", "my-plugin", "example"],
    "components": {
        "commands": true,
        "skills": true
    },
    "drifox": {
        "min_version": "0.5.0"
    }
}
```

---

## 校驗規則速查

| 規則 | 說明 |
|------|------|
| `name` | 必須 `^[a-z][a-z0-9-]{1,63}$`，與目錄名一致 |
| `version` | 必須符合 SemVer `^\d+\.\d+\.\d+(-[a-z0-9.-]+)?$` |
| `components.*` 啟用 | 對應目錄/文件必須存在 |
| `dependencies.*` | 被引用的插件也必須存在 |
| `description` | ≤200 字 |
| JSON Schema | 必須通過 `schemas/plugin.schema.json` |
