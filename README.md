# tweet-behavioral-risk-analysis

Real-time suicide-risk classification pipeline over tweets, built with **Python + AWS Bedrock (Claude Haiku 4.5) + Step Functions + Lambda + S3** as an AI-engineering portfolio project (Fall 2026).

The pipeline reads a static CSV of tweets as if they were arriving in real time, samples at least 100 tweets randomly (without replacement), and classifies each one via an LLM into a four-value `suicide_likelihood` scale plus a deterministic binary `alert_of_risk` flag — all while respecting a hard processing ceiling of **10 tweets/second** on fully event-driven AWS services.

> **Note:** this is a screening signal for engineering evaluation, not a medical product. Nothing here diagnoses anyone.

---

## Status

Complete — architecture designed, implemented, deployed and verified live on AWS, and documented.

---

## How it works

```
tweets.csv ──Upload──▶ S3 (raw/) ──PUT event──▶ Trigger Lambda
                                                      │ start execution
                                                      ▼
                                              Step Functions
                                              ┌──────────┐
                                              │  Sample  │  random.sample() without replacement
                                              └────┬─────┘
                                                   │ sampled tweets
                                                   ▼
                                              ┌──────────┐
                                              │ Classify │  token-bucket ≤ 10 tps
                                              └────┬─────┘  ──▶ Amazon Bedrock (Claude Haiku 4.5)
                                                   │
                                                   ▼
                                        S3 (results/) — JSONL + run summary
```

1. **Ingest** — upload any CSV with a `Tweet` column to `s3://<bucket>/raw/`. The content is stored byte-identical and never modified.
2. **Trigger** — the S3 PUT event starts the Step Functions state machine (event-driven by design).
3. **Sample** — the Sampler Lambda validates the schema and draws `n = min(config, rows)` tweets with `random.sample(..., k=n)` — random, **without replacement**. Default `n = 120` (minimum: 100). An 11-row file therefore processes all 11.
4. **Classify** — the Classifier Lambda paces itself with a **timestamp-based token bucket** (default capacity 1, refill 10/sec): each call waits until a token is available, so **no one-second window ever exceeds 10 request starts**. Each tweet is sent to Bedrock with the system prompt and the tweet text only — the dataset's `[Suicide]` label column is **never** used at inference. A strict four-value contract is enforced on the model's JSON output; `alert_of_risk` is derived in code, never trusted to the model.
5. **Store** — one JSONL result record per tweet, plus a run summary (counts per class, alert rate).
6. **Observe** — every step logs to CloudWatch; a billing alarm at $10 guards the budget.

## Output

Per tweet, a record in `s3://<bucket>/results/<run_id>.jsonl`:

```json
{
  "tweet_id": 1234,
  "tweet": "<original text, unchanged>",
  "suicide_likelihood": "potentially likely",
  "alert_of_risk": 1,
  "confidence": 0.82,
  "rationale": "short reason",
  "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
  "classified_at_utc": "2026-08-13T13:23:06Z"
}
```

| Column | Values |
|---|---|
| `suicide_likelihood` | `high risk` · `potentially likely` · `neutral` · `unlikely` (exactly these four — enforced in code) |
| `alert_of_risk` | `1` if the level is `high risk`/`potentially likely`, else `0` (deterministic, derived in code) |

If the model output is still invalid after one corrective retry, an error record is written (`"error": "invalid_model_output"`) with `suicide_likelihood` **absent** — the four-value enum never grows a fifth value. Infrastructure failures (permissions, throttling) are recorded distinctly as `"error": "inference_failed"` with details.

### Verified live run (2026-08-13)

Real dataset, 120 tweets sampled from 1,787, classified through Bedrock on AWS:

- **120/120 classified, 0 errors**
- Levels: `high risk` 5 · `potentially likely` 20 · `neutral` 2 · `unlikely` 93 — `alert_rate` 0.2083
- Pacing verified: zero 1-second windows exceeded 10 request starts
- 11-tweet sample fixture: 11/11 classified, 0 errors, with zero code changes

---

## Quickstart

### 1. Local run without AWS (mock model)

```bash
pip install -r requirements.txt

# Mock classification over the sample fixture (11 tweets)
python -m src.pipeline.cli --csv data/sample_tweets.csv --mock

# Any CSV with a Tweet column works; --validate compares against a Suicide label column
python -m src.pipeline.cli --csv path/to/your_tweets.csv --sample-size 120 --seed 7 --validate
```

Results land in `out/results.jsonl` + `out/summary.json` (gitignored).

> **Dataset note:** the full tweet dataset used for the live run is not redistributed here. The repo includes an 11-tweet sample fixture with blank labels so the pipeline can be exercised end to end. Supply your own CSV (columns `Tweet` and optionally `Suicide`) to run at scale.

### 2. Live run on AWS

Prerequisites: AWS credentials configured (`aws configure`), Bedrock model access approved for `us.anthropic.claude-haiku-4-5-20251001-v1:0`.

```bash
# Deploy (idempotent — safe to re-run)
python -m src.aws.deploy --bucket tweet-behavioral-risk-analysis-fall-2026 --region us-east-1

# Trigger a run by uploading a CSV
aws s3 cp path/to/your_tweets.csv s3://tweet-behavioral-risk-analysis-fall-2026/raw/

# Results
aws s3 ls s3://tweet-behavioral-risk-analysis-fall-2026/results/
aws s3 cp s3://tweet-behavioral-risk-analysis-fall-2026/results/<run_id>.jsonl .
```

The included file `data/sample_tweets.csv` (11 tweets, blank target) runs through the same path with **no code changes**. Any other CSV with the same `Tweet` column is a drop-in replacement — pipeline behavior is identical.

---

## Repository layout

```
README.md                     # this file
LICENSE                       # MIT
.gitignore                    # .env, config/.env, caches, secrets
.gitattributes                # data/** -text (byte-preserves CSV files)
.github/workflows/tests.yml   # CI: pytest on push
config/
  .env.example                # documented env keys (real .env is gitignored)
data/
  sample_tweets.csv           # 11-tweet sample fixture (blank labels)
docs/
  tweet-llm-pipeline-design.md  # architecture & design decisions
  implementation.md             # walkthrough: requirements → code → verification
  challenges.md                 # challenges encountered and how they were solved
src/
  pipeline/                     # core package: rate limiter, evaluator, prompts,
                                #   sampler, model clients, CLI, validation
  aws/                          # deploy script, Lambda handlers, state machine,
                                #   deployment runbook (README.md)
tests/                          # 24 unit tests (pytest)
out/                            # local CLI output (gitignored)
```

## Cost profile

- Haiku 4.5 pricing: a full 120-tweet run costs roughly **$0.01–0.05**; the whole project lands far below the $50 target.
- Cost controls: CloudWatch **billing alarm at $10**, `MAX_TWEETS`-style caps, bounded retries, pay-per-event architecture (no idle spend).

## Tests

```bash
python -m pytest tests -q      # 24 passing
```

Coverage highlights: token-bucket ≤10-start windows (property tests with a fake clock), float-resolution starvation regression, exact `alert_of_risk` mapping, rejection of any fifth class, retry-then-error-record, `inference_failed` error taxonomy, sampling without replacement, seed reproducibility, offline confusion math.

## Requirements → design

| Requirement | Response |
|---|---|
| Real-time classification pipeline | S3 PUT event → Step Functions → Sampler → Classifier, tweets processed "as they arrive" |
| `suicide_likelihood` with exactly 4 values | Prompt rubric + post-hoc enum validator (`validate_contract`) — invalid output is retried once, then an error record (never a 5th class) |
| `alert_of_risk` binary | Derived deterministically in code from the level |
| `[Suicide]` column not used at inference | Column stays out of every prompt; used only by offline `--validate` |
| ≤ 10 tweets/sec | Timestamp-based token bucket (C=1, R=10) — precise, drift-free, no blind sleeps |
| Sample ≥ 100, without replacement | `random.sample(pool, k=n)`, default 120 |
| System instructions in prompt | Full system prompt with evaluation rubric (verbatim in `src/pipeline/prompts.py`) |
| Python + Bedrock + event-driven + storage | 3.13 · Claude Haiku 4.5 via US cross-region inference profile · Step Functions + Lambda · S3 |
| Repo structure, README visible, config/.env gitignored, `src/` | As shown above |
| Small CSV (11 tweets, blank target) | File-agnostic pipeline; blank target treated as absent; fixture pre-tested 11/11 |
| Cost < $50 | ~0.01¢/tweet; billing alarm at $10 |

*Full design rationale, rate-limiter proof, and error-handling contracts: `docs/tweet-llm-pipeline-design.md`.*

## License

MIT — see [LICENSE](LICENSE).