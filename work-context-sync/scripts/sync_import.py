#!/usr/bin/env python3
"""
Hardcore sync: IMPORT a staged session onto this device.

Reads a staging folder produced by sync_export.py (and downloaded from GitHub),
then restores the session locally:
  1. Safety checks (WorkBuddy running warning, sessions.json backup)
  2. Platform match validation: compare manifest.sourcePlatform with the platform
     detected on this device; a mismatch aborts unless --force is given
  3. Path remapping: pick the local target workspace (auto-match by leaf name,
     otherwise require --target-workdir). A non-existent workspace is NOT created
     silently — the user must confirm (--create-workspace)
  4. Place conversation history at ~/.workbuddy/projects/<encoded>/<convId>.jsonl
     (an existing file is backed up first — 覆盖语义)
  5. Merge the session entry into ~/.workbuddy/app/sessions.json (legacy cache)
  6. Register the workspace AND the session in ~/.workbuddy/workbuddy.db  <-- 关键
  7. Restore workspace outputs/ and .workbuddy/ into the target workspace
     (per-file backup before overwrite; an auto-matched workspace that already
      holds same-named memory/ files is NOT selected silently — the user must
      confirm with --target-workdir)

Step 6 is what actually makes the session visible in the WorkBuddy UI. Since
v2.115.0 the session list and the workspace list are served from SQLite; the
`app/sessions.json` file is a legacy cache and adding an entry there alone will
NOT show up. See wb_db.py for the verified table layouts.

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
import wb_db
import fs_safety

MANIFEST_NAME = 'sync-manifest.json'


def detect_local_platform():
    """Best-effort id of the AI product installed on THIS device (e.g. 'workbuddy')."""
    try:
        from detect_platform import detect_platforms
        found = detect_platforms()
        ids = [f.get('id') for f in found if f.get('id')]
        if len(ids) == 1:
            return ids[0]
        if 'workbuddy' in ids:
            return 'workbuddy'
        return ids[0] if ids else None
    except Exception:
        return None


def backup_file(path):
    """Copy a file aside before overwriting it. Returns the backup path or None."""
    if not os.path.exists(path):
        return None
    bak = f'{path}.bak-{datetime.now().strftime("%Y%m%d-%H%M%S")}'
    shutil.copy2(path, bak)
    return bak


def jsonl_times(path):
    """Best-effort (first_ts_ms, last_ts_ms) from a conversation JSONL."""
    first = last = None
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = obj.get('timestamp')
                if ts:
                    if first is None:
                        first = ts
                    last = ts
    except IOError:
        pass
    return first, last


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
    Returns (path, source) where source is 'explicit' | 'same-path' | 'auto'
    | ('ambiguous', matches) | 'notfound'.
    """
    if explicit_target:
        return normalize_workdir(explicit_target), 'explicit'

    src = manifest.get('sourceWorkDir')
    # 1) 源工作空间在本机就存在 → 直接用它（新旧电脑路径一致时最省事）
    if src and os.path.isdir(normalize_workdir(src)):
        return normalize_workdir(src), 'same-path'

    # 2) 按末级目录名找同名工作空间
    leaf = manifest.get('sourceLeafName') or get_leaf_name(src or '')
    matches = find_matching_workspace(leaf, exclude=src)
    if len(matches) == 1:
        return matches[0], 'auto'
    if len(matches) > 1:
        return None, ('ambiguous', matches)
    return None, 'notfound'


def merge_session_entry(conversation_id, target_workdir, manifest):
    """Return the new sessions.json content (dict) with the entry merged in."""
    data = read_sessions_index()
    sessions = data.get('sessions', [])

    # Determine userId: prefer workbuddy.db (authoritative), then sessions.json
    local_user_id = None
    if wb_db.db_available():
        try:
            import sqlite3
            con = wb_db.connect()
            cur = con.cursor()
            cur.execute('SELECT user_id FROM sessions WHERE user_id IS NOT NULL LIMIT 1')
            row = cur.fetchone()
            con.close()
            local_user_id = row[0] if row else None
        except Exception:
            local_user_id = None
    if not local_user_id:
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


def import_session(staging_dir, target_workdir=None, apply=False,
                   force=False, create_workspace=False):
    manifest_path = os.path.join(staging_dir, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        raise SystemExit(f"错误：staging 目录缺少 {MANIFEST_NAME}：{staging_dir}")

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    conv_id = manifest['conversationId']
    src_plat = manifest.get('sourcePlatform')
    leaf = (manifest.get('sourceLeafName')
            or get_leaf_name(manifest.get('sourceWorkDir', '')) or '未命名工作空间')

    print('=' * 60)
    print('硬核同步 - 导入计划')
    print('=' * 60)
    print(f'  会话 ID      : {conv_id}')
    print(f'  标题        : {manifest.get("title")}')
    print(f'  源工作空间   : {manifest.get("sourceWorkDir")}')
    print(f'  源平台      : {src_plat or "(包内未记录)"}')

    # 1. safety
    running = is_workbuddy_running()
    if running:
        print('  [!] 检测到 WorkBuddy 正在运行')
        print('      导入后必须完全退出并重启 WorkBuddy 才能生效')
        print('      若重启后仍看不到会话，请重新运行本脚本')

    # 1b. platform match validation (manifest v3)
    local_plat = detect_local_platform()
    if src_plat and local_plat and src_plat != local_plat:
        print(f'  [!] 平台不匹配：源 = {src_plat} / 本机 = {local_plat}')
        print('      这条会话来自另一个 AI 产品，文件结构可能不同；默认中止。')
        print('      确需继续请加 --force。')
        if not force:
            raise SystemExit(3)
        print('      --force 已指定，继续。')
    elif src_plat and local_plat:
        print(f'  平台匹配     : 一致 ({local_plat})')

    # 2. target workspace (never created silently)
    target, source = pick_target_workspace(manifest, target_workdir)
    if target is None:
        if isinstance(source, tuple) and source[0] == 'ambiguous':
            print(f'\n错误：本机存在多个末级目录为 "{leaf}" 的工作空间，请用 --target-workdir 明确指定：')
            for m in source[1]:
                print(f'  - {m}')
        else:
            suggestion = wb_db.suggest_workspace(leaf)
            print(f'\n未找到末级目录为 "{leaf}" 的本机工作空间。')
            print('请把下面的建议路径交给用户确认（不要自行创建）：')
            print(f'  建议路径 : {suggestion}')
            print('用户确认后，用确认后的路径重新运行：')
            print(f'  --target-workdir "<确认后的路径>" --create-workspace --apply')
        raise SystemExit(2)

    if not os.path.isdir(target) and not create_workspace:
        print(f'\n目标工作空间当前不存在：{target}')
        print('为避免误建目录污染工作空间列表，需用户确认后加 --create-workspace 才会创建。')
        raise SystemExit(2)

    encoded = encode_workdir(target)
    print(f'  目标工作空间 : {target}  ({source}'
          + ('，将新建' if not os.path.isdir(target) else '') + ')')
    print(f'  projects 目录: {encoded}')
    print()

    workspace_src = os.path.join(staging_dir, 'workspace')
    src_wb = os.path.join(workspace_src, '.workbuddy')
    dst_wb = os.path.join(target, '.workbuddy')

    # 2b. 记忆冲突守卫：auto 同名匹配到的空间若已含同名 memory 文件，不静默选定
    if source == 'auto' and os.path.isdir(src_wb) and os.path.isdir(target):
        mem_conf = fs_safety.memory_conflicts(src_wb, dst_wb)
        if mem_conf:
            print()
            print('  [!] 自动匹配到的目标工作空间已含同名记忆文件，覆盖会丢失本机记忆：')
            for p in mem_conf:
                print(f'      - {os.path.join(dst_wb, p)}')
            print('      按安全规则，自动匹配不安全，已暂停（不自动选定目标）。')
            print('      确认要写入该工作空间时，用 --target-workdir 显式指定后重跑：')
            print(f'        --target-workdir "{target}" --apply')
            raise SystemExit(2)

    # 2c. overwrite detection (删除/覆盖语义 — 覆盖前先备份)
    jsonl_dst_preview = os.path.join(PROJECTS_DIR, encoded, f'{conv_id}.jsonl')
    will_overwrite = os.path.exists(jsonl_dst_preview) or wb_db.session_exists(conv_id)
    if will_overwrite:
        print('  [!] 本机已存在该会话，本次为「覆盖」：')
        if os.path.exists(jsonl_dst_preview):
            print(f'      - 对话记录将被覆盖（写前备份）：{jsonl_dst_preview}')
        if wb_db.session_exists(conv_id):
            print(f'      - workbuddy.db 的 sessions 行将被覆盖（写前备份整库）')
        print()

    # 3. plan
    plan = []
    jsonl_src = os.path.join(staging_dir, 'projects', manifest['sourceProjectsDir'],
                             f'{conv_id}.jsonl')
    jsonl_dst = os.path.join(PROJECTS_DIR, encoded, f'{conv_id}.jsonl')
    plan.append(('对话历史', jsonl_src, jsonl_dst))

    if os.path.isdir(workspace_src):
        for sub in ('outputs', '.workbuddy'):
            s = os.path.join(workspace_src, sub)
            if os.path.isdir(s):
                plan.append((f'工作空间/{sub}', s, os.path.join(target, sub)))

    print('将写入:')
    for label, src, dst in plan:
        print(f'  [{label}]')
        print(f'    {dst}')
        if label.startswith('工作空间/'):
            for rel, status in fs_safety.plan_conflicts(src, dst):
                mark = '内容相同' if status == 'same' else '★内容不同★将被覆盖'
                print(f'         [覆盖] {rel}  ({mark})')
    print(f'  [会话索引] {SESSIONS_INDEX} (合并条目, 遗留缓存, 写前备份)')
    if wb_db.db_available():
        print(f'  [SQLite]   {wb_db.DB_PATH}  (先备份整库)')
        print(f'             workspaces += {target}    <- 让工作空间出现在列表里')
        print(f'             sessions   += {conv_id}    <- 让会话出现在列表里')
    else:
        print('  [SQLite]   未找到 workbuddy.db，跳过（旧版只认 sessions.json）')
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

    # 4a2. create the target workspace only if the user confirmed
    if not os.path.isdir(target):
        os.makedirs(target, exist_ok=True)
        print(f'已创建目标工作空间 -> {target}')

    # 4b. conversation history (覆盖前先备份)
    if not os.path.exists(jsonl_src):
        raise SystemExit(f'错误：staging 中缺少对话历史：{jsonl_src}')
    os.makedirs(os.path.dirname(jsonl_dst), exist_ok=True)
    jbak = backup_file(jsonl_dst)
    if jbak:
        print(f'已备份原对话记录 -> {jbak}')
    shutil.copy2(jsonl_src, jsonl_dst)
    print(f'已写入对话历史 -> {jsonl_dst}')

    # 4c. workspace files (覆盖前逐文件备份)
    for label, src, dst in plan[1:]:
        copied, backups = fs_safety.copy_tree_with_backup(src, dst)
        for b in backups:
            print(f'已备份原文件 -> {b}')
        print(f'已写入 {label} -> {dst} ({copied} 个文件，备份 {len(backups)} 个)')

    # 4d. merge sessions.json (legacy cache — kept for older builds)
    new_data, replaced = merge_session_entry(conv_id, target, manifest)
    with open(SESSIONS_INDEX, 'w', encoding='utf-8') as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
    verb = '更新' if replaced else '新增'
    print(f'已{verb}会话索引条目: {conv_id}')

    # 4e. SQLite registration — the step that actually makes it visible in the UI
    if wb_db.db_available():
        # 覆盖 or 新增（决定是否已提示备份）
        db_had = wb_db.session_exists(conv_id)
        for p in wb_db.backup_db():
            print(f'已备份数据库 -> {p}')

        # timestamps: prefer manifest (v2+), else derive from the staged JSONL
        created_ms = manifest.get('createdAtMs')
        updated_ms = manifest.get('updatedAtMs')
        if not created_ms or not updated_ms:
            f_ts, l_ts = jsonl_times(jsonl_dst)
            created_ms = created_ms or f_ts
            updated_ms = updated_ms or l_ts

        ws_had = wb_db.workspace_exists(target)
        wb_db.register_workspace(target)
        wb_db.register_session(conv_id, target,
                               manifest.get('title') or get_leaf_name(target),
                               created_ms, updated_ms,
                               model=manifest.get('model'))
        w, s = wb_db.count()
        print(f'已{"覆盖" if db_had else "新增"} sessions 行；'
              f'workspaces {"已有" if ws_had else "新增"}工作空间行')
        print(f'已注册工作空间 + 会话到 workbuddy.db (workspaces={w}, sessions={s})')
        print(f'  回读: {wb_db.get_session(conv_id)}')
    else:
        print('未找到 workbuddy.db，跳过 SQLite 注册。')

    print()
    print('导入完成。请完全退出并重启 WorkBuddy。')
    print('提示：重启后在左侧"工作空间"里打开该工作空间，会话列表即可看到。')
    print('      若仍看不到，重新运行本脚本的 --apply（备份文件可回滚）。')
    return 0


def main():
    ap = argparse.ArgumentParser(description='Import a staged session (hardcore sync)')
    ap.add_argument('staging_dir', help='staging directory (from sync_export / GitHub download)')
    ap.add_argument('--target-workdir', help='explicit target workspace path on this device')
    ap.add_argument('--create-workspace', action='store_true',
                    help='确认新建目标工作空间（路径不存在时必需，避免误建）')
    ap.add_argument('--force', action='store_true',
                    help='平台不匹配时仍继续（默认中止）')
    ap.add_argument('--apply', action='store_true', help='actually write (default: dry-run)')
    args = ap.parse_args()

    sys.exit(import_session(args.staging_dir, args.target_workdir, args.apply,
                            force=args.force, create_workspace=args.create_workspace))


if __name__ == '__main__':
    main()
