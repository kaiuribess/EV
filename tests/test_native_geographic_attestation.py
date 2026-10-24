"""Offline checks: account access must not masquerade as current location."""
from datetime import timedelta
from types import SimpleNamespace

from evplatform.core.models import utcnow
from evplatform.core.native_runtime import current_geographic_attestation
from evplatform.core.readiness import evaluate_readiness

from test_launch_readiness import ready_fixture


def test_native_location_requires_current_attestation_for_exact_account():
    now = utcnow()
    account = "a" * 64
    snapshot = SimpleNamespace(identity_verified=True, account_fingerprint=account,
        environment="production", geographic_attestation_expires_at=None)
    assert not current_geographic_attestation(snapshot, account, "production", now)
    snapshot.geographic_attestation_expires_at = now - timedelta(milliseconds=1)
    assert not current_geographic_attestation(snapshot, account, "production", now)
    snapshot.geographic_attestation_expires_at = now + timedelta(minutes=1)
    assert current_geographic_attestation(snapshot, account, "production", now)
    assert not current_geographic_attestation(snapshot, "b" * 64, "production", now)
    assert not current_geographic_attestation(snapshot, account, "sandbox", now)


def test_novig_readiness_does_not_infer_location_from_wallet_or_saved_certificate():
    profile, runtime, now = ready_fixture()
    profile = profile.model_copy(update={"venue": "novig"})
    evidence = tuple(row.model_copy(update={"venue": "novig", "profile_digest": profile.digest()})
                     for row in profile.evidence)
    profile = profile.model_copy(update={"evidence": evidence})
    runtime = runtime.model_copy(update={"venue": "novig", "geographic_attestation_current": False})
    report = evaluate_readiness(profile, runtime, now=now)
    assert not report.safe_to_arm
    assert "account.geographic_attestation_missing" in {row.code for row in report.blockers}
    verified = runtime.model_copy(update={"geographic_attestation_current": True})
    assert evaluate_readiness(profile, verified, now=now).safe_to_arm
