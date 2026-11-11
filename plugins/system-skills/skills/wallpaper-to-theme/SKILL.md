---
name: wallpaper-to-theme
description: 把 Wallpaper Engine 创意工坊壁纸一键转成 DriFox 主题插件（video 直取视频、scene 解包取背景图与内嵌音乐），产出可直接安装的 themes 插件目录。当用户说「把壁纸做成主题」「迁移 wallpaper 壁纸」「创意工坊壁纸转 DriFox 主题」时使用。
license: GPL-3.0-or-later
---

# Wallpaper Engine → DriFox 主题插件

把 `steamapps/workshop/content/431960/<id>/` 下的壁纸目录，转成可安装的 DriFox 主题插件。

## 触发与第一动作

| 你说 | 动作 |
|------|------|
| 「把壁纸做成主题」「迁移这个壁纸」 | 跑 `scripts/migrate.py <壁纸目录> --out <输出目录>`（scene 建议改用 capture_scene.py 全保真渲染，见下） |
| 「我想要 XX 风格的主题」 | `scripts/search_wallpaper.py "XX"` 搜创意工坊，挑中订阅/下载后再迁移 |
| 「我有哪些壁纸可以转」 | 跑 `scripts/migrate.py --list <workshop 根目录>` |
| 「先看看会生成什么」 | 加 `--dry-run` |

**代理**：steamcommunity.com 直连不稳，搜索需设环境变量 `DRIFOX_STEAM_PROXY`（如本机 v2rayN http://127.0.0.1:10808，182.183 注意 18080 无服务； browse 页是 React 应用，数据嵌在转义 JSON 里，解析已内置）。

**第一动作永远是 `--dry-run`。** 先让用户看到类型识别、能取到什么素材、有哪些警告，确认后再真正落盘。

## 支持范围（先对号，再动手）

| 壁纸 type | 载体 | 处理方式 | 实测通过率 |
|---|---|---|---|
| `video` | 单个 mp4/webm | `anim_encode.py` 完整转动画 WebP（1600×900@15fps，超长片自动降 1280），音轨抽 mp3 | 全部 |
| `scene` | `scene.pkg` | **全保真渲染**：`capture_scene.py` 让 WE 原生渲染并录屏（shader/粒子/相机全保留），自动关闭日期时钟类属性 | 15/19 |
| `web` | 内嵌浏览器 | **不支持**，DriFox 主题无 Web 载体 | — |

实测 37 个真实壁纸（2026-11-08）：33 个产出动画素材（video 15/16 全成，scene 15/19，4 个剔除：gifscene 时钟无属性开关、媒体播放器类、日期文字层关不掉）。旧的「解 PKG 抽静态图」保留为无 WE 环境时的降级路径——静态图丢掉全部动态。

## 一步到位流程（按序执行，每步已验证）

0. **本地没有合适的？先搜**：`search_wallpaper.py "关键词" --sort week`（走 DRIFOX_STEAM_PROXY 代理；browse 页数据嵌在转义 JSON 里，解析已内置，含文件大小可提前避坑超大壁纸）。挑中后让用户在 Steam 里订阅，等 `workshop/content/431960/<id>/` 出现即接步骤 2。

1. **侦察**：`migrate.py --list <根>` 列全量；video 用 ffprobe 看时长与音轨；scene 解 pkg 看 objects 数量与 shader/粒子占比（>100 对象的复杂 scene 加载慢，settle 要加长）。
2. **video**：`anim_encode.py` 完整转 1600×900@15fps，不截断；≤60s 用 q72，更长按内置档位（120s→q66、200s→q60、更长→q56），产物 >40MB 自动降 1280×720 q72。音轨 `ffmpeg -vn -c:a libmp3lame -b:a 192k` 抽 mp3。
3. **scene**：`capture_scene.py <pkg> <out.mp4> --duration 30`（自动解包关日期属性、等首帧、置顶去最小化）。需要 WE 正在运行 + `pip install windows-capture`。做完 `-control unmute` 恢复音量。
4. **放弃名单（不要硬做）**：gifscene 时钟、媒体播放器类壁纸、日期是 script 文字层关不掉的、带播放进度 UI 的——录出来会永远显示录制时刻的时间/进度。
5. **组装**：取色（多帧采样→明暗判定→accent 从中间调高饱和簇取）→ 97 token 全集派生 → yaml。**hex 值必须带单引号**（`accent: '#ff2e63'`，不带引号会被 YAML 当注释，40 个 token 静默变 None）。`user_card_bg` / `assistant_card_bg` 必写。
6. **音频入库**：ffprobe 查时长，>240s 截断，flac/wav/超 12MB 转 192k mp3。
7. **校验三件套**：全部 colors token 非 None 且 ≥90 键；`src`/`tracks` 相对路径可解析到真实文件；icon 目检（webp 首帧居中裁方）。
8. **既有插件升级**：先 git 保护原 yaml，只替换 backgrounds 段加 animation，colors 原样保留；禁止批量脚本直接覆盖。

## 产出结构

```
<out>/
├── .drifox-plugin/plugin.json    manifest（components.themes = true）
├── assets/                       共用素材（主图 / 装饰件 / 音频）
├── themes/<theme-id>/<theme-id>.yaml
├── icon.png / icon_dark.png      主图裁方形
```

主题 yaml 会自动决定用哪几块 `backgrounds`：

- **有视频** → `chat.animation.src`（先用 `scripts/anim_encode.py` 转码，见下节）。**不要写 `overlay.video`**：Qt5 下依赖系统解码器，部分机器直接播不了，已停用
- **有主图** → `window.image` + `dim` 蒙版（整窗铺底），`sidebar` 半透明罩保左侧文字可读，`scene` 纯透明让窗口层透出。**不要把主图只挂在右侧对话区**（参考内置 true-black 的分层写法）
- **有透明前景图** → `scene.foreground`（`{image, opacity, enabled}`，与 scene.image 同矩形同拉伸，逐像素对齐）

> 前景图必须用 `scene.foreground`，不要用 `decorations`：decorations 的 reference 是对话滚动区且等比 fit，与 scene 的拉伸铺满不同，同一张壁纸拆出的前景层会与背景错位。
- **有音频** → `music.tracks` 列表

### 视频素材转码

Wallpaper Engine 的 mp4 不能直接给客户端播（Qt5 的 QMediaPlayer 依赖系统解码器，同一份素材在不同机器上能不能播取决于系统装了什么）。先转成动画 WebP：

```bash
python scripts/anim_encode.py <壁纸目录>/xxx.mp4 <out>/themes/<id>/bg/chat.webp --blur 0.4
```

整窗背景的推荐档位（2026-11-08 用户确认）：**1600×900@15fps 不截断完整转**（用户要求「完整播放一轮」）；时长 ≤60s 用 q72，更长的按 `anim_encode` 内建档位自动降质；产物 >40MB 时自动降 1280×720 换回画质。实测单帧解码成本（PyQt5 QImageReader）：1600×900=8.14ms、1280×720=5.33ms、800×450=2.52ms；宿主 fps 硬上限 15（`theme_manager._ANIM_FPS_CAP`），解码是逐帧流式的，时长只影响体积不影响内存。

视频里的音轨一并抽出（`ffmpeg -vn -c:a libmp3lame -b:a 192k`），挂 `music.tracks`。

需要「光影压暗」而非直接铺画面时，用 `--mode mask`（黑底 + 反相亮度 alpha），并把 `blend` 写成 `overlay`。

### scene 全保真渲染（首选路径）

scene 是多层贴图 + shader + 粒子 + 相机动画的实时合成，解 pkg 抽静态图会丢掉全部动态。用 WE 本体渲染再抓帧：

```bash
python scripts/capture_scene.py <壁纸目录>/scene.pkg <out>.mp4 --duration 30
```

原理与已验证的坑（全部实测踩过）：

1. `wallpaper64.exe -control openWallpaper -file <pkg> -playInWindow "DriFoxCap" -width 1920 -height 1080`（width/height 指定的是**客户区**尺寸，窗口被 WE 居中放置）。
2. **抓帧必须用 Windows Graphics Capture 直抓 HWND**（`pip install windows-capture`）。三条路里只有它能抗遮挡：gdigrab 走 BitBlt 抓 D3D 窗口是纯黑屏；ddagrab 抓全屏再裁窗口区域，一旦用户最小化窗口就录到桌面内容。
3. WGC 的帧含标题栏，裁剪偏移**不能用 GetWindowRect**（它含 DWM 阴影，实测差 12px），要用帧尺寸自校准：`dx=(frame_w-cw)//2`，`dy=frame_h-ch-dx`。
4. 录制前把 `-playInWindow` 窗口去掉 WS_MINIMIZEBOX（防误触最小化中断），WGC 靠「2.5s 无新帧」判 stall；WE 加载期间不重绘，要先等首帧（最多 12s）再开始计时。
5. WE 桌面壁纸音量会被 `-control mute` 波及，做完记得 `-control unmute`。

**日期/时钟去除**：很多 scene 自带动态日期、时钟、歌名层，录进主题会永远显示「录制那一刻」的时间。去除方式是**解包 scene.pkg 成项目目录**（WE 支持直接加载 project.json），把 `project.json → general.properties` 里日期类属性默认值改掉：bool 型设 `false`，slider 型设 `0`（属性名按 date/time/clock/hour/week/media/spectrum 关键词匹配）。capture_scene.py 已内置该流程。没有属性开关的（gifscene 的时钟是硬编码、部分壁纸日期是 script 驱动的文字层）→ 停止并告知用户，不要硬抠 scene.json。

**scene 没有循环周期**：project.json/scene.json 不声明任何时长字段，粒子是随机的，动画是无限程序化的。「完整播放一轮」只对 video 类型成立；scene 固定采样 30s 即可，加长只增加体积。

### 配色必须适配背景

`colors` 从主视觉图取色派生（`derive_colors()`：accent 取画面中间调高饱和主色，底色系从暗部压暗，文本色从亮部提亮），**禁止固定色板**。迁移后目检 yaml 的 accent 是否与画面同调——森林图出绿褐系、霓虹城出青紫系才算对。video 类型无帧可取时才允许兜底色板。

浅色模式（mode: light）配暗调主图时的实测经验：图必须淡化处理（`opacity: 0.55` + `blur: 6` + 浓白蒙版 `0.5`）做出雾感，卡片 alpha 提到 246+ 减少透色，否则暗部透出来整体发灰发脏。

**必写 token**：欢迎大卡与用户卡的背景不读 `card_bg`，走 `assistant_card_bg` / `user_card_bg`（经 `design_tokens.Colors.refresh()` 1:1 映射）；缺省时回落硬编码 `rgba(45,30,20,150)` 深棕 / `rgba(27,42,67,150)` 深蓝，浅色主题下会整块发深。迁移产出的 colors 必须包含这两个键，取值与 `card_bg` 同族。sidebar 需要与对话区拉开：半透明罩（alpha 140 左右）透出窗背景，比对话区略提亮即可，实色罩会显得死板。

装饰件判定：在非主图贴图里找「RGBA 且透明像素 ≥ 25%、宽度 ≥ 800」的那张。这是角色立绘/前景元素的典型特征。

## 硬停止

1. 壁纸是 `web` 类型 → 直接告知不支持，不要造 workaround
2. `--dry-run` 报「没有可直接取用的图片」→ 告知是 DXT 压缩贴图，当前脚本未实现该解码路径，**不要**手工糊一个纯色主题冒充
3. PKG 解析报「条目偏移不连续」→ 该 PKG 版本结构不同，停止并报告，不要猜字段位置
4. 输出目录已存在同名主题 → 先问用户是覆盖还是换目录

## 已知限制

- **无 WE 运行环境的机器**：全保真渲染需要 WE 正在运行 + windows-capture。降级路径是解 PKG 抽静态图（migrate.py），但 scene 会丢掉全部动态。
- **DXT 压缩贴图**（`TEXB` 头部声明 `isLZ4=1` 且载荷不是完整 PNG/JPEG）无法提取，只影响降级路径；全保真渲染不受影响。
- **PKG 内的音频**只在载荷是标准 `ID3`/`FF FB` 头时提取；被压缩的音轨会被跳过。入库前统一 ffprobe：>240s 截断，flac/wav/超大文件转 192k mp3（实测过单壁纸内嵌 137MB 完整专辑）。
- **动态日期/时钟/媒体层关不掉的壁纸**：gifscene（时钟硬编码无属性）、日期是 script 驱动文字层（如 Crimson Empress）、壁纸本体就是媒体播放器（Media Player | Seyul）→ 直接放弃，做成主题会永远显示录制时刻的时间/进度。
- **hex 色值必须带引号写进 yaml**：`accent: #ff2e63` 会被 YAML 当注释解析成 None，40 个 token 静默变空，切主题后大面积残留默认配色（深色主题黑字）。必须写 `accent: '#ff2e63'`。
- **既有插件升级不要用批量脚本直接覆盖**：会冲掉手调 colors 与既有音频命名。正确做法：git checkout 恢复被覆盖文件，只替换 backgrounds 段加 animation，colors 原样保留。

## 验证

```bash
# 1. 列出可用壁纸
python scripts/migrate.py --list "<workshop 根目录>"

# 2. 单张 dry-run
python scripts/migrate.py "<壁纸目录>" --out "<输出目录>" --dry-run

# 3. 真正生成
python scripts/migrate.py "<壁纸目录>" --out "<输出目录>"

# 4. 装到本地验证主题能被加载
#    把输出目录整体复制到 ~/.drifox/plugins/ 下，重启 DriFox，
#    在「设置 → 外观」切到对应深浅模式后应能看到新主题
```

生成后必须实机验证三件事：主题卡里能看到、背景图正常铺满、音乐能播（有音频时）。

真实输入输出见 `references/examples.md`（含 video 与 scene 两种类型的完整产出清单，以及失败样本的报错原文）。

## 与 plugin-creator 的关系

- 本技能只负责「从壁纸产出主题插件」这一条路径，产出的插件仍受 `plugin-creator` 的 themes 规范约束。
- 需要手改产物（调色、换锚点、加动态视频层）时，转 `plugin-creator/references/components/themes.md`。
- 需要发布到插件市场，转 `plugin-creator/references/publishing.md`。

## 素材版权

创意工坊壁纸的图/音/视频版权归原作者，商业作品还涉及发行商（如 CD Projekt RED 的 OST）。迁移产物**仅供本地个人使用**；要公开发布必须替换成自有或明确可商用素材。脚本产出的 `plugin.json` 默认 `license: MIT`，公开发布前必须改。

## 闭环

每次实际迁移后必须回写两件事：

1. **新踩的格式坑** → 补进 `references/format-notes.md`，写清「哪个壁纸 id + 什么现象 + 排查到哪一步」。实测数据是这份文档唯一的可信来源，不写就退化成猜测。
2. **失败样本** → 在 SKILL.md「已知限制」里更新通过率，不要维持旧的乐观数字。

本次实测基线：23 个真实壁纸，20 成功（8 video / 12 scene），3 失败——全部是 DXT 压缩贴图，已在 `references/format-notes.md` 记录卡点。
