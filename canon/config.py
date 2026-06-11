"""Shared scan-config loader with legacy-key normalization."""

from __future__ import annotations

import copy
import logging
import warnings
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def load_scan_config(path: Path | str) -> dict:
    """Load and normalize scan-config.yaml."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return _normalize(raw)


def _normalize(cfg: dict) -> dict:
    cfg = copy.deepcopy(cfg)
    for domain in cfg.get("domains", []):
        if "models" not in domain:
            models = []
            semantic_connector = domain.pop("semantic_connector", None)
            warehouse_connector = domain.pop("warehouse_connector", None)
            if semantic_connector:
                models.append(
                    {
                        "connector": semantic_connector,
                        "role": "semantic",
                        "primary": True,
                        "description": "Primary semantic model.",
                    }
                )
                warnings.warn(
                    f"Domain '{domain.get('name')}': 'semantic_connector' is deprecated; use 'models' list.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            if warehouse_connector:
                models.append(
                    {
                        "connector": warehouse_connector,
                        "role": "warehouse",
                        "primary": False,
                        "description": "SQL warehouse endpoint.",
                    }
                )
                warnings.warn(
                    f"Domain '{domain.get('name')}': 'warehouse_connector' is deprecated; use 'models' list.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            domain["models"] = models
        if "models" in domain:
            for model in domain["models"]:
                if model.get("role") == "semantic" and model.get("primary"):
                    domain.setdefault("semantic_connector", model["connector"])
                if model.get("role") == "warehouse":
                    domain.setdefault("warehouse_connector", model["connector"])
    return cfg
