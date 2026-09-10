---
description: 发布插件到 drifox-plugins 官方市场的完整流程
---

# 发布到官方市场

> 任何人都可以发布自己的插件！先 Fork → 再 PR，开发完成后提交到 [github.com/martin98-afk/drifox-plugins](https://github.com/martin98-afk/drifox-plugins) 让所有 DriFox 用户可用。

---

## 工作流总览

```
① Fork 官方仓库 → https://github.com/martin98-afk/drifox-plugins（点右上角 Fork）
② Clone 你的 fork → git clone https://github.com/<你的帐号>/drifox-plugins.git
③ 把你的插件复制到仓库中 → cp -r ~/.drifox/plugins/<name> plugins/<name>
④ 跑验证 → python tools/validate_plugins.py + generate_marketplace.py
⑤ Commit & Push 到你的 fork
⑥ 在 GitHub 提交 PR（你的 fork → martin98-afk/drifox-plugins main）
⑦ CI 自动校验，通过后 maintainer 合并 → 你的插件上架 🎉
```

---

## 完整提交流程（八步）

```bash
# 1. 先在 GitHub 上 Fork 官方市场仓库
#    网址：https://github.com/martin98-afk/drifox-plugins → 点右上角 Fork

# 2. clone 你的 fork
git clone https://github.com/<你的GitHub帐号>/drifox-plugins.git /tmp/dfp
cd /tmp/dfp

# 3. 把官方仓库设为 upstream（便于同步）
git remote add upstream https://github.com/martin98-afk/drifox-plugins.git

# 4. 建立特性分支
git checkout -b feat/<plugin-name>

# 5. 把你的插件从本地开发目录复制进来
cp -r ~/.drifox/plugins/<plugin-name> plugins/<plugin-name>

# 6. 跑验证
python tools/validate_plugins.py
python tools/generate_marketplace.py
#    marketplace.json 由 generate 脚本自动生成，无需手动编辑

# 7. commit 并推送
git add plugins/<plugin-name>/ marketplace.json
git commit -m "feat(<plugin-name>): 添加 xx 插件"
git push origin feat/<plugin-name>

# 8. 到 GitHub 上创建 Pull Request
#    你的 fork → martin98-afk/drifox-plugins main
#    链接：https://github.com/martin98-afk/drifox-plugins/pulls
```

> ⚠️ 第 6 步未通过 → 禁止进入第 7 步（SKILL.md 硬停止第 6 条）。

---

## Commit 规范

使用 Conventional Commits 格式：

```
feat(<plugin-name>): 添加新插件
fix(<plugin-name>): 修复 xx 问题
docs(<plugin-name>): 补充说明
refactor(<plugin-name>): 重构 xx 模块
```

---

## CI 说明

PR 提交后 GitHub Actions 自动执行：

1. **validate** — 检查所有插件 manifest + 组件完整性；跑 validate + generate，失败排查见 references/troubleshooting.md「PR 的 CI 失败」
2. **auto-fix-marketplace** — 如果 `marketplace.json` 过期，bot 自动修复并 commit 到 PR 分支
3. ✅ 全部通过 → 等待 maintainer 合并

### Bot 自动修复

当你修改 `plugin.json` 后忘了跑 `generate_marketplace.py` 时：
- CI 的 `auto-fix-marketplace` job 会自动生成并 commit 修复
- commit 含 `[skip ci]` 防止无限循环
- PR 场景 → commit 到 PR head 分支
- push main 场景 → commit 到 main

---

## PR 合并后

- marketplace.json 自动更新
- 你的插件名称出现在官方市场中
- 所有 DriFox 用户可通过 plugin-marketplace UI 浏览和安装你的插件 🎉

---

## 版本策略

| 变更类型 | 版本升级 | 示例 |
|---------|---------|------|
| 首次发布 | `0.1.0` → `1.0.0` | 稳定版 |
| Bug 修复 | 升 patch | `1.0.0` → `1.0.1` |
| 新增功能 | 升 minor | `1.0.0` → `1.1.0` |
| 破坏性变更 | 升 major | `1.0.0` → `2.0.0` |

破坏性变更必须在 PR 描述中写明迁移指南。

---

## 插件维护

- 不再维护的插件：`components` 全部设为 `false`，**不要删除插件目录**
- 新增事件或字段：同步更新 `schemas/plugin.schema.json`、`tools/generate_marketplace.py`、`docs/`
- 修改别人的插件：先开 Issue 讨论

marketplace.json 中每条记录的结构由 `tools/generate_marketplace.py` 自动从 `plugin.json` 生成，无需手动编辑。

---

## 参考

- [CONTRIBUTING.md](https://github.com/martin98-afk/drifox-plugins/blob/main/CONTRIBUTING.md) — 完整贡献指南
- [GitHub 仓库](https://github.com/martin98-afk/drifox-plugins) — 官方插件市场
