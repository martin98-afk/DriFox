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
   （不带 deps/：第三方依赖只写在 plugin.json 的 dependencies.pip）
④ 跑验证 → python tools/validate_plugins.py
   （generate_marketplace.py 只用于本地预览条目长什么样，产物不提交）
⑤ Commit & Push 到你的 fork
⑥ 在 GitHub 提交 PR（你的 fork → martin98-afk/drifox-plugins main）
⑦ CI 自动校验；合并后 CI 自动按 dependencies.pip 打包 tarball → 你的插件上架 🎉
```

---

## 完整提交流程（九步）

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

# 5. 改 plugin.json 的 version（新增功能升 minor，Bug 修复升 patch）
#    ⚠ 这一步不能漏：marketplace.json 的 version 从 plugin.json 生成，且
#      validate_plugins.py 会校验两者一致；版本号不变会让用户端拿不到更新。
#      已启用 tarball 通道的插件还靠版本变化触发 CI 重新打包发布。

# 6. 把你的插件从本地开发目录复制进来
#    ⚠ 第三方依赖不进仓库：依赖写进 plugin.json 的 dependencies.pip（平台键
#      win32/linux/darwin，见 manifest.md），仓库里不放 deps/ 目录；
#    deps/ 由市场 CI 打包时按声明从 PyPI 自动构建，打进按平台分发的 tarball
cp -r ~/.drifox/plugins/<plugin-name> plugins/<plugin-name>
#    本地开发曾把依赖装进 deps/ 的，复制前先清掉：
rm -rf plugins/<plugin-name>/deps

# 7. 跑验证
python tools/validate_plugins.py
#    只想要本地预览条目格式时才跑下面这条（它写 marketplace.json）：
#    python tools/generate_marketplace.py
#    ⚠ 不要把 marketplace.json 的改动一起提交：CONTRIBUTING 明确要求它由 CI
#      的 auto-fix-marketplace job 生成，手改会在 push 时和 bot 提交打架

# 8. commit 并推送（只加插件目录）
git add plugins/<plugin-name>/
git commit -m "feat(<plugin-name>): 添加 xx 插件"
git push origin feat/<plugin-name>

# 9. 到 GitHub 上创建 Pull Request
#    你的 fork → martin98-afk/drifox-plugins main
#    链接：https://github.com/martin98-afk/drifox-plugins/pulls
```

> ⚠️ 第 7 步验证未通过 → 禁止进入第 8 步推送（SKILL.md 硬停止第 6 条）。
> 唯一例外：报错只剩「marketplace.json 中 <name> 的 version/条目与 plugin.json 不一致」，那是待 CI 回填项，可继续推送并在 PR 里注明。

---

## 在多任务共用的 clone 里发版（别带入别人的改动）

常见场景：`drifox-plugins` clone 里已经有别人（或你上一轮）的未完成插件未被跟踪，`git status` 一屏红。直接 `git add -A` 会把它们一起推进 PR。

做法：不碰现有工作区，用 worktree 从远端基线拉一份干净、只含目标插件的分支。

```bash
cd <drifox-plugins clone>
git fetch origin main
git worktree add /tmp/dp-<plugin> -b feat/<plugin> origin/main
cd /tmp/dp-<plugin>
cp -R ~/.drifox/plugins/<plugin> plugins/<plugin>   # 只加这一个目录
python tools/validate_plugins.py plugins/<plugin> --strict
```

为什么用 `origin/main` 而不是本地 `main`：本地 main 可能攒着没推的提交，基于它开分支会把那些提交一起拉进 PR。

**本地验证的噪声**：工作区里存在未跟踪插件时，不带参数的 `validate_plugins.py` 会报「插件 X 未收录到 marketplace.json」。那不是你的问题，也不是 PR 会失败的理由（那些目录不会进提交）。带上插件路径跑（`validate_plugins.py plugins/<name>`）只看自己的。

---

## PR 首轮 CI 必红一次（预期，不要手改清单救它）

新插件 PR 的第一次 validate 一定失败，报「插件 <name> 未收录到 marketplace.json」。紧接着 `auto-fix-marketplace` job 会向 PR 分支补一个 `[skip ci]` 提交，把清单补齐，PR 转为 clean。

因此：

- 首轮红 ≠ 你写错了，先看是不是只有这同一条
- 不要为了把它变绿去手改 `marketplace.json` 提交（会与 bot 提交冲突，且下一次 bot 提交照样覆盖你）
- 不要因为首轮红就关 PR
- bot 提交带 `[skip ci]`，不会再触发第二轮校验；看到 PR `mergeable` 变 clean 即可人工合入

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

PR 阶段（GitHub Actions 自动执行）：

1. **validate** — 检查所有插件 manifest + 组件完整性；跑 validate + generate，失败排查见 references/troubleshooting.md「PR 的 CI 失败」
2. **auto-fix-marketplace** — 如果 `marketplace.json` 过期，bot 自动修复并 commit 到 PR 分支
3. ✅ 全部通过 → 等待 maintainer 合并

合并进 main 后（**release-plugins** workflow）：

4. **detect_plugin_changes.py** — 找出「已启用 tarball 通道且版本变化」的插件（比 URL 里的版本与 manifest version）
5. **build_plugin_tarballs.py --deps-source pip --strict-deps** — 按平台构建单插件 tarball：依赖按 `dependencies.pip` 从 PyPI 现场装进包（跨平台包合并进 default），缺依赖即失败，宁可不发也不发残缺包
6. **publish_plugin_tarballs.py** — 发布到 GitHub Releases（tag 约定 `plugin-<name>-v<version>`，asset 直链 + sha256）
7. **generate_marketplace.py** — 回填 `marketplace.json` 的 `tarball` 字段并提交（带 `[skip ci]`）

### Bot 自动修复

当你修改 `plugin.json` 后忘了跑 `generate_marketplace.py` 时：
- CI 的 `auto-fix-marketplace` job 会自动生成并 commit 修复
- commit 含 `[skip ci]` 防止无限循环
- PR 场景 → commit 到 PR head 分支
- push main 场景 → commit 到 main

---

## PR 合并后

- marketplace.json 自动更新，你的插件出现在官方市场中
- 所有 DriFox 用户可通过 plugin-marketplace UI 浏览和安装你的插件 🎉
- **安装通道双轨**：已启用 tarball 通道的插件优先走 tarball（单包直链 + sha256 校验，包内含当前平台依赖，下载即用）；失败回落 git-subdir 源码通道（装后需自行补依赖）。老客户端无 tarball 字段时照旧走 git-subdir，双轨兼容
- **首次启用 tarball**：合并后手动跑一次 `tools/build_plugin_tarballs.py` + `tools/publish_plugin_tarballs.py` + `tools/generate_marketplace.py`；之后升版本 push 到 main，CI 自动重打发布

---

## 版本策略

| 变更类型 | 版本升级 | 示例 |
|---------|---------|------|
| 首次发布 | `0.1.0` → `1.0.0` | 稳定版 |
| Bug 修复 | 升 patch | `1.0.0` → `1.0.1` |
| 新增功能 | 升 minor | `1.0.0` → `1.1.0` |
| 破坏性变更 | 升 major | `1.0.0` → `2.0.0` |

破坏性变更必须在 PR 描述中写明迁移指南。

已启用 tarball 通道的插件：版本号变了 push 到 main 即自动重打 tarball 并发布，无需手动跑打包脚本。

**升版本后的清单门禁（实测坑）**：`generate_marketplace.py` 会校验 `marketplace.json` 里 tarball URL 内嵌版本与新 `version` 是否一致，不一致时**拒绝写出清单**并提示先构建发布对应版本。此时本地改不动清单，`validate_plugins.py` 也会因 version 与清单不一致而失败。正确顺序：

```
push 含新 version 的 plugin.json 到 main
  → release-plugins.yml 的 detect_plugin_changes.py 比对 tarball URL 版本，发现有变更
  → CI 构建 tarball、发布 GitHub Release、generate_marketplace.py 回填清单
  → CI 以 [skip ci] 提交 marketplace.json
```

即：tarball 通道的插件**不要在本地手工改 marketplace.json 的 version**，也不要试图本地 `publish_plugin_tarballs.py`（需要 `GITHUB_TOKEN` 且写入远端 Release，属外部写操作）。本地只需保证 `plugin.json` 版本升了、代码 commit 完整。若 push 前想自查，跑 `validate_plugins.py` 观察是否只剩「清单版本不一致」这一条，并把它记为已知待 CI 回填项。

---

## 插件维护

- 不再维护的插件：`components` 全部设为 `false`，**不要删除插件目录**
- 新增事件或字段：同步更新 `schemas/plugin.schema.json`、`tools/generate_marketplace.py`、`docs/`
- 依赖变更：只改 `dependencies.pip`，下次 CI 打包自动生效；不要往仓库提交 deps/ 目录
- 修改别人的插件：先开 Issue 讨论

marketplace.json 中每条记录的结构由 `tools/generate_marketplace.py` 自动从 `plugin.json` 生成，无需手动编辑。

---

## 参考

- [CONTRIBUTING.md](https://github.com/martin98-afk/drifox-plugins/blob/main/CONTRIBUTING.md) — 完整贡献指南
- [GitHub 仓库](https://github.com/martin98-afk/drifox-plugins) — 官方插件市场
