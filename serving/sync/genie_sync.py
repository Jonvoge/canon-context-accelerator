"""Optional sync adapter for downstream platform-specific AI surfaces."""


class GenieSyncAdapter:
    """Sync Canon definitions to Databricks Genie knowledge store."""

    def __init__(self, config: dict) -> None:
        self.config = config

    def sync_domain(self, domain: str) -> dict:
        raise NotImplementedError("Genie sync not yet implemented")
