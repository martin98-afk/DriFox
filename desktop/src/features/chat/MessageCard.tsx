/**
 * MessageCard（T10 + 勘误）：单条消息卡。
 * - meta/footer bar 仅助手消息有，位置=卡片内部底部（分割线下方）：左「耗时 · tok/s」，右「模型名+分支+复制」
 * - 用户消息无 meta；时间戳在头像侧以弱化小字承载（muted 11px 无胶囊）；助手时间戳带胶囊底
 * - 用户 card-body 反转（row-reverse），气泡 --bubble-user，字母圆头像居右
 */

import { memo } from "react";
import type { ChatMessage } from "./blocks";
import { MessageBlocks } from "./BlockRenderer";
import { useContextMenuStore } from "../shell/contextMenuStore";
import type { CtxMenuItem } from "../shell/contextMenuStore";
import { useFloatingStore } from "../floating/floatingStore";

/** 成员字母头像配色（L橙/P红/B蓝/R青/F粉 系） */
const AVATAR_COLORS = ["#e8963e", "#e5534b", "#4c8dff", "#2ec7c7", "#e07ba0"];

function userAvatarColor(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function msgText(msg: ChatMessage): string {
  if (msg.text) return msg.text;
  return (msg.blocks ?? []).map((b) => (b.type === "text" ? b.content : "")).join("\n");
}

function fmtTs(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function copyMessage(msg: ChatMessage): void {
  void navigator.clipboard.writeText(msgText(msg)).catch(() => {});
}

/** 分支小图标（对齐截图 line-svg 语义） */
function BranchIcon() {
  return (
    <svg className="icon-branch" viewBox="0 0 16 16" width="13" height="13" aria-hidden>
      <circle cx="4" cy="3.5" r="1.8" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="4" cy="12.5" r="1.8" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="12" cy="6" r="1.8" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path d="M4 5.3v5.4M4 8h4.5a2.5 2.5 0 0 0 2.5-2V7.8" fill="none" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}

interface Props {
  msg: ChatMessage;
  index: number;
}

export const MessageCard = memo(function MessageCard({ msg }: Props) {
  const isUser = msg.role === "user";
  const avatarColor = userAvatarColor("review");

  // message 右键（§四）：复制正文/复制整卡 markdown/重新生成（mock）/引用（回填引用前缀）
  const onContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    const text = msgText(msg);
    const pushToast = useFloatingStore.getState().pushToast;
    const items: CtxMenuItem[] = [
      { key: "copy", label: "复制正文", icon: "⧉", action: () => void navigator.clipboard.writeText(text).catch(() => {}) },
      { key: "copy-md", label: "复制整卡 markdown", action: () => void navigator.clipboard.writeText(text).catch(() => {}) },
      { key: "regen", label: "重新生成", action: () => pushToast({ title: "重新生成", message: "已请求重新生成（mock）", level: "info" }) },
      {
        key: "quote",
        label: "引用",
        action: () =>
          window.dispatchEvent(
            new CustomEvent("drifox:queue-fill", { detail: { text: `> ${text.slice(0, 80)}${text.length > 80 ? "…" : ""}\n` } }),
          ),
      },
    ];
    useContextMenuStore.getState().openMenu(
      { x: e.clientX, y: e.clientY, target: "message", targetId: msg.id },
      items,
    );
  };

  if (isUser) {
    return (
      <article className="MessageCard is-user" onContextMenu={onContextMenu}>
        <div className="card-body is-reverse">
          <div className="content-col">
            <div className="user-bubble">{msg.text}</div>
          </div>
          <div className="avatar-col is-user-side">
            <span className="user-ts">{fmtTs(msg.ts)}</span>
            <span className="avatar avatar-letter" style={{ background: avatarColor }}>
              R
            </span>
          </div>
        </div>
      </article>
    );
  }

  return (
    <article className="MessageCard is-assistant" onContextMenu={onContextMenu}>
      <div className="card-body">
        <div className="avatar-col">
          <span className="avatar avatar-logo" aria-hidden>
            DF
          </span>
        </div>
        <div className="content-col">
          <div className="assistant-idrow">
            <span className="assistant-name">DriFox</span>
            <span className="assistant-ts">{fmtTs(msg.ts)}</span>
          </div>
          <MessageBlocks blocks={msg.blocks ?? []} home={msg.id} />
          <div className="msg-meta-row">
            <span className="meta-left">
              <span className="meta-elapsed">{msg.elapsedS != null ? `${msg.elapsedS}s` : "--s"}</span>
              <span className="meta-dot">·</span>
              <span className="meta-toks">{msg.tokPerS != null ? `${msg.tokPerS} tok/s` : "-- tok/s"}</span>
            </span>
            <span className="meta-right">
              <span className="meta-model">{msg.model ?? "glm-5.3-flash"}</span>
              <button className="icon-btn" title="分支" type="button">
                <BranchIcon />
              </button>
              <button className="icon-btn" title="复制" type="button" onClick={() => copyMessage(msg)}>
                <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden>
                  <rect x="5" y="5" width="8" height="9" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
                  <path d="M11 5V3.5A1.5 1.5 0 0 0 9.5 2h-5A1.5 1.5 0 0 0 3 3.5v7A1.5 1.5 0 0 0 4.5 12H5" fill="none" stroke="currentColor" strokeWidth="1.3" />
                </svg>
              </button>
            </span>
          </div>
        </div>
      </div>
    </article>
  );
});
