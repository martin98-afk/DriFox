/**
 * TitleBar：顶部 36px。中央四页签（对话=CHAT 常驻激活胶囊；助手/轨迹/会议=体系占位静态签）；
 * 左侧 80px 让位 macOS 红绿灯（overlay 方案，src-tauri 窗口配置后生效）+ 侧栏开关；右侧分屏占位 + 主题循环。
 */

import { useUiStore } from "../../stores/uiStore";
import { useWorkbenchStore } from "../workbench/workbenchStore";

const TABS: Array<{ id: string; label: string; active: boolean }> = [
  { id: "chat", label: "对话", active: true },
  { id: "agents", label: "助手", active: false },
  { id: "traces", label: "轨迹", active: false },
  { id: "meetings", label: "会议", active: false },
];

export function TitleBar() {
  const page = useUiStore((st) => st.page);
  const theme = useUiStore((st) => st.theme);
  const setTheme = useUiStore((st) => st.setTheme);
  const toggleSidebar = useUiStore((st) => st.toggleSidebar);
  const toggleWorkbench = useWorkbenchStore((st) => st.toggle);
  const workbenchOpen = useWorkbenchStore((st) => st.open);

  const THEME_CYCLE = ["auto", "light", "dark"] as const;
  const cycleTheme = () => setTheme(THEME_CYCLE[(THEME_CYCLE.indexOf(theme as "auto") + 1) % 3]);

  return (
    <header className="TitleBar">
      <div className="titlebar-left">
        <span className="traffic-light-spacer" aria-hidden />
        <button className="titlebar-icon-btn" title="折叠/展开侧栏" onClick={toggleSidebar} aria-label="侧栏开关">
          <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden>
            <rect x="1.5" y="2.5" width="13" height="11" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
            <line x1="6" y1="2.5" x2="6" y2="13.5" stroke="currentColor" strokeWidth="1.3" />
          </svg>
        </button>
      </div>

      <nav className="titlebar-tabs" aria-label="页面页签">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`titlebar-tab${t.active && page === "chat" ? " is-active" : ""}${t.active ? "" : " is-static"}`}
            title={t.active ? undefined : "体系占位（M3 接入插件 tab）"}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <div className="titlebar-right">
        <button className="titlebar-icon-btn" title="主题: 切换 auto/light/dark" onClick={cycleTheme}>
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden>
            <circle cx="8" cy="8" r="5.2" fill="none" stroke="currentColor" strokeWidth="1.3" />
            <path d="M8 2.8v10.4A5.2 5.2 0 0 0 8 2.8z" fill="currentColor" />
          </svg>
        </button>
        <button
          className={`titlebar-icon-btn${workbenchOpen ? " is-active" : ""}`}
          title="工作台侧板"
          onClick={toggleWorkbench}
          aria-label="工作台侧板"
        >
          <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden>
            <rect x="1.5" y="2.5" width="13" height="11" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
            <line x1="8" y1="2.5" x2="8" y2="13.5" stroke="currentColor" strokeWidth="1.3" />
          </svg>
        </button>
      </div>
    </header>
  );
}
