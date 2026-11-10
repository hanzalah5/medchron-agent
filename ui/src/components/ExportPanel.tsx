import { api, Approval, ExportFormat } from "../api";

const FORMATS: { fmt: ExportFormat; label: string }[] = [
  { fmt: "docx", label: "Word (.docx)" },
  { fmt: "xlsx", label: "Excel (.xlsx)" },
  { fmt: "md", label: "Markdown (.md)" },
];

/** Attorney approval gates every chronology export; any later review edit needs approval again. */
export function ExportPanel({
  matterId,
  approval,
  indexConfirmed,
  canApprove,
  onApprove,
}: {
  matterId: string;
  approval: Approval;
  indexConfirmed: boolean;
  canApprove: boolean;
  onApprove: () => void;
}) {
  return (
    <section className="card" data-testid="export-panel" aria-labelledby="export-title">
      <h2 id="export-title">Export the chronology</h2>
      <p className="muted small">
        Exports use the conventional layout (date, provider / facility, event, source with Bates number) and
        include reviewer edits. An attorney must approve the current version first. Every export is audited.
      </p>
      {approval.current ? (
        <p className="badge badge-ok" data-testid="approval-status">
          v{approval.version} approved by {approval.approved_by} · {approval.approved_at?.slice(0, 16).replace("T", " ")}
        </p>
      ) : (
        <p className="badge badge-warn" data-testid="approval-status">
          {approval.approved_by ? "Review changed after approval: approve again" : "Not approved for export"}
        </p>
      )}
      {!approval.current && (
        <div>
          <button
            onClick={onApprove}
            disabled={!canApprove || !indexConfirmed}
            data-testid="approve-chronology"
          >
            Approve v{approval.version ?? "?"} for export
          </button>
          {!indexConfirmed && <p className="muted small">Confirm the document index first.</p>}
          {!canApprove && <p className="muted small">Only an attorney can approve.</p>}
        </div>
      )}
      {approval.current && (
        <ul className="export-links">
          {FORMATS.map(({ fmt, label }) => (
            <li key={fmt}>
              <a className="button" href={api.exportUrl(matterId, fmt)} data-testid={`export-${fmt}`}>
                {label}
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
