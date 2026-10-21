"""Native venue protocols. Construction never opens a connection or places orders."""

from .common import NativeTradingGate, PreflightRequired, SequenceGap, UnknownOrderOutcome
from .kalshi import KalshiClient, KalshiSettings
from .novig import NovigClient, NovigSettings
from .prophetx import ProphetXClient, ProphetXSettings
from .polymarket_us import PolymarketUSClient, PolymarketUSSettings
from .bridge import NativeExecutionVenue, NativePreflight, VerifiedVenueMapping, native_execution_factories

__all__ = [
    "KalshiClient", "KalshiSettings", "NovigClient", "NovigSettings",
    "ProphetXClient", "ProphetXSettings", "NativeTradingGate",
    "PolymarketUSClient", "PolymarketUSSettings",
    "PreflightRequired", "SequenceGap", "UnknownOrderOutcome",
    "NativeExecutionVenue", "NativePreflight", "VerifiedVenueMapping", "native_execution_factories",
]


def native_manifests() -> list[dict]:
    return [client.get_manifest() for client in (KalshiClient, NovigClient, ProphetXClient, PolymarketUSClient)]
