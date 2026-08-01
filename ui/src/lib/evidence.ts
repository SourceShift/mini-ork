/**
 * Honest presentation of measured rates from the dispatch composer.
 *
 * The backend (mini_ork/web/routes/dispatch.py) enforces the STATISTICAL half of
 * honesty: any lane/topology below `min_samples` runs comes back with
 * `evidence:"none"`, `success_rate:null`, and `ci:null`. A 75% from n=4 is a
 * coincidence with a percent sign, so the server refuses to ship it.
 *
 * This module enforces the PRESENTATION half: given that a rate may be absent, or
 * present-but-wide, decide what the operator actually sees. Three rules:
 *   1. evidence:"none" → NEVER a number. Show the raw sample ("2/3 · n<5") so the
 *      operator sees WHY there's no rate — the missing number is the signal.
 *   2. a measured rate always ships with its Wilson interval width, because a
 *      point estimate without its uncertainty invites belief the sample can't bear.
 *   3. a wide interval is flagged, so 60% [15–92%] doesn't read like 60% [58–62%].
 */

import type { DispatchLane, DispatchTopology } from "./api";

export type EvidenceTone = "measured" | "wide" | "none";

export type EvidenceView = {
  /** Headline the UI renders in place of a rate. Never a bare fabricated %. */
  label: string;
  /** Wilson interval as human text, or the reason there isn't one. */
  detail: string;
  /** Drives pill colour: measured=green-ish, wide=amber, none=muted. */
  tone: EvidenceTone;
  /** True only when a real, defensible rate exists. Gate "recommended" UI on this. */
  hasRate: boolean;
};

const pct = (x: number): string => `${Math.round(x * 100)}%`;

/** A Wilson interval wider than this spans too much to act on as a point rate. */
const WIDE_CI_THRESHOLD = 0.4;

/**
 * Turn a measured rate + Wilson CI into an operator-facing view.
 *
 * @param rate     success_rate / win_rate from the backend (null ⇒ thin sample)
 * @param ci       [lo, hi] Wilson 95% interval (null ⇒ thin sample)
 * @param k        successes / wins (shown when there's no rate)
 * @param n        sample size (shown when there's no rate)
 * @param minSamples the server's threshold, for the "n<5" hint
 */
export function evidenceView(
  rate: number | null,
  ci: [number, number] | null,
  k: number,
  n: number,
  minSamples: number,
): EvidenceView {
  // Rule 1: no rate → show the sample, never a number.
  if (rate == null || ci == null) {
    return {
      label: n > 0 ? `${k}/${n}` : "no runs",
      detail: n > 0 ? `too few to rate · n<${minSamples}` : "never dispatched here",
      tone: "none",
      hasRate: false,
    };
  }
  const [lo, hi] = ci;
  const width = hi - lo;
  // Rule 3: wide interval is a different claim than a tight one.
  const wide = width >= WIDE_CI_THRESHOLD;
  return {
    label: pct(rate),
    // Rule 2: the point estimate never travels without its uncertainty.
    detail: `${pct(lo)}–${pct(hi)} · n=${n}`,
    tone: wide ? "wide" : "measured",
    hasRate: true,
  };
}

export function laneEvidence(lane: DispatchLane, minSamples: number): EvidenceView {
  return evidenceView(lane.success_rate, lane.ci, lane.successes, lane.runs, minSamples);
}

export function topologyEvidence(t: DispatchTopology, minSamples: number): EvidenceView {
  return evidenceView(t.win_rate, t.ci, t.wins, t.sample_size, minSamples);
}

/** pill-* class matching the evidence tone. */
export function evidencePillClass(tone: EvidenceTone): string {
  switch (tone) {
    case "measured":
      return "pill-ok";
    case "wide":
      return "pill-warn";
    default:
      return "pill-muted";
  }
}
