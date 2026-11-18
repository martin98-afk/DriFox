/**
 * UI 全局态：布局 / WS 连接状态 / 主题名 / 页面路由。
 */

import { create } from "zustand";
import type { WsStatus } from "../api/ws";

export type PageId = "chat" | "shares" | "history" | "shortcuts" | "marketplace" | "cleanup";

interface UiState {
  wsStatus: WsStatus;
  /** 主题名（light / dark / 自定义主题 id） */
  theme: string;
  sidebarCollapsed: boolean;
  /** 顶层页面路由（F3）：chat=对话，其余为五菜单占位页 */
  page: PageId;
  setWsStatus: (s: WsStatus) => void;
  setTheme: (theme: string) => void;
  setSidebarCollapsed: (v: boolean) => void;
  toggleSidebar: () => void;
  setPage: (page: PageId) => void;
}

export const useUiStore = create<UiState>((set) => ({
  wsStatus: "idle",
  theme: "light",
  sidebarCollapsed: false,
  page: "chat",
  setWsStatus: (wsStatus) => set({ wsStatus }),
  setTheme: (theme) => set({ theme }),
  setSidebarCollapsed: (sidebarCollapsed) => set({ sidebarCollapsed }),
  toggleSidebar: () => set((st) => ({ sidebarCollapsed: !st.sidebarCollapsed })),
  setPage: (page) => set({ page }),
}));
