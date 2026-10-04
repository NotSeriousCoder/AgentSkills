---
name: work-context-sync
description: This skill should be used when the user wants to sync AI assistant session context across devices via GitHub or Gitee. It uses platform HTTP APIs only (no git, no Docker), and stores credentials in a local config file written by a first-run bootstrap step. The logic is organised as TWO mainlines — 导出 (export) and 导入恢复 (import/restore) — each a closed loop, both sharing platform detection and path remapping. It supports two modes — "context bridge" (extract a readable SESSION_CONTEXT.md summary) and "hardcore sync" (migrate the full conversation history so the session truly appears in WorkBuddy's session list). It can also restore a standalone offline WorkBuddy 会话导出包 (.zip with 会话原始文件/ + 工作空间存档/) onto this machine, and can create a WorkBuddy workspace on demand. IMPORTANT: since WorkBuddy 2.115.0 the session list and workspace list are served from ~/.workbuddy/workbuddy.db (tables sessions/workspaces), NOT from app/sessions.json — every restore must register rows there via scripts/wb_db.py. Hard safety rules: verify the package's MD5 manifest before writing; never create a workspace silently (suggest a path, wait for user confirmation); back up before every overwrite; abort on platform mismatch (sourcePlatform vs local). Supports multiple AI products (WorkBuddy/腾讯, 通义灵码/阿里, 文心快码 Comate/百度, CodeArts Agent/华为). Trigger phrases include "sync session", "upload session to GitHub", "download session from GitHub", "continue session on another computer", "backup conversation context", "restore workspace context", "跨设备同步会话", "硬核同步", "迁移会话", "恢复会话导出包", "把压缩包里的会话恢复到本机", "创建工作空间", "会话看不到", "会话导不出来".
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
- Verify a package's MD5 manifest and record its `sourcePlatform` before restoring

## 流程总览（两条主线）

整个技能收敛成**两条各自闭环的主线**，共用同一套「平台识别 + 路径映射」：

```
导出主线                        导入恢复主线
意图识别                        意图识别
  ↓                               ↓
平台识别（定位会话文件）          确定会话源（有则识别 / 无则询问用户）
  ↓                               ↓
提取会话数据 + 文件               ┌─ GitHub/Gitee 仓库 → 下载会话文件
  ↓                               ├─ 本地文件夹 / 压缩包 → 解压 + MD5 完整性校验
打包（含 sourcePlatform）          └─ 百度云盘 / 其他方式 → 已识别但不支持，给替代方案
  ↓                               ↓
用户选择导出方式                  平台匹配校验（sourcePlatform vs 本机）
  ├─ 导出到本地（离线包）          ↓
  └─ 导出到云端仓库                目标会话已存在？ → 覆盖（先备份）
      ├─ 远端已存在相同会话？       ↓
      │   ├─ 存在 → 覆盖（先备份）  确认/建议目标工作空间（AI 建议 → 用户确认）
      │   └─ 不存在 → 直接上传       ↓
      └─ 上传成功？ 否 → 重试       注册注册表（workbuddy.db 的 workspaces + sessions 行）
  ↓                               ↓
流程完成                        流程完成（重启 WorkBuddy 后可见）
```

设计要点（与实现一一对应）：

| # | 要点 | 落点 |
|---|---|---|
| 1 | 导出/导入**两条主线**，不再写成"三个并列入口" | 本表 + `Workflow Decision Tree` |
| 2 | 导入侧「确定会话源」是**一个**环节（有则识别、无则询问），不是两个连续菱形 | 见下 Decision Tree |
| 3 | 三路会话源从**菱形拉三条独立出线**，不做十字裸交叉 | 同上 |
| 4 | `sourcePlatform` 是**打包元数据的一部分**，不是并列的第三路产物 | `sync_export.py` manifest v3 |
| 5 | 本地包进入前必须**解压 + MD5 完整性校验** | `restore_export_package.py` |
| 6 | 不可逆动作一律**先备份**（覆盖文件 / 覆盖 DB 行） | 各脚本写前备份 |
| 7 | 工作空间路径：**AI 给建议 → 用户确认 → 创建**，绝不静默新建 | `--create-workspace` |
| 8 | 百度云盘 / 其他方式：**已识别但不支持**，统一给替代方案 | 见「不支持的会话源」 |

可留档的可视化版本见 `outputs/工作流-定稿流程图.html`（左侧导航 + 导出线/导入线两张图）。

## When to Use

Use this skill when:
- The user wants to continue an AI assistant session on a different computer
- The user wants to backup or archive a session's context to GitHub or Gitee
- The user wants to restore a previously synced session from GitHub or Gitee
- The user mentions switching devices, cross-device work, session portability
- The user gives you an offline WorkBuddy 会话导出包 (`.zip`) and wants it back in WorkBuddy
- The user says a restored session **cannot be seen** in the WorkBuddy session list
- The user asks you to **create a WorkBuddy workspace** (＝ 登记一行 `workspaces`)

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
6. **Restoring a session ≠ copying files.** Since WorkBuddy 2.115.0 the session list and the
   workspace list come from `~/.workbuddy/workbuddy.db` (tables `sessions` / `workspaces`).
   Writing `app/sessions.json` alone does nothing visible in the UI. Always finish with
   `wb_db.register_workspace()` + `wb_db.register_session()`. See `scripts/wb_db.py`.
7. **Never create a workspace silently.** When the target workspace does not exist locally,
   the script prints a **suggested path** and exits. Relay that suggestion to the user, get
   confirmation, then re-run with `--target-workdir "<确认后的路径>" --create-workspace`.
   （工作空间路径口径：**AI 给建议 → 用户确认 → 创建**。）
8. **Verify before writing (离线包).** `restore_export_package.py` parses the package's MD5
   manifest and checks every raw file. A mismatch **aborts** — do not add `--force` unless the
   user explicitly accepts a possibly-corrupt package.
9. **Back up before every overwrite.** Overwriting an existing conversation log, the
   `sessions.json` entry, or a `workbuddy.db` row all back up first automatically. Never
   treat "导入" as a purely additive operation — for an existing convId it is an **覆盖**.
10. **Abort on platform mismatch.** The manifest records `sourcePlatform`; the importer
    compares it with the locally detected product. On mismatch the default is **abort**;
    `--force` (with explicit user consent) is required to proceed.

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
python scripts/pull_session.py <session-folder> --target-workdir "D:\Work\WB\x" --create-workspace --apply

# restore a LOCAL WorkBuddy 会话导出包 (.zip or folder) — no remote needed
python scripts/restore_export_package.py <包目录或.zip>                    # dry-run（含 MD5 校验）
python scripts/restore_export_package.py <包目录或.zip> --workspace "C:\Users\X\WorkBuddy\职业发展规划" --apply
python scripts/restore_export_package.py <包目录或.zip> --workspace "<用户确认的新路径>" --create-workspace --apply

# 单独创建一个工作空间（往 workspaces 表登记一行）
python scripts/wb_db.py backup
python -c "import sys; sys.path.insert(0,'scripts'); import wb_db; wb_db.register_workspace(r'C:\Users\X\WorkBuddy\新工作空间')"
python scripts/wb_db.py show        # 核对
```

Both accept `--backend github|gitee` to override the active platform.

**What `push_session.py` does:** resolve credentials → detect the local product →
locate the conversation JSONL → export a staging folder → upload it → record the
custom ID locally. **What `pull_session.py` does:** download the session folder →
run the hardcore import (path remapping, session-index merge, backup).

## Two Sync Modes

| | **Context bridge 模式** | **Hardcore sync 模式** |
|---|---|---|
| 产出 | `SESSION_CONTEXT.md` 摘要 | 完整对话历史 + `workbuddy.db` 的 `sessions`/`workspaces` 行（+ 遗留 `sessions.json`） |
| 另一台电脑效果 | 只恢复"上下文摘要"，看不到原对话 | **原对话真实出现在会话列表里** |
| 依赖 | 无 | 需要路径重映射 + 写 SQLite + 关闭/重启 WorkBuddy |
| 风险 | 极低 | 中（改 WorkBuddy 内部数据，有备份） |
| 适用 | 快速续接思路 | 完整迁移会话 |

入口一览（**这只是"从哪进"，不代表流程结构** —— 流程结构见上面的两条主线）：

| 入口 | 命令 | 属于哪条主线 | 场景 |
|---|---|---|---|
| GitHub/Gitee 硬核同步 | `push_session.py` / `pull_session.py` | 导出 + 导入 | 两台机器都装了本技能、有远端仓库 |
| **离线会话导出包** | `restore_export_package.py` | 导入 | 只有 WorkBuddy 自带"导出会话"产出的 zip |
| 仅上下文摘要 | `extract_context.py` | context bridge（另一条路） | 只要思路，不要对话 |

Ask the user which mode they want if it is ambiguous. Default to **context bridge** (safe).

## Hardcore Sync Mode

Full session migration. Copies the conversation history and registers it in the target device's session index so the session truly appears in the WorkBuddy UI.

### Data layout (why two locations matter)

| 数据 | 路径 | 类别 |
|------|------|------|
| 对话历史 | `~/.workbuddy/projects/<编码>/<conversationId>.jsonl` | 全局仓库 |
| **会话/工作空间注册表（权威）** | `~/.workbuddy/workbuddy.db` → `sessions` / `workspaces` 表 | 全局仓库 |
| 会话索引（旧版遗留缓存） | `~/.workbuddy/app/sessions.json` | 全局仓库 |
| 产物 | `<工作空间>/outputs/` | 工作空间 |
| 记忆 | `<工作空间>/.workbuddy/` | 工作空间 |

The conversation history is **not** inside the workspace folder — it lives under the global `~/.workbuddy/projects/` directory. This is why copying only the workspace `.workbuddy` + `outputs` is insufficient.

### ⚠️ WorkBuddy 2.115.0 起：UI 列表来自 SQLite，不是 sessions.json

实测（2026-10-04，WorkBuddy 2.115.0 / Windows）确认：

- `~/.workbuddy/workbuddy.db` 里两张表决定 UI 能看到什么：
  - `workspaces(path TEXT PRIMARY KEY, last_opened_at INTEGER)` —— 工作空间列表
  - `sessions(id, cwd, user_id, title, custom_title, status, created_at, updated_at,
    deleted_at, is_playground, model, last_activity_at, source_mode, permission_mode,
    use_sandbox_cli, mode, plugin_context_json, ...)` —— 会话列表
- `app/sessions.json` **仍在被重写**（不是死文件），但只是遗留缓存：
  **只往里加条目，会话不会出现在 UI 里**。
- 反证：DB `sessions` 有 17–18 行，`app/sessions.json` 只有 10–11 条，
  `workspace/sessions/` 只有 10 个目录 —— May/June 的会话**只存在于 DB**。

所以把一条外部会话搬进本机 UI，必须**三样齐备**：

| # | 要写入 | 位置 |
|---|--------|------|
| 1 | 对话记录 `<convId>.jsonl` | `~/.workbuddy/projects/<本机编码>/` |
| 2 | `sessions` 行（`cwd` = 本机工作空间路径） | `workbuddy.db` |
| 3 | `workspaces` 行 | `workbuddy.db` |

第 3 步最关键：**工作空间本身不在 `workspaces` 表里，会话就没有可挂靠的位置，界面里根本看不到**。
"帮我在 WorkBuddy 里创建一个工作空间" = 往 `workspaces` 表插一行（`path` + `last_opened_at`）。

> 写 DB 前务必备份 `workbuddy.db` + `-wal` + `-shm`：应用正占用它。WAL 模式支持并发写，
> 但要设 `PRAGMA busy_timeout`（脚本里已设 15s）。写完后重启 WorkBuddy 最稳。


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

**Manifest fields the importer depends on** (`formatVersion: 3`):

| 字段 | 用途 |
|---|---|
| `conversationId` / `title` | 会话标识与显示名 |
| `sourceWorkDir` / `sourceProjectsDir` / `sourceLeafName` | 路径重映射（末级目录同名匹配） |
| **`sourcePlatform`** | **导入端做「平台匹配校验」**（v3 新增） |
| `createdAtMs` / `updatedAtMs` | 建 `workbuddy.db` 行的准确时间戳 |
| `model` / `userId` | 建 `workbuddy.db` 行的模型与用户字段 |

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

# 2. apply (若目标工作空间是新建的，必须加 --create-workspace)
python scripts/sync_import.py <staging_dir> [--target-workdir <path>] [--create-workspace] --apply
```

Import steps:
1. Safety check (warns if WorkBuddy is running)
2. **平台匹配校验**：manifest 的 `sourcePlatform` vs 本机 `detect_platform`；不一致 → 中止
   （需用户确认后加 `--force` 才继续）
3. **Path remapping**: ① 源工作空间在本机存在 → 直接用它（`same-path`）；
   ② 否则按**末级目录同名**找（`path_map.find_matching_workspace`）；
   ③ 都失败 → 只打印**建议路径**并退出，等用户确认（不自行创建）
4. **工作空间不存在时不静默创建** —— 需显式 `--create-workspace`
5. 覆盖检测：已存在同名 jsonl 或 `sessions` 行 → 提示「覆盖（先备份）」
6. Place JSONL at `~/.workbuddy/projects/<本地编码>/<convId>.jsonl`（覆盖前先备份原文件）
7. **Merge** (not overwrite) the session entry into legacy `sessions.json` — 写前备份
8. **Register in `workbuddy.db`**：`workspaces` 行 + `sessions` 行（覆盖语义，写前备份整库）。
   时间戳取 manifest `createdAtMs`/`updatedAtMs`（缺失则从 jsonl 现场推导），
   `model` 取 manifest，`cwd` 用本机路径 ← **只有这一步决定 UI 可见性**
9. Restore `outputs/` and `.workbuddy/` into the target workspace
10. Instruct the user to fully quit and restart WorkBuddy

`sync_export.py` 会把这些字段写进 manifest（`formatVersion: 3`），所以导入端不需要再猜。

### Hardcore sync safety rules (mandatory)

- **Never overwrite `sessions.json`** — always merge; always back up first.
- **Never touch `IDENTITY.md` / `USER.md` / `SOUL.md`** — identity files must not cross devices.
- **Warn about a running WorkBuddy** — it may overwrite the index on exit; if the session
  does not appear after restart, re-run `--apply`.
- **Dry-run by default** — only write when `--apply` is passed.
- **Always register in `workbuddy.db` too** — `sessions.json` alone does NOT show up in the UI
  (see the 2.115.0 note above). Back up the DB before writing.
- **绝不静默创建工作空间** — 目标路径不存在时只**建议**，等用户确认后加 `--create-workspace`。
- **覆盖必先备份（含工作空间记忆）** — 覆盖已有会话文件、`sessions.json`、`workbuddy.db` 行
  **以及目标工作空间 `.workbuddy/`（memory + 素材）** 之前一律自动备份 `.bak-<时间戳>`。
  保护范围必须与风险范围对齐：`.workbuddy` 是"本机自己的记忆"，比外来会话文件更该保住。
- **auto 匹配撞记忆 → 不静默选定** — 同名匹配到的目标工作空间若已含**与包内同名的
  `memory/` 文件**，默认**停止**（退出码 2），打印完整覆盖清单后要求用户用
  `--workspace`（导出包）/ `--target-workdir`（远端）显式确认。见 `fs_safety.memory_conflicts()`。
- **平台不匹配默认中止** — `sourcePlatform` 与本机平台不一致时需用户确认后加 `--force`。

## 离线会话导出包（导入主线的「本地文件夹 / 压缩包」这一路）

有些用户拿到的不是 GitHub 上的 session 文件夹，而是 WorkBuddy 自带"导出会话"功能产出的
**离线压缩包**。它的结构与 `sync_export.py` 的 staging 完全不同：

```
<包名>_会话导出/
  会话原始文件/<convId>.jsonl                  # 核心对话记录
  会话原始文件/<convId>.meta.json
  会话原始文件/<convId>.file-rollback.ndjson
  工作空间存档/.workbuddy/...                   # 记忆 + 工作空间素材
  恢复会话.py  /  恢复说明.txt  /  <标题>.md      # 人可读全文 + 自带脚本
```

**关键坑（三层）**：
1. 包内自带的 `恢复会话.py` 只做「拷贝 jsonl + 拷贝 .workbuddy」，**既不写 `app/sessions.json`，
   也不写 `workbuddy.db`**。只跑自带脚本，会话在 UI 里根本不会出现。
2. 即使补写了 `app/sessions.json` **仍然看不到** —— 2.115.0 的 UI 读的是 `workbuddy.db`。
   而且工作空间本身必须先登记进 `workspaces` 表，否则会话没有可挂靠的位置。
3. 自带脚本拷 `.workbuddy` 时是**无备份裸覆盖**，会把目标工作空间自己的 `memory/` 直接
   冲掉。本技能用 `fs_safety.copy_tree_with_backup()` 逐个文件先 `.bak` 再写，并在 auto
   同名匹配撞到同名 `memory/` 文件时**停下等确认**（见 `scripts/fs_safety.py`）。

正确做法 —— 用 `restore_export_package.py`，它一次跑完整条链路：

**解压 → 解析包内 MD5 清单做完整性校验 → 确认目标工作空间 → 备份 → 覆盖写入 → 注册注册表**

```bash
# 1) 先 dry-run 看计划（自动读出会话 ID / 标题 / 时间跨度 / 源工作空间 / 模型 / 源平台）
#    同时完成解压 + MD5 完整性校验，逐项打印 [OK] / [不一致]
python scripts/restore_export_package.py "D:\Desktop\xxx_会话导出_20260101.zip"

# 2) 目标工作空间：① 源路径在本机存在 → 直接用
#                   ② 否则按「末级目录同名」自动匹配
#                   ③ 都没有 → 只打印建议路径，等用户确认（不自行创建）
python scripts/restore_export_package.py "D:\...\xxx.zip" \
    --workspace "C:\Users\71026\WorkBuddy\职业发展规划" --apply

# 3) 用户确认要新建时
python scripts/restore_export_package.py "D:\...\xxx.zip" \
    --workspace "<用户确认的路径>" --create-workspace --apply
```

对应定稿流程的 6 步，逐条落到脚本行为：

| 步 | 动作 | 实现 |
|---|---|---|
| 1 | 解压 | `locate_package()` 支持 `.zip` 或目录 |
| 2 | **MD5 完整性校验** | `find_expected_checksums()` 从包内 `恢复说明.txt` 的【文件校验】段落（或 `*.md5` / checksum 文件）解析 `<32位MD5>  <文件名>`；不通过则**中止**（需用户明确 `--force`） |
| 3 | 确认目标工作空间 | 源路径存在→`same-path`；否则同名匹配（**但若该空间已含与包内同名的 `memory/` 文件 → 不静默选定，退出码 2**）；都没有→**建议路径**（`wb_db.suggest_workspace`）+ 退出码 2 |
| 4 | 备份 | 覆盖已有原始文件前逐文件备份 `.bak-<时间戳>`；**`.workbuddy` 内每个被覆盖文件也逐文件备份**；`sessions.json` 整文件备份；DB 整库备份 |
| 5 | 覆盖写入 | 原始文件 → `~/.workbuddy/projects/<本机编码>/`；`工作空间存档/.workbuddy` → 目标工作空间（**逐文件备份后覆盖**；dry-run 会列出每个将覆盖文件及其「内容相同/不同」） |
| 6 | **注册注册表** | `workbuddy.db`：`workspaces` 行 + `sessions` 行 ← 真正决定 UI 可见性 |

注册字段口径：`cwd` 用本机路径、`user_id` 用本机已有值、时间戳取自 jsonl 首尾、
`model` 取自 `providerData`、`status='completed'`、`is_playground=0`；
`sourcePlatform` 固定记为 `workbuddy`（该导出格式只可能来自 WorkBuddy）。

**只想单独"创建一个工作空间"**（不恢复会话）：

```bash
python -c "import sys; sys.path.insert(0,'scripts'); import wb_db; \
wb_db.backup_db(); wb_db.register_workspace(r'C:\Users\71026\WorkBuddy\新工作空间')"
python scripts/wb_db.py show      # 核对
```

## 不支持的会话源（已识别但不支持 → 给替代方案）

导入侧「确定会话源」会识别到三类来源，其中两类当前**不支持**。遇到时**不要报"暂不实现"就结束**，
而是识别出来 + 明确告知 + 给出可走的替代方案：

| 会话源 | 状态 | 处理方式 |
|---|---|---|
| GitHub / Gitee 仓库 | ✅ 支持 | `pull_session.py <session-folder>` |
| 本地文件夹 / 压缩包 | ✅ 支持 | `restore_export_package.py <包>` |
| 百度云盘 | ❌ 不支持 | 识别到即提示：**请改用 ① 让导出方直接给本地包/压缩包，或 ② 把包放进 GitHub/Gitee 仓库再走远端同步** |
| 其他方式导出 | ❌ 不支持 | 同上，统一走这一个通用兜底：**提示替代方案**（本地包 或 远端仓库） |

> 两条"不支持"分支合并成**同一个通用兜底出口**，不再各自画一个断头框。

**注意**：不要在目标工作空间已有同名 `.workbuddy/memory/YYYY-MM-DD.md` 时把它当目标
（会覆盖当天记忆）。默认给「新建独立工作空间」或「让用户确认路径」。
> 该规则已由代码强制执行：auto 同名匹配撞到同名 `memory/` 文件时，
> `restore_export_package.py` / `sync_import.py` 会打印完整覆盖清单后**退出码 2**，
> 要求用 `--workspace` / `--target-workdir` 显式确认。


## Supported Platforms

| 平台 | 厂商 | 会话存储位置 | 支持状态 |
|------|------|-------------|---------|
| WorkBuddy | 腾讯 | `~/.workbuddy/projects/*/*.jsonl` + `workbuddy.db`(`sessions`/`workspaces`) + `app/sessions.json`(遗留) | ✅ 完整支持 |
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

两条主线，各自闭环。**先判断用户要"导出"还是"导入恢复"**，再进对应主线。

```
用户提出会话同步需求
|
+-- Step 0: 确定意图 —— 是「导出」还是「导入恢复」？
|   |   （含糊时问用户；"把会话搬到另一台电脑"通常两边都要做一遍）
|   |
|   +-- 要导出 --------------------------------------------> 【导出主线】
|   +-- 要导入 --------------------------------------------> 【导入主线】
|
+-- 【导出主线】
|   |
|   +-- 1. 平台识别：detect_platform.py 定位本机产品的会话文件
|   +-- 2. 提取会话数据 + 会话文件（WorkBuddy 走 projects/*.jsonl）
|   +-- 3. 打包会话文件与元数据（含 sourcePlatform）  ← manifest v3
|   +-- 4. 用户选择导出方式
|   |   |
|   |   +-- 导出到本地（离线包）
|   |   |     +-- 压缩保存到本地 → 流程完成
|   |   |
|   |   +-- 导出到云端仓库（GitHub / Gitee）→ push_session.py
|   |   |     +-- 远端已存在相同会话？
|   |   |     |     +-- 存在   → 覆盖远端会话内容（先备份）→ 上传
|   |   |     |     +-- 不存在 → 直接上传本地会话文件到云端 → 上传
|   |   |     +-- 上传成功？
|   |   |           +-- 是 → 记录自定义 ID → 流程完成
|   |   |           +-- 否 → 重试（回到上传）
|   |   |
|   |   +-- 其他方式导出 → 【已识别但不支持】统一兜底：提示替代方案 → 流程完成
|   |
|
+-- 【导入主线】
    |
    +-- 1. 确定会话源（一个环节：用户给了就识别，没给就问）
    |   |
    |   +-- 用户提供了哪种会话源？
    |         +-- GitHub / Gitee 仓库   → 下载会话文件
    |         +-- 本地文件夹 / 压缩包   → 解压 + MD5 完整性校验
    |         |      （校验不通过 → 中止，不写入任何文件）
    |         +-- 百度云盘              → 【已识别但不支持】→ 提示替代方案
    |         +-- 其他方式              → 【已识别但不支持】→ 提示替代方案
    |
    +-- 2. 平台匹配校验：sourcePlatform（来自 manifest）vs 本机平台
    |   |   （不匹配 → 默认中止；用户确认后才 --force）
    |   +-- 是否匹配？不匹配 → 提示不匹配 → 流程结束
    |
    +-- 3. 目标会话是否已存在？
    |   |   （已存在 → 本次为「覆盖」，写前先备份）
    |   +-- 删除/覆盖指定会话的对话记录（先备份）→ 导入会话文件
    |
    +-- 4. 确认目标工作空间
    |   |   +-- 源工作空间在本机存在     → 直接用它
    |   |   +-- 否则按末级目录同名匹配   → 唯一匹配则用它
    |   |   +-- 都不行 → AI 建议路径 → 用户确认 → --create-workspace 创建
    |
    +-- 5. 注册注册表
    |   |   = 同时往 workbuddy.db 写 workspaces 行 + sessions 行
    |
    +-- 流程完成（完全退出并重启 WorkBuddy 后可见）
```

配套脚本入口：

| 主线动作 | 命令 |
|---|---|
| 导出并上传（一次性） | `python scripts/push_session.py [--dry-run]` |
| 列出 / 下载并恢复远端会话 | `python scripts/pull_session.py --list` / `pull_session.py <folder> --apply` |
| 恢复本地离线导出包 | `python scripts/restore_export_package.py <包或.zip> --apply` |
| 只要上下文摘要（context bridge） | `python scripts/extract_context.py <jsonl> <out.md>` |

> **不要照旧版流程做**：上传前不需要先生成 `README.md` / 打包 `SESSION_CONTEXT.md`，
> 下载后也不需要"注入上下文"——那是 **context bridge 模式**的步骤（下面 Steps 2–6），
> 与 `push_session.py` / `pull_session.py` 走的硬核同步是两条不同的路。

## 平台识别与平台匹配校验

平台识别在**导出主线**里是第 1 步（用于定位会话文件），在**导入主线**里是第 2 步的
校验依据（`sourcePlatform` vs 本机）。两条主线都要用到，所以这里单独说明。

平台识别很重要，因为各产品把会话存在完全不同的位置，识别错了就会读到错的文件。

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

### 0.4 平台匹配校验（仅导入主线）

导入时必须比对**两个平台**：

| 一侧 | 来源 |
|---|---|
| 这份会话原本属于哪个平台 | 包的 `sync-manifest.json` → **`sourcePlatform`**（离线包固定为 `workbuddy`） |
| 本机装的是什么 | `detect_platform.py detect` → `detect_local_platform()` |

- 一致 → 继续。
- 不一致 → **默认中止**（退出码 3），提示用户"这条会话来自另一个 AI 产品，结构可能不同"；
  用户明确接受后才加 `--force`。
- 包内未记录（老包）→ 不阻断，标注"未知"继续。

这正是导出侧必须把 `sourcePlatform` 写进打包元数据的原因 —— 少了它导入侧无从比对。

## Step 1: Extract Session Context

> **本节及 Steps 2–6 都是 context bridge 模式专用的流程。**
> 硬核同步（`push_session.py` / `pull_session.py` / `restore_export_package.py`）**不走**这些步骤。

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

## Step 2 (context bridge): Check the Platform for Similar Sessions

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

## Step 3 (context bridge): Generate Session README

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

## Step 4 (context bridge): Package and Upload

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

## Step 5 (context bridge): Download and Restore

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

## Step 6 (context bridge): Inject Context into Conversation

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
- **UI 可见性取决于 `workbuddy.db`，不是 `sessions.json`。** 只写 `sessions.json` 是无效的（2026-10-04 实测踩过这个坑）。任何"搬会话到本机"的操作都要走 `wb_db.register_workspace()` + `wb_db.register_session()`。
- **工作空间本身也要登记**：`workspaces` 表里没有这一行，会话就没有可挂靠的位置，界面里看不到。
- **绝不静默创建工作空间**：目标路径不存在时只**建议**并退出（`wb_db.suggest_workspace`），
  用户确认后加 `--create-workspace` 才创建。工作空间路径口径 = AI 给建议 → 用户确认 → 创建。
- **导入 = 覆盖语义**（当 convId 已存在时）。覆盖会话文件 / `sessions.json` / `workbuddy.db` 行
  之前一律自动备份；不要把它当纯新增操作。
- **离线包先校验再写**：`restore_export_package.py` 会解析包内 MD5 清单，
  不一致即中止，除非用户明确接受 `--force`。
- **平台不匹配默认中止**：manifest 的 `sourcePlatform` 与本机平台不符时，需用户确认后 `--force`。
- **不支持的会话源统一兜底**：百度云盘 / 其他方式 → 提示替代方案（改用本地包或远端仓库），
  不要只回一句"暂不实现"就结束。
- **Hardcore sync modifies WorkBuddy internal data.** Always dry-run first, keep the automatic `sessions.json` + `workbuddy.db` backups, and restart WorkBuddy afterwards.
- **Platform detection is mandatory before every sync operation.** Different products store conversation data in different locations; skipping detection may read the wrong data.
- **Only WorkBuddy has a full parser.** For 通义灵码/文心快码/CodeArts, the skill locates storage but may need the user to specify files manually (encrypted or SQLite formats).
- **Session similarity matching is heuristic** (based on title word overlap). Always confirm with the user before refreshing an existing session.
- **Timestamp comparison** uses ISO 8601 format. Local modification time is tracked via `session-sync.json`; remote modification time comes from commit history.
- **API rate limits** apply (GitHub ~5000 req/h authenticated). The skill uses minimal calls.
- **Sensitive data warning**: Do not store API keys, passwords, or confidential information in the synced context. Review the packaged files before uploading.

## 版本兼容

| WorkBuddy 版本 | 会话/工作空间存储 | 本技能处理方式 |
|---|---|---|
| ≥ 2.115.0 | `workbuddy.db`（SQLite，权威）+ `app/sessions.json`（遗留缓存） | 两者都写；DB 为准 |
| < 2.115.0 | `app/sessions.json` | 只写 JSON 即可（脚本会自动检测 DB 是否存在，不存在则跳过） |

脚本用 `wb_db.db_available()` 判断，所以同一套命令在两种版本上都能跑。

**sync-manifest.json 版本**：

| formatVersion | 新增字段 | 兼容性 |
|---|---|---|
| 1 | 基础（convId / workDir / files） | — |
| 2 | `createdAtMs` / `updatedAtMs` / `model` / `userId` | 导入端无需再猜 DB 行 |
| **3** | **`sourcePlatform`** | 导入端可做平台匹配校验；老包缺该字段时标注"未知"不阻断 |

**验证记录**：2026-10-04 在 WorkBuddy 2.115.0 / Windows 11 上，用 `restore_export_package.py`
把一条离线导出包（684 条记录 / 6.7 MB jsonl / 源路径 `G:\...`）成功恢复到 `C:\Users\...`
的不同工作空间并在 UI 中可见 —— 说明跨盘符路径重映射 + SQLite 注册链路有效。
同日补齐：包内 MD5 清单校验（3 项全 OK）、平台匹配校验、工作空间"建议→确认"守卫。

**2026-10-05 加固（实测发现缺陷）**：原 `restore_export_package.py` 写工作空间存档
`.workbuddy` 时是**无备份裸覆盖**（会话文件/`sessions.json`/DB 都有备份，唯独最私密的
`memory/` 没有）。已修：①`fs_safety.copy_tree_with_backup()` 逐文件 `.bak`；
②dry-run 打印完整覆盖清单 + 每项「内容相同/不同」；③`fs_safety.memory_conflicts()` 守卫——
auto 同名匹配撞到同名 `memory/` 文件即退出码 2。同款修复同步应用到 `sync_import.py`。
实测：FDE 导出包（14 个 `.workbuddy` 文件，MD5 与本机全同）在守卫下先 exit 2，显式
`--workspace` 后 apply，14 个文件全部先备份再写入。

## Scripts Reference

### scripts/bootstrap.py
First-run setup and validation. Commands: `check`, `guide`, `set`, `verify [--create-repo]`.
**Run this before any remote operation.**

### scripts/push_session.py
One-shot upload: export the current session and push it to the remote repository.
Options: `--conversation-id`, `--session-name`, `--no-outputs`, `--no-memory`, `--dry-run`, `--backend`.

### scripts/pull_session.py
One-shot download + restore. Options: `--list`, `--target-workdir`, `--create-workspace`,
`--force`, `--apply`, `--backend`.

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
Manifest `formatVersion: 3` adds `sourcePlatform` on top of v2's `createdAtMs`, `updatedAtMs`,
`model`, `userId` (read from the JSONL / `workbuddy.db`), so the importing device can build an
accurate DB row **and** validate the platform. `--source-platform` overrides the detected value.
`workDir` resolution prefers `workbuddy.db`, falling back to `sessions.json`.

### scripts/sync_import.py
Hardcore sync import: restore a staged session onto this device. Steps = platform-match
validation (`--force` to override), cross-device path remapping (same-path → leaf-name match →
**建议路径 + 等用户确认**, never silently created: `--create-workspace`), overwrite detection with
**per-file backup before overwriting**, `sessions.json` merge, **`workbuddy.db` registration
(workspaces + sessions)**, then outputs/.workbuddy restore (**per-file `.bak` via
`fs_safety.copy_tree_with_backup`**; an auto-matched workspace carrying same-named `memory/`
files halts with exit 2 and demands `--target-workdir`). Dry-run by default; `--apply` to write.

### scripts/restore_export_package.py
Import a **WorkBuddy 自带"导出会话"离线包**（`.zip` 或目录，含 `会话原始文件/` +
`工作空间存档/`）。Full flow: 解压 → 解析包内 MD5 清单做**完整性校验**（不通过则中止）→
确认目标工作空间（源路径存在→直接用；否则同名匹配，**但若撞到同名 `memory/` 文件则退出码 2
等 `--workspace` 确认**；否则**建议路径 + 等用户确认**）→
**覆盖前逐文件备份（含 `.workbuddy`）** → 写入原始文件 + 工作空间 `.workbuddy` → 合并
`app/sessions.json` → **注册 `workbuddy.db` 的 workspaces + sessions 行**（真正决定 UI 可见性的一步）。
Options: `--workspace`, `--create-workspace`, `--force`, `--apply`, `--no-db`. Dry-run by default.

### scripts/fs_safety.py
导入侧共用的文件系统安全助手（`restore_export_package.py` / `sync_import.py` 都调用）。
原则：**保护范围与风险范围对齐**。`md5_file`、`rel_files`、`plan_conflicts(src, dst)`
（列出两边同名文件并标「内容相同/不同」）、`memory_conflicts()`（`memory/` 子集，用作守卫）、
`copy_tree_with_backup(src, dst)`（覆盖前逐个 `.bak-<时间戳>`，返回 `(copied, backups)`）。

### scripts/wb_db.py
WorkBuddy SQLite registry helper — the authoritative store for the session list and the
workspace list since v2.115.0. Functions: `db_available()`, `backup_db()`,
`register_workspace(path)`, `register_session(conv_id, cwd, title, created_at_ms,
updated_at_ms, model=...)`（**覆盖语义**，调用前先 `session_exists()` 判断覆盖/新增）,
`session_exists()`, `workspace_exists()`, `suggest_workspace(leaf)`, `get_session()`,
`get_session_full()`, `resolve_workdir()`, `list_workspaces()`, `count()`.
CLI: `show`, `backup`, `get <convId>`, `exists <convId>`, `suggest <leaf>`.
Use `register_workspace()` alone to simply "create a workspace" in WorkBuddy.
Always `backup_db()` first — the app holds the DB open (WAL, `busy_timeout` 15s).
