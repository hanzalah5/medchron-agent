const TONES: Record<string, string> = {
  completed: "ok",
  accepted: "ok",
  edited: "info",
  queued: "neutral",
  pending: "neutral",
  running: "info",
  completed_with_errors: "warn",
  budget_exceeded: "warn",
  rejected: "bad",
  failed: "bad",
  high: "ok",
  medium: "warn",
  low: "bad",
};

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  return <span className={`badge badge-${TONES[status] ?? "neutral"}`}>{label ?? status.replaceAll("_", " ")}</span>;
}
