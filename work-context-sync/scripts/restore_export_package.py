#!/usr/bin/env python3
"""
Hardcore sync: IMPORT a WorkBuddy "会话导出包" (session export package).

This handles the export format produced by WorkBuddy's built-in session export
tool — NOT the sync_export.py staging folder. Layout of that package:

    <包名>_会话导出/
      会话原始文件/<convId>.jsonl              # core conversation log
      会话原始文件/<convId>.meta.json
      会话原始文件/<convId>.file-rollback.ndjson
      工作空间存档/.workbuddy/...               # memory + workspace assets
      恢复会话.py  /  恢复说明.txt  /  <title>.md

The package ships its own 恢复会话.py, but it only copies files and does NOT
register the session — so the session never shows up in the WorkBuddy session list.
This script fixes that. Full flow (matches the confirmed flow chart):

    解压 → 解析包内 MD5 清单做完整性校验 → 确认目标工作空间 → 备份 → 覆盖写入
        → 注册 workbuddy.db(workspaces + sessions)

Usage:
    python scripts/restore_export_package.py <包目录或.zip> --workspace <本机路径>
    python scripts/restore_export_package.py <包目录或.zip>                 # 自动匹配同名工作空间
    python scripts/restore_export_package.py <包目录或.zip> ... --apply

Safety:
  - 完整性校验未通过 → 中止（除非 --force）
  - 目标工作空间不存在 → 只给「建议路径」，需用户确认后加 --create-workspace
  - 覆盖已有文件前先备份（逐文件 .bak-<时间戳>，含 .workbuddy 记忆），DB 写前备份整库
  - auto 同名匹配到的空间若已含同名 memory/ 文件 → 停止（退出码 2），需 --workspace 显式确认
  - 源平台固定记为 workbuddy（该导出格式只可能来自 WorkBuddy）

Defaults to DRY-RUN. Pass --apply to actually write.
"""

import os
import sys
import json
import glob
import shutil
import hashlib
import re
import zipfile
import argparse
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from path_map import (
    PROJECTS_DIR, SESSIONS_INDEX,
    encode_workdir, get_leaf_name, read_sessions_index,
    find_matching_workspace, normalize_workdir,
)
import wb_db
import fs_safety

RAW_DIR_NAME = '会话原始文件'
WS_ARCHIVE_NAME = '工作空间存档'


# ---------------------------------------------------------------- locating

def locate_package(path):
    """
    Accept a .zip or a directory. Return the package root directory
    (the folder that directly contains 会话原始文件/).
    Returns (pkg_root, tmpdir_or_None).
    """
    if zipfile.is_zipfile(path):
        tmp = tempfile.mkdtemp(prefix='wb_pkg_')
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
        path = tmp
    else:
        tmp = None

    # search for a dir containing 会话原始文件
    for root, dirs, _ in os.walk(path):
        if RAW_DIR_NAME in dirs:
            return root, tmp
    # fall back: maybe files sit directly at top level
    raise SystemExit(f'错误：在 {path} 中找不到「{RAW_DIR_NAME}」目录，不是有效的会话导出包。')


def read_jsonl_head_tail(jsonl_path):
    """Return (first_ts_ms, last_ts_ms, conv_id, title, source_cwd, model)."""
    first_ts = last_ts = None
    conv_id = None
    title = None
    src_cwd = None
    models = {}
    with open(jsonl_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if conv_id is None:
                conv_id = d.get('sessionId')
            if src_cwd is None and d.get('cwd'):
                src_cwd = d.get('cwd')
            if d.get('type') == 'ai-title' and d.get('aiTitle'):
                title = d['aiTitle']
            pd = d.get('providerData')
            if isinstance(pd, dict):
                for k, v in pd.items():
                    if 'model' in k.lower() and isinstance(v, str):
                        models[v] = models.get(v, 0) + 1
            ts = d.get('timestamp')
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
    model = max(models, key=models.get) if models else None
    return first_ts, last_ts, conv_id, title, src_cwd, model


# ---------------------------------------------------------------- integrity

CHECKSUM_LINE = re.compile(r'^\s*([0-9a-fA-F]{32})\s+\*?(.+?)\s*$')
CHECKSUM_INLINE = re.compile(r'MD5\s*\(([^)]+)\)\s*=\s*([0-9a-fA-F]{32})', re.I)


def find_expected_checksums(pkg_root):
    """
    Look for an MD5 manifest shipped inside the export package.

    Real packages put the hashes in 恢复说明.txt under 【文件校验】, one per line:
        <32 hex>  <filename>
    We also accept standalone *.md5 / checksum.txt style files and the
    `MD5 (file) = hash` form. Returns {basename: md5hex_lower}.
    """
    expected = {}
    candidates = []
    for fn in sorted(os.listdir(pkg_root)):
        full = os.path.join(pkg_root, fn)
        if not os.path.isfile(full):
            continue
        low = fn.lower()
        if (low.endswith('.txt') or low.endswith('.md5') or low.endswith('.md')
                or '校验' in fn or 'md5' in low or 'checksum' in low):
            candidates.append(full)

    for path in candidates:
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
        except IOError:
            continue
        for line in text.splitlines():
            m = CHECKSUM_LINE.match(line)
            if m:
                expected[os.path.basename(m.group(2))] = m.group(1).lower()
                continue
            for fm in CHECKSUM_INLINE.finditer(line):
                expected[os.path.basename(fm.group(1))] = fm.group(2).lower()
    return expected


def md5_of(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def verify_checksums(raw_dir, expected):
    """
    Verify files in raw_dir against `expected` ({basename: md5}).
    Returns (results, ok) where results = [(name, status, got, want)]
    and status in {'ok', 'mismatch', 'missing'}.
    """
    results = []
    ok = True
    for name, want in sorted(expected.items()):
        full = os.path.join(raw_dir, name)
        if not os.path.exists(full):
            # the checksum may refer to a file elsewhere in the package; skip silently
            results.append((name, 'missing', None, want))
            ok = False
            continue
        got = md5_of(full)
        if got == want:
            results.append((name, 'ok', got, want))
        else:
            results.append((name, 'mismatch', got, want))
            ok = False
    return results, ok


def iso_utc(ms):
    if ms is None:
        return datetime.now(timezone.utc).isoformat()
    dt = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{ms % 1000:03d}Z'


# ---------------------------------------------------------------- import

def restore(pkg_path, workspace=None, apply=False, use_db=True,
            create_workspace=False, force=False):
    pkg_root, tmp = locate_package(pkg_path)
    raw_dir = os.path.join(pkg_root, RAW_DIR_NAME)

    jsonls = sorted(glob.glob(os.path.join(raw_dir, '*.jsonl')))
    if not jsonls:
        raise SystemExit(f'错误：{raw_dir} 中没有 .jsonl 会话文件。')
    jsonl_src = jsonls[0]

    first_ts, last_ts, conv_id, title, src_cwd, model = read_jsonl_head_tail(jsonl_src)
    if not conv_id:
        conv_id = os.path.splitext(os.path.basename(jsonl_src))[0]

    # 这个格式只可能来自 WorkBuddy，所以源平台是确定的
    src_platform = 'workbuddy'

    print('=' * 60)
    print('会话导出包 - 恢复计划')
    print('=' * 60)
    print(f'  包目录      : {pkg_root}')
    print(f'  会话 ID     : {conv_id}')
    print(f'  标题        : {title or "(未命名)"}')
    print(f'  源工作空间  : {src_cwd or "(包内未记录)"}')
    print(f'  源平台      : {src_platform}')
    print(f'  时间跨度    : {iso_utc(first_ts)} ~ {iso_utc(last_ts)}')
    print(f'  模型        : {model or "(未知)"}')

    # --- integrity check (解压后先校验，再动任何文件) ---
    expected = find_expected_checksums(pkg_root)
    if expected:
        results, ok = verify_checksums(raw_dir, expected)
        print(f'  完整性校验  : 包内清单 {len(expected)} 项')
        for name, status, got, want in results:
            mark = {'ok': 'OK', 'mismatch': '不一致', 'missing': '清单文件缺失'}[status]
            print(f'    [{mark}] {name}')
            if status == 'mismatch':
                print(f'           期望 {want}')
                print(f'           实际 {got}')
        if not ok and not force:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            raise SystemExit('错误：完整性校验未通过，已中止（不写入任何文件）。确需继续请加 --force。')
    else:
        print('  完整性校验  : 包内未找到 MD5 清单，跳过')

    # --- pick target workspace ---
    if workspace:
        target, how = normalize_workdir(workspace), 'explicit'
    elif src_cwd and os.path.isdir(normalize_workdir(src_cwd)):
        # 源工作空间在本机就存在 → 直接用它（最省事，无需用户确认）
        target, how = normalize_workdir(src_cwd), 'same-path'
    else:
        leaf = get_leaf_name(src_cwd) if src_cwd else None
        if not leaf:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            raise SystemExit('错误：包内未记录源工作空间，请用 --workspace 指定目标路径。')
        matches = find_matching_workspace(leaf, exclude=src_cwd)
        if len(matches) == 1:
            target, how = matches[0], 'auto(同名工作空间)'
        elif len(matches) > 1:
            print(f'\n错误：本机存在多个末级目录为 "{leaf}" 的工作空间，请用 --workspace 指定：')
            for m in matches:
                print(f'  - {m}')
            raise SystemExit(2)
        else:
            suggestion = wb_db.suggest_workspace(leaf)
            print(f'\n未找到末级目录为 "{leaf}" 的本机工作空间。')
            print('请把下面的建议路径交给用户确认（不要自行创建）：')
            print(f'  建议路径 : {suggestion}')
            print('用户确认后，用确认后的路径重新运行：')
            print(f'  --workspace "<确认后的路径>" --create-workspace --apply')
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            raise SystemExit(2)

    if not os.path.isdir(target) and not create_workspace:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(
            f'错误：目标工作空间不存在：{target}\n'
            '      为避免误建目录污染工作空间列表，需用户确认后加 --create-workspace。')

    encoded = encode_workdir(target)
    dst_dir = os.path.join(PROJECTS_DIR, encoded)
    print(f'  目标工作空间: {target}  [{how}'
          + ('，将新建' if not os.path.isdir(target) else '') + ']')
    print(f'  projects 目录: {encoded}')
    print()

    src_wb = os.path.join(pkg_root, WS_ARCHIVE_NAME, '.workbuddy')
    dst_wb = os.path.join(target, '.workbuddy')
    raw_files = [(os.path.join(raw_dir, fn), os.path.join(dst_dir, fn))
                 for fn in sorted(os.listdir(raw_dir))]

    # 覆盖语义：先看有没有同名会话文件 / DB 行
    existing = [dst for _, dst in raw_files if os.path.exists(dst)]
    db_had = wb_db.session_exists(conv_id)
    if existing or db_had:
        print('  [!] 本机已存在该会话，本次为「覆盖」（写前先备份）：')
        for dst in existing:
            print(f'      - {dst}')
        if db_had:
            print('      - workbuddy.db 的 sessions 行')
        print()

    print('将写入:')
    for _, dst in raw_files:
        print(f'  {dst}')
    if os.path.isdir(src_wb):
        n = len(fs_safety.rel_files(src_wb))
        conflicts = fs_safety.plan_conflicts(src_wb, dst_wb)
        extra = f'，其中 {len(conflicts)} 个将覆盖现有文件' if conflicts else ''
        print(f'  {dst_wb}   ({n} 个文件{extra})')
        for rel, status in conflicts:
            mark = '内容相同' if status == 'same' else '★内容不同★将被覆盖'
            print(f'      [覆盖] {rel}  ({mark})')
    print(f'  [会话索引] {SESSIONS_INDEX}  (合并条目, 写入前先备份)')
    if use_db and wb_db.db_available():
        print(f'  [SQLite]   {wb_db.DB_PATH}  (先备份整库)')
        print(f'             workspaces += {target}          <- 让工作空间出现在列表里')
        print(f'             sessions   += {conv_id}          <- 让会话出现在列表里')
    elif use_db:
        print('  [SQLite]   未找到 workbuddy.db，跳过（旧版 WorkBuddy 只认 sessions.json）')
    print()

    # --- 记忆冲突守卫：auto 同名匹配到的空间若已含同名 memory 文件，不静默选定 ---
    #     放在"打印完整计划之后、真正写入之前"，让你先看清全部将覆盖文件，再决定。
    if how.startswith('auto') and os.path.isdir(src_wb):
        mem_conf = fs_safety.memory_conflicts(src_wb, dst_wb)
        if mem_conf:
            print('  [!] 自动匹配到的目标工作空间已含同名记忆文件，覆盖会丢失本机记忆：')
            for p in mem_conf:
                print(f'      - {os.path.join(dst_wb, p)}')
            print('      按安全规则，自动匹配不安全，已暂停（不自动选定目标）。')
            print('      确认要写入该工作空间时，用 --workspace 显式指定后重跑：')
            print(f'        --workspace "{target}" --apply')
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            raise SystemExit(2)

    if not apply:
        print('以上为 DRY-RUN，未写入任何文件。加 --apply 执行。')
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0

    # --- 0. create workspace only if confirmed ---
    if not os.path.isdir(target):
        os.makedirs(target, exist_ok=True)
        print(f'已创建目标工作空间 -> {target}')

    # --- 1. raw session files (覆盖前先备份) ---
    os.makedirs(dst_dir, exist_ok=True)
    for src, dst in raw_files:
        if os.path.exists(dst):
            bak = f'{dst}.bak-{datetime.now().strftime("%Y%m%d-%H%M%S")}'
            shutil.copy2(dst, bak)
            print(f'已备份原文件 -> {bak}')
        shutil.copy2(src, dst)
        print(f'已写入 {dst}')

    # --- 2. workspace archive (覆盖前逐文件备份) ---
    if os.path.isdir(src_wb):
        copied, backups = fs_safety.copy_tree_with_backup(src_wb, dst_wb)
        for b in backups:
            print(f'已备份原文件 -> {b}')
        print(f'已写入工作空间存档 -> {dst_wb} ({copied} 个文件，备份 {len(backups)} 个)')

    # --- 3. merge sessions.json ---
    if os.path.exists(SESSIONS_INDEX):
        bak = f'{SESSIONS_INDEX}.bak-{datetime.now().strftime("%Y%m%d-%H%M%S")}'
        shutil.copy2(SESSIONS_INDEX, bak)
        print(f'已备份会话索引 -> {bak}')

    data = read_sessions_index()
    sessions = data.get('sessions', [])
    local_uid = next((s.get('userId') for s in sessions if s.get('userId')), '')

    entry = {
        'conversationId': conv_id,
        'userId': local_uid,
        'workDir': target,
        'startedAt': iso_utc(first_ts),
        'resumedAt': iso_utc(last_ts),
    }
    sessions = [s for s in sessions if s.get('conversationId') != conv_id]
    # 按 resumedAt 倒序插入，保持与原列表一致的排序
    pos = len(sessions)
    for i, s in enumerate(sessions):
        if (s.get('resumedAt') or '') < entry['resumedAt']:
            pos = i
            break
    sessions.insert(pos, entry)
    data['sessions'] = sessions
    data['updatedAt'] = datetime.now(timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S.') + f'{datetime.now().microsecond // 1000:03d}Z'

    with open(SESSIONS_INDEX, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f'已合并会话索引条目 (第 {pos + 1} 位 / 共 {len(sessions)} 条)')

    # --- 4. SQLite registration (authoritative for the UI) ---
    if use_db and wb_db.db_available():
        for p in wb_db.backup_db():
            print(f'已备份数据库 -> {p}')
        ws_had = wb_db.workspace_exists(target)
        wb_db.register_workspace(target)
        wb_db.register_session(conv_id, target, title or conv_id,
                               first_ts, last_ts, model=model)
        w, s = wb_db.count()
        print(f'已{"覆盖" if db_had else "新增"} sessions 行；'
              f'workspaces {"已有" if ws_had else "新增"}工作空间行')
        print(f'已注册工作空间 + 会话到 workbuddy.db (workspaces={w}, sessions={s})')
        print(f'  回读: {wb_db.get_session(conv_id)}')
    elif use_db:
        print('未找到 workbuddy.db，跳过 SQLite 注册。')

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print('恢复完成。请**完全退出**并重启 WorkBuddy。')
    print('提示：重启后在左侧"工作空间"里打开该工作空间，会话列表即可看到。')
    print('      若仍看不到，再次运行本脚本加 --apply（备份文件可回滚）。')
    return 0


def main():
    ap = argparse.ArgumentParser(
        description='恢复 WorkBuddy 会话导出包（含会话索引注册）')
    ap.add_argument('package', help='导出包目录或 .zip 路径')
    ap.add_argument('--workspace', help='本机目标工作空间绝对路径（缺省则按同名自动匹配）')
    ap.add_argument('--apply', action='store_true', help='实际写入（默认仅 dry-run）')
    ap.add_argument('--create-workspace', action='store_true',
                    help='确认新建目标工作空间（路径不存在时必需，避免误建）')
    ap.add_argument('--force', action='store_true',
                    help='完整性校验不通过时仍继续（默认中止）')
    ap.add_argument('--no-db', action='store_true',
                    help='跳过 workbuddy.db 注册（仅写文件，旧版 WorkBuddy 才需要）')
    a = ap.parse_args()
    sys.exit(restore(a.package, a.workspace, a.apply, use_db=not a.no_db,
                     create_workspace=a.create_workspace, force=a.force))


if __name__ == '__main__':
    main()
