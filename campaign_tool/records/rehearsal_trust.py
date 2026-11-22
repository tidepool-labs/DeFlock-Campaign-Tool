"""Synthetic trust for serial, host-only rehearsals in a dedicated process.

Never use this context in a process serving live requests: approval's settings
are process globals. It makes no changes to the installed trust configuration.
"""
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from .. import approval
from . import run_stages

_ACTIVE = Lock()


def _has_symlink(path):
    # Inspect from the root down; do not traverse any symlink target.
    return any(part.is_symlink() for part in (*reversed(path.parents), path))


def sandbox_path(sandbox):
    """Return an absolute sandbox path, refusing aliases and the live anchor directory."""
    path = Path(sandbox).absolute()
    live = Path(approval.EXPECTED_MOUNT)
    if path.anchor != '/' or '..' in path.parts or path == live or live in path.parents or _has_symlink(path):
        raise ValueError('unsafe sandbox path')
    return path


def require_private(sandbox):
    info = sandbox.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()):
        raise ValueError('sandbox must be an owner-only 0700 directory')


@contextmanager
def sandbox_anchor(sandbox, signers_file):
    """Temporarily trust only the named synthetic file in a marked private sandbox.

    The file may be absent to rehearse unavailability. Filesystem inputs are
    owned by the rehearsal caller and must not be changed by concurrent writers.
    """
    sandbox = sandbox_path(sandbox)
    path = Path(signers_file).absolute()
    if (sandbox not in path.parents or '..' in path.parts or _has_symlink(path)
            or path in (sandbox / run_stages.SANDBOX_MARKER, approval.SSH_KEYGEN)):
        raise ValueError('signers file must be inside the sandbox, without aliases')
    try:
        require_private(sandbox)
    except OSError as exc:
        raise ValueError('sandbox directory unavailable') from exc
    if run_stages.sandbox_refusal(sandbox):
        raise ValueError('safe sandbox marker required')
    if not approval.init_userns():
        raise ValueError('rehearsal must run on the host')
    if not _ACTIVE.acquire(blocking=False):
        raise ValueError('sandbox anchor already active')
    original_signers, original_check = approval.TRUSTED_SIGNERS, approval._require_root

    def check(candidate):
        if Path(candidate) != path:
            return original_check(candidate)
        if _has_symlink(path):
            raise approval.TrustUnavailable('sandbox signers unsafe')
        try:
            info = path.lstat()
        except OSError as exc:
            raise approval.TrustUnavailable('sandbox signers unavailable') from exc
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise approval.TrustUnavailable('sandbox signers unsafe')

    try:
        approval.TRUSTED_SIGNERS = path
        approval._require_root = check
        yield
    finally:
        approval._require_root = original_check
        approval.TRUSTED_SIGNERS = original_signers
        _ACTIVE.release()
