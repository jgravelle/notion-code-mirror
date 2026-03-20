"""Pure helpers: Notion block builders and md_to_blocks()."""

import re
from typing import Any


def truncate_rich(text: str, max_len: int = 1990) -> str:
    """Truncate text to fit Notion's 2000-char rich_text limit."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "\u2026"


def _rich_text(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": truncate_rich(str(text))}}]


def paragraph_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(text)},
    }


def heading_block(text: str, level: int = 2) -> dict:
    t = f"heading_{level}"
    return {
        "object": "block",
        "type": t,
        t: {"rich_text": _rich_text(text)},
    }


def bullet_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rich_text(text)},
    }


def code_block(code: str, language: str = "plain text") -> dict:
    # Notion code blocks are limited to 2000 chars
    NOTION_CODE_LANGS = {
        "python", "javascript", "typescript", "go", "rust", "java", "c", "c++",
        "c#", "ruby", "php", "swift", "kotlin", "bash", "shell", "sql", "html",
        "css", "json", "yaml", "toml", "markdown", "plain text",
    }
    lang = language.lower().strip()
    if lang not in NOTION_CODE_LANGS:
        lang = "plain text"
    return {
        "object": "block",
        "type": "code",
        "code": {
            "rich_text": _rich_text(truncate_rich(code, 1990)),
            "language": lang,
        },
    }


def divider_block() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def callout_block(text: str, emoji: str = "💡") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": _rich_text(text),
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


def md_to_blocks(md: str) -> list[dict]:
    """Minimal Markdown → Notion blocks converter.

    Handles: h1/h2/h3, bullet lists, numbered lists,
    fenced code blocks, and paragraphs.
    Does NOT handle: inline bold/italic, tables, nested lists.
    """
    blocks: list[dict] = []
    lines = md.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code = "\n".join(code_lines)
            blocks.append(code_block(code, lang or "plain text"))
            i += 1  # skip closing ```
            continue

        # Headings
        if stripped.startswith("### "):
            blocks.append(heading_block(stripped[4:], 3))
            i += 1
            continue
        if stripped.startswith("## "):
            blocks.append(heading_block(stripped[3:], 2))
            i += 1
            continue
        if stripped.startswith("# "):
            blocks.append(heading_block(stripped[2:], 1))
            i += 1
            continue

        # Bullet list items
        if stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append(bullet_block(stripped[2:]))
            i += 1
            continue

        # Numbered list
        m = re.match(r"^\d+\.\s+(.+)$", stripped)
        if m:
            blocks.append(bullet_block(m.group(1)))
            i += 1
            continue

        # Horizontal rule
        if stripped in ("---", "***", "___"):
            blocks.append(divider_block())
            i += 1
            continue

        # Empty line
        if not stripped:
            i += 1
            continue

        # Paragraph: accumulate consecutive non-special lines
        para_lines = [stripped]
        i += 1
        while i < len(lines):
            next_stripped = lines[i].strip()
            if (
                not next_stripped
                or next_stripped.startswith("#")
                or next_stripped.startswith("- ")
                or next_stripped.startswith("* ")
                or next_stripped.startswith("```")
                or next_stripped in ("---", "***", "___")
                or re.match(r"^\d+\.\s+", next_stripped)
            ):
                break
            para_lines.append(next_stripped)
            i += 1

        para_text = " ".join(para_lines)
        blocks.append(paragraph_block(para_text))

    return blocks


def chunk_blocks(blocks: list[dict], size: int = 100) -> list[list[dict]]:
    """Split blocks into chunks of at most `size` (Notion's 100-block limit)."""
    return [blocks[i : i + size] for i in range(0, len(blocks), size)]
