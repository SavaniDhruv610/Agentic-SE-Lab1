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

## Implementation notes (submission)

### Files

| File | Purpose |
|---|---|
| `rag.py` | Loads and chunks the style guide, builds the embedding index, cosine-similarity search |
| `agent.py` | Agent loop, JSON protocol, actions, image resolution, checker integration, human feedback |
| `site/index.html` | The generated single-file website (photos embedded as data URIs) |
| `logs/run_01.jsonl` | Complete real gateway run that produced the submitted site (14 steps, `openai.gpt-6-luna`) |
| `logs/run_00_offline.jsonl` | Additional deterministic offline validation run (scripted model, BM25); exercises the human-feedback loop |

### How to run

From the repository root, on a machine in the university network or VPN:

```bash
uv run python task2_own_agent/agent.py \
  --content task1_opencode/content \
  --out task2_own_agent/site/index.html \
  --group "Five Bits"
```

If `uv` is not installed (for example in this workspace), use the existing virtualenv:
`.venv/bin/python task2_own_agent/agent.py ...`. Empty input at the feedback prompt
ends the run. Useful options:

| Option | Default | Meaning |
|---|---|---|
| `--model` | `$LAB_CHAT_MODEL` | chat model |
| `--max-steps` | `25` | maximum model calls |
| `--k` | `4` | style-guide chunks per search |
| `--log` | `logs/run_XX.jsonl` | JSONL log path; auto-incremented if omitted |
| `--rag-backend` | `auto` | `embeddings` (gateway), `bm25` (offline), or `auto` |
| `--max-tokens` | unset | forwarded to the model; unset is safest for long HTML output |
| `--no-feedback` | off | do not prompt for terminal feedback after `finish` |

### Design decisions

**Chunking.** Every `.md` file in `style_guide/` is loaded and split on blank-line
paragraph boundaries into chunks of at most 320 tokens. Each chunk starts with its
title, `Status` and `Category`, and carries its document id (for example `SG-13`).
Splitting is heading-aware and never splits inside an oversized block unless the
block alone exceeds the budget; in that case it splits on line boundaries.

**Embedding model and search.** The normal backend is the gateway embedding model
(`$LAB_EMBEDDING_MODEL`, `kiconnect.qwen3-embedding-8b`). Vectors are L2-normalised
and kept in an in-memory numpy matrix; search ranks by cosine similarity. `k=4` by
default. Deprecated documents are indexed but receive a score penalty, so the active
replacement is normally shown first. `--rag-backend bm25` switches to `rank-bm25`
for offline development; `auto` tries embeddings and falls back to BM25 if the
embedding call fails.

**Context budget.** `format_search_results` packs chunks so that the whole observation,
including its short footer, stays within 1,000 tokens (measured with `count_tokens`).
A single oversized chunk is truncated. The system prompt and the task description
contain only protocol and topic names, never style-guide rules.

**Prompt design.** The system prompt defines the JSON action protocol, the four
actions and the rule that only ACTIVE documents are authoritative. The task prompt
gives only the content directory, output path, group name and a list of topics to
search (metadata, structure, palettes, theming, cards, typography, responsiveness,
footer, accessibility). The model must fetch the actual rules itself.

**Invalid output.** Each model response is parsed by searching for the first valid
JSON object with an `action` key. Non-JSON output, unknown actions, missing/wrong
arguments and missing HTML are returned as an error observation and cost one step;
the loop does not crash. A `write_site` with base64 image data is rejected and the
model is told to use placeholders. `finish` is rejected until the most recent full
checker report passed.

**Images.** `read_profiles` lists the image files that actually exist. The profile
Markdown links (`images/<firstname>.jpg`) are stale: the real photos are in
`content/Photos/` with capitalised names. Placeholders are resolved by exact path,
basename, stem, and finally a small typo-tolerant similarity match (this maps
`yashik.jpg` to the actual `Yasik.png`). Base64 is never requested from the model.

**Logging and feedback.** `LLMClient` writes the raw chat requests/responses. The
agent additionally logs step-numbered `request`, `response`, `observation` and
`human_feedback` events with `llm.log_event`. After an accepted `finish`, non-empty
terminal feedback is logged and appended as a new user message; the last checker
report is cleared so the model must write and verify the site again before finishing.

### Known limitations

- The embedding backend needs the UPB gateway. The gateway was initially unreachable
  in this environment, so `logs/run_00_offline.jsonl` is a deterministic offline
  validation run with a scripted model (`scripted-offline-demo`) and the BM25 backend.
  Once gateway access became available, the final `logs/run_01.jsonl` was produced with
  the real chat and embedding models and generated the submitted `site/index.html`
  (14 steps, 25 model/embedding calls, 92,765 prompt + 17,194 completion tokens,
  $0.01156). The real run ends with an empty feedback input; the offline log exercises
  the non-empty human-feedback iteration because the scripted driver can supply it
  deterministically.
- Search returns whole-document chunks for the current 55-document guide because every
  document fits the 320-token budget. If the guide grows, the paragraph splitter will
  create multiple chunks per document.
- Deprecated chunks are penalised rather than removed, so they can still appear in a
  search result (clearly marked). The model is instructed to use only ACTIVE rules.
- The agent supports only the four required actions and writes one output file; it
  does not edit arbitrary project files or run shell commands.
- The checker covers the mechanical rules, not visual taste. The generated page is
  responsive and accessible but intentionally plain.
