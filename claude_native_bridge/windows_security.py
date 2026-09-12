"""Windows runtime ACL boundary. Call BEFORE creating any transport secrets.

Only an absent directory or an empty directory owned by the process token user
is accepted. Existing contents are never silently blessed. No native imports on
Unix. Windows ancestors must not be reparse points (including junctions).
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path


def _reject_reparse(path: Path) -> None:
    for part in (path, *path.parents):
        info = part.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("Runtime path must not traverse reparse points")


def secure_runtime_directory(path: Path) -> Path:
    """Create/protect an absolute, owner-only Windows directory; return its path.

    The parent must already exist. ACL grants only the current process token
    user full control, inheritable by files/subdirectories, without inherited
    ACEs. Raises on non-Windows, wrong owner, existing contents, or reparse paths.
    """
    if sys.platform != "win32":
        raise OSError("Windows runtime security requires Windows")
    import ntsecuritycon
    import pywintypes
    import win32api
    import win32con
    import win32file
    import win32security

    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Runtime path must be absolute")
    _reject_reparse(path.parent)
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    acl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION,
        win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
        ntsecuritycon.FILE_ALL_ACCESS,
        sid,
    )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(sid, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED
    )
    if not path.exists():
        attrs = pywintypes.SECURITY_ATTRIBUTES()
        attrs.SECURITY_DESCRIPTOR = descriptor
        win32file.CreateDirectory(str(path), attrs)
    _reject_reparse(path)
    if not path.is_dir() or any(path.iterdir()):
        raise ValueError("Runtime must be an empty directory")
    info = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION,
    )
    if info.GetSecurityDescriptorOwner() != sid:
        raise ValueError("Runtime must be owned by current process user")
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )
    info = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
    )
    actual = info.GetSecurityDescriptorDacl()
    if (
        not info.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
        or actual is None
        or actual.GetAceCount() != 1
        or actual.GetAce(0)[2] != sid
        or actual.GetAce(0)[1] != ntsecuritycon.FILE_ALL_ACCESS
    ):
        raise OSError("Runtime ACL verification failed")
    _reject_reparse(path)
    return path
