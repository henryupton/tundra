# How names in snowflake.core's REST models become ObjectNames. Each shape a dev probe
# recorded is here, with the name SHOW GRANTS gives the same grant: REST quotes a part
# only when it isn't a plain identifier, keywords included (AGGREGATE comes back bare),
# and spells a future grant's name as a quoted placeholder.

import pytest
from snowflake.core.role import Securable

from tundra.identifiers import (
    InvalidNameError,
    ObjectName,
    api_granted_name,
    canonical_from_api,
    granted_name,
)
from tundra.snowflake_connector import SnowflakeConnector


def securable(**parts) -> Securable:
    if "schema" in parts:
        parts["var_schema"] = parts.pop("schema")
    return Securable(**parts)


@pytest.mark.parametrize(
    "part,canonical",
    [
        ("RAW", "RAW"),
        ("AGGREGATE", "AGGREGATE"),
        ("GROUP", "GROUP"),
        ("SNOWFLAKE$LOCAL_APPLICATION", "SNOWFLAKE$LOCAL_APPLICATION"),
        ('"Mixed-Case"', "Mixed-Case"),
        ('"lower_case"', "lower_case"),
        ('"has ""quotes"""', 'has "quotes"'),
        ('"dot.inside"', "dot.inside"),
        ('"<TABLE>"', "<TABLE>"),
        # Bare is canonical as it stands, not resolved to uppercase
        ("lower_case", "lower_case"),
        ("CamelCase", "CamelCase"),
        ("gitlab-ci", "gitlab-ci"),
    ],
)
def test_canonical_from_api(part, canonical):
    assert canonical_from_api(part) == canonical


@pytest.mark.parametrize("part", ['""', '"unterminated', '"a"b"'])
def test_canonical_from_api_rejects_a_broken_quoted_part(part):
    with pytest.raises(InvalidNameError):
        canonical_from_api(part)


def test_from_api_builds_from_canonical_parts():
    assert ObjectName.from_api("RAW", '"Mixed-Case"') == ObjectName.from_canonical(
        "RAW", "Mixed-Case"
    )
    assert ObjectName.from_api("lower_db") == ObjectName.from_canonical("lower_db")
    assert ObjectName.from_api("lower_db") != ObjectName.single("lower_db")


# (securable_type, Securable parts, future, SHOW GRANTS granted_on, its name column),
# one row per shape the probe paired, with invented names
PROBE_SHAPES = [
    ("ACCOUNT", {"name": "AB12345"}, False, "ACCOUNT", None),
    ("AI GATEWAY", {"name": "SNOWFLAKE"}, False, "AI_GATEWAY", None),
    (
        "APPLICATION ROLE",
        {"database": "SNOWFLAKE", "name": "PUBLIC"},
        False,
        "APPLICATION_ROLE",
        "SNOWFLAKE.PUBLIC",
    ),
    (
        "APPLICATION ROLE",
        {"database": "SNOWFLAKE", "name": '"CORTEX-MODEL-ROLE-ALL"'},
        False,
        "APPLICATION_ROLE",
        'SNOWFLAKE."CORTEX-MODEL-ROLE-ALL"',
    ),
    ("COMPUTE POOL", {"name": "SYSTEM_COMPUTE_POOL_CPU"}, False, "COMPUTE_POOL", None),
    (
        "CORTEX AGENT",
        {"database": "INTELLIGENCE", "schema": "AGENTS", "name": "REVIEWER"},
        False,
        "CORTEX_AGENT",
        None,
    ),
    (
        "CORTEX SEARCH SERVICE",
        {"database": "SCRATCH", "schema": "FIELDWORK", "name": "INDEXING"},
        False,
        "CORTEX_SEARCH_SERVICE",
        None,
    ),
    ("DATABASE", {"name": "ANALYTICS"}, False, "DATABASE", "ANALYTICS"),
    (
        "DATABASE ROLE",
        {"database": "SNOWFLAKE", "name": "GOVERNANCE_VIEWER"},
        False,
        "DATABASE_ROLE",
        "SNOWFLAKE.GOVERNANCE_VIEWER",
    ),
    ("EXTERNAL VOLUME", {"name": "RAW_VOLUME"}, False, "EXTERNAL_VOLUME", "RAW_VOLUME"),
    (
        "FUNCTION",
        {"database": "RAW", "schema": "PUBLIC", "name": '"UTIL_PARSE(VARCHAR)"'},
        False,
        "FUNCTION",
        None,
    ),
    (
        "ICEBERG TABLE",
        {"database": "RAW", "schema": "IOT", "name": "EVENTS"},
        False,
        "ICEBERG_TABLE",
        "RAW.IOT.EVENTS",
    ),
    (
        "INTEGRATION",
        {"name": "SNOWFLAKE$LOCAL_APPLICATION"},
        False,
        "INTEGRATION",
        "SNOWFLAKE$LOCAL_APPLICATION",
    ),
    ("ROLE", {"name": "DATA_SCIENCE"}, False, "ROLE", "DATA_SCIENCE"),
    ("ROLE", {"name": '"ORG-WIDE-VIEWER"'}, False, "ROLE", '"ORG-WIDE-VIEWER"'),
    (
        "SCHEMA",
        {"database": "ANALYTICS", "name": "DRAFT_ZONE"},
        False,
        "SCHEMA",
        "ANALYTICS.DRAFT_ZONE",
    ),
    # A keyword schema: bare in REST, quoted by SHOW GRANTS
    (
        "SCHEMA",
        {"database": "LAB_RESULTS", "name": "AGGREGATE"},
        False,
        "SCHEMA",
        'LAB_RESULTS."AGGREGATE"',
    ),
    (
        "SNOWFLAKE INTELLIGENCE",
        {"name": "DEFAULT"},
        False,
        "SNOWFLAKE_INTELLIGENCE",
        None,
    ),
    (
        "TABLE",
        {"database": "ANALYTICS", "schema": "DW", "name": "DIM_SAMPLE"},
        False,
        "TABLE",
        "ANALYTICS.DW.DIM_SAMPLE",
    ),
    ("USER", {"name": "ETL_SERVICE"}, False, "USER", "ETL_SERVICE"),
    ("WAREHOUSE", {"name": "TRANSFORMING"}, False, "WAREHOUSE", "TRANSFORMING"),
    # Future grants: the name is the placeholder, quoted, with an underscore
    (
        "DYNAMIC TABLE",
        {"database": "SCRATCH", "name": '"<DYNAMIC_TABLE>"'},
        True,
        "DYNAMIC_TABLE",
        "SCRATCH.<DYNAMIC_TABLE>",
    ),
    (
        "DYNAMIC TABLE",
        {"database": "ANALYTICS", "schema": "DQ", "name": '"<DYNAMIC_TABLE>"'},
        True,
        "DYNAMIC_TABLE",
        "ANALYTICS.DQ.<DYNAMIC_TABLE>",
    ),
    (
        "ICEBERG TABLE",
        {"database": "ANALYTICS", "schema": "DW", "name": '"<ICEBERG_TABLE>"'},
        True,
        "ICEBERG_TABLE",
        "ANALYTICS.DW.<ICEBERG_TABLE>",
    ),
    (
        "SCHEMA",
        {"database": "ANALYTICS", "name": '"<SCHEMA>"'},
        True,
        "SCHEMA",
        "ANALYTICS.<SCHEMA>",
    ),
    (
        "SEMANTIC VIEW",
        {"database": "ANALYTICS", "schema": "DW", "name": '"<SEMANTIC_VIEW>"'},
        True,
        "SEMANTIC_VIEW",
        "ANALYTICS.DW.<SEMANTIC_VIEW>",
    ),
    (
        "STREAMLIT",
        {"database": "ANALYTICS", "schema": "DW", "name": '"<STREAMLIT>"'},
        True,
        "STREAMLIT",
        "ANALYTICS.DW.<STREAMLIT>",
    ),
    (
        "TABLE",
        {"database": "ANALYTICS", "name": '"<TABLE>"'},
        True,
        "TABLE",
        "ANALYTICS.<TABLE>",
    ),
    (
        "TABLE",
        {"database": "ANALYTICS", "schema": "DW", "name": '"<TABLE>"'},
        True,
        "TABLE",
        "ANALYTICS.DW.<TABLE>",
    ),
    (
        "TABLE",
        {"database": "LAB_RESULTS", "schema": "AGGREGATE", "name": '"<TABLE>"'},
        True,
        "TABLE",
        'LAB_RESULTS."AGGREGATE".<TABLE>',
    ),
    (
        "VIEW",
        {"database": "ANALYTICS", "name": '"<VIEW>"'},
        True,
        "VIEW",
        "ANALYTICS.<VIEW>",
    ),
    (
        "VIEW",
        {"database": "ANALYTICS", "schema": "DW", "name": '"<VIEW>"'},
        True,
        "VIEW",
        "ANALYTICS.DW.<VIEW>",
    ),
]


@pytest.mark.parametrize("securable_type,parts,future,granted_on,sql", PROBE_SHAPES)
def test_rest_grant_names_match_show_grants(
    securable_type, parts, future, granted_on, sql
):
    name = api_granted_name(securable_type, securable(**parts), future=future)

    if sql is None:
        assert name is None
    else:
        assert name == granted_name(granted_on, sql)
        assert SnowflakeConnector._api_granted_on(
            securable_type
        ) == SnowflakeConnector.normalise_granted_on(granted_on)


def test_a_placeholder_outside_a_future_listing_is_a_name():
    name = api_granted_name(
        "TABLE", securable(database="RAW", schema="PUBLIC", name='"<TABLE>"')
    )

    assert not name.is_future
    assert name.parts == ("RAW", "PUBLIC", "<TABLE>")


def test_a_bare_lowercase_part_is_canonical():
    name = api_granted_name(
        "SCHEMA", securable(database="RAW", name="dbt_heidi"), future=False
    )

    assert name == ObjectName('raw."dbt_heidi"')


@pytest.mark.parametrize(
    "securable_type,parts,future",
    [
        # Parts the kind doesn't have, or is missing
        ("TABLE", {"database": "RAW", "name": "EVENTS"}, False),
        ("DATABASE", {"database": "RAW", "name": "PUBLIC"}, False),
        ("SCHEMA", {"database": "RAW", "schema": "PUBLIC", "name": "X"}, False),
        ("DATABASE ROLE", {"name": "READER"}, False),
        ("SCHEMA", {"database": "RAW", "schema": "S", "name": '"<SCHEMA>"'}, True),
        # A future grant whose name isn't its kind's placeholder
        ("TABLE", {"database": "RAW", "name": '"<VIEW>"'}, True),
        ("TABLE", {"database": "RAW", "name": "EVENTS"}, True),
        ("TABLE", {"database": "RAW", "name": '"<table>"'}, True),
    ],
)
def test_parts_that_dont_fit_the_kind_raise(securable_type, parts, future):
    with pytest.raises(InvalidNameError):
        api_granted_name(securable_type, securable(**parts), future=future)


def test_future_grants_on_kinds_tundra_doesnt_manage_are_skipped():
    assert (
        api_granted_name(
            "FUNCTION",
            securable(database="RAW", schema="PUBLIC", name='"<FUNCTION>"'),
            future=True,
        )
        is None
    )
