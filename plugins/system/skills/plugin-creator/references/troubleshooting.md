---
description: 插件開發常見問題與解決方案
---

# 常見問題

---

## Manifest 相關

### ❌ validate_plugins.py 報錯「name does not match directory」

**原因**：`plugin.json` 的 `name` 字段與插件目錄名不一致。

**解決**：
```json
// 目錄：plugins/my-cool-plugin/
// ❌ "name": "myCoolPlugin"
// ✅ "name": "my-cool-plugin"
```

### ❌ JSON Schema 校驗失敗

**原因**：`plugin.json` 不符合 `schemas/plugin.schema.json`。

**解決**：
- 確認所有必填字段存在（name, description, version, components）
- 確認 `name` 符合 `^[a-z][a-z0-9-]{1,63}$`
- 確認 `version` 符合 SemVer
- 確認 `components` 至少啟用一個

### ❌ 「Components flag enabled but directory not found」

**原因**：`components.commands: true` 但沒有 `commands/` 目錄。

**解決**：刪除該目錄或設為 `false`。

---

## 運行時問題

### ❌ 插件不顯示在 /plugin-manager

**可能原因**：
1. `plugin.json` 位置錯誤 → 應為 `<name>/.drifox-plugin/plugin.json`
2. `name` 與目錄名不一致
3. JSON 語法錯誤
4. 插件放在不被掃描的目錄

**解決**：
- 檢查目錄結構
- 用 `python -c "import json; json.load(open('.drifox-plugin/plugin.json'))"` 測試 JSON
- 重啟 DriFox

### ❌ 命令不顯示 / 不觸發

**可能原因**：
1. `commands/*.md` frontmatter 缺少 `description` 或 `type`
2. 文件名含大寫或特殊字符
3. `components.commands` 未設為 `true`

**解決**：對照 `references/components.md §Commands` 檢查。

### ❌ Hook 不觸發

**可能原因**：
1. `hooks.json` 格式錯誤
2. 事件名拼寫錯誤（大小寫敏感）
3. Python 文件中函數名與 `hooks.json` 不匹配
4. Python 文件有語法錯誤

**解決**：
```bash
python -m py_compile hooks/<name>_hook.py
```

### ❌ UI 卡片空白 / 不顯示

→ **調用 `ui-plugin-creator` 技能**，提供詳細症狀。

---

## 發布問題

### ❌ PR 的 CI 失敗

**原因**：`validate_plugins.py` 未通過或 `marketplace.json` 不一致。

**解決**：（在 drifox-plugins 倉庫 clone 中執行）
```bash
python tools/validate_plugins.py
python tools/generate_marketplace.py
git add marketplace.json
git commit -m "chore: update marketplace.json"
```

### ❌ PR 被要求修改

**常見原因**：
- `description` 過長（>200 字）
- 缺少 `README.md`
- `version` 不合理
- 缺少 `license` 字段

---

## 其他

### ❌ 不知道從何開始

→ 複製 [plugins/example-plugin/](https://github.com/martin98-afk/drifox-plugins/tree/main/plugins/example-plugin) 作為起點。

### ❌ 不知道該用哪種組件

→ 看本技能 SKILL.md 的 §3 決策樹。

### ❌ 需要 UI 插件

→ 調用 `ui-plugin-creator` 技能，本技能不處理 UI 開發細節。
