#!/usr/bin/env python3
"""
Extract session context from WorkBuddy JSONL conversation logs.
Generates a structured SESSION_CONTEXT.md for cross-device sync.
"""

import json
import sys
import os
from datetime import datetime
from pathlib import Path


def parse_jsonl(jsonl_path):
    """Parse WorkBuddy conversation JSONL file."""
    messages = []
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                messages.append(msg)
            except json.JSONDecodeError:
                continue
    return messages


import re


def extract_user_queries(messages):
    """Extract all user queries from messages, filtering out system reminders."""
    queries = []
    for msg in messages:
        if msg.get('type') == 'message' and msg.get('role') == 'user':
            content = msg.get('content', [])
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'input_text':
                    text = part.get('text', '')
                    # Extract content from <user_query> tags if present
                    user_query_match = re.search(r'<user_query>(.*?)</user_query>', text, re.DOTALL)
                    if user_query_match:
                        query_text = user_query_match.group(1).strip()
                        if query_text:
                            text_parts.append(query_text)
                    else:
                        # If no user_query tag, check if it's mostly system reminder
                        if '<system-reminder' not in text and len(text.strip()) > 0:
                            text_parts.append(text.strip())
            if text_parts:
                queries.append({
                    'timestamp': msg.get('timestamp', 0),
                    'text': '\n'.join(text_parts)
                })
    return queries


def extract_ai_title(messages):
    """Extract AI-generated title if available."""
    for msg in messages:
        if msg.get('type') == 'ai-title':
            return msg.get('aiTitle', '')
    return None


def extract_function_calls(messages):
    """Extract key function calls (tool usage summary)."""
    calls = []
    for msg in messages:
        if msg.get('type') == 'function_call':
            call_id = msg.get('callId', '')
            name = msg.get('name', '')
            args = msg.get('arguments', {})
            calls.append({
                'timestamp': msg.get('timestamp', 0),
                'callId': call_id,
                'name': name,
                'arguments': args
            })
    return calls


def extract_reasoning(messages):
    """Extract reasoning/thinking blocks."""
    reasoning_blocks = []
    for msg in messages:
        if msg.get('type') == 'reasoning':
            content = msg.get('content', [])
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'reasoning_text':
                    reasoning_blocks.append({
                        'timestamp': msg.get('timestamp', 0),
                        'text': part.get('text', '')
                    })
    return reasoning_blocks


def extract_key_decisions(reasoning_blocks, queries):
    """Heuristically extract key decisions from reasoning blocks."""
    decisions = []
    for block in reasoning_blocks:
        text = block['text']
        # Look for decision-like patterns
        decision_markers = [
            '决定', '选择', '方案', '采用', '使用', '确定',
            'decide', 'choose', 'select', 'adopt', 'use', 'determine',
            '最终', '结论', 'result', 'conclusion', 'final'
        ]
        for marker in decision_markers:
            if marker in text and len(text) > 20:
                # Truncate long reasoning to key sentences
                sentences = text.split('。')
                for sent in sentences[:3]:
                    if any(m in sent for m in decision_markers) and len(sent) > 10:
                        decisions.append(sent.strip())
                break
    # Deduplicate while preserving order
    seen = set()
    unique_decisions = []
    for d in decisions:
        if d not in seen:
            seen.add(d)
            unique_decisions.append(d)
    return unique_decisions[:10]  # Limit to top 10


def extract_todos(queries, reasoning_blocks):
    """Heuristically extract pending tasks/todos."""
    todos = []
    # Check last user query for explicit todos
    if queries:
        last_query = queries[-1]['text']
        todo_markers = ['待办', 'TODO', 'todo', '待完成', '还需要', '下一步',
                        'pending', 'next step', 'need to', 'should']
        for marker in todo_markers:
            if marker in last_query:
                # Extract sentences around the marker
                sentences = last_query.split('。')
                for sent in sentences:
                    if any(m in sent for m in todo_markers):
                        todos.append(sent.strip())
                break
    return todos[:5]


def extract_file_operations(function_calls):
    """Extract file read/write operations."""
    file_ops = []
    for call in function_calls:
        name = call['name']
        args = call['arguments']
        if not isinstance(args, dict):
            continue
        if name in ('Read', 'Write', 'Edit') and 'file_path' in args:
            file_ops.append({
                'operation': name,
                'file': args['file_path'],
                'timestamp': call['timestamp']
            })
    return file_ops


def format_timestamp(ts_ms):
    """Convert millisecond timestamp to readable string."""
    if not ts_ms:
        return 'N/A'
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000)
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError):
        return str(ts_ms)


def generate_context_md(session_id, work_dir, messages):
    """Generate SESSION_CONTEXT.md content."""
    queries = extract_user_queries(messages)
    ai_title = extract_ai_title(messages)
    function_calls = extract_function_calls(messages)
    reasoning_blocks = extract_reasoning(messages)
    decisions = extract_key_decisions(reasoning_blocks, queries)
    todos = extract_todos(queries, reasoning_blocks)
    file_ops = extract_file_operations(function_calls)

    # Determine title
    title = ai_title or (queries[0]['text'][:80] + '...' if queries else 'Untitled Session')

    # Determine latest timestamp
    latest_ts = max([m.get('timestamp', 0) for m in messages] or [0])

    lines = [
        '# SESSION_CONTEXT.md',
        '',
        f'> **Session ID**: `{session_id}`',
        f'> **Workspace**: `{work_dir}`',
        f'> **Last Updated**: {format_timestamp(latest_ts)}',
        f'> **Generated At**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        '',
        '## Session Title',
        '',
        title,
        '',
        '## Initial Goal',
        '',
    ]

    if queries:
        first_query = queries[0]['text']
        # Truncate if too long
        if len(first_query) > 500:
            first_query = first_query[:500] + '\n... (truncated)'
        lines.append(first_query)
    else:
        lines.append('(No user query found)')

    lines.extend(['', '## Conversation Summary', ''])
    lines.append(f'- Total messages: {len(messages)}')
    lines.append(f'- User queries: {len(queries)}')
    lines.append(f'- Tool calls: {len(function_calls)}')
    lines.append('')

    # Recent query history (last 5)
    lines.extend(['## Recent Queries', ''])
    for i, q in enumerate(queries[-5:], 1):
        text = q['text']
        if len(text) > 200:
            text = text[:200] + '...'
        lines.append(f'{i}. [{format_timestamp(q["timestamp"])}] {text}')
    lines.append('')

    # Key Decisions
    if decisions:
        lines.extend(['## Key Decisions', ''])
        for i, d in enumerate(decisions, 1):
            lines.append(f'{i}. {d}')
        lines.append('')

    # Pending Tasks
    if todos:
        lines.extend(['## Pending Tasks / Next Steps', ''])
        for i, t in enumerate(todos, 1):
            lines.append(f'{i}. {t}')
        lines.append('')

    # File Operations
    if file_ops:
        lines.extend(['## Files Touched', ''])
        seen_files = set()
        for op in file_ops:
            key = (op['operation'], op['file'])
            if key not in seen_files:
                seen_files.add(key)
                lines.append(f'- `{op["operation"]}`: `{op["file"]}`')
        lines.append('')

    # Tool usage summary
    tool_counts = {}
    for call in function_calls:
        name = call['name']
        tool_counts[name] = tool_counts.get(name, 0) + 1

    if tool_counts:
        lines.extend(['## Tools Used', ''])
        for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
            lines.append(f'- `{name}`: {count} call(s)')
        lines.append('')

    lines.extend([
        '---',
        '',
        '*This file was auto-generated by work-context-sync skill for cross-device session continuity.*',
    ])

    return '\n'.join(lines)


def main():
    if len(sys.argv) < 3:
        print("Usage: extract_context.py <jsonl_path> <output_md_path>")
        sys.exit(1)

    jsonl_path = sys.argv[1]
    output_path = sys.argv[2]

    if not os.path.exists(jsonl_path):
        print(f"Error: JSONL file not found: {jsonl_path}")
        sys.exit(1)

    # Extract session ID from filename
    session_id = Path(jsonl_path).stem

    # Determine workspace directory
    work_dir = Path(jsonl_path).parent.name
    # The parent directory name is like "c-Users-71026-WorkBuddy-2026-10-02-00-59-02"
    # Try to find the actual workspace path from sessions.json
    sessions_json = Path.home() / '.workbuddy' / 'app' / 'sessions.json'
    actual_work_dir = work_dir
    if sessions_json.exists():
        try:
            with open(sessions_json, 'r', encoding='utf-8') as f:
                sessions_data = json.load(f)
            for sess in sessions_data.get('sessions', []):
                if sess.get('conversationId') == session_id:
                    actual_work_dir = sess.get('workDir', work_dir)
                    break
        except Exception:
            pass

    messages = parse_jsonl(jsonl_path)
    if not messages:
        print("Error: No valid messages found in JSONL file")
        sys.exit(1)

    context_md = generate_context_md(session_id, actual_work_dir, messages)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(context_md)

    print(f"Generated SESSION_CONTEXT.md: {output_path}")
    print(f"Session ID: {session_id}")
    print(f"Messages: {len(messages)}")


if __name__ == '__main__':
    main()
