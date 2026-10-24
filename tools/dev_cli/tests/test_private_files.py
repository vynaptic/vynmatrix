"""Owner-only files: POSIX mode bits, and the Windows ACL that replaces them."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from dev_cli.core import private_files

_USER = "S-1-5-21-1004336348-1177238915-682003330-1001"
#: What a file under a user profile inherits: owner, SYSTEM, Administrators.
_INHERITED = f"O:{_USER}D:AI(A;ID;FA;;;{_USER})(A;ID;FA;;;SY)(A;ID;FA;;;BA)"
_RESTRICTED = f"O:{_USER}D:PAI(A;;FA;;;{_USER})(A;;FA;;;SY)(A;;FA;;;BA)"


def test_posix_creation_is_mode_0600_and_never_replaces_a_file(tmp_path: Path) -> None:
    target = tmp_path / ".env"

    private_files.create(target, "SECRET=value\n")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert private_files.is_owner_only(target)
    assert target.read_text(encoding="utf-8") == "SECRET=value\n"
    with pytest.raises(FileExistsError):
        private_files.create(target, "SECRET=other\n")
    assert target.read_text(encoding="utf-8") == "SECRET=value\n"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644])
def test_posix_group_or_other_access_is_not_owner_only(tmp_path: Path, mode: int) -> None:
    target = tmp_path / ".env"
    target.write_text("SECRET=value\n", encoding="utf-8")
    os.chmod(target, mode)  # noqa: PTH101 - mirrors what an operator might run.

    assert not private_files.is_owner_only(target)
    private_files.restrict(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "sddl",
    [
        _RESTRICTED,
        f"O:{_USER}D:P(A;;FA;;;{_USER})",
        # An elevated creator may own the file through the Administrators group.
        "O:BAD:P(A;;FA;;;BA)(A;;FA;;;SY)",
        # Deny entries only remove access.
        f"O:{_USER}D:P(D;;FA;;;WD)(A;;FA;;;{_USER})",
        f"O:{_USER}D:P(A;;0x1f01ff;;;S-1-5-18)(A;;FA;;;{_USER})",
    ],
)
def test_a_protected_dacl_for_owner_system_and_administrators_is_owner_only(sddl: str) -> None:
    assert private_files.sddl_is_owner_only(sddl)


@pytest.mark.parametrize(
    "sddl",
    [
        # Inherited from the folder: a later folder change would reach the file.
        _INHERITED,
        # Inherited from C:\ outside a profile: every signed-in user can read.
        f"O:{_USER}D:AI(A;ID;FA;;;{_USER})(A;ID;0x1200a9;;;BU)(A;ID;0x1301bf;;;AU)",
        f"O:{_USER}D:P(A;;FA;;;{_USER})(A;;FR;;;WD)",
        f"O:{_USER}D:P(A;;FA;;;{_USER})(A;;FR;;;S-1-5-21-1-2-3-1002)",
        f"O:{_USER}D:NO_ACCESS_CONTROL",
        f"O:{_USER}G:SYD:P(A;;FA;;;{_USER})",
        f"O:{_USER}D:P(XA;;FA;;;{_USER};(Member_of {{SID(BA)}}))",
        "",
    ],
)
def test_any_other_descriptor_is_not_owner_only(sddl: str) -> None:
    assert not private_files.sddl_is_owner_only(sddl)


def test_the_owner_is_read_from_a_descriptor_whose_alias_runs_into_the_dacl() -> None:
    assert private_files.sddl_owner("O:BAD:PAI(A;;FA;;;BA)") == "BA"
    assert private_files.sddl_owner(_INHERITED) == _USER
    with pytest.raises(PermissionError):
        private_files.sddl_owner("D:PAI(A;;FA;;;BA)")


class _FakeWindowsAcl:
    """A file's descriptor as the Win32 API would report it, before and after a write."""

    def __init__(self, *, after_write: str = _RESTRICTED) -> None:
        self.current = _INHERITED
        self.after_write = after_write
        self.written: list[tuple[str, str]] = []

    def read(self, path: Path) -> str:
        assert path.exists()
        return self.current

    def write(self, path: Path, sddl: str) -> None:
        self.written.append((path.name, sddl))
        self.current = self.after_write
        # ``os.stat`` on Windows reports any writable file as 0o666.
        os.chmod(path, 0o666)  # noqa: PTH101


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> _FakeWindowsAcl:
    acl = _FakeWindowsAcl()
    monkeypatch.setattr(private_files, "_WINDOWS", True)
    monkeypatch.setattr(private_files, "_read_sddl", acl.read)
    monkeypatch.setattr(private_files, "_write_dacl", acl.write)
    return acl


def test_windows_creation_sets_a_protected_owner_dacl_before_any_content(
    tmp_path: Path, windows: _FakeWindowsAcl
) -> None:
    target = tmp_path / ".env"

    private_files.create(target, "SECRET=value\n")

    assert windows.written == [(".env", f"D:P(A;;FA;;;{_USER})(A;;FA;;;SY)(A;;FA;;;BA)")]
    # The mode bits Windows reports are not consulted; the ACL decides.
    assert stat.S_IMODE(target.stat().st_mode) == 0o666
    assert private_files.is_owner_only(target)
    assert target.read_text(encoding="utf-8") == "SECRET=value\n"


def test_windows_inherited_access_is_not_owner_only(
    tmp_path: Path, windows: _FakeWindowsAcl
) -> None:
    target = tmp_path / ".env"
    target.write_text("SECRET=value\n", encoding="utf-8")

    assert not private_files.is_owner_only(target)


def test_a_file_that_cannot_be_restricted_is_removed_before_it_holds_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acl = _FakeWindowsAcl(after_write=_INHERITED)
    monkeypatch.setattr(private_files, "_WINDOWS", True)
    monkeypatch.setattr(private_files, "_read_sddl", acl.read)
    monkeypatch.setattr(private_files, "_write_dacl", acl.write)
    target = tmp_path / ".env"

    with pytest.raises(PermissionError, match="could not be restricted"):
        private_files.create(target, "SECRET=value\n")

    assert not target.exists()
