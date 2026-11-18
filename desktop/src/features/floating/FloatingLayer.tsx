/**
 * FloatingLayer（§二 CardHost）：fixed 定位层，四向容器分桶渲染。
 * z 序：bottom 40 / side(left/right) 50 / top-overlay 60 / question 70 强制置顶。
 * 弹出 200ms EASE_OUT；关闭动画先行（closing=true 播出场动画）→ 超时兜底卸载。
 * question 独立置顶层渲染（分桶时排除，防双渲染）；Toast 挂 top 容器（5s 自动消失）。
 */

import { useEffect } from "react";
import { useFloatingStore } from "./floatingStore";
import type { FloatingCard } from "./floatingStore";
import { PermissionCard } from "./PermissionCard";
import { QuestionCard } from "./QuestionCard";
import { QueueMessageCard } from "./QueueMessageCard";
import { UndoCard } from "./UndoCard";
import { SettingsPopup } from "../settings/SettingsPopup";

function CardBody({ card }: { card: FloatingCard }) {
  switch (card.kind) {
    case "permission":
      return <PermissionCard cardId={card.id} />;
    case "question":
      return <QuestionCard cardId={card.id} />;
    case "queue":
      return <QueueMessageCard />;
    case "undo":
      return <UndoCard />;
    case "settings":
      return <SettingsPopup />;
  }
}

function ToastStack() {
  const toasts = useFloatingStore((st) => st.toasts);
  const dismissToast = useFloatingStore((st) => st.dismissToast);

  const ids = toasts.map((t) => t.id).join(",");
  useEffect(() => {
    if (toasts.length === 0) return;
    const timers = toasts.map((t) => window.setTimeout(() => dismissToast(t.id), 5000));
    return () => timers.forEach(clearTimeout);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids]);

  if (toasts.length === 0) return null;
  return (
    <div className="toast-stack" role="region" aria-label="通知">
      {toasts.map((t) => (
        <div key={t.id} className={`ToastCard level-${t.level}`}>
          <div className="toast-title">{t.title}</div>
          <div className="toast-message">{t.message}</div>
          <button type="button" className="toast-close" onClick={() => dismissToast(t.id)} aria-label="关闭">
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

export function FloatingLayer() {
  const cards = useFloatingStore((st) => st.cards);
  const removeCard = useFloatingStore((st) => st.removeCard);

  // closing 动画结束兜底卸载（CSS 动画 200ms；超时 400ms 双保险）
  const closingKey = cards.filter((c) => c.closing).map((c) => c.id).join(",");
  useEffect(() => {
    if (!closingKey) return;
    const ids = closingKey.split(",");
    const timers = ids.map((id) => window.setTimeout(() => removeCard(id), 400));
    return () => timers.forEach(clearTimeout);
  }, [closingKey, removeCard]);

  const bottom = cards.filter((c) => c.container === "bottom" && c.kind !== "question");
  const top = cards.filter((c) => c.container === "top");
  const left = cards.filter((c) => c.container === "left");
  const right = cards.filter((c) => c.container === "right");
  const questions = cards.filter((c) => c.kind === "question");

  const renderStack = (list: FloatingCard[]) =>
    list.map((c) => (
      <div key={`${c.kind}:${c.id}`} className={`floating-slot${c.closing ? " is-closing" : ""}`}>
        <CardBody card={c} />
      </div>
    ));

  return (
    <div className="FloatingLayer" aria-live="polite">
      <div className="floating-container fc-bottom">{renderStack(bottom)}</div>
      {left.length > 0 && <div className="floating-container fc-left">{renderStack(left)}</div>}
      {right.length > 0 && <div className="floating-container fc-right">{renderStack(right)}</div>}
      {top.length > 0 && <div className="floating-container fc-top">{renderStack(top)}</div>}
      {questions.length > 0 && <div className="floating-container fc-question">{renderStack(questions)}</div>}
      <ToastStack />
    </div>
  );
}
