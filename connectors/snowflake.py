"""Snowflake connector stub."""

from connectors.base import BaseConnector, MetadataSnapshot


class SnowflakeConnector(BaseConnector):
    """Snowflake metadata inspection through INFORMATION_SCHEMA."""

    def __init__(self, config: dict) -> None:
        self.config = config

    def validate_config(self) -> list[str]:
        raise NotImplementedError("Snowflake connector not yet implemented")

    def test_connection(self) -> bool:
        raise NotImplementedError("Snowflake connector not yet implemented")

    def fetch_metadata(self) -> MetadataSnapshot:
        raise NotImplementedError("Snowflake connector not yet implemented")

    def profile_dimension(self, source_ref: str, max_values: int = 500) -> list:
        raise NotImplementedError("Snowflake connector not yet implemented")
