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
  production: number;
  bates: string;
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
export type Production = {
  production_no: number;
  sha256: string;
  page_offset: number;
  page_count: number;
  created_by: string;
  created_at: string | null;
};
export type MatterDetail = {
  matter_id: string;
  title: string;
  pages: number;
  jobs: Job[];
  productions: Production[];
};

export type Approval = {
  version: number | null;
  approved_by: string | null;
  approved_at: string | null;
  current: boolean;
};
export type VersionInfo = {
  version: number;
  created_at: string | null;
  job_id: string | null;
  productions: number;
  events: number;
  diff: { added: number; changed: number; removed: number } | null;
};
export type EventChange = { old: ChronEvent; new: ChronEvent; fields: string[]; added_pages: number[] };
export type ChronologyDiff = {
  from_version: number | null;
  to_version: number | null;
  added: ChronEvent[];
  removed: ChronEvent[];
  changed: EventChange[];
  unchanged: number;
};
export type ProviderEntry = {
  facility: string;
  provider: string;
  first_date: string | null;
  last_date: string | null;
  documents: number;
  events: number;
  pages: number[];
  bates_ranges: string[];
};
export type Interval = {
  from_event: string;
  to_event: string;
  from_date: string;
  to_date: string;
  days: number;
  from_summary: string;
  to_summary: string;
  from_facility: string;
  to_facility: string;
};
export type TreatmentGaps = {
  min_days: number;
  encounters: number;
  intervals: Interval[];
  followup: Signal[];
};
export type ExportFormat = "docx" | "xlsx" | "md";

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
  chronology_approval: Approval;
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

export type Me = { user_id: string; roles: string[] };

export const api = {
  me: async (): Promise<Me | null> => {
    const r = await fetch("/api/me");
    return r.status === 401 ? null : json<Me>(r);
  },
  matters: () => fetch("/api/matters").then((r) => json<MatterSummary[]>(r)),
  matter: (id: string) => fetch(`/api/matters/${enc(id)}`).then((r) => json<MatterDetail>(r)),
  chronology: (id: string) => fetch(`/api/matters/${enc(id)}/chronology`).then((r) => json<Chronology>(r)),
  review: (id: string) => fetch(`/api/matters/${enc(id)}/review`).then((r) => json<ReviewState>(r)),
  page: (id: string, n: number) => fetch(`/api/matters/${enc(id)}/pages/${n}`).then((r) => json<Page>(r)),
  pageImage: (id: string, n: number) => `/api/matters/${enc(id)}/pages/${n}/image?dpi=110`,
  exportUrl: (id: string, fmt: ExportFormat = "md") => `/api/matters/${enc(id)}/chronology/export.${fmt}`,
  approveChronology: (id: string) =>
    fetch(`/api/matters/${enc(id)}/chronology/approve`, { method: "POST" }).then((r) => json<Approval>(r)),
  versions: (id: string) => fetch(`/api/matters/${enc(id)}/chronology/versions`).then((r) => json<VersionInfo[]>(r)),
  diff: (id: string, from: number | null, to: number) => {
    const q = new URLSearchParams({ to: String(to) });
    if (from !== null) q.set("from", String(from));
    return fetch(`/api/matters/${enc(id)}/chronology/diff?${q}`).then((r) => json<ChronologyDiff>(r));
  },
  providers: (id: string) => fetch(`/api/matters/${enc(id)}/providers`).then((r) => json<ProviderEntry[]>(r)),
  gaps: (id: string, minDays: number) =>
    fetch(`/api/matters/${enc(id)}/gaps?min_days=${minDays}`).then((r) => json<TreatmentGaps>(r)),
  addProduction: (id: string, form: FormData) =>
    fetch(`/api/matters/${enc(id)}/productions`, { method: "POST", body: form }).then((r) =>
      json<{ matter_id: string; production_no: number; job_id: string }>(r),
    ),
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
