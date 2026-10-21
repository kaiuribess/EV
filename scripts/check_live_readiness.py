"""Read-only launch check. Never opens a venue connection or changes arming state."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from evplatform.core.readiness import LaunchProfile, RuntimeReadiness, evaluate_readiness


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, help="Reviewed local launch-profile JSON (not an env file)")
    parser.add_argument("--runtime", type=Path, help="Current engine RuntimeReadiness JSON; expires after five seconds")
    parser.add_argument("--url", help="Read current readiness from the local API, e.g. http://127.0.0.1:8001")
    args = parser.parse_args(argv)
    try:
        if args.url:
            if args.profile or args.runtime:
                parser.error("Use --url or local files, not both")
            from urllib.parse import urlparse
            import httpx

            parsed = urlparse(args.url)
            if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or parsed.username or parsed.password or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment):
                raise ValueError("Only a loopback HTTP application URL is accepted")
            # No environment proxy, redirects, credential headers or request body.
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as client:
                response = client.get(args.url.rstrip("/") + "/api/readiness")
                response.raise_for_status()
                report = response.json()
            print(json.dumps(report, indent=2))
            return 0 if report.get("safe_to_arm") is True else 1
        profile = LaunchProfile.model_validate_json(args.profile.read_text(encoding="utf-8")) if args.profile else None
        runtime = RuntimeReadiness.model_validate_json(args.runtime.read_text(encoding="utf-8")) if args.runtime else RuntimeReadiness()
        report = evaluate_readiness(profile, runtime)
        print(report.model_dump_json(indent=2))
        return 0 if report.safe_to_arm else 1
    except ValidationError as exc:
        # Pydantic's ordinary error string includes rejected input, which might
        # accidentally contain a credential. Return locations/types only.
        print(json.dumps({"error": "invalid_configuration", "fields": [
            {"location": list(item["loc"]), "type": item["type"]} for item in exc.errors()
        ]}))
    except Exception as exc:
        # Do not print file contents, request URLs, tokens or account responses.
        print(json.dumps({"error": "readiness_check_failed", "type": type(exc).__name__}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
