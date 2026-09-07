---
description: 發布插件到 drifox-plugins 官方市場的完整流程
---

# 發布到官方市場

> 任何人都可以發布自己的插件！先 Fork → 再 PR，開發完成後提交到 [github.com/martin98-afk/drifox-plugins](https://github.com/martin98-afk/drifox-plugins) 讓所有 DriFox 用戶可用。

---

## 完整工作流

```
① Fork 官方倉庫 → https://github.com/martin98-afk/drifox-plugins（點右上角 Fork）
② Clone 你的 fork → git clone https://github.com/<你的帳號>/drifox-plugins.git
③ 把你的插件複製到倉庫中 → cp -r ~/.drifox/plugins/<name> plugins/<name>
④ 跑驗證 → python tools/validate_plugins.py + generate_marketplace.py
⑤ Commit & Push 到你的 fork
⑥ 在 GitHub 提交 PR（你的 fork → martin98-afk/drifox-plugins main）
⑦ CI 自動校驗，通過後 maintainer 合併 → 你的插件上架 🎉
```

---

## 步驟詳解

### ① 本地開發完成

確認你的插件：
- 在 DriFox 中能正常加載
- 所有組件功能正確
- 版本號已更新

### ② Fork + Clone

```bash
# 先到 https://github.com/martin98-afk/drifox-plugins 點右上角 Fork
# 然後 clone 你的 fork
git clone https://github.com/<你的GitHub帳號>/drifox-plugins.git /tmp/dfp
cd /tmp/dfp

# 把官方倉庫設為 upstream（便於同步）
git remote add upstream https://github.com/martin98-afk/drifox-plugins.git

# 從本地開發目錄複製你的插件
cp -r ~/.drifox/plugins/<your-plugin> plugins/<your-plugin>
```

### ③ 驗證

```bash
python tools/validate_plugins.py
python tools/generate_marketplace.py
```

兩條命令都應輸出 `OK`。

### ④ 創建分支

```bash
git checkout -b feat/<plugin-name>
```

### ⑤ Commit

```bash
git add plugins/<plugin-name>/ marketplace.json
git commit -m "feat(<plugin-name>): 添加 xx 插件"
```

使用 Conventional Commits 格式：
```
feat(<plugin-name>): 添加新插件
fix(<plugin-name>): 修復 xx 問題
docs(<plugin-name>): 補充說明
refactor(<plugin-name>): 重構 xx 模塊
```

### ⑥ Push + PR

```bash
# 推送到你的 fork
git push origin feat/<plugin-name>
```

然後到 [github.com/martin98-afk/drifox-plugins](https://github.com/martin98-afk/drifox-plugins) 上創建 Pull Request：
- **base repository**: `martin98-afk/drifox-plugins` → `main`
- **head repository**: `<你的帳號>/drifox-plugins` → `feat/<plugin-name>`

---

## CI 說明

PR 提交後 GitHub Actions 自動執行：

1. **validate** — 檢查所有插件 manifest + 組件完整性
2. **auto-fix-marketplace** — 如果 `marketplace.json` 過期，bot 自動修復並 commit 到 PR 分支
3. ✅ 全部通過 → 等待 maintainer 合併

### Bot 自動修復

當你修改 `plugin.json` 後忘了跑 `generate_marketplace.py` 時：
- CI 的 `auto-fix-marketplace` job 會自動生成並 commit 修復
- commit 含 `[skip ci]` 防止無限循環
- PR 場景 → commit 到 PR head 分支
- push main 場景 → commit 到 main

---

## 版本策略

| 變更類型 | 版本升級 | 示例 |
|---------|---------|------|
| 首次發布 | `0.1.0` → `1.0.0` | 穩定版 |
| Bug 修復 | 升 patch | `1.0.0` → `1.0.1` |
| 新增功能 | 升 minor | `1.0.0` → `1.1.0` |
| 破壞性變更 | 升 major | `1.0.0` → `2.0.0` |

破壞性變更必須在 PR 描述中寫明遷移指南。

---

## 插件維護

- 不再維護的插件：`components` 全部設為 `false`，**不要刪除插件目錄**
- 新增事件或字段：同步更新 `schemas/plugin.schema.json`、`tools/generate_marketplace.py`、`docs/`
- 修改別人的插件：先開 Issue 討論

---

## 參考

- [CONTRIBUTING.md](https://github.com/martin98-afk/drifox-plugins/blob/main/CONTRIBUTING.md) — 完整貢獻指南
- [GitHub 倉庫](https://github.com/martin98-afk/drifox-plugins) — 官方插件市場
