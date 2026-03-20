"""Tests for notion_blocks.py helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from notion_blocks import (
    chunk_blocks,
    code_block,
    divider_block,
    heading_block,
    md_to_blocks,
    paragraph_block,
    truncate_rich,
    bullet_block,
)


# ── truncate_rich ─────────────────────────────────────────────────────────────

def test_truncate_rich_short():
    assert truncate_rich("hello", 1990) == "hello"


def test_truncate_rich_exact():
    text = "a" * 1990
    assert truncate_rich(text, 1990) == text


def test_truncate_rich_over():
    text = "a" * 2000
    result = truncate_rich(text, 1990)
    assert len(result) == 1990
    assert result.endswith("\u2026")


def test_truncate_rich_empty():
    assert truncate_rich("") == ""


# ── Block builders ────────────────────────────────────────────────────────────

def test_paragraph_block_structure():
    b = paragraph_block("Hello world")
    assert b["type"] == "paragraph"
    assert b["paragraph"]["rich_text"][0]["text"]["content"] == "Hello world"


def test_heading_block_level1():
    b = heading_block("My Title", level=1)
    assert b["type"] == "heading_1"
    assert "heading_1" in b


def test_heading_block_level2():
    b = heading_block("Section", level=2)
    assert b["type"] == "heading_2"


def test_heading_block_level3():
    b = heading_block("Sub", level=3)
    assert b["type"] == "heading_3"


def test_bullet_block():
    b = bullet_block("list item")
    assert b["type"] == "bulleted_list_item"
    assert b["bulleted_list_item"]["rich_text"][0]["text"]["content"] == "list item"


def test_code_block_python():
    b = code_block("print('hi')", "python")
    assert b["type"] == "code"
    assert b["code"]["language"] == "python"


def test_code_block_unknown_lang_fallback():
    b = code_block("some code", "elixir")
    assert b["code"]["language"] == "plain text"


def test_code_block_long_truncated():
    code = "x" * 3000
    b = code_block(code)
    content = b["code"]["rich_text"][0]["text"]["content"]
    assert len(content) <= 1990


def test_divider_block():
    b = divider_block()
    assert b["type"] == "divider"


# ── chunk_blocks ─────────────────────────────────────────────────────────────

def test_chunk_blocks_empty():
    assert chunk_blocks([]) == []


def test_chunk_blocks_under_limit():
    blocks = [paragraph_block(f"p{i}") for i in range(50)]
    chunks = chunk_blocks(blocks)
    assert len(chunks) == 1
    assert len(chunks[0]) == 50


def test_chunk_blocks_exactly_100():
    blocks = [paragraph_block(f"p{i}") for i in range(100)]
    chunks = chunk_blocks(blocks)
    assert len(chunks) == 1


def test_chunk_blocks_101():
    blocks = [paragraph_block(f"p{i}") for i in range(101)]
    chunks = chunk_blocks(blocks, size=100)
    assert len(chunks) == 2
    assert len(chunks[0]) == 100
    assert len(chunks[1]) == 1


def test_chunk_blocks_250():
    blocks = [paragraph_block(f"p{i}") for i in range(250)]
    chunks = chunk_blocks(blocks, size=100)
    assert len(chunks) == 3
    assert all(len(c) <= 100 for c in chunks)


# ── md_to_blocks ─────────────────────────────────────────────────────────────

def test_md_heading_1():
    blocks = md_to_blocks("# Title")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "heading_1"


def test_md_heading_2():
    blocks = md_to_blocks("## Section")
    assert blocks[0]["type"] == "heading_2"


def test_md_heading_3():
    blocks = md_to_blocks("### Sub")
    assert blocks[0]["type"] == "heading_3"


def test_md_bullet_dash():
    blocks = md_to_blocks("- item one\n- item two")
    assert len(blocks) == 2
    assert all(b["type"] == "bulleted_list_item" for b in blocks)


def test_md_bullet_star():
    blocks = md_to_blocks("* foo\n* bar")
    assert all(b["type"] == "bulleted_list_item" for b in blocks)


def test_md_numbered_list():
    blocks = md_to_blocks("1. first\n2. second\n3. third")
    assert len(blocks) == 3
    assert all(b["type"] == "bulleted_list_item" for b in blocks)


def test_md_code_fence():
    md = "```python\ndef hello():\n    pass\n```"
    blocks = md_to_blocks(md)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == "python"


def test_md_code_fence_no_lang():
    md = "```\nsome code\n```"
    blocks = md_to_blocks(md)
    assert blocks[0]["code"]["language"] == "plain text"


def test_md_paragraph():
    blocks = md_to_blocks("This is a paragraph.")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "paragraph"


def test_md_paragraph_multiline():
    md = "Line one\nline two\nline three"
    blocks = md_to_blocks(md)
    assert len(blocks) == 1
    content = blocks[0]["paragraph"]["rich_text"][0]["text"]["content"]
    assert "Line one" in content
    assert "line three" in content


def test_md_divider():
    blocks = md_to_blocks("---")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "divider"


def test_md_mixed():
    md = """# Overview

This is a description.

## Features
- Fast indexing
- Incremental updates

```python
import os
```

---
"""
    blocks = md_to_blocks(md)
    types = [b["type"] for b in blocks]
    assert "heading_1" in types
    assert "heading_2" in types
    assert "paragraph" in types
    assert "bulleted_list_item" in types
    assert "code" in types
    assert "divider" in types


def test_md_empty_string():
    assert md_to_blocks("") == []


def test_md_only_blank_lines():
    assert md_to_blocks("\n\n\n") == []
