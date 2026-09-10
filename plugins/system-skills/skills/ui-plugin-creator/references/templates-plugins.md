# UI 插件代码模板 — 插件级骨架（register_ui 入口 / plugin.json / _vendor/）

> 何时读：从零新建 UI 插件（缺 register_ui/plugin.json）、或插件需要 requests/PIL 等第三方包时。
> 前置依赖：workflow.md（四步流程）；_vendor 打包验证见 testing-vendor.md。
> 产出：完整可加载的 UI 插件骨架（含 sys.modules 热重载清理与 _vendor sys.path 注入）。

## 四、完整插件注册入口 + plugin.json

### 4.1 plugin.json

```json
{
    "name": "<plugin-name>",
    "description": "<插件描述>",
    "version": "0.1.0",
    "author": {
        "name": "DriFox Contributors"
    },
    "type": "system",
    "components": {
        "ui": true
    }
}
```

> 如果插件放在 `plugins/<plugin-name>/`（系统插件），`type` 可以省略。
> 用户插件放在 `~/.drifox/plugins/<plugin-name>/`。

### 4.2 ui/__init__.py 完整模板

```python
# -*- coding: utf-8 -*-
"""<plugin-name> UI 组件入口"""

import sys
from pathlib import Path

from loguru import logger


def register_ui(registry):
    """注册 <plugin-name> 的 UI 组件

    热重载兼容：
    清理 sys.modules 中残留的子模块缓存，确保 Python 重新从 .py 源文件编译，
    避免旧的 __pycache__/.pyc 导致 NameError 等异常。

    注意：不主动删除 __pycache__/ 目录，Python 的 import 系统已通过
    源文件时间戳自动判断是否需要重新编译 .pyc，主动删除只会触发不必要的
    文件系统变更，导致插件热更新监视器误判为跨插件修改。

    外部依赖（如有）：
    如果插件需要 PyInstaller 未声明的纯 Python 包，可将包目录复制到
    ui/_vendor/，本函数开头会自动加入 sys.path。详见 SKILL.md §4.7。
    """
    # ── 加载 _vendor/（如有） ──
    vendor_dir = Path(__file__).parent / "_vendor"
    if vendor_dir.exists() and str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))
        logger.info(f"[<plugin-name>] _vendor/ 已加入 sys.path: {vendor_dir}")

    # 清理旧子模块缓存（避免热重载时 Python 用旧 sys.modules 缓存）
    safe_name = "<plugin_name>"  # 连字符替换为下划线
    prefix = f"ui_plugin_{safe_name}."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .cards import MyCardWidget

    # 注册浮动卡片（自动注册对应命令 /<card-id>）
    # container="bottom"：与系统配置卡片一致，显示在 chat_layout 下方并隐藏输入区
    # container="full"：完整覆盖对话区（与系统配置卡片一致，走覆盖层）
    registry.register_floating_card(
        plugin_name="<plugin-name>",
        card_id="<card-id>",
        widget_class=MyCardWidget,
        container="bottom",          # "bottom" | "top" | "left" | "right" | "full"
        title="卡片标题",
        default_visible=False,
    )

    # 注册内容块渲染器（可选）
    # from .renderers import render_my_content
    # registry.register_content_renderer(...)

    logger.info("[<plugin-name>] UI components registered")
```

---

## 五、外部依赖管理（_vendor/ 模式）

### 5.1 适用场景

> **UI 插件打包后从市场下载使用，不能再次打包主程序。**

PyInstaller `--onedir` 打包后，`dist/Drifox/_internal/` 只包含构建期检测到的第三方包。如果某个 UI 插件需要构建时未声明的纯 Python 包（如 `requests`、`markdown`、`jsonschema`），运行时 `import` 会失败。

**解决方案**：将包目录完整复制到 `plugins/<plugin>/ui/_vendor/`，在 `register_ui` 开头加入 `sys.path`。

### 5.2 目录结构

```
plugins/<plugin-name>/
└── ui/
    ├── __init__.py           # 含 _vendor/ 加载逻辑
    ├── cards.py              # 可 import _vendor/ 中的包
    └── _vendor/              # 第三方纯 Python 依赖
        ├── requests/
        │   ├── __init__.py
        │   ├── api.py
        │   ├── models.py
        │   └── ...
        └── markdown/
            ├── __init__.py
            └── ...
```

### 5.3 复制依赖到 _vendor/

```bash
# 从开发环境复制（PowerShell）
$pkg = python -c "import requests, os; print(os.path.dirname(requests.__file__))"
Copy-Item -Path $pkg -Destination "plugins/my-plugin/ui/_vendor/requests" -Recurse

# Bash / Git Bash
python -c "import requests, os, shutil; shutil.copytree(os.path.dirname(requests.__file__), 'plugins/my-plugin/ui/_vendor/requests')"

# 复制后清理 __pycache__/（可选，运行时会自动生成）
Get-ChildItem plugins/my-plugin/ui/_vendor -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force
```

**注意事项**：
- ✅ **必须**：复制完整包目录（含所有子模块和 `__init__.py`）
- ✅ **建议**：删除 `__pycache__/` 和 `.pyc`（减小体积）
- ⚠️ **不能**：直接复制 `site-packages/<pkg>.dist-info/`（PyInstaller 不需要，但会污染插件）
- ⚠️ **限制**：仅限**纯 Python 包**或**目标平台 + Python 版本完全一致**的 C 扩展包

### 5.4 cards.py 中使用 _vendor/ 包

```python
# -*- coding: utf-8 -*-
"""<plugin-name> 浮动卡片"""

# 在 register_ui 已经把 _vendor/ 加入 sys.path 后，
# 这里直接 import 即可，运行时从 _vendor/ 加载
import requests
from markdown import markdown

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget


class MyCard(QWidget):
    def fetch_data(self):
        # 使用 _vendor/ 中的 requests
        resp = requests.get("https://example.com/api/data")
        resp.raise_for_status()
        return resp.json()

    def render_markdown(self, text: str) -> str:
        # 使用 _vendor/ 中的 markdown
        return markdown(text)
```

### 5.5 完整 register_ui 模板（含 _vendor/）

```python
# -*- coding: utf-8 -*-
"""<plugin-name> UI 组件入口"""

import sys
from pathlib import Path

from loguru import logger


def register_ui(registry):
    """注册 <plugin-name> 的 UI 组件

    外部依赖（vendored 到 ui/_vendor/）：
        - requests: HTTP 请求
        - markdown: Markdown 渲染

    _vendor/ 机制：
        把第三方纯 Python 包放在 ui/_vendor/，本函数开头将其加入 sys.path。
        打包后即便 PyInstaller 未声明该包，import 也能成功。
        仅限纯 Python 包；含 C 扩展的包不支持（需主程序构建期声明）。

    ⚠️ sys.modules 缓存陷阱（关键！）：
        Python 的 import 机制：如果模块已在 sys.modules 中，直接返回缓存，**完全忽略**
        sys.path 顺序。如果 DriFox 启动时（或另一个插件）提前 import 了 _vendor/ 里的
        同名包（如 darkdetect），register_ui 中 sys.path.insert(0, vendor_dir) 不生效，
        cards.py 中 `import darkdetect` 仍会从原来的路径加载。
        必须在 sys.path.insert 之前显式删除缓存，强制重新从 _vendor/ 加载。

    热重载兼容：
        重新执行 register_ui 时 sys.path.insert 是幂等操作（前面的判断会跳过）；
        旧 sys.modules 子模块缓存会被清理，确保新代码生效。
    """
    # ── 1. 清理可能已缓存的 vendored 包（关键！） ──
    vendor_packages = ["requests", "markdown"]  # ← 替换成实际的 vendored 包名
    cleaned = []
    for pkg_name in vendor_packages:
        for mod_name in list(sys.modules.keys()):
            if mod_name == pkg_name or mod_name.startswith(f"{pkg_name}."):
                mod_obj = sys.modules[mod_name]
                mod_file = getattr(mod_obj, "__file__", "") or ""
                # 仅删除非 _vendor/ 来源的缓存（避免热重载时重复清理）
                if "_vendor" not in mod_file:
                    del sys.modules[mod_name]
                    cleaned.append(mod_name)
    if cleaned:
        logger.info(f"[<plugin-name>] 已清理 vendored 包缓存: {cleaned}")

    # ── 2. 加载 _vendor/ ──
    vendor_dir = Path(__file__).parent / "_vendor"
    if vendor_dir.exists() and str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))
        logger.info(f"[<plugin-name>] _vendor/ 已加入 sys.path: {vendor_dir}")
    else:
        logger.warning(
            f"[<plugin-name>] _vendor/ 不存在或已加入: {vendor_dir} "
            f"(exists={vendor_dir.exists()})"
        )

    # ── 3. 验证 _vendor/ 中的包真的可用（便于诊断） ──
    try:
        import requests  # noqa: F401

        if "_vendor" in requests.__file__:
            logger.info(f"[<plugin-name>] ✓ requests 已从 _vendor/ 加载: {requests.__file__}")
        else:
            logger.error(f"[<plugin-name>] ✗ requests 仍从非 _vendor/ 加载: {requests.__file__}")
    except ImportError as e:
        logger.error(f"[<plugin-name>] ✗ requests 加载失败: {e}")

    # ── 4. 清理旧子模块缓存（热重载兼容） ──
    safe_name = "<plugin-name>".replace("-", "_").replace(":", "_")
    prefix = f"ui_plugin_{safe_name}."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    # ── 5. 注册组件 ──
    from .cards import MyCardWidget

    registry.register_floating_card(
        plugin_name="<plugin-name>",
        card_id="<card-id>",
        widget_class=MyCardWidget,
        container="bottom",
        title="<卡片标题>",
        default_visible=False,
    )

    logger.info("[<plugin-name>] UI components registered")
```

### 5.5.1 常见陷阱：sys.modules 缓存

**症状**：在 dev 环境下运行插件，明明 `register_ui` 里把 `_vendor/` 加到了 `sys.path[0]`，但 `cards.py` 中 `import xxx` 仍然从 site-packages / venv 加载。

**原因**：
```python
# 假设 DriFox 启动时已 import 了 darkdetect（用于主题检测）
import darkdetect  # ← 现在 darkdetect 缓存在 sys.modules

# 你的 register_ui 执行
sys.path.insert(0, vendor_dir)  # sys.path 顺序已对
import darkdetect  # ← 但 sys.modules 已有缓存，直接返回，**不查 sys.path**
```

**修复**：必须在 `sys.path.insert` 之前清理缓存：

```python
# 正确的 register_ui 开头
for pkg_name in ["darkdetect", "packaging"]:
    for mod_name in list(sys.modules.keys()):
        if mod_name == pkg_name or mod_name.startswith(f"{pkg_name}."):
            mod_obj = sys.modules[mod_name]
            mod_file = getattr(mod_obj, "__file__", "") or ""
            if "_vendor" not in mod_file:
                del sys.modules[mod_name]
```

**判断是否真的从 `_vendor/` 加载**：在 `register_ui` 末尾加断言：

```python
import requests
assert "_vendor" in requests.__file__, (
    f"requests 应从 _vendor/ 加载，实际: {requests.__file__}"
)
```

**打包环境 vs 开发环境的差异**：
- **PyInstaller 打包后**：vendored 包根本不在 `_internal/` 中，第一次 import 必从 `_vendor/` 加载，没有此陷阱
- **dev 环境**：如果任何代码（包括主程序、其他插件）提前 import 了 vendored 包，必须手动清理缓存

### 5.6 验证 _vendor/ 在 PyInstaller 打包后可用

参考 `references/testing-vendor.md` 的完整测试脚本。

---

