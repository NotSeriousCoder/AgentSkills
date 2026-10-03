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

MANIFEST_NAME = 'sync-manifest.json'


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
                   include_outputs=True, include_memory=True):
    """Export a single session into out_dir. Returns the manifest dict."""
    # Resolve workDir if not provided
    entry = get_session_entry(conversation_id)
    if not work_dir:
        if entry:
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
    manifest = {
        'formatVersion': 1,
        'conversationId': conversation_id,
        'sourceWorkDir': work_dir,
        'sourceProjectsDir': encoded,
        'sourceLeafName': get_leaf_name(work_dir),
        'sessionEntry': session_entry,
        'title': read_title_from_jsonl(src_jsonl) or get_leaf_name(work_dir),
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
    )

    print('导出完成')
    print(f'  会话 ID     : {manifest["conversationId"]}')
    print(f'  源工作空间  : {manifest["sourceWorkDir"]}')
    print(f'  projects 目录: {manifest["sourceProjectsDir"]}')
    print(f'  标题        : {manifest["title"]}')
    print(f'  Staging     : {args.out}')
    print(f'  文件数      : {len(manifest["files"])} (+ {MANIFEST_NAME})')


if __name__ == '__main__':
    main()
