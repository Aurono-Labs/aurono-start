# src/report_validator.py

import json
import os
from jsonschema import validate, ValidationError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR = os.path.join(BASE_DIR, "schemas")

def _load_schema(filename: str) -> dict:
    with open(os.path.join(SCHEMA_DIR, filename), "r", encoding="utf-8") as f:
        return json.load(f)


daily_schema = _load_schema("daily_report.schema.json")
weekly_schema = _load_schema("weekly_report.schema.json")


def validate_daily_report(report: dict) -> None:
    """
    Validates the daily report against its schema.
    Raises ValidationError if invalid.
    """
    validate(instance=report, schema=daily_schema)


def validate_weekly_report(report: dict) -> None:
    """
    Validates the weekly report against its schema.
    Raises ValidationError if invalid.
    """
    validate(instance=report, schema=weekly_schema)

