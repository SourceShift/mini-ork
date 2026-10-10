"""The run-scoped env overlay: what a run declares, and what a node receives.

Two behaviours matter more than the parsing: a value another node declared must
not survive into a sibling (masking), and the operator's run file must beat the
recipe's own declaration (precedence), because the recipe knows *which* agent
needs a variable and the run supplies the value.
"""
import pytest
import yaml

from mini_ork.dispatch.run_env import (
    RESERVED_KEYS,
    RunEnv,
    RunEnvError,
    effective_node_overrides,
    load_run_env,
    parse_run_env,
    run_env_path,
)


# ── parsing ──────────────────────────────────────────────────────────────────


def test_parse_scalars_are_stringified_for_the_child():
    run_env = parse_run_env(
        {"env": {"MO_MIN": 40, "DEBUG": True, "RATIO": 0.5, "TOKEN": "abc"}},
        {},
    )
    assert run_env.run_level == {
        "MO_MIN": "40",
        "DEBUG": "1",
        "RATIO": "0.5",
        "TOKEN": "abc",
    }


def test_parse_false_is_zero_not_the_string_false():
    # YAML `false` stringifies as "False", which no shell script reads as off.
    assert parse_run_env({"env": {"OFF": False}}, {}).run_level == {"OFF": "0"}


def test_parse_null_declares_a_variable_absent():
    assert parse_run_env({"env": {"TOKEN": None}}, {}).run_level == {"TOKEN": None}


def test_parse_expands_placeholders_from_the_secret_store_view():
    run_env = parse_run_env({"env": {"TOKEN": "${STORE_TOKEN}"}}, {"STORE_TOKEN": "s3cr3t"})
    assert run_env.run_level == {"TOKEN": "s3cr3t"}


def test_parse_unset_placeholder_is_an_error():
    with pytest.raises(RunEnvError, match=r"\$\{MISSING\}"):
        parse_run_env({"env": {"TOKEN": "${MISSING}"}}, {})


def test_parse_rejects_executor_owned_keys():
    # A run file that could set MO_NODE_ID would race the publish carrying the
    # node's identity, so it is refused rather than ordered.
    for key in sorted(RESERVED_KEYS):
        with pytest.raises(RunEnvError, match="owned by the executor"):
            parse_run_env({"env": {key: "x"}}, {})


def test_parse_rejects_a_yaml_boolean_key():
    # `ON:` parses as the boolean True, so the variable would silently be "True".
    # The document has to come through YAML for that to happen.
    with pytest.raises(RunEnvError, match="YAML boolean"):
        parse_run_env(yaml.safe_load("env:\n  ON: 1\n"), {})


def test_parse_rejects_a_typoed_section():
    # `node:` instead of `nodes:` would otherwise silently drop every per-node
    # override and run every node with the run-level environment.
    with pytest.raises(RunEnvError, match="unknown section"):
        parse_run_env({"env": {}, "node": {"a": {"X": "1"}}}, {})


def test_parse_rejects_a_nested_value():
    with pytest.raises(RunEnvError, match="must be a string, number, boolean or null"):
        parse_run_env({"env": {"X": {"a": "b"}}}, {})


def test_parse_absent_document_is_empty():
    assert parse_run_env(None, {}).is_empty


# ── per-node resolution ──────────────────────────────────────────────────────


def test_node_overrides_masks_every_key_the_node_does_not_supply():
    run_env = parse_run_env(
        {"env": {"RUN_ONLY": "1"}, "nodes": {"collector": {"TOKEN": "t"}}}, {}
    )
    assert run_env.node_overrides("collector") == {"RUN_ONLY": "1", "TOKEN": "t"}
    # The reviewer never declared TOKEN, so it must be actively cleared rather
    # than inherited from the ambient environment.
    assert run_env.node_overrides("reviewer") == {"RUN_ONLY": "1", "TOKEN": None}


def test_declared_keys_unions_run_level_and_every_node():
    run_env = parse_run_env(
        {"env": {"A": "1"}, "nodes": {"n1": {"B": "2"}, "n2": {"C": "3"}}}, {}
    )
    assert run_env.declared_keys == frozenset({"A", "B", "C"})


def test_for_node_lets_the_node_override_the_run_level():
    run_env = parse_run_env({"env": {"T": "run"}, "nodes": {"n": {"T": "node"}}}, {})
    assert run_env.for_node("n")["T"] == "node"
    assert run_env.for_node("other")["T"] == "run"


# ── precedence at the node boundary ──────────────────────────────────────────


def test_run_file_beats_the_recipe_declaration():
    run_env = parse_run_env({"env": {"T": "from-run"}}, {})
    published = effective_node_overrides(run_env, "n", node_env={"T": "${SOME_REF}"})
    assert published["T"] == "from-run"


def test_node_level_beats_the_run_level():
    run_env = parse_run_env({"env": {"T": "run"}, "nodes": {"n": {"T": "node"}}}, {})
    assert effective_node_overrides(run_env, "n")["T"] == "node"


def test_a_declared_but_unsupplied_key_is_cleared_not_inherited():
    seen = effective_node_overrides(RunEnv(), "n", workflow_declared=["SECRET"])
    assert seen == {"SECRET": None}


def test_the_published_map_covers_the_union_of_all_declarations():
    run_env = parse_run_env({"nodes": {"n": {"B": "2"}}}, {})
    published = effective_node_overrides(
        run_env, "n", node_env={"A": "1"}, workflow_declared=["C"]
    )
    assert published == {"A": "1", "B": "2", "C": None}


def test_a_node_may_mask_a_run_level_value_the_recipe_declared():
    run_env = parse_run_env({"nodes": {"reviewer": {"T": None}}}, {})
    published = effective_node_overrides(run_env, "reviewer", node_env={"T": "${SOME_REF}"})
    assert published["T"] is None


# ── loading ──────────────────────────────────────────────────────────────────


def test_load_reads_the_run_config_file(tmp_path):
    path = run_env_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("env:\n  MO_MIN: 12\n")
    assert load_run_env(tmp_path, {}) .run_level == {"MO_MIN": "12"}


def test_load_missing_file_is_an_empty_overlay(tmp_path):
    assert load_run_env(tmp_path, {}).is_empty


def test_load_with_no_run_dir_is_empty():
    assert load_run_env("", {}).is_empty


def test_load_reports_a_malformed_document(tmp_path):
    path = run_env_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("env:\n  X: 1\n  Y:\n    nested: true\n")
    with pytest.raises(RunEnvError, match="must be a string"):
        load_run_env(tmp_path, {})
