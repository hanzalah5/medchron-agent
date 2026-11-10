import { useEffect, useState } from "react";
import { api, ProviderEntry } from "../api";
import { Highlight } from "./PageViewer";

/** Facility and clinician index: who appears in the records, when, and where (Bates ranges). */
export function ProvidersPanel({ matterId, onCite }: { matterId: string; onCite: (h: Highlight) => void }) {
  const [rows, setRows] = useState<ProviderEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.providers(matterId).then(setRows, (e: Error) => setError(e.message));
  }, [matterId]);
  return (
    <section className="card" data-testid="providers" aria-labelledby="providers-title">
      <h2 id="providers-title">Providers and facilities</h2>
      {error && <p className="error">{error}</p>}
      {!rows && !error && <p className="muted">Loading…</p>}
      {rows && rows.length === 0 && <p className="muted">No providers found.</p>}
      {rows && rows.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Facility</th>
              <th scope="col">Clinician</th>
              <th scope="col">First</th>
              <th scope="col">Last</th>
              <th scope="col">Documents</th>
              <th scope="col">Events</th>
              <th scope="col">Pages (Bates)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.facility}|${r.provider}`} data-testid="provider-row">
                <td>{r.facility}</td>
                <td>{r.provider}</td>
                <td className="nowrap">{r.first_date ?? "—"}</td>
                <td className="nowrap">{r.last_date ?? "—"}</td>
                <td>{r.documents}</td>
                <td>{r.events}</td>
                <td>
                  <button
                    className="cite"
                    onClick={() => onCite({ page: r.pages[0], start: 0, end: 0 })}
                    aria-label={`Open page ${r.pages[0]} for ${r.facility}`}
                  >
                    p{r.pages[0]}
                  </button>{" "}
                  <span className="small">{r.bates_ranges.join(", ")}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
