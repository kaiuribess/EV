"""Cross-check protected native history captures offline; never enable execution."""
import argparse
import json
from pathlib import Path

from evplatform.core.lifecycle_capture import build_fingerprint, protected_json
from evplatform.core.lifecycle_evidence import verify_native_lifecycle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--previous-capture", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        capture = json.loads(args.capture.read_text(encoding="utf-8"))
        previous = json.loads(args.previous_capture.read_text(encoding="utf-8")) if args.previous_capture else None
        report = verify_native_lifecycle(capture, expected_build_id=build_fingerprint(), previous_capture=previous)
        protected_json(args.output, report)
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"ok": False, "error": "invalid_evidence_or_protected_output_unavailable"}))
        return 2
    print(json.dumps({"ok": True, "status": report["status"], "native_order_count": report["native_order_count"],
        "certifies_current_oms": False, "execution_enabled": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
