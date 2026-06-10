import clsx from "clsx";
import { familyColor, statusPillClass } from "@/lib/format";

export function StatusPill({ status }: { status: string | null | undefined }) {
  if (!status)
    return (
      <span className="pill-muted" data-testid="pill-status" data-status="none">
        —
      </span>
    );
  return (
    <span className={statusPillClass(status)} data-testid="pill-status" data-status={status}>
      {status}
    </span>
  );
}

export function VerdictPill({ verdict }: { verdict: string | null | undefined }) {
  if (!verdict)
    return (
      <span className="pill-muted" data-testid="pill-verdict" data-verdict="pending">
        pending
      </span>
    );
  return (
    <span className={statusPillClass(verdict)} data-testid="pill-verdict" data-verdict={verdict}>
      {verdict}
    </span>
  );
}

export function FamilyPill({ family }: { family: string | null | undefined }) {
  const color = familyColor(family);
  return (
    <span
      className={clsx("pill ring-1")}
      data-testid="pill-family"
      data-family={family ?? "none"}
      style={{
        backgroundColor: `${color}20`,
        color,
        borderColor: `${color}50`,
      }}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: color }} />
      {family ?? "—"}
    </span>
  );
}
