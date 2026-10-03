#!/usr/bin/env python3
"""
Configuration for work-context-sync.

Uses a local config file so credentials survive GUI-launched hosts (which do not
inherit shell environment variables). Environment variables are kept only as a
fallback for backwards compatibility.

Config file location: ~/.work-context-sync/config.json
(outside the skill directory, so it is never bundled into the distributable zip
and survives reinstalling/upgrading the skill)
"""

import os
import json

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(os.path.expanduser('~'), '.work-context-sync')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.json')

DEFAULT_BRANCH = {'github': 'main', 'gitee': 'master'}
SUPPORTED_BACKENDS = ('github', 'gitee')

BACKEND_HOSTS = {
    'github': {
        'api_base': 'https://api.github.com',
        'web': 'https://github.com',
        'token_url': 'https://github.com/settings/tokens',
        'token_hint': '需要 repo 权限（私有仓库）',
    },
    'gitee': {
        'api_base': 'https://gitee.com/api/v5',
        'web': 'https://gitee.com',
        'token_url': 'https://gitee.com/profile/personal_access_tokens',
        'token_hint': '需要 projects 权限',
    },
}


def load_config():
    """Load config from file; returns {} if missing or unreadable."""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_config(cfg):
    """Persist config to file (chmod-restricted where supported)."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return CONFIG_PATH


def set_backend(backend, token=None, owner=None, repo=None, branch=None):
    """Create/update credentials for a backend; also mark it active."""
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(f'不支持的平台: {backend}（可选: {", ".join(SUPPORTED_BACKENDS)}）')
    cfg = load_config()
    section = cfg.get(backend, {}) or {}
    if token:
        section['token'] = token
    if owner:
        section['owner'] = owner
    if repo:
        section['repo'] = repo
    if branch:
        section['branch'] = branch
    section.setdefault('branch', DEFAULT_BRANCH[backend])
    cfg[backend] = section
    cfg['backend'] = backend
    save_config(cfg)
    return cfg


def resolve_credentials(backend=None):
    """
    Resolve active credentials. Priority:
      1. config file (chosen backend)
      2. environment variables (legacy fallback, GitHub only)
    Returns a dict: {backend, token, owner, repo, branch, source}
    Raises SystemExit with actionable guidance if nothing is configured.
    """
    cfg = load_config()
    backend = backend or cfg.get('backend') or 'github'

    section = cfg.get(backend, {}) or {}
    token = section.get('token')
    owner = section.get('owner')
    repo = section.get('repo')
    branch = section.get('branch') or DEFAULT_BRANCH.get(backend, 'main')
    source = 'config'

    if not (token and owner and repo) and backend == 'github':
        # legacy env fallback
        env_token = os.environ.get('GITHUB_TOKEN')
        env_owner = os.environ.get('GITHUB_OWNER')
        env_repo = os.environ.get('GITHUB_REPO')
        if env_token and env_owner and env_repo:
            token, owner, repo = env_token, env_owner, env_repo
            source = 'env'

    missing = [k for k, v in (('token', token), ('owner', owner), ('repo', repo)) if not v]
    if missing:
        hosts = BACKEND_HOSTS.get(backend, {})
        raise SystemExit(
            '尚未完成首次配置（缺少: ' + ', '.join(missing) + '）。\n'
            f'请先运行: python scripts/bootstrap.py set --backend {backend} '
            '--owner <用户名> --repo <仓库名> --token <访问令牌>\n'
            f'令牌获取: {hosts.get("token_url", "")}（{hosts.get("token_hint", "")}）'
        )

    return {
        'backend': backend,
        'token': token,
        'owner': owner,
        'repo': repo,
        'branch': branch,
        'source': source,
    }


def is_configured(backend=None):
    """Return True if credentials are resolvable without raising."""
    try:
        resolve_credentials(backend)
        return True
    except SystemExit:
        return False


def mask(token):
    """Mask a token for display."""
    if not token:
        return '(未设置)'
    if len(token) <= 8:
        return '****'
    return token[:4] + '...' + token[-4:]


def main():
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'show'
    cfg = load_config()

    if cmd == 'show':
        if not cfg:
            print('尚未配置。请运行 bootstrap.py set ...')
            return
        print(f'配置文件: {CONFIG_PATH}')
        print(f'当前平台: {cfg.get("backend")}')
        for b in SUPPORTED_BACKENDS:
            s = cfg.get(b) or {}
            if s:
                print(f'  [{b}] owner={s.get("owner")} repo={s.get("repo")} '
                      f'branch={s.get("branch")} token={mask(s.get("token"))}')
    elif cmd == 'path':
        print(CONFIG_PATH)
    elif cmd == 'check':
        print('已配置' if is_configured() else '未配置')
    else:
        print(f'未知命令: {cmd}')
        sys.exit(1)


if __name__ == '__main__':
    main()
