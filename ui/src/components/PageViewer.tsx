import { useEffect, useMemo, useState } from "react";
import { api, Page } from "../api";

export type Highlight = { page: number; start: number; end: number; quote?: string };

type Rect = { left: number; top: number; width: number; height: number };

/** Merge the boxes of words overlapping [start, end) into one rectangle per text line. */
export function highlightRects(page: Page, start: number, end: number): Rect[] {
  const hits = page.words.filter((w) => w.char_end > start && w.char_start < end);
  const lines: { y0: number; y1: number; x0: number; x1: number }[] = [];
  for (const w of hits) {
    const line = lines.find((l) => Math.abs(l.y0 - w.bbox.y0) < (w.bbox.y1 - w.bbox.y0) * 0.6);
    if (line) {
      line.x0 = Math.min(line.x0, w.bbox.x0);
      line.x1 = Math.max(line.x1, w.bbox.x1);
      line.y1 = Math.max(line.y1, w.bbox.y1);
    } else {
      lines.push({ ...w.bbox });
    }
  }
  const pad = 1.5;
  return lines.map((l) => ({
    left: ((l.x0 - pad) / page.width) * 100,
    top: ((l.y0 - pad) / page.height) * 100,
    width: ((l.x1 - l.x0 + 2 * pad) / page.width) * 100,
    height: ((l.y1 - l.y0 + 2 * pad) / page.height) * 100,
  }));
}

export function PageViewer({
  matterId,
  pageNo,
  totalPages,
  highlight,
  onNavigate,
}: {
  matterId: string;
  pageNo: number;
  totalPages: number;
  highlight: Highlight | null;
  onNavigate: (page: number) => void;
}) {
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setPage(null);
    setError(null);
    api
      .page(matterId, pageNo)
      .then((p) => live && setPage(p))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [matterId, pageNo]);

  const rects = useMemo(
    () => (page && highlight && highlight.page === pageNo ? highlightRects(page, highlight.start, highlight.end) : []),
    [page, highlight, pageNo],
  );

  return (
    <div className="viewer" data-testid="page-viewer">
      <div className="viewer-bar">
        <button onClick={() => onNavigate(pageNo - 1)} disabled={pageNo <= 1} aria-label="Previous page">
          ‹
        </button>
        <span data-testid="page-label">
          Page {pageNo} / {totalPages || "?"}
          {page?.bates && <span className="bates"> · {page.bates}</span>}
        </span>
        <button onClick={() => onNavigate(pageNo + 1)} disabled={!!totalPages && pageNo >= totalPages} aria-label="Next page">
          ›
        </button>
        {page && (
          <span className="viewer-meta">
            {page.source === "ocr" && <span className="badge badge-warn">OCR {page.ocr_confidence?.toFixed(0)}%</span>}
            {page.source === "none" && <span className="badge badge-bad">no text — needs review</span>}
            {page.duplicate_of && <span className="badge badge-neutral">duplicate of p{page.duplicate_of}</span>}
          </span>
        )}
      </div>
      {error && <p className="error">{error}</p>}
      <div className="page-frame">
        <img src={api.pageImage(matterId, pageNo)} alt={`Page ${pageNo}`} />
        {rects.map((r, i) => (
          <div
            key={i}
            className="hl"
            data-testid="highlight"
            style={{ left: `${r.left}%`, top: `${r.top}%`, width: `${r.width}%`, height: `${r.height}%` }}
          />
        ))}
      </div>
      {highlight?.quote && highlight.page === pageNo && (
        <blockquote className="quote" data-testid="cited-quote">
          “{highlight.quote}”
        </blockquote>
      )}
    </div>
  );
}
