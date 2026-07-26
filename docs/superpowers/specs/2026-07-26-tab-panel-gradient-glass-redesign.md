# Tab 管理器左侧面板风格优化 & 折叠功能

**日期**: 2026-07-26  
**状态**: 设计中  
**影响范围**: `app/widgets/tab_panel.py`, `app/widgets/tab_manager_window.py`

---

## 1. 目标

优化 Tab 管理器左侧面板（TabPanel）的视觉风格，从当前扁平设计升级为**渐变毛玻璃风格**，同时将标题栏静态图标改为**面板折叠/展开按钮**，折叠后保留 **48px 图标条**（项目头像 + 状态徽标）。

---

## 2. 功能概述

### 2.1 标题栏折叠按钮

- **位置**：标题栏最左侧，替换原有静态 `drifox.ico` 图标
- **图标**：`<rect>` + `<` 箭头组合的侧栏收折图标
- **展开态**：箭头指向左（收起面板）
- **收起态**：图标水平翻转（展开面板），tooltip 更新
- **交互**：点击切换面板展开/收起，QVariantAnimation 平滑过渡（~200ms）
- **热区**：28×26px，hover 时背景微亮

### 2.2 折叠图标条（收起态）

当面板收起时，不隐藏整个 TabPanel，而是变为 **48px 宽**的图标条：

```
┌──────────┬──────────────────────────────┐
│ [项] ◉   │                              │
│ [项]     │      会话内容区域              │
│ [项]     │                              │
│ ──────── │                              │
│   ＋     │                              │
└──────────┴──────────────────────────────┘
```

- 每个 Tab → 32×32 项目头像图标（与展开态相同颜色/首字母）
- 选中态 → 左侧渐变指示条 + 微光晕
- 流式输出 → 右上角蓝色脉动徽标
- 错误 → 右上角红色徽标
- 提问等待 → 右上角橙色脉动徽标
- 底部 `+` 按钮 → 虚线边框，hover 高亮 → 新建标签页
- 图标条顶部 → 渐变发光线（与展开态一致）

### 2.3 展开态面板（渐变毛玻璃）

```
┌──────────────────────┐
│  ═══ 顶部渐变发光线 ═══ │
│  [图标] DriFox  3 会话 │  ← 品牌区块
│  ──── 渐变分隔线 ────  │
│  [icon] 插件 A        │  ← 插件列表（保留图标+点击）
│  [icon] 插件 B        │
│  ──── 渐变分隔线 ────  │
│  ┌──────────────────┐ │
│  │ ＋ 新建标签页      │ │  ← 渐变按钮
│  └──────────────────┘ │
│  ┃ [项] 项目 A · 会话  │  ← 渐变背景 + 指示条
│    [项] 项目 B         │
│    [项] 项目 C         │
│  ──── 渐变分隔线 ────  │
│  ⚙ 设置  🔌 Gitee    │  ← 扁平 hover 按钮
└──────────────────────┘
```

---

## 3. 视觉 Token（样式参数）

### 3.1 面板背景

```
background: linear-gradient(180deg,
  rgba(CARD_BG_R, CARD_BG_G, CARD_BG_B, 0.97) 0%,
  rgba(CARD_BG_R, CARD_BG_G, CARD_BG_B, 0.98) 50%,
  rgba(CARD_BG_R-5, CARD_BG_G-5, CARD_BG_B-5, 0.99) 100%);
```

- 颜色取自 `Colors.CARD_BG` 的 RGB 分量
- 所有颜色通过 `Colors.*` 动态获取，主题切换时 `refresh_style()` 自动重建

### 3.2 顶部渐变发光线

```
height: 1px
background: linear-gradient(90deg, transparent, Colors.INFO(alpha=0.35),
            rgba(139,92,246,0.35), transparent)
margin: 0 12px
```

- 实现方式：自定义 `paintEvent` 在面板顶部绘制，或用一个 1px QFrame + stylesheet

### 3.3 品牌区块

- 图标：22×22 渐变方块 (`linear-gradient(135deg, #66c6ff, #8b5cf6)`)
- 文字 "DriFox"：13px, font-weight 600
- 会话计数徽标：`Colors.TEXT_MUTED` 色 + `rgba(255,255,255,0.05)` 背景胶囊

### 3.4 UI 插件列表

保留原有 `UIPluginRow` 结构（图标 + 文字 + 点击），升级外观：

```
# 每行
background: transparent
border-radius: 5px
padding: 5px 10px

# hover
background: rgba(255,255,255,0.04)

# 图标
16×16, 跟随主题自动切换深色/浅色图标
```

- 无插件时隐藏整个插件区域（含其上下分隔线）
- 最大高度 120px + 滚动条（与现状一致）
- 折叠态图标条中不显示插件

### 3.5 新建标签页按钮

```
background: linear-gradient(135deg,
  rgba(Colors.INFO_RGB, 0.12),
  rgba(139,92,246, 0.08))
border: 1px solid rgba(Colors.INFO_RGB, 0.18)
color: Colors.INFO (即 accent 色)
border-radius: 8px
```

- hover: 背景加深 0.08，border alpha 增至 0.35

### 3.6 Tab 项

| 状态 | 背景 | 其他 |
|---|---|---|
| 默认 | transparent | 关闭按钮不可见 |
| hover | `rgba(255,255,255,0.04)` | 关闭按钮可见 |
| 选中 | `linear-gradient(135deg, rgba(INFO,0.18), rgba(139,92,246,0.08))` | 左侧 2px 渐变指示条 + `box-shadow: 0 0 14px rgba(INFO,0.06)` |

- 指示条：`linear-gradient(180deg, Colors.INFO, #8b5cf6)`，2px 宽

### 3.7 渐变分隔线

```
height: 1px
background: linear-gradient(90deg, transparent, rgba(255,255,255,0.08), transparent)
margin: 0 16px
```

### 3.8 底部控件

```
font-size: 11px
color: Colors.TEXT_MUTED
padding: 4px 8px
border-radius: 5px
```

- hover: `background: rgba(255,255,255,0.06)`, color → `Colors.TEXT_PRIMARY`

### 3.9 折叠图标条

- 宽度：48px（CSS 固定）
- 图标：32×32，圆角 7px，居中排列，gap 6px
- 选中态指示条：左侧 2px 渐变，位置 `left: -4px`
- 状态徽标：8×8 圆形，右上角 `top: -2px; right: -2px`，`border: 2px solid 面板背景色`
- `+` 按钮：32×32，`border: 1px dashed rgba(255,255,255,0.15)`

---

## 4. 代码结构变更

### 4.1 `app/widgets/tab_panel.py`

| 变更 | 说明 |
|---|---|
| 新增 `IconStripWidget` 类 | 折叠态图标条，包含图标列表 + 新建按钮 + 状态徽标 |
| 修改 `TabPanel._setup_ui()` | 重构布局：品牌区块 → 插件胶囊 → 新建按钮 → Tab 列表 → 分隔线 → 底部 |
| 修改 `TabPanel.paintEvent()` | 绘制顶部渐变发光线 |
| 新增 `TabPanel.set_collapsed(bool)` | 切换到图标条模式，同步状态 |
| 修改 `TabItem.paintEvent()` | 选中态从纯色改为渐变 + 光晕 |
| 新增 CSS 类属性 | `_expanded_style` / `_collapsed_style` 两套 stylesheet，切换时整体替换 |
| 保留 `_plugin_scroll`，移除 `_plugin_title` | 插件列表保留滚动区，移除 "UI 插件" 标题文字，以渐变分隔线与品牌区块分隔 |

### 4.2 `app/widgets/tab_manager_window.py`

| 变更 | 说明 |
|---|---|
| 修改 `TabManagerTitleBar._setup_ui()` | 图标 `QLabel` → `TransparentToolButton` 折叠按钮 |
| 新增 `TabManagerTitleBar.toggleSidebarRequested` 信号 | 点击时发出 |
| 修改 `TabManagerWindow._setup_ui()` | 连接折叠信号 → `_tab_panel.set_collapsed()` |
| 修改 splitter | 折叠时 setSizes([48, 剩余]) |
| 新增 `TabManagerWindow._on_toggle_sidebar()` | 处理折叠/展开逻辑 |
| 保存侧栏状态 | 新增 `Settings.tab_sidebar_collapsed` 布尔配置项 |

### 4.3 `app/utils/config.py`

新增配置项：
```python
tab_sidebar_collapsed: bool = False  # 侧栏是否折叠
```

---

## 5. 动画过渡

- **折叠/展开**：通过 `QVariantAnimation` 驱动 Splitter 的 `setSizes`，200ms ease-out
- **Tab 项 hover**：纯 CSS `transition: background 0.15s`
- **折叠按钮 hover**：`transition: background 0.15s`
- **状态徽标脉动**：通过现有 `_anim_timer` / `_question_phase` 驱动，无需新增 timer

---

## 6. 主题兼容

- 所有颜色通过 `Colors.*` 类属性获取，不硬编码
- `refresh_style()` 方法重建所有 stylesheet，主题切换时自动调用
- 渐变方向与透明度固定（不随主题变化），RGB 分量从 `Colors.CARD_BG` 动态提取
- 发光 alpha 值固定，保证深/浅主题下观感一致

---

## 7. 向后兼容

- `TabPanel` 公开 API 不变（`add_tab`, `remove_tab`, `update_tab_*` 等方法签名不变）
- UI 插件列表接口不变（`refresh_ui_plugins()` 仍可用）
- 现有调用方（`TabManagerWindow.add_window` 等）无需修改
- 默认展开态，折叠状态由 Settings 配置项控制

---

## 8. 不变更范围

- 不修改右侧 QStackedWidget 内容区样式
- 不修改消息卡片、输入框、EdgeLauncher 等其他组件
- 不新增依赖包（所有效果用 PyQt5 原生 + stylesheet 实现）
- 不新增动画框架依赖

---

## 9. 风险 & 注意事项

1. **渐变提取复杂度**：`Colors.CARD_BG` 格式为 `rgba(r,g,b,{alpha})`，需要解析 RGB 分量构造渐变。建议在 `Colors` 类新增 `CARD_BG_RGB` 属性或工具方法。
2. **QSplitter handle**：折叠态时 splitter handle 应在图标条右侧保持可拖拽，但最小宽度锁定为 48px。
3. **Gitee 账号行**：折叠态图标条宽度不足以显示 Gitee 行，折叠时隐藏。
4. **设置按钮**：折叠态不显示底部按钮行，设置入口通过托盘菜单或快捷键访问。

---

## 10. 测试要点

- [ ] 深色/浅色主题切换后样式正确
- [ ] 字体缩放后图标尺寸与文字比例协调
- [ ] 折叠/展开动画流畅，无闪烁
- [ ] 折叠态切换 Tab 正常
- [ ] 流式/错误/提问状态徽标正确显示
- [ ] 无插件时胶囊区域不显示空白
- [ ] 关闭所有 Tab 后空状态页正常显示
- [ ] Gitee 账号行在展开态正常渲染
- [ ] 拖拽面板边缘 resize 正常
