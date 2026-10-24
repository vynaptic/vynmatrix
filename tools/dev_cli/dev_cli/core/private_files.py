"""Owner-only files on every supported host: POSIX mode bits or a Windows ACL.

``.env`` and ``owner.local.yaml`` hold every generated secret, so they must be
readable by their owner alone. POSIX expresses that as mode ``0600``. Windows
has no such bits — ``os.stat`` reports ``0o666`` for any writable file — so the
equivalent there is a protected access-control list that grants only the file's
owner, SYSTEM and the Administrators group, mirroring owner-plus-root on POSIX.
Both forms fail closed: a file that cannot be restricted is removed, and one
that is not restricted is reported as not private.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

PRIVATE_MODE = 0o600
_WINDOWS = sys.platform == "win32"

#: Trustees a private file may grant on Windows besides its owner. SDDL writes
#: well-known accounts as aliases; the SID forms are accepted as well.
_SYSTEM = frozenset({"SY", "S-1-5-18"})
_ADMINISTRATORS = frozenset({"BA", "S-1-5-32-544"})
#: Owner and DACL only, exactly what ``_read_sddl`` requests. The owner field
#: never contains a colon, so the first ``D:`` after it starts the DACL.
_SDDL_RE = re.compile(r"O:(?P<owner>[^:()]+?)D:(?P<flags>[A-Z]*)(?P<aces>(?:\([^()]*\))*)")
_ACE_RE = re.compile(r"\(([^()]*)\)")
_ACE_FIELDS = 6


def owner_only_dacl(owner: str) -> str:
    """A protected DACL granting full control to the owner, SYSTEM and Administrators."""
    return f"D:P(A;;FA;;;{owner})(A;;FA;;;SY)(A;;FA;;;BA)"


def sddl_owner(sddl: str) -> str:
    """The owner SID (or alias) of a descriptor read with owner and DACL information."""
    match = _SDDL_RE.fullmatch(sddl.strip())
    if match is None:
        msg = "Unrecognised Windows security descriptor"
        raise PermissionError(msg)
    return match["owner"]


def sddl_is_owner_only(sddl: str) -> bool:
    """True only for a protected DACL whose allow entries name the owner, SYSTEM or Administrators.

    Deny entries only remove access, so they are accepted. A null DACL, an
    inheriting DACL or any other trustee or entry type is not owner-only.
    """
    match = _SDDL_RE.fullmatch(sddl.strip())
    if match is None or "P" not in match["flags"]:
        return False
    allowed = {match["owner"], *_SYSTEM, *_ADMINISTRATORS}
    for ace in _ACE_RE.findall(match["aces"]):
        fields = ace.split(";")
        if len(fields) != _ACE_FIELDS:
            return False
        kind, trustee = fields[0], fields[-1]
        if kind == "D":
            continue
        if kind != "A" or trustee not in allowed:
            return False
    return True


def restrict(path: Path) -> None:
    """Make an existing file owner-only, verifying the result on Windows."""
    if not _WINDOWS:
        os.chmod(path, PRIVATE_MODE)  # noqa: PTH101 - Path.chmod has the same effect.
        return
    _write_dacl(path, owner_only_dacl(sddl_owner(_read_sddl(path))))
    if not sddl_is_owner_only(_read_sddl(path)):
        msg = f"{path.name} could not be restricted to its owner"
        raise PermissionError(msg)


def is_owner_only(path: Path) -> bool:
    """Whether nobody but the owner (and the host's superuser) can read ``path``."""
    if _WINDOWS:
        return sddl_is_owner_only(_read_sddl(path))
    return not stat.S_IMODE(path.stat().st_mode) & 0o077


def create(path: Path, content: str) -> None:
    """Create an owner-only text file, refusing to replace one that already exists.

    The file is restricted before any content is written, and removed again if
    it cannot be restricted or written, so a failure leaves nothing behind.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_MODE)
    completed = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            restrict(path)
            stream.write(content)
        completed = True
    finally:
        if not completed:
            path.unlink(missing_ok=True)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    from typing import Any

    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SDDL_REVISION_1 = 1

    def _libraries() -> tuple[Any, Any]:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.POINTER(ctypes.c_void_p)
        advapi32.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            pointer,
            pointer,
            pointer,
            pointer,
            pointer,
        ]
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            pointer,
            ctypes.POINTER(wintypes.ULONG),
        ]
        advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            pointer,
            ctypes.POINTER(wintypes.ULONG),
        ]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        advapi32.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            pointer,
            ctypes.POINTER(wintypes.BOOL),
        ]
        advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        return advapi32, kernel32

    def _read_sddl(path: Path) -> str:
        """The file's owner and DACL in SDDL, read through the Win32 security API."""
        advapi32, kernel32 = _libraries()
        information = _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION
        descriptor = ctypes.c_void_p()
        error = advapi32.GetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            information,
            None,
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if error:
            raise ctypes.WinError(error)
        try:
            text = ctypes.c_void_p()
            if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, _SDDL_REVISION_1, information, ctypes.byref(text), None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return ctypes.wstring_at(text.value)
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.LocalFree(descriptor)

    def _write_dacl(path: Path, sddl: str) -> None:
        """Replace the file's DACL with ``sddl`` and stop it inheriting from its folder."""
        advapi32, kernel32 = _libraries()
        descriptor = ctypes.c_void_p()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            present = wintypes.BOOL()
            defaulted = wintypes.BOOL()
            dacl = ctypes.c_void_p()
            if not advapi32.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not present.value:
                msg = "The owner-only descriptor carries no DACL"
                raise PermissionError(msg)
            error = advapi32.SetNamedSecurityInfoW(
                str(path),
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
            if error:
                raise ctypes.WinError(error)
        finally:
            kernel32.LocalFree(descriptor)

else:

    def _read_sddl(path: Path) -> str:
        msg = f"Windows security descriptors are unavailable on {sys.platform}: {path.name}"
        raise OSError(msg)

    def _write_dacl(path: Path, sddl: str) -> None:
        del sddl
        msg = f"Windows security descriptors are unavailable on {sys.platform}: {path.name}"
        raise OSError(msg)


__all__ = [
    "PRIVATE_MODE",
    "create",
    "is_owner_only",
    "owner_only_dacl",
    "restrict",
    "sddl_is_owner_only",
    "sddl_owner",
]
