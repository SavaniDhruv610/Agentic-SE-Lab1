"""Statistics of exported OpenCode sessions: prompts, model calls, tokens, cost, tools, context growth.

Input: the JSON files written by `opencode export <sessionID> > conversation/session_01.json`.

    python3 task1_opencode/session_stats.py                          # all files in task1_opencode/conversation/
    python3 task1_opencode/session_stats.py path/to/session.json     # specific files
    python3 task1_opencode/session_stats.py --md task1_opencode/conversation/stats.md    # also save as Markdown
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_session(path: Path) -> dict:
    """Read one exported session and add up its numbers."""
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw[raw.index("{"):])          # the export may start with a status line
    messages = data["messages"]
    answers = [m["info"] for m in messages if m["info"]["role"] == "assistant"]
    tokens = [a.get("tokens", {}) for a in answers]

    tools = Counter(part.get("tool", "?") for m in messages for part in m["parts"] if part["type"] == "tool")
    time = data.get("info", {}).get("time", {})
    return {
        "file": path.name,
        "title": data.get("info", {}).get("title", ""),
        "models": sorted({a.get("modelID", "?") for a in answers}),
        "prompts": sum(m["info"]["role"] == "user" for m in messages),
        "model_calls": len(answers),
        "tool_calls": sum(tools.values()),
        "tools": tools,
        "input": sum(t.get("input", 0) for t in tokens),
        "cached": sum(t.get("cache", {}).get("read", 0) for t in tokens),
        "output": sum(t.get("output", 0) for t in tokens),
        "reasoning": sum(t.get("reasoning", 0) for t in tokens),
        "cost": sum(a.get("cost") or 0 for a in answers),
        "minutes": (time.get("updated", 0) - time.get("created", 0)) / 60000,
        # what was sent to the model in each call = new input tokens + tokens read from the cache
        "context_per_call": [t.get("input", 0) + t.get("cache", {}).get("read", 0) for t in tokens],
    }


def summary_table(sessions: list[dict]) -> str:
    header = ["session", "prompts", "model calls", "tool calls", "input", "cached input", "output", "reasoning",
              "cost (USD)", "minutes"]
    rows = [[s["file"], s["prompts"], s["model_calls"], s["tool_calls"], s["input"], s["cached"], s["output"],
             s["reasoning"], f"{s['cost']:.5f}", f"{s['minutes']:.1f}"] for s in sessions]
    if len(sessions) > 1:
        numeric = ["prompts", "model_calls", "tool_calls", "input", "cached", "output", "reasoning"]
        rows.append(["TOTAL"] + [sum(s[key] for s in sessions) for key in numeric]
                    + [f"{sum(s['cost'] for s in sessions):.5f}", f"{sum(s['minutes'] for s in sessions):.1f}"])
    lines = ["| " + " | ".join(header) + " |", "|---|" + "---:|" * (len(header) - 1)]
    lines += ["| " + " | ".join(f"{cell:,}" if isinstance(cell, int) else str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def session_details(s: dict, width: int = 40) -> str:
    lines = [f"### {s['file']}: {s['title']}", "",
             f"- Model: {', '.join(s['models'])}",
             # "USD" instead of "$": two dollar signs on one line are rendered as a math formula in Markdown
             f"- Cost: {s['cost']:.5f} USD for {s['prompts']} prompt(s) and {s['model_calls']} model calls "
             f"({s['cost'] / max(s['model_calls'], 1):.5f} USD per call)",
             "- Tools: " + (", ".join(f"{name} x{count}" for name, count in s["tools"].most_common()) or "none")]
    context = s["context_per_call"]
    if context:
        lines += [f"- Tokens sent to the model per call: first {context[0]:,}, last {context[-1]:,} "
                  f"(x{context[-1] / max(context[0], 1):.1f}), sum {sum(context):,}", "",
                  "Context size per model call (every call re-sends the whole conversation so far):", "", "```"]
        largest = max(context)
        for number, size in enumerate(context, start=1):
            lines.append(f"call {number:>3}  {'#' * max(1, round(size / largest * width)):<{width}}  {size:>8,}")
        lines.append("```")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Statistics of exported OpenCode sessions.")
    parser.add_argument("files", nargs="*", type=Path, help="exported session JSON files (default: conversation/*.json)")
    parser.add_argument("--md", type=Path, help="also write the report to this Markdown file")
    args = parser.parse_args()

    files = args.files or sorted((HERE / "conversation").glob("*.json"))
    if not files:
        raise SystemExit("No session files found. Export one first: opencode export <sessionID> > conversation/session_01.json")
    sessions = [load_session(path) for path in files]

    report = "\n\n".join(["# OpenCode session statistics", summary_table(sessions),
                          "input = new input tokens, cached input = input tokens served from the provider cache "
                          "(cheaper), reasoning = hidden reasoning tokens (billed as output)."]
                         + [session_details(s) for s in sessions])
    print(report)
    if args.md:
        args.md.write_text(report + "\n", encoding="utf-8")
        print(f"\nSaved: {args.md}")


if __name__ == "__main__":
    main()
