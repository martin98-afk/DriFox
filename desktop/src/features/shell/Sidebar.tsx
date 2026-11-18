/**
 * Sidebar：宽 256px 可折叠（uiStore.sidebarCollapsed）。
 * ①五菜单行（切 page）②自定义插件折叠组（徽章 v1=0，localStorage 持久化）
 * ③新建对话主按钮 ④项目分组会话树 ⑤成员状态列表 ⑥底部用户条。
 */

import { useState } from "react";
import { useUiStore, type PageId } from "../../stores/uiStore";
import { useFloatingStore } from "../floating/floatingStore";
import { createMockSession } from "../chat/mockDriver";
import { SessionTree } from "./SessionTree";
import { MemberList } from "./MemberList";

const PLUGINS_COLLAPSED_KEY = "drifox.sidebar.pluginsCollapsed";

const MENUS: Array<{ page: PageId; label: string; icon: JSX.Element }> = [
  {
    page: "shares",
    label: "分享记录",
    icon: (
      <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden>
        <circle cx="4" cy="8" r="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <circle cx="12" cy="3.5" r="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <circle cx="12" cy="12.5" r="2" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <path d="M5.8 7l4.4-2.6M5.8 9l4.4 2.6" stroke="currentColor" strokeWidth="1.3" fill="none" />
      </svg>
    ),
  },
  {
    page: "history",
    label: "历史会话",
    icon: (
      <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden>
        <path d="M2.5 8a5.5 5.5 0 1 1 1.6 3.9" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <path d="M2.5 12.5V9.5h3" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <path d="M8 5v3.2l2.2 1.3" fill="none" stroke="currentColor" strokeWidth="1.3" />
      </svg>
    ),
  },
  {
    page: "shortcuts",
    label: "快捷键管理",
    icon: (
      <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden>
        <rect x="1.5" y="4" width="13" height="8" rx="1.8" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <path d="M4.2 6.5h.01M6.8 6.5h.01M9.4 6.5h.01M12 6.5h.01M4.9 9.5h6.2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none" />
      </svg>
    ),
  },
  {
    page: "marketplace",
    label: "插件市场",
    icon: (
      <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden>
        <path d="M2 4.5h12l-1 9H3l-1-9z" fill="none" stroke="currentColor" strokeWidth="1.3" />
        <path d="M5.5 6.5v-2a2.5 2.5 0 0 1 5 0v2" fill="none" stroke="currentColor" strokeWidth="1.3" />
      </svg>
    ),
  },
  {
    page: "cleanup",
    label: "系统清理",
    icon: (
      <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden>
        <path d="M3 4.5h10M6.5 4.5V3a1 1 0 0 1 1-1h1a1 1 0 0 1 1 1v1.5M4.5 4.5l.8 9a1 1 0 0 0 1 .9h3.4a1 1 0 0 0 1-.9l.8-9" fill="none" stroke="currentColor" strokeWidth="1.3" />
      </svg>
    ),
  },
];

export function Sidebar() {
  const page = useUiStore((st) => st.page);
  const setPage = useUiStore((st) => st.setPage);
  const [pluginsCollapsed, setPluginsCollapsed] = useState(
    () => localStorage.getItem(PLUGINS_COLLAPSED_KEY) === "1",
  );

  const openSettings = () => {
    const st = useFloatingStore.getState();
    const open = st.cards.some((c) => c.kind === "settings" && !c.closing);
    // toggle：已开则关；未开则开（gate 内互斥/压制自动生效）
    if (open) st.closeCard("settings-main");
    else st.showCard("settings", "top", "settings-main");
  };

  const togglePlugins = () => {
    const next = !pluginsCollapsed;
    setPluginsCollapsed(next);
    if (next) localStorage.setItem(PLUGINS_COLLAPSED_KEY, "1");
    else localStorage.removeItem(PLUGINS_COLLAPSED_KEY);
  };

  return (
    <aside className="Sidebar">
      <nav className="sidebar-menus" aria-label="功能菜单">
        {MENUS.map((m) => (
          <button
            key={m.page}
            type="button"
            className={`sidebar-menu-row${page === m.page ? " is-active" : ""}`}
            onClick={() => setPage(m.page)}
          >
            <span className="sidebar-menu-icon">{m.icon}</span>
            <span className="sidebar-menu-label">{m.label}</span>
          </button>
        ))}
      </nav>

      <button type="button" className="plugins-group-head" onClick={togglePlugins}>
        <span className={`group-arrow${pluginsCollapsed ? " is-collapsed" : ""}`} aria-hidden />
        <span className="plugins-group-label">自定义插件</span>
        <span className="plugins-badge">0</span>
      </button>
      {!pluginsCollapsed && <div className="plugins-group-list">插件槽位（v1 空）</div>}

      <button type="button" className="new-chat-btn" onClick={() => createMockSession()}>
        <span className="new-chat-plus" aria-hidden>+</span>
        <span>新建对话</span>
        <span className="new-chat-more" aria-hidden>⋮</span>
      </button>

      <SessionTree />

      <MemberList />

      <div className="sidebar-userbar">
        <span className="userbar-avatar" aria-hidden>
          <svg viewBox="0 0 24 24" width="22" height="22">
            <circle cx="12" cy="9" r="4" fill="currentColor" opacity="0.85" />
            <path d="M4 20a8 8 0 0 1 16 0" fill="currentColor" opacity="0.85" />
          </svg>
        </span>
        <span className="userbar-name">dingmama123141</span>
        <span className="userbar-gear" title="设置" role="button" tabIndex={0} onClick={openSettings} onKeyDown={(e) => { if (e.key === "Enter") openSettings(); }}>⚙️</span>
      </div>
    </aside>
  );
}
