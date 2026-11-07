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
| 「把壁纸做成主题」「迁移这个壁纸」 | 跑 `scripts/migrate.py <壁纸目录> --out <输出目录>` |
| 「我有哪些壁纸可以转」 | 跑 `scripts/migrate.py --list <workshop 根目录>` |
| 「先看看会生成什么」 | 加 `--dry-run` |

**第一动作永远是 `--dry-run`。** 先让用户看到类型识别、能取到什么素材、有哪些警告，确认后再真正落盘。

## 支持范围（先对号，再动手）

| 壁纸 type | 载体 | 处理方式 | 实测通过率 |
|---|---|---|---|
| `video` | 单个 mp4/webm | 用 `scripts/anim_encode.py` 转成动画 WebP（800×450@12fps），挂 `chat.animation` | 全部 |
| `scene` | `scene.pkg` 或 `gifscene.pkg` | 解 PKG 索引，抽主视觉图 + 内嵌音频 | 见下方限制 |
| `web` | 内嵌浏览器 | **不支持**，DriFox 主题无 Web 载体 | — |

实测 23 个真实壁纸：20 个成功（8 video / 12 scene），3 个失败，失败原因全部是「贴图只有 DXT 压缩，没有现成 PNG/JPEG」。这个限制写在 `references/format-notes.md`。

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

参数建议：`--width 800 --fps 12`（默认值，640 档整窗背景实测被嫌糊），`--duration 6~10` 截一段干净的循环，`--blur 0.4~0.8` 轻度预烘模糊（高模糊会把画面糊掉，实测 0.4 佳）。产物 0.2~0.5MB/5s，比原 mp4 小。

yaml 里对应写法：

```yaml
backgrounds:
  chat:
    animation:
      src: bg/chat.webp
      canvas: shared
      fps: 12
      fit: cover
```

需要「光影压暗」而非直接铺画面时，用 `--mode mask`（黑底 + 反相亮度 alpha），并把 `blend` 写成 `overlay`。

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

- **DXT 压缩贴图**（`TEXB` 头部声明 `isLZ4=1` 且载荷不是完整 PNG/JPEG）无法提取。这类壁纸的 `scene.pkg` 里所有贴图都是压缩原始像素，脚本会明确报错而不是产出空主题。批量测试里 3/23 属于这类。
- **PKG 内的音频**只在载荷是标准 `ID3`/`FF FB` 头时提取；被压缩的音轨会被跳过。
- **动态效果有限还原**：video 类型能整段转成动画 WebP 保住画面运动；scene 类型的 shader、粒子、视差滚动没有对应能力，迁移结果是静态背景图（加可选角色剪影）。

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
