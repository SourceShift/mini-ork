#!/usr/bin/env python3
"""grpo_route_train.py — REAL GRPO (RLVR) on the TraceOtter student.

This is the actual policy-gradient RL that the stack has been *citing* but not
running. It replaces SFT-on-distilled-traces (scripts/local_lora_train.py) with
verifiable-reward reinforcement learning on the one trainable policy we own —
the local Qwen student.

Why GRPO is well-defined HERE and not in the mini-ork router: the router's arms
are different closed-API model families (codex/kimi/minimax/opus) with no shared
weights and no gradients — so the router is a contextual bandit, correctly. The
student is a single weight set on a GPU we rent, so GRPO is exactly the right tool.

How it works:
  * Prompt: (instruction=system, input=user) — the same route context the SFT
    data uses. The student must emit a `route: <lane>` line.
  * G rollouts: GRPOTrainer samples `--num-generations` completions per prompt on
    the *local* model (the cheap kind of G-sampling — a rented GPU by the minute,
    NOT G calls to frontier APIs per task).
  * Reward (RLVR, VERIFIABLE — no learned reward model): route_of(completion) is
    checked against the gold route. The gold is mini-ork's reward_g-derived
    best-lane for that slice (it is what the SFT `output` already encodes), so
    the reward is grounded in real outcomes, deterministically checkable.
  * GRPO computes the group-relative advantage over the G rewards (mean-baseline,
    the same baseline the router's bandit borrows — but here it drives a real
    policy gradient through the LoRA), with a KL leash to the reference model.
  * Gate: held-out route accuracy via scripts/local_eval_route.py — NOT train loss
    (per the standing rule: gate on held-out eval).

Run (RunPod GPU; ~$0.70-class single-epoch job, same infra as the SFT autopilot):
  python scripts/grpo_route_train.py \
    --model Qwen/Qwen3-4B-Instruct-2507 \
    --data route_train.json --heldout route_test.json \
    --out grpo-out --num-generations 8 --max-steps 500

CPU-validate the reward + data plumbing (no GPU, no trl) before renting a GPU:
  python scripts/grpo_route_train.py --self-test
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# route_of mirrors scripts/local_eval_route.py exactly so training reward and the
# held-out gate score the same way — a reward that disagreed with the eval would
# optimize the wrong thing.
_ROUTE_RE = re.compile(r"^\s*route:\s*(.+?)\s*$", re.M)


def route_of(text: str) -> str:
    m = _ROUTE_RE.search(text or "")
    return m.group(1).strip().lower() if m else ""


def _prompt_key(instruction: str, user: str) -> str:
    return f"{(instruction or '').strip()}\x1f{(user or '').strip()}"


def load_route_rows(path: Path) -> list[dict]:
    """Load {instruction, input, output} rows (same schema as local_lora_train.py).
    JSON array or JSONL both accepted."""
    raw = path.read_text(encoding="utf-8").strip()
    if raw.startswith("["):
        rows = json.loads(raw)
    else:
        rows = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
    out = []
    for r in rows:
        gold = route_of(r.get("output") or "")
        if not gold:
            continue  # only rows with a checkable gold route are RLVR-usable
        out.append({"instruction": (r.get("instruction") or "").strip(),
                    "input": (r.get("input") or "").strip(), "gold": gold})
    return out


def build_reward_fn(rows: list[dict]):
    """Verifiable RLVR reward — NO learned reward model. Per completion:
      +1.0  exact gold route match (the historically-best lane),
      +0.2  a valid-but-wrong route (emitted a real lane, shaped away from garbage),
       0.0  no/invalid route.
    Grouped by prompt, GRPO turns these into a mean-baselined advantage."""
    gold_by_prompt = {_prompt_key(r["instruction"], r["input"]): r["gold"] for r in rows}
    valid_routes = {g for g in gold_by_prompt.values() if g}

    def reward_fn(prompts, completions, **kwargs):
        rewards = []
        for prompt, completion in zip(prompts, completions):
            text = _completion_text(completion)
            key = _prompt_key_from_prompt(prompt)
            gold = gold_by_prompt.get(key, "")
            got = route_of(text)
            if gold and got == gold:
                rewards.append(1.0)
            elif got in valid_routes:
                rewards.append(0.2)
            else:
                rewards.append(0.0)
        return rewards

    reward_fn.gold_by_prompt = gold_by_prompt  # exposed for --self-test
    reward_fn.valid_routes = valid_routes
    return reward_fn


def _completion_text(completion) -> str:
    """trl passes completions as a plain string (non-chat) or a list of message
    dicts (conversational). Handle both so the reward never silently reads ''."""
    if isinstance(completion, str):
        return completion
    if isinstance(completion, list) and completion:
        last = completion[-1]
        return last.get("content", "") if isinstance(last, dict) else str(last)
    return ""


def _prompt_key_from_prompt(prompt) -> str:
    """Reconstruct the (instruction, input) key from what trl hands the reward fn —
    the conversational prompt (list of {role, content} messages)."""
    if isinstance(prompt, list):
        sys_c = next((m.get("content", "") for m in prompt if m.get("role") == "system"), "")
        usr_c = next((m.get("content", "") for m in reversed(prompt)
                      if m.get("role") == "user"), "")
        return _prompt_key(sys_c, usr_c)
    return _prompt_key("", str(prompt))


def to_prompt(row: dict) -> dict:
    msgs = []
    if row["instruction"]:
        msgs.append({"role": "system", "content": row["instruction"]})
    msgs.append({"role": "user", "content": row["input"]})
    return {"prompt": msgs}


def self_test() -> int:
    """CPU-only smoke of the reward + prompt plumbing — proves the RLVR signal is
    right before renting a GPU. No model, no trl."""
    rows = [
        {"instruction": "Pick the best lane.", "input": "node=researcher class=code_fix",
         "output": "reasoning: cheap first\nroute: kimi_lens"},
        {"instruction": "Pick the best lane.", "input": "node=reviewer class=code_fix",
         "output": "route: opus_lens"},
        {"instruction": "", "input": "node=implementer class=framework_edit",
         "output": "route: codex_lens"},
    ]
    p = Path("/tmp/_grpo_selftest.json"); p.write_text(json.dumps(rows))
    loaded = load_route_rows(p)
    assert len(loaded) == 3, loaded
    rf = build_reward_fn(loaded)
    # reconstruct trl-shaped prompts + completions and check the reward is exact
    prompts = [to_prompt(r)["prompt"] for r in loaded]
    good = [[{"role": "assistant", "content": f"route: {r['gold']}"}] for r in loaded]
    wrongvalid = [[{"role": "assistant", "content": "route: opus_lens"}] for _ in loaded]
    garbage = [[{"role": "assistant", "content": "i think maybe use the good one"}] for _ in loaded]
    rg = rf(prompts, good)
    rw = rf(prompts, wrongvalid)
    rgar = rf(prompts, garbage)
    assert rg == [1.0, 1.0, 1.0], rg
    # row 1 gold IS opus_lens → exact 1.0; others valid-but-wrong → 0.2
    assert rw == [0.2, 1.0, 0.2], rw
    assert rgar == [0.0, 0.0, 0.0], rgar
    print("SELF-TEST PASS — RLVR route reward is exact:")
    print(f"  gold-match reward:        {rg}  (all 1.0)")
    print(f"  valid-but-wrong reward:   {rw}  (0.2 except the row whose gold IS opus)")
    print(f"  garbage reward:           {rgar}  (all 0.0)")
    print(f"  {len(loaded)} rows, {len(rf.valid_routes)} distinct valid lanes")
    return 0


def build_dataset_from_db(db: Path, out: Path, min_samples: int = 1) -> int:
    """Turn mini-ork's live learning data into RLVR route examples — this is what
    closes the flywheel: reward_g-stamped runs → agent_performance_memory advantages
    → (per-slice best lane = gold) → GRPO training data. Emits {instruction, input,
    output} rows where output is `route: <argmax-advantage lane>` for each
    (node_type/role, task_class) slice that clears the sample floor."""
    import sqlite3
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT role, task_class, agent_version_id, relative_advantage, runs_count "
            "FROM agent_performance_memory WHERE agent_version_id != '' "
            "AND runs_count >= ? ORDER BY role, task_class, relative_advantage DESC",
            (min_samples,)).fetchall()
    except sqlite3.Error as e:
        sys.stderr.write(f"agent_performance_memory unreadable: {e}\n"); return 1
    finally:
        con.close()
    best: dict[tuple, str] = {}
    for role, tc, lane, adv, n in rows:
        best.setdefault((role or "", tc or ""), lane)  # first = highest adv (ORDER BY)
    examples = [{
        "instruction": "You are mini-ork's lane router. Given the node role and task "
                       "class, emit the single best model lane on a `route:` line.",
        "input": f"role={role} task_class={tc}",
        "output": f"route: {lane}",
    } for (role, tc), lane in sorted(best.items())]
    out.write_text(json.dumps(examples, indent=1))
    print(f"[build-data] {len(examples)} route examples ({len({l for l in best.values()})} "
          f"distinct gold lanes) → {out}")
    return 0 if examples else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="CPU-only: validate the reward + data plumbing, no GPU/trl")
    ap.add_argument("--build-data", type=Path, metavar="STATE_DB",
                    help="build route_train.json from mini-ork's state.db (writes --out) then exit")
    ap.add_argument("--model", default="Qwen/Qwen3-4B-Instruct-2507")
    ap.add_argument("--data", type=Path, help="route train json/jsonl {instruction,input,output}")
    ap.add_argument("--heldout", type=Path, help="held-out route test set for the gate")
    ap.add_argument("--out", default="grpo-out")
    ap.add_argument("--num-generations", type=int, default=8, help="G rollouts per prompt")
    ap.add_argument("--max-steps", type=int, default=500)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--beta", type=float, default=0.04, help="KL coefficient to the ref policy")
    ap.add_argument("--max-prompt-len", type=int, default=1024)
    ap.add_argument("--max-completion-len", type=int, default=256)
    ap.add_argument("--gate-min-acc", type=float, default=0.0,
                    help="fail (rc 2) if held-out route acc < this")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.build_data:
        return build_dataset_from_db(args.build_data, Path(args.out if args.out.endswith(".json")
                                                          else "route_train.json"))
    if not args.data:
        ap.error("--data is required (or use --self-test / --build-data)")

    rows = load_route_rows(args.data)
    if not rows:
        sys.stderr.write("no RLVR-usable rows (none carry a checkable `route:` gold)\n")
        return 1
    reward_fn = build_reward_fn(rows)
    print(f"[grpo] {len(rows)} route prompts, {len(reward_fn.valid_routes)} distinct gold lanes")

    # Heavy deps imported lazily so --self-test stays CPU/trl-free.
    try:
        import torch  # noqa: F401
        from datasets import Dataset
        from peft import LoraConfig
        from trl import GRPOConfig, GRPOTrainer
    except ImportError as e:
        sys.stderr.write(
            f"training deps missing ({e}). Install on the GPU host: "
            "pip install 'trl>=0.14' peft datasets transformers accelerate torch\n"
            "Then re-run without --self-test on a GPU.\n")
        return 3

    train_ds = Dataset.from_list([to_prompt(r) for r in rows])
    # LoRA target modules mirror local_lora_train.py so the adapter is drop-in
    # compatible with the existing eval/serve path.
    peft_cfg = LoraConfig(
        r=16, lora_alpha=32, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"])
    cfg = GRPOConfig(
        output_dir=args.out,
        learning_rate=args.lr,
        beta=args.beta,                       # KL leash to the frozen ref policy
        num_generations=args.num_generations,  # G — the group size GRPO averages over
        max_prompt_length=args.max_prompt_len,
        max_completion_length=args.max_completion_len,
        max_steps=args.max_steps,
        per_device_train_batch_size=args.num_generations,
        gradient_accumulation_steps=4,
        logging_steps=10, save_steps=100, bf16=True,
        temperature=1.0,                      # sample diverse rollouts to score
        report_to=[],
    )
    trainer = GRPOTrainer(
        model=args.model,
        reward_funcs=[reward_fn],             # the verifiable RLVR reward
        args=cfg,
        train_dataset=train_ds,
        peft_config=peft_cfg,
    )
    trainer.train()
    trainer.save_model(args.out)
    print(f"[grpo] adapter saved → {args.out}")

    # Gate on held-out route accuracy (NOT train loss) via the existing eval.
    if args.heldout:
        import subprocess
        rep = str(Path(args.out) / "route_eval.json")
        subprocess.run([sys.executable, str(Path(__file__).parent / "local_eval_route.py"),
                        "--base", args.model, "--adapter", args.out,
                        "--data", str(args.heldout), "--out", rep], check=False)
        try:
            acc = json.loads(Path(rep).read_text()).get("tuned_route_acc", 0.0)
            base = json.loads(Path(rep).read_text()).get("base_route_acc", 0.0)
            print(f"[gate] held-out route acc: base={base} tuned={acc}")
            if acc < args.gate_min_acc:
                sys.stderr.write(f"[gate] FAIL tuned_route_acc {acc} < {args.gate_min_acc}\n")
                return 2
        except Exception as e:
            sys.stderr.write(f"[gate] eval report unreadable: {e}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
