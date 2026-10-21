"""Credential routing uses dummy strings and never opens a network connection."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evplatform.core.plugins import PluginRegistry
from evplatform.plugins.external import register_external, NovigMonitorConfig


@pytest.fixture
def monitor(monkeypatch):
    registry = PluginRegistry()
    register_external(registry, None)
    plugin = registry.records['novig'].plugin
    for key in ('NOVIG_API_KEY', 'NOVIG_PRIVATE_KEY_PATH', 'NOVIG_PAPER_TRADING_API_KEY',
                'NOVIG_PAPER_TRADING_PRIVATE_KEY_PATH', 'NOVIG_PRODUCTION_TRADING_API_KEY',
                'NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH'):
        monkeypatch.delenv(key, raising=False)
    return plugin


def test_existing_configuration_keeps_exact_legacy_pair(monitor):
    config = NovigMonitorConfig(environment='sandbox').model_dump()
    assert config['credential_profile'] == 'legacy'
    assert monitor.credential_names(config) == {'api_key': 'NOVIG_API_KEY', 'private_key_path': 'NOVIG_PRIVATE_KEY_PATH'}


@pytest.mark.parametrize('environment,prefix', [('sandbox', 'NOVIG_PAPER_TRADING'),
                                             ('production', 'NOVIG_PRODUCTION_TRADING')])
async def test_selected_environment_uses_its_complete_pair(monitor, monkeypatch, environment, prefix):
    for name in ('NOVIG', 'NOVIG_PAPER_TRADING', 'NOVIG_PRODUCTION_TRADING'):
        monkeypatch.setenv(name+'_API_KEY', name+'-key')
        monkeypatch.setenv(name+'_PRIVATE_KEY_PATH', name+'-path')
    recorded = []
    client = SimpleNamespace(connect=AsyncMock(), configure_subscriptions=AsyncMock(), disconnect=AsyncMock())
    monitor.factory = lambda settings: recorded.append(settings) or client
    monitor._run = AsyncMock()
    await monitor.start(SimpleNamespace(config={'environment': environment, 'credential_profile': 'environment'}))
    try:
        assert recorded[0].api_key == prefix+'-key'
        assert recorded[0].private_key_path == prefix+'-path'
        assert recorded[0].environment == environment
    finally:
        await monitor.stop()


@pytest.mark.parametrize('missing', ['api_key', 'private_key_path'])
async def test_partial_production_pair_never_borrows_legacy_or_paper(monitor, monkeypatch, missing):
    for prefix in ('NOVIG', 'NOVIG_PAPER_TRADING', 'NOVIG_PRODUCTION_TRADING'):
        monkeypatch.setenv(prefix+'_API_KEY', 'offline-key')
        monkeypatch.setenv(prefix+'_PRIVATE_KEY_PATH', 'offline-path')
    monkeypatch.delenv(monitor.credential_names({'environment':'production','credential_profile':'environment'})[missing])
    monitor.factory = lambda _: pytest.fail('Incomplete credentials must not create a client')
    await monitor.start(SimpleNamespace(config={'environment':'production','credential_profile':'environment'}))
    health = await monitor.health_check()
    assert health['status'] == 'blocked' and not health['authenticated']
    assert 'offline-' not in str(health)


def test_new_credential_choice_appears_in_manifest_metadata(monitor):
    assert monitor.manifest.config_schema['properties']['credential_profile']['enum'] == ['legacy', 'environment']
    assert not any('management' in name.lower() for names in monitor.credential_environments.values() for name in names.values())
