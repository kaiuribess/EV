"""One shared risk ledger for explicitly reviewed accounts on distinct venues."""
from typing import Literal

from pydantic import Field, model_validator

from .readiness import LaunchProfile, StrictRecord, digest_json


class LaunchPortfolio(StrictRecord):
    schema_version: Literal[1] = 1
    profiles: tuple[LaunchProfile, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def coherent_scope(self):
        first = self.profiles[0]
        if len({p.venue for p in self.profiles}) != len(self.profiles):
            raise ValueError("Only one reviewed account per venue is supported")
        if len({p.account_fingerprint for p in self.profiles}) != len(self.profiles):
            raise ValueError("Each venue must have its own authenticated account identity")
        for p in self.profiles[1:]:
            for field in ("environment", "build_id", "mapping_digest", "pricing_digest", "limits", "strategy_ids"):
                if getattr(p, field) != getattr(first, field):
                    raise ValueError(f"Portfolio profiles must share {field}")
        return self

    def digest(self):
        return digest_json({p.venue: p.digest() for p in self.profiles})
