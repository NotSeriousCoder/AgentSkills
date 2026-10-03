#!/usr/bin/env python3
"""
One-shot pull: download a session from the configured GitHub/Gitee repository
and restore it locally (with cross-device path remapping). No git.

Typical use:
    python scripts/pull_session.py --list
    python scripts/pull_session.py <session-folder>                 # dry-run
    python scripts/pull_session.py <session-folder> --apply
    python scripts/pull_session.py <session-folder> --target-workdir "D:\\Work\\WB\\x" --apply
"""

import os
import sys
import shutil
import argparse
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from wcs_config import resolve_credentials
from storage_backend import get_store
from sync_import import import_session


def list_sessions(store):
    sessions = store.list_sessions()
    if not sessions:
        print('云端没有任何会话。')
        return []
    print(f'云端会话 ({len(sessions)}):')
    rows = []
    for s in sessions:
        name = s['name']
        readme, _ = store.get_text(f'{name}/README.md')
        title = ''
        if readme:
            for line in readme.splitlines():
                if line.startswith('- **Title**:'):
                    title = line.split(':', 1)[1].strip()
                    break
        rows.append({'name': name, 'title': title})
        print(f"  - {name}")
        if title:
            print(f"      {title}")
    return rows


def download_to_staging(store, folder, staging):
    files = store.download_session(folder)
    if not files:
        print(f'错误: 未能下载 {folder}（可能不存在，或读取失败）')
        return False
    for rel, content in files.items():
        target = os.path.join(staging, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as f:
            f.write(content)
    total = sum(len(v) for v in files.values())
    print(f'已下载 {len(files)} 个文件, 共 {total / 1024:.1f} KB')
    return True


def main():
    ap = argparse.ArgumentParser(description='Download a session and restore it locally (no git)')
    ap.add_argument('session', nargs='?', help='remote session folder name')
    ap.add_argument('--list', action='store_true', help='list remote sessions and exit')
    ap.add_argument('--target-workdir', help='target workspace path on this device')
    ap.add_argument('--apply', action='store_true', help='actually write (default: dry-run)')
    ap.add_argument('--backend', choices=('github', 'gitee'), help='override backend')
    args = ap.parse_args()

    try:
        creds = resolve_credentials(args.backend)
    except SystemExit as e:
        print('[需要先配置]')
        print(str(e))
        return 2

    print(f"平台 {creds['backend']}  仓库 {creds['owner']}/{creds['repo']}  分支 {creds['branch']}")
    store = get_store(credentials=creds)

    if not store.repo_exists():
        print('错误: 仓库不可访问。可运行 bootstrap.py verify 排查。')
        return 1

    if args.list or not args.session:
        list_sessions(store)
        if not args.session:
            print('\n用法: python scripts/pull_session.py <session-folder> [--apply]')
            return 0
        return 0

    staging = tempfile.mkdtemp(prefix='wcs_pull_')
    try:
        if not download_to_staging(store, args.session, staging):
            return 1

        manifest = os.path.join(staging, 'sync-manifest.json')
        if not os.path.exists(manifest):
            print('警告: staging 缺少 sync-manifest.json，可能不是硬核同步包。')
            print(f'文件列表: {sorted(os.listdir(staging))}')

        # delegate to sync_import (dry-run unless --apply)
        rc = import_session(staging, args.target_workdir, args.apply)
        return rc
    finally:
        if args.apply:
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
