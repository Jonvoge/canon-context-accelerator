import pytest

from serving.fabric_proxy.server import _resolve_connector


def test_full_flow_resolve_and_execute():
    scan_config = {
        "connectors": [{
            "id": "retail-semantic",
            "type": "fabric_semantic",
            "auth_secret_name": "X",
            "options": {
                "workspace_id": "b53e615a-ab79-49f9-bcf0-67250a34f633",
                "dataset_id": "d4e5f6a7-1234-5678-9abc-def012345678",
                "dataset_name": "RetailSemanticModel",
            },
        }],
        "domains": [{"name": "retail", "semantic_connector": "retail-semantic"}],
    }
    target = _resolve_connector(scan_config, domain="retail", model="retail-semantic")
    assert target["workspace_id"] == "b53e615a-ab79-49f9-bcf0-67250a34f633"
    assert target["dataset_id"] == "d4e5f6a7-1234-5678-9abc-def012345678"


def test_unknown_domain_raises():
    scan_config = {"connectors": [], "domains": []}
    with pytest.raises(ValueError, match="not found"):
        _resolve_connector(scan_config, domain="finance", model="retail-semantic")
