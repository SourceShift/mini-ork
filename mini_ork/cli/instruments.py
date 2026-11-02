"""``mini-ork instruments`` — produce the verification instruments' records.

Three instruments in ``docs/architecture/verification-stack.md`` are consumed
but had no producer: ⑦ oracle integrity ("nothing writes it"), ⑩ verifier
mutation ("nothing writes scores"), and ⑧ judge validity (off, with no way to
measure before enforcing). This verb supplies the missing producers for ⑦ and
⑩; ⑧'s ``shadow`` value is a knob, not a subcommand.

    mini-ork instruments oracle-integrity [--db PATH] [--json]
    mini-ork instruments verifier-mutation --observable FILE [--card FILE] [--json]

Both verbs fail closed. An instrument that could not measure exits ``2`` and
never writes a record that would read as a pass:

- ``oracle-integrity`` — ``0`` the audit passed, ``1`` it failed a real
  measurement, ``2`` it could not run (the real judge or ``ExecOutcome`` was
  unavailable, or the record could not be written). A could-not-run audit
  writes nothing. A failing record *is* written, and the promotion gate's
  consumer reads it, so the verb warns on stderr that every would-be promotion
  now quarantines until a passing record replaces it — running a diagnostic
  must not silently stop promotions.
- ``verifier-mutation`` — ``0`` measured and admitted, ``1`` measured and below
  the floor, ``2`` not measured (baseline not PROVEN) or the observable was
  unreadable/invalid. An unmeasured card gets no sidecar: writing one would
  overwrite an earlier real measurement with a null score, and ``read_score``
  would then report "unmeasured" for a card that once measured badly. Also, a
  sidecar that could not be written is exit ``2``, never a silent success.

``--db`` defaults exactly as :meth:`mini_ork.context.RunContext.db_or_default`
resolves it — ``MINI_ORK_DB``, else ``$MINI_ORK_HOME/state.db``, else the
process-default home. There is no third fallback.

Import discipline: stdlib only at module scope; ``mini_ork.*`` is imported
function-locally, matching ``mini_ork/gates/oracle_integrity.py``.
"""
from __future__ import annotations

import json
import os
import sys


def _usage() -> str:
    return (
        "Usage: mini-ork instruments <subcommand> [options]\n"
        "\n"
        "  oracle-integrity [--db PATH] [--json]\n"
        "      Audit the real verdict judge against scripted defective evidence\n"
        "      (hermetic: no network, no model, no sandbox) and write\n"
        "      <dirname(db)>/oracle-integrity/latest.json. Exit 0 pass,\n"
        "      1 fail, 2 could not run (nothing written).\n"
        "\n"
        "  verifier-mutation --observable FILE [--card FILE] [--json]\n"
        "      Score an observable's verifier by its mutation kill rate. With\n"
        "      --card, write <card>.mutation.json. Exit 0 measured+admitted,\n"
        "      1 measured+below floor, 2 not measured (no sidecar written).\n"
        "\n"
        "--db defaults to $MINI_ORK_DB, else $MINI_ORK_HOME/state.db, else\n"
        ".mini-ork/state.db relative to the current directory.\n"
        "\n"
        "Options:\n"
        "  --json    Emit the record as the only stdout\n"
        "  --help    This message\n"
    )


def _oracle_integrity(argv: list[str]) -> int:
    db_path = None
    json_flag = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            sys.stdout.write(_usage())
            return 0
        if arg == "--json":
            json_flag = True
        elif arg == "--db" or arg.startswith("--db="):
            if arg == "--db":
                if i + 1 >= len(argv):
                    sys.stderr.write(
                        "instruments oracle-integrity: --db requires a path\n"
                    )
                    return 2
                i += 1
                db_path = argv[i]
            else:
                db_path = arg.split("=", 1)[1]
            if not db_path:
                sys.stderr.write(
                    "instruments oracle-integrity: --db requires a path\n"
                )
                return 2
        elif arg.startswith("-"):
            sys.stderr.write(
                f"instruments oracle-integrity: unknown flag: {arg}\n"
            )
            return 2
        else:
            sys.stderr.write(
                f"instruments oracle-integrity: unexpected argument: {arg}\n"
            )
            return 2
        i += 1

    from mini_ork.context import RunContext
    from mini_ork.gates import oracle_integrity

    resolved = db_path or RunContext.from_env().db_or_default()

    try:
        result = oracle_integrity.run_default_audit()
    except Exception as exc:  # noqa: BLE001 — could-not-run is 2, never a stub pass
        # A traceback would exit 1, which means "a real measurement failed".
        # An audit that could not run is not that, and it wrote nothing.
        sys.stderr.write(
            "instruments oracle-integrity: audit could not run "
            f"(no record written): {exc}\n"
        )
        return 2

    try:
        path = oracle_integrity.write_latest(resolved, result)
    except OSError as exc:
        sys.stderr.write(
            "instruments oracle-integrity: cannot write the record for db "
            f"{resolved} (no record written): {exc}\n"
        )
        return 2

    passed = result.get("pass") is True
    missed = list(result.get("missed") or [])
    controls_failed = list(result.get("controls_failed") or [])

    # This verb is a diagnostic, but its output is read by a live consumer. The
    # promotion gate treats a failing record as grounds to quarantine, and its
    # consumer is on unless MO_ORACLE_INTEGRITY is exactly "0" — mirror that
    # predicate rather than a broader off-set, so the warning matches what the
    # gate will actually do. Say it out loud: running a check must never be the
    # silent reason promotions stopped landing.
    if not passed and os.environ.get("MO_ORACLE_INTEGRITY", "1") != "0":
        sys.stderr.write(
            "instruments oracle-integrity: wrote a FAILING record to "
            f"{path}. The promotion gate's oracle-integrity consumer is on, so "
            "every would-be promotion now quarantines with "
            "oracle_integrity_failed until a passing record replaces this one. "
            "Set MO_ORACLE_INTEGRITY=0 to keep promoting while you fix the "
            f"judge, or point --db at a scratch state.db. Missed: "
            f"{', '.join(missed) if missed else '(none)'}"
            f"; controls failed: "
            f"{', '.join(controls_failed) if controls_failed else '(none)'}\n"
        )

    if json_flag:
        payload = dict(result)
        payload["path"] = path
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            f"pass: {str(passed).lower()}\n"
            f"missed: {', '.join(missed) if missed else '(none)'}\n"
            "controls_failed: "
            f"{', '.join(controls_failed) if controls_failed else '(none)'}\n"
            f"path: {path}\n"
        )
    return 0 if passed else 1


def _fmt_score(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return format(value, ".3f")
    return str(value)


def _verifier_mutation(argv: list[str]) -> int:
    observable_path = None
    card_path = None
    json_flag = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            sys.stdout.write(_usage())
            return 0
        if arg == "--json":
            json_flag = True
            i += 1
            continue
        matched = False
        for flag, name in (("--observable", "observable"), ("--card", "card")):
            if arg == flag or arg.startswith(flag + "="):
                if arg == flag:
                    if i + 1 >= len(argv):
                        sys.stderr.write(
                            f"instruments verifier-mutation: {flag} requires a path\n"
                        )
                        return 2
                    i += 1
                    value = argv[i]
                else:
                    value = arg.split("=", 1)[1]
                if not value:
                    sys.stderr.write(
                        f"instruments verifier-mutation: {flag} requires a path\n"
                    )
                    return 2
                if name == "observable":
                    observable_path = value
                else:
                    card_path = value
                matched = True
                break
        if not matched:
            if arg.startswith("-"):
                sys.stderr.write(
                    f"instruments verifier-mutation: unknown flag: {arg}\n"
                )
            else:
                sys.stderr.write(
                    f"instruments verifier-mutation: unexpected argument: {arg}\n"
                )
            return 2
        i += 1

    if observable_path is None:
        sys.stderr.write(
            "instruments verifier-mutation: --observable FILE is required\n"
        )
        return 2

    from mini_ork.gates import verifier_mutation

    try:
        with open(observable_path, encoding="utf-8") as fh:
            observable = json.load(fh)
    except OSError as exc:
        sys.stderr.write(
            f"instruments verifier-mutation: cannot read {observable_path}: {exc}\n"
        )
        return 2
    except ValueError as exc:  # JSONDecodeError, and a BOM/unhashable edge
        sys.stderr.write(
            f"instruments verifier-mutation: {observable_path} is not valid "
            f"JSON: {exc}\n"
        )
        return 2

    if not isinstance(observable, dict):
        sys.stderr.write(
            "instruments verifier-mutation: "
            f"{observable_path} must contain a JSON object\n"
        )
        return 2

    try:
        result = verifier_mutation.score(observable)
    except Exception as exc:  # noqa: BLE001 — could-not-measure is 2
        sys.stderr.write(
            f"instruments verifier-mutation: scoring failed: {exc}\n"
        )
        return 2

    # ``measured`` is the gate's word for "the scoring loop ran"; a real
    # measurement needs a number, so require both. ``score`` reports
    # ``measured: False`` for an empty denominator, but the belt-and-braces
    # ``kill_score is not None`` also covers any caller that hands back a
    # truthy ``measured`` with a null score — ``admit(None)`` is True, so
    # trusting the flag alone would print "admitted" for a card never scored.
    kill_score = result.get("kill_score")
    measured = result.get("measured") is True and kill_score is not None
    admitted = verifier_mutation.admit(kill_score) if measured else None
    if not measured and not result.get("reason"):
        result = dict(result)
        result["reason"] = (
            "no mutation applied to this observable — it carries none of the "
            "fields the mutations corrupt (body/expect_status), so there is "
            "no kill score to report"
        )

    sidecar = None
    if card_path is not None and measured:
        try:
            sidecar = verifier_mutation.write_score(card_path, result)
        except OSError as exc:
            sys.stderr.write(
                "instruments verifier-mutation: cannot write the sidecar for "
                f"{card_path}: {exc}\n"
            )
            return 2
    elif card_path is not None:
        sys.stderr.write(
            "instruments verifier-mutation: no sidecar written for "
            f"{card_path}: the verifier was not measured, and a null score "
            "must not replace a real one\n"
        )

    if json_flag:
        payload = dict(result)
        payload["admitted"] = admitted
        if sidecar is not None:
            payload["card"] = sidecar
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        lines = [
            f"measured: {str(measured).lower()}",
            f"kill_score: {_fmt_score(result.get('kill_score'))}",
            "killed: "
            f"{result.get('killed', 0)} survived: {result.get('survived', 0)} "
            f"abstained: {result.get('abstained', 0)}",
        ]
        if measured:
            lines.append(f"admitted: {str(admitted).lower()}")
        elif result.get("reason"):
            lines.append(f"reason: {result['reason']}")
        if sidecar is not None:
            lines.append(f"card: {sidecar}")
        sys.stdout.write("\n".join(lines) + "\n")

    if not measured:
        return 2
    return 0 if admitted else 1


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        sys.stderr.write("instruments: a subcommand is required\n")
        sys.stdout.write(_usage())
        return 2
    sub = argv[0]
    if sub in ("--help", "-h", "help"):
        sys.stdout.write(_usage())
        return 0
    if sub == "oracle-integrity":
        return _oracle_integrity(argv[1:])
    if sub == "verifier-mutation":
        return _verifier_mutation(argv[1:])
    sys.stderr.write(f"instruments: unknown subcommand: {sub}\n")
    sys.stdout.write(_usage())
    return 2


if __name__ == "__main__":
    sys.exit(main())
