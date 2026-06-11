#!/usr/bin/env python3
"""Generate paper figures using the same tool pattern as arXiv:2606.10662.

The DeLM source package for arXiv:2606.10662 embeds pre-rendered PDFs:
Matplotlib PDFs for quantitative plots and HeadlessChrome-rendered PDFs for
architecture diagrams. This script mirrors that approach with white-background
vector assets suitable for arXiv.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import matplotlib.pyplot as plt


ARTICLE_ROOT = Path(__file__).resolve().parents[1]
FIG_DIR = ARTICLE_ROOT / "assets" / "deterministic-figures"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


BASE_CSS = """
@page { size: 13.6in 7.65in; margin: 0; }
html, body {
  margin: 0;
  width: 13.6in;
  height: 7.65in;
  background: #ffffff;
}
body {
  font-family: Inter, Helvetica, Arial, sans-serif;
}
svg {
  display: block;
  width: 13.6in;
  height: 7.65in;
  background: #ffffff;
}
.box { fill: #ffffff; stroke: #1f2937; stroke-width: 2.2; rx: 14; }
.soft-blue { fill: #dbeafe; stroke: #1d4ed8; }
.soft-green { fill: #dcfce7; stroke: #15803d; }
.soft-violet { fill: #ede9fe; stroke: #6d28d9; }
.soft-red { fill: #fee2e2; stroke: #b91c1c; }
.soft-amber { fill: #fef3c7; stroke: #b45309; }
.label { font-size: 25px; font-weight: 700; fill: #111827; }
.small { font-size: 18px; font-weight: 600; fill: #374151; }
.tiny { font-size: 15px; font-weight: 500; fill: #4b5563; }
.micro { font-size: 13px; font-weight: 500; fill: #4b5563; }
.arrow { stroke: #111827; stroke-width: 3; fill: none; marker-end: url(#arrow); }
.muted { stroke: #6b7280; stroke-width: 2.2; fill: none; marker-end: url(#arrow-muted); }
"""


def render_html_pdf(name: str, svg_body: str) -> None:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>{BASE_CSS}</style>
</head>
<body>
<svg viewBox="0 0 1360 765" xmlns="http://www.w3.org/2000/svg">
<defs>
  <marker id="arrow" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto">
    <path d="M2,2 L10,6 L2,10 Z" fill="#111827" />
  </marker>
  <marker id="arrow-muted" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto">
    <path d="M2,2 L10,6 L2,10 Z" fill="#6b7280" />
  </marker>
</defs>
{svg_body}
</svg>
</body>
</html>
"""
    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / f"{name}.html"
        html_path.write_text(html, encoding="utf-8")
        out_path = FIG_DIR / f"{name}.pdf"
        subprocess.run(
            [
                str(CHROME),
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={out_path}",
                html_path.as_uri(),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


def diagram_trace_loop() -> None:
    render_html_pdf(
        "trace_governed_loop",
        """
<rect x="45" y="120" width="250" height="240" class="box soft-blue"/>
<text x="170" y="165" text-anchor="middle" class="label">Recipe DAG</text>
<circle cx="120" cy="225" r="42" fill="#ffffff" stroke="#1f2937" stroke-width="2"/>
<text x="120" y="221" text-anchor="middle" class="small">planner</text>
<text x="120" y="246" text-anchor="middle" class="tiny">plan</text>
<circle cx="222" cy="225" r="42" fill="#ffffff" stroke="#1f2937" stroke-width="2"/>
<text x="222" y="221" text-anchor="middle" class="small">worker</text>
<text x="222" y="246" text-anchor="middle" class="tiny">edit</text>
<circle cx="120" cy="310" r="36" fill="#ffffff" stroke="#1f2937" stroke-width="2"/>
<text x="120" y="315" text-anchor="middle" class="small">review</text>
<rect x="188" y="282" width="72" height="56" class="box" transform="rotate(45 224 310)"/>
<text x="224" y="314" text-anchor="middle" class="small">verify</text>
<path d="M158 225 L180 225" class="arrow"/>
<path d="M190 252 L150 288" class="arrow"/>
<path d="M154 310 L190 310" class="arrow"/>

<rect x="405" y="120" width="445" height="240" class="box soft-amber"/>
<text x="628" y="165" text-anchor="middle" class="label">Durable trace</text>
<g transform="translate(445 205)">
  <rect width="86" height="95" class="box"/>
  <text x="43" y="32" text-anchor="middle" class="small">lane</text>
  <text x="43" y="64" text-anchor="middle" class="micro">cheap</text>
  <text x="43" y="82" text-anchor="middle" class="micro">frontier</text>
</g>
<g transform="translate(555 205)">
  <rect width="86" height="95" class="box"/>
  <text x="43" y="32" text-anchor="middle" class="small">cost</text>
  <text x="43" y="68" text-anchor="middle" class="tiny">USD</text>
</g>
<g transform="translate(665 205)">
  <rect width="86" height="95" class="box"/>
  <text x="43" y="32" text-anchor="middle" class="small">verifier</text>
  <text x="43" y="68" text-anchor="middle" class="tiny">pass/fail</text>
</g>
<g transform="translate(775 205)">
  <rect width="86" height="95" class="box"/>
  <text x="43" y="32" text-anchor="middle" class="small">retry</text>
  <text x="43" y="68" text-anchor="middle" class="tiny">count</text>
</g>

<rect x="990" y="120" width="300" height="240" class="box soft-green"/>
<text x="1140" y="165" text-anchor="middle" class="label">Routing policy</text>
<polygon points="1140,205 1195,260 1140,315 1085,260" fill="#ffffff" stroke="#1f2937" stroke-width="2.2"/>
<text x="1140" y="255" text-anchor="middle" class="small">choose</text>
<text x="1140" y="278" text-anchor="middle" class="small">lane</text>
<rect x="1215" y="185" width="55" height="55" class="box soft-violet"/>
<text x="1243" y="218" text-anchor="middle" class="tiny">frontier</text>
<rect x="1215" y="280" width="55" height="55" class="box soft-blue"/>
<text x="1243" y="313" text-anchor="middle" class="tiny">cheap</text>

<rect x="415" y="480" width="250" height="130" class="box soft-red"/>
<text x="540" y="535" text-anchor="middle" class="label">Budget gate</text>
<text x="540" y="568" text-anchor="middle" class="small">remaining spend</text>
<rect x="710" y="480" width="250" height="130" class="box soft-violet"/>
<text x="835" y="535" text-anchor="middle" class="label">Verifier</text>
<text x="835" y="568" text-anchor="middle" class="small">deterministic check</text>

<path d="M295 240 L405 240" class="arrow"/>
<path d="M850 240 L990 240" class="arrow"/>
<path d="M1140 360 C1140 430 930 445 835 480" class="arrow"/>
<path d="M835 480 C780 410 670 380 628 360" class="muted"/>
<path d="M540 480 C540 410 585 395 628 360" class="muted"/>
<path d="M665 545 L710 545" class="arrow"/>
<path d="M960 545 C1110 545 1235 445 1235 335" class="muted"/>
<text x="350" y="220" text-anchor="middle" class="small">attempt</text>
<text x="920" y="220" text-anchor="middle" class="small">trace prefix</text>
""",
    )


def diagram_policy_decision() -> None:
    render_html_pdf(
        "policy_decision",
        """
<rect x="560" y="60" width="240" height="70" class="box soft-blue"/>
<text x="680" y="104" text-anchor="middle" class="label">next node</text>
<rect x="265" y="175" width="230" height="70" class="box soft-amber"/>
<text x="380" y="218" text-anchor="middle" class="label">trace context</text>
<rect x="560" y="175" width="240" height="70" class="box soft-amber"/>
<text x="680" y="218" text-anchor="middle" class="label">escalation score</text>
<polygon points="680,295 835,390 680,485 525,390" fill="#ffffff" stroke="#1f2937" stroke-width="2.5"/>
<text x="680" y="372" text-anchor="middle" class="small">failure / retry</text>
<text x="680" y="400" text-anchor="middle" class="small">/ risk?</text>
<rect x="250" y="565" width="250" height="80" class="box soft-violet"/>
<text x="375" y="614" text-anchor="middle" class="label">frontier lane</text>
<rect x="860" y="565" width="270" height="80" class="box soft-green"/>
<text x="995" y="614" text-anchor="middle" class="label">cheap worker lane</text>
<rect x="560" y="560" width="240" height="80" class="box"/>
<text x="680" y="609" text-anchor="middle" class="label">budget check</text>
<rect x="1040" y="365" width="260" height="85" class="box soft-red"/>
<text x="1170" y="400" text-anchor="middle" class="small">stop / rollback</text>
<text x="1170" y="428" text-anchor="middle" class="small">/ human gate</text>
<circle cx="680" cy="700" r="30" fill="#dcfce7" stroke="#15803d" stroke-width="2.5"/>
<text x="680" y="708" text-anchor="middle" class="small">proceed</text>

<path d="M680 130 L680 175" class="arrow"/>
<path d="M495 210 L560 210" class="arrow"/>
<path d="M680 245 L680 295" class="arrow"/>
<path d="M545 430 C420 460 375 500 375 565" class="arrow"/>
<text x="430" y="455" text-anchor="middle" class="small">yes</text>
<path d="M815 430 C940 460 995 500 995 565" class="arrow"/>
<text x="920" y="455" text-anchor="middle" class="small">no</text>
<path d="M500 600 L560 600" class="arrow"/>
<path d="M860 600 L800 600" class="arrow"/>
<path d="M800 575 C930 505 1040 450 1080 430" class="arrow"/>
<text x="930" y="548" text-anchor="middle" class="small">exceeds budget</text>
<path d="M680 640 L680 670" class="arrow"/>
<text x="724" y="667" class="small">ok</text>
""",
    )


def benchmark_cost_calls() -> None:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    policies = ["Frontier", "Cheap", "Static", "Trace"]
    cost = [0.536418, 0.435822, 0.416641, 0.393587]
    calls = [67, 12, 31, 27]
    colors = ["#94a3b8", "#93c5fd", "#c4b5fd", "#86efac"]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.8, 3.3), facecolor="white")
    ax1.bar(policies, cost, color=colors, edgecolor="#1f2937", linewidth=0.8)
    ax1.set_ylabel("USD / successful task")
    ax1.set_title("Cost per success")
    ax1.set_ylim(0, 0.62)
    for idx, value in enumerate(cost):
        ax1.text(idx, value + 0.014, f"{value:.3f}", ha="center", fontsize=9)

    ax2.bar(policies, calls, color=colors, edgecolor="#1f2937", linewidth=0.8)
    ax2.set_ylabel("Calls")
    ax2.set_title("Expensive model calls")
    ax2.set_ylim(0, 75)
    for idx, value in enumerate(calls):
        ax2.text(idx, value + 1.3, str(value), ha="center", fontsize=9)

    for ax in (ax1, ax2):
        ax.set_facecolor("white")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(axis="y", alpha=0.18)
    fig.suptitle("Controlled 48-run mini-ork benchmark", fontsize=12, y=1.03)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "benchmark_cost_calls.pdf", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def main() -> None:
    if not CHROME.exists():
        raise SystemExit(f"Google Chrome not found at {CHROME}")
    diagram_trace_loop()
    diagram_policy_decision()
    benchmark_cost_calls()


if __name__ == "__main__":
    main()
