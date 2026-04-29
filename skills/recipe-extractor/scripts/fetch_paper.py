#!/usr/bin/env python3
"""
fetch_paper.py — Extract text from paper PDF with table preservation.
Designed for LLM consumption: smart truncation, section filtering,
no redundant data.

Usage (local):
    python3 fetch_paper.py --file paper.pdf
    python3 fetch_paper.py --file paper.pdf --max-chars 80000

Usage (remote):
    python3 fetch_paper.py --url "https://arxiv.org/abs/2403.12345"

Output: JSON with { text, page_count, table_count, chars, truncated }
  - text = filtered running text + tables as readable text
  - NO tables_structured (removed — was redundant, doubled token cost)

Requires: pip install pymupdf
"""

import argparse
import json
import os
import re
import ssl
import sys
import tempfile
import urllib.request

# Default: ~60k chars ≈ ~15k tokens. Leaves room for LLM to generate full v2 JSON.
DEFAULT_MAX_CHARS = 60000

# Sections to strip (waste context, contain no recipe data)
STRIP_SECTIONS = [
    r'references?\s*$',
    r'bibliography\s*$',
    r'acknowledg[e]?ments?\s*$',
    r'supplementary\s+(?:material|information|data)',
    r'appendix\s*$',
    r'author\s+contributions?\s*$',
    r'conflict[s]?\s+of\s+interest',
    r'data\s+availability',
    r'funding\s*$',
    r'orcid',
    r'declaration\s+of\s+(?:competing\s+)?interest',
]

_STRIP_RE = [re.compile(pat, re.IGNORECASE) for pat in STRIP_SECTIONS]


def _make_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        return ctx
    except ssl.SSLError:
        pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _download(url, dest):
    ctx = _make_ssl_context()
    req = urllib.request.Request(url, headers={
        "User-Agent": "TinFlotationAgent/1.0 (mailto:research@example.com)",
        "Accept": "application/pdf,*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            data = resp.read()
            if not data[:5] == b"%PDF-":
                ct = resp.headers.get("Content-Type", "")
                if "html" in ct.lower() or data[:15].lower().startswith(b"<!doctype"):
                    return False
            with open(dest, "wb") as f:
                f.write(data)
            return True
    except Exception:
        return False


def resolve_pdf_urls(url, pdf_url=""):
    candidates = []
    if pdf_url and pdf_url.strip():
        candidates.append(pdf_url.strip())
    m = re.search(r'arxiv\.org/(?:abs|pdf)/(\S+?)(?:\.pdf)?$', url.strip())
    if m:
        candidates.append(f"https://arxiv.org/pdf/{m.group(1)}.pdf")
    if url.strip().lower().endswith(".pdf"):
        candidates.append(url.strip())
    seen = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


# ═══════════════════════════════════════════════════
# Section filtering — strip references, acknowledgments, etc.
# ═══════════════════════════════════════════════════

def _is_strip_heading(line):
    clean = line.strip()
    if not clean or len(clean) > 80:
        return False
    return any(pat.search(clean) for pat in _STRIP_RE)


def filter_sections(text):
    """Remove useless sections. Returns (filtered_text, list_of_stripped_section_names)."""
    lines = text.split('\n')
    result = []
    stripping = False
    stripped = []

    for line in lines:
        if line.startswith("--- Page "):
            stripping = False
            result.append(line)
            continue

        if _is_strip_heading(line):
            stripping = True
            stripped.append(line.strip())
            result.append(f"\n[Section '{line.strip()}' removed]\n")
            continue

        if stripping and re.match(r'^\d+[\.\)]\s+\w', line.strip()):
            if not _is_strip_heading(line):
                stripping = False

        if not stripping:
            result.append(line)

    return '\n'.join(result), stripped


# ═══════════════════════════════════════════════════
# Table extraction
# ═══════════════════════════════════════════════════

def extract_tables(page):
    tables = []
    try:
        for i, table in enumerate(page.find_tables()):
            extracted = table.extract()
            if not extracted or len(extracted) < 2:
                continue
            headers = [str(h).strip() if h else f"col_{j}" for j, h in enumerate(extracted[0])]
            rows = []
            for row in extracted[1:]:
                rd = {}
                for j, cell in enumerate(row):
                    key = headers[j] if j < len(headers) else f"col_{j}"
                    rd[key] = str(cell).strip() if cell else ""
                rows.append(rd)
            tables.append({"table_index": i + 1, "headers": headers, "rows": rows, "row_count": len(rows)})
    except Exception:
        pass
    return tables


def format_tables_text(tables):
    if not tables:
        return ""
    parts = ["\n\n========== EXTRACTED TABLES ==========\n"]
    for t in tables:
        parts.append(f"\n[Table on page {t['page']}, {t['row_count']} rows]")
        parts.append("Headers: " + " | ".join(t["headers"]))
        parts.append("-" * 60)
        for row in t["rows"]:
            parts.append(" | ".join(row.get(h, "") for h in t["headers"]))
        parts.append("")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════
# Smart truncation
# ═══════════════════════════════════════════════════

def smart_truncate(text, tables_text, max_chars):
    """
    Truncate running text while preserving tables in full.
    Strategy: keep head (abstract+methods, 60%) + tail (results+conclusion, 40%).
    """
    if max_chars <= 0:
        return text + tables_text, False

    tables_len = len(tables_text)
    text_budget = max_chars - tables_len

    if text_budget <= 0:
        # Tables alone exceed limit — keep minimal text
        return text[:5000] + "\n\n[... text truncated — tables preserved ...]\n" + tables_text, True

    if len(text) <= text_budget:
        return text + tables_text, False

    # Head = 60% (abstract, experimental, methods)
    # Tail = 40% (results, discussion, conclusion)
    head_n = int(text_budget * 0.6)
    tail_n = text_budget - head_n

    head = text[:head_n]
    tail = text[-tail_n:]

    # Clean break at newline
    hb = head.rfind('\n')
    if hb > head_n * 0.8:
        head = head[:hb]
    tb = tail.find('\n')
    if 0 < tb < tail_n * 0.2:
        tail = tail[tb + 1:]

    skipped = len(text) - len(head) - len(tail)
    combined = (
        head +
        f"\n\n[... {skipped} chars skipped (middle pages) — tables preserved below ...]\n\n" +
        tail +
        tables_text
    )
    return combined, True


# ═══════════════════════════════════════════════════
# Main extraction pipeline
# ═══════════════════════════════════════════════════

def extract_full(path, max_chars=DEFAULT_MAX_CHARS):
    try:
        import fitz
    except ImportError:
        return {"error": "pymupdf not installed. Run: pip3 install pymupdf", "text": ""}

    doc = fitz.open(path)
    pages_text = []
    all_tables = []

    for pn, page in enumerate(doc):
        pages_text.append(f"--- Page {pn + 1} ---\n{page.get_text()}")
        pt = extract_tables(page)
        for t in pt:
            t["page"] = pn + 1
        all_tables.extend(pt)
    doc.close()

    raw_text = "\n".join(pages_text)
    raw_chars = len(raw_text)

    # Phase 1: strip useless sections
    filtered, stripped = filter_sections(raw_text)

    # Phase 2: format tables as text
    tables_text = format_tables_text(all_tables)

    # Phase 3: smart truncation
    combined, truncated = smart_truncate(filtered, tables_text, max_chars)

    return {
        "text": combined,
        "page_count": len(pages_text),
        "table_count": len(all_tables),
        "raw_chars": raw_chars,
        "output_chars": len(combined),
        "stripped_sections": stripped,
        "truncated": truncated,
    }


def output_result(base, extracted):
    if "error" in extracted and extracted.get("error"):
        base.update({"success": False, "error": extracted["error"], "text": ""})
        print(json.dumps(base, ensure_ascii=False))
        sys.exit(1)

    base.update({
        "success": True,
        "chars": extracted["output_chars"],
        "raw_chars": extracted["raw_chars"],
        "text": extracted["text"],
        "page_count": extracted["page_count"],
        "table_count": extracted["table_count"],
        "truncated": extracted["truncated"],
        "stripped_sections": extracted["stripped_sections"],
    })
    # NO tables_structured — removed to save tokens
    print(json.dumps(base, ensure_ascii=False))


def handle_local(file_path, max_chars):
    if not os.path.exists(file_path):
        print(json.dumps({"file": file_path, "success": False,
                          "error": f"File not found: {file_path}", "text": ""}))
        sys.exit(1)
    if not file_path.lower().endswith(".pdf"):
        print(json.dumps({"file": file_path, "success": False,
                          "error": "Not a PDF.", "text": ""}))
        sys.exit(1)
    output_result({"file": file_path}, extract_full(file_path, max_chars))


def handle_remote(url, pdf_url, max_chars):
    candidates = resolve_pdf_urls(url, pdf_url)
    if not candidates:
        print(json.dumps({"url": url, "success": False,
                          "error": "No PDF URL resolved.", "text": ""}))
        sys.exit(1)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        used = ""
        for c in candidates:
            if _download(c, tmp_path):
                used = c
                break
        if not used:
            print(json.dumps({"url": url, "tried": candidates, "success": False,
                              "error": "Could not download PDF.", "text": ""}))
            sys.exit(1)
        output_result({"url": url, "pdf_url": used}, extract_full(tmp_path, max_chars))
    except Exception as e:
        print(json.dumps({"url": url, "success": False, "error": str(e), "text": ""}))
        sys.exit(1)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def main():
    p = argparse.ArgumentParser(description="Extract text + tables from paper PDF (LLM-optimized)")
    p.add_argument("--file", default="")
    p.add_argument("--url", default="")
    p.add_argument("--pdf-url", default="")
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                   help=f"Max output chars (default {DEFAULT_MAX_CHARS}). 0=unlimited.")
    args = p.parse_args()

    if args.file:
        handle_local(args.file, args.max_chars)
    elif args.url:
        handle_remote(args.url, args.pdf_url, args.max_chars)
    else:
        print(json.dumps({"error": "Provide --file or --url", "text": ""}))
        sys.exit(1)


if __name__ == "__main__":
    main()
