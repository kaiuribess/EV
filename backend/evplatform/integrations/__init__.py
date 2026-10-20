"""Built-in provider transports. Importing this module never starts network IO."""
from .base import ProviderEnvelope
from .oddspapi import OddsPapiRest
from .owls import OwlsRest
from .parlay import ParlayRest, ParlayWebSocket

PROVIDER_FACTORIES = {
    "parlay_rest": ParlayRest,
    "parlay_ws": ParlayWebSocket,
    "owls_rest": OwlsRest,
    "oddspapi_rest": OddsPapiRest,
}
PROVIDER_MANIFESTS = {name: factory.manifest for name, factory in PROVIDER_FACTORIES.items()}

__all__ = ["ProviderEnvelope", "PROVIDER_FACTORIES", "PROVIDER_MANIFESTS"]
