"""Sampler Lambda (Step Functions state: Sample).

Reads the uploaded CSV from S3 ``raw/``, validates the schema, draws a
random sample without replacement, and writes the sample to
``sampled/<run_id>.json`` for the Classifier state. The raw object is never
modified. The ``[Suicide]`` label stays in the sample payload for offline
validation, but is dropped before any Bedrock call downstream.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3

from pipeline.sampler import load_tweets_text, sample_rows

s3 = boto3.client("s3")


def handler(event: dict, context) -> dict:
    bucket = os.environ["S3_BUCKET"]
    key = unquote_plus(event["Records"][0]["s3"]["object"]["key"])
    if not key.startswith("raw/"):
        raise ValueError(f"unexpected object key (expected raw/ prefix): {key}")

    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8-sig")
    rows = load_tweets_text(body)
    size = int(os.environ.get("SAMPLE_SIZE", "120"))
    sampled = sample_rows(rows, size)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    sample_key = f"sampled/{run_id}.json"
    payload = {
        "run_id": run_id,
        "source": key,
        "sample": [{"tweet_id": r["tweet_id"], "tweet": r["tweet"], "label": r["label"]} for r in sampled],
    }
    s3.put_object(Bucket=bucket, Key=sample_key, Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    return {
        "run_id": run_id,
        "source": key,
        "sample_key": sample_key,
        "sampled_count": len(sampled),
    }