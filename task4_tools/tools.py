"""Sandboxed tool layer for Task 4.

The registry exposes a small set of file and process tools to an LLM agent. Everything is
contained to ``workspace``: reads cannot escape it, writes are limited to ``src/``, and
subprocesses run with a minimal environment that never contains the gateway API key.

``execute()`` is the only public entry point and never raises. Bad JSON, unknown tools,
wrong argument types and containment violations all become ``ToolResult(ok=False, ...)``.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from difflib import unified_diff
from pathlib import Path
from typing import Any, Iterable

import jsonschema

try:  # ``from tools import ...`` (tests) and ``from task4_tools.tools import ...`` both work
    # Prefer the top-level module so this is the *same* ToolResult class the tests import.
    from tool_contract import (
        DEFAULT_TIMEOUT_S,
        MAX_LIST_ENTRIES,
        MAX_OUTPUT_CHARS,
        MAX_READ_LINES,
        MAX_SEARCH_RESULTS,
        ToolRegistryBase,
        ToolResult,
    )
except ImportError:  # pragma: no cover - depends on how the module is imported
    try:
        from .tool_contract import (  # type: ignore[no-redef]
            DEFAULT_TIMEOUT_S,
            MAX_LIST_ENTRIES,
            MAX_OUTPUT_CHARS,
            MAX_READ_LINES,
            MAX_SEARCH_RESULTS,
            ToolRegistryBase,
            ToolResult,
        )
    except ImportError:
        from task4_tools.tool_contract import (  # type: ignore[no-redef]
            DEFAULT_TIMEOUT_S,
            MAX_LIST_ENTRIES,
            MAX_OUTPUT_CHARS,
            MAX_READ_LINES,
            MAX_SEARCH_RESULTS,
            ToolRegistryBase,
            ToolResult,
        )


class ToolError(Exception):
    """A user/model-facing error. Converted into ToolResult(ok=False) by ``execute``."""


_SHELL_META = set(";&|`$><\n\r")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _looks_absolute(raw: str) -> bool:
    return bool(_DRIVE_RE.match(raw)) or raw.startswith(("/", "\\", "//", "\\\\"))


def _shorten(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class ToolRegistry(ToolRegistryBase):
    def __init__(self, workspace: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        super().__init__(workspace=workspace, timeout_s=timeout_s)
        self.workspace = Path(workspace).resolve()
        self._root = self.workspace
        self._src = (self._root / "src").resolve()

    # ------------------------------------------------------------------ schemas

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": (
                        "List files in the workspace. Paths are relative to the workspace root. "
                        "Optionally filter with a glob pattern such as '**/*.py'."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Directory or file path relative to the workspace. Defaults to '.'.",
                                "minLength": 1,
                            },
                            "pattern": {
                                "type": "string",
                                "description": "Optional glob pattern used to filter relative file paths.",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": (
                        "Read a UTF-8 text file inside the workspace and return numbered lines. "
                        "Optionally select an inclusive 1-based line range."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "File path relative to the workspace root.",
                                "minLength": 1,
                            },
                            "start_line": {
                                "type": "integer",
                                "minimum": 1,
                                "description": "First line to return (1-based, inclusive).",
                            },
                            "end_line": {
                                "type": "integer",
                                "minimum": 1,
                                "description": "Last line to return (1-based, inclusive).",
                            },
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_code",
                    "description": (
                        "Search files inside the workspace with a regular expression. "
                        "Returns 'path:line: text' matches, optionally restricted by a glob."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "Python regular expression to search for.",
                            },
                            "glob": {
                                "type": "string",
                                "description": "Optional glob (for example '**/*.py') limiting which files are searched.",
                            },
                            "max_results": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": MAX_SEARCH_RESULTS,
                                "description": f"Maximum number of matches to return (at most {MAX_SEARCH_RESULTS}).",
                            },
                        },
                        "required": ["pattern"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_tests",
                    "description": (
                        "Run the workspace's pytest suite (or a single test file/node) and return a "
                        "structured summary with passed/failed/error/skipped counts and short tracebacks."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "target": {
                                "type": "string",
                                "description": (
                                    "Optional pytest target inside the workspace, e.g. 'tests/test_x.py' "
                                    "or 'tests/test_x.py::test_name'."
                                ),
                            }
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_cli",
                    "description": (
                        "Run 'python -m tinyledger <args>' in the workspace and return exit code, stdout and stderr."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "args": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Command line arguments passed to tinyledger, e.g. ['report', 'data/sample.csv'].",
                            }
                        },
                        "required": ["args"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": (
                        "Replace a unique string in an existing file below src/. Returns a unified diff. "
                        "Tests, data, configuration and ISSUE.md can never be edited."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Existing file path below src/, relative to the workspace root.",
                                "minLength": 1,
                            },
                            "old_str": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Exact text that must occur exactly once in the file.",
                            },
                            "new_str": {
                                "type": "string",
                                "description": "Replacement text.",
                            },
                        },
                        "required": ["path", "old_str", "new_str"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    # ------------------------------------------------------------------ execute

    def execute(self, name: str, arguments: str) -> ToolResult:
        """Validate and run one tool call. Never raises."""
        try:
            result = self._execute(name, arguments)
        except Exception as exc:  # defensive: no tool bug may crash the agent loop
            result = ToolResult(ok=False, error=f"internal error: {type(exc).__name__}: {exc}")
        return self._bound(result)

    def _execute(self, name: str, arguments: str) -> ToolResult:
        if not isinstance(name, str) or not name:
            return ToolResult(ok=False, error="tool name must be a non-empty string")
        if not isinstance(arguments, str):
            return ToolResult(ok=False, error="arguments must be a raw JSON string")

        schemas = {s["function"]["name"]: s["function"]["parameters"] for s in self.schemas()}
        if name not in schemas:
            return ToolResult(ok=False, error=f"unknown tool: {name!r}")

        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            return ToolResult(ok=False, error=f"invalid JSON arguments: {exc.msg} at line {exc.lineno} column {exc.colno}")
        if not isinstance(parsed, dict):
            return ToolResult(ok=False, error="arguments must be a JSON object")

        # jsonschema treats booleans as booleans, but be explicit about integer fields.
        for key in ("start_line", "end_line", "max_results"):
            if key in parsed and isinstance(parsed[key], bool):
                return ToolResult(ok=False, error=f"{key} must be an integer, not a boolean")

        try:
            jsonschema.validate(instance=parsed, schema=schemas[name])
        except jsonschema.ValidationError as exc:
            path = ".".join(str(p) for p in exc.absolute_path) or "arguments"
            return ToolResult(ok=False, error=f"invalid arguments for {name}: {path}: {exc.message}")
        except Exception as exc:  # malformed schema is a programming error, still stay alive
            return ToolResult(ok=False, error=f"schema validation failed: {exc}")

        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolResult(ok=False, error=f"tool {name!r} is not implemented")
        return handler(parsed)

    # ------------------------------------------------------------- containment

    def _normalise(self, raw: Any) -> str:
        if not isinstance(raw, str):
            raise ToolError("path must be a string")
        if "\x00" in raw:
            raise ToolError("path contains a NUL byte")
        if not raw.strip():
            raise ToolError("path must not be empty")
        # Treat Windows separators as separators on every platform so that
        # '..\\secrets\\canary.txt' cannot sneak past a POSIX-only check.
        text = raw.replace("\\", "/")
        if _looks_absolute(text):
            raise ToolError("absolute paths are not allowed")
        return text

    def _safe_path(self, raw: Any, *, must_exist: bool = False) -> Path:
        text = self._normalise(raw)
        try:
            target = (self._root / text).resolve()
        except (OSError, RuntimeError) as exc:
            raise ToolError(f"could not resolve path: {exc}") from exc
        if not target.is_relative_to(self._root):
            raise ToolError("path escapes the workspace")
        if must_exist and not target.exists():
            raise ToolError(f"path does not exist: {raw}")
        return target

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self._root).as_posix()
        except (ValueError, OSError, RuntimeError):
            return path.name

    def _iter_workspace_files(self, base: Path) -> Iterable[Path]:
        if base.is_file():
            if base.is_symlink():
                try:
                    if not base.resolve().is_relative_to(self._root):
                        return
                except (OSError, RuntimeError):
                    return
            yield base
            return
        if not base.is_dir():
            return

        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            kept: list[str] = []
            for dirname in dirnames:
                if dirname in _SKIP_DIRS:
                    continue
                candidate = Path(dirpath) / dirname
                if candidate.is_symlink():
                    # Never traverse symlinked directories, even if they point inside.
                    continue
                kept.append(dirname)
            dirnames[:] = kept

            for filename in filenames:
                candidate = Path(dirpath) / filename
                if candidate.is_symlink():
                    try:
                        if not candidate.resolve().is_relative_to(self._root):
                            continue
                    except (OSError, RuntimeError):
                        continue
                yield candidate

    @staticmethod
    def _glob_match(rel: str, pattern: str | None) -> bool:
        if not pattern:
            return True
        name = rel.rsplit("/", 1)[-1]
        return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern) or Path(rel).match(pattern)

    # ---------------------------------------------------------------- tools

    def _tool_list_files(self, args: dict) -> ToolResult:
        raw_path = args.get("path", ".")
        pattern = args.get("pattern")
        try:
            base = self._safe_path(raw_path, must_exist=True)
        except ToolError as exc:
            return ToolResult(ok=False, error=str(exc))

        entries: list[str] = []
        truncated = False
        for file_path in self._iter_workspace_files(base):
            rel = self._relative(file_path)
            if not self._glob_match(rel, pattern):
                continue
            if len(entries) < MAX_LIST_ENTRIES:
                entries.append(rel)
            else:
                truncated = True
                break
        entries.sort()
        return ToolResult(ok=True, output=entries, truncated=truncated)

    def _tool_read_file(self, args: dict) -> ToolResult:
        try:
            target = self._safe_path(args["path"], must_exist=True)
        except ToolError as exc:
            return ToolResult(ok=False, error=str(exc))
        if not target.is_file():
            return ToolResult(ok=False, error="path is not a regular file")
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(ok=False, error=f"could not read file: {exc}")

        lines = content.splitlines()
        start_line = int(args.get("start_line", 1))
        end_line = args.get("end_line")
        start_idx = max(0, start_line - 1)

        if end_line is None:
            selected = lines[start_idx:]
            truncated = len(selected) > MAX_READ_LINES
            selected = selected[:MAX_READ_LINES]
        else:
            end_idx = min(int(end_line), len(lines))
            selected = lines[start_idx:end_idx] if end_idx >= start_idx else []
            truncated = len(selected) > MAX_READ_LINES
            selected = selected[:MAX_READ_LINES]

        rendered = "\n".join(f"{start_idx + i + 1}: {line}" for i, line in enumerate(selected))
        return ToolResult(ok=True, output=rendered, truncated=truncated)

    def _tool_search_code(self, args: dict) -> ToolResult:
        try:
            regex = re.compile(args["pattern"])
        except re.error as exc:
            return ToolResult(ok=False, error=f"invalid regular expression: {exc}")

        glob_pattern = args.get("glob")
        limit = int(args.get("max_results", MAX_SEARCH_RESULTS))
        limit = max(1, min(limit, MAX_SEARCH_RESULTS))

        matches: list[str] = []
        truncated = False
        for file_path in self._iter_workspace_files(self._root):
            rel = self._relative(file_path)
            if not self._glob_match(rel, glob_pattern):
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    if len(matches) < limit:
                        matches.append(f"{rel}:{line_number}: {line.rstrip()}")
                    else:
                        truncated = True
                        break
            if truncated:
                break
        return ToolResult(ok=True, output="\n".join(matches), truncated=truncated)

    # ------------------------------------------------------------ subprocesses

    def _minimal_env(self, *, pythonpath: bool = False) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TMP", "TEMP"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        env.setdefault("PATH", os.defpath)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if pythonpath:
            env["PYTHONPATH"] = str(self._src)
        return env

    def _run_process(self, argv: list[str], env: dict[str, str]) -> tuple[int | None, str, str, bool]:
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            argv,
            cwd=str(self._root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **kwargs,
        )
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout_s)
            return proc.returncode, stdout or "", stderr or "", False
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    proc.kill()
            else:  # pragma: no cover - Windows
                proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            return None, stdout or "", stderr or "", True

    def _validate_test_target(self, target: str | None) -> str | None:
        if target is None:
            return None
        if not isinstance(target, str):
            raise ToolError("target must be a string")
        if any(ch in target for ch in "\x00\n\r"):
            raise ToolError("target contains a forbidden control character")
        if target.lstrip().startswith("-"):
            raise ToolError("target must not start with '-' (flags are not allowed)")
        parts = target.split("::")
        if any(part.startswith("-") for part in parts[1:]):
            raise ToolError("test node ids must not start with '-'")
        try:
            self._safe_path(parts[0], must_exist=True)
        except ToolError as exc:
            raise ToolError(f"invalid test target: {exc}") from exc
        return target

    def _tool_run_tests(self, args: dict) -> ToolResult:
        try:
            target = self._validate_test_target(args.get("target"))
        except ToolError as exc:
            return ToolResult(ok=False, error=str(exc))

        with tempfile.TemporaryDirectory(prefix="task4_pytest_") as temp_dir:
            report = Path(temp_dir) / "report.xml"
            argv = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={report}",
            ]
            if target:
                argv.append(target)

            try:
                code, stdout, stderr, timed_out = self._run_process(argv, self._minimal_env())
            except OSError as exc:
                return ToolResult(ok=False, error=f"could not start pytest: {exc}")

            if timed_out:
                return ToolResult(ok=False, error=f"pytest timed out after {self.timeout_s:g} seconds")

            if not report.exists():
                detail, cut = _shorten((stderr or stdout).strip(), 2_000)
                return ToolResult(
                    ok=False,
                    error=f"pytest did not produce a report (exit code {code}): {detail}",
                    truncated=cut,
                )

            try:
                summary = self._parse_junit(report)
            except (ET.ParseError, OSError) as exc:
                return ToolResult(ok=False, error=f"could not parse pytest report: {exc}")

            summary["exit_code"] = code
            return ToolResult(ok=True, output=summary)

    @staticmethod
    def _parse_junit(report: Path) -> dict[str, Any]:
        root = ET.parse(report).getroot()
        passed = failed = errors = skipped = 0
        failed_tests: list[dict[str, str]] = []
        for case in root.iter("testcase"):
            name = case.get("name") or "?"
            classname = case.get("classname") or ""
            full_name = f"{classname}::{name}" if classname else name
            failure = case.find("failure")
            error = case.find("error")
            skipped_el = case.find("skipped")
            if failure is not None:
                failed += 1
                failed_tests.append({"name": full_name, "status": "failed", "traceback": _last_text(failure)})
            elif error is not None:
                errors += 1
                failed_tests.append({"name": full_name, "status": "error", "traceback": _last_text(error)})
            elif skipped_el is not None:
                skipped += 1
            else:
                passed += 1

        # Keep the summary structured and bounded: names for the first failures, short
        # tracebacks only for the first few (the full counts above always cover everything).
        compact: list[dict[str, str]] = []
        for index, item in enumerate(failed_tests[:50]):
            entry = {"name": item["name"], "status": item["status"]}
            if index < 10:
                entry["traceback"] = item["traceback"][-400:]
            compact.append(entry)

        return {
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "total": passed + failed + errors + skipped,
            "failed_tests": compact,
        }

    def _validate_cli_arg(self, arg: Any) -> None:
        if not isinstance(arg, str):
            raise ToolError("every run_cli argument must be a string")
        if "\x00" in arg:
            raise ToolError("argument contains a NUL byte")
        if any(ch in _SHELL_META for ch in arg):
            raise ToolError(f"argument contains a shell metacharacter: {arg!r}")
        if arg.startswith("~"):
            raise ToolError("home-directory expansion is not allowed")

        candidates = [arg]
        if arg.startswith("--") and "=" in arg:
            candidates.append(arg.split("=", 1)[1])

        for candidate in candidates:
            text = candidate.replace("\\", "/")
            path_like = (
                "/" in text
                or text in {".", ".."}
                or text.startswith(("./", "../"))
                or _looks_absolute(text)
            )
            if not path_like:
                continue
            if _looks_absolute(text):
                raise ToolError(f"absolute paths are not allowed: {candidate!r}")
            try:
                resolved = (self._root / text).resolve()
            except (OSError, RuntimeError) as exc:
                raise ToolError(f"could not resolve argument {candidate!r}: {exc}") from exc
            if not resolved.is_relative_to(self._root):
                raise ToolError(f"argument escapes the workspace: {candidate!r}")

    def _tool_run_cli(self, args: dict) -> ToolResult:
        cli_args = args["args"]
        if not isinstance(cli_args, list) or not all(isinstance(a, str) for a in cli_args):
            return ToolResult(ok=False, error="args must be a list of strings")
        try:
            for arg in cli_args:
                self._validate_cli_arg(arg)
        except ToolError as exc:
            return ToolResult(ok=False, error=str(exc))

        argv = [sys.executable, "-m", "tinyledger", *cli_args]
        try:
            code, stdout, stderr, timed_out = self._run_process(argv, self._minimal_env(pythonpath=True))
        except OSError as exc:
            return ToolResult(ok=False, error=f"could not start tinyledger: {exc}")

        if timed_out:
            return ToolResult(ok=False, error=f"tinyledger timed out after {self.timeout_s:g} seconds")

        out, out_cut = _shorten(stdout, MAX_OUTPUT_CHARS // 2)
        err, err_cut = _shorten(stderr, MAX_OUTPUT_CHARS // 2)
        return ToolResult(
            ok=True,
            output={"exit_code": code, "stdout": out, "stderr": err},
            truncated=out_cut or err_cut,
        )

    # ---------------------------------------------------------------- edits

    def _tool_edit_file(self, args: dict) -> ToolResult:
        try:
            target = self._safe_path(args["path"], must_exist=True)
        except ToolError as exc:
            return ToolResult(ok=False, error=str(exc))

        if target == self._src or not target.is_relative_to(self._src):
            return ToolResult(ok=False, error="edits are only allowed below src/")
        if not target.is_file():
            return ToolResult(ok=False, error="target must be an existing regular file")
        if target.is_symlink():
            return ToolResult(ok=False, error="refusing to edit a symlink")

        old_str = args["old_str"]
        new_str = args["new_str"]
        if not old_str:
            return ToolResult(ok=False, error="old_str must not be empty")

        try:
            original = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return ToolResult(ok=False, error=f"could not read file: {exc}")

        count = original.count(old_str)
        if count == 0:
            return ToolResult(ok=False, error="old_str was not found in the file")
        if count != 1:
            return ToolResult(ok=False, error=f"old_str must match exactly once, but it matched {count} times")

        updated = original.replace(old_str, new_str, 1)
        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, error=f"could not write file: {exc}")

        rel = self._relative(target)
        diff = "\n".join(
            unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile=rel,
                tofile=rel,
                lineterm="",
            )
        )
        return ToolResult(ok=True, output=diff)

    # ------------------------------------------------------------- bounding

    def _bound(self, result: ToolResult) -> ToolResult:
        if len(result.to_content()) <= MAX_OUTPUT_CHARS:
            return result
        if result.ok and isinstance(result.output, str):
            return self._fit(result.output, ok=True)
        if result.ok and isinstance(result.output, list):
            return self._fit_list(result.output)
        if not result.ok:
            return self._fit(result.error or "", ok=False)
        try:
            text = json.dumps(result.output, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(result.output)
        return self._fit(text, ok=True)

    @staticmethod
    def _fit_list(items: list[Any]) -> ToolResult:
        def build(length: int) -> ToolResult:
            return ToolResult(ok=True, output=items[:length], truncated=True)

        best = build(0)
        low, high = 0, len(items)
        while low <= high:
            mid = (low + high) // 2
            candidate = build(mid)
            if len(candidate.to_content()) <= MAX_OUTPUT_CHARS:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best

    @staticmethod
    def _fit(text: str, *, ok: bool) -> ToolResult:
        def build(length: int) -> ToolResult:
            if ok:
                return ToolResult(ok=True, output=text[:length], truncated=True)
            return ToolResult(ok=False, error=text[:length], truncated=True)

        best = build(0)
        low, high = 0, len(text)
        while low <= high:
            mid = (low + high) // 2
            candidate = build(mid)
            if len(candidate.to_content()) <= MAX_OUTPUT_CHARS:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best


def _last_text(element: ET.Element, limit: int = 1_500) -> str:
    text = (element.text or element.get("message") or "").strip()
    if len(text) > limit:
        return text[-limit:]
    return text
