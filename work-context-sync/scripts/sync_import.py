#!/usr/bin/env python3
"""
Hardcore sync: IMPORT a staged session onto this device.

Reads a staging folder produced by sync_export.py (and downloaded from GitHub),
then restores the session locally:
  1. Safety checks (WorkBuddy running warning, sessions.json backup)
  2. Path remapping: pick the local target workspace (auto-match by leaf name,
     otherwise require --target-workdir)
  3. Place conversation history at ~/.workbuddy/projects/<encoded>/<convId>.jsonl
  4. Merge the session entry into ~/.workbuddy/app/sessions.json (append/update)
  5. Restore workspace outputs/ and .workbuddy/ into the target workspace

Defaults to DRY-RUN. Pass --apply to actually write.
"""

import os
import sys
import json
import shutil
import argparse
import subprocess
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from path_map import (
    PROJECTS_DIR, SESSIONS_INDEX,
    encode_workdir, get_leaf_name, read_sessions_index,
    find_matching_workspace, normalize_workdir,
)

MANIFEST_NAME = 'sync-manifest.json'


def is_workbuddy_running():
    """Best-effort check for a running WorkBuddy process on Windows."""
    if os.name != 'nt':
        return None
    try:
        out = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq WorkBuddy.exe'],
            capture_output=True, timeout=8
        )
        raw = out.stdout or b''
        # tasklist output on zh-CN Windows is GBK; decode defensively
        for enc in ('utf-8', 'gbk', 'cp936', 'latin-1'):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = ''
        return 'WorkBuddy.exe' in text
    except Exception:
        return None


def pick_target_workspace(manifest, explicit_target):
    """
    Decide the local target workspace path.
    Returns (path, source) where source is 'explicit' | 'auto' | None.
    """
    if explicit_target:
        return normalize_workdir(explicit_target), 'explicit'

    leaf = manifest.get('sourceLeafName') or get_leaf_name(manifest.get('sourceWorkDir', ''))
    matches = find_matching_workspace(leaf, exclude=manifest.get('sourceWorkDir'))
    if len(matches) == 1:
        return matches[0], 'auto'
    if len(matches) > 1:
        return None, ('ambiguous', matches)
    return None, 'notfound'


def merge_session_entry(conversation_id, target_workdir, manifest):
    """Return the new sessions.json content (dict) with the entry merged in."""
    data = read_sessions_index()
    sessions = data.get('sessions', [])

    # Determine userId: prefer an existing local one (same account on this device)
    local_user_id = None
    for s in sessions:
        if s.get('userId'):
            local_user_id = s['userId']
            break

    src_entry = manifest.get('sessionEntry', {}) or {}
    new_entry = {
        'conversationId': conversation_id,
        'userId': local_user_id or src_entry.get('userId', ''),
        'workDir': target_workdir,
        'startedAt': src_entry.get('startedAt', datetime.now().isoformat()),
        'resumedAt': datetime.now().isoformat(),
    }

    replaced = False
    for i, s in enumerate(sessions):
        if s.get('conversationId') == conversation_id:
            sessions[i] = new_entry
            replaced = True
            break
    if not replaced:
        sessions.insert(0, new_entry)

    data['sessions'] = sessions
    data['updatedAt'] = datetime.now().isoformat()
    return data, replaced


def import_session(staging_dir, target_workdir=None, apply=False):
    manifest_path = os.path.join(staging_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        raise SystemExit(f"错误：staging 目录缺少 {MANIFEST_NAME}：{staging_dir}")

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    conv_id = manifest['conversationId']
    print('=' * 60)
    print('硬核同步 - 导入计划')
    print('=' * 60)
    print(f'  会话 ID      : {conv_id}')
    print(f'  标题        : {manifest.get("title")}')
    print(f'  源工作空间   : {manifest.get("sourceWorkDir")}')

    # 1. safety
    running = is_workbuddy_running()
    if running:
        print('  [!] 检测到 WorkBuddy 正在运行')
        print('      导入后必须完全退出并重启 WorkBuddy 才能生效')
        print('      若重启后仍看不到会话，请重新运行本脚本')

    # 2. target workspace
    target, source = pick_target_workspace(manifest, target_workdir)
    if target is None:
        if isinstance(source, tuple) and source[0] == 'ambiguous':
            print('\n错误：本机存在多个同名工作空间，请用 --target-workdir 明确指定：')
            for m in source[1]:
                print(f'  - {m}')
        else:
            print(f'\n错误：本机未找到末级目录为 "{manifest.get("sourceLeafName")}" 的工作空间。')
            print('请用 --target-workdir <路径> 指定目标工作空间（不存在会自动创建）。')
        raise SystemExit(2)

    encoded = encode_workdir(target)
    print(f'  目标工作空间 : {target}  ({source})')
    print(f'  projects 目录: {encoded}')
    print()

    # 3. plan
    plan = []
    jsonl_src = os.path.join(staging_dir, 'projects', manifest['sourceProjectsDir'],
                             f'{conv_id}.jsonl')
    jsonl_dst = os.path.join(PROJECTS_DIR, encoded, f'{conv_id}.jsonl')
    plan.append(('对话历史', jsonl_src, jsonl_dst))

    workspace_src = os.path.join(staging_dir, 'workspace')
    if os.path.isdir(workspace_src):
        for sub in ('outputs', '.workbuddy'):
            s = os.path.join(workspace_src, sub)
            if os.path.isdir(s):
                plan.append((f'工作空间/{sub}', s, os.path.join(target, sub)))

    print('将写入:')
    for label, src, dst in plan:
        print(f'  [{label}]')
        print(f'    {dst}')
    print(f'  [会话索引] {SESSIONS_INDEX} (合并条目)')
    print()

    if not apply:
        print('以上为 DRY-RUN，未写入任何文件。加 --apply 执行。')
        return 0

    # 4. apply
    # 4a. backup sessions.json
    if os.path.exists(SESSIONS_INDEX):
        bak = f'{SESSIONS_INDEX}.bak-{datetime.now().strftime("%Y%m%d%H%M%S")}'
        shutil.copy2(SESSIONS_INDEX, bak)
        print(f'已备份会话索引 -> {bak}')

    # 4b. conversation history
    if not os.path.exists(jsonl_src):
        raise SystemExit(f'错误：staging 中缺少对话历史：{jsonl_src}')
    os.makedirs(os.path.dirname(jsonl_dst), exist_ok=True)
    shutil.copy2(jsonl_src, jsonl_dst)
    print(f'已写入对话历史 -> {jsonl_dst}')

    # 4c. workspace files
    for label, src, dst in plan[1:]:
        copied = 0
        for root, dirs, files in os.walk(src):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, src)
                tgt = os.path.join(dst, rel)
                os.makedirs(os.path.dirname(tgt), exist_ok=True)
                shutil.copy2(full, tgt)
                copied += 1
        print(f'已写入 {label} -> {dst} ({copied} 个文件)')

    # 4d. merge sessions.json
    new_data, replaced = merge_session_entry(conv_id, target, manifest)
    with open(SESSIONS_INDEX, 'w', encoding='utf-8') as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
    verb = '更新' if replaced else '新增'
    print(f'已{verb}会话索引条目: {conv_id}')

    print()
    print('导入完成。请完全退出并重启 WorkBuddy。')
    print('提示：若重启后看不到该会话，可能是 WorkBuddy 退出时覆盖了索引，')
    print('      重新运行本脚本的 --apply 即可（备份文件可回滚）。')
    return 0


def main():
    ap = argparse.ArgumentParser(description='Import a staged session (hardcore sync)')
    ap.add_argument('staging_dir', help='staging directory (from sync_export / GitHub download)')
    ap.add_argument('--target-workdir', help='explicit target workspace path on this device')
    ap.add_argument('--apply', action='store_true', help='actually write (default: dry-run)')
    args = ap.parse_args()

    sys.exit(import_session(args.staging_dir, args.target_workdir, args.apply))


if __name__ == '__main__':
    main()
