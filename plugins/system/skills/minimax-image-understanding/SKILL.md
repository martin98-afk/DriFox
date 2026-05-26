---
name: minimax-image-understanding
description: "跨平台截图分析工具，基于 MiniMax 多模态 API。支持 macOS (screencapture) 和 Windows (PowerShell) 截图，自动完成截图→Base64编码→API调用全流程。适用于：错误信息分析、代码解读、UI设计分析、文字提取、图表数据解读等场景。"
---

# MiniMax 图像理解技能

支持 macOS 和 Windows 的跨平台截图分析工具。

## 功能特性

1. **一键截图+分析** - 自动完成截图、编码、API调用全流程
2. **macOS 优先支持** - 使用原生 `screencapture` 命令
3. **Windows 支持** - 使用 PowerShell 截图
4. **4K屏幕支持** - 自动检测并截取完整画面
5. **图片解读** - 使用 MiniMax 多模态 AI 分析图片

## 快速开始

### 一键启动器（最简单）

```bash
cd /Users/dingma/work/DriFox/DriFox/app/skills/minimax-image-understanding/scripts
python launcher.py
```

启动器会自动：
1. 检测 Python 环境
2. 检查 API Key 配置
3. 测试截图功能
4. 执行截图分析

### 命令行使用

```bash
# 截图并分析
python capture_and_analyze.py

# 仅分析已有图片
python capture_and_analyze.py --no-screenshot -f myimage.png

# 自定义提示词
python capture_and_analyze.py -p "请分析这个错误信息"

# 仅截图
python take_screenshot.py screenshot.png

# 仅分析
python analyze_image.py --file screenshot.png
```

## API Key 配置

### 方式一：环境变量（推荐）

```bash
# macOS/Linux
export MINIMAX_API_KEY=sk-cp-xxxxxxxxxxxx

# Windows
set MINIMAX_API_KEY=sk-cp-xxxxxxxxxxxx
```

### 方式二：配置文件

```bash
# 创建配置文件
mkdir -p ~/.minimax
echo "sk-cp-xxxxxxxxxxxx" > ~/.minimax/api_key
```

## API 配置（已内置）

```python
api_host = "api.minimax.chat"
endpoint = "/v1/coding_plan/vlm"
```

## 工作流程

```
┌─────────────────────────────────────────┐
│  capture_and_analyze.py (一键入口)      │
└────────────────┬────────────────────────┘
                 │
         ┌───────┴───────┐
         ▼               ▼
    [macOS]          [Windows]
 screencapture    PowerShell截图
         │               │
         └───────┬───────┘
                 ▼
    ┌─────────────────────────┐
    │  Base64 编码 + API 调用  │
    │  prompt + image_url      │
    └────────────┬────────────┘
                 ▼
    ┌─────────────────────────┐
    │  MiniMax 视觉理解 API    │
    └────────────┬────────────┘
                 ▼
         返回分析结果
```

## 常见用法

| 场景 | 命令 |
|------|------|
| 分析报错 | `python capture_and_analyze.py -p "请分析这个截图中的错误信息"` |
| 代码解读 | `python capture_and_analyze.py -p "请描述截图中显示的代码内容"` |
| UI分析 | `python capture_and_analyze.py -p "分析这个界面的布局和设计"` |
| 文字提取 | `python capture_and_analyze.py -p "提取图片中的所有文字内容"` |
| 图表分析 | `python capture_and_analyze.py -p "描述这个图表显示的数据和趋势"` |

## 文件结构

```
minimax-image-understanding/
├── SKILL.md                      # 技能定义文件
├── scripts/
│   ├── launcher.py                # 一键启动器
│   ├── capture_and_analyze.py     # 一键截图+分析入口
│   ├── take_screenshot.py         # 独立截图脚本
│   ├── analyze_image.py          # 独立分析脚本
│   ├── requirements.txt           # Python依赖（可选）
│   └── common/
│       ├── __init__.py
│       └── utils.py               # 通用工具（截图、API Key、Python探测）
```

## 技术细节

### 跨平台支持

| 平台 | 截图方式 | 命令 |
|------|---------|------|
| macOS | screencapture | `screencapture -x output.png` |
| Windows | PowerShell | `System.Drawing.Graphics.CopyFromScreen` |

### API 调用格式

```python
payload = {
    "prompt": "请详细描述这张图片的内容",
    "image_url": "data:image/png;base64,<base64数据>"
}

url = "https://api.minimax.chat/v1/coding_plan/vlm"
```

### 响应格式

```json
{
  "base_resp": {
    "status_code": 0,
    "status_msg": "success"
  },
  "content": "图片分析结果..."
}
```

## 错误处理

| 错误 | 说明 | 解决方案 |
|------|------|---------|
| 截图权限 | macOS 未授权屏幕录制 | 系统设置 → 隐私与安全性 → 屏幕录制 |
| API Key 无效 | 401/403 错误 | 检查 MINIMAX_API_KEY 是否正确 |
| 模型不支持 | 2013 错误 | 确认使用 /v1/coding_plan/vlm 端点 |
| 网络超时 | 连接失败 | 检查网络连接或增加超时时间 |

## macOS 截图权限设置

如果遇到截图权限问题：

1. 打开 **系统设置** → **隐私与安全性** → **屏幕录制**
2. 找到并启用你的终端应用或 Python
3. 如果列表中没有，点击 **+** 添加

或者使用命令授权：
```bash
tccutil reset ScreenCapture
```
然后重新打开应用触发权限请求。
