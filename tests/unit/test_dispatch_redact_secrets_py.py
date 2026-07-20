import pytest

from mini_ork.dispatch.llm_dispatch import redact_secrets


@pytest.mark.parametrize("secret, context", [
    ("sk-ant-abc1234567890XYZdef", "401 Unauthorized"),
    ("sk-or-v1-deadbeefdeadbeefdeadbeef", "error"),
    ("sk-cp-1234567890abcdefghij", "MiniMax 401"),
    ("Bearer eyJhbGciOiJIUzI1NiJ9.foo.bar1234567", "Authorization"),
    ("sk-ant-secretvalue123", "PATH=/usr/bin"),
    ("406c14eba1d8f9f72b4e9a0c1c2d3e4f5061728394a5b6c7d8e9f0a1b2c3d4e5f", "GLM auth fail"),
])
def test_known_secret_shapes_are_redacted(secret, context):
    output = redact_secrets(f"{context}: {secret}")
    assert secret not in output
    assert context in output


def test_harmless_and_empty_strings_are_preserved():
    harmless = "harmless: file not found at /tmp/foo"
    assert redact_secrets(harmless) == harmless
    assert redact_secrets("") == ""
