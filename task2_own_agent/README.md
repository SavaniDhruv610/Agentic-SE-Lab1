# Task 2 – Build your own (small) coding agent

There is no starter code for this task, only this specification and some helpers.
Read the task description on the lab sheet first.

## What's in this folder

| File | Purpose |
|---|---|
| `style_guide/` | The fictional "UPB Agentic SE Web Style Guide" (55 Markdown documents) |
| `check_site.py` | Checks a website against the basic requirements AND the style guide (`--basic` for basic only) |
| `embed_images.py` | Replaces `{{image:<file>}}` placeholders with embedded base64 images |

Helpers you can import from `common/` (run your code from the repository root, or add it to `sys.path`):

```python
from common.llm_client import LLMClient, count_tokens
from common.site_checker import check_site, member_names_from_content
from common.embed_images import embed_images
```

## Specification

### Allowed dependencies
Python standard library, `numpy`, `rank-bm25`, `tiktoken`, the provided `common/` helpers,
optionally `sentence-transformers` for local embeddings.
Not allowed: LangChain, LlamaIndex, Haystack, agent SDKs, vector databases.

### RAG component – `rag.py`
- Loads all documents from `style_guide/` and splits them into chunks.
- Embeds the chunks, keeps the vectors in memory (a numpy matrix is enough).
- Provides `search(query: str, k: int) -> list[Chunk]`, returning the most similar chunks by cosine similarity.
  Each chunk carries its text and the id of its source document (e.g. `SG-13`).
- Context budget rules:
  - one `search_style_guide` observation may contain at most **1,000 tokens** of style guide text
    (measure with `count_tokens` from `common/llm_client.py`)
  - your system prompt and task description must not contain style guide text

### Agent loop – `agent.py`
The model answers with exactly one JSON object per step:

```json
{"thought": "short reasoning", "action": "<action name>", "args": {}}
```

Required actions:

| Action | Args | What your code does | Observation returned to the model |
|---|---|---|---|
| `search_style_guide` | `{"query": str}` | calls `rag.search` | retrieved chunks with document ids (≤ 1,000 tokens) |
| `read_profiles` | `{}` | reads all `content/*.md` files (not `_template.md`) | profile texts + list of available image files |
| `write_site` | `{}` + the HTML in a ` ```html ` code block directly after the JSON (or `{"html": str}`) | resolves `{{image:...}}` placeholders, saves the file, runs `check_site(..., mode="full")` | the checker report (or an error, e.g. unknown image) |
| `finish` | `{"summary": str}` | accepted only if the last checker report passed | if rejected: the failing checks |

Why the extra code block for `write_site`? A whole HTML page inside a JSON string needs thousands of escaped
quotes and newlines, and models get that wrong surprisingly often. You may support both variants.

Behaviour your loop must have:
- A maximum number of steps (e.g. 25). Stop with a clear message when it is reached.
- If the model output is not valid JSON or uses an unknown action or wrong args: don't crash.
  Send an error observation back and continue. Count it as a step.
- The model must never write base64 image data. It uses placeholders like `<img src="{{image:anna.jpg}}" alt="...">`.
- Human in the loop: after an accepted `finish`, ask the user for feedback on the terminal.
  Non-empty feedback is appended as a new user message and the loop continues. Empty input ends the run.
- Don't use the native function-calling / `tools` API. You build the protocol yourselves.
- A complete website is several thousand output tokens, plus the model's hidden reasoning tokens. Set `max_tokens` high
  enough (or don't set it), otherwise the answer is cut off (the log shows `"finish_reason": "length"`).
- Embeddings: use the gateway embedding model via `llm.embed(...)` (free), or a local model.

### Command line
From the repository root:

```bash
uv run python task2_own_agent/agent.py --content task1_opencode/content --out task2_own_agent/site/index.html
```

You may add options (model, max steps, k, ...). Document them in your README.

### Logs
- Store every message sent to and received from the model, with step numbers, in `task2_own_agent/logs/run_XX.jsonl`.
- The simplest way: use `LLMClient(log_path=...)` and add your own entries (observations, human feedback) with `llm.log_event({...})`.
- Submit the logs of at least one complete run that produced your submitted `site/index.html`.

### What your README must contain
- how to run the agent
- your design decisions: chunking, embedding model, k, prompt design, how you handle invalid output
- known limitations

---

# Implementation notes (submission)

## Files

| File | Content |
|---|---|
| `rag.py` | `StyleGuideRAG`: chunking, embedding, `search(query, k)`, token-limited observation |
| `agent.py` | the agent loop, the four actions, parsing of the model output, logging, human feedback |

## How to run

From the repository root, in the university network or VPN, with the key in `.env`:

```bash
uv run python task2_own_agent/agent.py --content task1_opencode/content --out task2_own_agent/site/index.html
```

The agent prints one line per step. After an accepted `finish` it asks for feedback on the terminal:
type feedback to continue, or press Enter to end the run.

| Option | Default | Meaning |
|---|---|---|
| `--content` | `task1_opencode/content` | directory with `<firstname>.md` and `images/` |
| `--out` | `task2_own_agent/site/index.html` | where the page is saved |
| `--model` | `$LAB_CHAT_MODEL` | chat model, e.g. the fallback model |
| `--embedding-model` | `$LAB_EMBEDDING_MODEL` | embedding model |
| `--max-steps` | 25 | step limit (counted again after each human feedback) |
| `--k` | 5 | chunks retrieved per search |
| `--max-tokens` | not set | `max_tokens` per model call |
| `--no-feedback` | off | end after the first accepted `finish` |
| `--log` | `logs/run_XX.jsonl` (next free number) | log file |

Other commands:

```bash
uv run python task2_own_agent/rag.py "which footer text is required"    # try the retrieval alone
uv run python task2_own_agent/check_site.py task2_own_agent/site/index.html --content task1_opencode/content
```

## Design decisions

**Chunking.** The style guide documents are short (28 to 144 words), so one document is one chunk. A document
longer than 300 tokens would be split at its `##` headings (none is at the moment). Every chunk starts with a
header built from the document: `[SG-13] Night mode palette (version 2) | Status: ACTIVE (version 2)`. The status
is in every chunk on purpose: the guide contains deprecated documents, and a deprecated rule without its status
looks exactly like a valid one.

**Embedding model.** The gateway embedding model (`$LAB_EMBEDDING_MODEL`) through `llm.embed`. The 55 vectors are
kept in one normalized numpy matrix, so cosine similarity is a single matrix-vector product. The matrix is cached
in `.cache/` (the file name contains the model name and a hash of the chunk texts).

**k and the context budget.** `k = 5`. Five chunks are about 500 to 700 tokens. `format_observation` adds chunks
in rank order and stops before the text would exceed 1,000 tokens (measured with `count_tokens`). A chunk is never
cut in the middle. Deprecated chunks are not filtered out: the ranking is pure cosine similarity, and the model
decides with the status line.

**Prompt design.** The system prompt contains the protocol, the technical requirements of the website and
instructions on how to use the style guide (search before writing, one query per aspect, never use a DEPRECATED
passage, look up the rule for every failed check). It contains no style guide text; a test checks that none of the
values the checker looks for appears in the prompts. Observations are sent as user messages starting with
`OBSERVATION:`.

**Invalid output.** `parse_reply` first removes the ```` ```html ```` block from the reply, then takes the first
JSON object from the rest, then validates the action name and the arguments (names and types). Every violation
raises a `ProtocolError`; its message goes back to the model as an error observation and the step counts.
Handled cases: empty reply, no JSON, broken JSON, unknown action, missing/wrong/unexpected arguments,
`write_site` without HTML, a code block that was cut off, base64 image data in the HTML, unknown image file,
`finish` before a passing report.

**History.** Each `write_site` reply contains the whole page. Before every model call only the newest page stays
in the history; older ones are replaced by a one-line note. This keeps the prompt small over many attempts.

**Logs.** `LLMClient` logs every request and response. We add one `observation` event per step (with step number
and action), plus `run_start`, `human_feedback`, `error` and `run_end`.

## Known limitations

- A cut-off reply is only detected through the unclosed code block; we do not read `finish_reason` from the API.
- The checker tests the presence of values and markup, not the look of the page. A page can pass and still be
  ugly; that is what the human feedback step is for.
- Retrieval is purely dense. Queries with exact identifiers would profit from a keyword component.
- The model chooses its own queries. If it never asks about a topic, it never sees the rule, and only finds out
  through a failed check.
- Feedback restarts the step limit; there is no overall limit across feedback rounds.

