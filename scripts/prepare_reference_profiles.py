"""Produce reviewed settings JSON locally; never connect accounts or enable trading."""
import argparse
import json
from pathlib import Path

from evplatform.core.profile_import import compile_priors


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=root / "config/book_weight_priors_2026-10-20.json")
    parser.add_argument("--output", type=Path, default=root / "data/reference_profiles.pricing.json")
    args = parser.parse_args()
    pricing = compile_priors(json.loads(args.source.read_text(encoding="utf-8-sig")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(pricing.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"profiles": len(pricing.reference_profiles),
        "enabled": pricing.enabled_reference_profiles, "mode": "research_only",
        "verified_ttl_rules": 0, "live_execution_allowed": False, "output": str(args.output)}))


if __name__ == "__main__":
    main()
