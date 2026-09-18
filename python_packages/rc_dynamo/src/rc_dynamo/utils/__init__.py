"""Application-independent helpers shared by the framework.

``settings`` parses the environment, ``aws`` builds cached boto3 clients from it,
and ``numeric`` converts between DynamoDB ``Decimal`` values and Python floats.
"""
