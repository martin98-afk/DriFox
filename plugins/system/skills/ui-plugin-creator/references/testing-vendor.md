# _vendor/ 机制验证指南

> 验证 UI 插件的 `_vendor/` 依赖在 PyInstaller 打包后真的可用。
> 仅适用于需要外部第三方包的 UI 插件。

---

## 一、为什么需要专门验证

`register_ui` 中的 `sys.path.insert(0, vendor_dir)` 在开发环境（直接 `python main.py`）一定能工作，因为 dev 环境下所有第三方包都已在 `site-packages` 里。

**但** PyInstaller `--onedir` 打包后，`_internal/` 只包含构建时声明的包。如果 `_vendor/` 机制出错（路径算错、`__init__.py` 缺失、Python 缓存陈旧等），只有打包后才会暴露。

所以：**必须用一个独立的测试 exe 验证**。

---

## 二、最小测试流程

### 2.1 准备测试插件

假设要验证 `plugins/my-plugin/` 的 `_vendor/` 机制，先确保插件目录如下：

```
plugins/my-plugin/
├── .drifox-plugin/
│   └── plugin.json
└── ui/
    ├── __init__.py      # 含 vendor 加载逻辑
    ├── cards.py         # 可选，仅 import 第三方包
    └── _vendor/
        └── <some-package>/   # 复制好的第三方包
            ├── __init__.py
            └── ...
```

### 2.2 写测试主脚本

创建 `_test_my_plugin.py`（项目根目录临时文件，跑完可删）：

```python
# -*- coding: utf-8 -*-
"""验证 my-plugin 的 _vendor/ 在 PyInstaller 打包后可用"""

import sys
from pathlib import Path


def main():
    plugin_path = Path(r"PLUGINS_PATH_PLACEHOLDER\my-plugin")
    ui_dir = plugin_path / "ui"
    vendor_dir = ui_dir / "_vendor"

    print(f"plugin_path: {plugin_path}")
    print(f"vendor_dir exists: {vendor_dir.exists()}")

    # === 模拟 UIPluginRegistry.load_plugin() ===
    sys.path.insert(0, str(ui_dir))

    # === 模拟 register_ui() ===
    sys.path.insert(0, str(vendor_dir))

    print("\n=== 关键测试 ===\n")

    # Test 1: 直接 import
    print("[Test 1] import <some-package>")
    try:
        import some_package  # ← 改成实际的包名
        print(f"  OK: {some_package.__file__}")
        if "_vendor" not in some_package.__file__:
            print("  FAIL: 不是从 _vendor/ 加载")
            sys.exit(1)
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        sys.exit(1)

    # Test 2: 调用 API
    print("\n[Test 2] 调用包 API")
    try:
        result = some_package.some_function()
        print(f"  OK: {result}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        sys.exit(1)

    # Test 3: 完整链路（用 importlib 加载 ui/__init__.py）
    print("\n[Test 3] 模拟 UIPluginRegistry.load_plugin() 完整链路")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ui_plugin_test", ui_dir / "__init__.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["ui_plugin_test"] = module
        spec.loader.exec_module(module)
        print(f"  OK: register_ui() 执行成功")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print("\n=== 所有验证通过 ===")


if __name__ == "__main__":
    main()
```

把 `PLUGINS_PATH_PLACEHOLDER` 改成 `Path(__file__).parent` 或绝对路径。

### 2.3 打包并运行

```bash
# 1. 打包（不要在命令中加 --hidden-import=<some-package>，
#    必须真实模拟"打包时未声明"的场景）
pyinstaller --onedir --windowed --noconfirm --clean --name=_test_my_plugin _test_my_plugin.py

# 2. 把 plugins/my-plugin/ 复制到 dist/_test_my_plugin/ 旁
#    Windows:
xcopy /E /I plugins\my-plugin dist\_test_my_plugin\my-plugin

# 3. 运行 exe
dist\_test_my_plugin\_test_my_plugin.exe
```

**期望输出**：
```
plugin_path: ...\plugins\my-plugin
vendor_dir exists: True
=== 关键测试 ===

[Test 1] import <some-package>
  OK: ...\plugins\my-plugin\ui\_vendor\<some-package>\__init__.py
[Test 2] 调用包 API
  OK: <expected result>
[Test 3] 模拟 UIPluginRegistry.load_plugin() 完整链路
  OK: register_ui() 执行成功
=== 所有验证通过 ===
```

**如果 Test 1 FAIL**，按下面顺序排查：

| 报错 | 排查 |
|------|------|
| `No module named '<pkg>'` | `_vendor/<pkg>/__init__.py` 缺失或包名拼错 |
| `ImportError: cannot import name 'X' from '<pkg>'` | 包复制不完整，缺子模块 |
| `ModuleNotFoundError: No module named 'A'`（其中 A 是 `_vendor/<pkg>` 内部的依赖） | 该包需要另一个包，需要把依赖也复制到 `_vendor/` |

---

## 三、在 DriFox 主程序中验证

最小测试通过后，还要在主程序里跑一遍：

1. **复制插件到用户目录**：
   ```bash
   cp -r plugins/my-plugin ~/.drifox/plugins/my-plugin
   ```
2. **启动 DriFox**（开发模式：`python main.py`）
3. **执行插件命令**（如 `/my-card`）
4. **观察日志**：确认 `_vendor/` 加载和第三方包 import 都成功
5. **触发热重载**：修改 `cards.py` 一行无关代码（如 `print`），让 watchfiles 重载插件
6. **再执行一次命令**：验证热重载后 `_vendor/` 仍然生效

---

## 四、CI 自动化（可选）

如果项目有 GitHub Actions / GitLab CI，可以在 PR 检查中加入：

```yaml
- name: Verify UI plugin _vendor/ mechanism
  run: |
    # 1. 临时给一个插件加 _vendor/ 测试
    python -c "
    import shutil, os
    import requests
    src = os.path.dirname(requests.__file__)
    dst = '.tmp_vendor_test/ui/_vendor/requests'
    os.makedirs(dst, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    "
    # 2. 打包并跑测试 exe
    pyinstaller --onedir --windowed --noconfirm --clean --name=_vendor_test _test_vendor.py
    cp -r .tmp_vendor_test dist/_vendor_test/vendor_test
    dist/_vendor_test/_vendor_test.exe
```

---

## 五、最小工作示例

仓库内置 `examples/vendor-demo/` 是一个最小可运行示例，结构：

```
examples/vendor-demo/
├── ui/
│   ├── __init__.py           # register_ui 含 _vendor/ 加载
│   ├── cards.py              # 简单浮动卡片，import 第三方包
│   └── _vendor/
│       └── darkdetect/       # 已复制的纯 Python 包
└── README.md                 # 跑测试的命令
```

直接按 §2.3 步骤操作即可验证 `darkdetect.theme()` 在 PyInstaller 打包后可用。