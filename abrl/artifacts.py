import json
import os

import boto3
from botocore.config import Config

BUCKET = "abrl-reports"


def client():
    endpoint = os.getenv("ABRL_ARTIFACT_URL")
    password = os.getenv("ABRL_ARTIFACT_PASSWORD")
    if not endpoint or not password:
        return None
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="abrl-artifacts",
        aws_secret_access_key=password,
        config=Config(
            connect_timeout=3,
            read_timeout=4,
            retries={"total_max_attempts": 1},
            s3={"addressing_style": "path"},
        ),
    )


def archive(data: dict) -> dict:
    s3 = client()
    if s3 is None:
        raise RuntimeError("Report storage is not configured.")
    safe = {k: v for k, v in data.items() if k not in ("snapshot", "trials")}
    safe["trials"] = [
        {k: trial[k] for k in ("id", "ordinal", "status", "outcome", "elapsed_ms", "attempts")}
        for trial in data["trials"]
    ]
    key = f"{data['mode']}/{data['run_id']}/{data['config_sha256']}.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(safe, indent=2).encode(),
        ContentType="application/json",
        CacheControl="no-store",
    )
    return {"bucket": BUCKET, "key": key, "mode": data["mode"]}
