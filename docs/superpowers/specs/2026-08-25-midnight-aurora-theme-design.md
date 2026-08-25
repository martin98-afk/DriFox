# 午夜极光主题设计

## 1. 目标

为 DriFox 增加一套独立的深色主题“午夜极光”，不改变当前主题选择，也不修改现有 UI 代码。主题采用分区背景：

- 右侧对话区：使用用户提供的极光参考图。
- 左侧区域：使用 Python 标准库生成的竖向长条极光渐变图。
- 窗口与组件：提供与背景协调的深色 UI 色板。

## 2. 主题结构

新增目录：

```text
plugins/system/themes/midnight_aurora/
├── midnight_aurora.yaml
├── right_aurora.png
└── sidebar_aurora.png
```

主题 ID 为 `midnight_aurora`，显示名称为“午夜极光”，显式声明为 `dark`。主题不自动启用，用户可在主题设置中手动选择。

主题使用现有 `backgrounds` schema：

- `backgrounds.window`：窗口深色底色。
- `backgrounds.sidebar`：左侧竖向极光图。
- `backgrounds.scene`：右侧极光场景图。

## 3. 右侧背景

`right_aurora.png` 从用户提供的源文件复制到主题目录，避免运行时依赖外部 Downloads 路径。

右侧区域配置目标：

- `image: right_aurora.png`
- `opacity: 0.86`
- `blur: 5`
- `dim: rgba(4, 5, 18, 0.34)`
- `enabled: true`

压暗与模糊仅由主题层处理，不修改图片源文件。场景图作为整个右侧 `_chat_frame` 的底层背景，现有布局控件继续显示在其上方。

## 4. 左侧背景生成

新增 `scripts/generate_midnight_aurora_sidebar.py`。脚本使用 Python 标准库，不增加 Pillow 等运行时依赖，默认生成 `480 × 1920` RGBA PNG。

生成算法：

1. 在水平方向按多个颜色节点插值：深靛蓝 → 紫罗兰 → 品红 → 青绿 → 深海蓝。
2. 在竖直方向叠加低频波浪与局部光带，模拟极光纵向流动。
3. 对左右边缘降低透明度，使渐变图在侧栏背景上自然融合。
4. 使用固定随机种子或纯数学参数，保证脚本重复运行得到稳定结果。
5. 直接写标准 RGBA PNG 扫描行并压缩输出。

主题中的 `backgrounds.sidebar` 指向 `sidebar_aurora.png`，并使用深色底色作为透明边缘和缩放时的兜底。

## 5. UI 色板

以现有 `midnight` 主题为模板，调整为午夜极光配色：

- 卡片与输入区：深靛蓝/紫黑半透明底。
- 主要文字：近白色。
- 辅助文字：蓝灰/淡紫灰。
- 强调色：青绿与品红。
- 边框：低饱和蓝紫。
- 发送按钮与交互反馈：保留深色主题可读性所需的轻量霓虹渐变。

不在本任务中修改 `app/utils/theme_manager.py`、`tab_manager_window.py` 或其他应用代码。

## 6. 验证标准

1. `midnight_aurora.yaml` 可被 PyYAML 正常解析。
2. 主题管理器能列出 `midnight_aurora`，且不会覆盖其他主题。
3. `right_aurora.png`、`sidebar_aurora.png` 均存在且可被 Qt 读取。
4. 生成脚本重复运行后，输出尺寸、模式和文件有效性一致。
5. 工作树中 `app/widgets/cards/floating/command_card.py` 的既有暂存修改保持不变。

## 7. 非目标

- 不改变当前主题选择。
- 不修改主题管理器的数据结构。
- 不增加运行时图片生成。
- 不修改 `app/widgets/cards/floating/command_card.py`。
