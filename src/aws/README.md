# AWS Deployment

Event-driven deployment of the pipeline: S3 PUT -> Step Functions -> 2 Lambdas -> Bedrock.

## Prerequisites

- AWS free account created and AWS CLI installed + configured (`aws configure`)
- Bedrock model access approved for `us.anthropic.claude-haiku-4-5-20251001-v1:0`
  (and its regional equivalent `anthropic.claude-haiku-4-5-20251001-v1:0`)

## Deploy

```powershell
python -m src.aws.deploy --bucket tweet-behavioral-risk-analysis-fall-2026 --region us-east-1
```

Idempotent: re-running updates Lambda code/config and the state machine. The
script creates:

| Resource | Name | Purpose |
|---|---|---|
| S3 bucket | `tweet-behavioral-risk-analysis-fall-2026` | `raw/` (byte-identical input), `sampled/`, `results/` |
| IAM role | `tweet-classifier-lambda-role` | Least-privilege: bucket objects, Bedrock invoke, SFN + Lambda invoke, PassRole |
| Lambda | `tweet-classifier-trigger` | S3 PUT on `raw/` -> `StartExecution` |
| Lambda | `tweet-classifier-sampler` | CSV parse + `random.sample` without replacement |
| Lambda | `tweet-classifier-classify` | Token-bucket (C=1, R=10) paced Bedrock classification |
| Step Functions | `tweet-classification-workflow` | `Sample` -> `Classify`, bounded retries, Fail on error |

## Run

```powershell
aws s3 cp data/sample_tweets.csv s3://tweet-behavioral-risk-analysis-fall-2026/raw/
# or any other CSV with a Tweet column:
aws s3 cp path/to/your_tweets.csv s3://tweet-behavioral-risk-analysis-fall-2026/raw/

# watch the execution
aws stepfunctions list-executions --state-machine-arn <arn> --query 'executions[0]'

# results
aws s3 ls s3://tweet-behavioral-risk-analysis-fall-2026/results/
aws s3 cp s3://tweet-behavioral-risk-analysis-fall-2026/results/<run_id>.jsonl .
```

## Local, without AWS (mock)

```powershell
python -m src.pipeline.cli --csv data/sample_tweets.csv --mock
python -m src.pipeline.cli --csv path/to/your_tweets.csv --sample-size 120 --seed 7 --validate
```

## Notes

- Rate limit env vars on the classifier (`RATE_LIMIT_CAPACITY=1`,
  `RATE_LIMIT_REFILL_PER_SECOND=10`) are the strict configuration; changing
  them adjusts the max 10/sec ceiling (optional extension).
- The `[Suicide]` label is never sent to the model; only the tweet text.
- Lambda packaging: `src/pipeline` + the three handlers are zipped together
  (`python3.13` runtime).