"""Deploy the pipeline to AWS: S3 bucket, Lambda roles/functions, Step
Functions state machine, and the S3 PUT -> SFN trigger wiring.

    python -m src.aws.deploy --bucket tweet-behavioral-risk-analysis-fall-2026

Requires AWS credentials on the CLI (or AWS_PROFILE). Idempotent: re-running
updates lambdas and the state machine. The pipeline code is packaged from
``src/`` automatically; the raw CSV is then uploaded with:

    aws s3 cp data/Suicide_Ideation_Dataset(Twitter-based).csv s3://<bucket>/raw/

Resources created:
- S3 bucket (raw/, sampled/, results/)
- IAM role with least-privilege policy scoped to that bucket, the Bedrock
  model, and the state machine
- 3 Lambda functions (trigger, sampler, classifier)
- 1 Step Functions state machine + S3 event notification to the trigger
"""

from __future__ import annotations

import argparse
import json
import tempfile
import zipfile
from pathlib import Path

import boto3

REPO_ROOT = Path(__file__).resolve().parents[2]
ASL_PATH = REPO_ROOT / "src" / "aws" / "state_machine.asl.json"
MODEL_ARN_PATTERN = "arn:aws:bedrock:{region}:{account}:model/{model_id}"
FOUNDATION_MODEL_ARN_PATTERN = "arn:aws:bedrock:*::foundation-model/{regional_model_id}"

ASSUME_ROLE_POLICY = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": ["lambda.amazonaws.com", "states.amazonaws.com"]},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)

LAMBDA_NAMES = {
    "trigger": "tweet-classifier-trigger",
    "sampler": "tweet-classifier-sampler",
    "classifier": "tweet-classifier-classify",
}
ROLE_NAME = "tweet-classifier-lambda-role"
SM_NAME = "tweet-classification-workflow"


def _session(args) -> tuple:
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    return (
        session.client("s3", region_name=args.region),
        session.client("iam", region_name=args.region),
        session.client("lambda", region_name=args.region),
        session.client("stepfunctions", region_name=args.region),
        session.client("sts", region_name=args.region),
    )


def _ensure_bucket(s3, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:
        s3.create_bucket(Bucket=bucket)
    for prefix in (b"raw/", b"sampled/", b"results/"):
        s3.put_object(Bucket=bucket, Key=prefix.decode(), Body=b"")


def _ensure_role(iam, bucket: str, region: str, account: str, model_id: str) -> str:
    try:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=ASSUME_ROLE_POLICY,
            Description="Least-privilege role for the tweet classification pipeline lambdas",
        )
    except iam.exceptions.EntityAlreadyExistsException:
        role = iam.get_role(RoleName=ROLE_NAME)
        iam.update_assume_role_policy(RoleName=ROLE_NAME, PolicyDocument=ASSUME_ROLE_POLICY)
    role_arn = role["Role"]["Arn"]

    iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")

    inline = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{bucket}",
                    f"arn:aws:s3:::{bucket}/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": [
                    MODEL_ARN_PATTERN.format(region=region, account=account, model_id=model_id),
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}",
                    FOUNDATION_MODEL_ARN_PATTERN.format(
                        region=region, regional_model_id=model_id.removeprefix("us.")
                    ),
                ],
            },
            {
                "Effect": "Allow",
                "Action": "states:StartExecution",
                "Resource": f"arn:aws:states:{region}:{account}:stateMachine:{SM_NAME}",
            },
            {
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Resource": [
                    f"arn:aws:lambda:{region}:{account}:function:{name}" for name in LAMBDA_NAMES.values()
                ],
            },
            {
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": role_arn,
            },
        ],
    }
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="tweet-classifier-policy", PolicyDocument=json.dumps(inline))
    return role_arn


def _package_code() -> str:
    """Zip src/pipeline + the aws handlers into one deployable bundle."""
    bundle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(bundle.name, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in (REPO_ROOT / "src" / "pipeline").rglob("*.py"):
            archive.write(path, arcname=f"pipeline/{path.name}")
        for name in ("lambda_trigger.py", "lambda_sampler.py", "lambda_classifier.py"):
            archive.write(REPO_ROOT / "src" / "aws" / name, arcname=name)
    bundle.close()
    return bundle.name


def _deploy_lambda(lambda_client, name: str, zip_path: str, role_arn: str, env: dict, timeout: int) -> None:
    handler_names = {
        "tweet-classifier-trigger": "lambda_trigger.handler",
        "tweet-classifier-sampler": "lambda_sampler.handler",
        "tweet-classifier-classify": "lambda_classifier.handler",
    }
    kwargs = {
        "FunctionName": name,
        "Runtime": "python3.13",
        "Role": role_arn,
        "Handler": handler_names[name],
        "Code": {"ZipFile": open(zip_path, "rb").read()},
        "Environment": {"Variables": env},
        "Timeout": timeout,
        "MemorySize": 256,
        "Publish": True,
    }
    try:
        lambda_client.create_function(**kwargs)
    except lambda_client.exceptions.ResourceConflictException:
        import time

        for attempt in range(5):
            try:
                lambda_client.update_function_code(
                    FunctionName=name, ZipFile=open(zip_path, "rb").read(), Publish=True
                )
                break
            except lambda_client.exceptions.ResourceConflictException:
                if attempt == 4:
                    raise
                time.sleep(5)
        for attempt in range(5):
            try:
                lambda_client.update_function_configuration(
                    FunctionName=name,
                    Role=role_arn,
                    Environment={"Variables": env},
                    Timeout=timeout,
                    MemorySize=256,
                )
                break
            except lambda_client.exceptions.ResourceConflictException:
                if attempt == 4:
                    raise
                time.sleep(5)


def deploy(args) -> None:
    s3, iam, lambda_client, sfn, sts = _session(args)
    account = sts.get_caller_identity()["Account"]
    model_id = args.model_id

    _ensure_bucket(s3, args.bucket)
    role_arn = _ensure_role(iam, args.bucket, args.region, account, model_id)
    zip_path = _package_code()

    _deploy_lambda(
        lambda_client, LAMBDA_NAMES["sampler"], zip_path, role_arn,
        {"S3_BUCKET": args.bucket, "SAMPLE_SIZE": str(args.sample_size)}, timeout=60,
    )
    _deploy_lambda(
        lambda_client, LAMBDA_NAMES["classifier"], zip_path, role_arn,
        {
            "S3_BUCKET": args.bucket,
            "RATE_LIMIT_CAPACITY": str(args.capacity),
            "RATE_LIMIT_REFILL_PER_SECOND": str(args.rate),
            "BEDROCK_MODEL_ID": model_id,
        },
        timeout=900,
    )

    asl = ASL_PATH.read_text(encoding="utf-8")
    asl = asl.replace("${REGION}", args.region).replace("${ACCOUNT_ID}", account)
    try:
        sfn.create_state_machine(name=SM_NAME, definition=asl, roleArn=role_arn)
    except sfn.exceptions.StateMachineAlreadyExists:
        sm = sfn.describe_state_machine(stateMachineArn=f"arn:aws:states:{args.region}:{account}:stateMachine:{SM_NAME}")
        sfn.update_state_machine(stateMachineArn=sm["stateMachineArn"], definition=asl)
    sm_arn = f"arn:aws:states:{args.region}:{account}:stateMachine:{SM_NAME}"

    _deploy_lambda(
        lambda_client, LAMBDA_NAMES["trigger"], zip_path, role_arn,
        {"STATE_MACHINE_ARN": sm_arn}, timeout=30,
    )

    try:
        lambda_client.add_permission(
            FunctionName=LAMBDA_NAMES["trigger"],
            StatementId="s3-invoke",
            Action="lambda:InvokeFunction",
            Principal="s3.amazonaws.com",
            SourceArn=f"arn:aws:s3:::{args.bucket}",
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass

    s3.put_bucket_notification_configuration(
        Bucket=args.bucket,
        NotificationConfiguration={
            "LambdaFunctionConfigurations": [
                {
                    "LambdaFunctionArn": f"arn:aws:lambda:{args.region}:{account}:function:{LAMBDA_NAMES['trigger']}",
                    "Events": ["s3:ObjectCreated:*"],
                    "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "raw/"}]}},
                }
            ]
        },
    )

    print(f"deployed. upload a CSV with:  aws s3 cp <file.csv> s3://{args.bucket}/raw/")
    print(f"state machine: {sm_arn}")
    print(f"results land in s3://{args.bucket}/results/<run_id>.jsonl")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--model-id", default="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    parser.add_argument("--sample-size", type=int, default=120)
    parser.add_argument("--rate", type=float, default=10.0)
    parser.add_argument("--capacity", type=float, default=1.0)
    return parser


if __name__ == "__main__":
    deploy(build_parser().parse_args())