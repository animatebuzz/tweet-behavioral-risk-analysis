# Challenges & Solutions

A candid log of the interesting problems this project produced, and how each was solved — the genuinely tricky spots worth walking through.

---

## 1. Rate limiting without the classic failure modes

**Problem.** The obvious rate-limiting approaches "easily produce errors": a naive `sleep(0.1)` per tweet drifts (sleep + actual LLM latency compounds and the true rate falls), a counter+reset loop bursts at every reset, and sleeping blindly wastes time when tokens are already available.

**Solution.** A timestamp-based token bucket (`src/pipeline/rate_limiter.py`): `available = min(capacity, available + elapsed*refill)` computed from `time.monotonic()`, with the required wait derived arithmetically and slept once. With the strict default `capacity=1, refill=10`, at most one token exists at any instant, so **no one-second window can contain more than 10 request starts** — the guarantee is structural, not statistical. Clock and sleeper are injected so the property is unit-tested with a fake clock.

**Residual risk.** If `capacity` were raised above 1 the strict one-second ceiling would soften (bursts of up to `capacity` starts). The default stays at 1; configurability is an extra-credit knob, documented as such.

## 2. The float-resolution stall (real bug, found by testing)

**Symptom.** With the fake clock the acquire loop could run forever: a computed wait like `0.9999999999999998` rounded *up* to 1.0 at wall-clock resolution, so a token was mathematically due but never actually available — a livelock.

**Fix.** Floor every computed wait at `MIN_WAIT = 1e-4` seconds (never `0`), and add a regression test (`test_subresolution_waits_never_starve_the_loop`) with hand-picked float-pathological values.

**Lesson.** Timestamp-based rate pacing is precise only if you also bound the *implementation's* float behavior; the wall clock is a coarser instrument than the maths.

## 3. Enforcing a strict four-value enum on free-form LLM output

**Problem.** The contract demands `suicide_likelihood` be exactly `high risk | potentially likely | neutral | unlikely`. Language models naturally hedge ("somewhat likely", "unclear", "moderate risk"...) and wrap output in code fences. Any fifth value — including a benign "unclear" — violates the contract.

**Solution.** Three layers:

1. A rubric system prompt that defines the four levels and demands strict JSON (no prose, no markdown).
2. `extract_json()` tolerates code fences; `validate_contract()` hard-rejects anything outside the four strings.
3. One corrective retry feeds the raw failure back to the model; if it still fails, an **error record** is emitted with `suicide_likelihood` **absent** — the enum never acquires a fifth value through the back door.

**Residual risk.** A tweet that consistently fails classification surfaces as an error record rather than a guess. That is a considered trade: honest "couldn't classify" over a fabricated fifth class.

## 4. Error taxonomy: don't blame the model for infrastructure

**Problem.** In the first live run, all 120 classifications failed with `invalid_model_output` — yet local runs passed 100/100. The blame was wrong: the deployed Lambda was hitting **IAM `AccessDeniedException`** on every Bedrock call, and the evaluator's retry path had swallowed *all* exceptions and labeled them as model-output failures.

**Fix.** Split the taxonomy: contract violations → `invalid_model_output` (after the corrective retry); anything else (permissions, throttling, timeouts) → `inference_failed` + `error_details`. This went from "120 mysterious invalid outputs" to "here is the exact ARN you forgot", and it's now covered by tests.

**Lesson.** Never collapse two failure classes into one label — observability is a debugging instrument, and the error record schema is part of the contract.

## 5. Cross-region inference profiles need three IAM ARNs

**Problem (the big deployment bug).** The classifier uses the US cross-region inference profile `us.anthropic.claude-haiku-4-5-20251001-v1:0`. IAM denied every call:

- The role allowed `bedrock:InvokeModel` on `...:model/us.anthropic…-v1:0` — wrong namespace.
- Adding `...:inference-profile/us.anthropic…-v1:0` fixed the profile check, but the call then required permission on the *underlying* foundation model — `...:foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0` — **in a different region** (us-east-2, even though the stack lives in us-east-1).

**Fix.** Grant `bedrock:InvokeModel` on all three ARNs, with the foundation-model ARN using a **wildcard region** (`arn:aws:bedrock:*::foundation-model/…`) because US profiles can route to any US region.

**Lesson.** With inference profiles, the resource you *think* you're calling is not the resource IAM checks — read the denial message's ARN before adding buckets of permissions.

## 6. Event-driven plumbing surprises

Three small but consequential deployment bugs, fixed in `src/aws/deploy.py` / `lambda_sampler.py`:

- **S3 notification ordering.** `PutBucketNotificationConfiguration` fails with "Unable to validate the following destination configurations" unless the Lambda's `s3.amazonaws.com` invoke permission exists *first*. The deploy script now grants the permission before wiring the notification.
- **URL-encoded S3 keys.** S3 event notifications URL-encode object keys (`Suicide_Ideation_Dataset%28Twitter-based%29.csv`). `lambda_sampler` decodes with `urllib.parse.unquote_plus`. Failing to did this looks like `NoSuchKey` on a file that visibly exists — a classic trap.
- **Lambda update races.** Idempotent re-deploys tripped "An update is in progress". Lambda updates aren't atomic with the last write; the deploy script now retries code/config updates with backoff (bounded, 5 attempts).

## 7. Step Functions couldn't assume the Lambda role

**Symptom.** State machine executions failed with `States.TaskFailed: The principal states.amazonaws.com is not authorized to assume the provided role`.

**Cause.** One role served both the Lambdas and the state machine, but its trust policy named only `lambda.amazonaws.com`.

**Fix.** Trust both principals; the deploy script now also *updates* the trust policy on existing roles (self-healing on re-run), instead of only on creation.

## 8. Local vs. Lambda credential asymmetry

**Symptom.** The AWS CLI worked, but the deploy script (plain boto3) died with `MissingDependencyException: botocore[crt]`.

**Cause.** This machine's keys live in the AWS CLI v2 **login credential store** rather than a plain shared-credentials file; the CLI bundles CRT support, the Python SDK doesn't.

**Fix.** `pip install "botocore[crt]"` (now in `requirements.txt`). Also a neat reminder that "it works on my machine" can be an artifact of which credential *path* the code takes.

## 9. Offline validation is approximate by design

The dataset has only `Not Suicide post` (1,127) and `Potential Suicide post` (660) — **no confirmed high-risk class**. Any comparison of the LLM's `high risk`/`potentially likely` against the label treats the two-class label as a proxy for the four-level reality. This is documented, reported as approximate, and used only for sanity checks — never at inference.

---

## Honorable mentions

- **Byte-identical dataset.** `.gitattributes` (`data/** -text`) plus a SHA-256 verification step kept the input pristine through git and HTTP round-trips.
- **Cost visibility.** A $10 CloudWatch billing alarm plus per-run caps made the "$100 credits" constraint a monitored quantity rather than a hope.
- **Small-file robustness by construction.** An 11-tweet fixture with blank labels exercises the smallest realistic input; `min(config, rows)` keeps the pipeline honest for small files.