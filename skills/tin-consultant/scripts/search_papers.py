#!/usr/bin/env python3
"""
search_papers.py — Multi-source open access paper search
Searches: arXiv, Semantic Scholar, CrossRef, OpenAlex, CORE
Deduplicates and returns top results sorted by citations.

Usage:
    python3 search_papers.py --query "cassiterite flotation" --limit 10
"""

import argparse
import json
import ssl
import sys
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET


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


def _fetch(url: str, timeout: int = 30) -> str:
    ctx = _make_ssl_context()
    req = urllib.request.Request(url, headers={
        "User-Agent": "TinFlotationAgent/1.0 (mailto:research@example.com)"
    })
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode()


# ─── arXiv ───────────────────────────────────────────────────────────

def search_arxiv(query: str, limit: int = 10) -> list[dict]:
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": limit,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    url = f"https://export.arxiv.org/api/query?{urllib.parse.urlencode(params)}"
    try:
        xml_data = _fetch(url)
    except Exception as e:
        print(f"[WARN] arXiv failed: {e}", file=sys.stderr)
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_data)
    papers = []
    for entry in root.findall("atom:entry", ns):
        title = entry.find("atom:title", ns)
        abstract = entry.find("atom:summary", ns)
        authors = entry.findall("atom:author/atom:name", ns)
        published = entry.find("atom:published", ns)
        link = entry.find("atom:id", ns)
        arxiv_url = link.text.strip() if link is not None else ""
        pdf_url = arxiv_url.replace("/abs/", "/pdf/") + ".pdf" if "/abs/" in arxiv_url else ""

        papers.append({
            "title": (title.text or "").strip().replace("\n", " "),
            "authors": [a.text.strip() for a in authors],
            "year": int(published.text[:4]) if published is not None else 0,
            "abstract": (abstract.text or "").strip().replace("\n", " "),
            "url": arxiv_url,
            "pdf_url": pdf_url,
            "doi": "",
            "journal": "arXiv",
            "citations": 0,
            "source": "arxiv",
        })
    return papers


# ─── Semantic Scholar ────────────────────────────────────────────────

def search_semantic_scholar(query: str, limit: int = 10) -> list[dict]:
    params = {
        "query": query,
        "limit": min(limit, 100),
        "fields": "title,authors,year,abstract,citationCount,externalIds,journal,url,openAccessPdf",
        "fieldsOfStudy": "Materials Science,Chemistry,Engineering",
    }
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{urllib.parse.urlencode(params)}"
    try:
        data = json.loads(_fetch(url))
    except Exception as e:
        print(f"[WARN] Semantic Scholar failed: {e}", file=sys.stderr)
        return []

    papers = []
    for p in data.get("data", []):
        ext_ids = p.get("externalIds") or {}
        doi = ext_ids.get("DOI", "")
        arxiv_id = ext_ids.get("ArXiv", "")
        oa_pdf = (p.get("openAccessPdf") or {}).get("url", "")

        paper_url = p.get("url", "")
        pdf_url = oa_pdf
        if arxiv_id:
            paper_url = f"https://arxiv.org/abs/{arxiv_id}"
            if not pdf_url:
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        elif doi and not paper_url:
            paper_url = f"https://doi.org/{doi}"

        papers.append({
            "title": p.get("title", ""),
            "authors": [a.get("name", "") for a in (p.get("authors") or [])],
            "year": p.get("year") or 0,
            "abstract": (p.get("abstract") or ""),
            "url": paper_url,
            "pdf_url": pdf_url,
            "doi": doi,
            "journal": (p.get("journal") or {}).get("name", ""),
            "citations": p.get("citationCount", 0),
            "source": "semantic_scholar",
        })
    return papers


# ─── CrossRef ────────────────────────────────────────────────────────

def search_crossref(query: str, limit: int = 10) -> list[dict]:
    params = {
        "query": query,
        "rows": limit,
        "sort": "relevance",
        "select": "title,author,published-print,container-title,DOI,abstract,is-referenced-by-count,link",
    }
    url = f"https://api.crossref.org/works?{urllib.parse.urlencode(params)}"
    try:
        data = json.loads(_fetch(url, timeout=45))
    except Exception as e:
        print(f"[WARN] CrossRef failed: {e}", file=sys.stderr)
        return []

    papers = []
    for item in data.get("message", {}).get("items", []):
        date_parts = (item.get("published-print") or {}).get("date-parts", [[0]])[0]
        year = date_parts[0] if date_parts else 0
        authors = [
            f"{a.get('given', '')} {a.get('family', '')}".strip()
            for a in (item.get("author") or [])
        ]
        doi = item.get("DOI", "")

        # Try to find a PDF link in CrossRef metadata
        pdf_url = ""
        for link in (item.get("link") or []):
            if link.get("content-type") == "application/pdf":
                pdf_url = link.get("URL", "")
                break

        papers.append({
            "title": (item.get("title") or [""])[0],
            "authors": authors,
            "year": year,
            "abstract": (item.get("abstract") or "").replace("<jats:p>", "").replace("</jats:p>", ""),
            "url": f"https://doi.org/{doi}" if doi else "",
            "pdf_url": pdf_url,
            "doi": doi,
            "journal": (item.get("container-title") or [""])[0],
            "citations": item.get("is-referenced-by-count", 0),
            "source": "crossref",
        })
    return papers


# ─── OpenAlex ────────────────────────────────────────────────────────

def search_openalex(query: str, limit: int = 10) -> list[dict]:
    """Search OpenAlex — free, covers 250M+ works, good OA coverage."""
    params = {
        "search": query,
        "per_page": limit,
        "sort": "relevance_score:desc",
        "select": "title,authorships,publication_year,doi,open_access,primary_location,cited_by_count,abstract_inverted_index",
        "mailto": "research@example.com",
    }
    url = f"https://api.openalex.org/works?{urllib.parse.urlencode(params)}"
    try:
        data = json.loads(_fetch(url))
    except Exception as e:
        print(f"[WARN] OpenAlex failed: {e}", file=sys.stderr)
        return []

    papers = []
    for item in data.get("results", []):
        # Reconstruct abstract from inverted index
        abstract = ""
        inv_idx = item.get("abstract_inverted_index")
        if inv_idx:
            word_positions = []
            for word, positions in inv_idx.items():
                for pos in positions:
                    word_positions.append((pos, word))
            word_positions.sort()
            abstract = " ".join(w for _, w in word_positions)

        authors = [
            (a.get("author") or {}).get("display_name", "")
            for a in (item.get("authorships") or [])
        ]

        doi = (item.get("doi") or "").replace("https://doi.org/", "")
        oa = item.get("open_access") or {}
        pdf_url = oa.get("oa_url", "") or ""

        primary = item.get("primary_location") or {}
        journal = (primary.get("source") or {}).get("display_name", "")
        landing_url = primary.get("landing_page_url", "")
        paper_url = f"https://doi.org/{doi}" if doi else landing_url

        papers.append({
            "title": (item.get("title") or ""),
            "authors": authors,
            "year": item.get("publication_year") or 0,
            "abstract": abstract,
            "url": paper_url,
            "pdf_url": pdf_url,
            "doi": doi,
            "journal": journal,
            "citations": item.get("cited_by_count", 0),
            "source": "openalex",
        })
    return papers


# ─── CORE ────────────────────────────────────────────────────────────

def search_core(query: str, limit: int = 10) -> list[dict]:
    """Search CORE — 300M+ open access articles."""
    params = {
        "q": query,
        "limit": limit,
    }
    url = f"https://api.core.ac.uk/v3/search/works?{urllib.parse.urlencode(params)}"
    try:
        data = json.loads(_fetch(url, timeout=30))
    except Exception as e:
        print(f"[WARN] CORE failed: {e}", file=sys.stderr)
        return []

    papers = []
    for item in data.get("results", []):
        doi = (item.get("doi") or "")
        pdf_url = item.get("downloadUrl", "") or item.get("sourceFulltextUrls", [""])[0] if item.get("sourceFulltextUrls") else ""

        papers.append({
            "title": item.get("title", ""),
            "authors": [a.get("name", "") for a in (item.get("authors") or [])],
            "year": item.get("yearPublished") or 0,
            "abstract": (item.get("abstract") or ""),
            "url": f"https://doi.org/{doi}" if doi else item.get("downloadUrl", ""),
            "pdf_url": pdf_url,
            "doi": doi,
            "journal": (item.get("journal") or {}).get("title", "") if isinstance(item.get("journal"), dict) else "",
            "citations": item.get("citationCount", 0),
            "source": "core",
        })
    return papers


# ─── Dedup & Sort ────────────────────────────────────────────────────

def deduplicate(papers: list[dict]) -> list[dict]:
    seen_dois = set()
    seen_titles = set()
    unique = []
    for p in papers:
        doi = p.get("doi", "").lower().strip()
        title_key = p.get("title", "").lower().strip()[:60]
        if doi and doi in seen_dois:
            continue
        if title_key and title_key in seen_titles:
            continue
        if doi:
            seen_dois.add(doi)
        if title_key:
            seen_titles.add(title_key)
        unique.append(p)
    return unique


def sort_results(papers: list[dict]) -> list[dict]:
    return sorted(papers, key=lambda p: (p.get("citations", 0), p.get("year", 0)), reverse=True)


# ─── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Multi-source academic paper search")
    parser.add_argument("--query", required=True, help="Search keywords")
    parser.add_argument("--limit", type=int, default=10, help="Max results (default 10)")
    args = parser.parse_args()

    all_papers = []
    sources = [
        ("Semantic Scholar", search_semantic_scholar),
        ("OpenAlex", search_openalex),
        ("CrossRef", search_crossref),
        ("CORE", search_core),
        ("arXiv", search_arxiv),
    ]

    for name, fn in sources:
        print(f"[INFO] Searching {name}...", file=sys.stderr)
        all_papers.extend(fn(args.query, args.limit))
        time.sleep(0.5)

    unique = deduplicate(all_papers)
    sorted_papers = sort_results(unique)[:args.limit]

    source_counts = {}
    for p in sorted_papers:
        s = p["source"]
        source_counts[s] = source_counts.get(s, 0) + 1

    pdf_available = sum(1 for p in sorted_papers if p.get("pdf_url"))

    print(json.dumps({
        "query": args.query,
        "count": len(sorted_papers),
        "pdf_available": pdf_available,
        "source_breakdown": source_counts,
        "papers": sorted_papers,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
