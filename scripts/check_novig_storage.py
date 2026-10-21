"""Local-only permissions diagnosis. Never constructs an HTTP client or submits an account action."""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

# Resolve this checkout even when invoked from System32 or with stale installs.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from evplatform.venues import novig_setup
from evplatform.venues.novig_setup import NovigSetup, SetupError, protect_private_path


def diagnose(journal: Path):
    report = {"network_requests": 0, "account_actions": 0,
              "python_bits": struct.calcsize("P") * 8,
              "storage_code_revision": hashlib.sha256(Path(novig_setup.__file__).read_bytes()).hexdigest()[:12],
              "checks": {}}
    try:
        original = journal.read_bytes()
        saved = json.loads(original)
        helper = NovigSetup(SimpleNamespace(settings=SimpleNamespace(api_key=saved["management_key_id"])),
                            journal.parent.parent)
        report["local_appdata_matches_saved_signer"] = (
            helper.secret_dir / saved["setup_id"] / "trading.pem" == Path(saved["private_key_path"]))
        paths = {"setup_directory": helper.setup_dir, "secret_directory": helper.secret_dir,
                 "operation_lock": helper.setup_dir / ".operation.lock", "journal": journal,
                 "private_signer": Path(saved["private_key_path"])}
        for label, path in paths.items():
            try:
                protect_private_path(path)
                report["checks"][label] = "passed"
            except SetupError as exc:
                report["checks"][label] = str(exc)
            except Exception:
                report["checks"][label] = "local_check_failed_details_redacted"
        try:
            with helper._locked():
                state, _ = helper._load(journal)
            report["checks"]["local_lock_and_signer"] = "passed"
            report["open_state"] = state.open_state
            report["recorded_transfers"] = len(state.transfers)
        except SetupError as exc:
            report["checks"]["local_lock_and_signer"] = str(exc)
        report["journal_unchanged"] = journal.read_bytes() == original
        report["ok"] = all(value == "passed" for value in report["checks"].values()) and report["journal_unchanged"]
    except Exception:
        report["ok"] = False
        report["error"] = "local_journal_or_storage_unavailable_details_redacted"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True)
    args = parser.parse_args()
    journal = Path(args.journal)
    if not journal.is_absolute():
        journal = ROOT / journal
    report = diagnose(journal)
    text = json.dumps(report, indent=2)
    print(text)
    # Diagnostics contain only fixed classifications, booleans and numeric codes.
    try:
        (ROOT / "data" / "novig-storage-diagnostic.json").write_text(text, encoding="utf-8")
    except OSError:
        pass
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
