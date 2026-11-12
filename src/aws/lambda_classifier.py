"""Classifier Lambda (Step Functions state: Classify).

Reads the sampled tweets from S3, classifies each one through Bedrock under
the token-bucket rate limit (strict default: 1 token, 10/sec, so no
one-second window exceeds 10 request starts), derives ``alert_of_risk`` in
code, and writes ``results/<run_id>.jsonl`` plus a run summary. The input
files are never modified.
"""

from __future__ import annotations

import json
import os

import boto3

from pipeline.constants import MODEL_ID, SUICIDE_LIKELIHOOD_LEVELS
from pipeline.evaluator import TweetEvaluator
from pipeline.model_client import BedrockClient
from pipeline.rate_limiter import TokenBucket

s3 = boto3.client("s3")


def _load_sample(bucket: str, sample_key: str) -> list[dict]:
    payload = json.loads(s3.get_object(Bucket=bucket, Key=sample_key)["Body"].read().decode("utf-8"))
    return payload["sample"]


def handler(event: dict, context) -> dict:
    bucket = os.environ["S3_BUCKET"]
    sample_key = event["sample_key"]
    capacity = float(os.environ.get("RATE_LIMIT_CAPACITY", "1"))
    refill = float(os.environ.get("RATE_LIMIT_REFILL_PER_SECOND", "10"))
    model_id = os.environ.get("BEDROCK_MODEL_ID", MODEL_ID)

    tweets = _load_sample(bucket, sample_key)
    run_id = event["run_id"]

    limiter = TokenBucket(capacity=capacity, refill_rate=refill)
    evaluator = TweetEvaluator(BedrockClient(model_id=model_id), model_id=model_id)

    records = []
    for tweet in tweets:
        limiter.acquire()
        # label is intentionally never sent to the evaluator / model
        records.append(evaluator.evaluate(tweet["tweet_id"], tweet["tweet"]))

    results_key = f"results/{run_id}.jsonl"
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    s3.put_object(Bucket=bucket, Key=results_key, Body=body.encode("utf-8"))

    counts = {level: 0 for level in SUICIDE_LIKELIHOOD_LEVELS}
    errors = 0
    for record in records:
        if "suicide_likelihood" in record:
            counts[record["suicide_likelihood"]] += 1
        else:
            errors += 1
    classified = len(records) - errors
    alerts = sum(1 for r in records if r.get("alert_of_risk") == 1)
    summary = {
        "run_id": run_id,
        "records": len(records),
        "classified": classified,
        "errors": errors,
        "counts_by_level": counts,
        "alert_of_risk_1": alerts,
        "alert_rate": round(alerts / classified, 4) if classified else None,
        "results_key": results_key,
    }
    s3.put_object(
        Bucket=bucket,
        Key=f"results/{run_id}_summary.json",
        Body=json.dumps(summary, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    return summary