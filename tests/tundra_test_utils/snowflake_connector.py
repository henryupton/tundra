from typing import Optional, Dict, List, Any
from tundra.snowflake_connector import SnowflakeConnector


class MockSnowflakeConnector(SnowflakeConnector):
    def show_databases(self) -> List[str]:
        return []

    def show_warehouses(self) -> List[str]:
        return []

    def show_integrations(self) -> List[str]:
        return []

    def show_roles(self) -> Dict[str, str]:
        return {}

    def show_users(self) -> List[str]:
        return []

    def show_schemas(self, database: Optional[str] = None) -> List[str]:
        return []

    def show_tables(
        self, database: Optional[str] = None, schema: Optional[str] = None
    ) -> List[str]:
        return []

    def show_views(
        self, database: Optional[str] = None, schema: Optional[str] = None
    ) -> List[str]:
        return []

    def show_iceberg_tables(
        self, database: Optional[str] = None, schema: Optional[str] = None
    ) -> List[str]:
        return []

    def show_future_grants(
        self, database: Optional[str] = None, schema: Optional[str] = None
    ) -> List[str]:
        return []

    def show_grants_to_role(self, role) -> Dict[str, Any]:
        return {}

    def show_grants_to_role_with_grant_option(self, role) -> Dict[str, Any]:
        return {}

    def show_roles_granted_to_user(self, user) -> List[str]:
        return []

    def get_current_user(self) -> str:
        return ""

    def get_current_role(self) -> str:
        return "securityadmin"

    def full_schema_list(self, schema: str) -> List[str]:
        return []
