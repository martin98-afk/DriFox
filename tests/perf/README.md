# tests/perf — 性能瓶颈回归测试

本目录对 **#1 性能瓶颈报告 Top5** 提供最小复现 / 现象确认 + 修复前后量化回归基线。

> 所有测试均为 **静态源码分析**：用 `pathlib` 读取 `app/` 下源码文本 + `re` 匹配。
> **未修改任何业务代码**，不 `import PyQt5`、不实例化任何 GUI 对象。
> `tests/conftest.py` 会自动建 `QApplication`，但本目录测试逻辑不依赖它。

## 文件清单

| 文件 | 对应瓶颈 | 测试要点 |
|---|---|---|
| `test_message_card_paint_throttle.py` | Top① 消息卡片动画高频绘制 | 动画定时器 50ms；paintEvent 已缓存渐变 `self._grad_*` / 裁剪路径 `self._clip_*`；每帧仍有颜色分配（build_gradient >=3、lerp_color 存在） |
| `test_lazy_batch_webengineview.py` | Top② WebEngineView 一次性实例化 | 懒加载分批 `_process_next_lazy_batch` + `ensure_rendered()` + `singleShot(80,...)`；Chromium 实例上限 `_max_rendered_cards` + LRU 回收 `_recycle_lru_batches` |
| `test_share_card_upload_nonblocking.py` | Top③ 分享上传阻塞主线程 | 上传入口 `_on_upload` + `uploader.upload_file(`；后台线程 `_ShareUploadThread(QThread)` + 按钮禁用 / "上传中"；底层 `requests.post(..., timeout=30)` |
| `test_startup_init_lazy.py` | Top④ 启动同步初始化链 | `self.backend.initialize(` -> `self._init_plugin_system()` 同步直调（前 6 行无 `singleShot`）-> `self._agent_manager.reload_agents()` 同步触发 |
| `test_memory_timer_and_branch_cache.py` | Top⑤ `_branch_cache` 淘汰/上限保护 | 断言存在 `_branch_cache.pop` 淘汰 + `_MAX_BRANCH*` 上限常量。说明：原 `_memory_timer`（标题栏 RSS 内存标签）已按需求下线，相关用例移除。 |

## 运行方式

```bash
# 全部性能测试
cd D:/work/DriFox && python -m pytest tests/perf/ -v

# 单文件
cd D:/work/DriFox && python -m pytest tests/perf/test_message_card_paint_throttle.py -v
```

## 环境要求

- pytest >= 7
- Python 3
- 对 `app/` 源码有读权限
- 无需显示器（测试不实例化 GUI）
- 无新三方依赖（仅标准库 `pathlib` / `re`）
- 跨平台，Windows 优先

## 说明

- 测试未修改任何业务代码，仅静态分析源码文本。
- **Top⑤ 修复后回归保护**：`test_memory_timer_and_branch_cache.py` 已固化为「
  存在 `_branch_cache.pop` + `_MAX_BRANCH*` 上限常量」断言（防止后续重构误删性能修复）。
  注：原 `_memory_timer`（标题栏 RSS 内存标签）已按需求下线，相关用例同步移除。
- **Top① `lerp_color` 计数偏差说明**：#1 报告估算「3 渐变 x ~9 lerp_color 约 27 QColor/帧」。
  实际源码中 `lerp_color` 定义为 helper 并在 `build_gradient` 的 `stops` 循环内调用 1 次
  （文本仅出现 2 处），运行时循环展开为每帧 ~27 次分配。因此测试断言改为
  `lerp_color(` 文本出现 **>=2**（证明 helper 定义并被调用），而非报告中的 >=20（文本计数口径不符）。
