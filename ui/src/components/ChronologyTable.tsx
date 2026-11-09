import { useState } from "react";
import { ChronEvent, Review, ReviewStatus } from "../api";
import { Highlight } from "./PageViewer";
import { StatusBadge } from "./StatusBadge";

type Filter = "all" | "pending" | "attention" | "rejected";

export function ChronologyTable({
  events,
  reviews,
  selected,
  onCite,
  onReview,
}: {
  events: ChronEvent[];
  reviews: Record<string, Review>;
  selected: string | null;
  onCite: (eventId: string, h: Highlight) => void;
  onReview: (eventId: string, status: ReviewStatus, extra?: { note?: string; summary?: string; date?: string }) => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [editing, setEditing] = useState<string | null>(null);

  const needsAttention = (e: ChronEvent) => e.confidence !== "high" || e.flags.length > 0;
  const statusOf = (e: ChronEvent): ReviewStatus => reviews[e.event_id]?.status ?? "pending";
  const shown = events.filter((e) =>
    filter === "all"
      ? true
      : filter === "pending"
        ? statusOf(e) === "pending"
        : filter === "attention"
          ? needsAttention(e)
          : statusOf(e) === "rejected",
  );
  const done = events.filter((e) => statusOf(e) !== "pending").length;

  return (
    <section className="card" data-testid="chronology">
      <div className="card-head">
        <h2>Chronology</h2>
        <span className="muted">
          {done}/{events.length} reviewed
        </span>
        <div className="seg" role="tablist">
          {(["all", "pending", "attention", "rejected"] as Filter[]).map((f) => (
            <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)} role="tab">
              {f === "attention" ? "needs attention" : f}
            </button>
          ))}
        </div>
      </div>
      <table className="table chron">
        <thead>
          <tr>
            <th>Date</th>
            <th>Type</th>
            <th>Event</th>
            <th>Sources</th>
            <th>Review</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((e) => {
            const r = reviews[e.event_id];
            const status = statusOf(e);
            return (
              <tr key={e.event_id} className={`${selected === e.event_id ? "sel" : ""} st-${status}`} data-testid="event-row">
                <td className="nowrap">
                  {r?.edited_date ?? e.date ?? <span className="muted">undated</span>}
                </td>
                <td>
                  <span className="type">{e.type.replace("_", " ")}</span>
                </td>
                <td>
                  {editing === e.event_id ? (
                    <EditForm
                      event={e}
                      review={r}
                      onCancel={() => setEditing(null)}
                      onSave={(summary, date, note) => {
                        onReview(e.event_id, "edited", { summary, date, note });
                        setEditing(null);
                      }}
                    />
                  ) : (
                    <>
                      <div className={status === "rejected" ? "struck" : ""}>{r?.edited_summary ?? e.summary}</div>
                      <div className="muted small">
                        {e.facility}
                        {e.provider ? ` · ${e.provider}` : ""}
                      </div>
                      <div className="chips">
                        {e.confidence !== "high" && <StatusBadge status={e.confidence} label={`${e.confidence} confidence`} />}
                        {e.flags.map((f) => (
                          <span key={f} className="badge badge-warn">
                            {f.replaceAll("_", " ")}
                          </span>
                        ))}
                        {r?.note && <span className="badge badge-neutral" title={r.note}>note</span>}
                      </div>
                    </>
                  )}
                </td>
                <td>
                  <div className="chips">
                    {e.sources.map((s) => (
                      <button
                        key={s.span_id}
                        className="cite"
                        title={s.quote}
                        data-testid="cite"
                        onClick={() => onCite(e.event_id, { page: s.page_no, start: s.char_start, end: s.char_end, quote: s.quote })}
                      >
                        p{s.page_no}
                      </button>
                    ))}
                  </div>
                </td>
                <td className="nowrap">
                  <StatusBadge status={status} />
                  <div className="actions">
                    <button title="Accept" aria-label="Accept" onClick={() => onReview(e.event_id, "accepted")}>
                      ✓
                    </button>
                    <button title="Edit" aria-label="Edit" onClick={() => setEditing(e.event_id)}>
                      ✎
                    </button>
                    <button
                      title="Reject"
                      aria-label="Reject"
                      onClick={() => {
                        const note = window.prompt("Reason for rejecting this event (required):");
                        if (note && note.trim()) onReview(e.event_id, "rejected", { note });
                      }}
                    >
                      ✕
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function EditForm({
  event,
  review,
  onSave,
  onCancel,
}: {
  event: ChronEvent;
  review?: Review;
  onSave: (summary: string, date: string, note: string) => void;
  onCancel: () => void;
}) {
  const [summary, setSummary] = useState(review?.edited_summary ?? event.summary);
  const [date, setDate] = useState(review?.edited_date ?? event.date ?? "");
  const [note, setNote] = useState(review?.note ?? "");
  return (
    <div className="edit">
      <input value={summary} onChange={(e) => setSummary(e.target.value)} maxLength={300} aria-label="Summary" />
      <input type="date" value={date} onChange={(e) => setDate(e.target.value)} aria-label="Date" />
      <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Reviewer note" aria-label="Note" />
      <div className="actions">
        <button onClick={() => onSave(summary, date, note)}>Save</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
