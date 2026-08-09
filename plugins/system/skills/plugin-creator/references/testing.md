---
description: 插件測試、驗證與除錯指南
---

# 測試與驗證

---

## 1. 本地熱更新測試

DriFox 使用 **watchfiles** 實現插件熱更新：

| 修改內容 | 更新方式 | 延遲 |
|---------|---------|------|
| commands/*.md | 自動生效 | 1-3 秒 |
| skills/*/SKILL.md | 自動生效 | 1-3 秒 |
| themes/*.yaml | 用 `/theme <name>` 切換 | 即時 |
| ui/ 卡片內容 | 自動重新載入 | 1-3 秒 |
| hooks/*.py | 需重啟 DriFox | — |
| .mcp.json | 需重啟 DriFox | — |
| .lsp.json | 需重啟 DriFox | — |
| plugin.json | 需重啟 DriFox | — |

### 測試流程

```
① 修改插件文件
② 等待 1-3 秒 watchfiles 檢測
③ 用對應功能測試：
   - Commands → 輸入 /<command-name>
   - Agents → 輸入 @<agent-name>
   - Skills → 觸發場景讓 AI 匹配
   - Themes → 輸入 /theme <name>
   - UI → 輸入 /<card-id> 或查看消息流
④ 用 /plugin-marketplace 查看插件狀態（已安裝/啟用/禁用）
```

---

## 2. 完整驗證（發布前必做）

clone 官方市場倉庫，把你的插件放進去跑驗證：

```bash
git clone https://github.com/martin98-afk/drifox-plugins.git /tmp/dfp
cp -r ~/.drifox/plugins/<name> /tmp/dfp/plugins/<name>
cd /tmp/dfp

# 校驗所有插件（manifest + 組件完整性 + marketplace 一致性）
python tools/validate_plugins.py

# 重新生成 marketplace.json（新增/修改插件後必做）
python tools/generate_marketplace.py
```

### validate_plugins.py 檢查項

- [x] plugin.json 存在且 JSON 合法
- [x] JSON Schema 校驗通過
- [x] name 與目錄名一致
- [x] version 符合 SemVer
- [x] description 不超過 200 字
- [x] 每個啟用的 component 有對應文件
- [x] commands/*.md 有完整 frontmatter
- [x] skills/*/SKILL.md 有 frontmatter
- [x] hooks Python 文件可編譯
- [x] marketplace.json 一致性

---

## 3. Python 語法檢查

```bash
# 檢查本地開發中的插件（在 ~/.drifox/plugins/ 下）
python -m py_compile ~/.drifox/plugins/<name>/hooks/<name>_hook.py
python -m py_compile ~/.drifox/plugins/<name>/ui/__init__.py

# 或是在 drifox-plugins 倉庫 clone 中檢查
python -m py_compile plugins/<name>/hooks/<name>_hook.py
```

---

## 4. 除錯技巧

### 插件未加載

1. 檢查 `plugin.json` 位置：`<plugin-name>/.drifox-plugin/plugin.json`
2. 檢查 `name` 字段是否與目錄名一致
3. 檢查 JSON 語法是否合法
4. 重啟 DriFox 後用 `/plugin-marketplace` 查看

### 命令不生效

1. 檢查 `commands/*.md` 的 frontmatter
2. 檢查 `description` 字段是否存在
3. 檢查文件名是否 `^[a-z][a-z0-9-]*\.md$`
4. 檢查 `plugin.json` 中 `components.commands: true`

### Hook 不觸發

1. 檢查 `hooks/hooks.json` 語法
2. 檢查事件名是否拼寫正確（區分大小寫）
3. 檢查函數引用路徑是否正確
4. 用 `python -m py_compile` 檢查語法
5. 重啟 DriFox

### UI 卡片不顯示

1. 檢查 `ui/__init__.py` 是否存在
2. 檢查是否定義了 `register_ui(registry)` 函數
3. 檢查 `plugin.json` 中 `components.ui: true`
4. → 調用 `ui-plugin-creator` 技能

### 驗證報錯

- 仔細閱讀 `validate_plugins.py` 的錯誤信息
- 對照 `schemas/plugin.schema.json` 檢查 manifest
- 確認所有引用的目錄和文件都存在
