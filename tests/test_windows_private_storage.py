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
