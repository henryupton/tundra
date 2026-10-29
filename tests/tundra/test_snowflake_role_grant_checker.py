import pytest

from tundra.snowflake_connector import SnowflakeConnector
from tundra.snowflake_permission import SnowflakePermission
from tundra.snowflake_role_grant_checker import (
    SnowflakeRoleGrantChecker,
)

from tundra_test_utils.snowflake_connector import MockSnowflakeConnector


@pytest.fixture
def mock_connector(mocker):
    mocker.patch.object(
        MockSnowflakeConnector,
        "show_grants_to_role_with_grant_option",
        return_value={
            "use": {
                "database": {"db 1": {"grant_option": False}},
                "schema": {"schema 3": {"grant_option": False}},
            },
            "ownership": {
                "schema": {
                    "schema 1": {"grant_option": True},
                    "schema 2": {"grant_option": True},
                },
                "table": {
                    "table 1": {"grant_option": True},
                    "table 2": {"grant_option": True},
                    "table 3": {"grant_option": True},
                },
            },
            "monitor": {
                "warehouse": {
                    "wh 1": {"grant_option": False},
                    "wh 2": {"grant_option": True},
                }
            },
            "manage grants": {"account": {"Account_Name": {"grant_option": False}}},
        },
    )
    return MockSnowflakeConnector()


@pytest.fixture
def grant_checker(mock_connector):
    return SnowflakeRoleGrantChecker(mock_connector)


class TestSnowflakeRoleGrantChecker:
    def test_get_permissions(self, grant_checker, mock_connector):
        permissions = grant_checker.get_permissions("my_role")
        mock_connector.show_grants_to_role_with_grant_option.assert_called_with(
            "my_role"
        )

        assert SnowflakePermission("db 1", "database", ["use"], False) in permissions
        assert (
            SnowflakePermission("wh 2", "warehouse", ["monitor"], False) in permissions
        )
        assert (
            SnowflakePermission("table 3", "table", ["ownership"], False) in permissions
        )

    @pytest.mark.parametrize(
        "permission,expected",
        [
            (SnowflakePermission("table 4", "table", ["ownership"], False), False),
            (SnowflakePermission("table 1", "table", ["ownership"], False), True),
            (SnowflakePermission("table 1", "table", ["select"], False), True),
            (SnowflakePermission("*", "account", ["ownership"], False), False),
            (SnowflakePermission("*", "account", ["manage grants"], False), True),
            # The role's own account permissions are named "*"
            (
                SnowflakePermission(
                    "Account_Name", "account", ["manage grants"], False
                ),
                False,
            ),
        ],
    )
    def test_has_permissions(self, grant_checker, permission, expected):
        for p in grant_checker.get_permissions("my_role"):
            print(p)
        assert grant_checker.has_permission("my_role", permission) == expected

    def test_has_permission_no_role(self, grant_checker):
        permission = SnowflakePermission("*", "account", ["ownership"], True)
        assert grant_checker.has_permission(None, permission) is True

    @pytest.mark.parametrize(
        "permission,expected",
        [
            (SnowflakePermission("table 4", "table", ["select"], True), False),
            (SnowflakePermission("schema 1", "schema", ["ownership"], False), True),
            (SnowflakePermission("table 1", "table", ["select"], False), True),
            (SnowflakePermission("*", "account", ["ownership"], False), False),
            (SnowflakePermission("wh 2", "warehouse", ["monitor"], False), True),
            (SnowflakePermission("wh 1", "warehouse", ["monitor"], False), False),
        ],
    )
    def test_can_grant_permission(self, grant_checker, permission, expected):
        print("Permissions for role:")
        for p in grant_checker.get_permissions("my_role"):
            print(p)
        assert grant_checker.can_grant_permission("my_role", permission) == expected

    def test_can_grant_permission_no_role(self, grant_checker):
        permission = SnowflakePermission("*", "account", ["ownership"], True)
        assert grant_checker.can_grant_permission(None, permission) is True


class TestGrantsReadFromSnowflake:
    @pytest.fixture
    def checker(self, mocker):
        rows = [
            ("MANAGE GRANTS", "ACCOUNT", "AB12345", "false"),
            ("USAGE", "SCHEMA", 'RAW."Mixed-Case"', "true"),
            ("USAGE", "FUNCTION", 'RAW.PUBLIC."F(A VARCHAR):VARCHAR"', "true"),
        ]
        # The connector's own reader, not the mock's
        mocker.patch.object(
            MockSnowflakeConnector,
            "show_grants_to_role_with_grant_option",
            SnowflakeConnector.show_grants_to_role_with_grant_option,
        )
        mocker.patch.object(
            MockSnowflakeConnector,
            "run_query",
            return_value=mocker.Mock(
                fetchall=lambda: [
                    {
                        "privilege": privilege,
                        "granted_on": granted_on,
                        "name": name,
                        "grant_option": grant_option,
                    }
                    for privilege, granted_on, name, grant_option in rows
                ]
            ),
        )
        return SnowflakeRoleGrantChecker(MockSnowflakeConnector())

    @pytest.mark.parametrize(
        "permission,expected",
        [
            (SnowflakePermission("*", "account", ["manage grants"], False), True),
            (
                SnowflakePermission("AB12345", "account", ["manage grants"], False),
                False,
            ),
            (SnowflakePermission('raw."Mixed-Case"', "schema", ["usage"], False), True),
            # Tundra doesn't read the names of function grants, so none is held
            (
                SnowflakePermission(
                    'RAW.PUBLIC."F(A VARCHAR):VARCHAR"', "function", ["usage"], False
                ),
                False,
            ),
        ],
    )
    def test_has_permission(self, checker, permission, expected):
        assert checker.has_permission("my_role", permission) is expected
