"""Point the bot's own code (src/) at the deployed table, for scripts run from a laptop."""
import os
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # so a script can import apply, store and the rest after this module


def use_live_table():
    """Set TABLE_NAME to the deployed table.

    The table is found from the CloudFormation stack (STACK_NAME, default
    nuffnuffbot), unless TABLE_NAME is already set.
    """
    # The AWS CLI reads AWS_REGION, but boto3 only reads AWS_DEFAULT_REGION.
    if os.environ.get("AWS_REGION"):
        os.environ.setdefault("AWS_DEFAULT_REGION", os.environ["AWS_REGION"])
    if "TABLE_NAME" not in os.environ:
        stack = os.environ.get("STACK_NAME", "nuffnuffbot")
        resource = boto3.client("cloudformation").describe_stack_resource(
            StackName=stack, LogicalResourceId="StockTable"
        )
        os.environ["TABLE_NAME"] = resource["StackResourceDetail"]["PhysicalResourceId"]
