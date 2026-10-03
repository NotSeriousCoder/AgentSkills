#!/usr/bin/env python3
"""
First-run bootstrap for work-context-sync.

Solves the #1 failure cause: credentials must be stored in a local config file,
because GUI-launched hosts (WorkBuddy, CodeArts) do NOT inherit shell environment
variables.

Commands:
    bootstrap.py check                     # is it configured?
    bootstrap.py guide                     # print step-by-step first-run guide
    bootstrap.py set --backend github --owner <u> --repo <r> --token <t> [--branch main]
    bootstrap.py verify [--backend github] [--create-repo]
"""

import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wcs_config import (
    CONFIG_PATH, load_config, save_config, set_backend,
    resolve_credentials, is_configured, mask, BACKEND_HOSTS, SUPPORTED_BACKENDS,
)
from storage_backend import get_store, RemoteError


def cmd_check():
    cfg = load_config()
    if not cfg:
        print('状态: 未配置')
        print('首次使用请运行: python scripts/bootstrap.py guide')
        return 1
    print(f'配置文件: {CONFIG_PATH}')
    print(f'当前平台: {cfg.get("backend")}')
    for b in SUPPORTED_BACKENDS:
        s = cfg.get(b) or {}
        if s:
            print(f'  [{b}] owner={s.get("owner")} repo={s.get("repo")} '
                  f'branch={s.get("branch")} token={mask(s.get("token"))}')
    print('状态: 已配置' if is_configured() else '状态: 配置不完整')
    return 0 if is_configured() else 1


def cmd_guide():
    print('=' * 64)
    print('work-context-sync 首次使用引导')
    print('=' * 64)
    print("""
本技能使用 HTTP API 访问代码托管平台，无需安装 git。

步骤 1 — 选择平台
    github （默认）或 gitee

步骤 2 — 获取访问令牌
""")
    for b in SUPPORTED_BACKENDS:
        h = BACKEND_HOSTS[b]
        print(f'    [{b}] {h["token_url"]}')
        print(f'          {h["token_hint"]}')
    print("""
步骤 3 — 准备仓库
    在平台上新建一个（可私有的）仓库用于存放会话，记下 owner 和 repo 名。
    例如 owner=NotSeriousCoder repo=AgentSession
    （本工具也能自动创建仓库，见 verify --create-repo）

步骤 4 — 写入配置（一次性）
    python scripts/bootstrap.py set \\
        --backend github \\
        --owner <你的用户名> \\
        --repo <仓库名> \\
        --token <你的令牌>

步骤 5 — 校验
    python scripts/bootstrap.py verify --create-repo

完成后即可正常使用导出/导入功能。配置保存在本地文件，重启后依然有效。
""")
    return 0


def cmd_set(args):
    if not args.token or not args.owner or not args.repo:
        print('错误: 必须同时提供 --token / --owner / --repo')
        return 2
    cfg = set_backend(args.backend, token=args.token, owner=args.owner,
                      repo=args.repo, branch=args.branch)
    print(f'已写入配置: {CONFIG_PATH}')
    s = cfg[args.backend]
    print(f'  [{args.backend}] owner={s["owner"]} repo={s["repo"]} '
          f'branch={s["branch"]} token={mask(s["token"])}')
    print('下一步: python scripts/bootstrap.py verify')
    return 0


def cmd_verify(args):
    try:
        creds = resolve_credentials(args.backend)
    except SystemExit as e:
        print(str(e))
        return 2

    print(f"平台: {creds['backend']}  仓库: {creds['owner']}/{creds['repo']} "
          f"分支: {creds['branch']}  凭据来源: {creds['source']}")

    store = get_store(credentials=creds)

    if store.repo_exists():
        print('仓库可访问 ✓')
    else:
        print('仓库不存在或不可访问。')
        if args.create_repo:
            try:
                store.ensure_repo(private=True)
                print('已自动创建仓库 ✓')
            except RemoteError as e:
                print(str(e))
                return 1
        else:
            print('若需自动创建，请加 --create-repo；或先在网页端手动创建。')
            return 1

    # connectivity round-trip: read root listing
    try:
        entries = store.list_dir('')
        print(f'连通性正常 ✓（根目录 {len(entries)} 个条目）')
    except Exception as e:
        print(f'连通性测试失败: {e}')
        return 1

    print('校验通过。可以开始使用导出/导入功能。')
    return 0


def main():
    ap = argparse.ArgumentParser(description='work-context-sync first-run bootstrap')
    sub = ap.add_subparsers(dest='command')

    sub.add_parser('check', help='检查配置状态')
    sub.add_parser('guide', help='打印首次使用引导')

    p_set = sub.add_parser('set', help='写入配置')
    p_set.add_argument('--backend', choices=SUPPORTED_BACKENDS, default='github')
    p_set.add_argument('--owner', required=True)
    p_set.add_argument('--repo', required=True)
    p_set.add_argument('--token', required=True)
    p_set.add_argument('--branch')

    p_ver = sub.add_parser('verify', help='校验令牌与仓库')
    p_ver.add_argument('--backend', choices=SUPPORTED_BACKENDS)
    p_ver.add_argument('--create-repo', action='store_true')

    args = ap.parse_args()

    if args.command == 'check':
        return cmd_check()
    if args.command == 'guide':
        return cmd_guide()
    if args.command == 'set':
        return cmd_set(args)
    if args.command == 'verify':
        return cmd_verify(args)

    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
