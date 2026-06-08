"""Abstract base connector for Canon platform adapters."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MeasureMetadata:
    name: str
    expression: str | None = None
    description: str | None = None
    table: str | None = None


@dataclass
class ColumnMetadata:
    name: str
    table: str
    data_type: str | None = None
    description: str | None = None


@dataclass
class TableMetadata:
    name: str
    columns: list[ColumnMetadata] = field(default_factory=list)
    description: str | None = None


@dataclass
class RelationshipMetadata:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    is_active: bool = True


@dataclass
class MetadataSnapshot:
    measures: list[MeasureMetadata] = field(default_factory=list)
    tables: list[TableMetadata] = field(default_factory=list)
    relationships: list[RelationshipMetadata] = field(default_factory=list)


class BaseConnector(ABC):
    """Abstract connector contract for Canon platform adapters."""

    @abstractmethod
    def validate_config(self) -> list[str]:
        """Validate connector configuration. Returns list of error messages (empty = valid)."""
        ...

    @abstractmethod
    def test_connection(self) -> bool:
        """Test connectivity to the platform. Returns True if successful."""
        ...

    @abstractmethod
    def fetch_metadata(self) -> MetadataSnapshot:
        """Fetch full metadata snapshot from the platform."""
        ...

    @abstractmethod
    def profile_dimension(self, source_ref: str, max_values: int = 500) -> list[Any]:
        """Profile distinct values for a dimension column."""
        ...

    def supports_dimension_profiling(self) -> bool:
        """Whether this connector supports dimension value profiling."""
        return True
