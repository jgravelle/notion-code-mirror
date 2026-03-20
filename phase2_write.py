"""Phase 2: Claude agent loop → writes a living Notion workspace.

Architecture
───────────
1. Build a structured prompt containing all RepoData.
2. Give Claude 5 lightweight tools (wrapping Notion API).
3. Run the agent loop until Claude calls `done`.
4. Batch-populate the API Reference database directly (bypasses agent loop
   to avoid 200+ round-trips through Claude).

Notion integration supports two transports:
  • Notion MCP (preferred) — @notionhq/notion-mcp-server via stdio
  • Direct HTTP (fallback) — uses NOTION_API_KEY directly
"""

import asyncio
import json
import os
import time
from typing import Any, Optional

import anthropic
import httpx

from notion_blocks import chunk_blocks, md_to_blocks, truncate_rich
from phase1_gather import RepoData

# ── Notion HTTP client (used as fallback and for batch DB population) ─────────

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionHTTPClient:
    """Thin async Notion REST API client."""

    def __init__(self, api_key: str):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(headers=self._headers, timeout=30)
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    async def _post(self, path: str, body: dict) -> dict:
        assert self._client
        r = await self._client.post(f"{NOTION_API_BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()

    async def _patch(self, path: str, body: dict) -> dict:
        assert self._client
        r = await self._client.patch(f"{NOTION_API_BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()

    async def create_page(
        self,
        parent_id: str,
        parent_type: str,
        title: str,
        blocks: list[dict],
        icon_emoji: str = "📄",
    ) -> str:
        """Create a Notion page and return its page_id."""
        if parent_type == "database_id":
            parent = {"database_id": parent_id}
        else:
            parent = {"page_id": parent_id}

        # First 100 blocks go in the create call; rest appended separately
        first_chunk = blocks[:100]
        body: dict = {
            "parent": parent,
            "icon": {"type": "emoji", "emoji": icon_emoji},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": truncate_rich(title, 250)}}]}
            },
            "children": first_chunk,
        }
        result = await self._post("/pages", body)
        page_id = result["id"]

        # Append remaining blocks in chunks of 100
        for chunk in chunk_blocks(blocks[100:], 100):
            await self._patch(f"/blocks/{page_id}/children", {"children": chunk})
            await asyncio.sleep(0.4)  # rate-limit

        return page_id

    async def append_blocks(self, block_id: str, blocks: list[dict]) -> None:
        """Append blocks to an existing page/block, in chunks."""
        for chunk in chunk_blocks(blocks, 100):
            await self._patch(f"/blocks/{block_id}/children", {"children": chunk})
            await asyncio.sleep(0.4)

    async def update_page(
        self,
        page_id: str,
        title: Optional[str] = None,
        blocks: Optional[list[dict]] = None,
    ) -> None:
        """Update page title and/or replace its content blocks."""
        if title:
            await self._patch(
                f"/pages/{page_id}",
                {
                    "properties": {
                        "title": {
                            "title": [{"type": "text", "text": {"content": truncate_rich(title, 250)}}]
                        }
                    }
                },
            )
        if blocks is not None:
            # Archive all existing children first by replacing them
            # Notion doesn't have a "replace children" endpoint; we append
            await self.append_blocks(page_id, blocks)

    async def create_database(
        self,
        parent_page_id: str,
        title: str,
        properties: dict,
        icon_emoji: str = "🗄️",
    ) -> str:
        """Create a Notion database under a page and return its database_id."""
        body = {
            "parent": {"page_id": parent_page_id},
            "icon": {"type": "emoji", "emoji": icon_emoji},
            "title": [{"type": "text", "text": {"content": truncate_rich(title, 250)}}],
            "properties": properties,
        }
        result = await self._post("/databases", body)
        return result["id"]

    async def add_db_row(
        self,
        database_id: str,
        name: str,
        kind: str,
        file: str,
        signature: str,
        summary: str,
    ) -> str:
        """Add a row to the API Reference database and return the page_id."""
        body = {
            "parent": {"database_id": database_id},
            "properties": {
                "Name": {
                    "title": [{"type": "text", "text": {"content": truncate_rich(name, 250)}}]
                },
                "Kind": {"select": {"name": truncate_rich(kind, 100)}},
                "File": {
                    "rich_text": [{"type": "text", "text": {"content": truncate_rich(file, 1990)}}]
                },
                "Signature": {
                    "rich_text": [{"type": "text", "text": {"content": truncate_rich(signature, 1990)}}]
                },
                "Summary": {
                    "rich_text": [{"type": "text", "text": {"content": truncate_rich(summary, 1990)}}]
                },
            },
        }
        result = await self._post("/pages", body)
        return result["id"]


# ── Tool definitions for Claude ───────────────────────────────────────────────

CLAUDE_TOOLS = [
    {
        "name": "notion_create_page",
        "description": (
            "Create a Notion page under a parent page or database. "
            "The content_md parameter accepts Markdown which will be converted to Notion blocks. "
            "Returns the new page's ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_id": {"type": "string", "description": "ID of parent page or database"},
                "parent_type": {
                    "type": "string",
                    "enum": ["page_id", "database_id"],
                    "description": "Whether parent is a page or database",
                },
                "title": {"type": "string", "description": "Page title"},
                "content_md": {
                    "type": "string",
                    "description": "Page content in Markdown format",
                },
                "icon_emoji": {
                    "type": "string",
                    "description": "Emoji icon for the page (e.g. '📊')",
                    "default": "📄",
                },
            },
            "required": ["parent_id", "parent_type", "title", "content_md"],
        },
    },
    {
        "name": "notion_create_database",
        "description": (
            "Create a Notion database under a parent page with predefined columns. "
            "Returns the new database's ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_page_id": {"type": "string", "description": "ID of the parent page"},
                "title": {"type": "string", "description": "Database title"},
                "icon_emoji": {
                    "type": "string",
                    "description": "Emoji icon for the database",
                    "default": "🗄️",
                },
            },
            "required": ["parent_page_id", "title"],
        },
    },
    {
        "name": "notion_update_page",
        "description": (
            "Update an existing Notion page's title and/or append new content. "
            "Used in --sync mode to refresh stale pages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "ID of the page to update"},
                "title": {"type": "string", "description": "New title (optional)"},
                "content_md": {
                    "type": "string",
                    "description": "Markdown content to append (optional)",
                },
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "done",
        "description": (
            "Signal that workspace creation is complete. "
            "Call this exactly once after creating all pages. "
            "The Python code will then batch-populate the API Reference database."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "root_page_id": {"type": "string", "description": "The workspace root page ID"},
                "overview_page_id": {"type": "string", "description": "The Overview page ID"},
                "architecture_page_id": {
                    "type": "string",
                    "description": "The Architecture page ID",
                },
                "api_db_id": {
                    "type": "string",
                    "description": "The API Reference database ID",
                },
                "module_page_ids": {
                    "type": "object",
                    "description": "Mapping of module/dir name → page ID",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["root_page_id", "overview_page_id", "architecture_page_id", "api_db_id"],
        },
    },
]


# ── Prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(
    data: RepoData,
    parent_id: str,
    state: Optional[dict],
    sync: bool,
) -> str:
    """Build the structured user prompt for the Claude agent."""
    repo_name = data.repo_key.split("/")[-1] if "/" in data.repo_key else data.repo_key
    lang_str = ", ".join(
        f"{lang} ({pct}%)" for lang, pct in sorted(
            data.languages.items(), key=lambda x: -x[1]
        )
    ) if data.languages else "unknown"

    symbol_preview = json.dumps(data.all_symbols[:20], indent=2)
    classes_preview = json.dumps(data.classes[:10], indent=2)
    dirs_preview = json.dumps(data.top_dirs, indent=2)

    # Dependency graph summary
    dep_summary_parts = []
    for f, g in list(data.dep_graphs.items())[:3]:
        if g:
            dep_summary_parts.append(
                f"- `{f}`: {g.get('node_count', 0)} nodes, {g.get('edge_count', 0)} edges"
            )
    dep_summary = "\n".join(dep_summary_parts) or "No dependency data available."

    # Context bundle preview
    ctx_preview = (data.context_bundle or "")[:2000]

    sync_instructions = ""
    if sync and state:
        sync_instructions = f"""
## SYNC MODE — Existing page IDs
You are UPDATING an existing workspace. Use `notion_update_page` for these existing pages:
- Root page: {state.get('notion_root_page_id', 'N/A')}
- Overview: {state.get('overview_page_id', 'N/A')}
- Architecture: {state.get('architecture_page_id', 'N/A')}
- API DB: {state.get('api_db_id', 'N/A')}
- Modules: {json.dumps(state.get('module_page_ids', {}), indent=2)}

Only CREATE new pages for modules that are not in the existing module_page_ids map.
"""

    return f"""You are writing a living code documentation workspace in Notion for the repository **{data.repo_key}**.

## Repository Summary
- **Name:** {repo_name}
- **Files:** {data.file_count}
- **Symbols:** {data.symbol_count}
- **Languages:** {lang_str}
- **Entry points:** {', '.join(f'`{f}`' for f in data.entry_files) or 'none identified'}
- **Top directories:** {', '.join(f'`{d}`' for d in data.top_dirs[:8])}

## Top Symbols (by centrality)
```json
{symbol_preview}
```

## Classes
```json
{classes_preview}
```

## Top-level Directories
```json
{dirs_preview}
```

## Dependency Graph Summary
{dep_summary}

## Code Context (top symbols)
{ctx_preview}

{sync_instructions}

## Your Task
Create the following Notion workspace structure under parent page ID `{parent_id}`:

```
🪞 {repo_name} — CodeMirror          ← root page
  📊 Overview                         ← repo stats, language breakdown, symbol inventory
  🏗️ Architecture                     ← YOUR PROSE: modules, data flow, entry points
  🗄️ API Reference                    ← database (you create it; Python populates rows)
  📁 Modules/{{dirname}}              ← one page per top directory
```

### Instructions
1. Call `notion_create_page` to create the ROOT page titled "🪞 {repo_name} — CodeMirror" under parent `{parent_id}`.
2. Create the **Overview** page under root. Include: file count, symbol count, language breakdown, top directories, top symbols list.
3. Create the **Architecture** page under root. Write 3-5 paragraphs of real prose describing the codebase structure, module relationships, entry points, and data flow. Use the dependency graph and context bundle data above.
4. Create the **API Reference** database under root using `notion_create_database`. Name it "API Reference".
5. Create one **Module page** per top directory (up to 8 dirs). Each page should list the files in that directory and describe its purpose.
6. Call `done` with all the page/database IDs you created.

### Important rules
- Write REAL prose for Architecture and module descriptions — not just lists of files.
- Keep each page focused; the API Reference database will be populated automatically by Python (don't add individual symbols yourself).
- Use appropriate emoji icons for each page.
- Call `done` exactly once when finished.
"""


# ── Agent loop ─────────────────────────────────────────────────────────────────

async def run_phase2(
    data: RepoData,
    parent_id: str,
    state: Optional[dict],
    sync: bool,
) -> dict:
    """Run the Claude agent loop to write the Notion workspace.

    Returns a state dict with all page/database IDs.
    """
    api_key = os.environ.get("NOTION_API_KEY", "")
    if not api_key:
        raise RuntimeError("NOTION_API_KEY environment variable is not set")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")

    client = anthropic.AsyncAnthropic(api_key=anthropic_key)

    async with NotionHTTPClient(api_key) as notion:
        done_result: Optional[dict] = None

        async def dispatch(tool_name: str, tool_input: dict) -> dict:
            """Dispatch a Claude tool call to the appropriate Notion operation."""
            nonlocal done_result

            await asyncio.sleep(0.4)  # Rate-limit Notion calls

            if tool_name == "notion_create_page":
                blocks = md_to_blocks(tool_input.get("content_md", ""))
                page_id = await notion.create_page(
                    parent_id=tool_input["parent_id"],
                    parent_type=tool_input.get("parent_type", "page_id"),
                    title=tool_input["title"],
                    blocks=blocks,
                    icon_emoji=tool_input.get("icon_emoji", "📄"),
                )
                print(f"    ✓ Created page: {tool_input['title']!r} → {page_id}")
                return {"page_id": page_id, "success": True}

            elif tool_name == "notion_create_database":
                db_properties = {
                    "Name": {"title": {}},
                    "Kind": {"select": {"options": [
                        {"name": "function", "color": "blue"},
                        {"name": "class", "color": "green"},
                        {"name": "method", "color": "purple"},
                        {"name": "variable", "color": "yellow"},
                        {"name": "constant", "color": "orange"},
                        {"name": "type", "color": "pink"},
                    ]}},
                    "File": {"rich_text": {}},
                    "Signature": {"rich_text": {}},
                    "Summary": {"rich_text": {}},
                }
                db_id = await notion.create_database(
                    parent_page_id=tool_input["parent_page_id"],
                    title=tool_input["title"],
                    properties=db_properties,
                    icon_emoji=tool_input.get("icon_emoji", "🗄️"),
                )
                print(f"    ✓ Created database: {tool_input['title']!r} → {db_id}")
                return {"database_id": db_id, "success": True}

            elif tool_name == "notion_update_page":
                blocks = md_to_blocks(tool_input.get("content_md", "")) if tool_input.get("content_md") else None
                await notion.update_page(
                    page_id=tool_input["page_id"],
                    title=tool_input.get("title"),
                    blocks=blocks,
                )
                print(f"    ✓ Updated page: {tool_input['page_id']}")
                return {"success": True}

            elif tool_name == "done":
                done_result = tool_input
                return {"success": True, "message": "Workspace creation complete!"}

            else:
                return {"error": f"Unknown tool: {tool_name}"}

        # ── Build initial prompt ──────────────────────────────────────────────
        prompt = _build_prompt(data, parent_id, state, sync)
        messages = [{"role": "user", "content": prompt}]

        print("  Running Claude agent loop...")
        iterations = 0
        max_iterations = 30  # safety cap

        while iterations < max_iterations and done_result is None:
            iterations += 1

            response = await client.messages.create(
                model="claude-opus-4-6",
                max_tokens=8192,
                tools=CLAUDE_TOOLS,
                messages=messages,
            )

            # Collect tool calls from the response
            tool_calls = [b for b in response.content if b.type == "tool_use"]

            if response.stop_reason == "end_turn" and not tool_calls:
                print("  Warning: Claude finished without calling `done`.")
                break

            # Execute all tool calls and collect results
            tool_results = []
            for block in tool_calls:
                result = await dispatch(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

            # Extend conversation
            messages.append({"role": "assistant", "content": response.content})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if done_result is not None:
                break

        if done_result is None:
            raise RuntimeError("Agent loop ended without calling `done`. Check Claude output.")

        # ── Batch-populate API Reference database ─────────────────────────────
        api_db_id = done_result.get("api_db_id", "")
        if api_db_id and data.all_symbols:
            print(f"  Populating API Reference database ({len(data.all_symbols)} symbols)...")
            for sym in data.all_symbols:
                try:
                    await notion.add_db_row(
                        database_id=api_db_id,
                        name=sym.get("name", ""),
                        kind=sym.get("kind", "function"),
                        file=sym.get("file", ""),
                        signature=sym.get("signature", ""),
                        summary=sym.get("summary", ""),
                    )
                    await asyncio.sleep(0.4)  # Rate-limit
                except Exception as e:
                    # Don't fail the whole run on a single bad row
                    print(f"    Warning: failed to add row for {sym.get('name', '?')}: {e}")

    return {
        "git_head": data.git_head,
        "notion_root_page_id": done_result.get("root_page_id", ""),
        "overview_page_id": done_result.get("overview_page_id", ""),
        "architecture_page_id": done_result.get("architecture_page_id", ""),
        "api_db_id": done_result.get("api_db_id", ""),
        "module_page_ids": done_result.get("module_page_ids", {}),
    }
