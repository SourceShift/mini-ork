import json

from mini_ork.dispatch.transcripts import write_exec_transcript


def test_sidecar_tokens_and_output_are_merged(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text('{"ok":true}\n<z-insight>{"leak":1}</z-insight>\n')
    (tmp_path / "out.txt.turns.jsonl").write_text(
        '{"input_tokens":1000,"output_tokens":50,"model":"codex","session_id":"s1"}\n'
    )
    result = write_exec_transcript(output, "codex")
    payload = json.loads(result.read_text())
    assert payload["totals"] == {"input_tokens": 1000, "output_tokens": 50}
    assert payload["turns"][0]["text"].startswith('{"ok":true}')


def test_missing_sidecar_writes_text_fallback(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text("plain body\n")
    payload = json.loads(write_exec_transcript(output, "codex").read_text())
    assert payload["fallback"] == "text-output"
    assert payload["turns"][0]["text"] == "plain body\n"
