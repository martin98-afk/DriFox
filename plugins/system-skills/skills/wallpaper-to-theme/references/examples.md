# 迁移示例（实测输出）

以下是本机 23 个创意工坊壁纸的真实迁移结果，用来说明脚本在不同类型壁纸上的实际行为。

## video 类型：Ori DE Silent

```
$ python scripts/migrate.py "G:/Steam/steamapps/workshop/content/431960/837761056" --out ./ori-de-silent

标题    : Ori DE Silent
类型    : video
主题 id : ori-de-silent
视频    : Ori DE Silent.mp4
背景图  : -
装饰件  : -
音乐    : 0 首
已生成 5 个文件：
  assets/Ori DE Silent.mp4
  themes/ori-de-silent/ori-de-silent.yaml
  .drifox-plugin/plugin.json
  icon.png
  icon_dark.png
```

产出的 yaml 关键段：

```yaml
backgrounds:
  overlay:
    target: chat_area
    video: '../../assets/Ori DE Silent.mp4'
    opacity: 0.35
    position: center
    enabled: true
```

注意视频文件名带空格，脚本用单引号包住了路径。手改 yaml 时保持引号，否则 YAML 会解析失败。

## scene 类型：Song-So-Mi Cyberpunk-2077

```
$ python scripts/migrate.py "G:/Steam/steamapps/workshop/content/431960/3244942566" --out ./cyberpunk

标题    : Song-So-Mi Cyberpunk-2077
类型    : scene
主题 id : song-so-mi-cyberpunk-2077
视频    : -
背景图  : 已取得
装饰件  : 已取得
音乐    : 3 首
已生成 9 个文件：
  assets/scene.png            (2560×1163, 3.0MB)
  assets/decoration.png       (1600×726, 0.26MB, 86.6% 透明)
  assets/01-...-never-fad.mp3 (7.05MB)
  assets/02-...-the-reb.mp3   (8.81MB)
  assets/03-...-phantom-lib.mp3 (18.42MB)
  themes/song-so-mi-cyberpunk-2077/song-so-mi-cyberpunk-2077.yaml
  .drifox-plugin/plugin.json
  icon.png
  icon_dark.png
```

产出同时用上三块 backgrounds：

```yaml
backgrounds:
  scene:
    image: ../../assets/scene.png
    opacity: 1.0
    dim: rgba(8, 6, 12, 0.42)
    foreground:
      image: ../../assets/decoration.png
      opacity: 1.0
      enabled: true
  music:
    tracks:
      - ../../assets/01-...mp3
    volume: 0.45
    loop: true
    fade_ms: 420
```

## 失败样本：Wicked Grace [4k]

```
$ python scripts/migrate.py ".../3465281285" --out ./wicked --dry-run

标题    : Wicked Grace [4k]
类型    : scene
背景图  : -
装饰件  : -
音乐    : 0 首
错误    : scene.pkg 里没有可直接取用的图片（可能是 DXT 压缩贴图，当前未支持）

迁移中止：存在未解决的问题
```

这个壁纸的 73 张贴图全部是 DXT 压缩（`format=4/9/7`），没有内嵌 PNG/JPEG，脚本明确报错而不是产出一个空主题。

## 批量结果

| 结果 | 数量 | 说明 |
|---|---|---|
| 成功 | 20 | 8 个 video + 12 个 scene |
| 失败：DXT 贴图 | 3 | `Wicked Grace` / `STEAM SUMMER SALE 2023` / `..and sometimes I'm happy` |
| 失败：其他 | 0 | |

`gifscene.pkg` 变体（两个 Summer Sale 壁纸）在补上文件名兼容后可以正常读取索引，失败原因仍然是 DXT 贴图。

## 验证产物能否被识别

```python
from app.utils.theme_manager import theme_manager
theme_manager._ensure_loaded()
theme_manager.list_themes()                          # 应包含新主题 id
theme_manager.get_theme_backgrounds('<theme-id>')    # 各区块字段非空
```

实测两个产物均为：

```
ori-de-silent: loaded=True   overlay=True  scene=False  deco=0  music=0
song-so-mi-cyberpunk-2077: loaded=True   overlay=False scene=True   deco=1  music=3
```
