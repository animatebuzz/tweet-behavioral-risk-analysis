"""Model clients: Bedrock (real) and a deterministic mock for offline tests.

The client interface is intentionally narrow — ``generate(system, tweet) ->
text`` — so the evaluator is fully testable without an AWS account. The
``[Suicide]`` label never reaches the model: only the tweet text is sent.
"""

from __future__ import annotations

import json
import os
from typing import Optional, Protocol


class ModelClient(Protocol):
    def generate(self, system: str, tweet: str) -> str:
        """Return the raw model text for one tweet."""


class BedrockClient:
    """Thin wrapper around the Bedrock Converse API (Claude Haiku 4.5)."""

    def __init__(
        self,
        model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        region_name: Optional[str] = None,
    ) -> None:
        import boto3

        self.model_id = model_id
        self._client = boto3.client("bedrock-runtime", region_name=region_name or os.getenv("AWS_REGION"))

    def generate(self, system: str, tweet: str) -> str:
        response = self._client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": tweet}]}],
            inferenceConfig={"maxTokens": 512, "temperature": 0},
        )
        return response["output"]["message"]["content"][0]["text"]


class MockClient:
    """Deterministic fake used by ``--mock`` and CI tests. Never deployed."""

    def __init__(self, responses: Optional[list[str]] = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, tweet: str) -> str:
        self.calls.append((system, tweet))
        if self.responses:
            return self.responses.pop(0)
        return json.dumps(
            {
                "suicide_likelihood": "unlikely",
                "alert_of_risk": 0,
                "confidence": 0.99,
                "rationale": "mock response: no suicidal content.",
            }
        )