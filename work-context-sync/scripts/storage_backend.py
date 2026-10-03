#!/usr/bin/env python3
"""
Unified remote storage backend for work-context-sync.

Supports GitHub and Gitee over their REST APIs — NO git CLI, NO extra runtime.
Only the Python standard library is used.

Usage:
    from storage_backend import get_store
    store = get_store()                 # active backend from config
    store.ensure_repo()
    store.put_file('path/file.txt', b'...', 'commit message')
"""

import os
import sys
import json
import base64
import urllib.request
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wcs_config import resolve_credentials, BACKEND_HOSTS


class RemoteError(Exception):
    pass


class _BaseBackend:
    """Common HTTP helpers. Subclasses define auth + endpoint specifics."""

    def __init__(self, token, owner, repo, branch, api_base):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.api_base = api_base.rstrip('/')

    # ---- low-level HTTP -------------------------------------------------

    def _request(self, method, url, data=None, headers=None, retries=3):
        """HTTP request with retries on transient failures (network errors / 5xx)."""
        import time
        req_headers = {'User-Agent': 'work-context-sync/2.0'}
        req_headers.update(self._auth_headers())
        if headers:
            req_headers.update(headers)
        body = None
        if data is not None:
            body = json.dumps(data).encode('utf-8')
            req_headers['Content-Type'] = 'application/json'

        last = (-1, {'message': 'unknown'})
        for attempt in range(retries):
            req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read().decode('utf-8')
                    return resp.status, (json.loads(raw) if raw.strip() else {})
            except urllib.error.HTTPError as e:
                raw = e.read().decode('utf-8', errors='ignore')
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {'message': raw}
                # retry only on server-side transient errors
                if e.code >= 500 and attempt < retries - 1:
                    last = (e.code, payload)
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return e.code, payload
            except Exception as e:
                last = (-1, {'message': str(e)})
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return last
        return last

    def _contents_url(self, path):
        raise NotImplementedError

    def _auth_headers(self):
        raise NotImplementedError

    def _write_params(self, content_b64, message, sha):
        raise NotImplementedError

    # ---- generic operations --------------------------------------------

    def repo_exists(self):
        status, _ = self._request('GET', f'{self.api_base}/repos/{self.owner}/{self.repo}')
        return status == 200

    def ensure_repo(self, private=True):
        """Return True if repo exists (or was created)."""
        if self.repo_exists():
            return True
        status, data = self._create_repo(private)
        if status in (200, 201):
            return True
        raise RemoteError(
            f'仓库 {self.owner}/{self.repo} 不存在，且自动创建失败（{status}: {data.get("message")}）。'
            '请在网页端手动创建后重试。'
        )

    def list_dir(self, path=''):
        """Return list of entries: [{name, type, path, sha}]."""
        status, data = self._request('GET', self._contents_url(path))
        if status != 200:
            return []
        if isinstance(data, list):
            return [
                {'name': i.get('name'), 'type': i.get('type'),
                 'path': i.get('path'), 'sha': i.get('sha')}
                for i in data
            ]
        return []

    def _raw_download(self, url):
        """Fetch raw bytes from a direct download URL (best effort)."""
        req = urllib.request.Request(url, headers=self._auth_headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception:
            return None

    def _fetch_large_file(self, sha, data):
        """
        Fallback for files whose API response omits inline content.
        Subclasses may override with a more reliable mechanism.
        """
        dl = data.get('download_url')
        if dl:
            return self._raw_download(dl)
        return None

    def get_file(self, path):
        """Return (content_bytes, sha) or (None, None) if absent.

        Note: GitHub omits the base64 `content` field for files larger than 1 MB
        and only provides `download_url`. Falling back is required for conversation
        logs, which routinely exceed 1 MB.
        """
        status, data = self._request('GET', self._contents_url(path))
        if status != 200 or not isinstance(data, dict):
            return None, None

        sha = data.get('sha')
        content = data.get('content')

        if content:
            try:
                return base64.b64decode(content), sha
            except Exception:
                pass

        raw = self._fetch_large_file(sha, data)
        if raw is not None:
            return raw, sha
        return None, sha

    def get_text(self, path):
        raw, sha = self.get_file(path)
        if raw is None:
            return None, None
        return raw.decode('utf-8', errors='ignore'), sha

    def put_file(self, path, content, message, sha=None):
        """Create or update a file. content: str or bytes."""
        if isinstance(content, str):
            content = content.encode('utf-8')
        content_b64 = base64.b64encode(content).decode('ascii')
        payload = self._write_params(content_b64, message, sha)
        status, data = self._request('PUT', self._contents_url(path), payload)
        if status not in (200, 201):
            # Gitee needs POST to create when the file does not exist
            status2, data2 = self._request('POST', self._contents_url(path), payload)
            if status2 in (200, 201):
                return status2, data2
        return status, data

    def _delete_params(self, message, sha):
        raise NotImplementedError

    def delete_file(self, path, message):
        """Delete a file. Returns True on success."""
        _, sha = self.get_file(path)
        if not sha:
            return False
        payload = self._delete_params(message, sha)
        status, _ = self._request('DELETE', self._contents_url(path), payload)
        return status in (200, 204)

    def get_last_commit_date(self, path):
        """Best-effort last commit date (ISO string) for a path."""
        return ''

    # ---- session-level convenience --------------------------------------

    def list_sessions(self):
        """List top-level session folders."""
        return [e for e in self.list_dir('') if e.get('type') == 'dir']

    def download_session(self, folder):
        """Recursively download a folder -> {rel_path: bytes}."""
        files = {}

        def walk(prefix):
            for entry in self.list_dir(prefix):
                name = entry.get('name')
                full = f'{prefix}/{name}' if prefix else name
                if entry.get('type') == 'file':
                    raw, _ = self.get_file(full)
                    if raw is not None:
                        rel = full[len(folder) + 1:] if full.startswith(folder + '/') else full
                        files[rel] = raw
                elif entry.get('type') == 'dir':
                    walk(full)

        walk(folder)
        return files

    def upload_session(self, folder, files_dict, message):
        """Upload {rel_path: content} under folder. Returns list of results."""
        results = []
        for rel, content in files_dict.items():
            full = f'{folder}/{rel}'
            _, sha = self.get_file(full)
            status, _ = self.put_file(full, content, message, sha)
            results.append({'path': full, 'status': status, 'success': status in (200, 201)})
        return results


class GitHubBackend(_BaseBackend):
    def __init__(self, token, owner, repo, branch='main'):
        super().__init__(token, owner, repo, branch, BACKEND_HOSTS['github']['api_base'])

    def _auth_headers(self):
        return {
            'Authorization': f'token {self.token}',
            'Accept': 'application/vnd.github.v3+json',
        }

    def _contents_url(self, path):
        p = path.strip('/')
        base = f'{self.api_base}/repos/{self.owner}/{self.repo}/contents'
        if p:
            base += f'/{p}'
        return f'{base}?ref={self.branch}'

    def _write_params(self, content_b64, message, sha):
        payload = {'message': message, 'content': content_b64, 'branch': self.branch}
        if sha:
            payload['sha'] = sha
        return payload

    def _delete_params(self, message, sha):
        return {'message': message, 'sha': sha, 'branch': self.branch}

    def _create_repo(self, private):
        return self._request('POST', f'{self.api_base}/user/repos', {
            'name': self.repo, 'private': bool(private), 'auto_init': True,
        })

    def _fetch_large_file(self, sha, data):
        """
        For files >1MB GitHub omits inline content. The Git Blobs API returns
        base64 content up to 100MB and stays on api.github.com with auth, which
        is more reliable than raw.githubusercontent.com for private repos.
        """
        if sha:
            status, blob = self._request(
                'GET', f'{self.api_base}/repos/{self.owner}/{self.repo}/git/blobs/{sha}'
            )
            if status == 200 and isinstance(blob, dict) and blob.get('content'):
                try:
                    return base64.b64decode(blob['content'])
                except Exception:
                    pass
        return super()._fetch_large_file(sha, data)

    def get_last_commit_date(self, path):
        status, data = self._request(
            'GET',
            f'{self.api_base}/repos/{self.owner}/{self.repo}/commits'
            f'?path={urllib.parse.quote(path)}&per_page=1&sha={self.branch}'
        )
        if status == 200 and isinstance(data, list) and data:
            return data[0].get('commit', {}).get('committer', {}).get('date', '')
        return ''


class GiteeBackend(_BaseBackend):
    def __init__(self, token, owner, repo, branch='master'):
        super().__init__(token, owner, repo, branch, BACKEND_HOSTS['gitee']['api_base'])

    def _auth_headers(self):
        # Gitee authenticates via the access_token parameter, not a header
        return {'Accept': 'application/json'}

    def _contents_url(self, path):
        p = path.strip('/')
        base = f'{self.api_base}/repos/{self.owner}/{self.repo}/contents'
        if p:
            base += f'/{p}'
        params = {'access_token': self.token, 'ref': self.branch}
        return f'{base}?{urllib.parse.urlencode(params)}'

    def _write_params(self, content_b64, message, sha):
        payload = {
            'access_token': self.token,
            'content': content_b64,
            'message': message,
            'branch': self.branch,
        }
        if sha:
            payload['sha'] = sha
        return payload

    def _delete_params(self, message, sha):
        return {
            'access_token': self.token,
            'sha': sha,
            'message': message,
            'branch': self.branch,
        }

    def _create_repo(self, private):
        return self._request('POST', f'{self.api_base}/user/repos', {
            'access_token': self.token,
            'name': self.repo,
            'private': bool(private),
            'auto_init': True,
        })


def get_store(backend=None, credentials=None):
    """Factory: build the active backend from config (or explicit credentials)."""
    creds = credentials or resolve_credentials(backend)
    b = creds['backend']
    if b == 'github':
        return GitHubBackend(creds['token'], creds['owner'], creds['repo'], creds['branch'])
    if b == 'gitee':
        return GiteeBackend(creds['token'], creds['owner'], creds['repo'], creds['branch'])
    raise ValueError(f'不支持的平台: {b}')


def generate_session_readme(session_id, title, work_dir, summary, custom_id=None):
    """Build a session README.md for remote storage."""
    from datetime import datetime
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
    import argparse
    ap = argparse.ArgumentParser(description='Test remote storage connectivity')
    ap.add_argument('--backend', choices=('github', 'gitee'))
    ap.add_argument('--list', action='store_true', help='list session folders')
    args = ap.parse_args()

    creds = resolve_credentials(args.backend)
    print(f"平台: {creds['backend']}  仓库: {creds['owner']}/{creds['repo']}  分支: {creds['branch']}")
    print(f"凭据来源: {creds['source']}")

    store = get_store(credentials=creds)
    if not store.repo_exists():
        print('仓库不存在或不可访问。')
        sys.exit(1)
    print('仓库可访问 ✓')
    if args.list:
        sessions = store.list_sessions()
        print(f'会话文件夹: {len(sessions)} 个')
        for s in sessions:
            print(f"  - {s['name']}")


if __name__ == '__main__':
    main()
