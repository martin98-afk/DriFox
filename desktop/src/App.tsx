import { useEffect } from "react";
import "./App.css";
import "./features/chat/chat.css";
import "./features/shell/shell.css";
import "./features/floating/floating.css";
import "./features/settings/settings.css";
import "./features/workbench/workbench.css";
import { TitleBar } from "./features/shell/TitleBar";
import { Sidebar } from "./features/shell/Sidebar";
import { ContextMenu } from "./features/shell/ContextMenu";
import { WelcomeCard } from "./features/shell/WelcomeCard";
import { PlaceholderPage } from "./features/shell/PlaceholderPage";
import { Workbench } from "./features/workbench/Workbench";
import { FloatingLayer } from "./features/floating/FloatingLayer";
import { RetryChip } from "./features/floating/RetryChip";
import { ChatArea } from "./features/chat/ChatArea";
import { InputArea } from "./features/chat/InputArea";
import { isMockMode, mockSend } from "./features/chat/mockDriver";
import { initApp, liveSend, liveStop } from "./api/live";
import { useUiStore, type PageId } from "./stores/uiStore";
import { useSessionStore } from "./stores/sessionStore";
import { useChatLog } from "./features/chat/chatLog";
import { useStreamStore } from "./stores/streamStore";

/** data-theme 同步：auto = 移除属性走 prefers-color-scheme（切换入口在 TitleBar） */
function applyTheme(mode: string): void {
  if (mode === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
}

/** 对话页：无会话/空会话 → 欢迎卡（空会话带 InputArea 可发送）；有消息 → 对话流 */
function ChatPage() {
  const currentSid = useSessionStore((st) => st.currentSid);
  const messages = useChatLog((st) => (currentSid ? st.messagesBySid[currentSid] : undefined)) ?? EMPTY_MSGS;
  const empty = messages.length === 0;
  const streaming = useStreamStore((st) => (currentSid ? st.streams[currentSid]?.phase === "streaming" : false));

  return (
    <>
      <div className="chat-topline">
        <RetryChip />
      </div>
      {empty || !currentSid ? <WelcomeCard /> : <ChatArea />}
      {currentSid && (
        <InputArea
          streaming={streaming}
          onStop={streaming && !isMockMode() ? () => void liveStop() : undefined}
          onSend={(text) => void (isMockMode() ? mockSend(text) : liveSend(text))}
        />
      )}
    </>
  );
}

/**
 * AppShell：TitleBar + (Sidebar + 主区按 page 路由)。
 * mock 模式（VITE_API_MODE=mock，默认）由 mockDriver 驱动闭环。
 */
export default function App() {
  const page = useUiStore((st) => st.page);
  const sidebarCollapsed = useUiStore((st) => st.sidebarCollapsed);
  const theme = useUiStore((st) => st.theme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    initApp(); // mock/live 分流集中此处（F10）
  }, []);

  return (
    <div className="app-shell">
      <TitleBar />
      <div className="app-body">
        {!sidebarCollapsed && <Sidebar />}
        <main className="app-main app-main-chat">
          {page === "chat" ? <ChatPage /> : <PlaceholderPage page={page as Exclude<PageId, "chat">} />}
        </main>
        <Workbench />
      </div>
      <FloatingLayer />
      <ContextMenu />
    </div>
  );
}

const EMPTY_MSGS: never[] = [];
