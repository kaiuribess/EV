"""CLI ledger scoping; fake network boundary, no real account access."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_launch_readiness import script


@pytest.mark.parametrize("environment,override,expected", [
    ("production", None, Path("data/native-production.db")),
    ("sandbox", None, Path("data/native-sandbox.db")),
    ("production", Path("custom-ledger.db"), Path("custom-ledger.db")),
])
async def test_history_capture_uses_environment_ledger_instead_of_demo(tmp_path, monkeypatch, environment, override, expected):
    module = script("capture_native_lifecycle")
    paths, saved = [], {}
    def snapshot(path):
        paths.append(path)
        return {"available": False, "tables": {}}
    async def capture(venue, selected_environment, values, **kwargs):
        assert kwargs["managed_orders"] == []
        return {"venue": venue, "environment": selected_environment, "authenticated": True,
                "identity_verified": True, "account_fingerprint": "a"*64, "errors": [], "collections": {}}
    monkeypatch.setattr(module, "local_oms_snapshot", snapshot)
    monkeypatch.setattr(module, "capture_venue", capture)
    monkeypatch.setattr(module, "dotenv_values", lambda _: {})
    monkeypatch.setattr(module, "build_fingerprint", lambda: "OFFLINE")
    monkeypatch.setattr(module, "protected_json", lambda path, value: saved.update({path.name: value}))
    await module.run(SimpleNamespace(output=tmp_path/"new", env_file=tmp_path/"unused", database=override,
                                    venue=["prophetx"], environment=environment, max_pages=4))
    assert paths == [expected]
    artifact = saved["account_history.json"]
    assert artifact["local_oms"]["available"] is False
    assert artifact["local_oms_scope"] == {"environment": environment, "database_name": expected.name,
                                         "explicit_override": override is not None}
    assert saved["coverage.json"]["local_oms_scope"] == artifact["local_oms_scope"]
    assert artifact["captured_at"] <= artifact["finished_at"]
