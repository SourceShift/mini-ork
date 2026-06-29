"""Dataset exporters for SFT stacks, starting with LLaMA-Factory."""

from __future__ import annotations

from pathlib import Path

from .io import write_json
from .models import Episode, Skill


def export_llamafactory(episodes: list[Episode], skills: list[Skill], out_dir: Path, dataset_name: str = "traceotter_sft") -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    examples = []
    for episode in episodes:
        if not episode.labels.should_imitate and episode.outcome.status != "completed":
            continue
        instruction = "Given this coding-agent task, choose the route, key procedure, and verification plan."
        input_text = _episode_prompt(episode, skills)
        output_text = _episode_target(episode)
        examples.append({"instruction": instruction, "input": input_text, "output": output_text})

    data_path = out_dir / f"{dataset_name}.json"
    write_json(data_path, examples)
    dataset_info = {
        dataset_name: {
            "file_name": data_path.name,
            "columns": {"prompt": "instruction", "query": "input", "response": "output"},
        }
    }
    write_json(out_dir / "dataset_info.json", dataset_info)
    (out_dir / "llamafactory_sft.yaml").write_text(_simple_yaml(_llamafactory_config(dataset_name)), encoding="utf-8")
    return {
        "dataset": str(data_path),
        "dataset_info": str(out_dir / "dataset_info.json"),
        "config": str(out_dir / "llamafactory_sft.yaml"),
        "examples": str(len(examples)),
    }


def _episode_prompt(episode: Episode, skills: list[Skill]) -> str:
    relevant = [skill for skill in skills if episode.episode_id in skill.source_episode_ids][:5]
    skill_text = "\n".join(f"- {skill.title}: {skill.trigger}" for skill in relevant) or "- none"
    steps = "\n".join(f"{s.index}. {s.action_kind}: {s.summary[:240]}" for s in episode.steps[:20])
    return (
        f"repo: {episode.repo}\n"
        f"cwd: {episode.cwd}\n"
        f"user_goal: {episode.user_goal}\n"
        f"task_type: {episode.task_type}\n"
        f"candidate_skills:\n{skill_text}\n"
        f"observed_steps:\n{steps}\n"
    )


def _episode_target(episode: Episode) -> str:
    tests = "\n".join(f"- {cmd}" for cmd in episode.outcome.tests_run) or "- focused verification not recorded"
    skills = "\n".join(f"- {s}" for s in episode.labels.useful_skill_candidates) or "- no reusable skill"
    return (
        f"route: {'direct_edit' if episode.task_type in {'bug_fix', 'training'} else 'docs_or_review'}\n"
        f"procedure:\n{skills}\n"
        f"verification:\n{tests}\n"
        f"outcome_status: {episode.outcome.status}"
    )


def _llamafactory_config(dataset_name: str) -> dict[str, object]:
    return {
        "stage": "sft",
        "do_train": True,
        "model_name_or_path": "Qwen/Qwen2.5-Coder-1.5B-Instruct",
        "dataset": dataset_name,
        "template": "qwen",
        "finetuning_type": "lora",
        "lora_target": "all",
        "output_dir": "saves/traceotter-qwen2.5-coder-1.5b-lora",
        "per_device_train_batch_size": 2,
        "gradient_accumulation_steps": 8,
        "learning_rate": 0.0002,
        "num_train_epochs": 3,
        "cutoff_len": 4096,
        "bf16": True,
        "logging_steps": 10,
        "save_steps": 200,
    }


def _simple_yaml(payload: dict[str, object]) -> str:
    lines = []
    for key, value in payload.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines) + "\n"
