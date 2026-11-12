"""Trigger Lambda (S3 PUT on raw/ -> Step Functions StartExecution).

Kept separate from the pipeline logic so the state machine is the single
entry point; this function only forwards the event.
"""

from __future__ import annotations

import json
import os

import boto3

sfn = boto3.client("stepfunctions")
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]


def handler(event: dict, context) -> dict:
    response = sfn.start_execution(stateMachineArn=STATE_MACHINE_ARN, input=json.dumps(event))
    return {"execution_arn": response["executionArn"]}