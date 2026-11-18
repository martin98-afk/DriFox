/**
 * SkeletonCard：流式等待态骨架（T10 修正5：stream.started 后首 delta 前，追加列表末尾，高约 120px）。
 */

export function SkeletonCard() {
  return (
    <div className="skeleton-card" aria-label="生成中">
      <div className="skeleton-card-avatar" />
      <div className="skeleton-card-body">
        <div className="skeleton-card-line" style={{ width: "42%" }} />
        <div className="skeleton-card-line" style={{ width: "78%" }} />
        <div className="skeleton-card-line" style={{ width: "60%" }} />
        <div className="skeleton-card-shimmer" />
      </div>
    </div>
  );
}
