---
name: ui-plugin-creator
description: DriFox UI 插件开发技能。用于创建、修改、调试 UI 插件（浮动卡片 / 内容块渲染器 / 消息元素工厂 / 欢迎卡片插件 tab）。
---

# ui-plugin-creator —— DriFox UI 插件开发技能

> 快速、规范地从用户意图转化为可工作的 UI 插件代码。
> **本文件是 TOC（总目录）**，详细内容按主题拆分到 `references/` 子文件，按需加载。

---

## ⚠️ 重要：新建 UI 插件必走的两个前置技能

**开发任何新的 UI 插件之前，必须先调用：**

| 技能 | 何时调用 | 产出 |
|------|---------|------|
| **`brainstorming`** | 收到"做个新插件"请求时**第一时间**调用 | 用户意图、需求边界、功能清单、设计方案 |
| **`frontend-design`** | brainstorming 完成后、**动手写代码前**调用 | UI 视觉稿、组件布局、交互流程、配色方案 |

> 🛑 **不要跳过这两个技能直接进入编码**。
> 即使是"很简单的卡片"，也可能因为需求理解偏差导致反复返工。
>
> ✅ **正确的顺序**：
> 1. `brainstorming` → 搞清楚"做什么 / 不做什么"
> 2. `frontend-design` → 设计出"长什么样 / 怎么交互"
> 3. `ui-plugin-creator`（本技能）→ 落地代码实现

### 例外：修改现有插件

> 改现有插件的样式、加按钮、调参数等小修改**不需要** brainstorming / frontend-design。
> 直接读 `references/modifying.md` 即可。

---

## 0. 加载流程

```
Step 1  收到"做个新 UI 插件"请求
        ├─ 🟡 立即调用 brainstorming → 产出需求文档
        └─ 🟡 立即调用 frontend-design → 产出 UI 设计稿
Step 2  理解意图 → 按 §1 决策树分派组件类型
Step 3  读 references/ 对应文件（按需加载）
        ├─ 开发流程 → references/workflow.md
        ├─ 核心模式 → references/patterns.md
        ├─ 代码模板 → references/templates.md
        ├─ 可复用控件库 → references/widgets.md (索引)
        │   ├─ 统计卡片 → widgets-statcard.md
        │   ├─ 图表 → widgets-charts.md
        │   ├─ 工具函数 → widgets-utils.md
        │   ├─ SQLite 模式 → widgets-sqlite.md
        │   ├─ 主题色 → widgets-theme.md
        │   └─ 弹窗对话框 → templates.md §七（统一 MaskDialogBase 风格）
        ├─ 修改现有插件 → references/modifying.md
        ├─ 验证清单 → references/checklist.md
        └─ _vendor/ 外部依赖 → references/templates.md §五
Step 4  按 workflow.md 工作流推进
Step 5  按 templates.md + widgets-*.md 生成代码
Step 6  按 checklist.md 验证
```

---

## 1. 组件类型决策树

| 用户说 | 组件类型 | 必读 references |
|--------|---------|-----------------|
| "加个卡片""做个设置界面""显示统计面板""搞个管理界面" | **浮动卡片**（FloatingCard） | `templates.md` §一 |
| "加个图表""画个柱状图""折线图""水平条形图" | **图表控件**（Chart Widget） | `widgets-charts.md` |
| "加个统计卡片""显示数字指标""做个 KPI 卡" | **统计卡片**（StatCard） | `widgets-statcard.md` |
| "读 SQLite""查 14 天数据""新字段 fallback" | **SQLite 读取** | `widgets-sqlite.md` |
| "主题色跟着变""跟随系统颜色""深浅色适配" | **主题色映射** | `widgets-theme.md` |
| "在聊天里显示HTML""渲染自定义内容""做个消息卡片样式" | **内容块渲染器**（ContentRenderer） | `templates.md` §二 |
| "欢迎卡片加个 tab""会话初始卡片""HTML 注入初始卡片" | **欢迎卡片插件 tab**（WelcomeTab） | `templates.md` §八 |
| "替换消息气泡""自定义消息控件""做个消息widget" | **消息元素工厂**（MessageFactory） | `templates.md` §三 |
| "做个插件市场""安装插件""插件管理" | **完整插件**（全组件） | `templates.md` §四 + `architecture.md` |
| "插件需要 requests/PIL/... 等第三方包""打包后再加依赖" | **外部依赖（_vendor/）** | `templates.md` §五 |
| "改现有插件""加个按钮""调样式" | **修改现有插件** | `modifying.md` |

> ⚠️ **新插件优先走浮动卡片**——这是最常见的 UI 插件形态。
> ⚠️ **图表/统计控件是浮动卡片内的常用组件**，从 `widgets-*.md` 直接复用即可。
> ⚠️ **内容渲染器只做"展示"，交互按钮用 data 属性桥接。**
> ⚠️ **消息工厂是高级用法——99% 场景用浮动卡片就够。**

---

## 2. references/ 文件结构

```
plugins/system/skills/ui-plugin-creator/
├─ SKILL.md                ← 本文件（TOC，~6KB）
└─ references/
   ├─ workflow.md          开发工作流（澄清需求 → 创建结构 → 验证 → 发布）
   ├─ patterns.md          核心模式（上下文注入/比例高度/异步/热重载/信号链/_vendor/）
   ├─ templates.md         代码模板（浮动卡片/内容渲染器/消息工厂/welcome tab/register_ui）
   ├─ widgets.md           可复用控件库索引（设计原则 + 整合示例 + 陷阱速查）
   ├─ widgets-statcard.md  _StatCard（多层级统计卡片）
   ├─ widgets-charts.md    _BarChartWidget / _LineChartWidget / _ProjectBarWidget
   ├─ widgets-utils.md     工具函数（format / token 估算 / 日期）
   ├─ widgets-sqlite.md    SQLite 读取模式（路径兜底 / N 天窗口 / fallback）
   ├─ widgets-theme.md     主题色映射（ctx → QColor 字典）
   ├─ modifying.md         修改现有插件的步骤与调试
   ├─ checklist.md         UI 插件验证清单（11 大类）
   ├─ architecture.md      UI 插件架构总览
   └─ testing-vendor.md    _vendor/ 打包测试脚本
```

---

## 3. 推荐学习路径

### 3.1 第一次做 UI 插件

> 🟡 **第一步：调用 `brainstorming` 技能**（不是本技能！）
> 🟡 **第二步：调用 `frontend-design` 技能**
> ✅ **第三步**：才进入本技能的工作流

```
brainstorming → frontend-design → ui-plugin-creator
  需求边界        UI 设计稿         代码实现
```

详细步骤见 `references/workflow.md §1`。

### 3.2 想加图表/统计卡片

1. 读 `widgets.md` 索引
2. 复制 `widgets-statcard.md` 或 `widgets-charts.md`
3. 配合 `widgets-theme.md` 适配主题色

### 3.3 想读 SQLite

1. 读 `widgets-sqlite.md §一/§二`
2. 用 `widgets-theme.md` 处理颜色

### 3.4 想加外部依赖（如 `requests`）

1. 读 `templates.md §五`（含完整 register_ui 模板）
2. 读 `patterns.md §7`（_vendor/ 模式）
3. 用 `testing-vendor.md` 验证打包

### 3.5 改现有插件

1. 读 `modifying.md`（步骤 + 调试技巧）
2. 用 `checklist.md §3` 验证修改

---

## 4. 与其他技能的衔接

```
ui-plugin-creator（本技能）
├─ 🟡 新建插件 → 先 brainstorming，再 frontend-design，本技能负责代码落地
├─ 遵循 drifox-dev 编码规范 → drifox-dev/references/conventions.md
├─ 复杂功能拆多步 → subagent-driven-development
└─ 调试 bug → diagnose
```

### 4.1 与 brainstorming 的边界

| 阶段 | 技能 | 产出 |
|------|------|------|
| 用户说"我想要..." | **brainstorming** | 搞清楚意图、列出功能点、确定边界 |
| brainstorming 结束 | → **frontend-design** | 视觉稿、组件清单、交互流程 |
| frontend-design 结束 | → **ui-plugin-creator**（本技能） | 代码实现、模式选择、验证发布 |

> 本技能**不**做需求探索和 UI 设计——这两步必须由前置技能完成。

### 4.2 与 frontend-design 的边界

| 阶段 | 技能 | 产出 |
|------|------|------|
| "这个卡片应该长什么样" | **frontend-design** | 视觉稿、配色、布局图、组件规格 |
| "这个视觉稿怎么落地成代码" | **ui-plugin-creator** | 选控件、复制模板、实现交互逻辑 |

> 本技能**不**做视觉设计——视觉稿由 frontend-design 完成，本技能负责把视觉稿翻译成 PyQt5 代码。

---

## 5. 实战经验总结（踩坑记录）

### 5.1 Python 3.14 异常语法

Python 3.14 不再支持 Python 2 风格的 `except Exception, e:` 语法，必须用 `except Exception as e:`。在卡片代码中：

```python
# ❌ 不可用（Python 2 语法，3.14 报 SyntaxError）
except OSError, PermissionError:
    pass

# ✅ 正确
except (OSError, PermissionError):
    pass

# ✅ 正确（带异常变量）
except (OSError, PermissionError) as e:
    logger.error(f"操作失败: {e}")
```

### 5.2 按钮高度 vs padding 陷阱

```python
# ❌ 按钮文字不显示
btn = QPushButton("文字")
btn.setFixedHeight(32)
btn.setStyleSheet("padding: 16px 0;")   # 32 - 16 - 16 = 0 → 内容区为 0

# ✅ 正确
btn.setStyleSheet("padding: 0 10px;")   # 左右 padding，不占用高度
```

**规则**：`fixedHeight` 小于 40px 的按钮，用 `padding: 0 Xpx`；大按钮用 `padding: Ypx 0`。

### 5.3 QTimer.singleShot 按钮自动恢复模式

当操作完成后需要临时显示成功文案，数秒后恢复默认文案：

```python
self._btn.setText("✅ 清理完成，释放 234 MB")
QTimer.singleShot(3000, self._reset_btn)  # 3 秒后恢复

def _reset_btn(self):
    if not self._is_busy:  # 防止恢复时正在执行新操作
        self._btn.setText("默认文案")
```

### 5.4 字体注入容易漏

**最常犯的 bug**：卡片看起来功能正常，但字体和主程序不一致。

```python
# ❌ 在 _apply_latest_theme 里只更新颜色
child.setStyleSheet("color: rgba(255,255,255,0.9);")

# ✅ 用 _make_style 同时注入颜色 + 字体
child.setStyleSheet(_make_style(tc, font_family, font_size))
```

### 5.5 异步 worker 生命周期

```python
def _cleanup_worker(self):
    if self._worker_thread is not None:
        try:
            self._worker_thread.quit()
            self._worker_thread.wait(500)  # 超时防止死锁
        except RuntimeError:
            pass
        self._worker_thread = None
    self._worker = None

def deleteLater(self):
    self._cleanup_worker()  # 必须清理！否则线程泄漏
    super().deleteLater()
```

### 5.6 生成代码后的自检清单

```
拿到新卡片 → 开程序 → 输 /system-cleaner
1. 字体和主界面一致吗？        → 不一致 → 检查 _apply_latest_theme 的字体处理
2. 按钮文字显示完整吗？         → 不完整 → 检查 button padding 和 fixedSize
3. 主题色跟随主程序变吗？      → 不跟  → 检查 set_context_provider + _apply_latest_theme
4. 刷新/操作不卡 UI 吗？       → 卡UI  → 检查是否用了 QThread
5. 连续快速点击会崩吗？        → 崩溃  → 检查 _is_busy 防重入 + worker cleanup
```