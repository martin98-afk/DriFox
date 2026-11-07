# Wallpaper Engine 文件格式笔记（实测）

所有结论来自本机 23 个真实创意工坊壁纸的实测，不是文档抄录。

## project.json

定位：`<workshop_id>/project.json`

关键字段：

| 字段 | 说明 |
|---|---|
| `type` | `video` / `scene` / `web`（大小写不统一，实测同时存在 `Video` 与 `video`） |
| `file` | 主载体文件名。video 类型是 mp4，scene 类型是 `scene.json` 或 `gifscene.json` |
| `title` | 显示名 |
| `preview` | 缩略图（`preview.jpg` / `preview.gif`） |

判类型时统一 `.lower()`。

## 壁纸目录构成

| type | 典型文件 |
|---|---|
| video | `project.json` + `<name>.mp4` + `preview.jpg` |
| scene | `project.json` + `scene.pkg` + `preview.gif` |
| scene（gif 变体） | `project.json` + `gifscene.pkg` + `preview.gif` + `shaders/` |

`gifscene.pkg` 与 `scene.pkg` 的索引结构完全一致，只是文件名不同，解析逻辑可以复用。

## PKG 索引结构（PKGV0018 ~ PKGV0023 实测一致）

```
offset 0   : u32   固定 8（不是版本号，含义未知）
offset 4   : char[8]  版本签名 "PKGV0018" .. "PKGV0023"
offset 12  : u32   条目数 count
offset 16  : count × {
    u32   nameLen
    char[nameLen]  name（UTF-8，无结尾 \0）
    u32   relOffset     ← 相对「数据区起点」，不是相对文件头
    u32   size
}
数据区起点 = 索引结束位置 = 16 + Σ(4 + nameLen + 8)
```

**踩过的坑**：`relOffset` 是相对数据区起点的。按绝对偏移读会得到乱码，或读出莫名其妙的字段值。校验方法：把所有条目按 `relOffset` 排序，应满足 `prev_end == next_offset` 且最后一条的 `end == 文件长度 - 数据区起点`。用这个断言能立刻判定解析是否正确。

实测 11 个 scene pkg（0018/0020/0021/0022/0023 五个版本）全部满足该断言。

## TEX 贴图

### 头部

```
"TEXV0005\0" "TEXI0001\0"
  i32 format, u32 flags, i32 texW, i32 texH, i32 imgW, i32 imgH, u32 unk
"TEXB0003\0" | "TEXB0004\0"
  ... mipmap 头部（字段语义随版本变化，见下）
  ... 载荷
```

`format` 取值实测：`0`（RGBA8888）、`4`（DXT5 类）、`8`（RG8）、`9`（R8，多用于 mask）。

### 载荷三种形态（按可靠性排序）

| 形态 | 特征 | 处理 |
|---|---|---|
| 完整 PNG | 载荷里有 `89 50 4E 47` 签名 | 从签名截取，PIL 直接解 |
| 完整 JPEG | 载荷里有 `FF D8 FF` 签名 | 从签名截取，PIL 直接解 |
| 原始像素 / DXT + LZ4 | 无图片签名，头里带 `isLZ4=1` 与解压后大小 | **未实现**，见下 |

**关键经验**：TEXB 内部字段布局不是稳定的。实测三个样本：

```
TEXB0003  mipCount=1, grid=13x4,  img=3962x1800, 0, 0, dataSize
TEXB0003  mipCount=2, -1, 1, img=7068x7650, isLZ4=1, ...
TEXB0004  mipCount=1, -1, 0, 5, w=2176, h=1280, isLZ4=1, dsize=2785280
TEXB0004  mipCount=1, 2, 0, 5, w=3840, h=2160, 0, 0
```

同样叫 `TEXB0004`，头部字段完全不同。**所以不要靠位置推断格式**。

可靠的策略是：不解析 mip 头，直接在 `TEXB` 之后扫 PNG/JPEG 签名。这个策略在 23 个真实壁纸里覆盖了 20 个。

### 未实现的 DXT 路径

需要的行为：`isLZ4=1` 时把载荷按 LZ4 解压成 `w*h` 字节原始像素，再按 DXT1/3/5 解码。

实测卡在 LZ4 这一步：`3465281285/materials/body.tex` 的载荷首 token `0x9a` 表示「9 字节 literal + offset 18724」，但当前位置输出只有 9 字节，标准 LZ4 block 的偏移不可能超过已输出长度。带 `uncompressed_size` 与不带都失败，也不是 LZ4 frame 格式（无 `04 22 4D 18` magic）。

推测是分块压缩（块大小 65536，载荷里出现 34 次 `00 00 01 00`），但缺少块长度表无法切分。要补齐需要：
1. 找到 WE 官方的 TEX 反编译实现（RePKG 的 C# 源码里应有）
2. 或对同一张贴图取两份不同压缩率的样本做差分

## 多贴图场景的「主图」歧义

### 2860240798（The last of us 最后生还者）实测

现象：用户看到的画面是 Ellie 弹吉他（1920x1080 层），脚本按「非 mask 贴图面积最大」选中 3456x2160 森林空景，产出背景与预期不符。

排查：PKG 126 条条目含 12 张可解码贴图（演职员表长图 477x5000、鸟粒子表 1603x616、多张候选背景），画面由多层 + 雨/鸟/树叶摇摆粒子经 shader 合成，合成结果无法从贴图清单推断。

结论：多贴图场景「面积最大」选图本质是猜，须对照 preview.gif 让用户确认，或导出全部候选让用户挑。单贴图场景（如 1998201410，13 条条目仅 1 张 tex）无歧义，可自动选。

### 1998201410 音频条目路径

`sounds/The Last Of us - Theme song.mp3`（4.5MB，ID3v4 头）正常提取。该壁纸仅此一条音轨，不存在漏提。

## 音频

`scene.pkg` 里的 `sounds/*.mp3` 是**明文**，以 `ID3` 开头即可直接取出，不需要解压。实测三首 Cyberpunk 2077 OST 全部可被 Qt 正常解码（287s / 231s / 483s）。

被压缩的音轨（非 `ID3` / `FF FB` 头）会被跳过，不产出坏文件。

## 图像尺寸与体积

- 主图常为 3840×2160 或 4096×2048，原样落盘单张 5~15MB
- 技能脚本默认缩到 2560 宽（背景）与 1600 宽（装饰件）
- 装饰件判定后如果透明像素 < 25%，说明它是又一张不透明背景图，不应当装饰件用
