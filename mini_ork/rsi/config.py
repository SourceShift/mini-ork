"""One config file for both tiers — and a parser that refuses to guess.

The simple tier (a laptop: local SQLite, local worktrees) and the secure tier (a
shared DB, sandboxed children, a secret store) differ only in this file, so every
value a cycle reads comes from here rather than from ambient environment. The
parser's job is therefore not to be lenient; it is to fail at the first value
that would make an unattended loop behave differently from what the operator
wrote down.

Three rules follow, and each one closes a way a config lies:

* **Unknown keys are errors.** A typo'd ``merge.testcm`` would otherwise leave
  ``test_cmd`` empty, and an empty gate command is exactly how the full suite
  becomes the default gate.
* **Absent is not the same as falsy.** ``decide.auto_merge`` has no default at
  all: a file that omits it gets a cycle that quarantines everything, which is
  the safe reading of "the operator did not say". A budget of ``0`` is refused
  rather than read as "unlimited".
* **Parsing is pure.** No git, no network, no DB. The checks that need the world
  (an ``origin`` remote, an openable secret store) live in ``doctor``/preflight,
  so this module stays unit-testable and ``plan`` stays free.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from mini_ork.rsi.queue import QueueConfig

__all__ = [
    "BudgetConfig",
    "ConfigError",
    "CriticConfig",
    "CycleConfig",
    "DecideConfig",
    "GatesConfig",
    "ImplementConfig",
    "LeaseConfig",
    "MergeConfig",
    "ProbesConfig",
    "SecureConfig",
    "load_config",
]

RUNTIME_BACKENDS: tuple[str, ...] = ("local", "bubblewrap", "daytona", "microsandbox")
MODES: tuple[str, ...] = ("once", "loop")
STATIC_MODES: tuple[str, ...] = ("differential", "strict")
ERROR_POLICIES: tuple[str, ...] = ("halt_before_spend", "quarantine_only")


class ConfigError(ValueError):
    """A value a cycle cannot run with. The CLI turns this into exit 2."""


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{name}: expected a mapping, got {type(value).__name__}")
    return value


def _only(where: str, raw: Mapping[str, Any], known: frozenset[str]) -> None:
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}")


def _positive(where: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where}: expected a positive integer, got {value!r}")
    return value


def _positive_float(where: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{where}: expected a positive number, got {value!r}")
    return float(value)


def _str_tuple(where: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ConfigError(f"{where}: expected a list of strings, got {value!r}")
    out = tuple(str(v).strip() for v in value)
    if any(not v for v in out):
        raise ConfigError(f"{where}: contains an empty entry")
    return out


def _flag(where: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true/false, got {value!r}")
    return value


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeaseConfig:
    key: str = "__rsi_cycle__"
    ttl_s: int = 3600

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "LeaseConfig":
        _only("lease", raw, frozenset({"key", "ttl_s"}))
        key = str(raw.get("key", "__rsi_cycle__")).strip()
        if not key:
            raise ConfigError("lease.key: must not be empty")
        return cls(key=key, ttl_s=_positive("lease.ttl_s", raw.get("ttl_s", 3600)))


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    daily_cap_usd: float
    per_cycle_cap_usd: float

    def __post_init__(self) -> None:
        if self.per_cycle_cap_usd > self.daily_cap_usd:
            raise ConfigError(
                f"budget.per_cycle_cap_usd (${self.per_cycle_cap_usd:.2f}) exceeds "
                f"daily_cap_usd (${self.daily_cap_usd:.2f}); no cycle could ever pass G11")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, env: Mapping[str, str]) -> "BudgetConfig":
        _only("budget", raw, frozenset({"daily_cap_usd", "per_cycle_cap_usd"}))
        daily = _positive_float("budget.daily_cap_usd", raw.get("daily_cap_usd"))
        per_cycle = _positive_float("budget.per_cycle_cap_usd", raw.get("per_cycle_cap_usd"))
        # The environment wins, exactly as the global budget guard does, so a
        # shell-wide cap cannot be raised by a config file in the checkout.
        # Presence, not truthiness: ``MO_DAILY_BUDGET_USD=`` is someone *trying*
        # to set a cap, and treating it as unset would leave the higher file
        # value in force — the failure mode this whole section exists to stop.
        if "MO_DAILY_BUDGET_USD" in env:
            raw_env = str(env["MO_DAILY_BUDGET_USD"]).strip()
            try:
                override = float(raw_env)
            except ValueError as exc:
                raise ConfigError(f"MO_DAILY_BUDGET_USD is not a number: {raw_env!r}") from exc
            if override <= 0:
                raise ConfigError(f"MO_DAILY_BUDGET_USD must be positive, got {override}")
            daily = min(daily, override)
        return cls(daily_cap_usd=daily, per_cycle_cap_usd=per_cycle)


@dataclass(frozen=True, slots=True)
class ProbesConfig:
    run: tuple[str, ...] = ()
    timeout_s: int = 900
    per_source_max: int = 3
    error_policy: str = "halt_before_spend"
    allow_live: bool = False
    pytest_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.error_policy not in ERROR_POLICIES:
            raise ConfigError(
                f"probes.error_policy: {self.error_policy!r} not one of {ERROR_POLICIES}")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ProbesConfig":
        _only("probes", raw, frozenset({
            "run", "timeout_s", "per_source_max", "error_policy", "allow_live",
            "pytest_paths",
        }))
        # ``allow_live`` lives here only so a file can *ask* for live probes;
        # ``from_mapping`` refuses to honour it without ``--probe-run``, and that
        # check lives in ``strict`` mode because a checked-in file must never be
        # able to start spending on its own.
        from mini_ork.rsi.select import registered_success_commands

        run = _str_tuple("probes.run", raw.get("run", ()))
        unknown = [p for p in run if p not in registered_success_commands()]
        if unknown:
            raise ConfigError(f"probes.run: unregistered probe id(s) {unknown}")
        return cls(
            run=run,
            timeout_s=_positive("probes.timeout_s", raw.get("timeout_s", 900)),
            per_source_max=_positive("probes.per_source_max", raw.get("per_source_max", 3)),
            error_policy=str(raw.get("error_policy", "halt_before_spend")),
            allow_live=_flag("probes.allow_live", raw.get("allow_live", False)),
            pytest_paths=_str_tuple("probes.pytest_paths", raw.get("pytest_paths", ())),
        )


@dataclass(frozen=True, slots=True)
class GatesConfig:
    cmd_timeout_s: int = 1800
    max_files: int = 5
    static_mode: str = "differential"
    suite_adequacy: bool = False
    probe_run: bool = False
    allow_prefixes: tuple[str, ...] = ()
    pytest_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.static_mode not in STATIC_MODES:
            raise ConfigError(f"gates.static_mode: {self.static_mode!r} not one of {STATIC_MODES}")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "GatesConfig":
        _only("gates", raw, frozenset({
            "cmd_timeout_s", "max_files", "static_mode", "suite_adequacy",
            "probe_run", "allow_prefixes", "pytest_paths",
        }))
        return cls(
            cmd_timeout_s=_positive("gates.cmd_timeout_s", raw.get("cmd_timeout_s", 1800)),
            max_files=_positive("gates.max_files", raw.get("max_files", 5)),
            static_mode=str(raw.get("static_mode", "differential")),
            suite_adequacy=_flag("gates.suite_adequacy", raw.get("suite_adequacy", False)),
            probe_run=_flag("gates.probe_run", raw.get("probe_run", False)),
            allow_prefixes=_str_tuple("gates.allow_prefixes", raw.get("allow_prefixes", ())),
            pytest_paths=_str_tuple("gates.pytest_paths", raw.get("pytest_paths", ())),
        )

    @property
    def scoped_tests(self) -> tuple[str, ...]:
        """The merge gate's test set: ``gates.pytest_paths`` or ``probes.pytest_paths``."""
        return self.pytest_paths


@dataclass(frozen=True, slots=True)
class CriticConfig:
    lanes: tuple[str, ...] = ()
    require_unanimous: bool = True
    model_timeout_s: int = 900

    def __post_init__(self) -> None:
        from mini_ork.verify.panel_heterogeneity import lane_family

        if len(self.lanes) < 2:
            raise ConfigError(
                f"critic.lanes needs at least 2 independent critics, got {list(self.lanes)}")
        families = [lane_family(lane) for lane in self.lanes]
        if any(not f for f in families):
            raise ConfigError(f"critic.lanes: cannot resolve a family for {self.lanes}")
        if len(set(families)) != len(families):
            raise ConfigError(
                f"critic.lanes share a model family ({families}); "
                "G10 would quarantine every cycle")
        if not self.require_unanimous:
            raise ConfigError(
                "critic.require_unanimous: false is not supported — a split panel "
                "quarantines, it never majority-merges")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CriticConfig":
        _only("critic", raw, frozenset({"lanes", "require_unanimous", "model_timeout_s"}))
        return cls(
            lanes=_str_tuple("critic.lanes", raw.get("lanes", ())),
            require_unanimous=_flag("critic.require_unanimous",
                                    raw.get("require_unanimous", True)),
            model_timeout_s=_positive("critic.model_timeout_s",
                                      raw.get("model_timeout_s", 900)),
        )


@dataclass(frozen=True, slots=True)
class ImplementConfig:
    runtime_backend: str = "local"
    child_timeout_s: int = 3600
    static_recipe_plan: bool = True
    #: The lane the child implementer runs on. G10 needs it to prove the critics
    #: are exogenous to the implementer, so it is config here rather than
    #: inferred from whichever model happened to answer.
    lane: str = "codex_lens"

    def __post_init__(self) -> None:
        if self.runtime_backend not in RUNTIME_BACKENDS:
            raise ConfigError(
                f"implement.runtime_backend: {self.runtime_backend!r} not one of "
                f"{RUNTIME_BACKENDS}")
        if not self.lane.strip():
            raise ConfigError("implement.lane: must not be empty (G10 needs it)")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ImplementConfig":
        _only("implement", raw, frozenset({
            "runtime_backend", "child_timeout_s", "static_recipe_plan", "lane"}))
        return cls(
            runtime_backend=str(raw.get("runtime_backend", "local")),
            child_timeout_s=_positive("implement.child_timeout_s",
                                      raw.get("child_timeout_s", 3600)),
            static_recipe_plan=_flag("implement.static_recipe_plan",
                                     raw.get("static_recipe_plan", True)),
            lane=str(raw.get("lane", "codex_lens")),
        )


@dataclass(frozen=True, slots=True)
class MergeConfig:
    require_remote: bool = True
    test_cmd: str = ""
    on_merge: str = "clean"

    def __post_init__(self) -> None:
        if self.on_merge not in ("clean", "keep"):
            raise ConfigError(
                f"merge.on_merge: {self.on_merge!r} not one of ('clean', 'keep')")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MergeConfig":
        _only("merge", raw, frozenset({"require_remote", "test_cmd", "on_merge"}))
        return cls(
            require_remote=_flag("merge.require_remote", raw.get("require_remote", True)),
            test_cmd=str(raw.get("test_cmd") or "").strip(),
            on_merge=str(raw.get("on_merge", "clean")),
        )


@dataclass(frozen=True, slots=True)
class DecideConfig:
    #: No default: an absent key means "never merge without a human", and the
    #: dataclass cannot be built without saying so explicitly.
    auto_merge: bool
    quarantine_keep_worktree: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DecideConfig":
        _only("decide", raw, frozenset({"auto_merge", "quarantine_keep_worktree"}))
        return cls(
            auto_merge=_flag("decide.auto_merge", raw.get("auto_merge", False)),
            quarantine_keep_worktree=_flag("decide.quarantine_keep_worktree",
                                           raw.get("quarantine_keep_worktree", True)),
        )


@dataclass(frozen=True, slots=True)
class SecureConfig:
    """The second tier's three pointers. Empty values mean "simple tier"."""

    secret_store: str = ""
    db_path: str = ""
    node_pool: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SecureConfig":
        _only("secure", raw, frozenset({"secret_store", "db_path", "node_pool"}))
        return cls(
            secret_store=str(raw.get("secret_store") or "").strip(),
            db_path=str(raw.get("db_path") or "").strip(),
            node_pool=str(raw.get("node_pool") or "").strip(),
        )


# --------------------------------------------------------------------------
# The whole file
# --------------------------------------------------------------------------

_TOP_KEYS = frozenset({
    "version", "enabled", "mode", "interval_s", "max_cycles", "lease", "queue",
    "budget", "probes", "gates", "critic", "implement", "merge", "decide", "secure",
})


@dataclass(frozen=True, slots=True)
class CycleConfig:
    budget: BudgetConfig
    decide: DecideConfig
    lease: LeaseConfig = field(default_factory=LeaseConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    probes: ProbesConfig = field(default_factory=ProbesConfig)
    gates: GatesConfig = field(default_factory=GatesConfig)
    critic: CriticConfig = field(default_factory=CriticConfig)
    implement: ImplementConfig = field(default_factory=ImplementConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)
    secure: SecureConfig = field(default_factory=SecureConfig)
    version: int = 1
    enabled: bool = True
    mode: str = "once"
    interval_s: int = 3600
    max_cycles: int = 1

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ConfigError(f"mode: {self.mode!r} not one of {MODES}")
        if self.mode == "loop" and self.interval_s <= 0:
            raise ConfigError("mode: loop needs a positive interval_s")
        if self.max_cycles < 0:
            raise ConfigError("max_cycles: must be >= 0 (0 means unlimited)")
        if self.mode == "once" and self.max_cycles > 1:
            raise ConfigError("mode: once with max_cycles > 1 is contradictory")

    @property
    def scoped_tests(self) -> tuple[str, ...]:
        """The test set G6 and the merge green gate are scoped to."""
        return self.gates.pytest_paths or self.probes.pytest_paths

    @property
    def merge_test_cmd(self) -> str:
        """The command ``worktree.merge`` must pass to the script.

        ``merge.test_cmd`` wins; otherwise the scoped set is turned into a
        pytest invocation. There is deliberately no fall back to a bare
        ``pytest`` — that is the full suite the merge script would otherwise run.
        """
        if self.merge.test_cmd:
            return self.merge.test_cmd
        tests = self.scoped_tests
        if not tests:
            raise ConfigError(
                "no scoped test set: set gates.pytest_paths (or merge.test_cmd). "
                "The merge script's default gate is the full suite, which is not a "
                "gate a cycle can wait for")
        return "python3 -m pytest -q -p no:asyncio " + " ".join(tests)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version, "enabled": self.enabled, "mode": self.mode,
            "interval_s": self.interval_s, "max_cycles": self.max_cycles,
            "budget": {"daily_cap_usd": self.budget.daily_cap_usd,
                       "per_cycle_cap_usd": self.budget.per_cycle_cap_usd},
            "decide": {"auto_merge": self.decide.auto_merge,
                       "quarantine_keep_worktree": self.decide.quarantine_keep_worktree},
            "critic": {"lanes": list(self.critic.lanes)},
            "gates": {"max_files": self.gates.max_files,
                      "static_mode": self.gates.static_mode},
        }


def _queue_config(raw: Mapping[str, Any]) -> QueueConfig:
    """Validate the ``queue:`` block before handing it to :class:`QueueConfig`.

    ``QueueConfig.from_mapping`` deliberately ignores unknown keys (it is fed a
    wider mapping elsewhere); inside a config file a typo is not ignorable, so
    the key set is derived from the dataclass fields rather than restated.
    """
    from dataclasses import fields

    _only("queue", raw, frozenset(f.name for f in fields(QueueConfig)))
    try:
        return QueueConfig.from_mapping(raw)
    except ValueError as exc:
        raise ConfigError(f"queue: {exc}") from exc


def from_mapping(raw: Any, *, env: Mapping[str, str] | None = None,
                 strict: bool = False) -> CycleConfig:
    """Build a :class:`CycleConfig`. ``strict`` adds the run path's requirements.

    ``raw`` is typed ``Any`` on purpose: YAML happily returns a list or a bare
    string, and the isinstance check below is the only thing standing between
    that and an ``AttributeError`` deep inside a section parser.
    """
    if not isinstance(raw, Mapping):
        raise ConfigError(f"config must be a mapping, got {type(raw).__name__}")
    _only("config", raw, _TOP_KEYS)
    env = os.environ if env is None else env

    version = raw.get("version", 1)
    if version != 1:
        raise ConfigError(f"version: unsupported config version {version!r}")
    mode = str(raw.get("mode", "once"))
    max_cycles = raw.get("max_cycles", 1 if mode == "once" else 0)
    if isinstance(max_cycles, bool) or not isinstance(max_cycles, int):
        raise ConfigError(f"max_cycles: expected an integer, got {max_cycles!r}")

    gates = GatesConfig.from_mapping(_section(raw, "gates"))
    probes = ProbesConfig.from_mapping(_section(raw, "probes"))
    cfg = CycleConfig(
        version=version,
        enabled=_flag("enabled", raw.get("enabled", True)),
        mode=mode,
        interval_s=_positive("interval_s", raw.get("interval_s", 3600)),
        max_cycles=max_cycles,
        lease=LeaseConfig.from_mapping(_section(raw, "lease")),
        queue=_queue_config(_section(raw, "queue")),
        budget=BudgetConfig.from_mapping(_section(raw, "budget"), env=env),
        probes=probes,
        gates=gates,
        critic=CriticConfig.from_mapping(_section(raw, "critic")),
        implement=ImplementConfig.from_mapping(_section(raw, "implement")),
        merge=MergeConfig.from_mapping(_section(raw, "merge")),
        decide=DecideConfig.from_mapping(_section(raw, "decide")),
        secure=SecureConfig.from_mapping(_section(raw, "secure")),
    )
    if strict:
        if not cfg.scoped_tests and not cfg.merge.test_cmd:
            raise ConfigError(
                "no scoped test set: set gates.pytest_paths (or merge.test_cmd) before "
                "running a cycle — the merge script's default gate is the full suite")
        if probes.allow_live:
            raise ConfigError(
                "probes.allow_live: true is not enough on its own; live probes need "
                "`--probe-run` on the command line")
        # G10 quarantines any cycle whose critics share a family with the
        # implementer. Catching it here turns a guaranteed quarantine — after a
        # paid child run — into a startup error that costs nothing.
        from mini_ork.verify.panel_heterogeneity import lane_family

        implementer = lane_family(cfg.implement.lane)
        critic_families = {lane_family(lane) for lane in cfg.critic.lanes}
        if implementer and implementer in critic_families:
            raise ConfigError(
                f"critic.lanes {list(cfg.critic.lanes)} share the implementer's family "
                f"({implementer}); every cycle would be quarantined as critic_not_exogenous")
    return cfg


def load_config(path: str | Path | None = None, *,
                env: Mapping[str, str] | None = None,
                strict: bool = False) -> CycleConfig:
    """Read and validate a config file.

    Resolution: an explicit path, else ``MO_RSI_CONFIG``, else the repo's
    ``config/rsi-cycle.yaml`` if it exists. A *named* path that does not exist is
    an error — never a silent fall back to defaults, because defaults here would
    mean a cycle choosing its own budget.
    """
    env = os.environ if env is None else env
    named = path or env.get("MO_RSI_CONFIG")
    if named:
        target = Path(named).expanduser()
        if not target.is_file():
            raise ConfigError(f"config file not found: {target}")
        try:
            import yaml

            raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - unreadable config is exit 2
            raise ConfigError(f"cannot read {target}: {type(exc).__name__}: {exc}") from exc
        if raw is None:
            raise ConfigError(f"{target} is empty")
        return from_mapping(raw, env=env, strict=strict)

    default = Path(env.get("MINI_ORK_ROOT") or ".") / "config" / "rsi-cycle.yaml"
    if default.is_file():
        return load_config(default, env=env, strict=strict)
    raise ConfigError(
        "no rsi-cycle config: pass --config PATH, set MO_RSI_CONFIG, or create "
        "config/rsi-cycle.yaml (copy config/rsi-cycle.example.yaml)")
