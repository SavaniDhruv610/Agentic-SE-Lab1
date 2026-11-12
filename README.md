# Lab 1 – Agentic Software Engineering (WS 2026/27)

Starter material for Lab 1. The tasks, deadlines and grading are described on the lab sheet.

## Team

Group name: **Five Bits**

| Name | Matriculation number |
|---|---|
| Dhruv Savani | `[TODO: matriculation number]` |
| Harshil Zadafiya | `[TODO: matriculation number]` |
| Jevin Vaghasiya | `[TODO: matriculation number]` |
| Pooja Garg | `[TODO: matriculation number]` |
| Yashik Khunt | `[TODO: matriculation number]` |

## Access to the UPB AI Gateway (do this first)

Every group member needs their own API key. Official guide: https://hilfe.uni-paderborn.de/AI-Gateway

1. **Activate the service** (once): log in to the Service Portal https://sp.upb.de/ with your university account →
   *Benutzerverwaltung* → *Weitere Dienste* → tab *Lehre und Studium* → *KI-Chat-Oberfläche* → *Optionen* → *Beantragen*.
   The request is approved automatically.
2. **Be in the university network:** the gateway only works from the university network (e.g. eduroam on campus)
   or via the university VPN. From home, connect the VPN first: https://hilfe.uni-paderborn.de/VPN_einrichten
3. **Create a key:** open https://ai-gateway.upb.de, log in with your university account, click *API-Key erstellen*
   and copy the key (`sk-...`). Store it safely; you can't see it again.
4. Keys **expire** after some time. If calls suddenly fail with `401`, create a new key and update your `.env`.
5. The terms of use of the university's AI services apply to the API as well.

Never commit the key, never paste it into a prompt, never put it into a file you submit.

## Setup

```bash
# 0. Install uv (once, no root needed), then open a new terminal (or: source ~/.bashrc)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 1. Python environment (Python 3.11+)
uv sync                    # or: python -m venv .venv && pip install -r requirements.txt
uv sync --extra local      # optional: local embedding / cross-encoder models (large download)

# 2. Gateway key
cp .env.example .env       # then put your key into .env (the model names are already filled in)

# 3. Check (in the university network or VPN)
uv run python common/hello_gateway.py
```

Run all commands from this directory (the repository root).

## How to run everything

Prerequisites: the virtualenv is installed, `.env` contains your key, and you are in the
university network or on the UPB VPN. Commands below use `.venv/bin/python`; replace it with
`uv run python` if you use `uv`.

### Task 1 – OpenCode setup and team website

Task 1 is interactive and per member; see `task1_opencode/README.md` for the full guide.

```bash
cd task1_opencode
opencode                  # create hello.py, then work on the team website
# save the exported session under task1_opencode/conversation/
cd ..
```

### Task 2 – Own agent

```bash
.venv/bin/python task2_own_agent/agent.py \
  --content task1_opencode/content \
  --out task2_own_agent/site/index.html \
  --group "Five Bits"
```

The agent prompts for human feedback after it finishes; press Enter on an empty line to end.

### Task 3 – RAG

```bash
# Final configuration (R5) on the dev split
.venv/bin/python task3_rag/pipeline.py --config r5 --split dev \
  --chunk-size 160 --overlap 32 --alpha 0.7 --forum-penalty 0.03 \
  --out task3_rag/results/retrieval_r5_160_a07_pen03_dev.jsonl \
  --timing task3_rag/results/timing_r5_160_a07_pen03_dev.json \
  --rewrite-cache .cache/task3_rag/rewrites/rewrites_v1.jsonl

# Retrieval metrics
.venv/bin/python task3_rag/evaluate.py \
  task3_rag/results/retrieval_r5_160_a07_pen03_dev.jsonl --k 5 \
  --json task3_rag/results/metrics_r5_160_a07_pen03_dev.json

# Generated answers + LLM judge sample
.venv/bin/python task3_rag/generate.py \
  task3_rag/results/retrieval_r5_160_a07_pen03_dev.jsonl --split dev \
  --out task3_rag/results/answers_r5_dev.jsonl \
  --log task3_rag/results/logs/generate_r5_dev.jsonl
.venv/bin/python task3_rag/judge.py task3_rag/results/answers_r5_dev.jsonl \
  --sample 50 --out task3_rag/results/judge_r5_dev.jsonl
```

The submitted test predictions are in `task3_rag/predictions_test.jsonl`.

### Task 4 – Tool-using agent

```bash
# 0. Optional connectivity check
.venv/bin/python common/hello_gateway.py

# 1. Fresh workspace + real model run
.venv/bin/python task4_tools/reset_workspace.py
.venv/bin/python task4_tools/agent.py

# 2. Save the diff and verify the workspace tests
git -C task4_tools/workspace diff > task4_tools/final.diff
.venv/bin/python -m pytest task4_tools/workspace/tests -v

# 3. Report check (expected: Total: 362,30 €)
cd task4_tools/workspace
PYTHONPATH=src ../../.venv/bin/python -m tinyledger report data/sample.csv --month 2026-03
cd ../..

# 4. Tool-layer / adversarial tests
.venv/bin/python -m pytest task4_tools/tests -v
```

Use `--mock` (`task4_tools/agent.py --mock`) for an offline deterministic run.

## Models, budget and limits

| Purpose | Model id | Cost |
|---|---|
| Default for all tasks (chat, agents, tool calling, judge) | `openai.gpt-6-luna` | $0.10 / 1M input tokens, $0.50 / 1M output tokens |
| Free fallback | `kiconnect.qwen3.8-27b` | free |
| Embeddings | `kiconnect.qwen3-embedding-8b` (fallback: `kiconnect.e5-mistral-7b-instruct`) | free |

- **Budget:** every student has **$20 per month** on the gateway. With `gpt-6-luna` the whole lab costs a group
  a few dollars, so you have plenty of room. It only gets expensive if you switch to one of the large models, which you don't need.
- **Fallback:** if your budget is used up (or a teammate's key can take over), use the free fallback model:
  `LLMClient(model=os.environ["LAB_FALLBACK_MODEL"])`. It is weaker, but supports tool calling.
  Say in your presentation which runs used which model.
- **Cost tracking:** `LLMClient` writes the cost of every call (`cost_usd`, reported by the gateway) into its log,
  and `llm.usage_summary()` prints tokens and cost so far. OpenCode shows the cost of a session in its UI.
- **Rate limits:** 100 requests per minute and at most 10 parallel requests per key. A normal agent loop stays far below this.
  Don't send requests from more than ~5 threads at the same time. `LLMClient` retries automatically when you hit the limit.
- **No `temperature`:** `gpt-6-luna` is a reasoning model and rejects the `temperature` parameter. Its output tokens
  include hidden reasoning tokens, so set `max_tokens` generously (or not at all).
- **Response cache:** the gateway caches responses. Sending exactly the same request again returns the stored answer
  within milliseconds (the log shows `"cache_hit": true`). Keep this in mind when you measure latency, and when you
  retry a request after a bad answer: change something (e.g. add the error message), otherwise you get the same answer again.

## Contents

| Folder | Content |
|---|---|
| `common/` | `llm_client.py` (gateway client with logging), `site_checker.py`, `embed_images.py` |
| `task1_opencode/` | OpenCode setup guide, config template, profile template |
| `task2_own_agent/` | specification, style guide (55 documents) |
| `task3_rag/` | PaderQueue dataset, questions, `evaluate.py`, `judge.py` |
| `task4_tools/` | toy repository `tinyledger`, tool interface, public tests, mock model |


## Troubleshooting

| Problem | Try this |
|---|---|
| `Command 'uv' not found` | Install uv (step 0 above) and open a new terminal, or use the venv route without uv |
| `401 Unauthorized` | Key missing in `.env`, or expired: create a new one at https://ai-gateway.upb.de |
| Connection timeout / connection refused | You are outside the university network. Connect the university VPN |

## Rules

- Never commit or submit your `.env` / API keys.
- Don't use agent or RAG frameworks (LangChain, LlamaIndex, ...) in Tasks 2–4.
- Every group member must be able to explain every part of the submission.
