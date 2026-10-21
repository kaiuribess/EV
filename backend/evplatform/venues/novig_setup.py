"""Operator-run Novig wallet setup, separate from the trading runtime.

The default operation is read-only. This module has no order, cancellation,
withdrawal, credential rotation, or live-enablement route. Financial mutations
require an interactive operator and a durable journal before transmission.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from .novig import NovigClient, NovigSettings


class SetupError(RuntimeError):
    """Only fixed local classifications, never server response text."""


class SetupHTTPError(SetupError):
    def __init__(self, status: int):
        self.status_code = status
        super().__init__(f"setup_http_{status}")


def _uuid(value) -> str:
    if not isinstance(value, str):
        raise SetupError("invalid_identifier")
    try:
        parsed = str(UUID(value))
    except ValueError:
        raise SetupError("invalid_identifier") from None
    if parsed != value.lower():
        raise SetupError("invalid_identifier")
    return parsed


def _stamp(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (ValueError, AttributeError, TypeError):
        raise SetupError("invalid_timestamp") from None


def validate_amount(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,5})?", value):
        raise SetupError("positive_decimal_amount_with_at_most_five_places_required")
    amount = Decimal(value)
    if amount <= 0:
        raise SetupError("positive_decimal_amount_required")
    return format(amount, ".5f")


def _money(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]{5}", value):
        raise SetupError("invalid_documented_balance")
    return value


def _fingerprint(key) -> str:
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256:" + hashlib.sha256(public).hexdigest()


def _key(row) -> dict:
    fields = {"keyId", "name", "fingerprint", "algorithm", "scope", "restriction", "createdAt", "lastUsedAt", "expiresAt"}
    if not isinstance(row, dict) or set(row) != fields:
        raise SetupError("invalid_documented_key_metadata")
    _uuid(row["keyId"])
    if (row["scope"] not in {"management", "management::read", "trading", "trading::read"}
            or row["algorithm"] not in {"Ed25519", "P-256"}
            or not isinstance(row["name"], str)
            or not isinstance(row["fingerprint"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", row["fingerprint"])
            or row["restriction"] is not None and not isinstance(row["restriction"], str)
            or row["lastUsedAt"] is not None):
        raise SetupError("invalid_documented_key_metadata")
    _stamp(row["createdAt"])
    if row["expiresAt"] is not None and _stamp(row["expiresAt"]) <= datetime.now(timezone.utc):
        raise SetupError("key_expired")
    return row


def _wallet(row) -> dict:
    if not isinstance(row, dict) or set(row) != {"keyId", "label", "balance"} or not isinstance(row["label"], str):
        raise SetupError("invalid_documented_wallet")
    _uuid(row["keyId"])
    _money(row["balance"])
    return row


def _rows(value, validate):
    if not isinstance(value, list):
        raise SetupError("documented_array_required")
    rows = [validate(row) for row in value]
    if len({row["keyId"] for row in rows}) != len(rows):
        raise SetupError("duplicate_remote_identity")
    return rows


def _balance(value, key_id=None) -> str:
    fields = {"balance"} if key_id is None else {"balance", "keyId"}
    if not isinstance(value, dict) or set(value) != fields or key_id is not None and value.get("keyId") != key_id:
        raise SetupError("wallet_identity_mismatch")
    return _money(value["balance"])


class TTYConfirmation:
    def __init__(self, input_stream=None, output_stream=None, read_line=None):
        self.input_stream = input_stream if input_stream is not None else sys.stdin
        self.output_stream = output_stream if output_stream is not None else sys.stdout
        self.read_line = read_line

    def require(self, phrase: str, description: str):
        if not self.input_stream.isatty() or not self.output_stream.isatty():
            raise SetupError("interactive_terminal_required")
        self.output_stream.write(description + "\nType exactly: " + phrase + "\n> ")
        self.output_stream.flush()
        value = self.read_line() if self.read_line else self.input_stream.readline()
        if value.rstrip("\r\n") != phrase:
            raise SetupError("operator_confirmation_did_not_match")


class _Approval:
    def __init__(self, method, path, body):
        self.binding = (method, path, json.dumps(body, sort_keys=True))
        self.expires = time.monotonic() + 120
        self.used = False

    def consume(self, method, path, body):
        if self.used or time.monotonic() >= self.expires or self.binding != (method, path, json.dumps(body, sort_keys=True)):
            raise SetupError("operator_confirmation_expired_or_changed")
        self.used = True


class NovigSetupTransport:
    """Dedicated array-aware signer; never extends the runtime's write surface."""
    def __init__(self, settings: NovigSettings, http: httpx.AsyncClient | None = None):
        if settings.environment != "production":
            raise SetupError("production_management_credentials_required")
        _uuid(settings.api_key)
        self.http = http or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self.owns_http = http is None
        self.client = NovigClient(settings, self.http)
        self._lock = asyncio.Lock()
        self._next_request = 0.0

    @property
    def settings(self):
        return self.client.settings

    async def close(self):
        if self.owns_http:
            await self.http.aclose()

    async def request(self, method, path, *, body=None, approval=None):
        ident = r"[0-9a-fA-F-]{36}"
        permitted_get = (path in {"/v3/keys", "/v3/account/balance", "/v3/account/subaccounts"}
            or re.fullmatch(rf"/v3/keys/{ident}", path)
            or re.fullmatch(rf"/v3/account/subaccounts/{ident}/balance", path)
            or re.fullmatch(rf"/v3/account/subaccounts/{ident}/transfers/{ident}", path))
        permitted_post = path == "/v3/account/subaccounts" or re.fullmatch(rf"/v3/account/subaccounts/{ident}/transfer", path)
        if not (method == "GET" and permitted_get and body is None or method == "POST" and permitted_post and isinstance(body, dict)):
            raise SetupError("setup_route_not_allowed")
        if method == "POST" and not isinstance(approval, _Approval):
            raise SetupError("interactive_confirmation_required")
        async with self._lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request = time.monotonic() + .5
            raw = b"" if body is None else json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
            headers = self.client.headers(method, path, body=raw)
            request = self.http.build_request(method, "https://api.novig.com" + path, content=raw, headers=headers)
            if request.url.host != "api.novig.com" or request.url.scheme != "https" or request.url.query:
                raise SetupError("setup_route_not_allowed")
            if method == "POST":
                approval.consume(method, path, body)
            try:
                response = await self.http.send(request, follow_redirects=False)
            except httpx.TransportError:
                raise SetupError("setup_transport_failed_outcome_unverified") from None
            expected = 200 if method == "GET" else 201 if path == "/v3/account/subaccounts" else 202
            if response.status_code != expected:
                raise SetupHTTPError(response.status_code)
            try:
                parsed = response.json(parse_float=Decimal)
            except ValueError:
                raise SetupError("invalid_setup_json") from None
            if not isinstance(parsed, (dict, list)):
                raise SetupError("invalid_setup_response")
            return parsed


def _reject_links(path: Path):
    for item in (path, *path.parents):
        if item.is_symlink() or item.exists() and getattr(item.stat(), "st_file_attributes", 0) & 0x400:
            raise SetupError("private_storage_links_not_allowed")


def protect_private_path(path: Path):
    """Replace Windows ACL with the current user and SYSTEM, then verify it."""
    _reject_links(path)
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    script = r'''
$ErrorActionPreference = 'Stop'
$target = $env:EV_SETUP_ACL_TARGET
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User
$system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$item = Get-Item -LiteralPath $target -Force
if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse' }
if ($item.PSIsContainer) { $acl = [Security.AccessControl.DirectorySecurity]::new() }
else { $acl = [Security.AccessControl.FileSecurity]::new() }
$acl.SetOwner($user)
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($user, $system)) {
  if ($item.PSIsContainer) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
  } else { $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow') }
  $acl.AddAccessRule($rule)
}
$item.SetAccessControl($acl)
$check = $item.GetAccessControl()
if (!$check.AreAccessRulesProtected -or $check.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $user.Value) { throw 'acl' }
$entries = @($check.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
if ($entries.Count -lt 1 -or $entries.Count -gt 2) { throw 'acl_count' }
foreach ($entry in $entries) {
  if ($entry.IdentityReference.Value -notin @($user.Value, $system.Value) -or $entry.AccessControlType -ne 'Allow' -or $entry.IsInherited -or $entry.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl) { throw 'acl_rule' }
}
'''
    env = dict(os.environ, EV_SETUP_ACL_TARGET=str(path.absolute()))
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
            base64.b64encode(script.encode("utf-16le")).decode()], env=env, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError("private_storage_acl_failed") from None
    if result.returncode:
        raise SetupError("private_storage_acl_failed")


def _atomic(path: Path, content: bytes, protect):
    _reject_links(path)
    # Restrict an empty staging directory first. Even a newly opened handle to
    # the temp file must never inherit the broader permissions of an env-file
    # parent directory before its own ACL is applied.
    staging = path.parent / (".novig-setup-" + uuid4().hex)
    staging.mkdir()
    temp = staging / "pending"
    try:
        protect(staging)
        with temp.open("xb") as handle:
            protect(temp)  # Empty file is protected before any secret is written.
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        protect(path)
    finally:
        if temp.exists():
            temp.unlink()
        staging.rmdir()


class FundingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_transfer_id: str = Field(pattern=r"^ev-[0-9a-f]{32}$")
    amount: str = Field(pattern=r"^[0-9]+\.[0-9]{5}$")
    state: Literal["unknown", "Requested", "Applied", "Rejected"] = "unknown"
    transfer_id: str | None = None


class SetupJournal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    environment: Literal["production"] = "production"
    setup_id: str
    management_key_id: str = Field(repr=False)
    management_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    label: str = Field(min_length=1, max_length=64)
    private_key_path: str = Field(repr=False)
    trading_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    open_state: Literal["prepared", "unknown", "verified"] = "prepared"
    trading_key_id: str | None = Field(default=None, repr=False)
    transfers: list[FundingRecord] = Field(default_factory=list)


class NovigSetup:
    def __init__(self, transport: NovigSetupTransport, setup_dir: Path, *, secret_dir: Path | None = None,
                 confirmation: TTYConfirmation | None = None, protect=protect_private_path):
        self.transport = transport
        self.setup_dir = Path(setup_dir).absolute()
        default = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "EVPlatform" / "secrets" / "novig"
        self.secret_dir = Path(secret_dir or default).absolute()
        self.confirmation = confirmation or TTYConfirmation()
        self.protect = protect

    def _private_directories(self):
        for path in (self.setup_dir, self.secret_dir):
            _reject_links(path)
        if any((parent / ".git").exists() for parent in (self.secret_dir, *self.secret_dir.parents)):
            raise SetupError("private_keys_must_be_outside_git_checkout")
        for path in (self.setup_dir, self.secret_dir):
            path.mkdir(parents=True, exist_ok=True)
            self.protect(path)

    @contextmanager
    def _locked(self):
        self._private_directories()
        lock_path = self.setup_dir / ".operation.lock"
        _reject_links(lock_path)
        with lock_path.open("a+b") as handle:
            self.protect(lock_path)
            if lock_path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SetupError("another_setup_operation_is_running") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    async def _management(self, state=None):
        key_id = self.transport.settings.api_key
        row = _key(await self.transport.request("GET", f"/v3/keys/{key_id}"))
        signer = self.transport.client._load_key()
        fingerprint = _fingerprint(signer)
        algorithm = "Ed25519" if isinstance(signer, ed25519.Ed25519PrivateKey) else "P-256" if (
            isinstance(signer, ec.EllipticCurvePrivateKey) and isinstance(signer.curve, ec.SECP256R1)) else None
        if (row["keyId"] != key_id or row["scope"] != "management" or row["fingerprint"] != fingerprint
                or row["restriction"] is not None or row["algorithm"] != algorithm):
            raise SetupError("management_identity_or_signer_mismatch")
        if state and (state.management_key_id != key_id or state.management_fingerprint != fingerprint):
            raise SetupError("journal_belongs_to_another_management_identity")
        return row

    async def status(self):
        await self._management()
        balance = _balance(await self.transport.request("GET", "/v3/account/balance"))
        keys = _rows(await self.transport.request("GET", "/v3/keys"), _key)
        wallets = _rows(await self.transport.request("GET", "/v3/account/subaccounts"), _wallet)
        return {"environment": "production", "management_verified": True, "primary_cash_usd": balance,
            "live_trading_keys": sum(row["scope"] == "trading" for row in keys), "live_subaccounts": len(wallets),
            "subaccount_balances_usd": [row["balance"] for row in wallets], "trading_enabled": False}

    async def prepare(self, label: str) -> Path:
        if not isinstance(label, str) or not label.strip() or len(label) > 64 or any(ord(c) < 32 for c in label):
            raise SetupError("label_must_be_one_to_64_printable_characters")
        with self._locked():
            management = await self._management()
            setup_id = str(uuid4())
            directory = self.setup_dir / setup_id
            private_directory = self.secret_dir / setup_id
            for path in (directory, private_directory):
                path.mkdir()
                self.protect(path)
            key = ed25519.Ed25519PrivateKey.generate()
            private_path = private_directory / "trading.pem"
            _atomic(private_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()), self.protect)
            state = SetupJournal(setup_id=setup_id, management_key_id=management["keyId"],
                management_fingerprint=management["fingerprint"], label=label, private_key_path=str(private_path),
                trading_fingerprint=_fingerprint(key))
            journal = directory / "journal.json"
            self._save(journal, state)
            return journal

    def _save(self, path, state):
        _atomic(Path(path), state.model_dump_json(indent=2).encode(), self.protect)

    def _load(self, path):
        path = Path(path).absolute()
        _reject_links(path)
        if path.name != "journal.json" or path.parent.parent != self.setup_dir:
            raise SetupError("journal_outside_setup_directory")
        try:
            state = SetupJournal.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise SetupError("invalid_setup_journal") from None
        if _uuid(state.setup_id) != path.parent.name or _uuid(state.management_key_id) != self.transport.settings.api_key:
            raise SetupError("journal_identity_mismatch")
        if state.trading_key_id is not None:
            _uuid(state.trading_key_id)
        if len({item.client_transfer_id for item in state.transfers}) != len(state.transfers):
            raise SetupError("duplicate_transfer_journal")
        for transfer in state.transfers:
            validate_amount(transfer.amount)
            if transfer.transfer_id is not None:
                _uuid(transfer.transfer_id)
        private_path = self.secret_dir / state.setup_id / "trading.pem"
        if Path(state.private_key_path) != private_path:
            raise SetupError("private_signer_path_mismatch")
        _reject_links(private_path)
        self.protect(private_path)
        try:
            signer = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        except (ValueError, OSError, TypeError):
            raise SetupError("private_signer_unavailable") from None
        if not isinstance(signer, ed25519.Ed25519PrivateKey) or _fingerprint(signer) != state.trading_fingerprint:
            raise SetupError("private_signer_fingerprint_mismatch")
        return state, signer

    async def _verified_wallet(self, state):
        await self._management(state)
        if state.trading_key_id is None:
            raise SetupError("trading_wallet_identity_unresolved")
        row = _key(await self.transport.request("GET", f"/v3/keys/{state.trading_key_id}"))
        if (row["keyId"] != state.trading_key_id or row["scope"] != "trading"
                or row["fingerprint"] != state.trading_fingerprint or row["algorithm"] != "Ed25519"
                or row["restriction"] is None):
            raise SetupError("trading_key_identity_or_scope_mismatch")
        wallets = _rows(await self.transport.request("GET", "/v3/account/subaccounts"), _wallet)
        if len([row for row in wallets if row["keyId"] == state.trading_key_id]) != 1:
            raise SetupError("trading_key_has_no_live_wallet")
        endpoint = f"/v3/account/subaccounts/{state.trading_key_id}/balance"
        balance = _balance(await self.transport.request("GET", endpoint), state.trading_key_id)
        # A signed personal read proves the saved private half actually reaches
        # the same wallet; a management label is never an account identifier.
        personal = NovigSetupTransport(NovigSettings(environment="production", api_key=state.trading_key_id,
            private_key_path=state.private_key_path), http=self.transport.http)
        personal_balance = _balance(await personal.request("GET", endpoint), state.trading_key_id)
        if personal_balance != balance:
            raise SetupError("wallet_changed_during_verification")
        return balance

    async def open_wallet(self, journal_path):
        with self._locked():
            state, signer = self._load(journal_path)
            if state.open_state != "prepared":
                raise SetupError("wallet_open_already_attempted_use_reconcile_never_retry")
            await self._management(state)
            keys = _rows(await self.transport.request("GET", "/v3/keys"), _key)
            if any(row["fingerprint"] == state.trading_fingerprint for row in keys):
                raise SetupError("public_key_already_registered_use_reconcile")
            body = {"label": state.label, "algorithm": "Ed25519", "publicKey": signer.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()}
            self.confirmation.require("OPEN NOVIG PRODUCTION WALLET",
                f"Create one real Novig trading subaccount for label {ascii(state.label)}, setup {state.setup_id}, signer ending {state.trading_fingerprint[-12:]}. Its initial balance is zero. No funding or orders will be sent.")
            await self._management(state)
            approval = _Approval("POST", "/v3/account/subaccounts", body)
            state.open_state = "unknown"
            self._save(journal_path, state)
            row = _wallet(await self.transport.request("POST", "/v3/account/subaccounts", body=body, approval=approval))
            if row["label"] != state.label or Decimal(row["balance"]) != 0:
                raise SetupError("wallet_open_response_mismatch_reconcile_required")
            state.trading_key_id = row["keyId"]
            self._save(journal_path, state)
            balance = await self._verified_wallet(state)
            state.open_state = "verified"
            self._save(journal_path, state)
            return self._summary(state, balance)

    async def reconcile(self, journal_path):
        with self._locked():
            state, _ = self._load(journal_path)
            await self._management(state)
            keys = _rows(await self.transport.request("GET", "/v3/keys"), _key)
            matches = [row for row in keys if row["fingerprint"] == state.trading_fingerprint and row["scope"] == "trading"]
            if not matches:
                return {**self._summary(state), "reconciliation": "unresolved_no_matching_trading_key_do_not_reopen"}
            if len(matches) != 1 or state.trading_key_id not in {None, matches[0]["keyId"]}:
                raise SetupError("ambiguous_trading_identity")
            state.trading_key_id = matches[0]["keyId"]
            balance = await self._verified_wallet(state)
            state.open_state = "verified"
            self._save(journal_path, state)
            return self._summary(state, balance)

    def _summary(self, state, balance=None):
        return {"environment": "production", "setup_id": state.setup_id, "open_state": state.open_state,
            "wallet_balance_usd": balance, "trading_enabled": False,
            "transfers": [{"client_transfer_id": row.client_transfer_id, "amount_usd": row.amount,
                "state": row.state} for row in state.transfers]}

    def _accept_transfer(self, raw, record):
        required = {"transferId", "status", "direction", "amount", "actualBalance", "clientTransferId", "createdAt", "updatedAt"}
        if not isinstance(raw, dict) or set(raw) != required:
            raise SetupError("invalid_documented_transfer")
        transfer_id = _uuid(raw["transferId"])
        if (raw["status"] not in {"Requested", "Applied", "Rejected"} or raw["direction"] != "fund"
                or _money(raw["amount"]) != record.amount or raw["clientTransferId"] != record.client_transfer_id
                or record.transfer_id not in {None, transfer_id}
                or _stamp(raw["updatedAt"]) < _stamp(raw["createdAt"])):
            raise SetupError("transfer_identity_or_amount_mismatch")
        if raw["actualBalance"] is not None:
            _money(raw["actualBalance"])
        if record.state in {"Applied", "Rejected"} and raw["status"] != record.state:
            raise SetupError("terminal_transfer_state_changed")
        record.transfer_id, record.state = transfer_id, raw["status"]

    async def fund(self, journal_path, amount: str):
        amount = validate_amount(amount)
        with self._locked():
            state, _ = self._load(journal_path)
            if state.open_state != "verified":
                raise SetupError("verified_wallet_required")
            if any(row.state in {"unknown", "Requested"} for row in state.transfers):
                raise SetupError("pending_transfer_use_recover_funding")
            await self._verified_wallet(state)
            self.confirmation.require(f"FUND NOVIG {amount}",
                f"Transfer exactly ${amount} from your real primary Novig cash wallet into label {ascii(state.label)}, setup {state.setup_id}, signer ending {state.trading_fingerprint[-12:]}. No order will be placed.")
            await self._verified_wallet(state)
            cash = _balance(await self.transport.request("GET", "/v3/account/balance"))
            if Decimal(amount) > Decimal(cash):
                raise SetupError("funding_exceeds_fresh_primary_cash")
            record = FundingRecord(client_transfer_id="ev-" + uuid4().hex, amount=amount)
            state.transfers.append(record)
            self._save(journal_path, state)
            await self._send_transfer(journal_path, state, record)
            return self._summary(state)

    async def _send_transfer(self, journal_path, state, record):
        path = f"/v3/account/subaccounts/{state.trading_key_id}/transfer"
        body = {"direction": "fund", "amount": record.amount, "clientTransferId": record.client_transfer_id}
        # The stable ID and exact amount were durably saved before dispatch.
        # Any timeout, malformed reply or interrupted process remains unknown.
        raw = await self.transport.request("POST", path, body=body, approval=_Approval("POST", path, body))
        acknowledgement = record.model_copy(deep=True)
        self._accept_transfer(raw, acknowledgement)
        record.transfer_id = acknowledgement.transfer_id
        # Preserve a validated ID even if a replay reports a terminal state,
        # but only a subsequent scoped GET proves that money actually moved.
        record.state = "Requested" if acknowledgement.state == "Requested" else "unknown"
        self._save(journal_path, state)

    async def recover_funding(self, journal_path, client_transfer_id: str):
        with self._locked():
            state, _ = self._load(journal_path)
            records = [row for row in state.transfers if row.client_transfer_id == client_transfer_id]
            if len(records) != 1 or state.open_state != "verified":
                raise SetupError("known_funding_journal_required")
            record = records[0]
            await self._verified_wallet(state)
            if record.transfer_id:
                endpoint = f"/v3/account/subaccounts/{state.trading_key_id}/transfers/{record.transfer_id}"
                self._accept_transfer(await self.transport.request("GET", endpoint), record)
                self._save(journal_path, state)
            else:
                if record.state != "unknown":
                    raise SetupError("transfer_journal_inconsistent")
                self.confirmation.require(f"RECOVER NOVIG {record.amount}",
                    f"Recover the unknown ${record.amount} funding request for label {ascii(state.label)}, setup {state.setup_id}, signer ending {state.trading_fingerprint[-12:]}, using its SAME saved idempotency ID. If the original request never arrived, this can submit that exact transfer now.")
                await self._verified_wallet(state)
                _balance(await self.transport.request("GET", "/v3/account/balance"))
                # Do not reject a lower cash balance here: the original transfer
                # may already have debited it. Same-ID recovery cannot add one.
                self._save(journal_path, state)
                await self._send_transfer(journal_path, state, record)
            return self._summary(state)

    async def export(self, journal_path, env_file: Path):
        with self._locked():
            state, _ = self._load(journal_path)
            if state.open_state != "verified":
                raise SetupError("verified_wallet_required")
            balance = await self._verified_wallet(state)
            path = Path(env_file).absolute()
            _reject_links(path)
            if not path.is_file():
                raise SetupError("existing_env_file_required")
            self.protect(path)
            original = path.read_bytes()
            values = dotenv_values(path)
            changes = {"NOVIG_PRODUCTION_TRADING_API_KEY": state.trading_key_id,
                "NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH": state.private_key_path,
                "EV_AUTO_TRADE": "false", "EV_LIVE_ENABLED": "false"}
            # Reject contradictory production pairs instead of replacing another
            # wallet. Legacy/paper values and all other settings stay untouched.
            for name in ("NOVIG_PRODUCTION_TRADING_API_KEY", "NOVIG_PRODUCTION_TRADING_PRIVATE_KEY_PATH"):
                if values.get(name) not in {None, "", changes[name]}:
                    raise SetupError("different_production_trading_pair_already_configured")
            lines = original.decode("utf-8-sig").splitlines()
            output, seen = [], set()
            for line in lines:
                match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
                name = match.group(1) if match else None
                if name in changes:
                    if name in seen:
                        raise SetupError("duplicate_target_env_setting")
                    output.append(name + "='" + changes[name].replace("\\", "\\\\").replace("'", "\\'") + "'")
                    seen.add(name)
                else:
                    output.append(line)
            for name, value in changes.items():
                if name not in seen:
                    output.append(name + "='" + value.replace("\\", "\\\\").replace("'", "\\'") + "'")
            if path.read_bytes() != original:
                raise SetupError("environment_file_changed_during_export")
            _atomic(path, ("\n".join(output) + "\n").encode(), self.protect)
            return {**self._summary(state, balance), "production_pair_exported": True,
                "legacy_credentials_preserved": True, "restart_or_monitor_switch_performed": False}
