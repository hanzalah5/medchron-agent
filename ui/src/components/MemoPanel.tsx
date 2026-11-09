import { api, Memo } from "../api";
import { Highlight } from "./PageViewer";
import { StatusBadge } from "./StatusBadge";

export function MemoPanel({
  matterId,
  memo,
  canDraft,
  blockers,
  drafting,
  onDraft,
  onApprove,
  onCite,
}: {
  matterId: string;
  memo: Memo | null;
  canDraft: boolean;
  blockers: string[];
  drafting: boolean;
  onDraft: () => void;
  onApprove: () => void;
  onCite: (h: Highlight) => void;
}) {
  const invalid = memo?.claims.filter((c) => !c.citation_valid).length ?? 0;
  return (
    <section className="card" data-testid="memo">
      <div className="card-head">
        <h2>Case memo</h2>
        {memo && <StatusBadge status={memo.status === "approved" ? "accepted" : "pending"} label={memo.status} />}
        <div className="actions" style={{ marginLeft: "auto" }}>
          <button onClick={onDraft} disabled={!canDraft || drafting} data-testid="draft-memo">
            {drafting ? "Drafting…" : memo ? "Redraft" : "Draft memo"}
          </button>
          {memo && memo.status === "draft" && (
            <button onClick={onApprove} disabled={invalid > 0} data-testid="approve-memo">
              Approve memo
            </button>
          )}
          {memo?.status === "approved" && (
            <a className="button" href={api.memoExportUrl(matterId, memo.memo_id)}>
              Export (.md)
            </a>
          )}
        </div>
      </div>
      {!canDraft && blockers.length > 0 && (
        <p className="muted small">Before drafting: {blockers.join(" · ")}</p>
      )}
      {memo && (
        <>
          {invalid > 0 && <p className="error">{invalid} passage(s) have citations that failed verification.</p>}
          <div className="memo-body" data-testid="memo-body">
            {memo.claims.map((c, i) => (
              <span key={i} className={c.citations.length ? "claim cited" : "claim"}>
                {c.text}
                {c.citations.map((s, k) => (
                  <button
                    key={k}
                    className={`cite ${c.citation_valid ? "" : "cite-bad"}`}
                    title={s.quote}
                    onClick={() => onCite({ page: s.page_no, start: s.char_start, end: s.char_end, quote: s.quote })}
                  >
                    p{s.page_no}
                  </button>
                ))}
              </span>
            ))}
          </div>
          {memo.approved_by && (
            <p className="muted small">
              Approved by {memo.approved_by} · {memo.approved_at?.slice(0, 16).replace("T", " ")}
            </p>
          )}
        </>
      )}
    </section>
  );
}
