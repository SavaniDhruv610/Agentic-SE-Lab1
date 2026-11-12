"""Task 2: our own small coding agent that builds the team website according to the style guide.

Run from the repository root:
    uv run python task2_own_agent/agent.py --content task1_opencode/content --out task2_own_agent/site/index.html

The protocol is hand-made (no native function calling): in every step the model answers with ONE
JSON object {"thought": ..., "action": ..., "args": {...}}. Our code executes the action and sends
the result back as an "OBSERVATION" user message.

    search_style_guide {"query": str}   -> retrieved style guide chunks (<= 1,000 tokens)
    read_profiles      {}               -> member profiles + available image files
    write_site         {} + ```html     -> placeholders resolved, file saved, checker report
    finish             {"summary": str} -> accepted only if the last checker report passed

Everything is logged to task2_own_agent/logs/run_XX.jsonl (LLM requests/responses + our events).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from common.embed_images import MIME_TYPES, embed_images  # noqa: E402
from common.llm_client import LLMClient, count_tokens  # noqa: E402
from common.site_checker import check_site, member_names_from_content  # noqa: E402
from rag import MAX_OBSERVATION_TOKENS, STYLE_GUIDE_DIR, StyleGuideRAG  # noqa: E402

ACTIONS = {
    # action -> {argument: (type, required)}
    "search_style_guide": {"query": (str, True)},
    "read_profiles": {},
    "write_site": {"html": (str, False)},
    "finish": {"summary": (str, True)},
}

# Note: this prompt contains no style guide text. The model has to retrieve every rule.
SYSTEM_PROMPT = """You are a web developer agent. You build a team website as ONE self-contained HTML file.
You work in steps. In every step you reply with exactly one JSON object and nothing before it:

{"thought": "short reasoning", "action": "<action name>", "args": {...}}

Available actions:

1. search_style_guide   args: {"query": "<what you want to know>"}
   Searches the "UPB Agentic SE Web Style Guide" and returns the most relevant passages with their document ids.
2. read_profiles        args: {}
   Returns the profile texts of all team members and the list of available image files.
3. write_site           args: {}
   Put the COMPLETE HTML document in a ```html code block directly after the JSON object. The page is saved and
   checked automatically; you get the checker report back.
4. finish               args: {"summary": "<what you built>"}
   Ends the task. It is only accepted after a write_site whose checker report passed completely.

Rules of the protocol:
- One action per reply. No text before the JSON object. Only write_site is followed by a code block.
- Images: never write image data or base64 yourself. Use a placeholder as the src, exactly like this:
  <img src="{{image:anna.jpg}}" alt="...">  The file name must be one of the available image files.
  The placeholder is replaced by the embedded image when the page is saved.
- Every write_site must contain the whole page, not a diff.

Requirements for the website:
- A single HTML file: all CSS and JavaScript inline, no external resources (no CDN, no web fonts, no links to
  stylesheets or scripts, no remote images).
- It presents every team member with their picture and the content of their profile (study background, interests,
  fun fact, their view on AI in software engineering today, their hot take), and it has a section that summarizes
  where the team thinks software engineering will be in 5-10 years.
- Responsive (works on phones and on desktop) and with a day/night theme toggle.
- Funny and creative in its texts and design, as far as the style guide allows.
- Use only facts from the profiles. Do not invent members or biographical details.
- The page must follow the style guide in every detail: exact colors, names, ids, texts, units and markup.

How to work with the style guide:
- You do not know the style guide. Do not guess its rules, look them up. Before the first write_site, search for
  every aspect of the page you are going to build, one focused query per aspect. A good first query asks for an
  overview or checklist of the rules; then follow up on every topic it mentions.
- Every passage shows the status of its document. The guide has changed over time: a passage marked DEPRECATED
  is NOT valid anymore. It names the document that replaces it; search for that topic again and use the ACTIVE rule.
- The checker report tells you which checks fail, but not what the rule says. For every failed check, search the
  style guide for the rule, then fix the page and write it again.
- When the report says that all checks passed, call finish."""

TASK_TEMPLATE = """Build the team website for our lab group and save it with write_site.
Start by reading the profiles and researching the style guide. You have at most {max_steps} steps."""

FEEDBACK_TEMPLATE = """FEEDBACK FROM THE USER (a human reviewed the page):
{feedback}

Revise the page accordingly (search the style guide again if needed), save it with write_site and call finish
when the checker passes. You have {max_steps} more steps."""

HTML_BLOCK = re.compile(r"```html[ \t]*\r?\n(.*)```", re.DOTALL | re.IGNORECASE)
OPEN_HTML_BLOCK = re.compile(r"```html[ \t]*\r?\n(.*)\Z", re.DOTALL | re.IGNORECASE)
BASE64_IMAGE = re.compile(r"data:image/[a-z0-9.+-]+;base64,", re.IGNORECASE)


class ProtocolError(Exception):
    """The model's reply does not follow the protocol. The message is sent back as an observation."""


def parse_reply(reply: str) -> tuple[str, dict, str | None]:
    """Model reply -> (action, args, html). Raises ProtocolError with a message for the model."""
    if not reply or not reply.strip():
        raise ProtocolError("Your reply was empty (maybe it was cut off). Reply with one JSON object.")

    # 1. Separate the HTML code block from the rest, so CSS braces can't be mistaken for the JSON object.
    html = None
    rest = reply
    block = HTML_BLOCK.search(reply)
    if block:
        html, rest = block.group(1), reply[: block.start()] + reply[block.end():]
    else:
        open_block = OPEN_HTML_BLOCK.search(reply)
        if open_block:      # no closing fence
            if "</html>" not in open_block.group(1).lower():
                raise ProtocolError(
                    "Your HTML code block is not closed and the document does not end with </html>: the reply was "
                    "probably cut off. Send the complete page again, more compact if necessary."
                )
            html, rest = open_block.group(1), reply[: open_block.start()]

    # 2. The first JSON object in the remaining text.
    decoder = json.JSONDecoder()
    step = None
    error = "no JSON object found"
    for match in re.finditer(r"\{", rest):
        try:
            candidate, _ = decoder.raw_decode(rest, match.start())
        except json.JSONDecodeError as exc:
            if error == "no JSON object found":
                error = f"{exc.msg} at character {exc.pos}"
            continue
        if isinstance(candidate, dict):
            step = candidate
            break
    if step is None:
        raise ProtocolError(
            f"Your reply is not valid JSON ({error}). Reply with exactly one JSON object: "
            '{"thought": "...", "action": "...", "args": {...}}. For write_site put the HTML into a ```html code '
            "block after the JSON object, not inside it."
        )

    # 3. Validate action and arguments.
    action = step.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise ProtocolError(f"Unknown action {action!r}. Available actions: {', '.join(ACTIONS)}.")
    args = step.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ProtocolError(f'"args" must be a JSON object, e.g. {{}} – got {type(args).__name__}.')
    spec = ACTIONS[action]
    unexpected = [key for key in args if key not in spec]
    if unexpected:
        raise ProtocolError(f"Unexpected argument(s) for {action}: {unexpected}. Allowed: {list(spec) or 'none'}.")
    for key, (expected_type, required) in spec.items():
        if key not in args:
            if required:
                raise ProtocolError(f'Action {action} needs the argument "{key}" ({expected_type.__name__}).')
            continue
        if not isinstance(args[key], expected_type) or (required and not args[key].strip()):
            raise ProtocolError(f'Argument "{key}" of {action} must be a non-empty {expected_type.__name__}.')

    if action == "write_site":
        html = html if html is not None else args.get("html")
        if not html or not html.strip():
            raise ProtocolError("write_site needs the complete HTML document in a ```html code block after the JSON object.")
    return action, args, html


def compact_history(messages: list[dict]) -> None:
    """Keep only the newest HTML page in the history; older pages are replaced by a short note.

    Every write_site reply contains the whole page (several thousand tokens). Without this, a run with
    five attempts would send the first page five more times.
    """
    with_html = [m for m in messages if m["role"] == "assistant" and HTML_BLOCK.search(m["content"] or "")]
    for message in with_html[:-1]:
        message["content"] = HTML_BLOCK.sub(
            lambda m: f"```html\n[{len(m.group(1))} characters of HTML omitted – replaced by a later version]\n```",
            message["content"],
        )


class Agent:
    def __init__(self, llm: LLMClient, rag: StyleGuideRAG, content_dir: Path, out_path: Path, k: int = 5,
                 max_steps: int = 25, max_tokens: int | None = None, verbose: bool = True) -> None:
        self.llm, self.rag = llm, rag
        self.content_dir, self.out_path = Path(content_dir), Path(out_path)
        self.images_dir = self.content_dir / "images"
        self.k, self.max_steps, self.max_tokens, self.verbose = k, max_steps, max_tokens, verbose
        self.member_names = member_names_from_content(self.content_dir)
        self.last_report = None          # CheckReport of the last successful write_site
        self.step = 0                    # global step counter (for the log)
        self.messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": TASK_TEMPLATE.format(max_steps=max_steps)},
        ]

    # ------------------------------------------------------------ actions

    def search_style_guide(self, query: str) -> str:
        chunks = self.rag.search(query, self.k)
        text = self.rag.format_observation(chunks, MAX_OBSERVATION_TOKENS)
        assert count_tokens(text) <= MAX_OBSERVATION_TOKENS
        return f'Style guide passages for "{query}" (most relevant first):\n\n{text}'

    def read_profiles(self) -> str:
        profiles = sorted(p for p in self.content_dir.glob("*.md") if not p.name.startswith("_"))
        if not profiles:
            return f"ERROR: no profiles found in {self.content_dir} (expected <firstname>.md files)."
        parts = []
        for path in profiles:
            text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)   # template comments
            parts.append(f"=== {path.name} ===\n{re.sub(chr(10) + r'{3,}', chr(10) * 2, text).strip()}")
        images = self._image_files()
        parts.append("Available image files (use as {{image:<file>}}): " + (", ".join(images) or "none"))
        return "\n\n".join(parts)

    def _image_files(self) -> list[str]:
        if not self.images_dir.is_dir():
            return []
        return sorted(p.name for p in self.images_dir.iterdir() if p.suffix.lower() in MIME_TYPES)

    def write_site(self, html: str) -> str:
        if BASE64_IMAGE.search(html):
            return ("ERROR: the page contains base64 image data. Never write image data yourself. Use the placeholder "
                    "{{image:<file>}} as the src of every image. Nothing was saved.")
        try:
            final_html = embed_images(html, self.images_dir)
        except (FileNotFoundError, ValueError, OSError) as exc:
            return (f"ERROR: {exc}. Available image files: {', '.join(self._image_files()) or 'none'}. "
                    "Nothing was saved.")
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.write_text(final_html, encoding="utf-8")
        size = self.out_path.stat().st_size
        self.last_report = check_site(final_html, mode="full", member_names=self.member_names, file_size=size)
        verdict = ("All checks passed. You can call finish." if self.last_report.passed
                   else "Some checks FAILED. Look up the rules for the failed checks in the style guide, fix the page "
                        "and call write_site again.")
        return f"Saved {self.out_path.name} ({size / 1024:.0f} KB). Checker report:\n{self.last_report.summary()}\n{verdict}"

    def finish(self) -> tuple[bool, str]:
        if self.last_report is None:
            return False, "finish rejected: no page has been saved yet. Call write_site first."
        if not self.last_report.passed:
            return False, ("finish rejected: the last checker report has failing checks:\n"
                           + self.last_report.summary(only_failures=True))
        return True, "finish accepted."

    # ------------------------------------------------------------ loop

    def run_until_finish(self) -> str:
        """Run steps until an accepted finish ("finished") or the step limit ("max_steps")."""
        for _ in range(self.max_steps):
            self.step += 1
            compact_history(self.messages)
            reply = self.llm.chat(self.messages, max_tokens=self.max_tokens)
            self.messages.append({"role": "assistant", "content": reply})

            finished = False
            try:
                action, args, html = parse_reply(reply)
            except ProtocolError as exc:
                action, observation = "invalid", f"ERROR: {exc}"
            else:
                if action == "search_style_guide":
                    observation = self.search_style_guide(args["query"])
                elif action == "read_profiles":
                    observation = self.read_profiles()
                elif action == "write_site":
                    observation = self.write_site(html)
                else:
                    finished, observation = self.finish()

            self.llm.log_event({"step": self.step, "event": "observation", "action": action,
                                "observation": observation, "finished": finished})
            if self.verbose:
                detail = args.get("query") or args.get("summary") or "" if action != "invalid" else ""
                print(f"[step {self.step}] {action} {detail[:90]}")
                if action in ("invalid", "write_site") or (action == "finish" and not finished):
                    shown = [line for line in observation.splitlines() if not line.startswith("[PASS]")]
                    print("    " + "\n    ".join(shown)[:2000])
            if finished:
                return "finished"
            self.messages.append({"role": "user", "content": f"OBSERVATION:\n{observation}"})
        return "max_steps"

    def add_feedback(self, feedback: str) -> None:
        self.llm.log_event({"step": self.step, "event": "human_feedback", "feedback": feedback})
        self.messages.append({"role": "user",
                              "content": FEEDBACK_TEMPLATE.format(feedback=feedback, max_steps=self.max_steps)})


def next_log_path(log_dir: Path) -> Path:
    numbers = [int(m.group(1)) for p in log_dir.glob("run_*.jsonl") if (m := re.fullmatch(r"run_(\d+)", p.stem))]
    return log_dir / f"run_{max(numbers, default=0) + 1:02d}.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description="Task 2: build the team website with our own agent.")
    parser.add_argument("--content", type=Path, default=HERE.parent / "task1_opencode" / "content",
                        help="directory with <firstname>.md profiles and images/")
    parser.add_argument("--out", type=Path, default=HERE / "site" / "index.html")
    parser.add_argument("--style-guide", type=Path, default=STYLE_GUIDE_DIR)
    parser.add_argument("--model", help="chat model (default: $LAB_CHAT_MODEL)")
    parser.add_argument("--embedding-model", help="embedding model (default: $LAB_EMBEDDING_MODEL)")
    parser.add_argument("--max-steps", type=int, default=25, help="step limit (per round of human feedback)")
    parser.add_argument("--k", type=int, default=5, help="chunks retrieved per search")
    parser.add_argument("--max-tokens", type=int, default=None, help="max_tokens per model call (default: not set)")
    parser.add_argument("--no-feedback", action="store_true", help="don't ask for human feedback after finish")
    parser.add_argument("--log", type=Path, help="log file (default: task2_own_agent/logs/run_XX.jsonl)")
    args = parser.parse_args()

    if not member_names_from_content(args.content):
        sys.exit(f"No member profiles in {args.content}. Create <firstname>.md files from _template.md first.")

    log_path = args.log or next_log_path(HERE / "logs")
    llm = LLMClient(model=args.model, embedding_model=args.embedding_model, log_path=log_path)
    rag = StyleGuideRAG(args.style_guide, embed_fn=llm.embed, model_name=llm.embedding_model or "embedding")
    agent = Agent(llm, rag, args.content, args.out, k=args.k, max_steps=args.max_steps, max_tokens=args.max_tokens)
    llm.log_event({"step": 0, "event": "run_start", "model": llm.model, "embedding_model": llm.embedding_model,
                   "k": args.k, "max_steps": args.max_steps, "n_chunks": len(rag.chunks),
                   "members": agent.member_names, "out": str(args.out)})
    print(f"Model: {llm.model} | {len(rag.chunks)} style guide chunks | members: {', '.join(agent.member_names)}")
    print(f"Log: {log_path}")

    while True:
        try:
            outcome = agent.run_until_finish()
        except KeyboardInterrupt:
            outcome = "interrupted"
        except Exception as exc:      # gateway problems: say what happened instead of a bare traceback
            llm.log_event({"step": agent.step, "event": "error", "error": f"{type(exc).__name__}: {exc}"})
            print(f"\nStopped: the model call failed ({type(exc).__name__}: {exc}).")
            print("Check the VPN / your key / your budget (see README). The log has all steps so far.")
            sys.exit(1)

        if outcome != "finished":
            reason = (f"the step limit of {args.max_steps} was reached without an accepted finish"
                      if outcome == "max_steps" else "interrupted by the user")
            print(f"\nStopped: {reason}.")
            if agent.last_report is not None:
                print(f"Last checker report of {args.out}:\n{agent.last_report.summary(only_failures=True)}")
            break

        print(f"\nThe agent finished. Site: {args.out}\n{agent.last_report.summary(only_failures=True)}")
        if args.no_feedback:
            break
        try:
            feedback = input("\nOpen the page in a browser. Feedback for the agent (empty = accept and end): ").strip()
        except EOFError:
            feedback = ""
        if not feedback:
            break
        agent.add_feedback(feedback)

    llm.log_event({"step": agent.step, "event": "run_end", "outcome": outcome, "steps": agent.step,
                   "checker_passed": bool(agent.last_report and agent.last_report.passed),
                   "usage": llm.usage_summary()})
    print(f"Steps: {agent.step} | usage: {llm.usage_summary()}")
    sys.exit(0 if outcome == "finished" else 1)


if __name__ == "__main__":
    main()
