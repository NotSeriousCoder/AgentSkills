#!/usr/bin/env python3
"""
One-shot push: export the current session and upload it to the configured
GitHub/Gitee repository. No git. No manual step chaining.

Typical use:
    python scripts/push_session.py
    python scripts/push_session.py --session-name my-session
    python scripts/push_session.py --dry-run
"""

import os
import sys
import json
import shutil
import argparse
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from wcs_config import resolve_credentials
from storage_backend import get_store
from sync_export import export_session, detect_conversation_id
from manage_session_id import (
    get_local_session_id, set_local_session_id, generate_custom_id,
    get_session_info,
)
from path_map import get_session_entry
from detect_platform import detect_platforms
import wb_db

MAX_SIMPLE_UPLOAD = 1 * 1024 * 1024  # warn threshold (platform API soft limit)


def collect_files(staging_dir):
    """Walk staging_dir -> {rel_path: bytes}."""
    files = {}
    for root, _dirs, names in os.walk(staging_dir):
        for fn in names:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, staging_dir).replace('\\', '/')
            with open(full, 'rb') as f:
                files[rel] = f.read()
    return files


def choose_folder_name(conv_id, work_dir, session_name, custom_id):
    """Prefer an existing remote folder (refresh), else explicit name, else custom id."""
    info = get_session_info(work_dir, conv_id) or {}
    existing = info.get('github_path')
    if existing and not session_name:
        return existing, True
    if session_name:
        return session_name, bool(existing)
    return custom_id, False


def main():
    ap = argparse.ArgumentParser(description='Export current session and upload it (no git)')
    ap.add_argument('--conversation-id', help='conversationId (default: auto-detect)')
    ap.add_argument('--session-name', help='remote folder name (default: reuse existing or custom id)')
    ap.add_argument('--no-outputs', action='store_true', help='skip outputs/')
    ap.add_argument('--no-memory', action='store_true', help='skip .workbuddy/')
    ap.add_argument('--dry-run', action='store_true', help='export only, do not upload')
    ap.add_argument('--backend', choices=('github', 'gitee'), help='override backend')
    args = ap.parse_args()

    # ---- 0. credentials -------------------------------------------------
    try:
        creds = resolve_credentials(args.backend)
    except SystemExit as e:
        print('[需要先配置]')
        print(str(e))
        return 2

    print(f"平台 {creds['backend']}  仓库 {creds['owner']}/{creds['repo']}  分支 {creds['branch']}")

    # ---- 1. platform sanity (informational) -----------------------------
    try:
        detected = detect_platforms()
        if detected:
            print('检测到的本机产品: ' + ', '.join(d['name'] for d in detected))
    except Exception:
        pass

    # ---- 2. locate the session -----------------------------------------
    conv_id = args.conversation_id or detect_conversation_id()
    if not conv_id:
        print('错误: 无法确定 conversationId，请用 --conversation-id 指定')
        return 2
    # Resolve workDir: workbuddy.db is authoritative, sessions.json is the legacy fallback
    work_dir = wb_db.resolve_workdir(conv_id)
    if not work_dir:
        entry = get_session_entry(conv_id)
        work_dir = (entry or {}).get('workDir')
    if not work_dir:
        print(f'错误: 在 workbuddy.db / sessions.json 中都找不到会话 {conv_id}')
        return 2
    print(f'会话 {conv_id}')
    print(f'工作空间 {work_dir}')

    # ---- 3. custom id + folder name ------------------------------------
    custom_id = get_local_session_id(work_dir, conv_id) or generate_custom_id()
    folder, is_refresh = choose_folder_name(conv_id, work_dir, args.session_name, custom_id)
    print(f'云端文件夹 {folder}  ({"刷新已有" if is_refresh else "新建"})')
    print(f'自定义 ID {custom_id}')

    # ---- 4. export to staging ------------------------------------------
    staging = tempfile.mkdtemp(prefix='wcs_push_')
    try:
        manifest = export_session(
            conv_id, work_dir, staging,
            include_outputs=not args.no_outputs,
            include_memory=not args.no_memory,
        )
        files = collect_files(staging)
        total = sum(len(v) for v in files.values())
        print(f'打包完成: {len(files)} 个文件, 共 {total / 1024:.1f} KB')

        big = {k: len(v) for k, v in files.items() if len(v) > MAX_SIMPLE_UPLOAD}
        if big:
            print('  [提示] 以下文件超过 1MB，若上传失败属于平台 API 限制：')
            for k, v in big.items():
                print(f'    - {k} ({v / 1024:.1f} KB)')

        if args.dry_run:
            print('\n[dry-run] 未上传。staging 保留在:')
            print(f'  {staging}')
            staging = None  # do not clean
            return 0

        # ---- 5. upload --------------------------------------------------
        store = get_store(credentials=creds)
        if not store.ensure_repo():
            print('错误: 仓库不可用')
            return 1

        results = store.upload_session(folder, files, f'Sync session {custom_id}')
        ok = [r for r in results if r['success']]
        bad = [r for r in results if not r['success']]
        print(f'上传完成: {len(ok)}/{len(results)} 成功')
        for r in bad:
            print(f"  [失败] {r['path']} (HTTP {r['status']})")

        if bad:
            print('\n上传未全部成功，配置与本地记录未更新。')
            return 1

        # ---- 6. record locally -----------------------------------------
        set_local_session_id(work_dir, conv_id, custom_id,
                             manifest.get('title'), folder)
        print(f'已记录本地元数据 -> {work_dir}/.workbuddy/session-sync.json')
        print(f"\n完成。云端路径: {folder}/  自定义 ID: {custom_id}")
        return 0
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
