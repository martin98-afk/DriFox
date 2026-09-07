# 开发工作流（新建插件）

> 本文是 `SKILL.md §3.1` 的完整展开。
> 决策树见 `SKILL.md §1`，核心模式见 `references/patterns.md`，模板见 `references/templates.md`。

---

## 📑 目录

> 行号对应本文件当前版本，编辑后请更新。

- **⚠️ 工作流总览（必读）** — L37–L70
- **Phase 0：前置技能调用（必走）** — L71–L171
  - 0.1 调用 brainstorming 技能 — L75–L108
  - 0.2 调用 frontend-design 技能 — L109–L158
  - 0.3 不需要前置技能的情况 — L159–L171
- **Phase 1：澄清技术细节** — L172–L212
  - 1.1 容器选择详解 — L186–L198
  - 1.2 异步 vs 同步 — L199–L212
- **Phase 2：创建插件目录结构** — L213–L267
  - 2.1 标准结构（系统插件 + 用户插件通用） — L215–L229
  - 2.2 最小结构（最简卡片） — L230–L241
  - 2.3 plugin.json 模板 — L242–L267
- **Phase 3：按模板生成代码** — L268–L300
  - 3.1 推荐顺序 — L270–L277
  - 3.2 设计约束（插件闭包） — L278–L284
  - 3.3 不需要从零写的部分 — L285–L300
- **Phase 4：验证** — L301–L343
  - 4.1 代码质量 — L303–L310
  - 4.2 实际加载测试 — L311–L322
  - 4.3 调试技巧 — L323–L337
  - 4.4 完整验证清单 — L338–L343
- **Phase 5：提交与发布** — L344–L382
  - 5.1 提交规范 — L346–L354
  - 5.2 用户插件发布 — L355–L373
  - 5.3 提交时附带的文档 — L374–L382
## ⚠️ 工作流总览（必读）

```
┌─────────────────────────────────────────────────────┐
│ Phase 0：前置技能调用（必走）                       │
│   ① brainstorming → 需求文档                        │
│   ② frontend-design → UI 设计稿                     │
└─────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────┐
│ Phase 1：澄清技术细节（本技能）                      │
│   容器类型 / 数据源 / 异步 / 上下文注入             │
└─────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────┐
│ Phase 2：创建目录结构 + plugin.json                 │
└─────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────┐
│ Phase 3：按模板生成代码                              │
│   骨架 → 控件 → 数据 → 入口 → 主题                 │
└─────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────┐
│ Phase 4：验证（ruff + 加载测试）                     │
└─────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────┐
│ Phase 5：提交与发布                                  │
└─────────────────────────────────────────────────────┘
```

---

## Phase 0：前置技能调用（必走）

> 🛑 **不要跳过这一步直接进入编码**。

### 0.1 调用 brainstorming 技能

**目的**：搞清楚"做什么 / 不做什么"，避免实现完才发现理解错需求。

**产出**（任选其一）：

| 产出物 | 包含内容 |
|--------|---------|
| 功能清单 | 核心功能 + 辅助功能 + 不做什么（边界） |
| 用户故事 | "作为 X，我想要 Y，以便 Z" |
| 验收标准 | 每条功能的具体"完成"判定 |

**典型场景**：

```bash
# 用户的原始请求
"我想做个统计卡片看每天用了多少 token"

# brainstorming 后会变成
"用户故事：作为开发者，我想在浮动卡片里看到最近 14 天每天消耗的 token 数，
          以便判断是否存在使用高峰。
 核心功能：
   1. 显示每日 token 折线图
   2. 显示总 token 数（累计）
   3. 显示日均 token 数
 不做什么：
   - 不显示按模型分类的 token（V1 范围外）
   - 不支持导出数据
 验收标准：
   - 打开卡片 < 1s 显示数据
   - 数字格式化（1.2k / 1.2M）
   - 主题色跟随主程序"
```

### 0.2 调用 frontend-design 技能

**目的**：把 brainstorming 的需求转成视觉稿，明确"长什么样 / 怎么交互"。

**产出**：

| 产出物 | 包含内容 |
|--------|---------|
| 视觉稿 | 卡片截图 / 线框图（粗略即可） |
| 组件清单 | 用到哪些控件（统计卡、柱状图等） |
| 交互流程 | 点击 / 滚动 / 刷新时的行为 |
| 配色方案 | 主色 / 强调色 / 文字色 |

**典型场景**：

```bash
# frontend-design 后的产出
"视觉稿：
  ┌────────────────────────────┐
  │ 📊 上下文用量统计    [×]  │  ← 头部
  ├────────────────────────────┤
  │ [总token] [总会话] [总消息]│  ← 第一行 _StatCard × 3
  │ 5.6M       283      953    │
  │ 日均398k   平均20/天 ...   │
  ├────────────────────────────┤
  │ 🔤 Token 用量趋势          │  ← _LineChartWidget
  │  ┌─────────────────────┐  │
  │  │   /\\    /\\         │  │
  │  │  /  \\  /  \\   ...  │  │
  │  └─────────────────────┘  │
  └────────────────────────────┘

 组件清单：
   - 头部：图标 + 标题 + 刷新按钮 + 关闭按钮
   - _StatCard × 3（总token/总会话/总消息）
   - _LineChartWidget × 1（token 趋势）
   - _BarChartWidget × 1（每日会话数）

 配色方案：
   - 主色：跟随主程序 ctx.colors.accent
   - 折线：success 色
   - 文字：白色（浮动卡片背景偏暗）

 交互流程：
   - 默认折叠
   - /context-usage-stats 命令打开
   - 刷新按钮：重新拉数据
   - 关闭：发射 closed 信号"
```

### 0.3 不需要前置技能的情况

> **修改现有插件**不需要 brainstorming / frontend-design。
> 直接读 `references/modifying.md`。
>
> 适用场景：
> - 加按钮 / 加图标
> - 调样式 / 调参数
> - 修 bug
> - 重构内部代码（不改对外行为）

---

## Phase 1：澄清技术细节

> 这一步是本技能负责的，brainstorming **不**做技术决策。

| 问题 | 选项 | 影响 |
|------|------|------|
| 放在哪个**方位**？ | `container="bottom"` / `container="top"` / `container="left"` / `container="right"` / `container="full"` | bottom 隐藏输入区；left/right 停靠在 Tab 窗口左右侧（类似 IDE 侧边栏）；full 完整覆盖对话区 |
| 数据来源？ | 本地（SQLite / 文件）/ 远程（HTTP API） | 决定异步 worker 是否需要 |
| 需要**异步操作**吗？ | 是 → QThread + pyqtSignal | 列表加载、安装/卸载、网络请求都算 |
| 需要**上下文注入**吗？ | 是 → `set_context_provider` + 拉模型 | 主题色/字体跟随系统变化 |
| 需要复用现成 widgets 吗？ | 见 `widgets.md` 索引 | 节省开发时间 |
| 需要外部依赖吗？ | 见 `templates.md §五`（`_vendor/` 模式） | 引入第三方包 |
| 有没有现成的相似插件可参考？ | 见 `plugins/` 目录 | 减少重复造轮子 |

### 1.1 容器选择详解

- **`container="left"`**：停靠在 Tab 窗口左侧停靠区（类似 VS Code 左侧栏）
  - 适合：文件浏览器、项目导航等需要常驻左侧的面板
- **`container="right"`**：停靠在 Tab 窗口右侧停靠区（类似 VS Code 右侧栏）
  - 适合：监控面板、参考手册、历史记录等不遮挡主对话区的辅助面板
- **`container="bottom"`**：与系统配置卡片一致，显示在 chat_layout 下方并隐藏输入区
  - 适合：设置卡片、统计面板、管理界面（占用屏幕中下部）
- **`container="top"`**：独立浮动，不影响输入区
  - 适合：快速信息卡（如通知、提醒）、辅助工具
- **`container="full"`**：完整覆盖整个对话区（与系统配置卡片一致，走覆盖层）
  - 适合：需要全屏沉浸式操作的界面（画布、编辑器、大面板）

### 1.2 异步 vs 同步

| 场景 | 推荐 |
|------|------|
| 读 SQLite < 100ms | 可同步（不阻塞 UI 时） |
| 读 SQLite > 100ms / 大数据量 | 必须异步（QThread + pyqtSignal） |
| HTTP 请求 | 必须异步 |
| 文件扫描（> 1k 文件） | 必须异步 |
| 读小文件 / 静态数据 | 可同步 |

> ⚠️ **99% 的情况建议异步**——简单且保证 UI 不卡顿。

---

## Phase 2：创建插件目录结构

### 2.1 标准结构（系统插件 + 用户插件通用）

```
plugins/<plugin-name>/
├── .drifox-plugin/
│   └── plugin.json          # 插件清单，声明 "ui": true
└── ui/
    ├── __init__.py           # register_ui 入口
    ├── cards.py              # 浮动卡片 widget（可选）
    ├── renderers.py          # 内容块渲染器（可选）
    ├── widgets.py            # 复用 widgets（可选，从 widgets-*.md 复制）
    └── _vendor/              # 可选：第三方纯 Python 依赖（见 templates.md §五）
        └── <package>/
```

### 2.2 最小结构（最简卡片）

```
plugins/<your-plugin>/
└── ui/
    ├── __init__.py       # register_ui
    └── cards.py          # 主卡片 widget
```

> 系统插件放在 `plugins/<plugin-name>/`（仓库内）。
> 用户插件放在 `~/.drifox/plugins/<plugin-name>/`（用户目录，运行时下载）。

### 2.3 plugin.json 模板

最小可用版本：

```json
{
    "name": "<plugin-name>",
    "description": "<一句话功能>",
    "version": "0.1.0",
    "author": {
        "name": "DriFox Contributors"
    },
    "type": "user",
    "components": {
        "ui": true
    }
}
```

> `type: "user"` 标记为用户插件；系统插件可省略此字段。
> `"ui": true` 声明这是一个 UI 插件，否则 `register_ui` 不会被调用。

完整字段说明见 `architecture.md`。

---

## Phase 3：按模板生成代码

### 3.1 推荐顺序

1. **先骨架** → 复制 `templates.md §一` 浮动卡片骨架到 `cards.py`
2. **再控件** → 按 frontend-design 的组件清单，从 `widgets-*.md` 复制 widgets 到 `cards.py` 或独立 `widgets.py`
3. **再数据** → 用 `widgets-sqlite.md` 的查询模式写 `_fetch_data()`
4. **再入口** → 复制 `templates.md §四` register_ui 入口到 `__init__.py`
5. **再主题** → 用 `widgets-theme.md` 的 `_make_chart_colors_from_context`

### 3.2 设计约束（插件闭包）

- ❌ **不导入** `app.core` 或 `app.widgets` 内部的任何模块
- ✅ 用 `pathlib` + `shutil` + `sqlite3` 等 stdlib 操作
- ✅ 用 `qfluentwidgets.isDarkTheme()` 做主题检测（但**优先用上下文注入的 colors**，见 `widgets-theme.md §八`）
- ✅ 用 `loguru` 做日志

### 3.3 不需要从零写的部分

| 你需要 | 复制自 |
|--------|--------|
| 完整浮动卡片骨架 | `templates.md §一.1` |
| register_ui 入口（基础） | `templates.md §四.2` |
| register_ui 入口（含 _vendor/） | `templates.md §五.5` |
| 统计卡片 widget | `widgets-statcard.md` |
| 柱状/折线/水平柱状图 | `widgets-charts.md` |
| 数字格式化 / token 估算 / 日期工具 | `widgets-utils.md` |
| SQLite 读取模式 | `widgets-sqlite.md` |
| 主题色映射 | `widgets-theme.md` |
| 异步 worker 模板 | `templates.md §一.1` |

---

## Phase 4：验证

### 4.1 代码质量

```bash
# ruff 检查（项目用 ruff，行宽 120，双引号）
ruff check plugins/<your-plugin>/ui/
ruff format --check plugins/<your-plugin>/ui/
```

### 4.2 实际加载测试

```bash
# dev 环境：直接运行
python main.py

# 触发插件热重载：修改 plugin.json 或 ui/ 下任一文件
# 主程序会自动检测变更并重新执行 register_ui

# 打包后测试：见 testing-vendor.md
```

### 4.3 调试技巧

```python
# 在 register_ui 末尾加诊断日志
def register_ui(registry):
    # ... 原有逻辑 ...
    logger.info(f"[<plugin>] UI registered, context_provider={registry.has_context_provider()}")

# 在 show_card 中加断点
def show_card(self):
    import pdb; pdb.set_trace()  # ← dev 环境调试
    self._apply_latest_theme()
    self._async_load_data()
```

### 4.4 完整验证清单

见 `references/checklist.md`。

---

## Phase 5：提交与发布

### 5.1 提交规范

```
feat(<plugin-name>): <功能描述>
fix(<plugin-name>): <修复描述>
docs(<plugin-name>): <文档更新>
chore(<plugin-name>): <杂项>)
```

### 5.2 用户插件发布

用户插件不在仓库内，运行时从市场下载：
- 打成 zip 包（含整个 `plugins/<plugin-name>/` 目录）
- 上传到插件市场
- 用户点击安装后解压到 `~/.drifox/plugins/<plugin-name>/`

打包脚本示例：

```bash
# 在项目根目录
python build.py plugin <plugin-name>
# 或手动：
cd plugins/<plugin-name>
zip -r ../../dist/<plugin-name>.zip . -x "*.pyc" -x "__pycache__/*"
```

> 用户插件的依赖必须用 `_vendor/` 模式（见 `templates.md §五`），否则打包后运行会 `ImportError`。

### 5.3 提交时附带的文档

按项目规范，**任何变化必须同步更新文档**。新建插件时建议附带：

- [ ] `.drifox-plugin/plugin.json`（必须）
- [ ] `ui/__init__.py`（必须）
- [ ] `ui/cards.py` 或 `ui/renderers.py`（必须）
- [ ] README.md（推荐，说明插件用途和用法）
- [ ] 截图 / GIF（推荐，便于用户理解）