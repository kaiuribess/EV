"""Offline-only comparison of explicit verified forecast exports; never activates trading."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from evplatform.core.model_validation import ValidationConfig, ValidationDataset, evaluate_reference_models


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="ValidationDataset JSON; explicit settled binary forecasts")
    parser.add_argument("--output", type=Path, required=True, help="New local research report JSON")
    parser.add_argument("--config", type=Path, help="Optional ValidationConfig JSON; fixed before evaluating")
    args = parser.parse_args(argv)
    if args.output.resolve() in {args.input.resolve(), *( [args.config.resolve()] if args.config else [])}:
        print(json.dumps({"ok": False, "error": "output_cannot_replace_input_or_configuration"}))
        return 2
    try:
        dataset = ValidationDataset.model_validate_json(args.input.read_text(encoding="utf-8"))
        config = ValidationConfig.model_validate_json(args.config.read_text(encoding="utf-8")) if args.config else ValidationConfig()
        report = evaluate_reference_models(dataset, config)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents an unnoticed overwrite of another report.
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2, allow_nan=False)
            output.write("\n")
    except (OSError, ValueError, ValidationError):
        # Never print arbitrary input rows, paths containing secrets, or values.
        print(json.dumps({"ok": False, "error": "invalid_input_conflicting_evidence_or_output_unavailable"}))
        return 2
    print(json.dumps({"ok": True, "status": report["status"], "input_rows": report["input_rows"],
        "eligible_events": report["eligible_events"], "test_events": report["test_events"],
        "learned_comparison_available": report["learned_comparison_available"], "execution_enabled": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
