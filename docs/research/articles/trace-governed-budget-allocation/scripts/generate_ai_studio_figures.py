#!/usr/bin/env python3
"""Generate local paper figures with Google AI Studio.

This mirrors the blog's `scripts/generate-blog-image.ts` flow in local-only
form: use an AI Studio Gemini image-preview model, request image output, and
write the returned inline image bytes into this arXiv package.
"""

from __future__ import annotations

import base64
import argparse
import json
import os
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path


ARTICLE_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ARTICLE_ROOT / "assets" / "ai-studio-figures"
PROMPT_DIR = ARTICLE_ROOT / "assets" / "prompts"
DEFAULT_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3-pro-image")

STYLE_PROMPT = """\
A clean academic technical diagram on a pure white background (#ffffff).
No cream paper, no beige tint, no paper grain, no texture, no shadows, no
photorealism. Use crisp black and charcoal strokes, geometric shapes, arrows,
and minimal English labels. Use restrained desaturated accents: sage, soft blue,
muted violet, and one red emphasis. Do not add a figure number, title, caption,
watermark, author name, logo, or explanatory paragraph inside the image. The
LaTeX paper will provide the caption separately. Keep every label short,
spelled correctly, and horizontally readable.
"""

FIGURES = [
    {
        "name": "fig1-trace-governed-loop.png",
        "aspect": "16:9",
        "prompt": """\
Create a mini-ork control-loop diagram with exactly these main boxes:
"Recipe DAG", "Durable trace", "Routing policy", "Budget gate",
"Deterministic verifier".

Inside "Recipe DAG", show four small nodes labeled "planner", "implementer",
"reviewer", "verifier". Inside "Durable trace", show four compact event cards
labeled "lane", "cost", "verifier", "retry". Inside "Routing policy", show two
choices labeled "frontier lane" and "cheap lane".

Use arrows to show:
Recipe DAG -> Durable trace -> Routing policy -> agent attempt -> Durable trace.
Budget gate and Deterministic verifier feed back into Routing policy.
Do not include any title, figure number, or caption inside the image.
""",
    },
    {
        "name": "fig2-policy-decision.png",
        "aspect": "16:9",
        "prompt": """\
Create a compact routing decision diagram with exactly these labels:
"next node", "trace context", "escalation score", "failure / retry / risk?",
"frontier lane", "cheap worker lane", "budget check", "stop / rollback /
human gate", "proceed".

Flow: next node + trace context -> escalation score -> decision diamond. The
"yes" branch goes to frontier lane. The "no" branch goes to cheap worker lane.
Both lanes go to budget check. Budget exceeded goes to stop / rollback / human
gate. Budget ok goes to proceed.

Do not include any title, figure number, caption, duplicate words, or long
sentences inside the image.
""",
    },
    {
        "name": "fig3-cost-result.png",
        "aspect": "16:9",
        "prompt": """\
Create a clean two-panel benchmark summary chart with exact policy labels:
"frontier-only", "cheap-only", "static role mapping", "trace-governed".

Left panel label: "cost per success". Right panel label: "expensive calls".
Left panel relative bar heights: frontier-only highest, cheap-only second,
static role mapping third, trace-governed lowest. Right panel relative bar
heights: frontier-only highest, static role mapping second, trace-governed
third, cheap-only lowest.

Add one compact callout: "26.63% lower cost vs frontier-only".
Do not claim trace-governed has the fewest calls. Do not include a title,
figure number, caption, or extra explanatory paragraph inside the image.
""",
    },
]


def load_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    raise SystemExit("GEMINI_API_KEY is required in the environment")


def request_image(api_key: str, prompt: str, *, model: str) -> tuple[bytes, str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI Studio request failed: HTTP {exc.code}: {details}") from exc

    for candidate in body.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                return base64.b64decode(inline["data"]), mime
    raise RuntimeError(f"AI Studio response contained no inline image data: {json.dumps(body)[:1000]}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate arXiv paper figures with Google Nano Banana image models.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        choices=["gemini-2.5-flash-image", "gemini-3.1-flash-image", "gemini-3-pro-image"],
        help="Gemini image model. Defaults to Nano Banana Pro / gemini-3-pro-image.",
    )
    parser.add_argument(
        "--image-size",
        default="2K",
        choices=["1K", "2K", "4K"],
        help="Image size for gemini-3.1-flash-image and gemini-3-pro-image.",
    )
    parser.add_argument(
        "--prompts-only",
        action="store_true",
        help="Write prompt files without calling the Gemini API.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = "" if args.prompts_only else load_api_key()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)

    for spec in FIGURES:
        final_prompt = textwrap.dedent(
            f"""\
            Generate the image with a {spec["aspect"]} aspect ratio. The canvas must be {spec["aspect"]}.

            {STYLE_PROMPT}

            SUBJECT:
            {spec["prompt"]}
            """
        ).strip()
        prompt_path = PROMPT_DIR / f"{Path(spec['name']).stem}.prompt.txt"
        prompt_path.write_text(final_prompt + "\n", encoding="utf-8")
        if args.prompts_only:
            print(f"Wrote prompt {prompt_path}", file=sys.stderr)
            continue
        print(f"Generating {spec['name']} with {args.model}...", file=sys.stderr)
        image, mime = request_image(
            api_key,
            final_prompt,
            model=args.model,
        )
        if mime == "image/jpeg":
            out_path = OUT_DIR / spec["name"].replace(".png", ".jpg")
        else:
            out_path = OUT_DIR / spec["name"]
        out_path.write_bytes(image)
        print(f"Wrote {out_path} ({len(image)} bytes, {mime})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
