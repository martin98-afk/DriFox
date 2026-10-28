# 设置卡（设置页配置 UI）

插件往「设置」页注册的配置卡。两份来源材料：

- 本文件：口径、契约、范式、审查清单
- 真实成品：`voice-reply/ui/config_card.py`（表单型 + 外链 + 连通性测试）、`ssh-toolkit/ui/connections_card.py`（列表型）

## 1. 自动卡还是自绘卡

| 场景 | 选择 |
|------|------|
| 字段都是 `text` / `password` / `bool` / `select` / `number` / `textarea` / `link` / `action` | **E1 自动卡**：只写 `config_schema`，零 UI 代码 |
| 需要可编辑下拉（下拉选 + 手填二合一） | 自绘卡 |
| 选项运行时才有（服务商、模型、连接列表） | 自绘卡 |
| 字段之间要联动（切服务商换模型清单） | 自绘卡 |
| 需要「测试连接」按钮、行尾外链、状态置灰 | 自绘卡 |
| 条目列表（增删行） | 自绘卡（列表型） |

## 2. 注册契约

```python
registry.register_settings_card(
    PLUGIN_NAME,                  # 插件名
    f"{PLUGIN_NAME}-config",      # card_id
    "卡片标题",                    # title
    _MyConfigCard,                # widget_class，构造签名必须是 (parent=None)
    section="plugins",
    priority=1,                   # >0 覆盖同 card_id 的 E1 自动卡
)
```

- 放在 `ui/__init__.py` 的 `register_ui` 里**延迟导入**卡片类（`from .config_card import _MyConfigCard`），避免热重载时的半初始化模块
- 外面包 `try/except` + `logger.warning`：旧宿主没有该方法时静默降级回 E1 自动卡
- `config_schema` 要保留：它是降级路径，也是能力/字段的唯一声明

> **覆盖即接管**：`priority > 0` 会盖掉自动卡，`config_schema` 里的 `link` / `action` 字段随之消失。自绘卡必须把这些入口补回来（行尾外链 / 按钮），否则用户丢功能。

## 3. 布局口径（表单型）

```python
self.viewLayout.setContentsMargins(48, 8, 48, 8)
self.viewLayout.setSpacing(4)

row = QWidget(self.view)
layout = QHBoxLayout(row)
layout.setContentsMargins(0, 4, 0, 4)
layout.setSpacing(12)
label = BodyLabel("字段名", row)
label.setMinimumWidth(120)                       # 标签列定宽，全卡对齐
layout.addWidget(label, 0, Qt.AlignLeft | Qt.AlignVCenter)
layout.addWidget(control, 1)                     # 控件 stretch=1，撑满剩余宽度
self.viewLayout.addWidget(row)
```

- **禁止** `control.setMinimumWidth(220)` + `Qt.AlignRight`：控件被挤成固定窄条，长值（模型名、URL）被裁
- 行尾动作（获取 Key、打开文档）用内联行：主控件 `stretch=1`，尾部控件固定宽右对齐
- 重置 / 危险按钮：右对齐、固定宽约 120–140，**不撑满整行**
- 字段不适用时整行 `setEnabled(False)` 置灰，不要 `setVisible(False)`：隐藏会改变卡片高度、引发布局跳动
- 多行控件（TextEdit）所在行的标签用 `Qt.AlignTop`，否则标签漂在中间

## 4. 控件选择

| 内容 | 控件 | 要点 |
|------|------|------|
| 单值枚举 | `ComboBox` | `addItem(label, userData=value)`，userData 必须关键字传 |
| 可手填的枚举 | `EditableComboBox` | 继承 `QLineEdit`；选中后文本是 item label，真值取 `currentData()` |
| URL / 路径 / 短文本 | `LineEdit` | 开 clearButton |
| 密钥 | `PasswordLineEdit` | 不明文回显，不写日志 |
| 多行文本 | `TextEdit` | 最小高度 ≥140 |
| 外链 | `HyperlinkButton` | `setUrl()` 后点击自动用系统浏览器打开 |
| 开关 | `SwitchButton` | 挂 header 右侧或单独成行 |
| 动作按钮 | `PushButton` | 定宽，放在底部动作行 |

## 5. 回显

- 逐控件 `blockSignals(True)` → 写值 → `blockSignals(False)`，避免回显触发保存
- 存值不在选项清单里（服务商被删、模型下线）时 `insertItem` 插回首项保住用户配置，不静默重置
- 空值 = 清除 → 回默认，与 `PluginConfigStore` 语义一致
- 长文本回显后 `setCursorPosition(0)`：默认光标在末尾，会把 `FunAudioLLM/CosyVoice2-0.5B` 显示成 `…2-0.5B`
- 回显结束调一次状态刷新（置灰 / 联动），保证首屏就是正确状态

## 6. 折叠高度

`ExpandSettingCard` 展开会把 `spaceWidget` 撑成内容等高，导致展开高度 = 内容 + 等高空白、卡片关不上。统一覆写：

```python
def _adjustViewSize(self):
    h = self.viewLayout.sizeHint().height()
    try:
        self.spaceWidget.setFixedHeight(0)
    except Exception:
        pass
    self.setFixedHeight(self.card.height() + (h if self.isExpand else 0))

def setExpand(self, isExpand: bool):
    if self.isExpand == isExpand:
        return
    self.isExpand = isExpand
    self.setProperty("isExpand", isExpand)
    self.setStyle(QApplication.style())
    self.card.expandButton.setExpand(isExpand)
    self._adjustViewSize()
```

卡片里有自动换行提示（`setWordWrap(True)`）时，`sizeHint()` 在未布局时按默认宽度算会少算高度，底部被截。此时按卡片实宽补一刀：

```python
width = self.width() - 96              # 卡片宽 - 左右边距
if width > 200 and self.viewLayout.hasHeightForWidth():
    h = max(h, self.viewLayout.heightForWidth(width))
```

不要用 `self.view.width()`：构造期它还是默认小值，会把提示行算成几十行高。

## 7. 列表型卡

连接列表、条目管理这类不是字段表单的卡：

- 行结构：状态点 / 图标 → 主键（加粗）→ 次要信息（灰色、可省略号）→ 行尾动作按钮
- 行内边距 `12, 6`，行间距 4，外层沿用 `48, 8, 48, 8`
- 空态与错误态用一行提示文字（灰 / 红），不用弹窗
- 行尾按钮 `background: transparent` + 细边框，hover 才上色
- 删除类操作二次确认，确认文案写清连带影响
- 展开态增删行后补一次 `_adjustViewSize()`

## 8. 外部服务连通性测试（强烈建议）

凡是让用户填第三方 API Key / 端点的卡，都要给一个「测试连接」：不验证的话，用户只能等到真用时才发现 Key 无效或模型名写错。

**要求**

- 真发一次最小请求（不是只校验格式）：LLM 发一句极短 ping，TTS 合成一两个字，ASR 上传 1 秒音调 WAV
- 后台线程发（`QThread`），主线程只更新 UI，按钮期间显示「测试中…」并禁用
- 结果就地显示在按钮同一行，成功绿 / 失败红，超长截断 + tooltip 给全文，**不弹 InfoBar**（会遮设置页）
- 用界面当前值而不是已存值（用户可能刚改完还没失焦）
- 失败原因要人话：401/403 → 「API Key 无效或无权限」，404 → 「端点或模型不存在」，429 → 「被限流」，超时单独说

**骨架**

```python
class _ProbeWorker(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, url, model, key, parent=None):
        super().__init__(parent)
        self._url, self._model, self._key = url, model, key

    def run(self) -> None:
        import time
        started = time.monotonic()
        try:
            data = synthesize(self._url, self._model, self._key)   # 复用插件自己的调用函数
        except Exception as e:                                     # noqa: BLE001
            self.done.emit(False, str(e))
            return
        self.done.emit(True, f"可用 · {int((time.monotonic() - started) * 1000)} ms · {len(data) // 1024} KB")


def _on_test(self) -> None:
    key = self._key_edit.text().strip()
    if not key:
        self._show_probe(False, "未填写 API Key")
        return
    self._test_btn.setEnabled(False)
    self._test_btn.setText("测试中…")
    self._probe = _ProbeWorker(self._url_edit.text().strip(), self._current_model(), key, self)
    self._probe.done.connect(self._on_probe_done)
    self._probe.start()

def _on_probe_done(self, ok: bool, message: str) -> None:
    self._test_btn.setEnabled(True)
    self._test_btn.setText("测试连接")
    self._show_probe(ok, message)

def _show_probe(self, ok: bool, message: str) -> None:
    short = message if len(message) <= 44 else message[:44] + "…"
    self._test_result.setText(("✓ " if ok else "✗ ") + short)
    self._test_result.setToolTip(message)
    self._test_result.setStyleSheet("color: #3ea76a;" if ok else "color: #e5484d;")
```

worker 必须存到 `self._probe`，否则被 GC 掉线程直接死。

**测试音频（ASR 类）**：生成 1 秒 440Hz 正弦 WAV（16kHz / 单声道 / 16bit）。别用静音——部分平台对纯静音判「无效音频」返回 4xx，把配置正确误报成失败。判可用看 HTTP 200 + 无错误字段，**不看识别文本是否为空**。

**不支持测试的情况**：插件用的是主程序里已配置好的服务商/模型（宿主服务商设置页已做过连通性验证）时，不必重复造按钮。

## 9. 审查清单

1. 每个字段都有**可见** label，不靠 tooltip 说明字段含义
2. 长值完整可见：控件撑满、长文本光标归零、无固定窄宽
3. `config_schema` 里的 `link` / `action` 在自绘卡里都有对应入口
4. 不适用的字段已置灰（整行），布局不跳动
5. 重置按钮右对齐不撑满；底部动作行左测试 / 右重置
6. 展开无空白、折叠能收回去，增删行后高度同步
7. 填第三方凭据的卡有「测试连接」，结果就地展示
8. 深浅主题都正常：优先用 qfluentwidgets 控件自带配色，必须自定义时用 `rgba()`
9. 文案中文、标点统一，不出现「请点击下方按钮进行…」式冗余

## 10. 常见坑

| 现象 | 原因与修法 |
|------|-----------|
| 下拉选中文本被裁掉前半截 | `EditableComboBox` 回显后光标在末尾，`setCursorPosition(0)` |
| 选中音色/模型后配置被写坏 | 取的是 `text()`（item label）而非 `currentData()`（真值） |
| `currentData()` 恒为 None | `addItem` 位置传参把值当成了 icon，`userData=` 必须关键字传 |
| 枚举属性不存在导致整卡构建失败 | 枚举名不可凭记忆（`GLOBE` 不是 `GLOB`），用 `getattr(F, "X", None) or F.SETTING` |
| 卡片展开后关不上 | 未归零 `spaceWidget`，见第 6 节 |
| 自动换行提示被截掉 | `sizeHint()` 按默认宽度算少，用卡片实宽 `heightForWidth` 补 |
| 未 show 时高度算少 | `sizeHint()` 在未显示时不完整，需要时手工累加 |
| 设置卡与工具组件状态不同步 | UI 侧必须 import 同一顶层模块名复用实例，按路径重载会得到两个实例 |
| 测试按钮点完无反应 | worker 只存了局部变量被 GC，挂到 `self._probe` |
