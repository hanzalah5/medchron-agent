import { Issue, TriageStatus } from "../api";
import { Highlight } from "./PageViewer";
import { StatusBadge } from "./StatusBadge";

const TRIAGE: { status: TriageStatus; label: string }[] = [
  { status: "pursue", label: "Pursue" },
  { status: "expert", label: "Ask expert" },
  { status: "dismiss", label: "Dismiss" },
];

export function IssuesPanel({
  issues,
  onCite,
  onTriage,
}: {
  issues: Issue[];
  onCite: (h: Highlight) => void;
  onTriage: (issueId: string, status: TriageStatus, note: string) => void;
}) {
  const pending = issues.filter((i) => i.triage_status === "pending").length;
  return (
    <section className="card" data-testid="issues">
      <div className="card-head">
        <h2>Questions for expert review</h2>
        <span className="muted">
          {issues.length - pending}/{issues.length} triaged · raised by the issue analyst and checked by the verifier
        </span>
      </div>
      {issues.length === 0 && <p className="muted">The analyst recorded no supportable issues.</p>}
      <ul className="issues">
        {issues.map((i) => (
          <li key={i.issue_id} className={`issue tri-${i.triage_status}`} data-testid="issue">
            <div className="chips">
              <span className="badge badge-warn">{i.category.replaceAll("_", " ")}</span>
              <StatusBadge
                status={i.verdict === "supported" ? "accepted" : i.verdict === "partial" ? "medium" : "rejected"}
                label={`verifier: ${i.verdict}`}
              />
              {i.triage_status !== "pending" && <StatusBadge status="edited" label={`triage: ${i.triage_status}`} />}
            </div>
            <p className="issue-text">{i.description}</p>
            {i.verdict !== "supported" && i.verdict_reason && <p className="muted small">Verifier: {i.verdict_reason}</p>}
            <div className="chips">
              {i.citations.map((c) => (
                <button
                  key={c.span_id + c.char_start}
                  className="cite"
                  title={c.quote}
                  onClick={() => onCite({ page: c.page_no, start: c.char_start, end: c.char_end, quote: c.quote })}
                >
                  p{c.page_no}
                </button>
              ))}
            </div>
            <div className="actions">
              {TRIAGE.map((t) => (
                <button
                  key={t.status}
                  className={i.triage_status === t.status ? "on" : ""}
                  onClick={() => {
                    let note = "";
                    if (t.status === "dismiss") {
                      note = window.prompt("Reason for dismissing this issue (required):") ?? "";
                      if (!note.trim()) return;
                    }
                    onTriage(i.issue_id, t.status, note);
                  }}
                >
                  {t.label}
                </button>
              ))}
            </div>
            {i.triage_note && <p className="muted small">Note: {i.triage_note}</p>}
          </li>
        ))}
      </ul>
    </section>
  );
}
