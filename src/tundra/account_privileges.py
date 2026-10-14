"""Account-level (global) privileges a role can carry via `account_privileges`.

Tundra runs as SECURITYADMIN. Most global privileges are grantable from there through
MANAGE GRANTS, but Snowflake reserves a few for ACCOUNTADMIN: a GRANT that mixes them
in returns "grant partially executed" and applies none of it. Those stay outside the
spec, and the spec loader rejects them so nobody finds out at apply time.

Revocation follows the same boundary. `SHOW GRANTS TO ROLE` reports a grantor for every
account-level row; Snowflake's own privileges on its system-defined roles (CREATE
DATABASE on SYSADMIN, MANAGE GRANTS on SECURITYADMIN) carry an empty one and cannot be
revoked, so those rows are never candidates either.
"""

from typing import FrozenSet

ACCOUNTADMIN_ONLY_PRIVILEGES: FrozenSet[str] = frozenset(
    {
        "create share",
        "import share",
        "manage share target",
    }
)


def normalise_account_privilege(privilege: str) -> str:
    """Lower-case and collapse whitespace, matching how SHOW GRANTS is keyed."""
    return " ".join(privilege.lower().split())
