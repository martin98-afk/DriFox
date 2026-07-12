---
description: 锁屏远程：保持系统唤醒、屏幕常亮并锁屏，便于手机远程操控与自动化持续运行
type: function
---

锁屏远程控制（需 Windows 平台，依赖系统电源 API）：

- `/lock-remote on`     开启：系统保持唤醒、屏幕常亮、立即锁屏
- `/lock-remote off`    关闭：恢复系统正常休眠策略
- `/lock-remote status` 查看当前状态

适用于离开电脑但需保持自动化任务运行、并通过手机（Gateway / 本地 API）远程操控的场景。
