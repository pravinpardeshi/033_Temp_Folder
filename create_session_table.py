"""
Create the ChatSessions DynamoDB table for session metadata.

Schema must match SessionRepository in session_memory_store.py:

    PK = USER#<actor_id>                    (HASH,  string)
    SK = SESSION#<session_id>               (RANGE, string)  session metadata
    SK = MESSAGE#<session_id>#<sequence>    (RANGE, string)  transcript turns

Session metadata attributes:
    title, created_at, updated_at, last_message, agent, message_count, ttl

Transcript turn attributes:
    session_id, role, content, agent, created_at, ttl

Table name comes from SESSIONS_TABLE (default: ChatSessions).
"""

import boto3
from botocore.exceptions import ClientError

EXPECTED_KEYS = {"PK": "HASH", "SK": "RANGE"}


def create_session_table(table_name: str = "ChatSessions", region: str = "us-east-1") -> None:
    resource = boto3.resource("dynamodb", region_name=region)
    client = boto3.client("dynamodb", region_name=region)

    # Idempotent: verify an existing table has the schema the app expects.
    try:
        existing = client.describe_table(TableName=table_name)["Table"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            print(f"AWS Error: {exc}")
            return
        existing = None

    if existing is not None:
        schema = {
            key["AttributeName"]: key["KeyType"]
            for key in existing["KeySchema"]
        }
        if schema == EXPECTED_KEYS:
            print(f"Table '{table_name}' already exists with the correct PK/SK schema.")
        else:
            print(
                f"WARNING: table '{table_name}' exists but has key schema {schema}, "
                f"expected {EXPECTED_KEYS}. The app writes PK/SK keys and will fail "
                f"against this table. Delete it with:\n"
                f"  aws dynamodb delete-table --table-name {table_name} --region {region}\n"
                f"or point SESSIONS_TABLE at a different table name."
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
            return
    except Exception as exc:
        print(f"Error creating table: {exc}")
        return

    try:
        client.update_time_to_live(
            TableName=table_name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
        )
        print("TTL enabled on attribute 'ttl'.")
    except ClientError as exc:
        print(f"TTL enable skipped: {exc}")


if __name__ == "__main__":
    create_session_table()
