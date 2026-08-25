# 午夜极光主题 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增可选的深色内置主题“午夜极光”：右侧使用用户极光图，左侧使用可重复生成的竖向极光渐变图。

**Architecture:** 仅新增主题资源与标准库生成脚本，不修改主题管理器或窗口布局代码。主题通过现有 `backgrounds.window`、`backgrounds.sidebar`、`backgrounds.scene` 分区配置加载；`midnight_aurora` 不自动切换，不改变用户当前选择。

**Tech Stack:** Python 3.14+ 标准库（PNG 编码）、PyYAML、PyQt5（仅用于最终资源读取验证）、pytest。

## Global Constraints

- 主题 ID 固定为 `midnight_aurora`，显示名称固定为“午夜极光”，显式声明 `mode: dark`。
- 右侧图片固定放在 `plugins/system/themes/midnight_aurora/right_aurora.png`，不引用用户本机 Downloads 路径。
- 左侧图片固定放在 `plugins/system/themes/midnight_aurora/sidebar_aurora.png`，默认尺寸为 `480 × 1920`。
- 左侧生成脚本为 `scripts/generate_midnight_aurora_sidebar.py`，只使用 Python 标准库，不增加 Pillow 或其他运行时依赖。
- 左侧图片固定为深紫 `#190C3A` 到薰衣草 `#B99ADD` 的单色水平渐变，不叠加光带、波浪或其他色相。
- 右侧主题参数固定为 `opacity: 0.86`、`blur: 5`、`dim: rgba(4, 5, 18, 0.34)`。
- 不修改 `app/utils/theme_manager.py`、`app/widgets/tab_manager_window.py` 或其他应用代码。
- 不修改 `app/widgets/cards/floating/command_card.py` 的既有暂存修改。
- 右侧原始图片不裁切、不改写；压暗和模糊由主题配置完成。

---

## 文件地图

- Create: `plugins/system/themes/midnight_aurora/midnight_aurora.yaml` — 主题注册、区域背景和 UI 色板。
- Create: `plugins/system/themes/midnight_aurora/right_aurora.png` — 从用户提供的源图复制的主题资源。
- Create: `plugins/system/themes/midnight_aurora/sidebar_aurora.png` — 脚本生成的左侧背景。
- Create: `scripts/generate_midnight_aurora_sidebar.py` — 可重复生成左侧 PNG 的工具。
- Create: `tests/utils/test_midnight_aurora_sidebar.py` — 脚本输出格式和确定性测试。
- No modify: `app/**`、其他主题文件、用户配置。

---

### Task 1: 添加左侧极光图生成器与测试

**Files:**
- Create: `scripts/generate_midnight_aurora_sidebar.py`
- Create: `tests/utils/test_midnight_aurora_sidebar.py`

**Interfaces:**
- Produces: `generate_sidebar_png(output: Path, width: int = 480, height: int = 1920) -> None`。
- Produces: `main() -> int`，读取 `--output`、`--width`、`--height` 并写出 RGBA PNG。
- Test consumes: `subprocess.run([sys.executable, SCRIPT, "--output", output, "--width", "32", "--height", "64"], check=True)`。

- [ ] **Step 1: 更新生成器回归测试（先确认当前实现不满足单色要求）**

在 `tests/utils/test_midnight_aurora_sidebar.py` 写入：

```python
import hashlib
import struct
import subprocess
import sys
import zlib
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "generate_midnight_aurora_sidebar.py"


def read_png_rgba(path: Path) -> tuple[int, int, list[bytes]]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    assert (bit_depth, color_type) == (8, 6)
    offset = 8
    compressed = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        if kind == b"IDAT":
            compressed.extend(payload)
        offset += length + 12
    raw = zlib.decompress(compressed)
    stride = width * 4
    rows = []
    for row_index in range(height):
        start = row_index * (stride + 1)
        assert raw[start] == 0
        rows.append(raw[start + 1 : start + 1 + stride])
    return width, height, rows


def test_generate_sidebar_png_is_single_purple_gradient_and_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    command = [sys.executable, str(SCRIPT), "--output", str(first), "--width", "32", "--height", "64"]
    subprocess.run(command, check=True)
    subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(second), "--width", "32", "--height", "64"],
        check=True,
    )

    width, height, rows = read_png_rgba(first)
    second_width, second_height, second_rows = read_png_rgba(second)
    assert (width, height) == (32, 64)
    assert (second_width, second_height) == (32, 64)
    assert tuple(rows[0][0:3]) == (25, 12, 58)
    assert tuple(rows[0][-4:-1]) == (185, 154, 221)
    center_offset = (width // 2) * 4
    assert all(row[center_offset : center_offset + 3] == rows[0][center_offset : center_offset + 3] for row in rows)
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
```

- [ ] **Step 2: 运行测试确认先失败**

运行：

```bash
pytest tests/utils/test_midnight_aurora_sidebar.py -v
```

预期：在旧生成器上 FAIL，因为首尾颜色不是深紫到薰衣草，且中心像素会随高度变化。

- [ ] **Step 3: 实现标准库 PNG 生成器**

实现以下结构，颜色和数学参数写死，避免每次输出变化：

```python
from __future__ import annotations

import argparse
import binascii
import struct
import zlib
from pathlib import Path

Color = tuple[int, int, int]
GRADIENT_START = (25, 12, 58)
GRADIENT_END = (185, 154, 221)


def interpolate_color(position: float) -> Color:
    position = min(1.0, max(0.0, position))
    return tuple(
        int(start + (end - start) * position)
        for start, end in zip(GRADIENT_START, GRADIENT_END)
    )


def _clamp(value: float) -> int:
    return max(0, min(255, int(round(value))))


def generate_sidebar_png(output: Path, width: int = 480, height: int = 1920) -> None:
    if width < 2 or height < 2:
        raise ValueError("width and height must be at least 2")
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            horizontal = x / (width - 1)
            red, green, blue = interpolate_color(horizontal)
            edge = min(1.0, horizontal / 0.12, (1.0 - horizontal) / 0.12)
            edge = max(0.0, min(1.0, edge))
            alpha = _clamp(255 * (0.28 + 0.72 * edge))
            rows.extend((red, green, blue, alpha))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(build_png(width, height, bytes(rows)))


def build_png(width: int, height: int, pixels: bytes) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels, 9)) + chunk(b"IEND", b"")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成午夜极光左侧竖向背景图")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--height", type=int, default=1920)
    args = parser.parse_args()
    generate_sidebar_png(args.output, args.width, args.height)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

实际实现需保持 `generate_sidebar_png` 的公开签名与测试一致，并在导入时只定义常量和函数，不生成文件。

- [ ] **Step 4: 运行测试确认通过**

运行：

```bash
pytest tests/utils/test_midnight_aurora_sidebar.py -v
```

预期：PASS，输出为可解析的 RGBA PNG，且相同参数哈希一致。

- [ ] **Step 5: 做脚本静态检查**

运行：

```bash
ruff check scripts/generate_midnight_aurora_sidebar.py tests/utils/test_midnight_aurora_sidebar.py
ruff format --check scripts/generate_midnight_aurora_sidebar.py tests/utils/test_midnight_aurora_sidebar.py
```

- [ ] **Step 6: 提交生成器与测试**

```bash
git add scripts/generate_midnight_aurora_sidebar.py tests/utils/test_midnight_aurora_sidebar.py
git commit -m "feat: add midnight aurora sidebar generator"
```

---

### Task 2: 新增午夜极光主题资源

**Files:**
- Create: `plugins/system/themes/midnight_aurora/midnight_aurora.yaml`
- Create: `plugins/system/themes/midnight_aurora/right_aurora.png`
- Create: `plugins/system/themes/midnight_aurora/sidebar_aurora.png`

**Interfaces:**
- Theme `id` consumed by `ThemeManager.list_themes()`: `midnight_aurora`。
- `backgrounds.sidebar.image` consumed by `TabManagerWindow._resolve_theme_image()`: `sidebar_aurora.png`。
- `backgrounds.scene.image` consumed by `SceneLayer.apply_config()`: `right_aurora.png`。

- [ ] **Step 1: 复制右侧源图并创建主题目录**

运行：

```powershell
New-Item -ItemType Directory -Force -Path plugins/system/themes/midnight_aurora | Out-Null
Copy-Item -LiteralPath 'C:\Users\black\Downloads\v2-65cc56a83738b069c7eb28470e9b2561_r.png' -Destination 'plugins/system/themes/midnight_aurora/right_aurora.png' -Force
python scripts/generate_midnight_aurora_sidebar.py --output plugins/system/themes/midnight_aurora/sidebar_aurora.png
```

预期：两个 PNG 均存在；右侧文件保持源文件字节不变，左侧文件为 `480 × 1920` RGBA PNG。

- [ ] **Step 2: 创建完整主题 YAML**

以 `plugins/system/themes/midnight/midnight.yaml` 的完整 token 集合为模板，新建 `midnight_aurora.yaml`，替换头部和背景配置：

```yaml
name: 午夜极光
id: midnight_aurora
mode: dark
window:
  gradient_start: rgba(7, 10, 28, 255)
  gradient_end: rgba(20, 13, 43, 255)
backgrounds:
  window:
    color: rgba(7, 10, 28, 238)
    enabled: true
  sidebar:
    image: sidebar_aurora.png
    opacity: 0.96
    color: rgba(8, 11, 29, 235)
    enabled: true
  scene:
    image: right_aurora.png
    opacity: 0.86
    blur: 5
    dim: rgba(4, 5, 18, 0.34)
    enabled: true
```

保留午夜主题的完整 `colors` 字段，并按以下值调整主要 token；未列出的午夜 token 保持原值，避免引入无关的 UI 行为变化：

```yaml
  card_bg: rgba(14, 18, 38, 230)
  card_bg_solid: rgba(14, 18, 38, 250)
  content_bg: '#11172b'
  border: '#3b4771'
  border_accent: '#65ddc0'
  text_primary: '#f4f7ff'
  text_secondary: rgba(226, 235, 255, 0.72)
  text_muted: '#93a0bd'
  accent: '#65ddc0'
  accent_warm: '#f28ab8'
  hover_bg: rgba(101, 221, 192, 0.12)
  selected_bg: rgba(101, 221, 192, 0.32)
  capsule_bg: rgba(20, 24, 48, 180)
  capsule_border: rgba(70, 80, 126, 200)
  input_bg_start: rgba(12, 17, 36, 150)
  input_bg_end: rgba(21, 22, 50, 150)
  input_focus_bg_start: rgba(17, 23, 45, 220)
  input_focus_bg_end: rgba(27, 27, 58, 220)
  input_border: '#36436d'
  input_focus_border: '#8df2d5'
  realtime_accent: '#65ddc0'
  realtime_accent_warm: '#f28ab8'
  system_accent: '#65ddc0'
  send_btn_start: '#65ddc0'
  send_btn_end: '#f28ab8'
  send_btn_hover_start: '#8df2d5'
  send_btn_hover_end: '#f6a6ca'
```

保留 `input_glow_preset: "breath"` 及午夜主题中未列出的时间线、环形、分支和状态色 token；只改变上表列出的值。

- [ ] **Step 3: 检查 YAML 与资源引用**

运行：

```bash
python -c "from pathlib import Path; import yaml; p=Path('plugins/system/themes/midnight_aurora/midnight_aurora.yaml'); data=yaml.safe_load(p.read_text(encoding='utf-8')); assert data['id']=='midnight_aurora'; assert data['mode']=='dark'; assert data['backgrounds']['sidebar']['image']=='sidebar_aurora.png'; assert data['backgrounds']['scene']['image']=='right_aurora.png'; print(data['name'], 'ok')"
python -c "from pathlib import Path; from PyQt5.QtGui import QImage; root=Path('plugins/system/themes/midnight_aurora'); a=QImage(str(root/'sidebar_aurora.png')); b=QImage(str(root/'right_aurora.png')); assert not a.isNull() and a.width()==480 and a.height()==1920; assert not b.isNull(); print(a.width(), a.height(), b.width(), b.height())"
```

- [ ] **Step 4: 提交主题资源**

```bash
git add plugins/system/themes/midnight_aurora/midnight_aurora.yaml plugins/system/themes/midnight_aurora/right_aurora.png plugins/system/themes/midnight_aurora/sidebar_aurora.png
git commit -m "feat: add midnight aurora theme"
```

---

### Task 3: 完成主题回归与范围检查

**Files:**
- Test: `tests/utils/test_midnight_aurora_sidebar.py`
- Test: `plugins/system/themes/midnight_aurora/midnight_aurora.yaml` 及主题目录 PNG

**Interfaces:**
- Consumes: `ThemeManager.get_theme_backgrounds("midnight_aurora")` 返回 `window`、`sidebar`、`chat_area`、`scene`、`decorations` 五个键；本主题不设置 `chat_area`，该键按现有兼容逻辑为 `None`。
- Consumes: `TabManagerWindow._resolve_theme_image()` 将两个相对图片名解析为主题目录绝对路径。

- [ ] **Step 1: 运行新增测试**

```bash
pytest tests/utils/test_midnight_aurora_sidebar.py -v
```

- [ ] **Step 2: 运行主题管理器相关回归测试**

```bash
pytest tests/test_theme_manager_pet.py tests/test_theme_refresh_version.py tests/plugins/test_builtin_reloaders.py -q
```

- [ ] **Step 3: 运行项目规定的格式检查**

```bash
ruff check scripts/generate_midnight_aurora_sidebar.py tests/utils/test_midnight_aurora_sidebar.py
ruff format --check scripts/generate_midnight_aurora_sidebar.py tests/utils/test_midnight_aurora_sidebar.py
```

- [ ] **Step 4: 核对当前主题配置未被改变**

```bash
git diff --name-only
git diff --cached --name-only
```

预期：应用代码、现有主题文件、配置均不在本次新增文件列表中；`app/widgets/cards/floating/command_card.py` 只保留用户原有暂存状态，不被本任务提交覆盖。

- [ ] **Step 5: 完成最终提交与交付说明**

仅在上述检查通过后提交本次资源（若 Task 1、Task 2 已按步骤提交，则无需额外提交），然后报告新增主题 ID、资源路径、生成命令和测试结果。
