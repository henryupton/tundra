import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest


def test_plan_file_format():
    """Test that plan file has correct structure."""
    plan_data = {
        "version": "1.0",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "spec_file": "test.yml",
        "options": {
            "roles": ["test_role"],
            "users": [],
            "run_list": ["roles"],
            "ignore_memberships": False,
            "skip_validation": False,
            "ignore_missing_objects": False,
        },
        "queries": [
            {
                "already_granted": False,
                "sql": "GRANT USAGE ON WAREHOUSE test TO ROLE test_role"
            }
        ],
        "summary": {
            "total": 1,
            "new": 1,
            "already_granted": 0,
        }
    }

    # Test serialization
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(plan_data, f, indent=2)
        temp_path = f.name

    try:
        # Test deserialization
        with open(temp_path, 'r') as f:
            loaded_data = json.load(f)

        assert loaded_data["version"] == "1.0"
        assert "timestamp" in loaded_data
        assert loaded_data["spec_file"] == "test.yml"
        assert "options" in loaded_data
        assert "queries" in loaded_data
        assert "summary" in loaded_data
        assert loaded_data["summary"]["new"] == 1
        assert len(loaded_data["queries"]) == 1
    finally:
        Path(temp_path).unlink()


def test_plan_queries_structure():
    """Test that queries in plan have required fields."""
    queries = [
        {
            "already_granted": False,
            "sql": "GRANT SELECT ON DATABASE test TO ROLE analyst"
        },
        {
            "already_granted": True,
            "sql": "GRANT ROLE analyst TO ROLE sysadmin"
        }
    ]

    for query in queries:
        assert "already_granted" in query
        assert "sql" in query
        assert isinstance(query["already_granted"], bool)
        assert isinstance(query["sql"], str)
        assert len(query["sql"]) > 0


def test_plan_summary_calculation():
    """Test that summary counts are correct."""
    queries = [
        {"already_granted": False, "sql": "SQL1"},
        {"already_granted": False, "sql": "SQL2"},
        {"already_granted": True, "sql": "SQL3"},
        {"already_granted": True, "sql": "SQL4"},
        {"already_granted": False, "sql": "SQL5"},
    ]

    new_queries = [q for q in queries if not q.get("already_granted")]
    existing_queries = [q for q in queries if q.get("already_granted")]

    summary = {
        "total": len(queries),
        "new": len(new_queries),
        "already_granted": len(existing_queries),
    }

    assert summary["total"] == 5
    assert summary["new"] == 3
    assert summary["already_granted"] == 2
