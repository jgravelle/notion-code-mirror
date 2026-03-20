"""Phase 2: Claude agent loop → writes a living Notion workspace.

Architecture
───────────
1. Build a structured prompt containing all RepoData.
2. Give Claude 5 lightweight tools (wrapping Notion MCP).
3. Run the agent loop until Claude calls `done`.
4. Batch-populate the API Reference database directly (bypasses agent loop
   to avoid 200+ round-trips through Claude).

Notion transport (in priority order):
  1. Notion MCP  — @notionhq/notion-mcp-server via stdio (tools discovered dynamically)
  2. Direct HTTP — NOTION_API_KEY → api.notion.com (automatic fallback)
"""

import asyncio
import json
import os
import sys
from typing import Any, Optional

import anthropic
import httpx

from mcp_client import notion_mcp_session, MCPSession
from notion_blocks import chunk_blocks, md_to_blocks, truncate_rich
from phase1_gather import RepoData

# ── Notion MCP tool discovery ─────────────────────────────────────────────────

# Ordered candidate lists: first exact match wins, then fuzzy fallback below.
_TOOL_CANDIDATES: dict[str, list[str]] = {
    "create_page":     ["API-post-page", "create-a-page", "notion_create_page",
                        "createPage", "post-page", "pages-create"],
    "update_page":     ["API-patch-page-page_id", "update-page", "notion_update_page",
                        "updatePage", "patch-page", "pages-update"],
    "create_database": ["API-post-database", "create-a-database", "notion_create_database",
                        "createDatabase", "post-database", "databases-create"],
    "append_blocks":   ["API-patch-block-block_id-children", "append-block-children",
                        "notion_append_block_children", "appendBlockChildren",
                        "patch-block-children", "blocks-children-append"],
}

_FUZZY_KEYWORDS: dict[str, tuple[set[str], set[str]]] = {
    # role: (must contain one of these, must contain one of these)
    "create_page":     ({"page"}, {"create", "post", "add", "new"}),
    "update_page":     ({"page"}, {"update", "patch", "edit", "modify"}),
    "create_database": ({"database", "db"}, {"create", "post", "add", "new"}),
    "append_blocks":   ({"block", "children", "child"}, {"append", "add", "patch", "post"}),
}


def _discover_notion_tools(tool_names: list[str]) -> dict[str, str]:
    """Return {role: actual_tool_name} by scanning available MCP tools."""
    tool_set = set(tool_names)
    mapping: dict[str, str] = {}

    for role, candidates in _TOOL_CANDIDATES.items():
        # Exact match first
        for c in candidates:
            if c in tool_set:
                mapping[role] = c
                break
        if role in mapping:
            continue

        # Fuzzy: both keyword sets must match
        must_a, must_b = _FUZZY_KEYWORDS[role]
        for tool in tool_names:
            tl = tool.lower()
            if any(k in tl for k in must_a) and any(k in tl for k in must_b):
                mapping[role] = tool
                break

    return mapping


# ── Notion MCP client ─────────────────────────────────────────────────────────

class NotionMCPClient:
    """Calls Notion via the MCP stdio server. Raises on any failure."""

    def __init__(self, session: MCPSession):
        self._s = session
        self._tools: dict[str, str] = {}

    async def setup(self) -> bool:
        """Discover tools. Returns True if all required roles are mapped."""
        try:
            names = await self._s.list_tools()
            self._tools = _discover_notion_tools(names)
            mapped = list(self._tools.keys())
            print(f"    Notion MCP: {len(names)} tools available, mapped: {mapped}")
            required = {"create_page", "create_database", "append_blocks"}
            return required.issubset(self._tools)
        except Exception as e:
            print(f"    Notion MCP tool discovery failed: {e}")
            return False

    def _page_args(
        self,
        parent_id: str,
        parent_type: str,
        title: str,
        blocks: list[dict],
        icon_emoji: str,
    ) -> dict:
        parent_key = "database_id" if parent_type == "database_id" else "page_id"
        return {
            "parent": {parent_key: parent_id},
            "icon": {"type": "emoji", "emoji": icon_emoji},
            "properties": {
                "title": {
                    "title": [{"type": "text", "text": {"content": truncate_rich(title, 250)}}]
                }
            },
            "children": blocks[:100],
        }

    async def create_page(
        self,
        parent_id: str,
        parent_type: str,
        title: str,
        blocks: list[dict],
        icon_emoji: str = "📄",
    ) -> str:
        tool = self._tools["create_page"]
        args = self._page_args(parent_id, parent_type, title, blocks, icon_emoji)
        result = await self._s.call(tool, **args)
        page_id = result.get("id") or result.get("page_id", "")
        if not page_id:
            raise RuntimeError(f"create_page returned no id: {result}")

        # Append remaining blocks
        for chunk in chunk_blocks(blocks[100:], 100):
            await asyncio.sleep(0.4)
            await self.append_blocks(page_id, chunk)

        return page_id

    async def append_blocks(self, block_id: str, blocks: list[dict]) -> None:
        tool = self._tools["append_blocks"]
        for chunk in chunk_blocks(blocks, 100):
            await self._s.call(tool, block_id=block_id, children=chunk)
            await asyncio.sleep(0.4)

    async def create_database(
        self,
        parent_page_id: str,
        title: str,
        properties: dict,
        icon_emoji: str = "🗄️",
    ) -> str:
        tool = self._tools["create_database"]
        result = await self._s.call(
            tool,
            parent={"page_id": parent_page_id},
            icon={"type": "emoji", "emoji": icon_emoji},
            title=[{"type": "text", "text": {"content": truncate_rich(title, 250)}}],
            properties=properties,
        )
        db_id = result.get("id") or result.get("database_id", "")
        if not db_id:
            raise RuntimeError(f"create_database returned no id: {result}")
        return db_id

    async def update_page(
        self,
        page_id: str,
        title: Optional[str] = None,
        blocks: Optional[list[dict]] = None,
    ) -> None:
        if title and "update_page" in self._tools:
            tool = self._tools["update_page"]
            await self._s.call(
                tool,
                page_id=page_id,
                properties={
                    "title": {
                        "title": [{"type": "text", "text": {"content": truncate_rich(title, 250)}}]
                    }
                },
            )
        if blocks:
            await self.append_blocks(page_id, blocks)

    async def add_db_row(
        self,
        database_id: str,
        name: str,
        kind: str,
        file: str,
        signature: str,
        summary: str,
    ) -> str:
        tool = self._tools["create_page"]
        result = await self._s.call(
            tool,
            parent={"database_id": database_id},
            properties={
                "Name": {
                    "title": [{"type": "text", "text": {"content": truncate_rich(name, 250)}}]
                },
                "Kind":      {"select": {"name": truncate_rich(kind, 100)}},
                "File":      {"rich_text": [{"type": "text", "text": {"content": truncate_rich(file, 1990)}}]},
                "Signature": {"rich_text": [{"type": "text", "text": {"content": truncate_rich(signature, 1990)}}]},
                "Summary":   {"rich_text": [{"type": "text", "text": {"content": truncate_rich(summary, 1990)}}]},
            },
        )
        return result.get("id", "")


# ── Direct HTTP fallback ───────────────────────────────────────────────────────

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION  = "2022-06-28"


class NotionHTTPClient:
    """Thin async Notion REST API client (fallback when MCP is unavailable)."""

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

    async def create_page(self, parent_id, parent_type, title, blocks, icon_emoji="📄") -> str:
        parent_key = "database_id" if parent_type == "database_id" else "page_id"
        result = await self._post("/pages", {
            "parent": {parent_key: parent_id},
            "icon": {"type": "emoji", "emoji": icon_emoji},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": truncate_rich(title, 250)}}]}
            },
            "children": blocks[:100],
        })
        page_id = result["id"]
        for chunk in chunk_blocks(blocks[100:], 100):
            await self._patch(f"/blocks/{page_id}/children", {"children": chunk})
            await asyncio.sleep(0.4)
        return page_id

    async def append_blocks(self, block_id: str, blocks: list[dict]) -> None:
        for chunk in chunk_blocks(blocks, 100):
            await self._patch(f"/blocks/{block_id}/children", {"children": chunk})
            await asyncio.sleep(0.4)

    async def create_database(self, parent_page_id, title, properties, icon_emoji="🗄️") -> str:
        result = await self._post("/databases", {
            "parent": {"page_id": parent_page_id},
            "icon": {"type": "emoji", "emoji": icon_emoji},
            "title": [{"type": "text", "text": {"content": truncate_rich(title, 250)}}],
            "properties": properties,
        })
        return result["id"]

    async def update_page(self, page_id, title=None, blocks=None) -> None:
        if title:
            await self._patch(f"/pages/{page_id}", {
                "properties": {
                    "title": {"title": [{"type": "text", "text": {"content": truncate_rich(title, 250)}}]}
                }
            })
        if blocks:
            await self.append_blocks(page_id, blocks)

    async def add_db_row(self, database_id, name, kind, file, signature, summary) -> str:
        result = await self._post("/pages", {
            "parent": {"database_id": database_id},
            "properties": {
                "Name":      {"title": [{"type": "text", "text": {"content": truncate_rich(name, 250)}}]},
                "Kind":      {"select": {"name": truncate_rich(kind, 100)}},
                "File":      {"rich_text": [{"type": "text", "text": {"content": truncate_rich(file, 1990)}}]},
                "Signature": {"rich_text": [{"type": "text", "text": {"content": truncate_rich(signature, 1990)}}]},
                "Summary":   {"rich_text": [{"type": "text", "text": {"content": truncate_rich(summary, 1990)}}]},
            },
        })
        return result["id"]


# ── Unified Notion client (MCP preferred, HTTP fallback) ──────────────────────

class NotionClient:
    """MCP-first Notion client with transparent HTTP fallback.

    Preference order:
      1. Notion MCP via stdio (NotionMCPClient)
      2. Direct HTTP to api.notion.com (NotionHTTPClient)
    """

    def __init__(self, api_key: str, mcp_session: Optional[MCPSession] = None):
        self._api_key = api_key
        self._mcp_session = mcp_session
        self._mcp: Optional[NotionMCPClient] = None
        self._http: Optional[NotionHTTPClient] = None
        self.using_mcp = False

    async def __aenter__(self):
        if self._mcp_session:
            candidate = NotionMCPClient(self._mcp_session)
            if await candidate.setup():
                self._mcp = candidate
                self.using_mcp = True
                print("    Transport: Notion MCP ✓")
            else:
                print("    Transport: Notion MCP unavailable — falling back to HTTP")

        if not self.using_mcp:
            self._http = NotionHTTPClient(self._api_key)
            await self._http.__aenter__()
            print("    Transport: Notion HTTP")

        return self

    async def __aexit__(self, *args):
        if self._http:
            await self._http.__aexit__(*args)

    def _backend(self):
        return self._mcp if self.using_mcp else self._http

    async def create_page(self, parent_id, parent_type, title, blocks, icon_emoji="📄") -> str:
        return await self._backend().create_page(parent_id, parent_type, title, blocks, icon_emoji)

    async def append_blocks(self, block_id, blocks) -> None:
        return await self._backend().append_blocks(block_id, blocks)

    async def create_database(self, parent_page_id, title, properties, icon_emoji="🗄️") -> str:
        return await self._backend().create_database(parent_page_id, title, properties, icon_emoji)

    async def update_page(self, page_id, title=None, blocks=None) -> None:
        return await self._backend().update_page(page_id, title=title, blocks=blocks)

    async def add_db_row(self, database_id, name, kind, file, signature, summary) -> str:
        return await self._backend().add_db_row(database_id, name, kind, file, signature, summary)


# ── Tool definitions for Claude ───────────────────────────────────────────────

CLAUDE_TOOLS = [
    {
        "name": "notion_create_page",
        "description": (
            "Create a Notion page under a parent page or database. "
            "content_md accepts Markdown which will be converted to Notion blocks. "
            "Returns the new page's ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_id":   {"type": "string", "description": "ID of parent page or database"},
                "parent_type": {"type": "string", "enum": ["page_id", "database_id"]},
                "title":       {"type": "string"},
                "content_md":  {"type": "string", "description": "Page content in Markdown"},
                "icon_emoji":  {"type": "string", "default": "📄"},
            },
            "required": ["parent_id", "parent_type", "title", "content_md"],
        },
    },
    {
        "name": "notion_create_database",
        "description": (
            "Create the API Reference database under a parent page. "
            "Returns the database ID. Python will populate the rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_page_id": {"type": "string"},
                "title":          {"type": "string"},
                "icon_emoji":     {"type": "string", "default": "🗄️"},
            },
            "required": ["parent_page_id", "title"],
        },
    },
    {
        "name": "notion_update_page",
        "description": "Update an existing page title and/or append content. Sync mode only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id":    {"type": "string"},
                "title":      {"type": "string"},
                "content_md": {"type": "string"},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "done",
        "description": (
            "Signal workspace creation is complete. Call exactly once after all pages are created. "
            "Python will then batch-populate the API Reference database rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "root_page_id":         {"type": "string"},
                "overview_page_id":     {"type": "string"},
                "architecture_page_id": {"type": "string"},
                "api_db_id":            {"type": "string"},
                "module_page_ids":      {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["root_page_id", "overview_page_id", "architecture_page_id", "api_db_id"],
        },
    },
]


# ── Prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(data: RepoData, parent_id: str, state: Optional[dict], sync: bool) -> str:
    repo_name = data.repo_key.split("/")[-1] if "/" in data.repo_key else data.repo_key
    lang_str = ", ".join(
        f"{lang} ({pct}%)"
        for lang, pct in sorted(data.languages.items(), key=lambda x: -x[1])
    ) if data.languages else "unknown"

    symbol_preview   = json.dumps(data.all_symbols[:20], indent=2)
    classes_preview  = json.dumps(data.classes[:10], indent=2)
    dirs_preview     = json.dumps(data.top_dirs, indent=2)

    dep_summary = "\n".join(
        f"- `{f}`: {g.get('node_count', 0)} nodes, {g.get('edge_count', 0)} edges"
        for f, g in list(data.dep_graphs.items())[:3] if g
    ) or "No dependency data available."

    ctx_preview = (data.context_bundle or "")[:2000]

    sync_block = ""
    if sync and state:
        sync_block = f"""
## SYNC MODE — existing page IDs
Use `notion_update_page` for these pages instead of creating new ones:
- Root:         {state.get('notion_root_page_id', 'N/A')}
- Overview:     {state.get('overview_page_id', 'N/A')}
- Architecture: {state.get('architecture_page_id', 'N/A')}
- API DB:       {state.get('api_db_id', 'N/A')}
- Modules:      {json.dumps(state.get('module_page_ids', {}), indent=2)}

Only CREATE new pages for modules not already in the module_page_ids map.
"""

    return f"""You are writing a living code documentation workspace in Notion for **{data.repo_key}**.

## Repository summary
- Name: {repo_name}
- Files: {data.file_count} | Symbols: {data.symbol_count}
- Languages: {lang_str}
- Entry points: {', '.join(f'`{f}`' for f in data.entry_files) or 'none identified'}
- Top directories: {', '.join(f'`{d}`' for d in data.top_dirs[:8])}

## Top symbols (by centrality)
```json
{symbol_preview}
```

## Classes
```json
{classes_preview}
```

## Top-level directories
```json
{dirs_preview}
```

## Dependency graph summary
{dep_summary}

## Code context (top symbols)
{ctx_preview}
{sync_block}
## Your task
Build this workspace under parent page `{parent_id}`:

```
🪞 {repo_name} — CodeMirror          ← root page
  📊 Overview                         ← stats, language breakdown, symbol inventory
  🏗️ Architecture                     ← YOUR PROSE: modules, data flow, entry points
  🗄️ API Reference                    ← database (create it; Python populates rows)
  📁 Modules/{{dirname}}              ← one page per top directory (up to 8)
```

### Rules
1. Create ROOT page "🪞 {repo_name} — CodeMirror" under parent `{parent_id}`.
2. Create **Overview** under root — file count, symbol count, language breakdown, top symbols list.
3. Create **Architecture** under root — write 3–5 paragraphs of real prose about module structure, data flow, and entry points. Use the dependency graph and context above.
4. Create **API Reference** database under root via `notion_create_database`. Python populates rows — do NOT add symbols yourself.
5. Create one **module page** per top directory (up to 8). Describe each module's purpose in a sentence or two.
6. Call `done` with all IDs once finished.
"""


# ── Agent loop ─────────────────────────────────────────────────────────────────

async def run_phase2(
    data: RepoData,
    parent_id: str,
    state: Optional[dict],
    sync: bool,
) -> dict:
    """Run the Claude agent loop and return the saved state dict."""
    api_key = os.environ.get("NOTION_API_KEY", "")
    if not api_key:
        raise RuntimeError("NOTION_API_KEY is not set")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    client = anthropic.AsyncAnthropic(api_key=anthropic_key)

    # Try to open Notion MCP session; fall back gracefully if npx/package unavailable
    mcp_session: Optional[MCPSession] = None
    _mcp_ctx = None
    try:
        _mcp_ctx = notion_mcp_session()
        mcp_session = await _mcp_ctx.__aenter__()
        print("  Notion MCP session opened")
    except Exception as e:
        print(f"  Notion MCP unavailable ({e}), will use HTTP")
        mcp_session = None
        _mcp_ctx = None

    try:
        async with NotionClient(api_key, mcp_session) as notion:
            done_result: Optional[dict] = None

            async def dispatch(tool_name: str, tool_input: dict) -> dict:
                nonlocal done_result
                await asyncio.sleep(0.4)

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
                    db_id = await notion.create_database(
                        parent_page_id=tool_input["parent_page_id"],
                        title=tool_input["title"],
                        properties={
                            "Name":      {"title": {}},
                            "Kind":      {"select": {"options": [
                                {"name": "function",  "color": "blue"},
                                {"name": "class",     "color": "green"},
                                {"name": "method",    "color": "purple"},
                                {"name": "variable",  "color": "yellow"},
                                {"name": "constant",  "color": "orange"},
                                {"name": "type",      "color": "pink"},
                            ]}},
                            "File":      {"rich_text": {}},
                            "Signature": {"rich_text": {}},
                            "Summary":   {"rich_text": {}},
                        },
                        icon_emoji=tool_input.get("icon_emoji", "🗄️"),
                    )
                    print(f"    ✓ Created database: {tool_input['title']!r} → {db_id}")
                    return {"database_id": db_id, "success": True}

                elif tool_name == "notion_update_page":
                    blocks = md_to_blocks(tool_input["content_md"]) if tool_input.get("content_md") else None
                    await notion.update_page(
                        page_id=tool_input["page_id"],
                        title=tool_input.get("title"),
                        blocks=blocks,
                    )
                    print(f"    ✓ Updated page: {tool_input['page_id']}")
                    return {"success": True}

                elif tool_name == "done":
                    done_result = tool_input
                    return {"success": True}

                return {"error": f"Unknown tool: {tool_name}"}

            # ── Agent loop ────────────────────────────────────────────────────
            prompt   = _build_prompt(data, parent_id, state, sync)
            messages = [{"role": "user", "content": prompt}]
            print("  Running Claude agent loop...")

            for iteration in range(30):
                response = await client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=8192,
                    tools=CLAUDE_TOOLS,
                    messages=messages,
                )

                tool_calls = [b for b in response.content if b.type == "tool_use"]

                if response.stop_reason == "end_turn" and not tool_calls:
                    print("  Warning: Claude ended without calling `done`.")
                    break

                tool_results = []
                for block in tool_calls:
                    result = await dispatch(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

                messages.append({"role": "assistant", "content": response.content})
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})

                if done_result is not None:
                    break

            if done_result is None:
                raise RuntimeError("Agent loop ended without `done`. Check Claude output.")

            # ── Batch-populate API Reference DB ───────────────────────────────
            api_db_id = done_result.get("api_db_id", "")
            if api_db_id and data.all_symbols:
                print(f"  Populating API Reference ({len(data.all_symbols)} symbols via "
                      f"{'MCP' if notion.using_mcp else 'HTTP'})...")
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
                        await asyncio.sleep(0.4)
                    except Exception as e:
                        print(f"    Warning: skipped {sym.get('name', '?')}: {e}")

    finally:
        if _mcp_ctx and mcp_session:
            try:
                await _mcp_ctx.__aexit__(None, None, None)
            except Exception:
                pass

    return {
        "git_head":              data.git_head,
        "notion_root_page_id":   done_result.get("root_page_id", ""),
        "overview_page_id":      done_result.get("overview_page_id", ""),
        "architecture_page_id":  done_result.get("architecture_page_id", ""),
        "api_db_id":             done_result.get("api_db_id", ""),
        "module_page_ids":       done_result.get("module_page_ids", {}),
        "transport":             "mcp" if (mcp_session is not None) else "http",
    }
