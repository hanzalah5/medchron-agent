import { useCallback, useEffect, useState } from "react";
import { api, Chronology, Job, MatterDetail, ReviewState, ReviewStatus } from "../api";
import { ChronologyTable } from "./ChronologyTable";
import { Highlight, PageViewer } from "./PageViewer";
import { StatusBadge } from "./StatusBadge";

export function MatterView({ matterId }: { matterId: string }) {
  const [detail, setDetail] = useState<MatterDetail | null>(null);
  const [chron, setChron] = useState<Chronology | null>(null);
  const [review, setReview] = useState<ReviewState | null>(null);
  const [progress, setProgress] = useState<string[]>([]);
  const [page, setPage] = useState(1);
  const [highlight, setHighlight] = useState<Highlight | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    try {
      const d = await api.matter(matterId);
      setDetail(d);
      const job = d.jobs[0];
      if (job && !["queued", "running"].includes(job.status)) {
        const [c, r] = await Promise.all([api.chronology(matterId), api.review(matterId)]);
        setChron(c);
        setReview(r);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, [matterId]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // Stream job progress while the pipeline runs.
  const job: Job | undefined = detail?.jobs[0];
  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    const es = api.jobEvents(job.job_id);
    es.onmessage = (msg) => {
      const data = JSON.parse(msg.data);
      if (data.message) setProgress((p) => [...p, data.message]);
      if (data.done) {
        es.close();
        loadAll();
      }
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, [job?.job_id, job?.status, loadAll]);

  async function onReview(eventId: string, status: ReviewStatus, extra?: { note?: string; summary?: string; date?: string }) {
    try {
      const r = await api.reviewEvent(matterId, eventId, {
        status,
        note: extra?.note ?? "",
        summary: extra?.summary ?? null,
        date: extra?.date || null,
      });
      setReview((prev) => (prev ? { ...prev, events: { ...prev.events, [eventId]: r } } : prev));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function confirmIndex() {
    const r = await api.confirmIndex(matterId);
    setReview((prev) => (prev ? { ...prev, ...r } : prev));
  }

  function cite(eventId: string | null, h: Highlight) {
    setSelected(eventId);
    setHighlight(h);
    setPage(h.page);
  }

  const totalPages = detail?.pages ?? 0;
  const pagesNeedingReview = chron?.flags.filter((f) => f.kind === "page_needs_review") ?? [];
  const otherFlags = chron?.flags.filter((f) => f.kind !== "page_needs_review") ?? [];

  return (
    <div className="matter">
      <div className="matter-head">
        <div>
          <a href="#/" className="muted small">
            ← All matters
          </a>
          <h1>{detail?.title || matterId}</h1>
          <div className="muted small">
            {matterId} · {totalPages} pages {job && <StatusBadge status={job.status} />}
            {job?.provider === "oracle" && <span className="badge badge-neutral">demo oracle (not a model)</span>}
          </div>
        </div>
        {chron && (
          <a className="button" href={api.exportUrl(matterId)} data-testid="export">
            Export draft (.md)
          </a>
        )}
      </div>
      {error && <p className="error">{error}</p>}

      {job && ["queued", "running"].includes(job.status) && (
        <section className="card" data-testid="progress">
          <h2>Processing…</h2>
          <ol className="progress">
            {progress.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ol>
        </section>
      )}
      {job?.status === "failed" && <p className="error">Job failed: {job.error}</p>}

      {chron && review && (
        <div className="split">
          <div className="left">
            <section className="card" data-testid="for-review">
              <h2>For review</h2>
              <ul className="signals">
                {chron.conflicts.map((c, i) => (
                  <li key={`c${i}`}>
                    <span className="badge badge-warn">date conflict</span> {c.description}
                  </li>
                ))}
                {chron.gaps.map((g, i) => (
                  <li key={`g${i}`}>
                    <span className="badge badge-warn">possible missing records</span> {g.description}
                  </li>
                ))}
                {otherFlags.map((f, i) => (
                  <li key={`f${i}`}>
                    <span className="badge badge-bad">{f.kind.replaceAll("_", " ")}</span> {f.note}{" "}
                    {f.page_no && (
                      <button className="cite" onClick={() => cite(null, { page: f.page_no!, start: 0, end: 0 })}>
                        p{f.page_no}
                      </button>
                    )}
                  </li>
                ))}
                {chron.conflicts.length + chron.gaps.length + otherFlags.length === 0 && (
                  <li className="muted">No review signals.</li>
                )}
              </ul>
            </section>

            <ChronologyTable
              events={chron.events}
              reviews={review.events}
              selected={selected}
              onCite={cite}
              onReview={onReview}
            />

            <section className="card" data-testid="doc-index">
              <div className="card-head">
                <h2>Document index</h2>
                {review.index_confirmed_at ? (
                  <span className="badge badge-ok">
                    confirmed by {review.index_confirmed_by} · {review.index_confirmed_at.slice(0, 16).replace("T", " ")}
                  </span>
                ) : (
                  <button onClick={confirmIndex} data-testid="confirm-index">
                    Confirm index
                  </button>
                )}
              </div>
              {pagesNeedingReview.length > 0 && (
                <p className="muted small">
                  Pages needing review:{" "}
                  {pagesNeedingReview.map((f) => (
                    <button key={f.page_no} className="cite" onClick={() => cite(null, { page: f.page_no!, start: 0, end: 0 })}>
                      p{f.page_no}
                    </button>
                  ))}
                </p>
              )}
              <table className="table">
                <thead>
                  <tr>
                    <th>Pages</th>
                    <th>Type</th>
                    <th>Title</th>
                    <th>Date</th>
                    <th>Facility</th>
                  </tr>
                </thead>
                <tbody>
                  {chron.documents.map((d) => (
                    <tr key={d.doc_id}>
                      <td>
                        <button className="cite" onClick={() => cite(null, { page: d.start_page, start: 0, end: 0 })}>
                          {d.start_page === d.end_page ? `p${d.start_page}` : `p${d.start_page}–${d.end_page}`}
                        </button>
                      </td>
                      <td>{d.doc_type.replaceAll("_", " ")}</td>
                      <td>{d.title}</td>
                      <td className="nowrap">{d.date}</td>
                      <td>{d.facility}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </div>
          <div className="right">
            <PageViewer
              matterId={matterId}
              pageNo={page}
              totalPages={totalPages}
              highlight={highlight}
              onNavigate={(n) => setPage(Math.max(1, Math.min(totalPages || n, n)))}
            />
          </div>
        </div>
      )}
    </div>
  );
}
