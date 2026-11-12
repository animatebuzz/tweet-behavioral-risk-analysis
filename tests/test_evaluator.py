"""Evaluator tests: JSON contract, retry-once, four-value enum, error records."""

from __future__ import annotations

import json

from src.pipeline.constants import ERROR_INFERENCE_FAILED, ERROR_INVALID_MODEL_OUTPUT
from src.pipeline.evaluator import (
    InvalidModelOutput,
    TweetEvaluator,
    derive_alert_of_risk,
    extract_json,
    validate_contract,
)
from src.pipeline.model_client import MockClient


def sample_record(level="high risk", confidence=0.9, rationale="x"):
    return {
        "suicide_likelihood": level,
        "alert_of_risk": 1,
        "confidence": confidence,
        "rationale": rationale,
    }


def test_extract_json_with_fences() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_rejects_non_json() -> None:
    try:
        extract_json("no json here")
    except InvalidModelOutput:
        return
    raise AssertionError("expected InvalidModelOutput")


def test_validate_contract_rejects_fifth_class() -> None:
    for bad in ("unclear", "maybe", None, 3):
        try:
            validate_contract({"suicide_likelihood": bad, "confidence": 0.5, "rationale": "x"})
        except InvalidModelOutput:
            continue
        raise AssertionError(f"expected rejection for {bad!r}")


def test_alert_mapping_exact() -> None:
    assert derive_alert_of_risk("high risk") == 1
    assert derive_alert_of_risk("potentially likely") == 1
    assert derive_alert_of_risk("neutral") == 0
    assert derive_alert_of_risk("unlikely") == 0


def test_evaluate_returns_result_record() -> None:
    client = MockClient([json.dumps(sample_record("potentially likely", 0.82))])
    record = TweetEvaluator(client).evaluate(7, "wish I could disappear")
    assert record["suicide_likelihood"] == "potentially likely"
    assert record["alert_of_risk"] == 1
    assert record["tweet_id"] == 7
    assert record["model"]
    assert "classified_at_utc" in record
    # the label column must never reach the model — tweet text is the only input
    assert all("Suicide" not in system for system, _ in client.calls)


def test_alert_is_derived_not_trusted_to_model() -> None:
    wrong = sample_record("neutral", 0.5, "x")
    wrong["alert_of_risk"] = 1  # model lies; code must override
    record = TweetEvaluator(MockClient([json.dumps(wrong)])).evaluate(1, "t")
    assert record["suicide_likelihood"] == "neutral"
    assert record["alert_of_risk"] == 0


def test_retry_once_then_error_record_without_fifth_class() -> None:
    client = MockClient(["garbage", "still not json"])
    record = TweetEvaluator(client, corrective_retries=1).evaluate(9, "t")
    assert client.responses == []  # both attempts consumed
    assert record["error"] == ERROR_INVALID_MODEL_OUTPUT
    assert "suicide_likelihood" not in record
    assert record["tweet_id"] == 9


def test_retry_exhaustion_labels_error_record_inference_failed() -> None:
    class InvalidThenThrowing:
        def generate(self, system: str, tweet: str) -> str:
            if not getattr(self, "called", False):
                self.called = True
                return "garbage"
            raise RuntimeError("ThrottlingException: rate exceeded")

    record = TweetEvaluator(InvalidThenThrowing()).evaluate(4, "t")
    assert record["error"] == ERROR_INFERENCE_FAILED
    assert "ThrottlingException" in record["error_details"]


def test_never_swallow_infrastructure_failures_as_invalid_output() -> None:
    class ThrowingClient:
        def generate(self, system: str, tweet: str) -> str:
            raise RuntimeError("AccessDeniedException: bedrock:InvokeModel denied")

    record = TweetEvaluator(ThrowingClient()).evaluate(5, "t")
    assert record["error"] == ERROR_INFERENCE_FAILED
    assert "AccessDeniedException" in record["error_details"]
    assert "suicide_likelihood" not in record


def test_corrective_retry_recovers_on_second_attempt() -> None:
    client = MockClient(["garbage", json.dumps(sample_record("unlikely", 0.7, "ok"))])
    record = TweetEvaluator(client).evaluate(3, "t")
    assert record["suicide_likelihood"] == "unlikely"
    assert record["alert_of_risk"] == 0
    assert "error" not in record