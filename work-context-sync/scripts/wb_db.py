#!/usr/bin/env python3
"""
WorkBuddy SQLite registry (workbuddy.db) — session/workspace registration.

IMPORTANT (verified on WorkBuddy 2.115.0, 2026-10-04):

WorkBuddy's session list and workspace list are served from SQLite, NOT from
`~/.workbuddy/app/sessions.json`. That JSON file is a legacy cache: it is still
rewritten, but adding an entry to it alone will NOT make a session appear in the UI.

The authoritative stores are two tables in `~/.workbuddy/workbuddy.db`:

    workspaces(path TEXT PRIMARY KEY, last_opened_at INTEGER)      -- 最近工作空间列表
    sessions(id TEXT PRIMARY KEY, cwd TEXT, user_id TEXT, title TEXT, ...)

So restoring a foreign session onto a new device requires THREE things:
    1. conversation log  ~/.workbuddy/projects/<encoded>/<convId>.jsonl
    2. a `sessions` row      (cwd = the device-local workspace path)
    3. a `workspaces` row    (so the workspace itself appears in the picker)

Evidence: `sessions.json` (10-11 entries) and `workspace/sessions/` (10 dirs)
both mirror subsets of the 17-18 rows in the `sessions` table; the May/June
sessions exist ONLY in the DB.

Always back up the DB (+ -wal / -shm) before writing: the app has it open.
"""

import os
import json
import shutil
import sqlite3
from datetime import datetime

WORKBUDDY_HOME = os.path.expanduser(os.path.join('~', '.workbuddy'))
DB_PATH = os.path.join(WORKBUDDY_HOME, 'workbuddy.db')
SESSIONS_JSON = os.path.join(WORKBUDDY_HOME, 'app', 'sessions.json')

DEFAULT_PLUGIN_CTX = (
    '{"version":1,"selectedPluginIds":[],"microSceneIds":[],'
    '"projectResources":{"connectorSkillIds":[],"mcpServerNames":[],'
    '"projectSkillNames":[]},"pluginSettings":{"enabledPlugins":{},'
    '"extraKnownMarketplaces":{}}}'
)
DEFAULT_EXPERT_SEL = '{"version":1,"selection":null}'


def db_available():
    return os.path.exists(DB_PATH)


def backup_db(tag=None):
    """Copy workbuddy.db (+ -wal/-shm) aside. Returns list of created backup paths."""
    tag = tag or datetime.now().strftime('%Y%m%d-%H%M%S')
    made = []
    for ext in ('', '-wal', '-shm'):
        src = DB_PATH + ext
        if os.path.exists(src):
            dst = f'{src}.bak-{tag}'
            shutil.copy2(src, dst)
            made.append(dst)
    return made


def connect():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute('PRAGMA busy_timeout = 15000')
    return con


def local_user_id(cur):
    cur.execute('SELECT user_id FROM sessions WHERE user_id IS NOT NULL LIMIT 1')
    row = cur.fetchone()
    return row[0] if row else ''


def register_workspace(path, when_ms=None):
    """Upsert into `workspaces` so the workspace shows up in WorkBuddy's picker."""
    when_ms = when_ms or int(datetime.now().timestamp() * 1000)
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            'INSERT INTO workspaces(path, last_opened_at) VALUES(?,?) '
            'ON CONFLICT(path) DO UPDATE SET last_opened_at=excluded.last_opened_at',
            (path, when_ms))
        con.commit()
    finally:
        con.close()
    return when_ms


def session_exists(conv_id):
    """True if a `sessions` row already exists for this conversation id."""
    if not db_available():
        return False
    con = connect()
    try:
        cur = con.cursor()
        cur.execute('SELECT 1 FROM sessions WHERE id=? LIMIT 1', (conv_id,))
        return cur.fetchone() is not None
    finally:
        con.close()


def workspace_exists(path):
    """True if this exact workspace path is already registered."""
    if not db_available():
        return False
    con = connect()
    try:
        cur = con.cursor()
        cur.execute('SELECT 1 FROM workspaces WHERE path=? LIMIT 1', (path,))
        return cur.fetchone() is not None
    finally:
        con.close()


def suggest_workspace(leaf, root=None):
    """
    Suggest a local workspace path for a session whose source workspace is not
    present on this device. The caller (agent) must show this to the user and
    get confirmation before actually creating it — never create silently.
    """
    root = root or os.path.join(os.path.expanduser('~'), 'WorkBuddy')
    return os.path.join(root, leaf) if leaf else os.path.join(root, '未命名工作空间')


def register_session(conv_id, cwd, title, created_at_ms, updated_at_ms,
                     model=None, user_id=None, status='completed',
                     mode='craft', permission_mode='bypassPermissions',
                     use_sandbox_cli=1, is_playground=0,
                     source_mode='working', plugin_context_json=None,
                     expert_selection=None):
    """
    Overwrite-or-insert a row into the `sessions` table. `cwd` must be the LOCAL
    workspace path. Timestamps are epoch milliseconds. Unspecified columns fall
    back to the defaults observed in real sessions.

    语义 = 「覆盖」(delete-then-insert)：导入流程里对已存在的 convId 就是覆盖写入，
    不做保留合并。调用方负责先 `backup_db()`，并在覆盖已有会话文件前另行备份 jsonl。
    Use `session_exists()` first if you need to report 覆盖 vs 新增 to the user.
    """
    con = connect()
    try:
        cur = con.cursor()
        uid = user_id or local_user_id(cur)
        cur.execute('DELETE FROM sessions WHERE id = ?', (conv_id,))
        cur.execute(
            '''INSERT INTO sessions
            (id, cwd, user_id, title, custom_title, status, created_at, updated_at,
             deleted_at, is_playground, model, last_activity_at, source_mode,
             is_background_automation, expert_id, expert_locale, expert_runtime_identity,
             expert_marketplace, permission_mode, use_sandbox_cli, project_id, mode,
             plugin_context_json, last_user_prompt_expert_selection)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (conv_id, cwd, uid, title, None, status, created_at_ms, updated_at_ms,
             None, is_playground, model, updated_at_ms, source_mode,
             None, None, None, None,
             None, permission_mode, use_sandbox_cli, None, mode,
             plugin_context_json or DEFAULT_PLUGIN_CTX,
             expert_selection or DEFAULT_EXPERT_SEL))
        con.commit()
    finally:
        con.close()


def get_session(conv_id):
    if not db_available():
        return None
    con = connect()
    try:
        cur = con.cursor()
        cur.execute('SELECT id, cwd, title, status FROM sessions WHERE id=?', (conv_id,))
        return cur.fetchone()
    finally:
        con.close()


def get_session_full(conv_id):
    """
    Return the full `sessions` row as a dict (column -> value), or None.
    Prefer this over `app/sessions.json` when exporting: the DB is authoritative.
    """
    if not db_available():
        return None
    con = connect()
    try:
        cur = con.cursor()
        cur.execute('SELECT * FROM sessions WHERE id=?', (conv_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        con.close()


def resolve_workdir(conv_id):
    """Return the local workDir for a session, preferring the DB, then sessions.json."""
    row = get_session_full(conv_id)
    if row and row.get('cwd'):
        return row['cwd']
    try:
        with open(SESSIONS_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for s in data.get('sessions', []):
            if s.get('conversationId') == conv_id:
                return s.get('workDir')
    except (IOError, ValueError):
        pass
    return None


def count():
    if not db_available():
        return (None, None)
    con = connect()
    try:
        cur = con.cursor()
        cur.execute('SELECT count(*) FROM workspaces')
        w = cur.fetchone()[0]
        cur.execute('SELECT count(*) FROM sessions')
        s = cur.fetchone()[0]
        return (w, s)
    finally:
        con.close()


def list_workspaces():
    """Return [(path, last_opened_at), ...] newest first."""
    if not db_available():
        return []
    con = connect()
    try:
        cur = con.cursor()
        cur.execute('SELECT path, last_opened_at FROM workspaces ORDER BY last_opened_at DESC')
        return cur.fetchall()
    finally:
        con.close()


def main():
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        print('Commands:')
        print('  show                    -> counts + workspace list')
        print('  backup                  -> back up the db')
        print('  get <convId>            -> one session row')
        print('  exists <convId>         -> whether a sessions row exists (覆盖 vs 新增)')
        print('  suggest <leaf>          -> suggest a local workspace path for a leaf name')
        return
    cmd = sys.argv[1]
    if cmd == 'show':
        print('db:', DB_PATH, 'exists=', db_available())
        print('counts (workspaces, sessions):', count())
        con = connect()
        cur = con.cursor()
        cur.execute('SELECT path, last_opened_at FROM workspaces ORDER BY last_opened_at DESC')
        for p, t in cur.fetchall():
            print('  ', datetime.fromtimestamp(t / 1000).strftime('%Y-%m-%d %H:%M:%S'), p)
        con.close()
    elif cmd == 'backup':
        for p in backup_db():
            print('backed up ->', p)
    elif cmd == 'get':
        print(get_session(sys.argv[2]))
    elif cmd == 'exists':
        print(session_exists(sys.argv[2]))
    elif cmd == 'suggest':
        print(suggest_workspace(sys.argv[2]))
    else:
        print('未知命令:', cmd)


if __name__ == '__main__':
    main()
