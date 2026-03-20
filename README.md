# NotionCodeMirror

**One command. Any GitHub repo. A living Notion workspace that stays in sync with your code — without feeding your entire codebase to an LLM.**

```bash
python notionmirror.py https://github.com/you/your-repo --notion-parent-id <page-id>
# → Done. Workspace: https://notion.so/...
```

---

## The problem

Documentation rots. You write a README, a Confluence page, or a Notion doc — and the moment you merge the next PR, it's already wrong. Keeping docs in sync with code is a solved problem in theory and an unsolved one in practice.

This project solves it by generating docs *from* the code, on demand, and writing them directly into Notion in a structured, navigable workspace.

---

## What it builds

Run one command against any repo and get a fully-populated Notion workspace:

```
🪞 your-repo — CodeMirror
  📊 Overview          → file count, language breakdown, symbol inventory
  🏗️ Architecture      → Claude-written narrative: modules, data flow, entry points
  🗄️ API Reference     → searchable database: Name | Kind | File | Signature | Summary
  📁 Modules/
    📄 src/            → file list, key symbols, Claude description of what this does
    📄 tests/
    📄 ...
```

The Architecture page is real prose written by Claude — not a file tree dump. The API Reference is a Notion database you can filter, sort, and share.

---

## What does a run actually cost?

The naive approach — dumping source files into a Claude prompt — would consume roughly **100K–500K tokens** for a medium-sized repo. NotionCodeMirror doesn't do that.

Phase 1 uses **zero Claude tokens**. [jcodemunch-mcp](https://github.com/jgravelle/jcodemunch-mcp) handles all the code analysis: indexing, symbol extraction, BM25 ranking, dependency traversal, class hierarchies. It's purpose-built for this and runs entirely locally. No LLM involved.

Phase 2 gives Claude a **pre-digested summary** — ranked symbols, dependency edges, file structure — rather than raw source. A repo with 200 files and 2MB of source typically produces ~8–12K tokens of structured input. Claude reads that, writes prose, and makes a handful of Notion API calls.

The batch DB optimization cuts costs further: Claude creates the API Reference database and hands back its ID. Python populates the rows directly via HTTP — no LLM in the loop for 100+ symbol inserts.

| Scenario | Naive approach | NotionCodeMirror |
|---|---|---|
| Phase 1 (code analysis) | ~200K tokens | **0 tokens** |
| Phase 2 input context | ~200K tokens | ~8–12K tokens |
| DB row population (100 symbols) | ~50K tokens | **0 tokens** |
| Incremental re-run, no code changes | ~200K tokens | **0 tokens** |

The result is a full workspace for a typical repo in the **10–15K token range** — comparable to a single detailed Claude conversation, not a bulk indexing job.

---

## How it works

NotionCodeMirror chains two MCP servers together with Claude as the orchestrator:

```
GitHub repo
    │
    ▼
[jcodemunch-mcp]          Phase 1 — Data gathering        ← 0 Claude tokens
  index_repo()            Indexes code, extracts symbols, builds dependency graph
  search_symbols()        Ranks symbols by centrality (BM25 + import graph)
  get_dependency_graph()  Maps module relationships
  get_context_bundle()    Pulls rich context for top symbols
    │
    ▼  RepoData (~8–12K tokens of structured data, not raw source)
    │
    ▼
[Claude claude-opus-4-6]  Phase 2 — Synthesis + writing   ← ~10–15K tokens total
  Reads pre-digested symbol data (not source files)
  Writes Architecture narrative in real prose
  Calls 5 Notion tools to build workspace structure
    │
    ▼
[Notion API]              Batch row inserts bypass Claude  ← 0 Claude tokens
  Pages, databases, and blocks created
```

**Multi-MCP orchestration** is the core idea: jcodemunch-mcp does the code-understanding work that would otherwise require massive context windows; Claude gets a clean structured summary and focuses on synthesis and writing.

---

## Quick start

**Prerequisites:** Python 3.10+, a Notion integration token, an Anthropic API key, and `jcodemunch-mcp` installed.

```bash
# 1. Clone and install dependencies
git clone https://github.com/jgravelle/notion-code-mirror
cd notion-code-mirror
pip install -r requirements.txt
pip install jcodemunch-mcp   # the code analysis MCP server

# 2. Set environment variables
export ANTHROPIC_API_KEY=sk-ant-...
export NOTION_API_KEY=secret_...
export GITHUB_TOKEN=ghp_...      # optional, raises rate limit to 5000 req/hr

# 3. Run it
python notionmirror.py https://github.com/you/your-repo \
  --notion-parent-id <your-notion-page-id>
```

**Getting your Notion page ID:** Open any Notion page, click "Share" → "Copy link". The ID is the last segment of the URL (32 hex characters, with or without hyphens).

**Setting up a Notion integration:** Go to [notion.so/my-integrations](https://www.notion.so/my-integrations), create an integration, copy the token, and share your target page with the integration.

---

## CLI reference

```
python notionmirror.py <source> [options]

Arguments:
  source                  GitHub URL or local directory path

Options:
  --notion-parent-id ID   Notion page to create the workspace under (required)
  --sync                  Update an existing workspace instead of creating a new one
  --force                 Re-run even if no code changes detected
  --no-ai-summaries       Skip AI symbol summaries (faster, less rich)
  --dry-run               Phase 1 only — print gathered data as JSON, no Notion writes

Environment variables:
  ANTHROPIC_API_KEY       Required
  NOTION_API_KEY          Required
  GITHUB_TOKEN            Optional (higher GitHub API rate limit)
```

### Examples

```bash
# First run — creates a new workspace
python notionmirror.py https://github.com/you/repo --notion-parent-id abc123

# Sync — updates existing workspace if code changed since last run
python notionmirror.py https://github.com/you/repo --notion-parent-id abc123 --sync

# Local repo
python notionmirror.py ./path/to/local/project --notion-parent-id abc123

# Just see what data gets gathered, no Notion writes
python notionmirror.py https://github.com/you/repo --dry-run

# Faster run, skip AI summaries
python notionmirror.py https://github.com/you/repo --notion-parent-id abc123 --no-ai-summaries
```

---

## Incremental sync

The first run indexes the repo and saves a state file at `~/.notion-code-mirror/<repo>.json`. Subsequent `--sync` runs compare the current git tree SHA against the stored one. If nothing changed, it exits immediately:

```
✅ No changes detected (git_head unchanged). Workspace is up to date.
```

If code changed, Claude updates only the affected pages — existing page IDs are passed in so nothing gets duplicated.

---

## Project structure

```
notion-code-mirror/
├── notionmirror.py      CLI entry point (argparse + async orchestration)
├── phase1_gather.py     jcodemunch-mcp calls → RepoData dataclass
├── phase2_write.py      Claude agent loop + Notion HTTP client
├── mcp_client.py        Async context managers for MCP stdio connections
├── notion_blocks.py     Block builders + md_to_blocks() Markdown converter
├── state.py             Load/save ~/.notion-code-mirror/{repo}.json
├── requirements.txt
└── tests/
    ├── test_notion_blocks.py
    ├── test_state.py
    └── test_phase1.py
```

---

## Design notes

**Why not call Notion MCP from within Claude's tool calls?**
The API Reference database can have 100+ rows. Routing each row through a Claude tool call (Claude emits `tool_use` → Python calls Notion → result goes back to Claude → repeat) burns tokens on every round-trip and adds latency. Instead, Claude creates the database shell and calls `done()` with its ID. Python inserts every row directly via HTTP — zero LLM involvement, no per-row cost.

**Why a minimal Markdown converter instead of a library?**
Notion's block structure doesn't map 1:1 to Markdown — inline formatting, nested lists, and tables need special handling that most converters get wrong. A 100-line purpose-built converter that handles exactly what Claude generates is more reliable than a general-purpose library that produces invalid block trees.

**Why jcodemunch-mcp instead of dumping source files into Claude?**
A typical 200-file Python project is 1–3MB of source. At ~4 bytes/token that's 250K–750K tokens — before you've written a single word of documentation. jcodemunch-mcp pre-processes everything offline: it builds a BM25 index ranked by import-graph centrality, extracts signatures and summaries, and traces dependency edges. What Claude receives is a ~8–12K token structured digest. Same information, a fraction of the cost. That's the entire point of the tool.

---

## Supported languages

Whatever jcodemunch-mcp supports: Python, TypeScript, JavaScript, Go, Rust, Java, C/C++, C#, Ruby, PHP, Swift, Kotlin, and more.

---

## Running the tests

```bash
pytest tests/ -v
```

No external API calls required — tests cover the pure-logic helpers (block builders, state management, data extraction functions).

---

## Built for the Notion MCP Challenge

This project was built for the [DEV.to Notion MCP Hackathon](https://dev.to/challenges/notion). It demonstrates:

- Multi-MCP orchestration (jcodemunch-mcp + Notion API)
- Claude as a thin synthesis layer, not a bulk indexer — Phase 1 costs zero tokens
- Incremental sync via git tree SHA comparison — repeat runs cost nothing if code hasn't changed
- Batch DB population that bypasses Claude entirely for row inserts
- Production-grade rate limiting, chunking, and error handling

---

## License

MIT
