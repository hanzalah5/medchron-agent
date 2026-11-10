import { FormEvent, useEffect, useState } from "react";
import { api, ChronEvent, ChronologyDiff, Production, VersionInfo } from "../api";
import { Highlight } from "./PageViewer";

function eventLine(e: ChronEvent) {
  return `${e.date ?? "(undated)"} · ${e.type} · ${e.summary}`;
}

/** Productions received, the chronology versions they produced, and what changed between two versions. */
export function VersionsPanel({
  matterId,
  productions,
  provider,
  busy,
  onAdded,
  onCite,
}: {
  matterId: string;
  productions: Production[];
  provider: string;
  busy: boolean;
  onAdded: () => void;
  onCite: (h: Highlight) => void;
}) {
  const [versions, setVersions] = useState<VersionInfo[]>([]);
  const [from, setFrom] = useState<number | null>(null);
  const [to, setTo] = useState<number | null>(null);
  const [diff, setDiff] = useState<ChronologyDiff | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    api.versions(matterId).then((v) => {
      setVersions(v);
      if (v.length) {
        const last = v[v.length - 1].version;
        setTo(last);
        setFrom(last > 1 ? last - 1 : null);
      }
    }, (e: Error) => setError(e.message));
  }, [matterId, productions.length, busy]);

  useEffect(() => {
    if (to === null) return;
    api.diff(matterId, from, to).then(setDiff, (e: Error) => setError(e.message));
  }, [matterId, from, to]);

  async function upload(ev: FormEvent<HTMLFormElement>) {
    ev.preventDefault();
    const form = new FormData(ev.currentTarget);
    setUploading(true);
    setError(null);
    try {
      await api.addProduction(matterId, form);
      onAdded();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
    }
  }

  return (
    <>
      <section className="card" data-testid="productions" aria-labelledby="productions-title">
        <h2 id="productions-title">Productions</h2>
        <table className="table">
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">Pages</th>
              <th scope="col">Received</th>
              <th scope="col">By</th>
            </tr>
          </thead>
          <tbody>
            {productions.map((p) => (
              <tr key={p.production_no}>
                <td>{p.production_no}</td>
                <td>
                  {p.page_count ? `p${p.page_offset + 1}–${p.page_offset + p.page_count}` : "processing"}
                </td>
                <td className="nowrap">{p.created_at?.slice(0, 16).replace("T", " ")}</td>
                <td>{p.created_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <form className="inline-form" onSubmit={upload} aria-label="Add a production">
          <label>
            Later production (PDF) <input type="file" name="file" accept="application/pdf" required />
          </label>
          <input type="hidden" name="provider" value={provider} />
          <button type="submit" disabled={uploading || busy} data-testid="add-production">
            {uploading ? "Uploading…" : "Add production"}
          </button>
        </form>
        <p className="muted small">
          The whole matter is re-run. Documents already processed are reused, and the chronology gets a new
          version. Reviewer decisions on unchanged events are kept.
        </p>
      </section>

      <section className="card" data-testid="versions" aria-labelledby="versions-title">
        <div className="card-head">
          <h2 id="versions-title">Chronology versions</h2>
          {versions.length > 0 && (
            <div className="inline-form">
              <label>
                From{" "}
                <select
                  value={from ?? ""}
                  onChange={(e) => setFrom(e.target.value ? Number(e.target.value) : null)}
                  data-testid="diff-from"
                >
                  <option value="">(nothing)</option>
                  {versions.map((v) => (
                    <option key={v.version} value={v.version}>
                      v{v.version}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                To{" "}
                <select value={to ?? ""} onChange={(e) => setTo(Number(e.target.value))} data-testid="diff-to">
                  {versions.map((v) => (
                    <option key={v.version} value={v.version}>
                      v{v.version}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}
        </div>
        {error && <p className="error">{error}</p>}
        <ul className="signals">
          {versions.map((v) => (
            <li key={v.version}>
              <strong>v{v.version}</strong> · {v.events} events · {v.productions} production(s)
              {v.diff && ` · +${v.diff.added} added, ${v.diff.changed} changed, −${v.diff.removed} removed`}
            </li>
          ))}
        </ul>
        {diff && (
          <div data-testid="diff">
            <h3>
              {diff.from_version ? `v${diff.from_version} → v${diff.to_version}` : `v${diff.to_version}`}:{" "}
              {diff.added.length} added, {diff.changed.length} changed, {diff.removed.length} removed,{" "}
              {diff.unchanged} unchanged
            </h3>
            <ul className="diff">
              {diff.added.map((e) => (
                <li key={`a${e.event_id}`} className="diff-added" data-testid="diff-added">
                  <span className="badge badge-ok">added</span> {eventLine(e)}{" "}
                  <button className="cite" onClick={() => onCite({ page: e.sources[0].page_no, start: e.sources[0].char_start, end: e.sources[0].char_end, quote: e.sources[0].quote })}>
                    p{e.sources[0].page_no}
                  </button>
                </li>
              ))}
              {diff.changed.map((c) => (
                <li key={`c${c.new.event_id}`} className="diff-changed" data-testid="diff-changed">
                  <span className="badge badge-warn">changed: {c.fields.join(", ")}</span> {eventLine(c.new)}
                  {c.added_pages.length > 0 && ` (new source on p${c.added_pages.join(", p")})`}
                </li>
              ))}
              {diff.removed.map((e) => (
                <li key={`r${e.event_id}`} className="diff-removed" data-testid="diff-removed">
                  <span className="badge badge-bad">removed</span> <s>{eventLine(e)}</s>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </>
  );
}
