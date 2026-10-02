#!/usr/bin/env python3
"""
Local session ID management for work-context-sync skill.
Stores custom session IDs in workspace .workbuddy/session-sync.json.
"""

import json
import os
import sys
import uuid
from pathlib import Path
from datetime import datetime


def get_sync_metadata_path(work_dir):
    """Get the path to session sync metadata file."""
    return Path(work_dir) / '.workbuddy' / 'session-sync.json'


def read_sync_metadata(work_dir):
    """Read session sync metadata from workspace."""
    meta_path = get_sync_metadata_path(work_dir)
    if meta_path.exists():
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def write_sync_metadata(work_dir, metadata):
    """Write session sync metadata to workspace."""
    meta_path = get_sync_metadata_path(work_dir)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)


def get_local_session_id(work_dir, conversation_id):
    """Get custom session ID for a conversation, if it exists."""
    metadata = read_sync_metadata(work_dir)
    sessions = metadata.get('sessions', {})
    session_data = sessions.get(conversation_id, {})
    return session_data.get('custom_id')


def set_local_session_id(work_dir, conversation_id, custom_id, title=None, github_path=None, platform=None):
    """Set custom session ID for a conversation."""
    metadata = read_sync_metadata(work_dir)
    if 'sessions' not in metadata:
        metadata['sessions'] = {}

    metadata['sessions'][conversation_id] = {
        'custom_id': custom_id,
        'title': title,
        'github_path': github_path,
        'last_synced': datetime.now().isoformat(),
        'created_at': metadata['sessions'].get(conversation_id, {}).get('created_at', datetime.now().isoformat())
    }

    if platform:
        metadata['platform'] = platform

    write_sync_metadata(work_dir, metadata)
    return custom_id


def set_platform(work_dir, platform_id):
    """Record the detected/confirmed platform for a workspace."""
    metadata = read_sync_metadata(work_dir)
    metadata['platform'] = platform_id
    write_sync_metadata(work_dir, metadata)
    return platform_id


def get_platform(work_dir):
    """Get the recorded platform for a workspace, if any."""
    metadata = read_sync_metadata(work_dir)
    return metadata.get('platform')


def generate_custom_id():
    """Generate a unique custom session ID."""
    return f"wcs-{uuid.uuid4().hex[:12]}"


def get_session_info(work_dir, conversation_id):
    """Get full session info for a conversation."""
    metadata = read_sync_metadata(work_dir)
    sessions = metadata.get('sessions', {})
    return sessions.get(conversation_id, {})


def list_local_sessions(work_dir):
    """List all locally tracked sessions."""
    metadata = read_sync_metadata(work_dir)
    return metadata.get('sessions', {})


def update_last_synced(work_dir, conversation_id):
    """Update the last_synced timestamp for a session."""
    metadata = read_sync_metadata(work_dir)
    if 'sessions' in metadata and conversation_id in metadata['sessions']:
        metadata['sessions'][conversation_id]['last_synced'] = datetime.now().isoformat()
        write_sync_metadata(work_dir, metadata)


def get_all_workspaces_sessions():
    """Get all sessions from all workspaces."""
    workbuddy_dir = Path.home() / '.workbuddy'
    sessions_json = workbuddy_dir / 'app' / 'sessions.json'

    if not sessions_json.exists():
        return []

    try:
        with open(sessions_json, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return []

    all_sessions = []
    for sess in data.get('sessions', []):
        work_dir = sess.get('workDir', '')
        conv_id = sess.get('conversationId', '')
        meta = get_session_info(work_dir, conv_id)
        all_sessions.append({
            'conversation_id': conv_id,
            'work_dir': work_dir,
            'custom_id': meta.get('custom_id'),
            'title': meta.get('title'),
            'last_synced': meta.get('last_synced'),
            'started_at': sess.get('startedAt'),
            'resumed_at': sess.get('resumedAt')
        })

    return all_sessions


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: manage_session_id.py <command> [args...]")
        print("Commands: get, set, list, generate")
        sys.exit(1)

    command = sys.argv[1]

    if command == 'get':
        if len(sys.argv) < 4:
            print("Usage: manage_session_id.py get <work_dir> <conversation_id>")
            sys.exit(1)
        work_dir = sys.argv[2]
        conv_id = sys.argv[3]
        custom_id = get_local_session_id(work_dir, conv_id)
        if custom_id:
            print(custom_id)
        else:
            print("(no custom ID)")

    elif command == 'set':
        if len(sys.argv) < 5:
            print("Usage: manage_session_id.py set <work_dir> <conversation_id> <custom_id>")
            sys.exit(1)
        work_dir = sys.argv[2]
        conv_id = sys.argv[3]
        custom_id = sys.argv[4]
        set_local_session_id(work_dir, conv_id, custom_id)
        print(f"Set custom ID: {custom_id}")

    elif command == 'list':
        if len(sys.argv) < 3:
            print("Usage: manage_session_id.py list <work_dir>")
            sys.exit(1)
        work_dir = sys.argv[2]
        sessions = list_local_sessions(work_dir)
        for conv_id, data in sessions.items():
            print(f"  {conv_id}: {data.get('custom_id', 'N/A')} (synced: {data.get('last_synced', 'never')})")

    elif command == 'generate':
        print(generate_custom_id())

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == '__main__':
    main()
