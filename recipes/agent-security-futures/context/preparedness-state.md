# Preparedness baseline — what a team deploying agents already has

This is the state the reviewers score against. It describes ordinary,
2026-current engineering practice for a team that ships software with AI agents
and runs agents against real systems. It is a written baseline, not a survey of
one specific company: when a control is described as missing, it means missing
from this baseline, not from every organisation.

## What the baseline has

- **Human review before merge.** A person reads the diff. Review catches plain
  mistakes; it does not catch an injected instruction the reviewer reads as a
  normal comment, and it does not scale to agent-volume changes.
- **CI test and lint gates.** Tests, type checks, lint, dependency and secret
  scanning run on every change. All of them answer "is this change correct or
  already known-bad", not "is this change trying to do something else".
- **Credential hygiene, partly.** Secrets live in an environment store or a
  vault, not in the repo. Long-lived broad-scope tokens still exist (CI tokens,
  cloud credentials visible to the whole job).
- **Endpoint and network controls on developer machines.** EDR, a managed
  browser, disk encryption, a VPN or zero-trust proxy.
- **Sandboxing, sometimes.** Container isolation for build and test jobs; often
  no isolation at all for an agent that runs on a developer's machine with the
  developer's own credentials and filesystem access.
- **Logs.** Application logs and CI logs. They record what a pipeline did, not
  which model, prompt, tool call and approval produced an action, and they are
  not tamper-evident.

## What the baseline does not have

- **Agent-specific authorization.** Permissions are the developer's, inherited
  wholesale. There is no per-agent identity, no scoped capability token, no
  approval step before a destructive action.
- **A mediated tool boundary.** Tool calls go straight to the shell, filesystem
  and network. Nothing inspects the arguments before the call or the result
  after it.
- **An untrusted-content boundary.** Repo files, issue bodies, dependency
  READMEs, web pages and tool results all reach the model as text with no
  provenance marking and no rule that says which of them may influence which
  action.
- **Agent action provenance.** No hash-chained record linking an action to the
  input that caused it, so incident response cannot answer "what did this agent
  read before it did that".
- **Security evaluation of agent output.** Code written by an agent is tested
  for correctness and lint, but not re-examined for the vulnerability it just
  introduced, and not replayed against an exploit to prove the fix.
- **Monitoring for abnormal agent behaviour.** Nothing baselines what an agent
  normally does, so a hijacked agent doing something unusual looks like work.
- **A response plan for agent-caused incidents.** No owner, no kill switch
  practice, no way to enumerate what an agent has touched.

## How to read the baseline

- A technique that already exists as ordinary engineering practice (secret
  scanning, dependency scanning, container isolation) is `commoditised`: the
  work is extending it to agents, not inventing it.
- A technique that exists in a weaker form (logs but not provenance, review but
  not tool mediation) is `partial`.
- A technique with nothing in the baseline behind it is `unaddressed`.
- The baseline is deliberately not flattering. Most teams that deploy agents
  today look roughly like this; the report is more useful if it assumes so.
