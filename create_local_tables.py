"""Create the app tables in DynamoDB (local or AWS).

Tables:
    ChatSessions  PK/SK  chat metadata + transcript (created by create_session_table)
    SsoUsers      PK/SK  one profile item per SSO user

Usage (local DynamoDB on port 8000):

    set DYNAMODB_ENDPOINT_URL=http://localhost:8000      (Windows cmd)
    $env:DYNAMODB_ENDPOINT_URL="http://localhost:8000"   (PowerShell)
    python create_local_tables.py

Against real AWS, leave DYNAMODB_ENDPOINT_URL unset.
"""

import os

# DynamoDB Local ignores credentials but botocore still requires some.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")

import boto3
from botocore.exceptions import ClientError

from create_session_table import create_session_table

EXPECTED_KEYS = {"PK": "HASH", "SK": "RANGE"}


def create_sso_table(
    table_name: str = "SsoUsers",
    region: str = "us-east-1",
    endpoint_url: str = "",
) -> None:
    resource = boto3.resource(
        "dynamodb", region_name=region, endpoint_url=endpoint_url or None
    )
    client = boto3.client(
        "dynamodb", region_name=region, endpoint_url=endpoint_url or None
    )

    try:
        existing = client.describe_table(TableName=table_name)["Table"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            print(f"AWS Error: {exc}")
            return
        existing = None

    if existing is not None:
        schema = {
            key["AttributeName"]: key["KeyType"] for key in existing["KeySchema"]
        }
        if schema == EXPECTED_KEYS:
            print(f"Table '{table_name}' already exists with the correct PK/SK schema.")
        else:
            print(
                f"WARNING: table '{table_name}' exists with key schema {schema}, "
                f"expected {EXPECTED_KEYS}."
            )
        return

    try:
        table = resource.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        print(f"Creating table {table_name} ...")
        table.meta.client.get_waiter("table_exists").wait(TableName=table_name)
        print(f"Table '{table_name}' created successfully.")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceInUseException":
            print("Table already exists.")
        else:
            print(f"AWS Error: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"Error creating table: {exc}")


def main() -> None:
    endpoint_url = os.environ.get("DYNAMODB_ENDPOINT_URL", "")
    region = os.environ.get("AWS_REGION", "us-east-1")
    sessions_table = os.environ.get("SESSIONS_TABLE", "ChatSessions")
    users_table = os.environ.get("SSO_USERS_TABLE", "SsoUsers")

    where = endpoint_url or f"AWS {region}"
    print(f"Creating tables in {where}")

    create_session_table(
        table_name=sessions_table,
        region=region,
        endpoint_url=endpoint_url,
    )
    create_sso_table(
        table_name=users_table,
        region=region,
        endpoint_url=endpoint_url,
    )

    print("\nDone. Tables:")
    client = boto3.client(
        "dynamodb", region_name=region, endpoint_url=endpoint_url or None
    )
    for name in client.list_tables().get("TableNames", []):
        print(f"  - {name}")


if __name__ == "__main__":
    main()
