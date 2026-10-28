#!/usr/bin/env python3
"""Transfer a legacy API checkout without following links or changing foreign inodes."""
import os
import pwd
import stat
import sys


def visit(fd, old_uid, new_uid, new_gid, apply=False):
    info = os.fstat(fd)
    if info.st_uid not in (old_uid, new_uid):
        raise ValueError('Checkout contains an unexpected owner; resolve ownership before migration.')
    if stat.S_ISDIR(info.st_mode):
        with os.scandir(fd) as entries:
            for entry in entries:
                # Symlinks need no ownership change: access is controlled by their containing directory.
                if entry.is_symlink():
                    continue
                mode = entry.stat(follow_symlinks=False).st_mode
                if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                    raise ValueError('Checkout contains a special file; resolve it before migration.')
                child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    visit(child, old_uid, new_uid, new_gid, apply)
                finally:
                    os.close(child)
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Checkout contains a special file or hard link; resolve it before migration.')
    if apply and info.st_uid == old_uid:
        # fchown acts on the checked inode, even if its directory entry changes concurrently.
        os.fchown(fd, new_uid, new_gid)


def migrate(path, old_user, new_user):
    if not os.path.isabs(path) or os.path.normpath(path) != path or path == '/':
        raise ValueError('Use a canonical absolute installation directory.')
    old = pwd.getpwnam(old_user)
    new = pwd.getpwnam(new_user)
    if 0 in (old.pw_uid, new.pw_uid) or new_user in ('www-data', 'apache', 'nginx'):
        raise ValueError('The API needs a dedicated non-root account.')
    if old_user != new_user and old.pw_uid == new.pw_uid:
        raise ValueError('The dedicated account must have a different UID from the legacy account.')
    # Open every ancestor without following symlinks. The parent must protect the checkout entry.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.strip('/').split('/'):
            parent = os.fstat(fd)
            if parent.st_uid != 0 or parent.st_mode & 0o022:
                raise ValueError('Installation ancestors must be root-owned and not writable by others.')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        visit(fd, old.pw_uid, new.pw_uid, new.pw_gid)
        visit(fd, old.pw_uid, new.pw_uid, new.pw_gid, apply=True)
    finally:
        os.close(fd)


if __name__ == '__main__':
    try:
        if os.geteuid() != 0 or len(sys.argv) != 4:
            raise ValueError('Run as root: migrate-runtime-owner.py DIRECTORY OLD_USER NEW_USER')
        migrate(*sys.argv[1:])
    except (OSError, KeyError, ValueError) as error:
        sys.exit(str(error))
