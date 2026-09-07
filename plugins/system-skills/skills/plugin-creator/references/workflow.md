---
description: 插件開發完整流程：從需求到發布
---

# 開發工作流

> 完整的插件開發生命週期指引。

---

## 階段一：需求釐清

如果需求不明確，先用 `brainstorming` 技能：

```
用戶說"做個 xx 插件"
  ↓
brainstorming 技能
  ↓
產出：需求文檔、功能清單、邊界定義
  ↓
確定需要哪些組件（commands / agents / skills / hooks / mcp / lsp / themes / ui）
```

如果是 UI 插件 → 調用 `ui-plugin-creator` 技能（含前端設計）。

---

## 階段二：Scaffold

```bash
# 從 GitHub 下載 example-plugin 作為起點
# 請參考 SKILL.md §4.1 的詳細 git sparse-checkout 步驟
# 目標目錄：~/.drifox/plugins/<your-plugin>/

# 修改 manifest
# 編輯 ~/.drifox/plugins/<your-plugin>/.drifox-plugin/plugin.json
```

manifest 必填：
- `name`（與目錄名一致）
- `description`
- `version: "0.1.0"`
- `components`（只保留你需要的）

---

## 階段三：開發

按 §3 決策樹分派組件類型，開發順序建議：

```
① Manifest（plugin.json）→ 定義插件身份
② Commands（如果有）→ 用戶入口優先
③ Skills（如果有）→ AI 行為定義
④ Agents（如果有）→ 角色定義
⑤ Hooks（如果有）→ 後臺行為
⑥ MCP / LSP（如果有）→ 運行時擴展
⑦ Themes（如果有）→ 視覺呈現
⑧ UI（如果有）→ 調用 ui-plugin-creator
```

每個組件開發完後即時測試。

---

## 階段四：測試

1. 本地熱更新測試（watchfiles）
2. 跑 `validate_plugins.py` 檢查完整性

---

## 階段五：發布

任何人都可以發布自己的插件到官方市場！需要先 Fork 再 PR：

```bash
# 0. 先去 GitHub Fork 官方倉庫
#    https://github.com/martin98-afk/drifox-plugins → 點右上角 Fork

# 1. clone 你的 fork
git clone https://github.com/<你的GitHub帳號>/drifox-plugins.git /tmp/dfp
cd /tmp/dfp

# 2. 把官方倉庫設為 upstream
git remote add upstream https://github.com/martin98-afk/drifox-plugins.git

# 3. 建立特性分支
git checkout -b feat/<plugin-name>

# 4. 從本地開發目錄複製插件
cp -r ~/.drifox/plugins/<plugin-name> plugins/<plugin-name>

# 5. 跑驗證
python tools/validate_plugins.py
python tools/generate_marketplace.py

# 6. commit 並推送
git add plugins/<plugin-name>/ marketplace.json
git commit -m "feat(<plugin-name>): 添加 xx 插件"
git push origin feat/<plugin-name>

# 7. 到 GitHub 創建 PR
#    你的 fork → martin98-afk/drifox-plugins main
#    https://github.com/martin98-afk/drifox-plugins/pulls
```

---

## 決策矩陣

| 插件類型 | 建議組件 | 參考插件 |
|---------|---------|---------|
| 代碼工具插件 | commands + skills | `code-reviewer`、`git-workflow` |
| 語言增強插件 | skills + hooks | `python-pro`、`frontend-pro` |
| UI 儀表板插件 | ui | `context-usage-stats`、`git-dashboard` |
| 自動化工作流 | hooks + commands | `evolver`、`hookify` |
| 主題插件 | themes | 系統 themes/ |
| 完整插件 | 全部 | `example-plugin` |
