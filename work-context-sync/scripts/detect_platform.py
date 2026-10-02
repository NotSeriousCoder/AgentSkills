#!/usr/bin/env python3
"""
Platform detection for work-context-sync skill.
Scans common paths to determine which AI assistant product is installed,
so the skill can locate the correct conversation storage location.
"""

import os
import sys
import json
import glob
from pathlib import Path

# Path to platforms.json (same directory as this script)
PLATFORMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'platforms.json')


def expand_path(pattern):
    """Expand ~ and environment variables in a path pattern."""
    expanded = os.path.expandvars(pattern)
    if expanded.startswith('~'):
        expanded = os.path.expanduser(expanded)
    return expanded


def detect_platforms():
    """Detect which platforms are installed by scanning their storage paths."""
    if not os.path.exists(PLATFORMS_FILE):
        return []

    with open(PLATFORMS_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)

    detected = []
    for platform in config.get('platforms', []):
        matched_paths = []
        for pattern in platform.get('conversation_logs_patterns', []):
            expanded = expand_path(pattern)
            matches = glob.glob(expanded, recursive=True)
            if matches:
                matched_paths.extend(matches)

        # Also check sessions_index path if present
        if platform.get('sessions_index'):
            index_path = expand_path(platform['sessions_index'])
            if os.path.exists(index_path):
                matched_paths.append(index_path)

        if matched_paths:
            detected.append({
                'id': platform['id'],
                'name': platform['name'],
                'vendor': platform['vendor'],
                'supported': platform.get('supported', False),
                'matched_paths': matched_paths,
                'note': platform.get('note', '')
            })

    return detected


def get_platform_by_id(platform_id):
    """Get platform config by ID."""
    if not os.path.exists(PLATFORMS_FILE):
        return None

    with open(PLATFORMS_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)

    for platform in config.get('platforms', []):
        if platform['id'] == platform_id:
            return platform
    return None


def list_all_platforms():
    """List all registered platforms (regardless of detection)."""
    if not os.path.exists(PLATFORMS_FILE):
        return []

    with open(PLATFORMS_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)

    return config.get('platforms', [])


def format_detection_result(detected):
    """Format detection result for user display."""
    if not detected:
        return "No known platform detected."

    lines = []
    for d in detected:
        support = "支持" if d['supported'] else "未完全支持"
        lines.append(f"- {d['name']}（{d['vendor']}）[{support}]")
        for p in d['matched_paths'][:3]:
            lines.append(f"  路径: {p}")
    return '\n'.join(lines)


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: detect_platform.py <command> [args]")
        print("Commands: detect [--json], list, get <platform_id>")
        sys.exit(1)

    command = sys.argv[1]
    json_only = '--json' in sys.argv

    if command == 'detect':
        detected = detect_platforms()
        if json_only:
            # Clean JSON output (drop verbose matched_paths for agent parsing)
            clean = []
            for d in detected:
                clean.append({
                    'id': d['id'],
                    'name': d['name'],
                    'vendor': d['vendor'],
                    'supported': d['supported'],
                    'note': d['note']
                })
            print(json.dumps(clean, ensure_ascii=False))
        else:
            if detected:
                print(f"检测到 {len(detected)} 个平台:")
                print(format_detection_result(detected))
            else:
                print("未检测到已知平台")

    elif command == 'list':
        platforms = list_all_platforms()
        print(f"已注册 {len(platforms)} 个平台:")
        for p in platforms:
            print(f"  - {p['id']}: {p['name']}（{p['vendor']}）")

    elif command == 'get':
        if len(sys.argv) < 3:
            print("Usage: detect_platform.py get <platform_id>")
            sys.exit(1)
        platform = get_platform_by_id(sys.argv[2])
        if platform:
            print(json.dumps(platform, ensure_ascii=False, indent=2))
        else:
            print(f"平台不存在: {sys.argv[2]}")
            sys.exit(1)

    else:
        print(f"未知命令: {command}")
        sys.exit(1)


if __name__ == '__main__':
    main()
