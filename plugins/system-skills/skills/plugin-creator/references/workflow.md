---
description: 插件开发完整流程：从需求澄清到 Scaffold、迭代循环、版本管理、发布
---

# 开发工作流

> 完整的插件开发生命周期指引。承接自 SKILL.md §4（已瘦身至路由面）。

---

## 阶段一：需求澄清

如果需求不明确，先用 `brainstorming` 技能：

```
用户说"做个 xx 插件"
  ↓
brainstorming 技能
  ↓
产出：需求文档、功能清单、边界定义
  ↓
确定需要哪些组件（commands / agents / skills / hooks / mcp / lsp / themes / ui
                 / tools / providers / team_templates）
```

如果是 UI 插件 → 调用 `ui-plugin-creator` 技能（含前端设计）。

---

## 阶段二：Scaffold（四步）

所有插件建立在 `~/.drifox/plugins/` 下，DriFox 的 watchfiles 会自动热加载。

```
① 获取 example-plugin 作为起点：

   git clone --depth=1 --filter=blob:none --no-checkout \
     https://github.com/martin98-afk/drifox-plugins.git /tmp/dfp
   cd /tmp/dfp
   git sparse-checkout set plugins/example-plugin
   git checkout main
   cp -r plugins/example-plugin ~/.drifox/plugins/<your-plugin>
   rm -rf /tmp/dfp

② 修改 manifest：
   编辑 ~/.drifox/plugins/<your-plugin>/.drifox-plugin/plugin.json →
   - name:        "<your-plugin>"（小写 kebab-case，与目录名一致）
   - description: "一句话描述"
   - version:     "0.1.0"
   - author:      你的名字
   - components:  只保留你需要的 flag

③ 清理不需要的组件目录与文件：
   用不到的组件直接删除对应目录，并在 plugin.json 中设为 false

④ 按组件类型逐一实现（顺序建议见阶段三）
```

> 💡 也可以不复制，直接在 `~/.drifox/plugins/<your-plugin>/` 下手动建目录 + 写 plugin.json
> （完整字段见 references/manifest.md）。最小真实骨架可直接参考本技能 `examples/` 目录。

---

## 阶段三：开发

按组件类型分派，开发顺序建议：

```
① Manifest（plugin.json）→ 定义插件身份
② Commands（如果有）→ 用户入口优先
③ Skills（如果有）→ AI 行为定义
④ Agents（如果有）→ 角色定义
⑤ Hooks（如果有）→ 后台行为
⑥ MCP / LSP（如果有）→ 运行时扩展
⑦ Themes（如果有）→ 视觉呈现
⑧ UI（如果有）→ 调用 ui-plugin-creator
⑨ Tools / Providers / Team Templates（如果有）→ 见 components.md 对应章节
```

每个组件开发完后即时测试。

---

## 阶段四：迭代循环

```
修改文件 → DriFox watchfiles 热更新（1-3 秒） → 测试效果 → 再改
```

- **commands** 和 **skills** 修改后立即生效
- **hooks** / **mcp** / **lsp** 修改后可能需要重启 DriFox
- **themes** 修改后用 `/theme <name>` 切换查看
- **ui** 修改后卡片自动重新加载
- 用 `/plugin-marketplace` 查看已安装插件状态（启/禁/卸）

热更新延迟明细见 references/testing.md。

---

## 阶段五：测试与发布

1. 本地热更新测试 + `validate_plugins.py` → 见 references/testing.md
2. Fork → PR 上架官方市场 → 见 references/publishing.md

---

## 版本管理（SemVer）

```jsonc
// 开发阶段：0.1.x
"version": "0.1.0"

// 首次发布：1.0.0
// 遵循 SemVer：major.minor.patch
// 破坏性变更 → 升 major
// 新增功能   → 升 minor
// Bug 修复   → 升 patch
```

每次修改后记得更新 `plugin.json` 的 `version` 字段（CI 会校验一致性）。

---

## 决策矩阵

| 插件类型 | 建议组件 | 参考插件 |
|---------|---------|---------|
| 代码工具插件 | commands + skills | `code-reviewer`、`git-workflow` |
| 语言增强插件 | skills + hooks | `python-pro`、`frontend-pro` |
| UI 仪表板插件 | ui | `context-usage-stats`、`git-dashboard` |
| 自动化工作流 | hooks + commands | `evolver`、`hookify` |
| 工具/服务商插件 | tools / providers | `win-powershell`、system-providers |
| 团队协作插件 | team_templates + agents | system-team-templates |
| 完整插件 | 全部 | `example-plugin` |
