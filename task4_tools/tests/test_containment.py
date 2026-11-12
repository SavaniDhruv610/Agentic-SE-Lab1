"""Adversarial tests for Task 4 containment and robustness.

These complement ``test_tool_contract.py`` without modifying it. They exercise path prefix
escapes, symlinks, write restrictions, timeouts, output bounds, environment stripping,
malformed arguments and process-argument injection.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tool_contract import MAX_LIST_ENTRIES, MAX_OUTPUT_CHARS, ToolResult


def call(registry, name: str, **kwargs) -> ToolResult:
    result = registry.execute(name, json.dumps(kwargs))
    assert isinstance(result, ToolResult)
    json.loads(result.to_content())
    return result


def text_of(result: ToolResult) -> str:
    return result.output if isinstance(result.output, str) else json.dumps(result.output)


def make_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks are not supported here: {exc}")


# ---------------------------------------------------------------- path escapes


def test_sibling_prefix_is_not_inside_workspace(registry, workspace, tmp_path):
    sibling = tmp_path / "workspace_sibling"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("SIBLING-PREFIX-SECRET")

    for name, kwargs in [
        ("read_file", {"path": "../workspace_sibling/secret.txt"}),
        ("list_files", {"path": "../workspace_sibling"}),
        ("edit_file", {"path": "../workspace_sibling/secret.txt", "old_str": "a", "new_str": "b"}),
    ]:
        result = call(registry, name, **kwargs)
        assert not result.ok, name
        assert "SIBLING-PREFIX-SECRET" not in result.to_content()


def test_absolute_and_windows_style_paths_are_rejected(registry, workspace, tmp_path):
    outside = tmp_path / "absolute.txt"
    outside.write_text("ABSOLUTE-SECRET")
    for raw in [str(outside), "/etc/passwd", "C:\\Windows\\win.ini", "..\\..\\secrets\\canary.txt"]:
        result = call(registry, "read_file", path=raw)
        assert not result.ok, raw
        assert "ABSOLUTE-SECRET" not in result.to_content()


def test_file_symlink_escape_is_rejected_and_not_walked(registry, workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("SYMLINK-FILE-SECRET")
    link = workspace / "src" / "leak.txt"
    make_symlink(link, outside)

    assert not call(registry, "read_file", path="src/leak.txt").ok

    listed = text_of(call(registry, "list_files", path="."))
    assert "src/leak.txt" not in listed

    searched = text_of(call(registry, "search_code", pattern="SYMLINK-FILE-SECRET"))
    assert "SYMLINK-FILE-SECRET" not in searched

    edit = call(registry, "edit_file", path="src/leak.txt", old_str="SYMLINK", new_str="PWNED")
    assert not edit.ok
    assert outside.read_text() == "SYMLINK-FILE-SECRET"


def test_directory_symlink_escape_is_rejected_and_not_walked(registry, workspace, tmp_path):
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "canary.txt").write_text("SYMLINK-DIR-SECRET")
    link = workspace / "linked"
    make_symlink(link, outside, directory=True)

    assert not call(registry, "read_file", path="linked/canary.txt").ok
    assert not call(registry, "list_files", path="linked").ok

    listed = text_of(call(registry, "list_files", path="."))
    assert "linked/canary.txt" not in listed

    searched = text_of(call(registry, "search_code", pattern="SYMLINK-DIR-SECRET"))
    assert "SYMLINK-DIR-SECRET" not in searched


def test_prefix_traversal_back_into_workspace_is_allowed_for_reads(registry):
    result = call(registry, "read_file", path="src/../src/tinyledger/ledger.py")
    assert result.ok
    assert "class Ledger" in text_of(result)


# ---------------------------------------------------------------- write containment


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_settle.py",
        "tests/../tests/test_settle.py",
        "data/sample.csv",
        "ISSUE.md",
        "README.md",
        "src/../tests/test_settle.py",
        "src/tinyledger/new_file.py",
    ],
)
def test_edit_is_limited_to_existing_files_below_src(registry, workspace, path):
    target = workspace / path
    before = target.read_text() if target.exists() else None

    result = call(registry, "edit_file", path=path, old_str="a", new_str="b")

    assert not result.ok, path
    if before is None:
        assert not target.exists()
    else:
        assert target.read_text() == before


def test_edit_requires_unique_and_nonempty_old_string(registry):
    duplicate = call(
        registry,
        "edit_file",
        path="src/tinyledger/ledger.py",
        old_str="expense",
        new_str="item",
    )
    assert not duplicate.ok

    empty = call(
        registry,
        "edit_file",
        path="src/tinyledger/ledger.py",
        old_str="",
        new_str="x",
    )
    assert not empty.ok


# ---------------------------------------------------------------- processes


def test_run_tests_timeout_is_reported_cleanly(workspace):
    from tools import ToolRegistry

    (workspace / "tests" / "test_hang.py").write_text(
        "import time\n\n\ndef test_hang():\n    time.sleep(30)\n",
        encoding="utf-8",
    )
    registry = ToolRegistry(workspace=workspace, timeout_s=1)
    result = call(registry, "run_tests", target="tests/test_hang.py")

    assert not result.ok
    assert "timed out" in (result.error or "").lower()


def test_subprocess_does_not_inherit_gateway_key(workspace, monkeypatch):
    from tools import ToolRegistry

    monkeypatch.setenv("UPB_AI_GATEWAY_KEY", "should-never-leak")
    (workspace / "tests" / "test_env.py").write_text(
        "import os\n\n\ndef test_no_gateway_key():\n"
        "    assert 'UPB_AI_GATEWAY_KEY' not in os.environ\n"
        "    assert 'should-never-leak' not in repr(os.environ)\n",
        encoding="utf-8",
    )
    registry = ToolRegistry(workspace=workspace, timeout_s=60)
    result = call(registry, "run_tests", target="tests/test_env.py")

    assert result.ok, result.error
    assert result.output["failed"] == 0
    assert result.output["errors"] == 0
    assert result.output["passed"] >= 1


@pytest.mark.parametrize("target", ["--help", "-k test_settle", "tests/test_settle.py::-k"])
def test_run_tests_rejects_flag_injection(registry, target):
    result = call(registry, "run_tests", target=target)
    assert not result.ok
    assert (result.error or "").strip()


@pytest.mark.parametrize(
    "args",
    [
        ["report; cat ../secrets/canary.txt"],
        ["report", "../secrets/canary.txt"],
        ["report", "data/../../secrets/canary.txt"],
        ["report", "/etc/passwd"],
        ["report", "C:\\Windows\\win.ini"],
        ["report", "data/sample.csv", "--month", "2026-03; cat secrets"],
    ],
)
def test_run_cli_rejects_path_and_shell_injection(registry, args):
    result = call(registry, "run_cli", args=args)
    assert not result.ok
    assert "CANARY" not in result.to_content()


def test_run_cli_valid_arguments_still_work(registry):
    result = call(registry, "run_cli", args=["report", "data/sample.csv", "--month", "2026-03"])
    assert result.ok, result.error
    assert result.output["exit_code"] == 0
    assert "Total" in result.output["stdout"]


# ---------------------------------------------------------------- bounds


def test_single_huge_line_is_bounded(registry, workspace):
    (workspace / "src" / "huge.txt").write_text("X" * 100_000, encoding="utf-8")
    result = call(registry, "read_file", path="src/huge.txt")

    assert result.ok
    assert result.truncated
    assert len(result.to_content()) <= MAX_OUTPUT_CHARS


def test_list_files_honours_entry_limit(registry, workspace):
    many = workspace / "src" / "many"
    many.mkdir()
    for index in range(MAX_LIST_ENTRIES + 10):
        (many / f"file_{index:04d}.txt").write_text("x", encoding="utf-8")

    result = call(registry, "list_files", path="src/many")

    assert result.ok
    assert result.truncated
    if isinstance(result.output, list):
        assert len(result.output) <= MAX_LIST_ENTRIES
    else:
        assert text_of(result).count("file_") <= MAX_LIST_ENTRIES


def test_read_file_honours_line_limit(registry, workspace):
    (workspace / "src" / "lines.txt").write_text(
        "\n".join(f"line {i}" for i in range(1_000)),
        encoding="utf-8",
    )
    result = call(registry, "read_file", path="src/lines.txt")

    assert result.ok
    assert result.truncated
    assert len(text_of(result).splitlines()) <= 200


# ---------------------------------------------------------------- malformed input


@pytest.mark.parametrize("arguments", ["not json", "[]", "null", '"a string"', "123", "true"])
def test_non_object_or_invalid_json_is_rejected(registry, arguments):
    result = registry.execute("read_file", arguments)
    assert not result.ok
    assert result.error
    json.loads(result.to_content())


def test_boolean_is_not_an_integer(registry):
    assert not call(registry, "read_file", path="README.md", start_line=True).ok
    assert not call(registry, "read_file", path="README.md", end_line=False).ok
    assert not call(registry, "search_code", pattern="x", max_results=True).ok


def test_wrong_types_and_extra_arguments_are_rejected(registry):
    assert not call(registry, "read_file", path="README.md", start_line="one").ok
    assert not call(registry, "search_code", pattern=123).ok
    assert not call(registry, "run_cli", args="report").ok
    assert not call(registry, "run_cli", args=["report", 1]).ok
    assert not call(registry, "read_file", path="README.md", mode="w").ok
    assert not call(registry, "list_files", unexpected=True).ok


def test_invalid_regex_is_rejected(registry):
    result = call(registry, "search_code", pattern="(")
    assert not result.ok
    message = (result.error or "").lower()
    assert "regex" in message or "regular expression" in message


def test_unknown_tool_is_rejected(registry):
    result = registry.execute("run_shell", json.dumps({"command": "ls"}))
    assert not result.ok
    assert "unknown tool" in (result.error or "").lower()


# ---------------------------------------------------------------- imports


def test_both_import_styles_share_the_contract_module():
    import importlib

    import tools as top_level_tools

    package_tools = importlib.import_module("task4_tools.tools")
    from task4_tools.tool_contract import ToolResult as package_result
    from tool_contract import ToolResult as top_result

    assert top_result is package_result
    assert issubclass(top_level_tools.ToolRegistry, package_tools.ToolRegistryBase)
    assert issubclass(package_tools.ToolRegistry, top_level_tools.ToolRegistryBase)
