"""Local runner: sample -> rate-limited Bedrock classification -> JSONL.

Usage:
    python -m src.pipeline.cli --csv data/sample_tweets.csv
    python -m src.pipeline.cli --csv path/to.csv --sample-size 120 --seed 42
    python -m src.pipeline.cli --csv path/to.csv --mock --validate

``--mock`` runs the full pipeline with a deterministic fake model client so
the flow can be exercised without an AWS account (CI / quick checks).
Real runs use Bedrock via the credentials configured in ``config/.env``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

from .constants import (
    DEFAULT_SAMPLE_SIZE,
    MAX_TWEETS_PER_SECOND,
    MIN_SAMPLE_SIZE,
    MODEL_ID,
    SUICIDE_LIKELIHOOD_LEVELS,
)
from .evaluator import TweetEvaluator
from .model_client import BedrockClient, MockClient
from .rate_limiter import TokenBucket
from .sampler import load_tweets, sample_rows
from .validation import confusion


def _dotenv(path: Path) -> None:
    """Minimal .env loader (no external dependency)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _summary(records: list[dict]) -> dict:
    counts = {level: 0 for level in SUICIDE_LIKELIHOOD_LEVELS}
    errors = 0
    for record in records:
        if "suicide_likelihood" in record:
            counts[record["suicide_likelihood"]] += 1
        else:
            errors += 1
    classified = len(records) - errors
    alerts = sum(1 for r in records if r.get("alert_of_risk") == 1)
    return {
        "records": len(records),
        "classified": classified,
        "errors": errors,
        "counts_by_level": counts,
        "alert_of_risk_1": alerts,
        "alert_rate": round(alerts / classified, 4) if classified else None,
    }


def run(args: argparse.Namespace) -> int:
    _dotenv(Path(__file__).resolve().parents[2] / "config" / ".env")
    rows = load_tweets(args.csv)
    rng = random.Random(args.seed) if args.seed is not None else None
    sampled = sample_rows(rows, args.sample_size, rng=rng)
    print(f"loaded {len(rows)} tweets; sampled {len(sampled)} without replacement")

    client = MockClient() if args.mock else BedrockClient(model_id=args.model)
    limiter = TokenBucket(capacity=args.capacity, refill_rate=args.rate)
    evaluator = TweetEvaluator(client, model_id=args.model)

    records = []
    for row in sampled:
        limiter.acquire()
        records.append(evaluator.evaluate(row["tweet_id"], row["tweet"]))

    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "results.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary = _summary(records)
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {jsonl_path} and {summary_path}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.validate:
        metrics = confusion(records, rows)
        print("offline [Suicide] comparison:", json.dumps(metrics, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        default=r"data/sample_tweets.csv",
        help="input CSV with a Tweet column (any compatible CSV accepted as-is)",
    )
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE,
                        help=f"tweets to analyze (default {DEFAULT_SAMPLE_SIZE}, min {MIN_SAMPLE_SIZE})")
    parser.add_argument("--rate", type=float, default=float(MAX_TWEETS_PER_SECOND),
                        help="token refill per second (default 10)")
    parser.add_argument("--capacity", type=float, default=1.0,
                        help="token bucket capacity; 1 = strict 10/sec ceiling (default)")
    parser.add_argument("--model", default=MODEL_ID, help="Bedrock model id")
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed for reproducible sampling (default: non-deterministic)")
    parser.add_argument("--output", default="out", help="directory for results.jsonl + summary.json")
    parser.add_argument("--mock", action="store_true",
                        help="use a deterministic fake model client (no AWS needed)")
    parser.add_argument("--validate", action="store_true",
                        help="offline comparison against the [Suicide] label after the run")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.sample_size < MIN_SAMPLE_SIZE:
        parser.error(f"--sample-size must be >= {MIN_SAMPLE_SIZE} (minimum 100)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())