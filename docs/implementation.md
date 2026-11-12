# Implementation Walkthrough

How the implementation satisfies each requirement, how the code is organized, and how it was verified end-to-end on AWS. Companion to the [design document](tweet-llm-pipeline-design.md).

---

## 1. Code map

```
src/pipeline/                    # core, AWS-free package (fully testable locally)
  constants.py                   # model IDs, 4-value enum, error codes, S3 prefixes
  rate_limiter.py                # TokenBucket: timestamp-based, clock/sleeper injectable
  prompts.py                     # SYSTEM_PROMPT (rubric, verbatim from design §Appendix A)
  model_client.py                # BedrockClient (boto3 converse) + MockClient (tests/--mock)
  evaluator.py                   # extract_json, validate_contract, derive_alert_of_risk, TweetEvaluator
  sampler.py                     # CSV parsing, load_tweets, random.sample without replacement
  validation.py                  # offline confusion matrix vs the [Suicide] label
  cli.py                         # local runner: --mock / real Bedrock / --validate
src/aws/                         # deployment layer
  deploy.py                      # idempotent deploy: bucket, IAM, 3 Lambdas, SFN, S3 trigger
  lambda_trigger.py              # S3 PUT on raw/ → sfn.start_execution
  lambda_sampler.py              # read CSV from S3, sample, write sampled/<run_id>.json
  lambda_classifier.py           # paced Bedrock classification → results/<run_id>.jsonl
  state_machine.asl.json         # Sample → Classify, bounded retries, Fail on error
  README.md                      # deployment runbook
tests/                           # 24 tests: limiter props, contract, sampling, evaluator
```

## 2. Requirements → implementation

### 2.1 Rates: the ≤ 10 tweets/second ceiling

**Approach:** a token bucket (strict default `capacity=1, refill=10/sec`), not a blind `sleep(0.1)` loop. Typical rate-limit attempts "easily produce errors" — they drift, burst on counter resets, or race when calls overlap.

`TokenBucket.acquire()` (`rate_limiter.py`):

- Tokens are computed from **timestamps** (`time.monotonic()`): `available = min(capacity, available + elapsed * refill_rate)`.
- Wait time is derived precisely: `wait = (tokens_needed - tokens_available) / refill_rate`, then slept once. No drift over a full run.
- With `C=1` at most one token exists at any instant, so request starts are spaced ≥ 1/R seconds apart — **no one-second window can contain more than 10 starts**, by construction.
- `MIN_WAIT = 1e-4` floors any computed wait so float resolution can never stall the loop (a real bug found during development — see challenges).
- Clock and sleeper are injectable; tests use a fake clock to assert the 10/sec bound as a property.

The rate is configurable via env/SSM (`RATE_LIMIT_CAPACITY`, `RATE_LIMIT_REFILL_PER_SECOND`) without code edits — the strict default stays the guaranteed ceiling.

### 2.2 The four-value enum, exactly

The model is asked for strict JSON:

```json
{"suicide_likelihood": "<level>", "alert_of_risk": 0|1, "confidence": <0-1>, "rationale": "<text>"}
```

`evaluator.py` then enforces the contract in code:

1. `extract_json` tolerates code fences and picks the first `{...}` object.
2. `validate_contract` rejects anything where `suicide_likelihood` is not one of the exact four strings (`high risk`, `potentially likely`, `neutral`, `unlikely`), or `confidence`/`rationale` are malformed. There is no fifth "unclear" value in the system — a level can never be anything else.
3. On a contract violation the model gets **one corrective retry** (the raw output is appended as a hint). Still invalid → an **error record** with `suicide_likelihood` absent.
4. Non-contract failures (permissions, throttling, timeouts) produce `"error": "inference_failed"` with `error_details` — infrastructure problems are never mislabeled as model-output problems.

### 2.3 `alert_of_risk` is derived, never trusted

`derive_alert_of_risk(level)` maps `{high risk, potentially likely} → 1`, `{neutral, unlikely} → 0`. The model's own `alert_of_risk` field is ignored; the code is the single source of truth (test: a model that "lies" gets corrected).

### 2.4 `[Suicide]` column — inference hygiene

- The sampler loads rows with the label attached (`label`), because sampling must happen over the full row set.
- The classifier **drops it before any model call** — the system prompt contains the rubric only; the user message contains the tweet text only.
- Offline `--validate` mode compares predicted risk (`high risk`/`potentially likely`) against the label and reports a confusion matrix — approximate by design, since the dataset has no confirmed high-risk class.

### 2.5 Sampling: random without replacement

`random.sample(pool, k)` guarantees distinct indices in one call — no replacement. `SAMPLE_SIZE` defaults to 120 (≥ 100 required); `min(config, rows)` makes an 11-row file process all 11. A `RANDOM_SEED` option makes runs reproducible for debugging.

### 2.6 Event-driven orchestration

- An S3 PUT event on `raw/` invokes the trigger Lambda, which calls `sfn.start_execution` with the event.
- Step Functions `tweet-classification-workflow` runs `Sample → Classify` with bounded retries and a `Fail` state that marks the run failed with a readable cause.
- Everything is pay-per-event: nothing is scheduled, nothing loops when idle.

### 2.7 Storage & immutability

- `raw/` holds the input byte-identical (`data/** -text` in `.gitattributes` prevents checkout-side EOL mangling).
- `sampled/<run_id>.json`, `results/<run_id>.jsonl`, `results/<run_id>_summary.json` are per-run; the input is never written back.

## 3. Deployment

```bash
python -m src.aws.deploy --bucket tweet-behavioral-risk-analysis-fall-2026 --region us-east-1
```

The script is **idempotent** and self-healing on re-run: it creates the bucket + prefixes, an IAM role with least-privilege policies, packages `src/pipeline` + the three handlers into one zip, creates or updates the Lambdas (python3.13), registers/updates the state machine, grants the S3→Lambda permission, and installs the bucket notification — in dependency order. See `src/aws/README.md` for the runbook; see `docs/challenges.md` for the deployment bugs this process discovered.

**IAM notes that matter:**

- The execution role's trust policy allows both `lambda.amazonaws.com` and `states.amazonaws.com` (Step Functions assumes the same role).
- Bedrock `InvokeModel` for a cross-region inference profile requires permissions on **three** resource ARNs: `model/<id>`, `inference-profile/<id>`, and the underlying `foundation-model/<regional-id>` — the last with a **wildcard region** (`arn:aws:bedrock:*::foundation-model/…`), because US profiles can route to any US region.

## 4. Verification evidence (live, 2026-08-13)

| Check | Result |
|---|---|
| Real CSV run (120 sampled from 1,787) | ✅ 120/120 classified, 0 errors |
| Level distribution | ✅ `high risk` 5 · `potentially likely` 20 · `neutral` 2 · `unlikely` 93 |
| Alert rate | ✅ 0.2083 (25/120) |
| Pacing | ✅ avg 1.76s between completions; zero 1-second windows > 10 starts |
| `alert_of_risk` ↔ level consistency | ✅ 0 mismatches across 120 records |
| Confidence field | ✅ all within [0, 1] (0.72–0.99) |
| Small fixture (11 tweets, blank labels) | ✅ 11/11 classified, 0 errors — same code path, no changes |
| Local mock + validation | ✅ 24/24 tests; mock run 120/120 with sensible offline confusion (TN 74, FN 46, acc 0.617) |

Full results live in `s3://tweet-behavioral-risk-analysis-fall-2026/results/` (runs `20260813-132306` and `20260813-132844`).

## 5. Error handling summary

| Failure | Handling |
|---|---|
| Invalid model JSON / out-of-contract | 1 corrective retry → `invalid_model_output` error record (enum stays 4-valued) |
| Bedrock permission / throttling / timeout | `inference_failed` error record with details (no misleading label) |
| Missing `Tweet` column / empty file | Sampler raises, state machine fails before any spend |
| Blank `Suicide` column (sample file) | Accepted; label unused |
| S3 read/write | SDK retries + SFN retry; trigger event is idempotent |
| IAM/billing runaway | Least-privilege role + billing alarm at $10 + hard caps |

## 6. Cost

A verified 120-tweet live run uses ≈ 13K tokens (≈ $0.01–0.05 with Haiku 4.5). All development/deployment runs to date total well under $0.20 against the $100 credit budget; CloudWatch alarm alerts at $10.