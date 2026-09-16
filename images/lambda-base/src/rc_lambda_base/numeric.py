"""Recursive float <-> Decimal conversion for DynamoDB reads and writes."""

from decimal import Decimal


def decimal_to_float(obj):
    """Recursively convert Decimal values to float."""
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: decimal_to_float(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decimal_to_float(item) for item in obj]
    return obj


def float_to_decimal(obj):
    """Recursively convert float values to Decimal for DynamoDB writes."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: float_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [float_to_decimal(item) for item in obj]
    return obj
