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
  chat:        {...}   # 右侧整个圆角矩形（新名，等价旧 scene，推荐）
  scene:       {...}   # 旧名字，仍可用；与 chat 同时存在时 chat 逐字段覆盖
  decorations: [...]   # 装饰件数组
  overlay:     {...}   # 旧动态视频层（Qt5 已停用，改用 animation）
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
| `animation` | object | 无 | 动画背景（动画 WebP / GIF），见下节 |
| `enabled` | bool | `true` | 关闭后忽略该区域 |

`scene` 撑满整个右侧圆角矩形，覆盖 tab bar / 对话区 / 输入框 / 各方向停靠区 / UI 插件槽位，是主题主背景的首选落点。`chat_area` 是更内层的老区域，仅在需要兼容旧主题时用。

### animation（动画背景）

三个区域（`window` / `sidebar` / `chat`）都能各自配动态背景，互不影响：

```yaml
backgrounds:
  chat:
    animation:
      src: bg/chat.webp
      canvas: shared      # 与其它区域共用同一张动画
      fps: 12
      blend: overlay
      fit: cover
      position: center
      opacity: 0.6
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `src` | string | 必填 | 动画素材路径（动画 WebP 优先，也兼容 GIF） |
| `canvas` | string | `independent` | `shared` = 多区域共用同一张动画，各取自己的矩形，接缝连续且只解码一次 |
| `fps` | int | `12` | 播放帧率，上限 15（超过自动压到 15） |
| `blend` | string | `normal` | `overlay` = 光影压暗（素材需预烘成黑底+反相亮度 alpha） |
| `fit` | string | `cover` | `cover` / `contain` / `tile` |
| `position` | string | `center` | cover 裁剪的垂直对齐：`top` / `center` / `bottom` |
| `opacity` | float | `1.0` | 0.0~1.0 |
| `enabled` | bool | `true` | 关闭后不加载 |

**为什么不用视频**：Qt5 的 `QMediaPlayer` 只是壳，解码交给系统媒体栈（Windows 的 DirectShow/Media Foundation、Linux 的 GStreamer、macOS 的 AVFoundation）。同一份 mp4 在不同机器上能不能播取决于那台机器装了什么解码器，实测 MediaFoundation 注册缺失时报 `InvalidMedia` 且 `errorString` 为空。动画 WebP 由 Qt 内置 qwebp 插件直接解码，三平台行为一致，零外部依赖。

**素材规格**（客户端成本随像素量线性上升）：

| 规格 | 单帧解码 | 建议 |
|------|----------|------|
| 640×360 @12fps | 2.6~3.2ms | 推荐；配 8~12px 模糊，光影/雾/流体类无损 |
| 800×450 @12fps | ~5ms | 实拍素材要留细节时用 |
| 1280×720 @12fps | 8.8~9.5ms | 不推荐，软渲染机器会明显吃 CPU |

5 秒 / 640×360 / q55 约 0.2~0.5MB。素材分辨率在转码时降下来，客户端不做实时缩放。

**转码**（制作端一次性，客户端不需要 ffmpeg）：

```bash
# 普通动态背景
python scripts/anim_encode.py in.mp4 bg/chat.webp --blur 0.8

# 光影遮罩（黑底 + 反相亮度 alpha，复刻旧 overlay 的效果）
python scripts/anim_encode.py in.mp4 bg/leaves.webp --mode mask --blur 1.0
```

脚本在 `wallpaper-to-theme/scripts/anim_encode.py`。

**降级链**：动画素材缺失或解码失败 → 同区域的 `image` 静态图 → `color` 底色 → 透明。不会白屏，也不会反复重试。

**性能行为**：全局只有一个时钟，多区域同时动时共用；窗口最小化或区域隐藏时自动停表，不烧 CPU。

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
| `fit_width` | bool | `false` | 宽度超出锚定容器时等比缩小（只缩不放） |
| `fit_height` | bool | `false` | 高度超出锚定容器时等比缩小（只缩不放） |

`anchor` 取值：`scene`（撑满对话区）、`scene_top`、`scene_bottom`、`input_top`、`input_bottom`。

**`fit_width` / `fit_height` 必须给大图开**：不写时按图片**原尺寸**摆放（历史行为）。把一张 1600px 宽的立绘挂到 700px 宽的对话区上，会左右各溢出一半，用户侧表现为「人物跑出屏幕」。开了 `fit_width: true` 后按容器宽度等比缩放并保持锚点对齐，窗口变窄时会重新计算。

两者可同时开，取更严格的缩放比例。原图比容器小时不会放大（避免糊）。

### overlay（旧动态光影视频层，已停用）

> **Qt5 下不再播放**：QMediaPlayer 依赖系统解码器，同一素材在部分机器上无法播放。动态背景改用上面的 `animation`（动画 WebP）。`overlay` 字段保留仅为兼容存量主题与 Qt6（QtMultimedia 自带 ffmpeg 后端）复活用；Qt5 读到 `video` 会记一条 info 后跳过，不影响静态背景。

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
- **外观页的「主题音乐」卡只在当前主题有可用音乐时出现**：没配 `music`、`enabled: false` 或曲目文件全部无效时整卡隐藏，不会留一条调不出声的音量条。
- 卡片展开是当前主题歌单，逐曲可开关。关掉的曲目按主题记在 `<app_data>/plugin_data/<插件名>/theme_music.json`（跟着插件数据走，不占主程序配置）；关掉正在播放的那首会立即跳到下一首启用曲，全部关掉则暂停但保留列表，重新打开可立即恢复。

> **`tracks` 不支持 `:qrc`**。QtMultimedia 的后端（Windows 上是 DirectShow / WMF）读不到 Qt 资源系统，`:qrc` 路径的音频会直接报 ResourceError。音乐只支持文件路径。

> 配了 `music` 或 `overlay.video` 的主题，在设置 → 外观 → 主题风格的主题格左上角会带对应角标（场记板 = 动态视频层，音符 = 背景音乐），用来一眼分辨哪些主题不是静态配色。

## 资源路径三种写法

| 写法 | 示例 | 解析规则 |
|------|------|---------|
| 主题目录内相对路径 | `bg.png` / `leaves-overlay.mp4` | 拼到当前主题目录下 |
| 跨主题/跨目录相对路径 | `../shared/rain.mp3` | 拼到主题目录后规范化，`..` 会被解析 |
| 绝对路径 | `D:/assets/piano.ogg` | 原样使用 |
| qrc 路径 | `:/icons/fox_bg.png` | 仅图片与视频可用；**音乐不支持** |

文件不存在时：图片/视频静默跳过（不显示该层），音乐记 warning 并跳过该曲。不会因此阻止主题加载。

`:qrc` 路径只能引用打包进程序的资源，插件主题通常用不到，自己的素材一律放主题目录里。

### 多主题共用素材：放插件根 `assets/`

插件内有多个主题要共用同一批图/音频时，把素材放插件根的 `assets/`，主题 yaml 用 `../../assets/xxx` 引用：

```
<plugin>/
├── assets/                     ← 共用素材（不是主题目录）
│   ├── bg.jpg
│   └── 01-track.mp3
└── themes/
    ├── dark/dark.yaml          → image: ../../assets/bg.jpg
    └── light/light.yaml        → image: ../../assets/bg.jpg
```

**不要把共用素材放 `themes/shared/`**：`themes/` 下每个子目录都会被当成主题候选，校验器会报「shared 目录下没有 .yaml 文件」直接判失败。`assets/` 在 `themes/` 之外，不会被扫描。

单主题独用的素材放自己的主题目录里即可，路径写 `bg.jpg`。

> 已知限制：主题预览卡（设置-外观里的缩略图）只会取 `window → scene → chat_area → sidebar` 里第一个有图区域，并且按 `basename` 在**主题目录内**找文件。素材放在 `assets/` 的跨目录主题，缩略图会显示不出来（界面本身正常）。想有缩略图就把主背景图放回主题目录内。

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
| 装饰件跑出可视区 | 大图没开 `fit_width` / `fit_height`，按原尺寸溢出容器 | 加 `fit_width: true`（竖图再叠 `fit_height: true`） |
| 主题缩略图是空的 | 主背景图放到了 `assets/`，缩略图只在主题目录内找文件 | 把主背景图放回主题目录，或用主题目录内的图作 `scene.image` |
| 主题卡里找不到自己写的主题 | 主题 `mode` 与当前深浅模式不符（深色主题只在深色模式下列出） | 切到对应深浅模式；或把 `mode` 改成你要的模式 |
| 校验报「shared 目录下没有 .yaml」 | 共用素材放在了 `themes/shared/` | 移到插件根 `assets/`，yaml 里改 `../../assets/...` |
| 视频层黑屏 | 用了 multiply 期望的素材但引擎行为不符 | 本项目已改用 alpha 遮罩压暗，素材用「黑底白纹」剪切即可 |
| 竖版视频只看到中段 | `position` 默认 `center` | 配 `position: top` 或 `bottom` |
| 视频出现在错误区域 | 没写 `target` 或写错值 | 显式写 `target`，非法值会静默回落 `chat_area` |
| 音乐不响 | 用了 `:qrc` 路径，或文件不存在 | 改文件路径；看日志里的 `[ThemeMusic]` warning |
| 切主题音乐重启 | 两个主题的 `tracks` 解析结果不同 | 想续播就指向同一批文件（可跨主题共用） |
| 音量滑块找不到 | 当前主题没配 `music`（无音乐时「主题音乐」卡整卡隐藏） | 属于预期；给主题加 `backgrounds.music` 后卡片出现 |
| 单曲开关关了还在放 | 曲目路径解析不一致（同一文件不同写法） | 开关按解析后的绝对路径比对，同一曲目在 yaml 里只写一次 |

## 参考

- 内置主题：`plugins/system-themes/themes/`（11 套）
- 主题实现：`app/utils/theme_manager.py`（加载与归一化）、`app/widgets/scene_layer.py`（场景层）、`app/widgets/decoration_layer.py`（装饰件）、`app/widgets/video_overlay_layer.py`（视频层）、`app/widgets/theme_music_player.py`（音乐播放与单曲过滤）、`app/utils/theme_music_store.py`（单曲开关存储）
