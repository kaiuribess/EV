"""Real Windows filesystem checks; no account/network actions or real keys."""
import json
import os
import subprocess

import pytest

from evplatform.venues.novig_setup import SetupError, _atomic, protect_private_path


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration")


def assert_private_acl(path):
    # Independently inspect through .NET; implementation uses Win32 ctypes.
    script = r"""
$ErrorActionPreference = 'Stop'
$target = $env:EV_TEST_ACL_TARGET
$sections = [Security.AccessControl.AccessControlSections]::Owner -bor [Security.AccessControl.AccessControlSections]::Access
if ([IO.Directory]::Exists($target)) { $acl = [IO.Directory]::GetAccessControl($target, $sections) }
else { $acl = [IO.File]::GetAccessControl($target, $sections) }
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$entries = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
$bad = @($entries | Where-Object {
    $_.IdentityReference.Value -notin @($user, 'S-1-5-18') -or $_.IsInherited -or
    $_.AccessControlType -ne 'Allow' -or $_.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl
})
@{ protected = $acl.AreAccessRulesProtected; owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -eq $user;
   count = $entries.Count; invalid = $bad.Count } | ConvertTo-Json -Compress
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=dict(os.environ, EV_TEST_ACL_TARGET=str(path)), capture_output=True, text=True,
        timeout=15, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    assert json.loads(result.stdout) == {"protected": True, "owner": True, "count": 2, "invalid": 0}


def test_actual_windows_acl_protection_needs_no_shell_or_script_policy(tmp_path, monkeypatch):
    directory = tmp_path / "private directory"
    directory.mkdir()
    file = directory / "offline.txt"
    file.write_text("OFFLINE fixture")
    def forbidden_shell(*args, **kwargs):
        raise AssertionError("Protection must not launch PowerShell or any subprocess")
    with monkeypatch.context() as patch:
        patch.setenv("PSExecutionPolicyPreference", "Restricted")
        patch.setenv("PATH", "")
        patch.setattr(subprocess, "run", forbidden_shell)
        for path in (directory, file, directory, file):
            protect_private_path(path)
    assert_private_acl(directory)
    assert_private_acl(file)
    assert file.read_text() == "OFFLINE fixture"


def test_actual_atomic_write_preserves_private_windows_acl(tmp_path):
    directory = tmp_path / "atomic"
    directory.mkdir()
    protect_private_path(directory)
    target = directory / "offline.txt"
    for content in (b"OFFLINE initial", b"OFFLINE replacement"):
        _atomic(target, content, protect_private_path)
        assert target.read_bytes() == content
        assert_private_acl(target)
    assert [entry.name for entry in directory.iterdir()] == ["offline.txt"]


def test_windows_acl_failure_is_redacted_and_blocks_write(tmp_path, monkeypatch):
    from evplatform.venues import windows_private_storage
    def denied(path):
        raise OSError("OFFLINE sensitive diagnostic must not surface")
    monkeypatch.setattr(windows_private_storage, "protect_windows_path", denied)
    target = tmp_path / "must-not-write.txt"
    with pytest.raises(SetupError, match="^private_storage_acl_failed$"):
        _atomic(target, b"OFFLINE payload", protect_private_path)
    assert not target.exists()


def test_windows_acl_failure_preserves_safe_operation_and_numeric_code(tmp_path, monkeypatch):
    from evplatform.venues import windows_private_storage
    def denied(path):
        raise windows_private_storage.WindowsStorageError("read_permissions", 5)
    monkeypatch.setattr(windows_private_storage, "protect_windows_path", denied)
    target = tmp_path / "must-not-write.txt"
    with pytest.raises(SetupError, match="^private_storage_acl_failed:read_permissions:win32_5$"):
        _atomic(target, b"OFFLINE payload", protect_private_path)
    assert not target.exists()


def test_win32_read_failure_reports_return_code_not_stale_last_error(tmp_path, monkeypatch):
    import ctypes
    from evplatform.venues import windows_private_storage
    real_load = ctypes.WinDLL
    class ReadDenied:
        def __call__(self, *args):
            ctypes.set_last_error(122)
            return 5  # GetNamedSecurityInfo returns its error directly.
    class Library:
        def __init__(self, real):
            self.real = real
            self.GetNamedSecurityInfoW = ReadDenied()
        def __getattr__(self, name):
            return getattr(self.real, name)
    def load(name, **kwargs):
        real = real_load(name, **kwargs)
        return Library(real) if name == "advapi32" else real
    monkeypatch.setattr(ctypes, "WinDLL", load)
    with pytest.raises(windows_private_storage.WindowsStorageError) as error:
        windows_private_storage.protect_windows_path(tmp_path)
    assert error.value.operation == "read_permissions" and error.value.code == 5
