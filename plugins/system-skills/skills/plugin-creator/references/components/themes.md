---
description: Themes（主题）开发指南——文件布局、colors token、backgrounds 各区域、动态视频、主题音乐、路径解析与排障
---

# Themes（主题）组件开发

## 文件位置

```
<plugin>/themes/<theme-name>/<theme-name>.yaml
<plugin>/themes/<theme-name>/           ← 主题资源（图片/视频/音频）放同目录
```

目录名 = 主题 id = yaml 文件 stem。`id` 字段必填，必须与目录名一致。

一个插件可以带多个主题，主题之间是并列的子目录：

```
<plugin>/themes/
├── amber/
│   ├── amber.yaml
│   └── bg.png
├── ocean/
│   └── ocean.yaml
└── shared/          ← 不是主题（无 yaml），只放跨主题共用的素材
    └── rain.mp3
```

> `shared/` 这种没有 yaml 的目录不会被当成主题加载，可以安全用来放共用素材。

## 最小模板

```yaml
name: "琥珀日落"
id: "amber"
mode: dark          # dark / light，决定浅色检测与图标反色

window:
  gradient_start: rgba(20, 16, 8, 255)
  gradient_end: rgba(45, 31, 13, 255)

colors:
  card_bg: rgba(55, 38, 18, 232)
  text_primary: '#f5edd8'
  accent: '#d4893b'
```

只写 `name` / `id` / `colors` 也能通过加载；`backgrounds` 区块全部可选。

## 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | ✅ | 主题显示名（可中文） |
| `id` | string | ✅ | 主题标识符，须与目录名一致 |
| `mode` | string | ❌ | `dark` / `light`，缺省按 `text_primary` 亮度自动判定 |
| `window` | object | ❌ | 窗口渐变等外观（`gradient_start` / `gradient_end`） |
| `colors` | object | ✅ | 颜色 token 集 |
| `backgrounds` | object | ❌ | 多区域背景、动态视频、主题音乐 |
| `background` | object | ❌ | 旧字段（`window_bg` / `chat_list`），仅为兼容存量主题保留，新主题不要用 |

## backgrounds 总览

```yaml
backgrounds:
  window:      {...}   # 整个窗口最底层
  sidebar:     {...}   # 左侧 Tab 面板
  chat_area:   {...}   # 右侧对话区纯色底
  scene:       {...}   # 右侧整个圆角矩形的场景图（推荐用这个做主题主背景）
  decorations: [...]   # 装饰件数组
  overlay:     {...}   # 动态光影视频覆盖层
  music:       {...}   # 主题背景音乐播放列表
```

### 区域背景（window / sidebar / chat_area / scene）

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `color` | string | 无 | 纯色底，支持 `#hex` 与 `rgba()` |
| `image` | string | 无 | 图片路径，可与 `color` 共存（color 作底色） |
| `opacity` | float | `1.0` | 图片透明度 0.0~1.0 |
| `blur` | int | `0` | 模糊半径 0~30（仅图片层，纯色不受影响） |
| `dim` | string | 无 | 半透明蒙版，叠在图片上压暗提升可读性 |
| `enabled` | bool | `true` | 关闭后忽略该区域 |

`scene` 撑满整个右侧圆角矩形，覆盖 tab bar / 对话区 / 输入框 / 各方向停靠区 / UI 插件槽位，是主题主背景的首选落点。`chat_area` 是更内层的老区域，仅在需要兼容旧主题时用。

### decorations（装饰件）

数组，每项：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | string | 无 | 唯一标识，仅用于调试 |
| `image` | string | 必填 | 图片路径 |
| `anchor` | string | `scene` | 锚点，见下表 |
| `offset` | [int, int] | `[0, 0]` | 像素偏移 `[x, y]` |
| `stretch_x` | bool | `false` | 水平拉伸（仅 `anchor: scene` 有效） |
| `opacity` | float | `1.0` | 0.0~1.0 |

`anchor` 取值：`scene`（撑满对话区）、`scene_top`、`scene_bottom`、`input_top`、`input_bottom`。

### overlay（动态光影视频层）

```yaml
backgrounds:
  overlay:
    target: chat_area
    video: leaves-overlay.mp4
    opacity: 0.28
    brightness_boost: rgba(255, 253, 247, 0.12)
    position: top
    enabled: true
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `target` | string | `chat_area` | 挂载区域：`window` / `sidebar` / `chat_area` / `scene`，非法值回落 `chat_area` |
| `video` | string | 必填 | 视频路径 |
| `opacity` | float | `0.28` | 压暗强度 0.0~1.0 |
| `brightness_boost` | string | 无 | 亮度补偿色，支持 `rgba()`，补偿遮罩造成的压暗 |
| `position` | string | `center` | cover 裁剪的垂直对齐：`top` / `center` / `bottom` |
| `enabled` | bool | `true` | 关闭后停止播放并隐藏 |

**混合语义**：视频画面按明暗转成黑色 alpha 遮罩压暗下层（白色区域透明、暗部留阴影），不是 CSS 的 `multiply` 混合模式。效果是缓慢流动的光影，只影响下方的背景层，不影响上层 UI 卡片。视频恒静音、循环播放。

**`target` 的挂载点**：`window` 挂窗口自身（贴所有内容之下）、`sidebar` 挂左侧面板、`chat_area` / `scene` 都挂右侧圆角矩形。一个主题只允许一处视频，切换 target 时其它区域的视频会停止。

**`position` 为什么重要**：竖版素材（如 1080×1920）在横窗内按 cover 等比放大后高度溢出，`center` 只显示原图中段。树影这类集中在顶部的素材必须配 `top`，否则看到的是一片空白。

### music（主题背景音乐）

```yaml
backgrounds:
  music:
    tracks:
      - ../shared/rain.mp3
      - ambient-2.mp3
    volume: 0.6
    loop: true
    fade_ms: 300
    enabled: true
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `tracks` | list[string] | 必填 | 有序播放列表，至少一条；写单个字符串会被当作单元素列表 |
| `volume` | float | `0.6` | 主题推荐音量 0.0~1.0 |
| `loop` | bool | `true` | `true` = 列表循环；`false` = 播完最后一首停止 |
| `fade_ms` | int | `300` | 换列表时淡出→淡入的总时长，`0` = 直接切 |
| `enabled` | bool | `true` | 关闭后停止播放 |

行为约定：

- 切到带 `music` 的主题自动播放；切到没有 `music`（或 `enabled: false`）的主题立即停止。
- **多个主题用同一个 `tracks` 列表时，来回切换不打断当前曲目**（内部按解析后的绝对路径元组比对身份）。
- 换列表时淡出约 `fade_ms/2`、换曲、再淡入 `fade_ms/2`，中间有一瞬静音。
- 应用失焦（切到别的窗口）自动暂停，切回续播。用户主动停止后不会自动复活。
- `volume` 只是主题推荐值。用户在外观页调过「主题音乐音量」后，恒以用户设置为准。
- 单曲无法解码时跳过继续下一首；全部无法播放则停止，只记日志不弹窗。

> **`tracks` 不支持 `:qrc`**。QtMultimedia 的后端（Windows 上是 DirectShow / WMF）读不到 Qt 资源系统，`:qrc` 路径的音频会直接报 ResourceError。音乐只支持文件路径。

## 资源路径三种写法

| 写法 | 示例 | 解析规则 |
|------|------|---------|
| 主题目录内相对路径 | `bg.png` / `leaves-overlay.mp4` | 拼到当前主题目录下 |
| 跨主题/跨目录相对路径 | `../shared/rain.mp3` | 拼到主题目录后规范化，`..` 会被解析 |
| 绝对路径 | `D:/assets/piano.ogg` | 原样使用 |
| qrc 路径 | `:/icons/fox_bg.png` | 仅图片与视频可用；**音乐不支持** |

文件不存在时：图片/视频静默跳过（不显示该层），音乐记 warning 并跳过该曲。不会因此阻止主题加载。

`:qrc` 路径只能引用打包进程序的资源，插件主题通常用不到，自己的素材一律放主题目录里。

## colors 颜色 token

```yaml
colors:
  card_bg: rgba(...)         # 卡片背景
  card_bg_solid: rgba(...)   # 卡片纯色背景（截图/导出用）
  content_bg: '#hex'         # 内容区背景
  border: '#hex'             # 边框
  border_accent: '#hex'      # 强调边框
  text_primary: '#hex'       # 主文本
  text_secondary: rgba(...)  # 次文本
  text_muted: '#hex'         # 弱化文本
  accent: '#hex'             # 主色调
  accent_warm: '#hex'        # 互补色
  hover_bg: rgba(...)        # hover 背景
  selected_bg: rgba(...)     # 选中背景
  input_glow_preset: subtle  # 输入框聚焦发光风格：subtle / breath / platinum / ember
```

完整 token 列表（数十项，含骨架屏、工具条、时间轴、状态环等）以 `plugins/system-themes/themes/` 下 11 个内置主题为参考。缺 token 时渲染层用默认色兜底，不会报错，但主题会显得不统一。

颜色格式：`#rrggbb`、`#rgb`、`rgba(r, g, b, a)`。`rgb()` 不带 alpha 的写法不保证被接受，统一带 alpha 更稳。

## 使用

```bash
/theme amber
```

或在外观设置里点主题缩略图。默认主题写在 `~/.drifox/config.json` 的 `ui.theme`。

## 校验

- 目录名满足 `^[a-z][a-z0-9-]+$`
- 目录内至少一个 `*.yaml`
- 顶层必须有 `id`，且与目录名一致
- yaml 能被解析（字段语义由 DriFox 渲染层负责，lint 阶段不校验）

## 排障

| 现象 | 原因 | 修法 |
|------|------|------|
| 背景图不显示 | 路径写错或文件不在主题目录 | 用相对主题目录的路径；确认文件名大小写 |
| 视频层黑屏 | 用了 multiply 期望的素材但引擎行为不符 | 本项目已改用 alpha 遮罩压暗，素材用「黑底白纹」剪切即可 |
| 竖版视频只看到中段 | `position` 默认 `center` | 配 `position: top` 或 `bottom` |
| 视频出现在错误区域 | 没写 `target` 或写错值 | 显式写 `target`，非法值会静默回落 `chat_area` |
| 音乐不响 | 用了 `:qrc` 路径，或文件不存在 | 改文件路径；看日志里的 `[ThemeMusic]` warning |
| 切主题音乐重启 | 两个主题的 `tracks` 解析结果不同 | 想续播就指向同一批文件（可跨主题共用） |
| 音量滑块调了没反应 | 主题没配 `music` | 音量只作用于主题音乐 |

## 参考

- 内置主题：`plugins/system-themes/themes/`（11 套）
- 主题实现：`app/utils/theme_manager.py`（加载与归一化）、`app/widgets/scene_layer.py`（场景层）、`app/widgets/decoration_layer.py`（装饰件）、`app/widgets/video_overlay_layer.py`（视频层）、`app/widgets/theme_music_player.py`（音乐）
