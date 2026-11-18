/**
 * QuestionCard（§一 2）：
 * questions[{label,description?,options[],multiple?}] 渲染；单选/多选高亮；自定义答案输入；
 * 多题分页导航；跳过（answers 空）/提交 → mock 应答 + consumeQuestion（live 走 REST 联调波次）。
 */

import { useEffect, useMemo, useState } from "react";
import { useStreamStore } from "../../stores/streamStore";
import { useSessionStore } from "../../stores/sessionStore";
import { useFloatingStore } from "./floatingStore";
import { restApi } from "../../api/rest";
import { isMockMode } from "../chat/mockDriver";

interface QuestionSpec {
  label: string;
  description?: string;
  options?: string[];
  multiple?: boolean;
}

interface Props {
  /** 卡 id = question id */
  cardId: string;
}

export function QuestionCard({ cardId }: Props) {
  const currentSid = useSessionStore((st) => st.currentSid);
  const q = useStreamStore((st) => {
    const s = currentSid ? st.streams[currentSid] : undefined;
    return s?.questions.find((x) => x.id === cardId) ?? null;
  });

  const closeCard = useFloatingStore((st) => st.closeCard);
  const consumeQuestion = useStreamStore((st) => st.consumeQuestion);

  const specs = useMemo<QuestionSpec[]>(() => {
    const raw = (q?.questions ?? []) as unknown;
    return Array.isArray(raw) ? (raw as QuestionSpec[]) : [];
  }, [q?.questions]);

  const [page, setPage] = useState(0);
  /** answers[qIndex] = 选中的 option 数组（单选至多 1 项） */
  const [answers, setAnswers] = useState<Record<number, string[]>>({});
  const [custom, setCustom] = useState<Record<number, string>>({});
  const [submitted, setSubmitted] = useState(false);

  const total = specs.length;
  const spec = specs[page];

  useEffect(() => {
    setPage(0);
    setAnswers({});
    setCustom({});
    setSubmitted(false);
  }, [cardId]);

  const toggleOption = (qi: number, opt: string, multiple: boolean) => {
    setAnswers((prev) => {
      const cur = prev[qi] ?? [];
      if (multiple) {
        return { ...prev, [qi]: cur.includes(opt) ? cur.filter((x) => x !== opt) : [...cur, opt] };
      }
      return { ...prev, [qi]: cur.includes(opt) ? [] : [opt] };
    });
  };

  const addCustom = (qi: number) => {
    const v = (custom[qi] ?? "").trim();
    if (!v) return;
    setAnswers((prev) => ({ ...prev, [qi]: [...(prev[qi] ?? []), v] }));
    setCustom((prev) => ({ ...prev, [qi]: "" }));
  };

  const submit = async (skip: boolean) => {
    if (submitted || !q || !currentSid) return;
    setSubmitted(true);
    // 收集按题序展平的选中项；跳过 = answers 空数组（§一 2 语义）
    const collected = skip
      ? []
      : specs.flatMap((_, qi) => answers[qi] ?? []);
    if (isMockMode()) {
      // mock 应答（行为不变）
      await new Promise((r) => setTimeout(r, 150));
    } else {
      await restApi.answerQuestion(q.id, collected);
    }
    consumeQuestion(currentSid, q.id);
    closeCard(cardId);
  };

  if (!q || total === 0) return null;

  const selected = answers[page] ?? [];

  return (
    <div className="FloatingCard QuestionCard" role="dialog" aria-label="助手提问">
      <div className="question-head">
        <span className="question-badge">提问</span>
        {total > 1 && <span className="question-pager-ind">{page + 1}/{total}</span>}
      </div>

      <div className="question-body">
        <div className="question-label">{spec.label}</div>
        {spec.description && <div className="question-desc">{spec.description}</div>}

        <div className="question-options" role={spec.multiple ? "group" : "radiogroup"}>
          {(spec.options ?? []).map((opt) => {
            const active = selected.includes(opt);
            return (
              <button
                key={opt}
                type="button"
                className={`question-option${active ? " is-active" : ""}`}
                onClick={() => toggleOption(page, opt, spec.multiple === true)}
                disabled={submitted}
              >
                <span className={`question-check${spec.multiple ? " is-multi" : ""}`} aria-hidden>
                  {spec.multiple ? (active ? "☑" : "☐") : active ? "◉" : "○"}
                </span>
                {opt}
              </button>
            );
          })}
        </div>

        <div className="question-custom">
          <input
            type="text"
            className="question-custom-input"
            placeholder="自定义答案（回车添加）"
            value={custom[page] ?? ""}
            onChange={(e) => setCustom((prev) => ({ ...prev, [page]: e.target.value }))}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                e.stopPropagation();
                addCustom(page);
              }
            }}
          />
        </div>
      </div>

      <div className="question-footer">
        <div className="question-nav">
          <button type="button" className="question-nav-btn" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
            上一题
          </button>
          <button
            type="button"
            className="question-nav-btn"
            disabled={page >= total - 1}
            onClick={() => setPage((p) => p + 1)}
          >
            下一题
          </button>
        </div>
        <div className="question-actions">
          <button type="button" className="question-skip" onClick={() => void submit(true)} disabled={submitted}>
            跳过
          </button>
          <button type="button" className="question-submit" onClick={() => void submit(false)} disabled={submitted}>
            提交
          </button>
        </div>
      </div>
    </div>
  );
}
