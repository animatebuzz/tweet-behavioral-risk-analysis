"""Shared constants for the tweet classification pipeline."""

MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MODEL_ID_REGIONAL = "anthropic.claude-haiku-4-5-20251001-v1:0"

SUICIDE_LIKELIHOOD_LEVELS = (
    "high risk",
    "potentially likely",
    "neutral",
    "unlikely",
)

ALERT_LEVELS = frozenset({"high risk", "potentially likely"})

MAX_TWEETS_PER_SECOND = 10
DEFAULT_SAMPLE_SIZE = 120
MIN_SAMPLE_SIZE = 100

ERROR_INVALID_MODEL_OUTPUT = "invalid_model_output"
ERROR_INFERENCE_FAILED = "inference_failed"

S3_RAW_PREFIX = "raw/"
S3_RESULTS_PREFIX = "results/"