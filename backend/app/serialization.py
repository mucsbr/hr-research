from datetime import date, datetime
from uuid import UUID

from sqlalchemy import inspect


def safe_value(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: safe_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_value(v) for v in value]
    return value


def row(obj, exclude=()):
    """Only for public domain entities; never serialize user/secrets with this helper."""
    return {
        item.key: safe_value(getattr(obj, item.key))
        for item in inspect(obj).mapper.column_attrs if item.key not in exclude
    }
