---
name: work-context-sync
description: This skill should be used when the user wants to sync AI assistant session context across devices via GitHub. It supports multiple AI products (WorkBuddy/腾讯, 通义灵码/阿里, 文心快码 Comate/百度, CodeArts Agent/华为), detects the platform in use, extracts conversation context, packages workspace artifacts and memory files, uploads to a GitHub repository, and restores context on another machine. Trigger phrases include "sync session", "upload session to GitHub", "download session from GitHub", "continue session on another computer", "backup conversation context", "restore workspace context", "跨设备同步会话".
agent_created: true
---

# Work Context Sync

## Overview

Sync AI assistant session context across devices using GitHub as the storage backend. The skill first detects which AI product the user is using (WorkBuddy, 通义灵码, 文心快码 Comate, or CodeArts Agent), locates the correct conversation storage location for that platform, then extracts conversation summaries, workspace artifacts, and memory files. It packages everything and uploads to a GitHub repository. On another device, the skill can download and restore the context.

**Key capabilities:**
- Multi-platform support: auto-detect which AI product is installed
- Extract structured context from local conversation logs (platform-specific parsers)
- Package workspace outputs, memory files, and session metadata
- Upload to GitHub with duplicate detection and session ID management
- Download and restore context on another device
- Compare local vs. remote timestamps to avoid overwriting newer data

## When to Use

Use this skill when:
- The user wants to continue an AI assistant session on a different computer
- The user wants to backup or archive a session's context to GitHub
- The user wants to restore a previously synced session from GitHub
- The user mentions switching devices, cross-device work, session portability

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

1. **GitHub Personal Access Token** with `repo` scope
   - Generated at: https://github.com/settings/tokens
   - Store securely; the skill reads it from environment variable `GITHUB_TOKEN`
2. **GitHub Repository** (can be private) for storing session contexts
   - Default repo name: `workbuddy-session-sync`
   - Configure via `GITHUB_OWNER` and `GITHUB_REPO` environment variables
3. **Python 3** available in the environment

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

## Step 2: Check GitHub for Similar Sessions

Before uploading, check if a similar session already exists on GitHub:

```bash
export GITHUB_TOKEN=<token>
export GITHUB_OWNER=<username>
export GITHUB_REPO=workbuddy-session-sync

python scripts/github_sync.py find "<session_title>"
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

Create a `README.md` for the session folder on GitHub:

```python
from scripts.github_sync import generate_session_readme

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
from scripts.github_sync import GitHubSync

sync = GitHubSync(token, owner, repo)
files = {
    'SESSION_CONTEXT.md': context_md_content,
    'README.md': readme_content,
    'outputs/report.xlsx': report_bytes,
    # ... other files
}
results = sync.upload_session('session-folder-name', files, 'Sync session context')
```

**After upload:**
- Record the custom ID locally using `manage_session_id.py`
- Store in `<workspace>/.workbuddy/session-sync.json`

## Step 5: Download and Restore

### Case A: Local session has custom ID

1. Read custom ID from local `session-sync.json`
2. Search GitHub for matching session by ID
3. Compare timestamps:
   - If GitHub version is newer: download and restore
   - If local is newer: warn user, ask for confirmation
4. Download session package
5. Extract files to workspace
6. Read `SESSION_CONTEXT.md` and inject into conversation context

### Case B: Local session has no custom ID

1. List all available sessions from GitHub
2. Present to user with titles and last modified dates
3. User selects session to download
4. Download and extract
5. Record new custom ID locally

**Download command:**
```python
from scripts.github_sync import GitHubSync

sync = GitHubSync(token, owner, repo)
files = sync.download_session('session-folder-name')
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

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | Yes | - | GitHub Personal Access Token with repo scope |
| `GITHUB_OWNER` | Yes | - | GitHub username or organization |
| `GITHUB_REPO` | No | `workbuddy-session-sync` | Repository name for session storage |

## Important Notes

- **This skill does NOT sync actual conversation history** (raw log files). It syncs extracted context, workspace artifacts, and memory files to enable cross-device continuity.
- **Platform detection is mandatory before every sync operation.** Different products store conversation data in different locations; skipping detection may read the wrong data.
- **Only WorkBuddy has a full parser.** For 通义灵码/文心快码/CodeArts, the skill locates storage but may need the user to specify files manually (encrypted or SQLite formats).
- **Session similarity matching is heuristic** (based on title word overlap). Always confirm with the user before refreshing an existing session.
- **Timestamp comparison** uses ISO 8601 format. Local modification time is tracked via `session-sync.json`; GitHub modification time comes from commit history.
- **GitHub API rate limits** apply: 5000 requests/hour for authenticated users. The skill uses minimal API calls (list, get content, create/update).
- **Sensitive data warning**: Do not store API keys, passwords, or confidential information in the synced context. Review `SESSION_CONTEXT.md` before uploading.

## Scripts Reference

### scripts/platforms.json
Platform registry defining each product's conversation storage paths, format, and parser identifier. Edit this file to add new platforms.

### scripts/detect_platform.py
Scans local filesystem to detect installed AI products. Commands: `detect [--json]`, `list`, `get <platform_id>`.

### scripts/extract_context.py
Extracts structured context from WorkBuddy JSONL conversation logs. (Full parser; other platforms pending.)

### scripts/github_sync.py
GitHub API client for listing, uploading, and downloading session packages.

### scripts/manage_session_id.py
Local session ID tracking and metadata management.
