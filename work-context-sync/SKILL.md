---
name: work-context-sync
description: This skill should be used when the user wants to sync AI assistant session context across devices via GitHub or Gitee. It uses platform HTTP APIs only (no git, no Docker), and stores credentials in a local config file written by a first-run bootstrap step. It supports two modes — "context bridge" (extract a readable SESSION_CONTEXT.md summary) and "hardcore sync" (migrate the full conversation history + sessions.json entry so the session truly appears on another device, with cross-device path remapping). Supports multiple AI products (WorkBuddy/腾讯, 通义灵码/阿里, 文心快码 Comate/百度, CodeArts Agent/华为). Trigger phrases include "sync session", "upload session to GitHub", "download session from GitHub", "continue session on another computer", "backup conversation context", "restore workspace context", "跨设备同步会话", "硬核同步", "迁移会话".
agent_created: true
---

# Work Context Sync

## Overview

Sync AI assistant session context across devices using GitHub as the storage backend. The skill detects which AI product the user is using (WorkBuddy, 通义灵码, 文心快码 Comate, or CodeArts Agent), locates the correct conversation storage location, packages the data, uploads to a GitHub repository, and restores it on another device.

**Key capabilities:**
- Multi-platform support: auto-detect which AI product is installed
- **Two sync modes** (see below): context bridge (summary) and hardcore sync (full session migration)
- Extract structured context from local conversation logs (platform-specific parsers)
- Migrate full conversation history with cross-device path remapping
- Upload to GitHub with duplicate detection and session ID management
- Compare local vs. remote timestamps to avoid overwriting newer data

## When to Use

Use this skill when:
- The user wants to continue an AI assistant session on a different computer
- The user wants to backup or archive a session's context to GitHub or Gitee
- The user wants to restore a previously synced session from GitHub or Gitee
- The user mentions switching devices, cross-device work, session portability

## Execution Contract (MANDATORY)

Follow these rules exactly. They exist because a previous run failed: the agent
bypassed the scripts, ran `git` commands, hit an auth 404, and stalled for 9+ minutes.

1. **NEVER run `git` commands** (`git clone/pull/push/remote/config`). This skill uses
   the platform REST APIs through `scripts/storage_backend.py`. No git binary is needed.
2. **ALWAYS call the scripts in `scripts/`** for remote operations. Do not hand-roll
   `curl`/web requests or clone the repository.
3. **Run `bootstrap.py` first when credentials are missing.** If any script exits with
   "尚未完成首次配置", do NOT improvise — follow the printed instruction, or run
   `python scripts/bootstrap.py guide` and relay it to the user.
4. **Credentials live in `config.json`, not environment variables.** GUI-launched hosts
   (WorkBuddy, CodeArts) do not inherit shell env vars; do not rely on `GITHUB_TOKEN`.
5. **Prefer writing script output to a file when stdout is unreliable** (Windows hosts
   sometimes return empty stdout). Do not loop on retries — read the file instead.

## First-Run Bootstrap

Do this once per device, before any upload/download:

```bash
# 1. check whether it is configured
python scripts/bootstrap.py check

# 2. if not configured, show the guide (or set directly with the user's values)
python scripts/bootstrap.py guide
python scripts/bootstrap.py set --backend github \
    --owner <用户名> --repo <仓库名> --token <访问令牌>

# 3. verify token + repository (optionally auto-create the repo)
python scripts/bootstrap.py verify --create-repo
```

Ask the user for the token/owner/repo values, run `set`, then `verify`. The config is
persisted to `config.json` in the skill directory, so later runs skip this entirely.

**Token sources:** GitHub `https://github.com/settings/tokens` (repo scope);
Gitee `https://gitee.com/profile/personal_access_tokens` (projects scope).

## One-Shot Commands (preferred)

Prefer these over chaining the lower-level scripts. They call the correct scripts
internally, so there is no room for improvisation.

```bash
# upload the current session (export + upload in one step)
python scripts/push_session.py
python scripts/push_session.py --dry-run          # export only, nothing uploaded
python scripts/push_session.py --session-name my-session

# list / download / restore a remote session
python scripts/pull_session.py --list
python scripts/pull_session.py <session-folder>                     # dry-run
python scripts/pull_session.py <session-folder> --apply
python scripts/pull_session.py <session-folder> --target-workdir "D:\Work\WB\x" --apply
```

Both accept `--backend github|gitee` to override the active platform.

**What `push_session.py` does:** resolve credentials → detect the local product →
locate the conversation JSONL → export a staging folder → upload it → record the
custom ID locally. **What `pull_session.py` does:** download the session folder →
run the hardcore import (path remapping, session-index merge, backup).

## Two Sync Modes

| | **Context bridge 模式** | **Hardcore sync 模式** |
|---|---|---|
| 产出 | `SESSION_CONTEXT.md` 摘要 | 完整对话历史 + `sessions.json` 条目 |
| 另一台电脑效果 | 只恢复"上下文摘要"，看不到原对话 | **原对话真实出现在会话列表里** |
| 依赖 | 无 | 需要路径重映射 + 关闭/重启 WorkBuddy |
| 风险 | 极低 | 中（改 WorkBuddy 内部数据，有备份） |
| 适用 | 快速续接思路 | 完整迁移会话 |

Ask the user which mode they want if it is ambiguous. Default to **context bridge** (safe).

## Hardcore Sync Mode

Full session migration. Copies the conversation history and registers it in the target device's session index so the session truly appears in the WorkBuddy UI.

### Data layout (why two locations matter)

| 数据 | 路径 | 类别 |
|------|------|------|
| 对话历史 | `~/.workbuddy/projects/<编码>/<conversationId>.jsonl` | 全局仓库 |
| 会话索引 | `~/.workbuddy/app/sessions.json` | 全局仓库 |
| 产物 | `<工作空间>/outputs/` | 工作空间 |
| 记忆 | `<工作空间>/.workbuddy/` | 工作空间 |

The conversation history is **not** inside the workspace folder — it lives under the global `~/.workbuddy/projects/` directory. This is why copying only the workspace `.workbuddy` + `outputs` is insufficient.

### workDir encoding (verified against real sessions)

```
C:\Users\71026\WorkBuddy\2026-10-02-00-59-02
  -> c-Users-71026-WorkBuddy-2026-10-02-00-59-02
```

Rule: lowercase the drive letter, drop the colon, replace `\` with `-`. Implemented in `scripts/path_map.py::encode_workdir`.

### Export (on the source device)

```bash
python scripts/sync_export.py \
  --conversation-id <convId> \
  --out <staging_dir> \
  [--no-outputs] [--no-memory]
```

Produces a staging folder:
```
<staging>/
  sync-manifest.json                          # source info + file list
  projects/<encoded>/<convId>.jsonl           # conversation history
  workspace/outputs/...                       # optional
  workspace/.workbuddy/...                    # optional
```

Then upload the staging folder to the configured platform with `storage_backend.py`
(no git): create a session folder and `put_file` each staged file, e.g.

```python
import sys; sys.path.insert(0, 'scripts')
from storage_backend import get_store
store = get_store()                      # active backend from config.json
store.ensure_repo()
store.upload_session('<session-folder>', {
    'sync-manifest.json': open('<staging>/sync-manifest.json','rb').read(),
    'projects/<encoded>/<convId>.jsonl': open('...','rb').read(),
}, 'Sync session')
```

### Import (on the target device)

```bash
# 1. dry-run first (default) — shows the plan, writes nothing
python scripts/sync_import.py <staging_dir> [--target-workdir <path>]

# 2. apply
python scripts/sync_import.py <staging_dir> [--target-workdir <path>] --apply
```

Import steps:
1. Safety check (warns if WorkBuddy is running)
2. **Path remapping**: find the local target workspace whose leaf folder name matches the
   source (`path_map.find_matching_workspace`); if 0 or >1 match, require `--target-workdir`
3. Place JSONL at `~/.workbuddy/projects/<本地编码>/<convId>.jsonl` (conversationId preserved)
4. **Merge** (not overwrite) the session entry into local `sessions.json`, using the local
   device's `userId`; backs up `sessions.json` first
5. Restore `outputs/` and `.workbuddy/` into the target workspace
6. Instruct the user to fully quit and restart WorkBuddy

### Hardcore sync safety rules (mandatory)

- **Never overwrite `sessions.json`** — always merge; always back up first.
- **Never touch `IDENTITY.md` / `USER.md` / `SOUL.md`** — identity files must not cross devices.
- **Warn about a running WorkBuddy** — it may overwrite the index on exit; if the session
  does not appear after restart, re-run `--apply`.
- **Dry-run by default** — only write when `--apply` is passed.

## Supported Platforms

| 平台 | 厂商 | 会话存储位置 | 支持状态 |
|------|------|-------------|---------|
| WorkBuddy | 腾讯 | `~/.workbuddy/projects/*/*.jsonl` + `app/sessions.json` | ✅ 完整支持 |
| 通义灵码 Tongyi Lingma | 阿里云 | `~/.tongyi/lingma/chat-history/`（加密）或 `~/.lingma/logs/*.jsonl` | ⚠️ 部分支持 |
| 文心快码 Comate | 百度 | `.comate/`（Memory），会话主要云端 | ⚠️ 部分支持 |
| CodeArts Agent | 华为云 | `%APPDATA%/codearts-agent/User/chat_sessions/` 或 `~/.codeartsdoer/codearts-data/opencode.db` | ⚠️ 部分支持 |

**Support levels:**
- ✅ **完整支持**：能自动定位、解析会话记录、提取上下文
- ⚠️ **部分支持**：能定位会话存储位置，但解析器尚未完善，需手动指定文件或依赖后续迭代

**扩展新平台**：编辑 `scripts/platforms.json`，新增一条平台记录（含存储路径 patterns 和 parser 标识），即可接入新平台，无需修改核心流程。

## Prerequisites

1. **Python 3** — the only runtime dependency (no git, no Docker, no Node).
2. **A remote repository** on GitHub or Gitee (can be private) for storing sessions.
3. **A personal access token** for that platform:
   - GitHub: https://github.com/settings/tokens (`repo` scope)
   - Gitee: https://gitee.com/profile/personal_access_tokens (`projects` scope)

Then run **First-Run Bootstrap** (above) once per device. Credentials are stored in
`config.json` inside the skill directory, **not** in environment variables.

## Workflow Decision Tree

```
User requests session sync
|
+-- Step 0: CONFIRM PLATFORM (mandatory first step)
|   |
|   +-- Run detect_platform.py to scan installed products
|   +-- If exactly one platform detected: use it (still confirm with user if ambiguous)
|   +-- If multiple platforms detected: ask user "你在使用什么产品？" with options
|   +-- If zero detected: list all registered platforms, ask user to choose
|   +-- Record confirmed platform ID
|
+-- Upload current session to GitHub?
|   |
|   +-- Locate conversation logs for confirmed platform
|   +-- Extract context (platform-specific parser)
|   +-- Check GitHub for similar sessions
|   +-- If similar found: ask user (refresh existing vs. create new)
|   +-- Generate session README with unique ID
|   +-- Package SESSION_CONTEXT.md + artifacts + memory
|   +-- Upload to GitHub
|   +-- Record custom ID locally
|
+-- Download session from GitHub?
    |
    +-- Check if local session has custom ID
    +-- If has ID: match on GitHub, compare timestamps
    +-- If no ID: list available sessions, ask user to choose
    +-- Download session package
    +-- Extract to workspace
    +-- Inject context into current conversation
```

## Step 0: Confirm Platform (MANDATORY)

Before any upload or download, determine which AI product the user is using. This is critical because each product stores conversation data in different locations.

### 0.1 Auto-detect

Run the detection script:

```bash
python scripts/detect_platform.py detect --json
```

Returns a clean JSON array of detected platforms, e.g.:
```json
[
  {"id": "workbuddy", "name": "WorkBuddy", "vendor": "腾讯", "supported": true, "note": ""},
  {"id": "huawei-codearts", "name": "CodeArts Agent", "vendor": "华为云", "supported": false, "note": "..."}
]
```

### 0.2 Handle detection results

**Exactly one platform detected:**
- Use it directly. If `supported` is false, inform the user that this platform has limited support and may require manual file specification.

**Multiple platforms detected:**
- Ask the user which product they are using. Use `AskUserQuestion` with the detected platforms as options. Example question: "你在使用什么产品？"

**Zero platforms detected:**
- List all registered platforms (`detect_platform.py list`) and ask the user to choose.
- If the user's product is not listed, ask them to provide the conversation storage path manually.

### 0.3 Get platform details

```bash
python scripts/detect_platform.py get <platform_id>
```

Returns the platform's full config, including `conversation_logs_patterns` for locating session data.

## Step 1: Extract Session Context

**WorkBuddy** (full support) — run the context extraction script:

```bash
python scripts/extract_context.py <jsonl_path> <output_md_path>
```

**Parameters:**
- `jsonl_path`: Path to conversation JSONL (e.g., `~/.workbuddy/projects/<workspace>/<conversationId>.jsonl`)
- `output_md_path`: Where to write `SESSION_CONTEXT.md`

**Other platforms** (partial support) — the parser may not be implemented yet. For these:
- Locate the conversation files using the platform's `conversation_logs_patterns`
- If the files are readable (e.g., `.lingma/logs/*.jsonl`), attempt extraction with a generic approach
- If encrypted or in SQLite (e.g., Huawei `opencode.db`, 通义灵码 `chat-history/`), ask the user to manually specify which files to package

**What it extracts:**
- Session title (from AI-generated title or first user query)
- Initial goal / first user query
- Conversation summary (message counts, tool calls)
- Recent query history (last 5)
- Key decisions (heuristically extracted from reasoning blocks)
- Pending tasks / next steps
- Files touched (Read/Write/Edit operations)
- Tools used summary

## Step 2: Check the Platform for Similar Sessions

Before uploading, list existing session folders on the configured platform:

```python
import sys; sys.path.insert(0, 'scripts')
from storage_backend import get_store
store = get_store()                      # uses config.json (no env vars, no git)
for s in store.list_sessions():
    title, _ = store.get_text(f"{s['name']}/README.md")
    print(s['name'], title or '')
```

**Similarity matching:**
- Uses Jaccard index on title words
- Threshold: 0.6 (configurable)
- Returns matching sessions with their custom IDs and last modified dates

**If similar sessions found:**
- Present the list to the user
- Ask: "Refresh existing session 'X' or create a new folder?"
- If refresh: reuse the existing session's custom ID
- If new: generate a new custom ID

## Step 3: Generate Session README

Create a `README.md` for the session folder on the remote platform:

```python
import sys; sys.path.insert(0, 'scripts')
from storage_backend import generate_session_readme

readme = generate_session_readme(
    session_id="<conversationId>",
    title="<session_title>",
    work_dir="<workspace_path>",
    summary="<brief_summary>",
    custom_id="<custom_id>"  # optional
)
```

**README contents:**
- Custom Session ID (unique, reusable across devices)
- Original Conversation ID (WorkBuddy internal)
- Session title
- Workspace path
- Last synced timestamp
- Brief summary

## Step 4: Package and Upload

**Package contents:**
1. `SESSION_CONTEXT.md` - structured context summary
2. `README.md` - session metadata with custom ID
3. Workspace `outputs/` directory (if exists)
4. Workspace `.workbuddy/memory/` directory (if exists)
5. Any other user-specified files

**Upload command:**
```python
import sys; sys.path.insert(0, 'scripts')
from storage_backend import get_store

store = get_store()                      # active backend from config.json
store.ensure_repo()
files = {
    'SESSION_CONTEXT.md': context_md_content,
    'README.md': readme_content,
    'outputs/report.xlsx': report_bytes,
    # ... other files
}
results = store.upload_session('session-folder-name', files, 'Sync session context')
```

**After upload:**
- Record the custom ID locally using `manage_session_id.py`
- Store in `<workspace>/.workbuddy/session-sync.json`

## Step 5: Download and Restore

### Case A: Local session has custom ID

1. Read custom ID from local `session-sync.json`
2. Locate the matching session folder on the remote platform
3. Compare timestamps:
   - If the remote version is newer: download and restore
   - If local is newer: warn user, ask for confirmation
4. Download session package
5. Extract files to workspace
6. Read `SESSION_CONTEXT.md` and inject into conversation context

### Case B: Local session has no custom ID

1. List all available sessions from the remote platform
2. Present to user with titles and last modified dates
3. User selects session to download
4. Download and extract
5. Record new custom ID locally

**Download command:**
```python
import sys; sys.path.insert(0, 'scripts')
from storage_backend import get_store

store = get_store()
files = store.download_session('session-folder-name')
# files is a dict: {relative_path: content_bytes}
```

## Step 6: Inject Context into Conversation

After downloading `SESSION_CONTEXT.md`, inject its contents into the current conversation by reading the file and including it in the reasoning or as a system message reference.

**Key sections to inject:**
- Session Title
- Initial Goal
- Key Decisions
- Pending Tasks / Next Steps
- Files Touched

## Local Session ID Management

The skill stores session metadata locally at `<workspace>/.workbuddy/session-sync.json`:

```json
{
  "platform": "workbuddy",
  "sessions": {
    "<conversationId>": {
      "custom_id": "wcs-abc123def456",
      "title": "BidMaster AI development",
      "github_path": "bidmaster-ai-development",
      "last_synced": "2026-10-02T01:20:00",
      "created_at": "2026-10-02T01:00:00"
    }
  }
}
```

The `platform` field records which AI product this workspace uses, so future syncs skip re-detection.

**Management commands:**
```bash
# Get custom ID for a session
python scripts/manage_session_id.py get <work_dir> <conversation_id>

# Set custom ID
python scripts/manage_session_id.py set <work_dir> <conversation_id> <custom_id>

# List all tracked sessions in a workspace
python scripts/manage_session_id.py list <work_dir>

# Generate a new unique ID
python scripts/manage_session_id.py generate
```

## Configuration

Credentials are stored in `config.json` inside the skill directory (created by
`bootstrap.py`), **not** in environment variables.

```json
{
  "backend": "github",
  "github": { "token": "ghp_...", "owner": "YourName", "repo": "YourRepo", "branch": "main" },
  "gitee":  { "token": "...",     "owner": "YourName", "repo": "YourRepo", "branch": "master" }
}
```

| Field | Description |
|-------|-------------|
| `backend` | Active platform: `github` or `gitee` |
| `<backend>.token` | Personal access token |
| `<backend>.owner` | Username or organization |
| `<backend>.repo` | Repository name for sessions |
| `<backend>.branch` | Branch (default `main` for GitHub, `master` for Gitee) |

Both platforms can be configured at once; switch with
`python scripts/bootstrap.py set --backend gitee ...`.

Legacy `GITHUB_TOKEN` / `GITHUB_OWNER` / `GITHUB_REPO` env vars are still read as a
fallback for GitHub only, but do not rely on them.

## Important Notes

- **NEVER use git.** All remote operations go through `scripts/storage_backend.py` (HTTP API).
- **Context bridge mode does NOT sync actual conversation history** (raw log files) — it syncs an extracted summary. Use **hardcore sync mode** to migrate the real conversation.
- **Hardcore sync modifies WorkBuddy internal data.** Always dry-run first, keep the automatic `sessions.json` backup, and restart WorkBuddy afterwards.
- **Platform detection is mandatory before every sync operation.** Different products store conversation data in different locations; skipping detection may read the wrong data.
- **Only WorkBuddy has a full parser.** For 通义灵码/文心快码/CodeArts, the skill locates storage but may need the user to specify files manually (encrypted or SQLite formats).
- **Session similarity matching is heuristic** (based on title word overlap). Always confirm with the user before refreshing an existing session.
- **Timestamp comparison** uses ISO 8601 format. Local modification time is tracked via `session-sync.json`; remote modification time comes from commit history.
- **API rate limits** apply (GitHub ~5000 req/h authenticated). The skill uses minimal calls.
- **Sensitive data warning**: Do not store API keys, passwords, or confidential information in the synced context. Review the packaged files before uploading.

## Scripts Reference

### scripts/bootstrap.py
First-run setup and validation. Commands: `check`, `guide`, `set`, `verify [--create-repo]`.
**Run this before any remote operation.**

### scripts/push_session.py
One-shot upload: export the current session and push it to the remote repository.
Options: `--conversation-id`, `--session-name`, `--no-outputs`, `--no-memory`, `--dry-run`, `--backend`.

### scripts/pull_session.py
One-shot download + restore. Options: `--list`, `--target-workdir`, `--apply`, `--backend`.

### scripts/wcs_config.py
Local config file management (read/write `config.json`, credential resolution, masking).
Commands: `show`, `path`, `check`.

### scripts/storage_backend.py
Unified GitHub + Gitee REST client (no git). Provides `get_store()`, then
`repo_exists`, `ensure_repo`, `list_dir`, `get_file`, `put_file`, `delete_file`,
`list_sessions`, `upload_session`, `download_session`. Also `generate_session_readme()`.
Handles files >1 MB via the Git Blobs API (GitHub omits inline content for large files).
Retries transient network errors. Run `python scripts/storage_backend.py --list` for a
connectivity + listing smoke test.

### scripts/platforms.json
Platform registry defining each product's conversation storage paths, format, and parser identifier. Edit this file to add new platforms.

### scripts/detect_platform.py
Scans local filesystem to detect installed AI products. Commands: `detect [--json]`, `list`, `get <platform_id>`.

### scripts/extract_context.py
Extracts structured context from WorkBuddy JSONL conversation logs. (Full parser; other platforms pending.)

### scripts/manage_session_id.py
Local session ID tracking and metadata management.

### scripts/path_map.py
workDir ↔ projects directory encoding, plus cross-device workspace matching.
Commands: `encode`, `decode`, `leaf`, `resolve`, `workspaces`, `match <leaf_name>`.

### scripts/sync_export.py
Hardcore sync export: package a session's conversation history + session entry + (optionally)
workspace outputs/memory into a staging folder with `sync-manifest.json`.

### scripts/sync_import.py
Hardcore sync import: restore a staged session onto this device with cross-device path
remapping, session-index merging, and automatic backup. Dry-run by default; use `--apply` to write.
