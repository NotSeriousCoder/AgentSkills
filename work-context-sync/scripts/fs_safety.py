#!/usr/bin/env python3
"""
Filesystem safety helpers shared by the import scripts.

Principle: **the scope of protection must match the scope of risk.**
Anything we are about to OVERWRITE in the target workspace gets backed up
first, and the dry-run plan must LIST the files that would be overwritten —
including `.workbuddy/` (memory + workspace assets), which used to be copied
with a bare `shutil.copy2` and no backup.

Used by:
  - restore_export_package.py  (离线会话导出包)
  - sync_import.py             (GitHub/Gitee 硬核同步)
"""

import os
import hashlib
import shutil
from datetime import datetime

MEMORY_DIR = 'memory'


def md5_file(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def rel_files(root):
    """All files under `root`, as forward-slash relative paths, sorted."""
    out = []
    for r, _, files in os.walk(root):
        for fn in files:
            full = os.path.join(r, fn)
            out.append(os.path.relpath(full, root).replace(os.sep, '/'))
    return sorted(out)


def _abs(root, rel):
    return os.path.join(root, rel.replace('/', os.sep))


def plan_conflicts(src_root, dst_root):
    """
    Files that exist in BOTH ``src_root`` and ``dst_root`` — i.e. the ones that
    would be overwritten by a copy.

    Returns a sorted list of ``(rel_path, status)`` where status is
    ``'same'`` (identical bytes → overwrite is a no-op) or ``'diff'``
    (different bytes → real overwrite). rel_path uses forward slashes.
    """
    result = []
    if not os.path.isdir(src_root) or not os.path.isdir(dst_root):
        return result
    for rel in rel_files(src_root):
        d = _abs(dst_root, rel)
        if not os.path.exists(d):
            continue
        try:
            status = 'same' if md5_file(_abs(src_root, rel)) == md5_file(d) else 'diff'
        except OSError:
            status = 'diff'
        result.append((rel, status))
    return result


def memory_conflicts(src_root, dst_root):
    """
    Subset of :func:`plan_conflicts` under ``memory/`` — the day logs and the
    long-term notes. Used as the guard: auto-matching a workspace that already
    holds same-named memory would silently clobber it.
    """
    return [p for p, _ in plan_conflicts(src_root, dst_root)
            if p == MEMORY_DIR or p.startswith(MEMORY_DIR + '/')]


def copy_tree_with_backup(src_root, dst_root):
    """
    Copy the ``src_root`` tree over ``dst_root``, backing up **each** file that
    already exists to ``<dst>.bak-<timestamp>`` before overwriting it.

    Returns ``(copied_count, backup_paths)``.
    """
    copied = 0
    backups = []
    if not os.path.isdir(src_root):
        return copied, backups
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    for rel in rel_files(src_root):
        s = _abs(src_root, rel)
        d = _abs(dst_root, rel)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        if os.path.exists(d):
            bak = f'{d}.bak-{stamp}'
            shutil.copy2(d, bak)
            backups.append(bak)
        shutil.copy2(s, d)
        copied += 1
    return copied, backups
