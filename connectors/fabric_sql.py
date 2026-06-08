"""Fabric SQL endpoint connector via pyodbc."""

from connectors.base import BaseConnector, MetadataSnapshot


class FabricSqlConnector(BaseConnector):
    """Fabric lakehouse/warehouse SQL endpoint metadata and fallback SQL access."""

    def __init__(self, config: dict) -> None:
        self.config = config

    def validate_config(self) -> list[str]:
        errors = []
        required = ["endpoint", "tenant_id", "client_id"]
        for key in required:
            if key not in self.config:
                errors.append(f"Missing required config: {key}")
        return errors

    def test_connection(self) -> bool:
        raise NotImplementedError("Phase 2 implementation")

    def fetch_metadata(self) -> MetadataSnapshot:
        raise NotImplementedError("Phase 2 implementation")

    def profile_dimension(self, source_ref: str, max_values: int = 500) -> list:
        raise NotImplementedError("Phase 2 implementation")
