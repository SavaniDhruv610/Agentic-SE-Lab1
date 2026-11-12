"""A small ReAct-style coding agent that builds the team website.

Protocol (one JSON object per model step):

    {"thought": "...", "action": "search_style_guide", "args": {"query": "..."}}
    {"thought": "...", "action": "read_profiles", "args": {}}
    {"thought": "...", "action": "write_site", "args": {}}
    ```html
    <!doctype html>...
    ```
    {"thought": "...", "action": "finish", "args": {"summary": "..."}}

Run from the repository root:

    uv run python task2_own_agent/agent.py \
        --content task1_opencode/content \
        --out task2_own_agent/site/index.html
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from common.embed_images import PLACEHOLDER, image_to_data_uri  # noqa: E402
from common.llm_client import LLMClient  # noqa: E402
from common.site_checker import CheckReport, check_site, member_names_from_content  # noqa: E402

try:  # direct script import
    from rag import StyleGuideRAG, format_search_results
except ImportError:  # imported as task2_own_agent.agent
    from .rag import StyleGuideRAG, format_search_results  # type: ignore


ACTIONS = {"search_style_guide", "read_profiles", "write_site", "finish"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}

SYSTEM_PROMPT = """\
You are an autonomous coding agent that builds a single-file team website for the
Agentic Software Engineering lab at Paderborn University.

In every step you answer with exactly one JSON object:

{"thought": "short reasoning", "action": "<action>", "args": {...}}

Available actions:
- search_style_guide, args {"query": "<text>"}
  Returns the most relevant chunks of the style guide together with their source
  document ids. Only documents whose status is ACTIVE are authoritative; documents
  marked DEPRECATED are historical. The checker does not quote the rules, so search
  for the relevant topics yourself.
- read_profiles, args {}
  Returns all member profiles and the list of image files that actually exist.
- write_site, args {}
  Put the complete HTML page in a fenced ```html code block immediately after the
  JSON object (or use {"html": "<page>"} as the argument). The host saves the page,
  embeds images and runs the full checker. Use placeholders such as
  <img src="{{image:Photos/Name.png}}" alt="Photo of Name">; never write base64
  image data yourself.
- finish, args {"summary": "<short summary>"}
  Only accepted when the most recent checker report passed.

Working rules:
- Use the profile files as the source for names and personal content.
- Search the style guide before the first write and again whenever the checker
  reports failures. Fix every failing check.
- Keep the group name consistent between the page title and the header.
- Output only one action per step. Do not use native tool calling.
"""


def _short(text: str, limit: int = 300) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def parse_model_output(raw: str) -> tuple[dict | None, str, str | None]:
    """Extract the first valid JSON action object and return (object, remainder, error)."""
    if not raw or not raw.strip():
        return None, "", "The response was empty."

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw):
        try:
            obj, end = decoder.raw_decode(raw[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            return obj, raw[match.start() + end :], None

    # Give a useful error if there was JSON but no action key, or no JSON at all.
    start = raw.find("{")
    if start == -1:
        return None, "", "No JSON object found in the response."
    try:
        obj, end = decoder.raw_decode(raw[start:])
    except json.JSONDecodeError as exc:
        return None, "", f"Invalid JSON: {exc.msg} at character {exc.pos}."
    if not isinstance(obj, dict):
        return None, "", "The JSON value is not an object."
    return None, raw[start + end :], "The JSON object has no 'action' field."


def extract_html(raw: str, remainder: str, args: dict) -> str | None:
    """Find HTML in the JSON argument or in a fenced code block after the JSON."""
    arg_html = args.get("html")
    if isinstance(arg_html, str) and arg_html.strip():
        return arg_html

    blocks = [
        (match.group(1).strip().lower(), match.group(2))
        for match in re.finditer(r"```([^\n`]*)\n(.*?)```", raw, re.DOTALL)
    ]

    # Prefer an explicitly tagged ```html block.
    for language, content in blocks:
        if language == "html" and content.strip():
            return content

    # Otherwise accept a block whose content looks like a complete HTML document.
    for language, content in reversed(blocks):
        if language not in {"json", "javascript", "js"} and re.search(
            r"<(?:!doctype|html|head|body)\b", content, re.IGNORECASE
        ):
            return content

    # Last resort: an untagged block after the JSON object.
    for match in re.finditer(r"```\s*\n(.*?)```", remainder, re.DOTALL):
        if match.group(1).strip():
            return match.group(1)
    return None


class TeamSiteAgent:
    def __init__(
        self,
        llm: LLMClient,
        rag: StyleGuideRAG,
        content_dir: Path,
        out_path: Path,
        *,
        group_name: str | None = None,
        max_steps: int = 25,
        k: int = 4,
        max_tokens: int | None = None,
        interactive_feedback: bool = True,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        self.llm = llm
        self.rag = rag
        self.content_dir = Path(content_dir)
        self.out_path = Path(out_path)
        self.group_name = group_name
        self.max_steps = max_steps
        self.k = k
        self.max_tokens = max_tokens
        self.interactive_feedback = interactive_feedback
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.last_report: CheckReport | None = None
        self.member_names = member_names_from_content(self.content_dir)
        self._image_cache: list[Path] | None = None

    # ------------------------------------------------------------------- run

    def run(self) -> int:
        self.output_fn("Building style-guide index ...")
        self.rag.build()
        self.output_fn(
            f"Indexed {len(self.rag.chunks)} chunks from {len(self.rag.documents)} documents "
            f"({self.rag.active_backend})."
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._task_prompt()},
        ]
        self.llm.log_event(
            {
                "step": 0,
                "event": "task",
                "content": self._task_prompt(),
                "content_dir": str(self.content_dir),
                "out_path": str(self.out_path),
                "group_name": self.group_name,
            }
        )

        for step in range(1, self.max_steps + 1):
            self.output_fn(f"\n=== Step {step}/{self.max_steps} ===")
            self.llm.log_event({"step": step, "event": "request", "messages": messages})
            try:
                raw = self.llm.chat(messages, max_tokens=self.max_tokens)
            except Exception as exc:  # network / API errors must not leave a broken log
                msg = f"Model request failed: {type(exc).__name__}: {exc}"
                self.output_fn(msg)
                self.llm.log_event({"step": step, "event": "error", "content": msg})
                return 1

            self.llm.log_event({"step": step, "event": "response", "content": raw})
            messages.append({"role": "assistant", "content": raw})

            parsed, remainder, error = parse_model_output(raw)
            if error is not None or parsed is None:
                observation = (
                    f"ERROR: {error} Reply with exactly one JSON object like "
                    '{"thought": "...", "action": "read_profiles", "args": {}}'
                )
                self._send_observation(messages, step, observation, action=None)
                continue

            action = parsed.get("action")
            args = parsed.get("args", {})
            if not isinstance(action, str) or not isinstance(args, dict):
                observation = (
                    "ERROR: 'action' must be a string and 'args' must be a JSON object."
                )
                self._send_observation(messages, step, observation, action=None)
                continue

            self.output_fn(f"Action: {action}  {_short(json.dumps(args))}")
            observation, stop = self._execute(step, action, args, raw, remainder)
            self.llm.log_event(
                {
                    "step": step,
                    "event": "observation",
                    "action": action,
                    "content": observation,
                }
            )
            self.output_fn(f"Observation: {_short(observation, 500)}")
            if stop:
                self.output_fn(f"\nStopped after {step} step(s). {self.llm.usage_summary()}")
                return 0
            messages.append({"role": "user", "content": f"Observation (step {step}):\n{observation}"})

        self.output_fn(
            f"\nMaximum number of steps ({self.max_steps}) reached without an accepted finish. "
            f"{self.llm.usage_summary()}"
        )
        self.llm.log_event(
            {
                "step": self.max_steps,
                "event": "max_steps_reached",
                "content": f"Stopped after {self.max_steps} steps.",
            }
        )
        return 1

    def _task_prompt(self) -> str:
        group = (
            f'The team/group name is "{self.group_name}". Use it consistently in the page title '
            "and in the header."
            if self.group_name
            else "Choose a short group name and use it consistently in the page title and the header."
        )
        return f"""\
Build the team website from the member profiles in: {self.content_dir}
Save the final page to: {self.out_path}

{group}

Start with read_profiles. Then use search_style_guide for every aspect you need:
page metadata and title, page structure and required sections, day and night palettes,
theme switching/persistence/toggle, member cards/photos/hot takes, typography and fonts,
responsive breakpoints, footer, and accessibility. Follow the ACTIVE documents.

Then write the complete single-file HTML page with write_site. Use {{image:<path>}}
placeholders for every member photo, never base64. If the checker reports failures,
search for the relevant ACTIVE rules, fix the page and call write_site again. When the
checker passes, call finish.
"""

    def _send_observation(
        self, messages: list[dict[str, str]], step: int, observation: str, action: str | None
    ) -> None:
        self.llm.log_event(
            {"step": step, "event": "observation", "action": action, "content": observation}
        )
        self.output_fn(f"Observation: {_short(observation, 500)}")
        messages.append({"role": "user", "content": f"Observation (step {step}):\n{observation}"})

    # --------------------------------------------------------------- actions

    def _execute(
        self, step: int, action: str, args: dict, raw: str, remainder: str
    ) -> tuple[str, bool]:
        if action not in ACTIONS:
            return (
                f"ERROR: unknown action '{action}'. Available actions: {', '.join(sorted(ACTIONS))}.",
                False,
            )

        if action == "search_style_guide":
            return self._search_style_guide(args)
        if action == "read_profiles":
            return self._read_profiles(args)
        if action == "write_site":
            return self._write_site(args, raw, remainder)
        if action == "finish":
            return self._finish(step, args)
        return ("ERROR: unreachable action handler.", False)

    def _search_style_guide(self, args: dict) -> tuple[str, bool]:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return ("ERROR: search_style_guide needs a non-empty string argument 'query'.", False)
        k = args.get("k", self.k)
        if not isinstance(k, int) or k < 1:
            return ("ERROR: 'k' must be a positive integer.", False)
        chunks = self.rag.search(query.strip(), k=k)
        observation = format_search_results(chunks, max_tokens=1000)
        return (observation, False)

    def _read_profiles(self, args: dict) -> tuple[str, bool]:
        if not self.content_dir.is_dir():
            return (f"ERROR: content directory not found: {self.content_dir}", False)

        sections: list[str] = []
        for path in sorted(self.content_dir.glob("*.md")):
            if path.name.startswith("_"):
                continue
            sections.append(f"### {path.name}\n{path.read_text(encoding='utf-8').strip()}")

        images = self._available_images()
        image_lines = [
            f"- {{image:{self._relative_image(p)}}}" for p in images
        ] or ["- (no image files found)"]

        members = ", ".join(self.member_names) if self.member_names else "(none found)"
        observation = (
            f"MEMBERS: {members}\n\n"
            "MEMBER PROFILES\n\n"
            + "\n\n---\n\n".join(sections)
            + "\n\nAVAILABLE IMAGE FILES\n"
            "Use exactly these paths in placeholders. The image links inside the Markdown "
            "profiles may be stale, so prefer this list.\n"
            + "\n".join(image_lines)
        )
        return (observation, False)

    def _write_site(self, args: dict, raw: str, remainder: str) -> tuple[str, bool]:
        # Any write attempt invalidates an earlier passing report: finish must only
        # succeed on the report produced by the most recent write_site call.
        self.last_report = None
        html = extract_html(raw, remainder, args)
        if not html or not html.strip():
            return (
                "ERROR: write_site needs the HTML in a fenced ```html code block after the JSON "
                "object, or in the 'html' argument.",
                False,
            )

        if re.search(r"data:image/|base64,", html, re.IGNORECASE):
            return (
                "ERROR: do not write base64 or data:image URIs yourself. Use placeholders like "
                '<img src="{{image:Photos/Name.png}}" alt="Photo of Name"> instead.',
                False,
            )

        try:
            embedded, count = self._embed_placeholders(html)
        except (FileNotFoundError, ValueError) as exc:
            return (f"ERROR: could not embed images: {exc}", False)

        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.write_text(embedded, encoding="utf-8")
        report = check_site(
            embedded,
            mode="full",
            member_names=self.member_names or None,
            file_size=len(embedded.encode("utf-8")),
        )
        self.last_report = report

        observation = (
            f"Saved {self.out_path} ({len(embedded.encode('utf-8')) / 1024:.0f} KB); "
            f"embedded {count} image placeholder(s).\n\n"
            f"Checker report:\n{report.summary()}"
        )
        if report.passed:
            observation += "\n\nThe checker passed. You may call finish."
        else:
            observation += (
                "\n\nFix the failing checks above. Search the style guide for the relevant "
                "ACTIVE rules and call write_site again with the complete corrected page."
            )
        return (observation, False)

    def _finish(self, step: int, args: dict) -> tuple[str, bool]:
        summary = args.get("summary", "")
        if not isinstance(summary, str):
            summary = str(summary)

        if self.last_report is None:
            return (
                "FINISH REJECTED: no site has been written since the last feedback. "
                "Call write_site first.",
                False,
            )
        if not self.last_report.passed:
            failures = "\n".join(
                f"[FAIL] {r.name}: {r.message}" for r in self.last_report.failures()
            )
            return (
                "FINISH REJECTED: the last checker report did not pass. Failing checks:\n"
                + failures
                + "\nFix them and call write_site again.",
                False,
            )

        self.output_fn(f"\nSite accepted. Summary: {summary or '(none)'}")
        if not self.interactive_feedback:
            return ("FINISH ACCEPTED. The run is complete.", True)

        try:
            self.output_fn("Enter feedback for another iteration, or press Enter to end the run:")
            feedback = self.input_fn("> ").strip()
        except (EOFError, KeyboardInterrupt):
            feedback = ""

        if not feedback:
            return ("FINISH ACCEPTED. The run is complete.", True)

        self.last_report = None
        self.llm.log_event({"step": step, "event": "human_feedback", "content": feedback})
        return (
            "HUMAN FEEDBACK (the site was accepted, but the user asks for changes):\n"
            f"{feedback}\n\n"
            "Incorporate this feedback. You must call write_site again and get a passing "
            "checker report before calling finish.",
            False,
        )

    # --------------------------------------------------------------- images

    def _available_images(self) -> list[Path]:
        if self._image_cache is None:
            self._image_cache = sorted(
                (
                    p
                    for p in self.content_dir.rglob("*")
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
                ),
                key=lambda p: self._relative_image(p).lower(),
            )
        return self._image_cache

    def _relative_image(self, path: Path) -> str:
        try:
            return path.relative_to(self.content_dir).as_posix()
        except ValueError:
            return path.name

    def _resolve_image(self, name: str) -> Path:
        content = self.content_dir.resolve()
        raw_name = name.strip().replace("\\", "/")

        direct = (content / raw_name).resolve()
        if content == direct or content not in direct.parents:
            raise ValueError(f"Image path escapes the content directory: {name}")
        if direct.is_file():
            return direct

        images = self._available_images()
        lowered = raw_name.lower()
        stem = Path(raw_name).stem.lower()

        for predicate in (
            lambda p: self._relative_image(p).lower() == lowered,
            lambda p: p.name.lower() == lowered,
            lambda p: p.stem.lower() == stem,
        ):
            matches = [p for p in images if predicate(p)]
            if matches:
                return matches[0].resolve()

        # Small typo tolerance: e.g. a profile says yashik.jpg but the file is Yasik.png.
        if images:
            best = max(images, key=lambda p: SequenceMatcher(None, stem, p.stem.lower()).ratio())
            if SequenceMatcher(None, stem, best.stem.lower()).ratio() >= 0.75:
                return best.resolve()

        available = ", ".join(self._relative_image(p) for p in images) or "none"
        raise FileNotFoundError(f"Image '{name}' not found. Available image files: {available}")

    def _embed_placeholders(self, html: str) -> tuple[str, int]:
        matches = list(PLACEHOLDER.finditer(html))
        if not matches:
            return html, 0

        def replace(match: re.Match) -> str:
            path = self._resolve_image(match.group(1))
            return image_to_data_uri(path)

        return PLACEHOLDER.sub(replace, html), len(matches)


def _next_log_path(logs_dir: Path) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    used = set()
    for path in logs_dir.glob("run_*.jsonl"):
        match = re.fullmatch(r"run_(\d+)\.jsonl", path.name)
        if match:
            used.add(int(match.group(1)))
    n = 1
    while n in used:
        n += 1
    return logs_dir / f"run_{n:02d}.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the team website with a small RAG agent.")
    parser.add_argument(
        "--content",
        type=Path,
        default=Path("task1_opencode/content"),
        help="directory with <firstname>.md profile files (default: task1_opencode/content)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("task2_own_agent/site/index.html"),
        help="output HTML file (default: task2_own_agent/site/index.html)",
    )
    parser.add_argument(
        "--style-guide",
        type=Path,
        default=SCRIPT_DIR / "style_guide",
        help="style-guide directory (default: task2_own_agent/style_guide)",
    )
    parser.add_argument("--group", default=None, help="team/group name to use on the page")
    parser.add_argument("--model", default=None, help="chat model (default: LAB_CHAT_MODEL)")
    parser.add_argument("--max-steps", type=int, default=25, help="maximum model steps (default: 25)")
    parser.add_argument("--k", type=int, default=4, help="style-guide chunks per search (default: 4)")
    parser.add_argument(
        "--rag-backend",
        choices=["auto", "embeddings", "bm25"],
        default="auto",
        help="retrieval backend; auto uses embeddings and falls back to BM25 (default: auto)",
    )
    parser.add_argument("--max-tokens", type=int, default=None, help="max output tokens per model call")
    parser.add_argument("--log", type=Path, default=None, help="JSONL log path (default: logs/run_XX.jsonl)")
    parser.add_argument(
        "--no-feedback",
        action="store_true",
        help="end immediately after an accepted finish instead of asking for terminal feedback",
    )
    args = parser.parse_args(argv)

    log_path = args.log or _next_log_path(SCRIPT_DIR / "logs")
    llm = LLMClient(model=args.model, log_path=log_path)
    rag = StyleGuideRAG(args.style_guide, llm, backend=args.rag_backend)
    agent = TeamSiteAgent(
        llm,
        rag,
        args.content,
        args.out,
        group_name=args.group,
        max_steps=args.max_steps,
        k=args.k,
        max_tokens=args.max_tokens,
        interactive_feedback=not args.no_feedback,
    )

    print(f"Chat model: {llm.model}")
    print(f"Embedding model: {llm.embedding_model}")
    print(f"Log: {llm.log_path}")
    start = time.time()
    try:
        return agent.run()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130
    finally:
        print(f"Elapsed: {time.time() - start:.1f}s")
        print(f"Usage: {llm.usage_summary()}")


if __name__ == "__main__":
    raise SystemExit(main())
