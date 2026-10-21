"""Protect local secrets using Win32 directly, independent of shell policy.

Only owner/DACL information is accessed; audit privileges are never requested.
Reference: https://learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-setnamedsecurityinfow
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from pathlib import Path


class WindowsStorageError(OSError):
    """Fixed operation labels and numeric OS codes only; never paths or secrets."""

    def __init__(self, operation: str, code: int = 0):
        self.operation, self.code = operation, int(code)
        super().__init__(f"{operation}:win32_{self.code}")


def protect_windows_path(path: Path):
    """Require current-user ownership and a protected user/SYSTEM-only DACL."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ptr = ctypes.c_void_p
    out = ctypes.POINTER(ptr)
    dword = wintypes.DWORD
    boolean = wintypes.BOOL

    def api(library, name, result, *arguments):
        function = getattr(library, name)
        function.restype, function.argtypes = result, list(arguments)
        return function

    process = api(kernel, "GetCurrentProcess", wintypes.HANDLE)
    close = api(kernel, "CloseHandle", boolean, wintypes.HANDLE)
    free = api(kernel, "LocalFree", ptr, ptr)
    open_token = api(advapi, "OpenProcessToken", boolean, wintypes.HANDLE, dword, out)
    token_info = api(advapi, "GetTokenInformation", boolean, ptr, ctypes.c_int, ptr, dword, ctypes.POINTER(dword))
    sid_text = api(advapi, "ConvertSidToStringSidW", boolean, ptr, out)
    parse_sd = api(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", boolean,
                   wintypes.LPCWSTR, dword, out, ctypes.POINTER(dword))
    get_owner = api(advapi, "GetSecurityDescriptorOwner", boolean, ptr, out, ctypes.POINTER(boolean))
    get_dacl = api(advapi, "GetSecurityDescriptorDacl", boolean, ptr, ctypes.POINTER(boolean), out, ctypes.POINTER(boolean))
    get_control = api(advapi, "GetSecurityDescriptorControl", boolean, ptr, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(dword))
    equal_sid = api(advapi, "EqualSid", boolean, ptr, ptr)
    get_ace = api(advapi, "GetAce", boolean, ptr, dword, out)
    read_sd = api(advapi, "GetNamedSecurityInfoW", dword, wintypes.LPCWSTR, ctypes.c_int,
                  dword, out, out, out, out, out)
    write_sd = api(advapi, "SetNamedSecurityInfoW", dword, wintypes.LPWSTR, ctypes.c_int,
                   dword, ptr, ptr, ptr, ptr)

    def require(ok, operation="descriptor_validation", code=0):
        if not ok:
            raise WindowsStorageError(operation, ctypes.get_last_error() if code is None else code)

    def descriptor_parts(descriptor):
        owner, dacl = ptr(), ptr()
        present, defaulted = boolean(), boolean()
        require(get_owner(descriptor, ctypes.byref(owner), ctypes.byref(defaulted)), "get_owner", None)
        require(get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)), "get_dacl", None)
        require(owner.value and present.value and dacl.value)
        return owner, dacl

    def entries(dacl):
        # ACL header: revision, reserved, byte size, ACE count, reserved.
        _, _, size, count, _ = struct.unpack("<BBHHH", ctypes.string_at(dacl, 8))
        require(size >= 8 and count <= 2)
        rows = []
        for index in range(count):
            ace = ptr()
            require(get_ace(dacl, index, ctypes.byref(ace)), "get_ace", None)
            _, _, length = struct.unpack("<BBH", ctypes.string_at(ace, 4))
            require(4 <= length <= size - 8)
            rows.append(ctypes.string_at(ace, length))
        return sorted(rows)

    token, sid_string, expected = ptr(), ptr(), ptr()
    try:
        require(open_token(process(), 0x0008, ctypes.byref(token)), "open_process_token", None)  # TOKEN_QUERY
        needed = dword()
        token_info(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
        require(needed.value > 0)
        buffer = ctypes.create_string_buffer(needed.value)
        require(token_info(token, 1, buffer, needed, ctypes.byref(needed)), "get_token_user", None)
        user_sid = ptr.from_buffer(buffer).value
        require(sid_text(user_sid, ctypes.byref(sid_string)), "convert_user_sid", None)
        user = ctypes.wstring_at(sid_string)
        inheritance = "OICI" if path.is_dir() else ""
        sddl = f"O:{user}D:P(A;{inheritance};FA;;;{user})(A;{inheritance};FA;;;SY)"
        require(parse_sd(sddl, 1, ctypes.byref(expected), None), "build_private_descriptor", None)
        owner, dacl = descriptor_parts(expected)
        expected_entries = entries(dacl)

        def matches():
            actual = ptr()
            try:
                # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION.
                code = read_sd(str(path.absolute()), 1, 0x5, None, None, None, None, ctypes.byref(actual))
                require(code == 0, "read_permissions", code)
                actual_owner, actual_dacl = descriptor_parts(actual)
                control, revision = wintypes.WORD(), dword()
                require(get_control(actual, ctypes.byref(control), ctypes.byref(revision)), "read_protection", None)
                if not equal_sid(owner, actual_owner) or not control.value & 0x1000:
                    return False
                try:
                    return entries(actual_dacl) == expected_entries
                except OSError:
                    return False
            finally:
                if actual.value:
                    free(actual)

        if not matches():
            # Replace only owner/DACL. Do not request SACL/audit privileges.
            code = write_sd(str(path.absolute()), 1, 0x80000005, owner, None, dacl, None)
            require(code == 0, "write_permissions", code)
            require(matches(), "verify_permissions")
    finally:
        if expected.value:
            free(expected)
        if sid_string.value:
            free(sid_string)
        if token.value:
            close(token)
