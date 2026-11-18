/**
 * SessionTree：项目分组会话树。
 * 分组头（DF 图标+项目名+计数+折叠箭头 120ms 旋转）；会话行 44px 两行式（标题/预览）；
 * 折叠态 localStorage 持久化；hover 300ms 延迟预览浮层（v1：标题+时间）。
 */

import { useCallback, useMemo, useRef, useState } from "react";
import { useSessionStore } from "../../stores/sessionStore";
import type { SessionInfo } from "../../api/ws";
import { useChatLog } from "../chat/chatLog";
import { isMockMode, switchMockSession, deleteMockSessionWithUndo, renameMockSession } from "../chat/mockDriver";
import { liveSwitchSession } from "../../api/live";
import { useContextMenuStore } from "./contextMenuStore";
import { useFloatingStore } from "../floating/floatingStore";

const COLLAPSE_PREFIX = "drifox.sidebar.projCollapsed.";
const PIN_KEY = "drifox.pinnedSessions";

function readPinned(): Set<string> {
  try {
    const raw = localStorage.getItem(PIN_KEY);
    return raw ? new Set(JSON.parse(raw) as string[]) : new Set();
  } catch {
    return new Set();
  }
}

function writePinned(set: Set<string>): void {
  localStorage.setItem(PIN_KEY, JSON.stringify([...set]));
}

interface Group {
  project: string;
  sessions: SessionInfo[];
}

function readCollapsed(project: string): boolean {
  return localStorage.getItem(COLLAPSE_PREFIX + project) === "1";
}

export function SessionTree() {
  const sessions = useSessionStore((st) => st.sessions);
  const order = useSessionStore((st) => st.order);
  const currentSid = useSessionStore((st) => st.currentSid);
  const messagesBySid = useChatLog((st) => st.messagesBySid);

  const [collapsedSet, setCollapsedSet] = useState<Set<string>>(() => {
    const s = new Set<string>();
    for (const id of order) {
      const p = sessions[id]?.project;
      if (p && readCollapsed(p)) s.add(p);
    }
    return s;
  });

  // hover 预览浮层：300ms 延迟
  const [preview, setPreview] = useState<{ sid: string; x: number; y: number } | null>(null);
  const hoverTimer = useRef<number | null>(null);
  // 置顶（mock 记忆 localStorage）与重命名内联编辑态
  const [pinned, setPinned] = useState<Set<string>>(readPinned);
  const [editingSid, setEditingSid] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");

  const groups = useMemo<Group[]>(() => {
    const byProject = new Map<string, Group["sessions"]>();
    for (const id of order) {
      const s = sessions[id];
      if (!s) continue;
      const key = s.project ?? "未分组";
      const list = byProject.get(key) ?? [];
      list.push(s);
      byProject.set(key, list);
    }
    return [...byProject.entries()].map(([project, list]) => {
      // 组内置顶在前（置顶 mock 记忆）
      list.sort((a, b) => Number(pinned.has(b.id)) - Number(pinned.has(a.id)));
      return { project, sessions: list };
    });
  }, [sessions, order, pinned]);

  const toggleGroup = useCallback((project: string) => {
    setCollapsedSet((prev) => {
      const next = new Set(prev);
      if (next.has(project)) {
        next.delete(project);
        localStorage.removeItem(COLLAPSE_PREFIX + project);
      } else {
        next.add(project);
        localStorage.setItem(COLLAPSE_PREFIX + project, "1");
      }
      return next;
    });
  }, []);

  const lastTs = useCallback(
    (sid: string): number => {
      const list = messagesBySid[sid];
      return list && list.length > 0 ? list[list.length - 1].ts : 0;
    },
    [messagesBySid],
  );

  const previewText = useCallback(
    (sid: string): string => {
      const list = messagesBySid[sid];
      if (!list || list.length === 0) return "（空会话）";
      const last = list[list.length - 1];
      const text = last.role === "user" ? last.text ?? "" : (last.blocks ?? []).map((b) => (b.type === "text" ? b.content : "")).join(" ");
      return text.slice(0, 60) || "（空会话）";
    },
    [messagesBySid],
  );

  const onSessionEnter = useCallback((sid: string, e: React.MouseEvent) => {
    if (hoverTimer.current !== null) clearTimeout(hoverTimer.current);
    hoverTimer.current = window.setTimeout(() => {
      const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
      setPreview({ sid, x: rect.right + 8, y: rect.top });
    }, 300);
  }, []);

  const onSessionLeave = useCallback(() => {
    if (hoverTimer.current !== null) clearTimeout(hoverTimer.current);
    hoverTimer.current = null;
    setPreview(null);
  }, []);

  const previewMsg = preview ? sessions[preview.sid] : null;

  /** session 右键（§四）：重命名/删除（undo 链）/置顶/分享 + 插件项合并由 ContextMenu 层处理 */
  const sessionCtx = (s: SessionInfo) => (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const pushToast = useFloatingStore.getState().pushToast;
    const isPinned = pinned.has(s.id);
    const items = [
      { key: "rename", label: "重命名", action: () => { setEditTitle(s.title ?? ""); setEditingSid(s.id); } },
      { key: "delete", label: "删除", danger: true, action: () => deleteMockSessionWithUndo(s.id) },
      {
        key: "pin",
        label: isPinned ? "取消置顶" : "置顶",
        action: () => {
          const next = new Set(pinned);
          if (isPinned) next.delete(s.id);
          else next.add(s.id);
          setPinned(next);
          writePinned(next);
        },
      },
      { key: "share", label: "分享", action: () => pushToast({ title: "分享", message: `已生成「${s.title ?? s.id}」分享链接（mock）`, level: "info" }) },
    ];
    useContextMenuStore.getState().openMenu({ x: e.clientX, y: e.clientY, target: "session", targetId: s.id }, items);
  };

  const commitRename = () => {
    if (!editingSid || !editTitle.trim()) {
      setEditingSid(null);
      return;
    }
    // live 分支走 restApi PATCH（sessionStore.renameSession）；mock 本地一字不动
    if (isMockMode()) renameMockSession(editingSid, editTitle.trim());
    else void useSessionStore.getState().renameSession(editingSid, editTitle.trim());
    setEditingSid(null);
  };

  const selectSession = (sid: string) => {
    if (isMockMode()) switchMockSession(sid);
    else void liveSwitchSession(sid);
  };

  return (
    <div className="SessionTree">
      {groups.map((g) => {
        const collapsed = collapsedSet.has(g.project);
        return (
          <div key={g.project} className="session-group">
            <button type="button" className="session-group-head" onClick={() => toggleGroup(g.project)}>
              <span className={`group-arrow${collapsed ? " is-collapsed" : ""}`} aria-hidden />
              <span className="group-logo" aria-hidden>DF</span>
              <span className="group-name">{g.project}</span>
              <span className="group-count">{g.sessions.length}</span>
            </button>
            {!collapsed && (
              <div className="session-group-list">
                {g.sessions.map((s) => (
                  <div
                    key={s.id}
                    className={`session-row${currentSid === s.id ? " is-active" : ""}${pinned.has(s.id) ? " is-pinned" : ""}`}
                    role="button"
                    tabIndex={0}
                    onClick={() => (editingSid === s.id ? undefined : selectSession(s.id))}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && editingSid !== s.id) selectSession(s.id);
                    }}
                    onMouseEnter={(e) => onSessionEnter(s.id, e)}
                    onMouseLeave={onSessionLeave}
                    onContextMenu={sessionCtx(s)}
                  >
                    {editingSid === s.id ? (
                      <input
                        className="session-rename-input"
                        value={editTitle}
                        autoFocus
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) => setEditTitle(e.target.value)}
                        onBlur={commitRename}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") commitRename();
                          if (e.key === "Escape") setEditingSid(null);
                        }}
                      />
                    ) : (
                      <>
                        <span className="session-row-title">
                          {pinned.has(s.id) && <span className="session-pin-mark" title="已置顶">📌</span>}
                          {s.title}
                        </span>
                        <span className="session-row-preview">{previewText(s.id)}</span>
                        <button
                          type="button"
                          className="session-row-del"
                          title="删除会话（60s 内可撤销）"
                          aria-label={`删除 ${s.title}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            deleteMockSessionWithUndo(s.id);
                          }}
                        >
                          ×
                        </button>
                      </>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
      {previewMsg && preview && (
        <div className="session-preview-pop" style={{ left: preview.x, top: preview.y }}>
          <div className="session-preview-title">{previewMsg.title}</div>
          <div className="session-preview-ts">
            {lastTs(preview.sid)
              ? new Date(lastTs(preview.sid)).toLocaleString("zh-CN", { hour12: false })
              : "暂无消息"}
          </div>
        </div>
      )}
    </div>
  );
}
