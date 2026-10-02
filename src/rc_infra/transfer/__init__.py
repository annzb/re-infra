"""Manual, operator-run data transfer between DynamoDB tables and between S3 buckets.

Never invoked by `rc-infra` or by any workflow: tests/test_architecture.py enforces both.
This is the only package allowed to know about older generations of the infrastructure.
"""
