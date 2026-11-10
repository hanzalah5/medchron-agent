import { useEffect, useState } from "react";
import { api, TreatmentGaps } from "../api";

/** Gaps in treatment: long intervals between encounters, and directed follow-ups with no record. */
export function GapsPanel({ matterId }: { matterId: string }) {
  const [minDays, setMinDays] = useState(30);
  const [data, setData] = useState<TreatmentGaps | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const handle = setTimeout(() => {
      api.gaps(matterId, minDays).then(setData, (e: Error) => setError(e.message));
    }, 200);
    return () => clearTimeout(handle);
  }, [matterId, minDays]);
  return (
    <section className="card" data-testid="gaps" aria-labelledby="gaps-title">
      <div className="card-head">
        <h2 id="gaps-title">Gaps in treatment</h2>
        <label className="small">
          Minimum gap (days){" "}
          <input
            type="number"
            min={1}
            max={3650}
            value={minDays}
            onChange={(e) => setMinDays(Math.max(1, Number(e.target.value) || 1))}
            data-testid="gap-days"
          />
        </label>
      </div>
      {error && <p className="error">{error}</p>}
      {data && (
        <>
          <p className="muted small">
            {data.encounters} encounters in the chronology. Intervals of {data.min_days} days or more between
            consecutive encounters are listed below. An interval is a place to look, not a finding.
          </p>
          {data.intervals.length === 0 ? (
            <p className="muted">No intervals of {data.min_days} days or more.</p>
          ) : (
            <ul className="signals">
              {data.intervals.map((i) => (
                <li key={`${i.from_event}-${i.to_event}`} data-testid="gap-interval">
                  <span className="badge badge-warn">{i.days} days</span> {i.from_date} {i.from_summary} (
                  {i.from_facility}) → {i.to_date} {i.to_summary} ({i.to_facility})
                </li>
              ))}
            </ul>
          )}
          <h3>Directed follow-up with no later record</h3>
          {data.followup.length === 0 ? (
            <p className="muted">None found.</p>
          ) : (
            <ul className="signals">
              {data.followup.map((g, k) => (
                <li key={k}>
                  <span className="badge badge-warn">possible missing records</span> {g.description}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
