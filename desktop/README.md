# DriFox Desktop（Tauri 2 + React 18 + TypeScript + Vite）

桌面端空壳工程，目标：全量复刻 PyQt5 主程序 UI。

## 启动

```bash
cd desktop
npm install
npm run tauri dev   # 弹出 1280x800 窗口，前端热更新（HMR）生效
```

## 构建与类型检查

```bash
npm run build       # tsc 类型检查 + vite 产物构建
```

前置依赖：Node ≥ 18、Rust stable（rustup）。

## 目录结构

```
desktop/
├── src/                  # React 前端
│   ├── api/ws.ts         # WebSocket 客户端骨架（自动重连 + 事件分发表）
│   ├── theme/tokens.css  # 主题 CSS 变量（暗色优先）
│   ├── App.tsx           # 三区布局：左侧栏 + 主内容区 + 状态栏
│   └── main.tsx          # 挂载入口
└── src-tauri/            # Tauri 壳（Rust）
    ├── tauri.conf.json   # 窗口配置：标题 DriFox，1280x800
    └── src/lib.rs        # Builder 入口，后续挂 command 与事件桥
```

## 约定

- 前端与 Python 主进程通信走 `src/api/ws.ts`，消息协议 `{ type, payload }`，事件类型清单待约定（见 ws.ts TODO）。
- 主题只调 `src/theme/tokens.css` 的 CSS 变量，不散落硬编码色值。
