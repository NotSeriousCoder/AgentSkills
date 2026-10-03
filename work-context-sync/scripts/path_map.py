#!/usr/bin/env python3
"""
Path mapping for work-context-sync hardcore mode.

WorkBuddy stores each session's conversation log at:
    ~/.workbuddy/projects/<encoded-workdir>/<conversationId>.jsonl

where <encoded-workdir> is derived from the session's absolute workDir:
    C:\\Users\\71026\\WorkBuddy\\2026-10-02-00-59-02
      -> c-Users-71026-WorkBuddy-2026-10-02-00-59-02

This module handles encoding, reverse lookup, and cross-device workspace matching.
"""

import os
import sys
import json
import glob

WORKBUDDY_HOME = os.path.expanduser('~/.workbuddy')
SESSIONS_INDEX = os.path.join(WORKBUDDY_HOME, 'app', 'sessions.json')
PROJECTS_DIR = os.path.join(WORKBUDDY_HOME, 'projects')


def normalize_workdir(workdir):
    """Normalize a Windows path: forward slashes -> backslashes, strip trailing slash."""
    if not workdir:
        return workdir
    wd = workdir.replace('/', '\\')
    if len(wd) > 3 and wd.endswith('\\'):
        wd = wd[:-1]
    return wd


def encode_workdir(workdir):
    """
    Encode an absolute workDir into WorkBuddy's projects subdirectory name.
    Rule (verified against 9/9 real sessions):
        drive letter lowercased, colon dropped, backslashes -> hyphens.
    """
    wd = normalize_workdir(workdir)
    if len(wd) < 2 or wd[1] != ':':
        # Non-drive path (e.g. UNC) - best effort: just replace separators
        return wd.replace(':', '').replace('\\', '-')
    drive = wd[0].lower()
    rest = wd[2:]  # drop 'X:'
    return drive + rest.replace('\\', '-')


def decode_workdir(encoded):
    """
    Best-effort reverse of encode_workdir. Ambiguous when folder names contain
    hyphens, so prefer resolve_workdir_from_encoded() which uses sessions.json.
    """
    if not encoded:
        return None
    drive = encoded[0].upper()
    rest = encoded[1:].replace('-', '\\')
    return f'{drive}:{rest}'


def get_leaf_name(path):
    """Return the last path component (folder name)."""
    return normalize_workdir(path).rstrip('\\').split('\\')[-1]


def read_sessions_index():
    """Read the local WorkBuddy sessions.json, returning a dict or empty structure."""
    if not os.path.exists(SESSIONS_INDEX):
        return {'version': 1, 'sessions': []}
    try:
        with open(SESSIONS_INDEX, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {'version': 1, 'sessions': []}


def get_session_entry(conversation_id):
    """Get a single session entry from sessions.json by conversationId."""
    data = read_sessions_index()
    for s in data.get('sessions', []):
        if s.get('conversationId') == conversation_id:
            return s
    return None


def resolve_workdir_from_encoded(encoded):
    """
    Given an encoded projects subdirectory name, find the real workDir.
    Prefer sessions.json lookup; fall back to decode.
    """
    data = read_sessions_index()
    for s in data.get('sessions', []):
        if encode_workdir(s.get('workDir', '')) == encoded:
            return s.get('workDir')
    return decode_workdir(encoded)


def list_local_workspaces():
    """
    Collect candidate workspaces on this machine:
    - all workDirs referenced in sessions.json
    - directories under the common WorkBuddy root (~/WorkBuddy)
    Returns a de-duplicated list of absolute paths.
    """
    workspaces = []
    seen = set()

    def add(p):
        p = normalize_workdir(p)
        if p and p not in seen:
            seen.add(p)
            workspaces.append(p)

    for s in read_sessions_index().get('sessions', []):
        if s.get('workDir'):
            add(s['workDir'])

    # Common convention: <home>/WorkBuddy/<name>
    common_root = os.path.join(os.path.expanduser('~'), 'WorkBuddy')
    if os.path.isdir(common_root):
        for entry in os.listdir(common_root):
            full = os.path.join(common_root, entry)
            if os.path.isdir(full):
                add(full)

    return workspaces


def find_matching_workspace(leaf_name, exclude=None):
    """
    Find local workspaces whose last directory component equals leaf_name.
    Returns a list of matching absolute paths (best-effort, may be empty or >1).
    """
    matches = []
    for ws in list_local_workspaces():
        if exclude and normalize_workdir(ws) == normalize_workdir(exclude):
            continue
        if get_leaf_name(ws) == leaf_name:
            matches.append(ws)
    return matches


def ensure_projects_dir(encoded):
    """Create the projects/<encoded> directory if missing; return its path."""
    target = os.path.join(PROJECTS_DIR, encoded)
    os.makedirs(target, exist_ok=True)
    return target


def main():
    if len(sys.argv) < 2:
        print("Usage: path_map.py <command> [args]")
        print("Commands:")
        print("  encode <workdir>              -> encoded folder name")
        print("  decode <encoded>              -> workdir (best effort)")
        print("  leaf <workdir>                -> last folder name")
        print("  resolve <encoded>             -> workdir via sessions.json")
        print("  workspaces                    -> list local workspaces")
        print("  match <leaf_name>             -> find workspaces with same leaf name")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == 'encode':
        print(encode_workdir(sys.argv[2]))
    elif cmd == 'decode':
        print(decode_workdir(sys.argv[2]))
    elif cmd == 'leaf':
        print(get_leaf_name(sys.argv[2]))
    elif cmd == 'resolve':
        print(resolve_workdir_from_encoded(sys.argv[2]))
    elif cmd == 'workspaces':
        for w in list_local_workspaces():
            print(w)
    elif cmd == 'match':
        leaf = sys.argv[2]
        matches = find_matching_workspace(leaf)
        if matches:
            print(f"找到 {len(matches)} 个同名工作空间（末级目录 = {leaf}）:")
            for m in matches:
                print(f"  {m}")
                print(f"    -> projects 目录名: {encode_workdir(m)}")
        else:
            print(f"未找到同名工作空间（末级目录 = {leaf}）")
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)


if __name__ == '__main__':
    main()
