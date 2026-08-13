# project-dashboard 插件设计文档

> 日期：2026-08-13
> 状态：已批准
> 分支：dev

## 1. 目标

为 DriFox 新增一个「项目信息看板」欢迎卡片 tab + 配套 function 命令：

- **command**（`/project-dashboard`）：在项目固定位置（git 根 `.drifox/reports/`）生成项目多元化图表信息 HTML 文件
- **UI 插件**（welcome tab）：在欢迎卡片上通过 iframe 展示该 HTML，tab 底部保留追问按钮，点击即重新生成

## 2. 架构

```
plugins/project-dashboard/
├─ .drifox-plugin/plugin.json          # name=project-dashboard, components: ui + commands
├─ commands/project-dashboard.md       # type: function 命令定义（描述 + 参数提示）
└─ ui/
   ├─ __init__.py                      # register_ui：注册 welcome tab + FunctionCommandHandlers handler
   └─ dashboard.py                     # 生成 HTML 核心逻辑（git log + 文件扫描 + echarts 组装）
```

## 3. 数据流

```
用户点 welcome tab 内追问按钮「🔄 重新生成」
→ context-tag (data-action="ask", data-content="/project-dashboard")   [复用现成机制]
→ contextActionRequested("ask", "/project-dashboard")
→ handle_recommended_question → send_preset_question → _on_send_clicked
→ cmd_mgr.execute → FUNCTION handler 执行
→ dashboard.py：git log 采集 + 文件系统扫描
→ 生成 <git-root>/.drifox/reports/project-dashboard.html
→ handler 触发 welcome card 重渲染（set_welcome_mode 同 mode）
→ render_func 重新执行 → iframe src 带 ?t=mtime 时间戳 → 浏览器重新加载最新文件
```

**零主程序改动**：整条链路复用现有 welcome tab + context-tag 追问机制。

## 4. welcome tab 设计

| 项 | 值 |
|---|---|
| mode_key | `project-dashboard` |
| label | `📊 项目看板` |
| render_func | `render_welcome_tab(ctx)` |

### 4.1 render_func 输出（HTML 片段）

```
[概要行] 项目名 · 分支 · 生成时间 · commit 总数（纯文字，1 行）
[iframe] <iframe src="file:///<git-root>/.drifox/reports/project-dashboard.html?t=<mtime>">
[追问按钮] <span class="context-tag" data-action="ask" data-content="/project-dashboard">🔄 重新生成</span>
```

- HTML 文件不存在 → 概要行显示「尚未生成」+ 追问按钮（无 iframe）
- **高度控制**：iframe 固定高度 `height: 460px`（主程序 echarts 容器 400px 参考值），宽度 100%
- 明暗适配：概要行文字色读 `ctx["is_dark"]`（同 calendar 模板）
- project_root 获取：`os.getcwd()` 兜底；优先从 `UIPluginRegistry` 活跃窗口 context provider 拉取（不依赖 ctx 注入扩展）

### 4.2 概要行内容（文字少而精，用户明确「不能多，主要是图表」）

```
**项目名** · `分支` · 生成于 08-13 11:30 · 共 128 commits
```

## 5. command 设计

### 5.1 命令定义 `commands/project-dashboard.md`

```yaml
---
description: 生成项目信息看板 HTML（commit 趋势/语言分布/贡献者/文件统计），输出到 .drifox/reports/
type: function
---
```

### 5.2 handler 注册（ui/__init__.py）

```python
from app.core.command_manager import CommandManager, CommandType
from app.core.builtin_commands import FunctionCommandHandlers

CommandManager.get_instance().register(
    name="project-dashboard",
    command_type=CommandType.FUNCTION,
    description="生成项目信息看板 HTML",
)
FunctionCommandHandlers.register("project-dashboard", _handler)
```

### 5.3 生成逻辑（ui/dashboard.py）

1. 解析工作目录（优先活跃窗口 provider 的 project_root，兜底 os.getcwd()）
2. 定位 git 根（`git rev-parse --show-toplevel`）
3. 采集数据（全部 subprocess 调 git，超时保护）：
   - **commit 趋势**：近 30 天每日 commit 数（`git log --since=30d --pretty=format:%ad --date=short` + 按日聚合）
   - **贡献者 Top**：`git shortlog -sne --no-merges | head -8`（姓名 + commit 数）
   - **语言分布**：遍历 git 根下文件（跳过 `.git`、`.drifox`、`node_modules`、`__pycache__`、`venv` 等），按扩展名映射语言，统计行数/文件数
   - **文件统计**：Top 扩展名文件数条形图
4. 生成独立 HTML：内联 CSS + echarts（引用 DriFox `app/resources/web/vendor/echarts.min.js` 的 file:// 绝对路径，离线可用）
5. 明暗：HTML 内嵌深色/浅色两套 CSS，跟随参数（render 时读 ctx 传入）

### 5.4 图表清单（4 图，echarts 自绘）

| 图表 | 类型 | 数据源 |
|---|---|---|
| commit 趋势 | 折线/柱状（近 30 天） | git log 按日聚合 |
| 贡献者 Top8 | 水平条形图 | git shortlog |
| 语言分布 | 饼图/环形图 | 扩展名统计 |
| 文件类型 Top | 水平条形图 | 扩展名文件数 |

## 6. iframe 内 HTML 技术要点

- `<script>` 在 iframe 内**可正常执行**（iframe 是独立文档，不受欢迎卡片 innerHTML 限制）——echarts 正常初始化
- echarts 加载：`<script src="file:///<DriFox根>/app/resources/web/vendor/echarts.min.js">`，跟随 `_PROJECT_ROOT` / `sys._MEIPASS` 探测（复用 message_card `_vendor_script_tags_cache` 的探测逻辑思路）
- 生成时间戳：HTML 内嵌 `生成于 YYYY-MM-DD HH:MM` 文字
- 高度：HTML body 高度 ≈ 460px 以内（与 iframe 匹配，避免滚动条）
- 明暗：HTML 接收 is_dark 参数生成对应色板（不依赖 prefers-color-scheme，与 Qt 主题对齐）

## 7. 错误处理

| 场景 | 行为 |
|---|---|
| 非 git 仓库 | 概要行提示「当前目录不是 git 仓库」，不生成 HTML |
| git 命令失败/超时 | 对应图表显示空数据提示，其余图表正常 |
| 目录不存在 | 自动创建 `.drifox/reports/` |
| HTML 不存在（首用） | tab 显示「尚未生成」+ 追问按钮 |

## 8. 验证清单

```
启动程序 → 欢迎卡片出现「📊 项目看板」tab
1. tab 标签文字显示正确？            → label 参数
2. 首用（无 HTML）显示「尚未生成」+ 按钮？
3. 点追问按钮 → command 执行 → HTML 生成 → tab 自动刷新显示 iframe？
4. iframe 内 4 个图表渲染正确？      → echarts 正常初始化（无 JS 报错）
5. 明暗主题切换颜色跟随？            → ctx["is_dark"] 生效
6. 高度合适（无多余滚动条）？        → iframe 460px 固定
7. 概要行文字精简（≤2 行）？
8. 再次点击追问按钮 → 重新生成（时间戳更新）？
```

## 9. 明确不做（YAGNI）

- 不做自动检测 git 变更重生成（用户确认手动触发）
- 不做图表交互定制（tooltip/缩放用 echarts 默认）
- 不做多项目切换（跟随当前项目）
- 不加第三方依赖（纯 stdlib + subprocess + 现有 vendor echarts）
