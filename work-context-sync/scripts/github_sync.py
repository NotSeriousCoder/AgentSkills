#!/usr/bin/env python3
"""
GitHub sync module for work-context-sync skill.
Handles upload/download of session contexts to/from GitHub repository.
"""

import json
import os
import sys
import base64
import hashlib
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path


class GitHubSync:
    """GitHub repository sync client."""

    def __init__(self, token, repo_owner, repo_name, branch='main'):
        self.token = token
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.branch = branch
        self.base_url = f'https://api.github.com/repos/{repo_owner}/{repo_name}'
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'work-context-sync/1.0'
        }

    def _api_request(self, method, endpoint, data=None, headers_override=None):
        """Make a GitHub API request."""
        url = f'{self.base_url}/{endpoint}'
        req_headers = dict(self.headers)
        if headers_override:
            req_headers.update(headers_override)

        if data is not None and isinstance(data, dict):
            data = json.dumps(data).encode('utf-8')
            req_headers['Content-Type'] = 'application/json'

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8')
            try:
                err_data = json.loads(body)
            except json.JSONDecodeError:
                err_data = {'message': body}
            return e.code, err_data
        except Exception as e:
            return -1, {'message': str(e)}

    def _get_content(self, path):
        """Get file/directory content from repo. Returns (status, data)."""
        return self._api_request('GET', f'contents/{path}?ref={self.branch}')

    def _create_or_update_file(self, path, content, message, sha=None):
        """Create or update a file in the repo."""
        # Content must be base64 encoded
        if isinstance(content, str):
            content_bytes = content.encode('utf-8')
        else:
            content_bytes = content
        encoded = base64.b64encode(content_bytes).decode('utf-8')

        data = {
            'message': message,
            'content': encoded,
            'branch': self.branch
        }
        if sha:
            data['sha'] = sha

        return self._api_request('PUT', f'contents/{path}', data)

    def repo_exists(self):
        """Check if the repository exists and is accessible."""
        status, data = self._api_request('GET', '')
        return status == 200

    def list_sessions(self):
        """List all session directories in the repo."""
        status, data = self._get_content('')
        if status != 200:
            return []

        sessions = []
        for item in data:
            if item.get('type') == 'dir':
                session_name = item.get('name')
                # Skip common non-session directories
                if session_name in ('.git', '.github', 'docs'):
                    continue
                sessions.append({
                    'name': session_name,
                    'path': item.get('path'),
                    'sha': item.get('sha')
                })
        return sessions

    def get_session_readme(self, session_name):
        """Get session README content if it exists."""
        readme_path = f'{session_name}/README.md'
        status, data = self._get_content(readme_path)
        if status == 200 and data.get('type') == 'file':
            content = base64.b64decode(data.get('content', '')).decode('utf-8')
            return {
                'content': content,
                'sha': data.get('sha'),
                'last_modified': data.get('commit', {}).get('committer', {}).get('date', '')
            }
        return None

    def parse_session_id_from_readme(self, readme_content):
        """Extract custom session ID from README content."""
        if not readme_content:
            return None
        for line in readme_content.split('\n'):
            if line.strip().startswith('**Session ID**:'):
                parts = line.split(':', 1)
                if len(parts) == 2:
                    return parts[1].strip().strip('`').strip()
            if line.strip().startswith('- **Session ID**:'):
                parts = line.split(':', 1)
                if len(parts) == 2:
                    return parts[1].strip().strip('`').strip()
        return None

    def parse_session_title_from_readme(self, readme_content):
        """Extract session title from README content."""
        if not readme_content:
            return None
        lines = readme_content.split('\n')
        for i, line in enumerate(lines):
            if line.strip() == '## Session Title':
                if i + 1 < len(lines):
                    return lines[i + 1].strip()
        return None

    def find_similar_sessions(self, title, threshold=0.6):
        """Find sessions with similar titles."""
        sessions = self.list_sessions()
        similar = []

        title_lower = title.lower()
        title_words = set(title_lower.split())

        for session in sessions:
            readme = self.get_session_readme(session['name'])
            if not readme:
                continue

            session_title = self.parse_session_title_from_readme(readme['content']) or session['name']
            session_id = self.parse_session_id_from_readme(readme['content'])

            # Simple similarity: Jaccard index on words
            session_words = set(session_title.lower().split())
            if not session_words:
                continue

            intersection = title_words & session_words
            union = title_words | session_words
            similarity = len(intersection) / len(union) if union else 0

            if similarity >= threshold or title_lower in session_title.lower() or session_title.lower() in title_lower:
                similar.append({
                    'name': session['name'],
                    'title': session_title,
                    'session_id': session_id,
                    'similarity': similarity,
                    'last_modified': readme.get('last_modified', ''),
                    'readme_sha': readme['sha']
                })

        # Sort by similarity descending
        similar.sort(key=lambda x: x['similarity'], reverse=True)
        return similar

    def upload_session(self, session_name, files_dict, commit_message):
        """
        Upload session files to GitHub.
        files_dict: {path_relative_to_session: content_bytes_or_str}
        """
        results = []
        for rel_path, content in files_dict.items():
            full_path = f'{session_name}/{rel_path}'

            # Check if file already exists
            status, data = self._get_content(full_path)
            existing_sha = data.get('sha') if status == 200 else None

            status, resp = self._create_or_update_file(
                full_path, content, commit_message, existing_sha
            )
            results.append({
                'path': full_path,
                'status': status,
                'success': status in (200, 201)
            })

        return results

    def download_session(self, session_name):
        """Download all files from a session directory."""
        status, data = self._get_content(session_name)
        if status != 200:
            return None

        if data.get('type') == 'file':
            # Single file
            content = base64.b64decode(data.get('content', ''))
            return {session_name: content}

        # Directory
        files = {}
        for item in data:
            if item.get('type') == 'file':
                file_status, file_data = self._get_content(item.get('path'))
                if file_status == 200:
                    content = base64.b64decode(file_data.get('content', ''))
                    files[item.get('name')] = content
            elif item.get('type') == 'dir':
                # Recursively get subdirectory contents
                sub_files = self._download_directory(item.get('path'))
                files.update(sub_files)

        return files

    def _download_directory(self, dir_path):
        """Recursively download directory contents."""
        status, data = self._get_content(dir_path)
        if status != 200:
            return {}

        files = {}
        for item in data:
            if item.get('type') == 'file':
                file_status, file_data = self._get_content(item.get('path'))
                if file_status == 200:
                    content = base64.b64decode(file_data.get('content', ''))
                    rel_path = item.get('path').replace(dir_path + '/', '', 1)
                    files[rel_path] = content
            elif item.get('type') == 'dir':
                sub_files = self._download_directory(item.get('path'))
                files.update(sub_files)

        return files

    def get_file_last_modified(self, path):
        """Get the last modified date of a file."""
        status, data = self._get_content(path)
        if status == 200:
            commits_url = data.get('commits_url', '').replace('{/sha}', '')
            if commits_url:
                req = urllib.request.Request(
                    f'{commits_url}?path={path}&per_page=1',
                    headers=self.headers
                )
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        commits = json.loads(resp.read().decode('utf-8'))
                        if commits:
                            return commits[0].get('commit', {}).get('committer', {}).get('date', '')
                except Exception:
                    pass
            return data.get('commit', {}).get('committer', {}).get('date', '')
        return None


def generate_session_readme(session_id, title, work_dir, summary, custom_id=None):
    """Generate a session README.md for GitHub storage."""
    actual_id = custom_id or session_id
    lines = [
        '# Session README',
        '',
        f'- **Session ID**: `{actual_id}`',
        f'- **Original Conversation ID**: `{session_id}`',
        f'- **Title**: {title}',
        f'- **Workspace**: `{work_dir}`',
        f'- **Last Synced**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        '',
        '## Summary',
        '',
        summary or '(No summary provided)',
        '',
        '---',
        '',
        '*This README is auto-generated by work-context-sync skill.*',
    ]
    return '\n'.join(lines)


def main():
    """CLI entry point for testing."""
    if len(sys.argv) < 2:
        print("Usage: github_sync.py <command> [args...]")
        print("Commands: list, upload, download, find")
        sys.exit(1)

    command = sys.argv[1]

    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        print("Error: GITHUB_TOKEN environment variable not set")
        sys.exit(1)

    repo = os.environ.get('GITHUB_REPO', 'workbuddy-session-sync')
    owner = os.environ.get('GITHUB_OWNER')
    if not owner:
        print("Error: GITHUB_OWNER environment variable not set")
        sys.exit(1)

    sync = GitHubSync(token, owner, repo)

    if not sync.repo_exists():
        print(f"Error: Repository {owner}/{repo} not found or not accessible")
        sys.exit(1)

    if command == 'list':
        sessions = sync.list_sessions()
        print(f"Found {len(sessions)} session(s):")
        for s in sessions:
            readme = sync.get_session_readme(s['name'])
            title = sync.parse_session_title_from_readme(readme['content']) if readme else s['name']
            sid = sync.parse_session_id_from_readme(readme['content']) if readme else None
            print(f"  - {s['name']}: {title} (ID: {sid or 'N/A'})")

    elif command == 'find':
        if len(sys.argv) < 3:
            print("Usage: github_sync.py find <title>")
            sys.exit(1)
        title = sys.argv[2]
        similar = sync.find_similar_sessions(title)
        print(f"Found {len(similar)} similar session(s):")
        for s in similar:
            print(f"  - {s['name']}: {s['title']} (similarity: {s['similarity']:.2f})")

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == '__main__':
    main()
