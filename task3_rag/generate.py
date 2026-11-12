"""Generate grounded answers from Task 3 retrieval results.

Input is a retrieval JSONL file produced by ``pipeline.py``; output is the answer
format expected by ``judge.py`` (id, answer, contexts).  For the test split the
same script also writes the submission schema (id, answer, retrieved_doc_ids).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.llm_client import LLMClient  # noqa: E402

ABSTENTION = "The documentation does not contain this information."

SYSTEM_PROMPT = """You are a careful documentation assistant for PaderQueue, a fictional message queue.
Answer the user's question using ONLY the retrieved context below.
- The context is the only source of truth. Do not use outside knowledge or guess.
- Prefer official documentation (config, error, cli, changelog, incident, runbook) over forum posts.
- Pay close attention to version numbers. If the question names a version, answer for that version.
- Include exact identifiers, values, flags, and error codes in your answer.
- Be concise: usually one to three sentences.
- If the context does not contain enough information to answer, reply with exactly this sentence and nothing else:
  The documentation does not contain this information."""

USER_TEMPLATE = """RETRIEVED CONTEXT:
{context}

QUESTION:
{question}

Answer the question using only the context above."""


def load_jsonl(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def format_context(retrieved: list[dict]) -> str:
    blocks = []
    for i, item in enumerate(retrieved, start=1):
        header = f"[{i}] doc={item.get('doc_id', '?')} type={item.get('type', '?')}"
        if item.get("version"):
            header += f" version={item['version']}"
        header += f" title={item.get('title', '')}"
        blocks.append(header + "\n" + item.get("chunk_text", ""))
    return "\n\n---\n\n".join(blocks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate answers from a Task 3 retrieval file.")
    parser.add_argument("retrieval", type=Path)
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--timing", type=Path)
    parser.add_argument("--max-tokens", type=int, default=800)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--model")
    parser.add_argument("--predictions", action="store_true",
                        help="write the test submission schema (id, answer, retrieved_doc_ids)")
    args = parser.parse_args()

    questions = {q["id"]: q for q in load_jsonl(HERE / "data" / f"questions_{args.split}.jsonl")}
    rows = load_jsonl(args.retrieval)
    if args.limit:
        rows = rows[: args.limit]

    existing = load_jsonl(args.out) if args.out.exists() else []
    done = {entry["id"] for entry in existing}
    rows = [row for row in rows if row["id"] not in done]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out_handle = args.out.open("a", encoding="utf-8")

    llm = LLMClient(model=args.model, log_path=args.log)
    answers = list(existing)
    latencies = []
    started_all = time.perf_counter()
    for i, row in enumerate(rows, start=1):
        question = questions[row["id"]]["question"]
        contexts = [item.get("chunk_text", "") for item in row.get("retrieved", [])]
        prompt = USER_TEMPLATE.format(context=format_context(row.get("retrieved", [])), question=question)
        started = time.perf_counter()
        answer = llm.chat(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            max_tokens=args.max_tokens,
        ).strip()
        latency = time.perf_counter() - started
        latencies.append(latency)
        if not answer:
            answer = ABSTENTION
        if args.predictions:
            doc_ids = list(dict.fromkeys(row.get("retrieved_doc_ids", [])))[:5]
            entry = {"id": row["id"], "answer": answer, "retrieved_doc_ids": doc_ids}
        else:
            entry = {"id": row["id"], "answer": answer, "contexts": contexts}
            if args.split == "test":
                entry["retrieved_doc_ids"] = row.get("retrieved_doc_ids", [])
        answers.append(entry)
        out_handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        out_handle.flush()
        print(f"[{i}/{len(rows)}] {row['id']} {latency:.2f}s {answer[:80]!r}")
    out_handle.close()

    wall_time = time.perf_counter() - started_all
    timing = {
        "retrieval": str(args.retrieval),
        "split": args.split,
        "n_answers": len(answers),
        "new_answers": len(rows),
        "resumed": bool(done),
        "wall_time_s": round(wall_time, 3),
        "mean_latency_s": round(statistics.mean(latencies), 3) if latencies else 0.0,
        "median_latency_s": round(statistics.median(latencies), 3) if latencies else 0.0,
        "p95_latency_s": round(sorted(latencies)[max(0, int(0.95 * len(latencies)) - 1)], 3) if latencies else 0.0,
        "llm_calls": llm.n_calls,
        "prompt_tokens": llm.total_prompt_tokens,
        "completion_tokens": llm.total_completion_tokens,
        "cost_usd": round(llm.total_cost_usd, 8),
        "abstentions": sum(1 for entry in answers if entry["answer"].strip().lower() == ABSTENTION.lower()),
    }
    print(json.dumps(timing, indent=2))
    if args.timing:
        args.timing.parent.mkdir(parents=True, exist_ok=True)
        args.timing.write_text(json.dumps(timing, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
