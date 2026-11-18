/**
 * PermissionCard（§一 1 全语义）：
 * - 三档风险 danger/warn/info：配色/图标/默认焦点（拒绝为安全默认）/允许钮防误触延迟解锁（danger 2.5s/warn 0.8s/info 0）
 * - 作用域三档选择器：tool=仅本次（不传 auto_allow）/round/session（映射 auto_allow）
 * - 键盘：Enter 允许 · Esc 拒绝 · Ctrl+P 预览 toggle
 * - 防重复提交锁；裁决 mock 应答（live 模式 restApi 扩展 auto_allow 参数后接，联调波次）
 * - risk_level/preview_payload 为规格补充字段（ws.ts EventMap 收窄待联调波次），此处从 payload 宽松读取
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useStreamStore } from "../../stores/streamStore";
import { useSessionStore } from "../../stores/sessionStore";
import { useFloatingStore } from "./floatingStore";
import { restApi } from "../../api/rest";
import { isMockMode } from "../chat/mockDriver";

type Risk = "danger" | "warn" | "info";
const UNLOCK_MS: Record<Risk, number> = { danger: 2500, warn: 800, info: 0 };

interface Props {
  /** 卡 id = tool_call_id */
  cardId: string;
}

export function PermissionCard({ cardId }: Props) {
  const currentSid = useSessionStore((st) => st.currentSid);
  const perm = useStreamStore((st) => {
    const s = currentSid ? st.streams[currentSid] : undefined;
    return s?.permissions.find((p) => p.toolCallId === cardId) ?? null;
  });

  const closeCard = useFloatingStore((st) => st.closeCard);
  const consumePermission = useStreamStore((st) => st.consumePermission);

  const [scope, setScope] = useState<"tool" | "round" | "session">("tool");
  const [locked, setLocked] = useState(true); // 防误触：允许钮延迟解锁
  const [decided, setDecided] = useState(false); // 防重复提交锁
  const [previewOpen, setPreviewOpen] = useState(false);
  const denyRef = useRef<HTMLButtonElement>(null);
  const unlockTimer = useRef<number | null>(null);

  // F8：b 表顶层字段优先，arguments 兑底（联调确认后下版删兑底）
  const topRisk = perm?.riskLevel;
  const topPreview = perm?.previewPayload;
  const legacy = (perm?.arguments ?? null) as { risk_level?: string; preview_payload?: unknown } | null;
  const risk: Risk = topRisk === "danger" || topRisk === "warn" || topRisk === "info"
    ? topRisk
    : legacy?.risk_level === "danger" || legacy?.risk_level === "warn"
      ? (legacy.risk_level as Risk)
      : "info";
  const previewData = topPreview !== undefined ? topPreview : legacy?.preview_payload;
  const hasPreview = previewData != null;

  const argsText = useMemo(() => {
    try {
      return typeof perm?.arguments === "string" ? perm.arguments : JSON.stringify(perm?.arguments, null, 2) ?? "";
    } catch {
      return String(perm?.arguments);
    }
  }, [perm?.arguments]);

  // 安全默认：焦点在拒绝；danger/warn 档允许钮延迟解锁
  useEffect(() => {
    denyRef.current?.focus();
    setLocked(UNLOCK_MS[risk] > 0);
    setDecided(false);
    setPreviewOpen(false);
    if (unlockTimer.current !== null) clearTimeout(unlockTimer.current);
    if (UNLOCK_MS[risk] > 0) {
      unlockTimer.current = window.setTimeout(() => setLocked(false), UNLOCK_MS[risk]);
    }
    return () => {
      if (unlockTimer.current !== null) clearTimeout(unlockTimer.current);
    };
  }, [risk, cardId]);

  const decide = async (decision: "approve" | "deny") => {
    if (decided || !perm || !currentSid) return;
    setDecided(true);
    // 作用域映射（§一 1）：tool=仅本次不传 auto_allow / round=auto_allow:round / session=auto_allow:session+session_allow
    const body =
      scope === "round"
        ? { decision, auto_allow: "round" }
        : scope === "session"
          ? { decision, auto_allow: "session", session_allow: true }
          : { decision };
    if (isMockMode()) {
      // mock 应答（行为不变）
      await new Promise((r) => setTimeout(r, 150));
    } else {
      await restApi.decidePermission(perm.toolCallId, body);
    }
    consumePermission(currentSid, perm.toolCallId);
    closeCard(cardId);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (decided) return;
    if (e.key === "Enter" && !locked) {
      e.preventDefault();
      void decide("approve");
    } else if (e.key === "Escape") {
      e.preventDefault();
      void decide("deny");
    } else if (e.key === "p" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (hasPreview) setPreviewOpen((v) => !v);
    }
  };

  if (!perm) return null;

  return (
    <div
      className={`FloatingCard PermissionCard risk-${risk}`}
      role="dialog"
      aria-label="权限请求"
      onKeyDown={onKeyDown}
      tabIndex={-1}
    >
      <div className="perm-head">
        <span className="perm-risk-icon" aria-hidden>{risk === "danger" ? "⚠" : risk === "warn" ? "▲" : "ℹ"}</span>
        <span className="perm-tool-name">{perm.toolName}</span>
        <span className="perm-risk-badge">{risk === "danger" ? "高危" : risk === "warn" ? "注意" : "常规"}</span>
      </div>

      <pre className="perm-args">{argsText}</pre>

      {hasPreview && previewOpen && (
        <pre className="perm-preview">{"预览：" + JSON.stringify(previewData, null, 2)}</pre>
      )}

      <div className="perm-scope" role="radiogroup" aria-label="作用域">
        {(["tool", "round", "session"] as const).map((s) => (
          <button
            key={s}
            type="button"
            role="radio"
            aria-checked={scope === s}
            className={`perm-scope-btn${scope === s ? " is-active" : ""}`}
            onClick={() => setScope(s)}
            disabled={decided}
          >
            {s === "tool" ? "仅本次" : s === "round" ? "本轮" : "会话级"}
          </button>
        ))}
      </div>

      <div className="perm-actions">
        <button
          type="button"
          className="perm-btn perm-allow"
          disabled={locked || decided}
          onClick={() => void decide("approve")}
          title={locked && risk === "danger" ? "高危操作防误触延迟中" : undefined}
        >
          {locked ? `允许 (${Math.ceil(UNLOCK_MS[risk] / 1000)}s)` : "允许"}
        </button>
        <button ref={denyRef} type="button" className="perm-btn perm-deny" onClick={() => void decide("deny")} disabled={decided}>
          拒绝
        </button>
        {hasPreview && (
          <button type="button" className="perm-btn perm-preview" onClick={() => setPreviewOpen((v) => !v)}>
            {previewOpen ? "收起预览" : "预览"}
          </button>
        )}
      </div>
      <div className="perm-kbd-hint">Enter 允许 · Esc 拒绝{hasPreview ? " · Ctrl+P 预览" : ""}</div>
    </div>
  );
}
