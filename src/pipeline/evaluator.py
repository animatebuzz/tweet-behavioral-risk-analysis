"""Single-tweet classification enforcing the four-value enum contract.

The model output is parsed and validated against the required
enum; ``alert_of_risk`` is derived deterministically in code from
``suicide_likelihood`` (single source of truth, never trusted to the model).
If the output is still invalid after one corrective retry, an **error record**
is produced with ``suicide_likelihood`` absent — the enum stays strictly
four-valued.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

from .constants import (
    ALERT_LEVELS,
    ERROR_INFERENCE_FAILED,
    ERROR_INVALID_MODEL_OUTPUT,
    MODEL_ID,
    SUICIDE_LIKELIHOOD_LEVELS,
)
from .model_client import ModelClient
from .prompts import CORRECTIVE_HINT_SUFFIX, SYSTEM_PROMPT


class InvalidModelOutput(ValueError):
    """Model output could not be parsed or failed contract validation."""


def extract_json(text: str) -> dict:
    """Parse a JSON object from model text, tolerating code fences."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match is None:
        raise InvalidModelOutput("no JSON object found in model output")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise InvalidModelOutput(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidModelOutput("model output is not a JSON object")
    return data


def validate_contract(data: dict) -> dict:
    """Validate the parsed output against the enum contract."""
    level = data.get("suicide_likelihood")
    if level not in SUICIDE_LIKELIHOOD_LEVELS:
        raise InvalidModelOutput(
            f"suicide_likelihood must be one of {SUICIDE_LIKELIHOOD_LEVELS}, got {level!r}"
        )
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        raise InvalidModelOutput(f"confidence must be a number in [0, 1], got {confidence!r}")
    rationale = data.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise InvalidModelOutput("rationale must be a non-empty string")
    return {"suicide_likelihood": level, "confidence": float(confidence), "rationale": rationale.strip()}


def derive_alert_of_risk(level: str) -> int:
    """1 for high-risk/potentially-likely, 0 otherwise (§3, §4)."""
    return 1 if level in ALERT_LEVELS else 0


class TweetEvaluator:
    """Rate-aware classifier for one tweet."""

    def __init__(
        self,
        client: ModelClient,
        system_prompt: str = SYSTEM_PROMPT,
        model_id: str = MODEL_ID,
        corrective_retries: int = 1,
    ) -> None:
        self.client = client
        self.system_prompt = system_prompt
        self.model_id = model_id
        self.corrective_retries = corrective_retries

    def _attempt(self, system: str, tweet: str) -> dict:
        raw = self.client.generate(system, tweet)
        return validate_contract(extract_json(raw))

    def evaluate(self, tweet_id: int, tweet: str) -> dict:
        """Return a result record or an error record (never a fifth class)."""
        base = {
            "tweet_id": tweet_id,
            "tweet": tweet,
            "model": self.model_id,
            "classified_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        try:
            data = self._attempt(self.system_prompt, tweet)
        except InvalidModelOutput as first_exc:
            hint = self.system_prompt + CORRECTIVE_HINT_SUFFIX.format(
                raw=_safe_excerpt(first_exc)
            )
            try:
                data = self._attempt(hint, tweet)
            except InvalidModelOutput:
                base.pop("tweet")
                base["error"] = ERROR_INVALID_MODEL_OUTPUT
                return base
            except Exception as exc:
                base.pop("tweet")
                base["error"] = ERROR_INFERENCE_FAILED
                base["error_details"] = _safe_excerpt(exc)
                return base
        except Exception as exc:
            base.pop("tweet")
            base["error"] = ERROR_INFERENCE_FAILED
            base["error_details"] = _safe_excerpt(exc)
            return base
        return {
            **base,
            "suicide_likelihood": data["suicide_likelihood"],
            "alert_of_risk": derive_alert_of_risk(data["suicide_likelihood"]),
            "confidence": data["confidence"],
            "rationale": data["rationale"],
        }


def _safe_excerpt(exc: Exception) -> str:
    text = str(exc)
    return text[:500] if text else "unknown error"