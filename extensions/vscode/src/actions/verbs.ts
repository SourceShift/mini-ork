/**
 * Which CLI invocations deserve a confirm sheet.
 *
 * Kept out of `dispatcher.ts` on purpose: that module imports `vscode`, so a
 * rule that lives there cannot be unit-tested. This file has no imports, which
 * makes the classification — the only thing standing between one click and a
 * stopped run — directly testable.
 */

/**
 * Verbs whose effect cannot be undone from the IDE. The spec usually attaches
 * its own `confirm`, but a page that forgets one must still not let a single
 * click stop a run or discard a worktree.
 */
export const DESTRUCTIVE_VERBS: ReadonlySet<string> = new Set([
  'stop',
  'kill',
  'discard',
  'dismiss',
  'reject',
  'rollback',
  'undo',
  'delete',
  'remove',
  'prune',
  'reset',
  'merge',
  'revert',
  'unset-env',
  'cancel',
]);

/** Verbs that spend money or start paid work. Always confirmed. */
export const COSTLY_VERBS: ReadonlySet<string> = new Set([
  'run',
  'race',
  'retry',
  'repair',
  'recover',
  'resume',
  'poll',
  'certify',
  'start',
  'launch',
]);

/** The verb a command spends its effect on, or `args[0]` when none is known. */
export function verbOf(args: string[] | undefined): string | undefined {
  if (!args || !args.length) {
    return undefined;
  }
  // The spec is not uniform about where the verb sits: `["run", …]` and
  // `["repair", …]` lead with it, but the board and pm families put a noun
  // first — `["board", "kill", id]`, `["pm", "remove", pkg]`. So the head and
  // the token after it are both inspected — and only those, because everything
  // past them is an id or, worse, free text: a thread whose message is exactly
  // "stop" must not turn into a confirmation dialog about stopping a run.
  for (const token of args.slice(0, 2)) {
    if (DESTRUCTIVE_VERBS.has(token) || COSTLY_VERBS.has(token)) {
      return token;
    }
    // `race-start`, `race-stop`, `race-abort` — one family, same guard.
    if (token.startsWith('race-')) {
      return token.slice(5);
    }
  }
  return args[0];
}

/** True when a confirmation sheet must be shown before running `args`. */
export function needsConfirm(args: string[] | undefined): boolean {
  const verb = verbOf(args);
  return verb !== undefined && (DESTRUCTIVE_VERBS.has(verb) || COSTLY_VERBS.has(verb));
}

/** True when `args` would be destructive rather than merely expensive. */
export function isDestructive(args: string[] | undefined): boolean {
  const verb = verbOf(args);
  return verb !== undefined && DESTRUCTIVE_VERBS.has(verb);
}
