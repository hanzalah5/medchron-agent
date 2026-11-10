import { useCallback, useEffect, useState } from "react";
import { api, Chronology, Issue, Job, MatterDetail, Me, Memo, ReviewState, ReviewStatus, TriageStatus } from "../api";
import { ChronologyTable } from "./ChronologyTable";
import { ExportPanel } from "./ExportPanel";
import { GapsPanel } from "./GapsPanel";
import { ProvidersPanel } from "./ProvidersPanel";
import { VersionsPanel } from "./VersionsPanel";
import { IssuesPanel } from "./IssuesPanel";
import { MemoPanel } from "./MemoPanel";
import { Highlight, PageViewer } from "./PageViewer";
import { StatusBadge } from "./StatusBadge";

const TABS = ["Review", "Providers", "Gaps", "Versions", "Export"] as const;
type Tab = (typeof TABS)[number];

export function MatterView({ matterId, me }: { matterId: string; me: Me }) {
  const [tab, setTab] = useState<Tab>("Review");
  const [detail, setDetail] = useState<MatterDetail | null>(null);
  const [chron, setChron] = useState<Chronology | null>(null);
  const [review, setReview] = useState<ReviewState | null>(null);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [memo, setMemo] = useState<Memo | null>(null);
  const [progress, setProgress] = useState<string[]>([]);
  const [page, setPage] = useState(1);
  const [highlight, setHighlight] = useState<Highlight | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    try {
      const d = await api.matter(matterId);
      setDetail(d);
      const job = d.jobs.find((j) => j.kind !== "memo");
      if (job && !["queued", "running"].includes(job.status)) {
        const [c, r, i, m] = await Promise.all([
          api.chronology(matterId),
          api.review(matterId),
          api.issues(matterId),
          api.memo(matterId),
        ]);
        setChron(c);
        setReview(r);
        setIssues(i);
        setMemo(m);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, [matterId]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // Stream job progress while the pipeline (or a memo draft) runs.
  const job: Job | undefined = detail?.jobs.find((j) => j.kind !== "memo");
  const memoJob: Job | undefined = detail?.jobs.find((j) => j.kind === "memo");
  const drafting = !!memoJob && ["queued", "running"].includes(memoJob.status);
  useEffect(() => {
    if (!memoJob || !drafting) return;
    const es = api.jobEvents(memoJob.job_id);
    es.onmessage = (msg) => {
      if (JSON.parse(msg.data).done) {
        es.close();
        loadAll();
      }
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, [memoJob?.job_id, drafting, loadAll]);
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

  async function onTriage(issueId: string, status: TriageStatus, note: string) {
    try {
      const updated = await api.triageIssue(matterId, issueId, { status, note });
      setIssues((prev) => prev.map((i) => (i.issue_id === issueId ? updated : i)));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function draftMemo() {
    try {
      await api.requestMemo(matterId, job?.provider ?? "anthropic");
      await loadAll();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function approveMemo() {
    if (!memo) return;
    try {
      setMemo(await api.approveMemo(matterId, memo.memo_id));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function confirmIndex() {
    const r = await api.confirmIndex(matterId);
    setReview((prev) => (prev ? { ...prev, ...r } : prev));
  }

  async function approveChronology() {
    try {
      const approval = await api.approveChronology(matterId);
      setReview((prev) => (prev ? { ...prev, chronology_approval: approval } : prev));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function refreshReview() {
    try {
      setReview(await api.review(matterId));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function cite(eventId: string | null, h: Highlight) {
    setSelected(eventId);
    setHighlight(h);
    setPage(h.page);
  }

  const totalPages = detail?.pages ?? 0;
  const untriaged = issues.filter((i) => i.triage_status === "pending").length;
  const blockers = [
    ...(review?.index_confirmed_at ? [] : ["confirm the document index"]),
    ...(untriaged ? [`triage ${untriaged} issue(s)`] : []),
  ];
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
          <button className="button" onClick={() => { setTab("Export"); refreshReview(); }} data-testid="export">
            Export…
          </button>
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
        <div className="tabs" role="tablist" aria-label="Matter views">
          {TABS.map((t) => (
            <button
              key={t}
              role="tab"
              id={`tab-${t}`}
              aria-selected={tab === t}
              aria-controls={`panel-${t}`}
              className={tab === t ? "tab tab-active" : "tab"}
              onClick={() => {
                setTab(t);
                if (t === "Export") refreshReview();
              }}
              data-testid={`tab-${t.toLowerCase()}`}
            >
              {t}
            </button>
          ))}
        </div>
      )}

      {chron && review && (
        <div className="split">
          <div className="left" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
            {tab === "Providers" && <ProvidersPanel matterId={matterId} onCite={(h) => cite(null, h)} />}
            {tab === "Gaps" && <GapsPanel matterId={matterId} />}
            {tab === "Versions" && detail && (
              <VersionsPanel
                matterId={matterId}
                productions={detail.productions}
                provider={job?.provider ?? "anthropic"}
                busy={!!job && ["queued", "running"].includes(job.status)}
                onAdded={() => {
                  setProgress([]);
                  loadAll();
                }}
                onCite={(h) => cite(null, h)}
              />
            )}
            {tab === "Export" && (
              <ExportPanel
                matterId={matterId}
                approval={review.chronology_approval}
                indexConfirmed={!!review.index_confirmed_at}
                canApprove={me.roles.includes("attorney")}
                onApprove={approveChronology}
              />
            )}
            {tab === "Review" && (<>
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

            <IssuesPanel issues={issues} onCite={(h) => cite(null, h)} onTriage={onTriage} />

            <ChronologyTable
              events={chron.events}
              reviews={review.events}
              selected={selected}
              onCite={cite}
              onReview={onReview}
            />

            <MemoPanel
              matterId={matterId}
              memo={memo}
              canDraft={blockers.length === 0}
              blockers={blockers}
              drafting={drafting}
              onDraft={draftMemo}
              onApprove={approveMemo}
              onCite={(h) => cite(null, h)}
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
            </>)}
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
