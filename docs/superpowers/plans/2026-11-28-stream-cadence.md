# 流式阅读体验实施计划

目标：分别验证正文分批到达时的显示节奏和工具完成后的结果层次。

- [ ] 在 `tests/perf/test_visible_text_cadence.py` 记录逐帧字数，回放等量但分批输入；记录修改前后最长停顿、每次新增字数及首尾延迟，校验完稿内容。
- [ ] 在 `app/widgets/card_viewers.py` 的正文揭示路径中只调整回放证实有影响的分支；复跑均匀输入和成批输入。
- [ ] 核对 `app/widgets/render_helpers.py` 生成的完成态和 `app/widgets/card_viewers.py` 的共用样式，在普通区和折叠区降低嵌套视觉重量；保留错误、详情、文件交互。
- [ ] 使用 `.venv/Scripts/python.exe -m pytest` 跑生产 WebEngine 前台回放和工具块回归；隔离 `uv.lock`。
