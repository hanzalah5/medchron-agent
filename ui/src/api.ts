// Typed client for the MedChron API.

export type EventSource = {
  span_id: string;
  page_no: number;
  quote: string;
  char_start: number;
  char_end: number;
  match_score: number;
};

export type ChronEvent = {
  event_id: string;
  date: string | null;
  date_text: string;
  type: string;
  summary: string;
  facility: string;
  provider: string;
  doc_id: string;
  sources: EventSource[];
  confidence: "high" | "medium" | "low";
  flags: string[];
};

export type DocumentSegment = {
  doc_id: string;
  doc_type: string;
  title: string;
  date: string | null;
  facility: string;
  provider: string;
  start_page: number;
  end_page: number;
};

export type CaseFlag = { kind: string; page_no: number | null; span_id: string | null; note: string };
export type Signal = { description: string; event_ids: string[] };

export type Chronology = {
  case_id: string;
  documents: DocumentSegment[];
  events: ChronEvent[];
  conflicts: Signal[];
  gaps: Signal[];
  flags: CaseFlag[];
};

export type Word = {
  text: string;
  bbox: { x0: number; y0: number; x1: number; y1: number };
  char_start: number;
  char_end: number;
};

export type Page = {
  page_no: number;
  text: string;
  words: Word[];
  source: "text_layer" | "ocr" | "none";
  ocr_confidence: number | null;
  width: number;
  height: number;
  duplicate_of: number | null;
};

export type Job = {
  job_id: string;
  matter_id: string;
  status: string;
  provider: string;
  kind: "pipeline" | "memo";
  error: string | null;
  summary: Record<string, unknown> | null;
  created_at: string | null;
  finished_at: string | null;
};

export type MatterSummary = { matter_id: string; title: string; created_at: string | null; job: Job | null };
export type MatterDetail = { matter_id: string; title: string; pages: number; jobs: Job[] };

export type ReviewStatus = "pending" | "accepted" | "edited" | "rejected";
export type Review = {
  event_id: string;
  status: ReviewStatus;
  note: string;
  edited_summary: string | null;
  edited_date: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
};
export type ReviewState = {
  index_confirmed_by: string | null;
  index_confirmed_at: string | null;
  events: Record<string, Review>;
};

export type IssueCitation = { span_id: string; page_no: number; quote: string; char_start: number; char_end: number };
export type TriageStatus = "pending" | "pursue" | "expert" | "dismiss";
export type Issue = {
  issue_id: string;
  category: string;
  description: string;
  event_ids: string[];
  citations: IssueCitation[];
  severity_hint: string;
  verdict: string;
  verdict_reason: string;
  triage_status: TriageStatus;
  triage_note: string;
  triaged_by: string | null;
};
export type MemoClaim = { text: string; citations: IssueCitation[]; citation_valid: boolean };
export type Memo = {
  memo_id: string;
  status: "draft" | "approved";
  markdown: string;
  claims: MemoClaim[];
  created_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
};

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

const enc = encodeURIComponent;

export const api = {
  matters: () => fetch("/api/matters").then((r) => json<MatterSummary[]>(r)),
  matter: (id: string) => fetch(`/api/matters/${enc(id)}`).then((r) => json<MatterDetail>(r)),
  chronology: (id: string) => fetch(`/api/matters/${enc(id)}/chronology`).then((r) => json<Chronology>(r)),
  review: (id: string) => fetch(`/api/matters/${enc(id)}/review`).then((r) => json<ReviewState>(r)),
  page: (id: string, n: number) => fetch(`/api/matters/${enc(id)}/pages/${n}`).then((r) => json<Page>(r)),
  pageImage: (id: string, n: number) => `/api/matters/${enc(id)}/pages/${n}/image?dpi=110`,
  exportUrl: (id: string) => `/api/matters/${enc(id)}/export.md`,
  upload: (form: FormData) =>
    fetch("/api/matters", { method: "POST", body: form }).then((r) =>
      json<{ matter_id: string; job_id: string }>(r),
    ),
  reviewEvent: (
    id: string,
    eventId: string,
    body: { status: ReviewStatus; note?: string; summary?: string | null; date?: string | null },
  ) =>
    fetch(`/api/matters/${enc(id)}/events/${enc(eventId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => json<Review>(r)),
  confirmIndex: (id: string) =>
    fetch(`/api/matters/${enc(id)}/index/confirm`, { method: "POST" }).then((r) =>
      json<{ index_confirmed_by: string; index_confirmed_at: string }>(r),
    ),
  issues: (id: string) => fetch(`/api/matters/${enc(id)}/issues`).then((r) => json<Issue[]>(r)),
  triageIssue: (id: string, issueId: string, body: { status: TriageStatus; note?: string }) =>
    fetch(`/api/matters/${enc(id)}/issues/${enc(issueId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => json<Issue>(r)),
  requestMemo: (id: string, provider: string) =>
    fetch(`/api/matters/${enc(id)}/memo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider }),
    }).then((r) => json<{ job_id: string }>(r)),
  memo: async (id: string): Promise<Memo | null> => {
    const r = await fetch(`/api/matters/${enc(id)}/memo`);
    return r.status === 404 ? null : json<Memo>(r);
  },
  approveMemo: (id: string, memoId: string) =>
    fetch(`/api/matters/${enc(id)}/memo/${enc(memoId)}/approve`, { method: "POST" }).then((r) => json<Memo>(r)),
  memoExportUrl: (id: string, memoId: string) => `/api/matters/${enc(id)}/memo/${enc(memoId)}/export.md`,
  jobEvents: (jobId: string) => new EventSource(`/api/jobs/${enc(jobId)}/events`),
};
