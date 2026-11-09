import { FormEvent, useEffect, useState } from "react";
import { api, MatterSummary } from "../api";
import { StatusBadge } from "./StatusBadge";

export function MatterList() {
  const [matters, setMatters] = useState<MatterSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.matters().then(setMatters).catch((e: Error) => setError(e.message));
  useEffect(() => {
    load();
  }, []);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api.upload(new FormData(e.currentTarget));
      window.location.hash = `#/matters/${encodeURIComponent(res.matter_id)}`;
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page-narrow">
      <section className="card">
        <h1>New matter</h1>
        <p className="muted">
          Upload a <strong>synthetic</strong> record production (PDF). The pipeline OCRs and indexes it, then extracts a
          cited chronology for review.
        </p>
        <form className="upload" onSubmit={onSubmit}>
          <label>
            PDF
            <input name="file" type="file" accept="application/pdf" required />
          </label>
          <label>
            Matter ID <span className="muted">(optional)</span>
            <input name="matter_id" pattern="[A-Za-z0-9_.\-]{1,64}" placeholder="auto" />
          </label>
          <label>
            Title
            <input name="title" placeholder="e.g. Synthetic case 3" />
          </label>
          <label>
            Engine
            <select name="provider" defaultValue="oracle">
              <option value="anthropic">Claude (needs API key on the server)</option>
              <option value="oracle">Demo oracle (bundled synthetic cases only; not a model)</option>
            </select>
          </label>
          <button type="submit" disabled={busy}>
            {busy ? "Uploading…" : "Upload and run"}
          </button>
        </form>
        {error && <p className="error">{error}</p>}
      </section>

      <section className="card">
        <h2>Matters</h2>
        {matters === null ? (
          <p className="muted">Loading…</p>
        ) : matters.length === 0 ? (
          <p className="muted">No matters yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Matter</th>
                <th>Title</th>
                <th>Latest job</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {matters.map((m) => (
                <tr key={m.matter_id}>
                  <td>
                    <a href={`#/matters/${encodeURIComponent(m.matter_id)}`}>{m.matter_id}</a>
                  </td>
                  <td>{m.title}</td>
                  <td>{m.job ? <StatusBadge status={m.job.status} /> : "—"}</td>
                  <td className="muted">{m.created_at?.slice(0, 16).replace("T", " ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
