#!/usr/bin/env python3
"""
Hardcore sync: EXPORT the current session into a staging folder.

Produces a staging directory containing everything needed to restore the
session on another device:
    <staging>/
        sync-manifest.json                          # source info + file list
        projects/<encoded-workdir>/<convId>.jsonl   # conversation history
        workspace/outputs/...                       # optional deliverables
        workspace/.workbuddy/...                    # optional memory + local meta

The staging folder is then uploaded via storage_backend.py (see push_session.py for the
one-shot flow) and restored on the target device via sync_import.py (see pull_session.py).

Note: the manifest also carries `createdAtMs`, `updatedAtMs` and `model` read from the
conversation JSONL, because the target device needs them to build an accurate
`workbuddy.db` sessions row (the only thing that makes the session visible in the UI).

formatVersion 3 adds `sourcePlatform` — the product this session came from. The
importing device compares it with its own detected platform (see sync_import.py);
a mismatch means "this session belongs to another AI product" and must be surfaced
to the user before writing anything.
"""

import os
import sys
import json
import shutil
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from path_map import (
    WORKBUDDY_HOME, PROJECTS_DIR, SESSIONS_INDEX,
    encode_workdir, get_leaf_name, get_session_entry, read_sessions_index,
)
import wb_db

MANIFEST_NAME = 'sync-manifest.json'


def detect_source_platform():
    """
    Return the id of the product this session belongs to (best effort), e.g. 'workbuddy'.
    Falls back to None when detection is unavailable — the importer treats None as
    "unknown, do not block".
    """
    try:
        from detect_platform import detect_platforms
        found = detect_platforms()
        if len(found) == 1:
            return found[0].get('id')
        # more than one installed — prefer workbuddy if present, else leave ambiguous
        ids = [f.get('id') for f in found if f.get('id')]
        if 'workbuddy' in ids:
            return 'workbuddy'
        return ids[0] if ids else None
    except Exception:
        return None


def detect_conversation_id():
    """Auto-detect current conversation id from environment."""
    return os.environ.get('CLAUDE_SESSION_ID') or os.environ.get('CODEBUDDY_SESSION_ID')


def read_title_from_jsonl(jsonl_path):
    """Read AI-generated title from the conversation JSONL."""
    try:
        with open(jsonl_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get('type') == 'ai-title':
                    return obj.get('aiTitle', '')
    except IOError:
        pass
    return ''


def read_jsonl_meta(jsonl_path):
    """
    Scan the JSONL for the fields the target device needs to register the session:
        (first_ts_ms, last_ts_ms, model)
    `model` is the most common model name found in `providerData`.
    """
    first_ts = last_ts = None
    models = {}
    try:
        with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                pd = obj.get('providerData')
                if isinstance(pd, dict):
                    for k, v in pd.items():
                        if 'model' in k.lower() and isinstance(v, str):
                            models[v] = models.get(v, 0) + 1
                ts = obj.get('timestamp')
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
    except IOError:
        pass
    model = max(models, key=models.get) if models else None
    return first_ts, last_ts, model


def copy_tree_filtered(src, dst, skip_names=None):
    """Copy a directory tree, skipping given dir/file names. Returns copied rel paths."""
    skip_names = set(skip_names or [])
    copied = []
    if not os.path.isdir(src):
        return copied
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip_names]
        for fn in files:
            if fn in skip_names:
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, src)
            target = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(full, target)
            copied.append(rel.replace('\\', '/'))
    return copied


def export_session(conversation_id, work_dir, out_dir,
                   include_outputs=True, include_memory=True,
                   source_platform=None):
    """Export a single session into out_dir. Returns the manifest dict."""
    # Resolve workDir: prefer workbuddy.db (authoritative), fall back to sessions.json
    db_row = wb_db.get_session_full(conversation_id)
    entry = get_session_entry(conversation_id)
    if not work_dir:
        if db_row and db_row.get('cwd'):
            work_dir = db_row['cwd']
        elif entry:
            work_dir = entry.get('workDir')
        else:
            raise SystemExit(f"错误：找不到会话 {conversation_id} 的 workDir，请用 --workdir 指定")

    encoded = encode_workdir(work_dir)
    src_jsonl = os.path.join(PROJECTS_DIR, encoded, f'{conversation_id}.jsonl')

    if not os.path.exists(src_jsonl):
        raise SystemExit(f"错误：对话历史不存在：{src_jsonl}")

    # Prepare staging layout
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    projects_out = os.path.join(out_dir, 'projects', encoded)
    os.makedirs(projects_out, exist_ok=True)

    # 1. conversation history
    shutil.copy2(src_jsonl, os.path.join(projects_out, f'{conversation_id}.jsonl'))

    files = [f'projects/{encoded}/{conversation_id}.jsonl']

    # 2. session entry (from sessions.json)
    session_entry = entry or {
        'conversationId': conversation_id,
        'workDir': work_dir,
    }

    # 3. optional: workspace outputs + memory
    workspace_out = os.path.join(out_dir, 'workspace')
    if include_outputs and os.path.isdir(os.path.join(work_dir, 'outputs')):
        copied = copy_tree_filtered(
            os.path.join(work_dir, 'outputs'),
            os.path.join(workspace_out, 'outputs')
        )
        files += [f'workspace/outputs/{p}' for p in copied]

    if include_memory and os.path.isdir(os.path.join(work_dir, '.workbuddy')):
        copied = copy_tree_filtered(
            os.path.join(work_dir, '.workbuddy'),
            os.path.join(workspace_out, '.workbuddy')
        )
        files += [f'workspace/.workbuddy/{p}' for p in copied]

    # 4. manifest
    first_ts, last_ts, model = read_jsonl_meta(src_jsonl)
    manifest = {
        'formatVersion': 3,
        'conversationId': conversation_id,
        'sourceWorkDir': work_dir,
        'sourceProjectsDir': encoded,
        'sourceLeafName': get_leaf_name(work_dir),
        # 源平台：导入端拿它做「平台匹配校验」（v3 新增）
        'sourcePlatform': source_platform or detect_source_platform(),
        'sessionEntry': session_entry,
        'title': read_title_from_jsonl(src_jsonl) or get_leaf_name(work_dir),
        # 目标设备建 workbuddy.db 行所需（v2 新增）
        'createdAtMs': first_ts,
        'updatedAtMs': last_ts,
        'model': model,
        'userId': (db_row or {}).get('user_id') or session_entry.get('userId', ''),
        'packedAt': datetime.now().isoformat(),
        'packedBy': 'work-context-sync (hardcore mode)',
        'includesOutputs': include_outputs,
        'includesMemory': include_memory,
        'files': files,
    }
    with open(os.path.join(out_dir, MANIFEST_NAME), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return manifest


def main():
    ap = argparse.ArgumentParser(description='Export current session for hardcore sync')
    ap.add_argument('--conversation-id', help='conversationId (default: auto-detect from env)')
    ap.add_argument('--workdir', help='workspace path (default: from sessions.json)')
    ap.add_argument('--out', required=True, help='staging output directory')
    ap.add_argument('--no-outputs', action='store_true', help='skip outputs/ folder')
    ap.add_argument('--no-memory', action='store_true', help='skip .workbuddy/ folder')
    ap.add_argument('--source-platform', help='override the recorded source platform id')
    args = ap.parse_args()

    conv_id = args.conversation_id or detect_conversation_id()
    if not conv_id:
        raise SystemExit("错误：无法确定 conversationId，请用 --conversation-id 指定")

    manifest = export_session(
        conv_id,
        args.workdir,
        args.out,
        include_outputs=not args.no_outputs,
        include_memory=not args.no_memory,
        source_platform=args.source_platform,
    )

    print('导出完成')
    print(f'  会话 ID     : {manifest["conversationId"]}')
    print(f'  源工作空间  : {manifest["sourceWorkDir"]}')
    print(f'  源平台      : {manifest["sourcePlatform"] or "(未识别)"}')
    print(f'  projects 目录: {manifest["sourceProjectsDir"]}')
    print(f'  标题        : {manifest["title"]}')
    print(f'  Staging     : {args.out}')
    print(f'  文件数      : {len(manifest["files"])} (+ {MANIFEST_NAME})')


if __name__ == '__main__':
    main()
