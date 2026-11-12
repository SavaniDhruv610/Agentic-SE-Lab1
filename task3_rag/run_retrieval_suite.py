"""Run the full Task 3 retrieval matrix in one process.

This is a convenience wrapper around ``pipeline.build_index``.  It reuses the
embedding cache and the already-imported gateway client, which matters on slow
network drives.  Individual configurations can still be run with ``pipeline.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common.llm_client import LLMClient  # noqa: E402

try:
    from .pipeline import build_index, summarize_llm_log
except ImportError:  # pragma: no cover
    from pipeline import build_index, summarize_llm_log

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
LOGS = RESULTS / "logs"


def run_one(args, name: str, *, config: str, chunk_size: int, overlap: int,
            alpha: float = 0.5, forum_penalty: float = 0.0) -> dict:
    log_path = LOGS / f"{name}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    llm = LLMClient(log_path=log_path)
    results, timing = build_index(
        config=config,
        split=args.split,
        chunk_size=chunk_size,
        overlap=overlap,
        alpha=alpha,
        forum_penalty=forum_penalty,
        llm=llm,
        cache_root=Path(".cache/task3_rag"),
        limit=args.limit,
        query_batch_size=args.query_batch_size,
        rewrite_cache=Path(".cache/task3_rag/rewrites/rewrites_v1.jsonl"),
    )
    out = RESULTS / f"retrieval_{name}_{args.split}.jsonl"
    timing_path = RESULTS / f"timing_{name}_{args.split}.json"
    with out.open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    timing["name"] = name
    timing["gateway_log"] = summarize_llm_log(log_path)
    timing["llm_usage"] = {
        "calls": llm.n_calls,
        "prompt_tokens": llm.total_prompt_tokens,
        "completion_tokens": llm.total_completion_tokens,
        "cost_usd": round(llm.total_cost_usd, 8),
    }
    timing_path.write_text(json.dumps(timing, indent=2), encoding="utf-8")
    print(
        f"{name:<12} chunks={timing['chunks']['n_chunks']:>5} "
        f"index={timing['index_build_time_s']:>6.2f}s "
        f"online={timing['mean_online_ms']:>7.1f}ms/q "
        f"emb_hits={timing.get('embeddings', {}).get('cache_hits', 0)} "
        f"cost=${timing['llm_usage']['cost_usd']:.5f}"
    )
    return timing


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all retrieval configurations for Task 3.")
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--query-batch-size", type=int, default=64)
    parser.add_argument("--skip-r3-sweep", action="store_true")
    parser.add_argument("--only", nargs="*", help="subset of configuration names")
    args = parser.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)

    configs = [
        ("r1_080", dict(config="r1", chunk_size=80, overlap=16)),
        ("r1_160", dict(config="r1", chunk_size=160, overlap=32)),
        ("r1_320", dict(config="r1", chunk_size=320, overlap=48)),
        ("r2_160", dict(config="r2", chunk_size=160, overlap=32)),
        ("r3_080", dict(config="r3", chunk_size=80, overlap=16)),
        ("r3_160", dict(config="r3", chunk_size=160, overlap=32)),
        ("r3_320", dict(config="r3", chunk_size=320, overlap=48)),
        ("r4_a03", dict(config="r4", chunk_size=160, overlap=32, alpha=0.3)),
        ("r4_a05", dict(config="r4", chunk_size=160, overlap=32, alpha=0.5)),
        ("r4_a07", dict(config="r4", chunk_size=160, overlap=32, alpha=0.7)),
        ("r5_pen00", dict(config="r5", chunk_size=160, overlap=32, alpha=0.5, forum_penalty=0.0)),
        ("r5_pen03", dict(config="r5", chunk_size=160, overlap=32, alpha=0.5, forum_penalty=0.03)),
        # Extra sweep used to select the final chunk size / alpha / penalty.
        ("r4_080_a05", dict(config="r4", chunk_size=80, overlap=16, alpha=0.5)),
        ("r4_080_a07", dict(config="r4", chunk_size=80, overlap=16, alpha=0.7)),
        ("r4_080_a085", dict(config="r4", chunk_size=80, overlap=16, alpha=0.85)),
        ("r5_080_a05_pen03", dict(config="r5", chunk_size=80, overlap=16, alpha=0.5, forum_penalty=0.03)),
        ("r5_080_a07_pen00", dict(config="r5", chunk_size=80, overlap=16, alpha=0.7, forum_penalty=0.0)),
        ("r5_080_a07_pen03", dict(config="r5", chunk_size=80, overlap=16, alpha=0.7, forum_penalty=0.03)),
        ("r5_080_a07_pen06", dict(config="r5", chunk_size=80, overlap=16, alpha=0.7, forum_penalty=0.06)),
        ("r5_080_a085_pen03", dict(config="r5", chunk_size=80, overlap=16, alpha=0.85, forum_penalty=0.03)),
        ("r5_160_a07_pen03", dict(config="r5", chunk_size=160, overlap=32, alpha=0.7, forum_penalty=0.03)),
        ("r5_160_a085_pen03", dict(config="r5", chunk_size=160, overlap=32, alpha=0.85, forum_penalty=0.03)),
        ("r5_160_a065_pen03", dict(config="r5", chunk_size=160, overlap=32, alpha=0.65, forum_penalty=0.03)),
        ("r5_160_a075_pen03", dict(config="r5", chunk_size=160, overlap=32, alpha=0.75, forum_penalty=0.03)),
        ("r5_160_a080_pen03", dict(config="r5", chunk_size=160, overlap=32, alpha=0.80, forum_penalty=0.03)),
        ("r5_160_a07_pen02", dict(config="r5", chunk_size=160, overlap=32, alpha=0.7, forum_penalty=0.02)),
        ("r5_160_a07_pen04", dict(config="r5", chunk_size=160, overlap=32, alpha=0.7, forum_penalty=0.04)),
        ("r5_160_a07_pen05", dict(config="r5", chunk_size=160, overlap=32, alpha=0.7, forum_penalty=0.05)),
    ]
    if args.skip_r3_sweep:
        configs = [item for item in configs if item[0] not in {"r3_080", "r3_320", "r1_080", "r1_320"}]

    summary = {}
    for name, kwargs in configs:
        if args.only and name not in args.only:
            continue
        summary[name] = run_one(args, name, **kwargs)

    (RESULTS / f"timing_{args.split}_all.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nWrote {RESULTS / f'timing_{args.split}_all.json'}")


if __name__ == "__main__":
    main()
