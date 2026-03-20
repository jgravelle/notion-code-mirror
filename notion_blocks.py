"""Pure helpers: Notion block builders and md_to_blocks()."""

import re
from typing import Any


def truncate_rich(text: str, max_len: int = 1990) -> str:
    """Truncate text to fit Notion's 2000-char rich_text limit."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "\u2026"


# ── Inline markdown → Notion rich_text ───────────────────────────────────────

# Matches (in priority order): inline code, bold+italic, bold, italic, plain text.
# Underscore variants (_italic_, __bold__) are intentionally omitted —
# they cause false positives on snake_case and __dunder__ identifiers.
_INLINE_RE = re.compile(
    r"(`[^`]+`"           # `code`
    r"|\*\*\*[^*]+\*\*\*" # ***bold italic***
    r"|\*\*[^*]+\*\*"     # **bold**
    r"|\*[^*\n]+\*"       # *italic*
    r"|[^`*]+)"           # plain text (including underscores)
)


def _parse_inline(text: str) -> list[dict]:
    """Convert inline markdown to a Notion rich_text array with annotations."""
    parts: list[dict] = []
    for m in _INLINE_RE.finditer(str(text)):
        seg = m.group(0)
        if not seg:
            continue

        annotations: dict = {}
        content = seg

        if seg.startswith("`") and seg.endswith("`") and len(seg) > 1:
            content = seg[1:-1]
            annotations["code"] = True
        elif seg.startswith("***") and seg.endswith("***") and len(seg) > 5:
            content = seg[3:-3]
            annotations["bold"] = True
            annotations["italic"] = True
        elif seg.startswith("**") and seg.endswith("**") and len(seg) > 3:
            content = seg[2:-2]
            annotations["bold"] = True
        elif seg.startswith("*") and seg.endswith("*") and len(seg) > 1:
            content = seg[1:-1]
            annotations["italic"] = True

        entry: dict = {"type": "text", "text": {"content": truncate_rich(content)}}
        if annotations:
            entry["annotations"] = annotations
        parts.append(entry)

    return parts or [{"type": "text", "text": {"content": ""}}]


def _rich_text(text: str) -> list[dict]:
    """Backwards-compatible wrapper: parse inline markdown into rich_text."""
    return _parse_inline(text)


# ── Block builders ────────────────────────────────────────────────────────────

def paragraph_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _parse_inline(text)},
    }


def heading_block(text: str, level: int = 2) -> dict:
    t = f"heading_{level}"
    return {
        "object": "block",
        "type": t,
        t: {"rich_text": _parse_inline(text)},
    }


def bullet_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _parse_inline(text)},
    }


def code_block(code: str, language: str = "plain text") -> dict:
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
            "rich_text": [{"type": "text", "text": {"content": truncate_rich(code, 1990)}}],
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
            "rich_text": _parse_inline(text),
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


# ── Table handling ────────────────────────────────────────────────────────────

def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and len(s) > 2


def _is_separator_row(line: str) -> bool:
    """Matches |---|---|---| divider rows."""
    return bool(re.match(r"^\|[\s\-:|]+\|$", line.strip()))


def _parse_table_row(line: str) -> list[str]:
    """Split a markdown table row into cell strings."""
    s = line.strip().strip("|")
    return [cell.strip() for cell in s.split("|")]


def _table_to_blocks(table_lines: list[str]) -> list[dict]:
    """Convert a markdown table to Notion blocks.

    Header row → heading_3 with column names joined by ' | '
    Data rows  → bullet blocks, one per row
    """
    blocks: list[dict] = []
    rows = [r for r in table_lines if not _is_separator_row(r) and _is_table_row(r)]
    if not rows:
        return blocks

    # First row is the header
    headers = _parse_table_row(rows[0])
    blocks.append(heading_block(" | ".join(headers), 3))

    for row_line in rows[1:]:
        cells = _parse_table_row(row_line)
        # Pair each cell with its header: "Header: value  |  Header2: value2"
        pairs = []
        for h, c in zip(headers, cells):
            if c:
                pairs.append(f"{h}: {c}" if h else c)
        if pairs:
            blocks.append(bullet_block("  |  ".join(pairs)))

    return blocks


# ── Main converter ────────────────────────────────────────────────────────────

def md_to_blocks(md: str) -> list[dict]:
    """Markdown → Notion blocks.

    Handles: h1/h2/h3, bullet/numbered lists, fenced code,
    inline bold/italic/code, markdown tables, paragraphs.
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
            blocks.append(code_block("\n".join(code_lines), lang or "plain text"))
            i += 1
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

        # Markdown table — collect all consecutive table rows
        if _is_table_row(stripped):
            table_lines = []
            while i < len(lines) and (_is_table_row(lines[i].strip()) or _is_separator_row(lines[i].strip())):
                table_lines.append(lines[i])
                i += 1
            blocks.extend(_table_to_blocks(table_lines))
            continue

        # Bullets
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
            ns = lines[i].strip()
            if (
                not ns
                or ns.startswith("#")
                or ns.startswith("- ")
                or ns.startswith("* ")
                or ns.startswith("```")
                or _is_table_row(ns)
                or ns in ("---", "***", "___")
                or re.match(r"^\d+\.\s+", ns)
            ):
                break
            para_lines.append(ns)
            i += 1

        blocks.append(paragraph_block(" ".join(para_lines)))

    return blocks


def chunk_blocks(blocks: list[dict], size: int = 100) -> list[list[dict]]:
    """Split blocks into chunks of at most `size` (Notion's 100-block limit)."""
    return [blocks[i : i + size] for i in range(0, len(blocks), size)]
