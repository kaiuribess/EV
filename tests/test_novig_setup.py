"""OFFLINE ONLY: every exchange request is intercepted by HTTPX MockTransport."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import dotenv_values

from evplatform.venues.novig import NovigSettings, novig_canonical
from evplatform.venues.novig_setup import NovigSetup, NovigSetupTransport, SetupError, TTYConfirmation, _Approval, validate_amount


MANAGEMENT_ID = "00000000-0000-4000-8000-000000000001"
WALLET_ID = "00000000-0000-4000-8000-000000000002"
OTHER_ID = "00000000-0000-4000-8000-000000000003"


def fingerprint(key):
    return hashlib.sha256(key.public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()


class Terminal(io.StringIO):
    def isatty(self):
        return True


class AcceptConfirmation:
    """Injected human-action double; only used behind MockTransport."""
    def __init__(self):
        self.requests = []

    def require(self, phrase, description):
        self.requests.append((phrase, description))


def restarted(helper):
    """Discard all service/client in-memory state, retaining only disk and server."""
    transport = NovigSetupTransport(helper.transport.settings, http=helper.transport.http)
    return NovigSetup(transport, helper.setup_dir, secret_dir=helper.secret_dir,
                      confirmation=helper.confirmation, protect=helper.protect)


class OfflineExchange:
    def __init__(self, management_key):
        self.management_key = management_key
        self.public_keys = {MANAGEMENT_ID: management_key.public_key()}
        self.requests = []
        self.wallets = []
        self.keys = {MANAGEMENT_ID: self.identity(MANAGEMENT_ID, "management", fingerprint(management_key.public_key()))}
        self.cash = "100.00000"
        self.wallet_balance = "0.00000"
        self.transfers = {}
        self.open_timeout = False
        self.transfer_timeout = False
        self.before_mutation = None

    @staticmethod
    def identity(key_id, scope, fp):
        return {"keyId": key_id, "scope": scope, "algorithm": "Ed25519", "fingerprint": "sha256:"+fp,
            "createdAt": "2026-10-21T00:00:00Z", "expiresAt": None, "lastUsedAt": None,
            "name": "OFFLINE fixture", "restriction": None if scope == "management" else "OFFLINE wallet"}

    def handle(self, request):
        assert request.url.host == "api.novig.com"
        key_id = request.headers["Novig-Key-Id"]
        signed = novig_canonical(request.headers["Novig-Timestamp"], request.method,
            request.url.path, request.url.query.decode(), request.content)
        self.public_keys[key_id].verify(base64.b64decode(request.headers["Novig-Signature"]), signed)
        self.requests.append(request)
        path, method = request.url.path, request.method
        if method == "GET":
            if path == "/v3/keys":
                return httpx.Response(200, json=list(self.keys.values()))
            if path.startswith("/v3/keys/"):
                row = self.keys.get(path.rsplit("/", 1)[1])
                return httpx.Response(200, json=row) if row else httpx.Response(404, json={"code": "KEY_NOT_FOUND"})
            if path == "/v3/account/subaccounts":
                return httpx.Response(200, json=self.wallets)
            if path == "/v3/account/balance":
                return httpx.Response(200, json={"balance": self.cash})
            if path == f"/v3/account/subaccounts/{WALLET_ID}/balance":
                return httpx.Response(200, json={"keyId": WALLET_ID, "balance": self.wallet_balance})
            if path.startswith(f"/v3/account/subaccounts/{WALLET_ID}/transfers/"):
                transfer_id = path.rsplit("/", 1)[1]
                row = next((item for item in self.transfers.values() if item["transferId"] == transfer_id), None)
                return httpx.Response(200, json=row) if row else httpx.Response(404, json={"code": "TRANSFER_NOT_FOUND"})
        if method == "POST":
            if self.before_mutation:
                self.before_mutation(request)
            body = json.loads(request.content)
            if path == "/v3/account/subaccounts":
                assert set(body) == {"label", "publicKey", "algorithm"}
                assert body["algorithm"] == "Ed25519"
                public = serialization.load_pem_public_key(body["publicKey"].encode())
                self.public_keys[WALLET_ID] = public
                self.keys[WALLET_ID] = self.identity(WALLET_ID, "trading", fingerprint(public))
                self.wallets.append({"keyId": WALLET_ID, "label": body["label"], "balance": "0.00000"})
                if self.open_timeout:
                    raise httpx.ReadTimeout("OFFLINE accepted opening but response lost", request=request)
                return httpx.Response(201, json=self.wallets[-1])
            if path == f"/v3/account/subaccounts/{WALLET_ID}/transfer":
                assert set(body) == {"amount", "direction", "clientTransferId"}
                assert body["direction"] == "fund"
                key = body["clientTransferId"]
                assert len(key) <= 64
                row = self.transfers.setdefault(key, {"transferId": str(UUID(int=100+len(self.transfers))),
                    "clientTransferId": key, "amount": body["amount"],
                    "direction": "fund", "status": "Requested", "actualBalance": None,
                    "createdAt": "2026-10-21T00:00:00Z", "updatedAt": "2026-10-21T00:00:00Z"})
                assert row["amount"] == body["amount"]
                if self.transfer_timeout:
                    raise httpx.ReadTimeout("OFFLINE accepted transfer but response lost", request=request)
                return httpx.Response(202, json=row)
        raise AssertionError(f"Unexpected OFFLINE request: {method} {path}")


@pytest.mark.parametrize("value,expected", [("1", "1.00000"), ("0.00001", "0.00001"), ("123.45678", "123.45678")])
def test_funding_amount_keeps_exact_five_place_cash(value, expected):
    assert validate_amount(value) == expected


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "1e2", "0.000001", "1.234567", " 1", "1 ", 1, 1.2, True, None])
def test_invalid_or_inexact_funding_amount_rejected(value):
    with pytest.raises(SetupError):
        validate_amount(value)


def test_tty_confirmation_requires_both_terminals_and_exact_phrase():
    for input_stream, output_stream in [(io.StringIO(), Terminal()), (Terminal(), io.StringIO())]:
        confirmation = TTYConfirmation(input_stream=input_stream, output_stream=output_stream, read_line=lambda *_: "OPEN EXACT")
        with pytest.raises(SetupError):
            confirmation.require("OPEN EXACT", "OFFLINE test only")
    confirmation = TTYConfirmation(input_stream=Terminal(), output_stream=Terminal(), read_line=lambda *_: "yes")
    with pytest.raises(SetupError):
        confirmation.require("OPEN EXACT", "OFFLINE test only")
    TTYConfirmation(input_stream=Terminal(), output_stream=Terminal(), read_line=lambda *_: "OPEN EXACT").require("OPEN EXACT", "OFFLINE test only")


@pytest.fixture
async def setup_case(tmp_path, monkeypatch):
    # Test --basetemp lives inside this checkout on Windows. Model an isolated
    # temp tree for this fixture; the production Git refusal has its own test.
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: False if path.name == ".git" else exists(path))
    # Only HTTP MockTransport runs here; rate delays are not the subject of
    # these state-machine tests. All real signing and dispatch checks execute.
    original_request = NovigSetupTransport.request
    async def immediate(self, *args, **kwargs):
        self._next_request = 0
        return await original_request(self, *args, **kwargs)
    monkeypatch.setattr(NovigSetupTransport, "request", immediate)
    key = Ed25519PrivateKey.generate()
    pem = tmp_path / "offline-management.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    exchange = OfflineExchange(key)
    confirmation = AcceptConfirmation()
    settings = NovigSettings(environment="production", api_key=MANAGEMENT_ID,
                             private_key_path=str(pem), requests_per_second=100)
    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange.handle), trust_env=False) as http:
        transport = NovigSetupTransport(settings, http=http)
        helper = NovigSetup(transport, setup_dir=tmp_path / "journals", secret_dir=tmp_path / "offline-secrets",
                            confirmation=confirmation, protect=lambda path: None)
        yield helper, exchange, confirmation, tmp_path


async def test_status_is_only_read_only_and_never_exposes_private_key(setup_case):
    helper, exchange, confirmation, _ = setup_case
    result = await helper.status()
    assert exchange.requests and all(r.method == "GET" for r in exchange.requests)
    assert not confirmation.requests
    encoded = json.dumps(result)
    assert "PRIVATE KEY" not in encoded and "Novig-Signature" not in encoded


async def test_prepare_does_not_open_or_fund_and_open_intent_is_durable_before_post(setup_case):
    helper, exchange, confirmation, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    assert isinstance(journal, Path) and journal.is_file()
    assert not any(r.method == "POST" for r in exchange.requests)
    assert not confirmation.requests
    state = json.loads(journal.read_text())
    assert state["environment"] == "production" and state["open_state"] == "prepared"
    def before_send(request):
        saved = json.loads(journal.read_text())
        assert saved["open_state"] == "unknown"
        assert "PRIVATE KEY" not in journal.read_text()
    exchange.before_mutation = before_send
    await helper.open_wallet(journal)
    state = json.loads(journal.read_text())
    assert state["open_state"] == "verified" and state["trading_key_id"] == WALLET_ID
    assert len([r for r in exchange.requests if r.method == "POST"]) == 1
    assert len(confirmation.requests) == 1


async def test_open_timeout_cannot_blindly_retry_and_fingerprint_reconciliation_recovers(setup_case):
    helper, exchange, _, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    exchange.open_timeout = True
    with pytest.raises(SetupError):
        await helper.open_wallet(journal)
    assert json.loads(journal.read_text())["open_state"] == "unknown"
    helper = restarted(helper)
    with pytest.raises(SetupError):
        await helper.open_wallet(journal)
    assert len([r for r in exchange.requests if r.method == "POST"]) == 1
    exchange.open_timeout = False
    await helper.reconcile(journal)
    state = json.loads(journal.read_text())
    assert state["open_state"] == "verified" and state["trading_key_id"] == WALLET_ID
    assert len([r for r in exchange.requests if r.method == "POST"]) == 1


async def test_same_label_with_wrong_signer_is_never_bound_to_opening(setup_case):
    helper, exchange, _, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    exchange.open_timeout = True
    with pytest.raises(SetupError):
        await helper.open_wallet(journal)
    exchange.keys[WALLET_ID]["fingerprint"] = "sha256:"+"a"*64
    result = await helper.reconcile(journal)
    assert result["reconciliation"].startswith("unresolved")
    assert json.loads(journal.read_text())["open_state"] == "unknown"
    assert len([r for r in exchange.requests if r.method == "POST"]) == 1


@pytest.mark.parametrize("field,value", [("scope", "trading"), ("fingerprint", "sha256:"+"a"*64),
    ("algorithm", "P-256"), ("keyId", OTHER_ID), ("expiresAt", "2000-01-01T00:00:00Z")])
async def test_wrong_or_expired_management_identity_prevents_mutations(setup_case, field, value):
    helper, exchange, _, _ = setup_case
    exchange.keys[MANAGEMENT_ID][field] = value
    with pytest.raises(SetupError):
        await helper.prepare("OFFLINE wallet")
    assert not any(r.method == "POST" for r in exchange.requests)


async def test_requested_transfer_is_not_applied_and_pending_transfer_blocks_another(setup_case):
    helper, exchange, _, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    await helper.open_wallet(journal)
    def before_send(request):
        if request.url.path.endswith("/transfer"):
            saved = json.loads(journal.read_text())
            transfer = saved["transfers"][-1]
            assert transfer["state"] == "unknown"
            assert json.loads(request.content)["clientTransferId"] == transfer["client_transfer_id"]
    exchange.before_mutation = before_send
    await helper.fund(journal, "1.23456")
    state = json.loads(journal.read_text())
    assert state["transfers"][-1]["state"] == "Requested"
    with pytest.raises(SetupError):
        await helper.fund(journal, "2")
    requests = [r for r in exchange.requests if r.method == "POST" and r.url.path.endswith("/transfer")]
    assert len(requests) == 1 and json.loads(requests[0].content)["amount"] == "1.23456"
    row = next(iter(exchange.transfers.values()))
    row["status"] = "Applied"
    exchange.wallet_balance = "1.23456"
    await helper.recover_funding(journal, state["transfers"][-1]["client_transfer_id"])
    assert json.loads(journal.read_text())["transfers"][-1]["state"] == "Applied"


async def test_unknown_transfer_recovery_never_changes_id_amount_or_wallet(setup_case):
    helper, exchange, _, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    await helper.open_wallet(journal)
    exchange.transfer_timeout = True
    with pytest.raises(SetupError):
        await helper.fund(journal, "2.00001")
    saved = json.loads(journal.read_text())["transfers"][-1]
    assert saved["state"] == "unknown"
    helper = restarted(helper)
    with pytest.raises(SetupError):
        await helper.fund(journal, "3")
    exchange.transfer_timeout = False
    exchange.cash = "0.00000"  # The original transfer may already have debited it.
    next(iter(exchange.transfers.values()))["status"] = "Applied"
    exchange.wallet_balance = "2.00001"
    await helper.recover_funding(journal, saved["client_transfer_id"])
    recovered = json.loads(journal.read_text())["transfers"][-1]
    assert recovered["state"] == "unknown" and recovered["transfer_id"]
    # An idempotent POST replay recovers identity; only scoped GET proves finality.
    await helper.recover_funding(journal, saved["client_transfer_id"])
    assert json.loads(journal.read_text())["transfers"][-1]["state"] == "Applied"
    requests = [r for r in exchange.requests if r.method == "POST" and r.url.path.endswith("/transfer")]
    assert len(requests) == 2 and len({r.url.path for r in requests}) == 1
    assert all(r.content == requests[0].content for r in requests)
    assert len(exchange.transfers) == 1


@pytest.mark.parametrize("cash,amount", [("0.00000", "0.00001"), ("1.00000", "1.00001"), (1.2, "1"), ("NaN", "1")])
async def test_insufficient_or_untrustworthy_cash_prevents_transfer(setup_case, cash, amount):
    helper, exchange, _, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    await helper.open_wallet(journal)
    exchange.cash = cash
    with pytest.raises(SetupError):
        await helper.fund(journal, amount)
    assert not any(r.method == "POST" and r.url.path.endswith("/transfer") for r in exchange.requests)


async def test_private_key_directory_inside_git_is_rejected_before_any_request(tmp_path):
    # Deliberately does not use setup_case's simulated isolated-directory patch.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    helper = NovigSetup(object(), checkout / "journals", secret_dir=checkout / "secrets", protect=lambda path: None)
    with pytest.raises(SetupError, match="private_keys_must_be_outside_git_checkout"):
        await helper.prepare("OFFLINE wallet")
    assert not (checkout / "secrets").exists()


async def test_confirmation_refusal_never_opens_or_funds(setup_case):
    helper, exchange, confirmation, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    helper.confirmation = TTYConfirmation(input_stream=io.StringIO("yes"), output_stream=Terminal())
    with pytest.raises(SetupError, match="interactive_terminal_required"):
        await helper.open_wallet(journal)
    assert json.loads(journal.read_text())["open_state"] == "prepared"
    assert not any(r.method == "POST" for r in exchange.requests)
    helper.confirmation = confirmation
    await helper.open_wallet(journal)
    helper.confirmation = TTYConfirmation(input_stream=Terminal(), output_stream=Terminal(), read_line=lambda: "yes")
    with pytest.raises(SetupError, match="operator_confirmation_did_not_match"):
        await helper.fund(journal, "1")
    assert json.loads(journal.read_text())["transfers"] == []
    assert not any(r.url.path.endswith("/transfer") for r in exchange.requests)


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/v3/account/subaccounts", {"label": "OFFLINE"}),
    ("POST", f"/v3/account/subaccounts/{WALLET_ID}/transfer", {"amount": "1.00000"}),
    ("POST", "/v3/orders", {}), ("DELETE", "/v3/keys/" + WALLET_ID, None),
    ("GET", "/v3/account/transfers", None),
])
async def test_transport_refuses_unapproved_mutations_and_unneeded_routes(setup_case, method, path, body):
    helper, exchange, _, _ = setup_case
    with pytest.raises(SetupError):
        await helper.transport.request(method, path, body=body)
    assert exchange.requests == []


@pytest.mark.parametrize("field,value", [("environment", "paper"), ("management_key_id", OTHER_ID),
    ("management_fingerprint", "sha256:" + "b" * 64), ("private_key_path", "C:/OFFLINE/wrong.pem"),
    ("trading_fingerprint", "sha256:" + "c" * 64)])
async def test_tampered_journal_cannot_open_wallet(setup_case, field, value):
    helper, exchange, _, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    state = json.loads(journal.read_text())
    state[field] = value
    journal.write_text(json.dumps(state))
    with pytest.raises(SetupError):
        await helper.open_wallet(journal)
    assert not any(r.method == "POST" for r in exchange.requests)


async def test_concurrent_helpers_cannot_mutate_same_setup_directory(setup_case):
    helper, exchange, _, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    second = restarted(helper)
    with helper._locked():
        with pytest.raises(SetupError, match="another_setup_operation_is_running"):
            await second.open_wallet(journal)
    assert not any(r.method == "POST" for r in exchange.requests)
    assert json.loads(journal.read_text())["open_state"] == "prepared"


@pytest.mark.parametrize("terminal", ["Applied", "Rejected"])
async def test_terminal_post_ack_requires_scoped_get_before_final_state(setup_case, terminal):
    helper, exchange, _, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    await helper.open_wallet(journal)
    def before_send(request):
        if request.url.path.endswith("/transfer"):
            body = json.loads(request.content)
            exchange.transfers[body["clientTransferId"]] = {"transferId": str(UUID(int=700)),
                "clientTransferId": body["clientTransferId"], "amount": body["amount"],
                "direction": "fund", "status": terminal, "actualBalance": None,
                "createdAt": "2026-10-21T00:00:00Z", "updatedAt": "2026-10-21T00:00:00Z"}
    exchange.before_mutation = before_send
    result = await helper.fund(journal, "5")
    assert result["transfers"][-1]["state"] == "unknown"
    saved = json.loads(journal.read_text())["transfers"][-1]
    assert saved["transfer_id"] == str(UUID(int=700))
    await restarted(helper).recover_funding(journal, saved["client_transfer_id"])
    assert json.loads(journal.read_text())["transfers"][-1]["state"] == terminal
    assert len([r for r in exchange.requests if r.method == "POST" and r.url.path.endswith("/transfer")]) == 1


async def test_changed_terminal_transfer_cannot_overwrite_durable_state(setup_case):
    helper, exchange, _, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    await helper.open_wallet(journal)
    await helper.fund(journal, "1")
    row = next(iter(exchange.transfers.values()))
    row["status"] = "Applied"
    await helper.recover_funding(journal, row["clientTransferId"])
    committed = journal.read_bytes()
    row["status"] = "Rejected"
    with pytest.raises(SetupError, match="terminal_transfer_state_changed"):
        await helper.recover_funding(journal, row["clientTransferId"])
    assert journal.read_bytes() == committed


async def test_export_preserves_other_credentials_disables_trading_and_never_activates(setup_case):
    helper, exchange, confirmation, tmp_path = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    await helper.open_wallet(journal)
    env = tmp_path / "offline.env"
    env.write_text("# OFFLINE fixture\nNOVIG_API_KEY='offline-paper-key'\nOTHER_SERVICE_KEY='offline-other'\nEV_AUTO_TRADE=true\nEV_LIVE_ENABLED=true\n")
    before_confirmations = len(confirmation.requests)
    result = await helper.export(journal, env)
    values = dotenv_values(env)
    assert values["NOVIG_API_KEY"] == "offline-paper-key" and values["OTHER_SERVICE_KEY"] == "offline-other"
    assert values["NOVIG_PRODUCTION_TRADING_API_KEY"] == WALLET_ID
    assert values["NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH"] == json.loads(journal.read_text())["private_key_path"]
    assert values["EV_AUTO_TRADE"] == values["EV_LIVE_ENABLED"] == "false"
    assert not result["trading_enabled"] and not result["restart_or_monitor_switch_performed"]
    assert "PRIVATE KEY" not in env.read_text() and "PRIVATE KEY" not in json.dumps(result)
    assert len(confirmation.requests) == before_confirmations
    assert len([r for r in exchange.requests if r.method == "POST"]) == 1


async def test_export_does_not_replace_another_production_wallet(setup_case):
    helper, _, _, tmp_path = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    await helper.open_wallet(journal)
    env = tmp_path / "offline.env"
    original = ("NOVIG_PRODUCTION_TRADING_API_KEY=" + OTHER_ID + "\nEV_AUTO_TRADE=false\n").encode()
    env.write_bytes(original)
    with pytest.raises(SetupError, match="different_production_trading_pair_already_configured"):
        await helper.export(journal, env)
    assert env.read_bytes() == original


async def test_wallet_changed_during_confirmation_blocks_funding(setup_case):
    helper, exchange, confirmation, _ = setup_case
    journal = await helper.prepare("OFFLINE wallet")
    await helper.open_wallet(journal)
    original_confirmation = confirmation.require
    def changed_during_prompt(phrase, description):
        original_confirmation(phrase, description)
        exchange.keys[WALLET_ID]["scope"] = "trading::read"
    confirmation.require = changed_during_prompt
    with pytest.raises(SetupError, match="trading_key_identity_or_scope_mismatch"):
        await helper.fund(journal, "1")
    assert json.loads(journal.read_text())["transfers"] == []
    assert not any(r.method == "POST" and r.url.path.endswith("/transfer") for r in exchange.requests)


async def test_transport_confirmation_binds_exact_request_expires_and_is_single_use(setup_case):
    helper, exchange, _, _ = setup_case
    path = f"/v3/account/subaccounts/{WALLET_ID}/transfer"
    body = {"amount": "1.00000", "direction": "fund", "clientTransferId": "ev-" + "a" * 32}
    expired = _Approval("POST", path, body)
    expired.expires = 0
    with pytest.raises(SetupError, match="operator_confirmation_expired_or_changed"):
        await helper.transport.request("POST", path, body=body, approval=expired)
    changed = _Approval("POST", path, body)
    with pytest.raises(SetupError, match="operator_confirmation_expired_or_changed"):
        await helper.transport.request("POST", path, body={**body, "amount": "2.00000"}, approval=changed)
    with pytest.raises(SetupError, match="operator_confirmation_expired_or_changed"):
        await helper.transport.request("POST", path.replace(WALLET_ID, OTHER_ID), body=body, approval=changed)
    assert exchange.requests == []
    approval = _Approval("POST", path, body)
    await helper.transport.request("POST", path, body=body, approval=approval)
    with pytest.raises(SetupError, match="operator_confirmation_expired_or_changed"):
        await helper.transport.request("POST", path, body=body, approval=approval)
    assert len(exchange.requests) == 1 and len(exchange.transfers) == 1
