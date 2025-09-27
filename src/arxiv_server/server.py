import argparse
import os
import re
import difflib
import json
import asyncio
import random
import uuid
from typing import Optional, Tuple, Dict, Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
import feedparser
import fitz

mcp = FastMCP("arxiv-server")

USER_AGENT = "arxiv-mcp/1.0"
ARXIV_API_BASE = "https://export.arxiv.org/api"
DEFAULT_TIMEOUT = 60.0
RETRY_ATTEMPTS = 3
RETRY_BASE = 0.5  # seconds
HTTP_LIMITS = httpx.Limits(max_keepalive_connections=5, max_connections=10)

# Ensure DOWNLOAD_PATH exists; fall back to ./downloads for local runs
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH") or os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

ARXIV_ID_RE = re.compile(
    r"^(?:arXiv:)?(?P<id>[\d]{4}\.[\d]{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2})?\/\d{7}(?:v\d+)?)$",
    re.IGNORECASE,
)

def _error(code: str, message: str, *, retry_after: Optional[int] = None) -> str:
    """Return structured JSON error."""
    payload = {
        "status": "error",
        "code": code,
        "message": message,
        "retry_after": retry_after,
        "request_id": str(uuid.uuid4()),
    }
    return json.dumps(payload)

async def _retry_sleep(attempt: int) -> None:
    # Exponential backoff with jitter
    base = RETRY_BASE * (2 ** attempt)
    await asyncio.sleep(base + random.random() * 0.25)

async def make_api_call(url: str, params: Dict[str, str]) -> Optional[str]:
    """Make a request to the arXiv API with retries."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/atom+xml"}
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, limits=HTTP_LIMITS) as client:
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                return resp.text
            except Exception:
                if attempt < RETRY_ATTEMPTS - 1:
                    await _retry_sleep(attempt)
                    continue
                return None

async def get_pdf(url: str) -> Optional[bytes]:
    """Get PDF document as bytes from arXiv.org with retries."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/pdf"}
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, limits=HTTP_LIMITS) as client:
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.content
            except Exception:
                if attempt < RETRY_ATTEMPTS - 1:
                    await _retry_sleep(attempt)
                    continue
                return None

def find_best_match(target_title: str, entries: list, threshold: float = 0.8):
    """Find the entry whose title best matches the target title."""
    target_title_lower = target_title.lower()
    best_entry = None
    best_score = 0.0
    for entry in entries:
        entry_title_lower = entry.title.lower()
        score = difflib.SequenceMatcher(None, target_title_lower, entry_title_lower).ratio()
        if score > best_score:
            best_score = score
            best_entry = entry
    if best_score >= threshold:
        return best_entry
    return None

async def fetch_information(title: str):
    """Get information about the article."""
    formatted_title = format_text(title)
    url = f"{ARXIV_API_BASE}/query"
    params = {
        "search_query": f"ti:{formatted_title}",
        "start": 0,
        "max_results": 25,
    }
    data = await make_api_call(url, params=params)
    if data is None:
        return "Unable to retrieve data from arXiv.org."
    feed = feedparser.parse(data)
    error_msg = (
        "Unable to extract information for the provided title. "
        "This issue may stem from an incorrect or incomplete title, "
        "or because the work has not been published on arXiv."
    )
    if not feed.entries:
        return error_msg
    best_match = find_best_match(target_title=formatted_title, entries=feed.entries)
    if best_match is None:
        return str(error_msg)
    return best_match

async def resolve_article(title: Optional[str] = None, arxiv_id: Optional[str] = None) -> Tuple[str, str] | str:
    """
    Resolve to a direct PDF URL and arXiv ID using either a title or an arXiv ID.
    Preference order: arxiv_id > title.
    """
    if arxiv_id:
        m = ARXIV_ID_RE.match(arxiv_id.strip())
        if not m:
            return _error("INVALID_ID", f"Not a valid arXiv ID: {arxiv_id}")
        vid = m.group("id")
        return (f"https://arxiv.org/pdf/{vid}", vid)
    if not title:
        return _error("MISSING_PARAM", "Provide either 'arxiv_id' or 'title'.")
    info = await fetch_information(title)
    if isinstance(info, str):
        return _error("NOT_FOUND", str(info))
    resolved_id = info.id.split("/abs/")[-1]
    direct_pdf_url = f"https://arxiv.org/pdf/{resolved_id}"
    return (direct_pdf_url, resolved_id)

def format_text(text: str) -> str:
    """Clean a given text string by removing escape sequences and leading and trailing whitespaces."""
    # Remove common escape sequences
    text_without_escapes = re.sub(r"\\[ntr]", " ", text)
    # Replace colon with space
    text_without_colon = text_without_escapes.replace(":", " ")
    # Remove both single quotes and double quotes
    text_without_quotes = re.sub(r"['\"]", "", text_without_colon)
    # Collapse multiple spaces into one
    text_single_spaced = re.sub(r"\s+", " ", text_without_quotes)
    # Trim leading and trailing spaces
    cleaned_text = text_single_spaced.strip()
    return cleaned_text

@mcp.tool()
async def get_article_url(title: Optional[str] = None, arxiv_id: Optional[str] = None) -> str:
    """
    Retrieve the direct PDF URL of an article on arXiv.org by title or arXiv ID.

    Args:
        title: Article title.
        arxiv_id: arXiv ID (e.g., 1706.03762 or arXiv:1706.03762v7).

    Returns:
        URL that can be used to retrieve the article, or structured error JSON.
    """
    result = await resolve_article(title=title, arxiv_id=arxiv_id)
    if isinstance(result, str):
        return result
    article_url, _ = result
    return article_url

@mcp.tool()
async def download_article(
    title: Optional[str] = None,
    arxiv_id: Optional[str] = None,
) -> str:
    """
    Download the article as a PDF file. Resolve by arXiv ID or title.

    Args:
        title: Article title.
        arxiv_id: arXiv ID.

    Returns:
        Success message or structured error JSON.
    """
    result = await resolve_article(title=title, arxiv_id=arxiv_id)
    if isinstance(result, str):
        return result
    article_url, resolved_id = result
    headers = {"User-Agent": USER_AGENT, "Accept": "application/pdf"}
    file_path = os.path.join(DOWNLOAD_PATH, f"{resolved_id}.pdf")
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, limits=HTTP_LIMITS) as client:
        for attempt in range(RETRY_ATTEMPTS):
            try:
                async with client.stream("GET", article_url, headers=headers) as resp:
                    resp.raise_for_status()
                    with open(file_path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                f.write(chunk)
                return json.dumps({
                    "status": "ok",
                    "message": "Download successful.",
                    "path": file_path,
                })
            except Exception as e:
                if attempt < RETRY_ATTEMPTS - 1:
                    await _retry_sleep(attempt)
                    continue
                return _error("DOWNLOAD_FAILED", f"Unable to retrieve or save the article: {e}")

@mcp.tool()
async def load_article_to_context(
    title: Optional[str] = None,
    arxiv_id: Optional[str] = None,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
    max_pages: Optional[int] = None,
    max_chars: Optional[int] = None,
    preview: bool = False,
) -> str:
    """
    Load the article text into context. Supports title or arXiv ID resolution and partial extraction.

    Args:
        title: Article title.
        arxiv_id: arXiv ID.
        start_page: 1-based start page (inclusive).
        end_page: 1-based end page (inclusive).
        max_pages: hard cap on number of pages to extract.
        max_chars: hard cap on number of characters to extract.
        preview: if True, only validate availability and return minimal info.

    Returns:
        Article text or structured error JSON.
    """
    result = await resolve_article(title=title, arxiv_id=arxiv_id)
    if isinstance(result, str):
        return result
    article_url, resolved_id = result

    if preview:
        # Lightweight availability check
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, limits=HTTP_LIMITS) as client:
                head = await client.head(article_url, headers={"User-Agent": USER_AGENT})
                ok = head.status_code < 400
        except Exception:
            ok = False
        return json.dumps({"status": "ok" if ok else "error", "reachable": ok, "arxiv_id": resolved_id, "url": article_url})

    pdf_bytes = await get_pdf(article_url)
    if pdf_bytes is None:
        return _error("FETCH_FAILED", "Unable to retrieve the article from arXiv.org.")

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        return _error("PDF_OPEN_FAILED", f"Unable to open PDF: {e}")

    total_pages = doc.page_count
    # Normalize page bounds (1-based inputs)
    s = max(1, start_page) if start_page else 1
    e = min(end_page, total_pages) if end_page else total_pages
    if s > e or s < 1:
        return _error("BAD_RANGE", f"Invalid page range [{s}, {e}] for total_pages={total_pages}")

    # Apply max_pages cap
    if max_pages is not None:
        e = min(e, s + max_pages - 1)

    parts = []
    chars = 0
    for p in range(s - 1, e):
        page_text = doc.load_page(p).get_text()
        if not page_text:
            continue
        if max_chars is not None and chars + len(page_text) > max_chars:
            remain = max_chars - chars
            if remain > 0:
                parts.append(page_text[:remain])
                chars += remain
            break
        parts.append(page_text)
        chars += len(page_text)
    return "".join(parts)

@mcp.tool()
async def get_details(title: Optional[str] = None, arxiv_id: Optional[str] = None) -> str:
    """
    Retrieve metadata of an article by title or arXiv ID.

    Args:
        title: Article title.
        arxiv_id: arXiv ID.

    Returns:
        JSON string containing article details or structured error JSON.
    """
    if arxiv_id:
        # Quick path via ID
        res = await resolve_article(arxiv_id=arxiv_id)
        if isinstance(res, str):
            return res
        _, vid = res
        # Fetch the /abs entry for richer fields
        params = {"search_query": f"id:{vid}", "start": 0, "max_results": 1}
        data = await make_api_call(f"{ARXIV_API_BASE}/query", params=params)
        if data is None:
            return _error("API_ERROR", "Unable to retrieve data from arXiv.org.")
        feed = feedparser.parse(data)
        if not feed.entries:
            return _error("NOT_FOUND", f"No metadata for {vid}")
        info = feed.entries[0]
    else:
        info = await fetch_information(title or "")
        if isinstance(info, str):
            return _error("NOT_FOUND", str(info))

    entry_id = info.id
    link = info.link
    article_title = info.title
    authors = [author["name"] for author in info.authors]
    vid = entry_id.split("/abs/")[-1]
    direct_pdf_url = f"https://arxiv.org/pdf/{vid}"
    updated = getattr(info, "updated", "Unknown")
    published = getattr(info, "published", "Unknown")
    summary = getattr(info, "summary", "Unknown")
    info_dict = {
        "arXiv ID": vid,
        "Title": article_title,
        "Authors": authors,
        "Link": link,
        "Direct PDF URL": direct_pdf_url,
        "Published": published,
        "Updated": updated,
        "Summary": summary,
    }
    return json.dumps(info_dict)

@mcp.tool()
async def search_arxiv(
    ctx: Context,
    all_fields: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None,
    abstract: Optional[str] = None,
    start: int = 0,
    max_results: int = 10,
) -> Any:
    """
    Performs a search query on the arXiv API based on specified parameters and returns matching article metadata.
    This function allows for flexible querying of the arXiv database. Only parameters that are explicitly provided
    will be included in the final search query. Results are returned in a JSON-formatted string with article titles
    as keys and their corresponding arXiv IDs as values.

    Args:
        all_fields: General keyword search across all metadata fields including title, abstract, authors, comments, and categories.
        title: Keyword(s) to search for within the titles of articles.
        author: Author name(s) to filter results by.
        abstract: Keyword(s) to search for within article abstracts.
        start: Index of the first result to return; used for paginating through search results. Defaults to 0.
        max_results: Maximum number of results to return (1-50).

    Returns:
        A JSON-formatted string containing article titles and their associated arXiv IDs;
        otherwise, a structured error JSON string.
    """
    prefixed_params = []
    if author:
        author = format_text(author)
        prefixed_params.append(f"au:{author}")
    if all_fields:
        all_fields = format_text(all_fields)
        prefixed_params.append(f"all:{all_fields}")
    if title:
        title = format_text(title)
        prefixed_params.append(f"ti:{title}")
    if abstract:
        abstract = format_text(abstract)
        prefixed_params.append(f"abs:{abstract}")
    # Construct search query
    search_query = " AND ".join(prefixed_params)
    params = {
        "search_query": search_query,
        "start": start,
        "max_results": max(1, min(max_results, 50)),
    }
    await ctx.info("Calling the API")
    response = await make_api_call(f"{ARXIV_API_BASE}/query", params=params)
    if response is None:
        return _error("API_ERROR", "Unable to retrieve data from arXiv.org.")
    feed = feedparser.parse(response)
    error_msg = (
        "Unable to extract information for your query. "
        "This issue may stem from an incorrect search query."
    )
    if not feed.entries:
        return _error("NOT_FOUND", error_msg)
    entries: Dict[str, Dict[str, Any]] = {}
    await ctx.info("Extracting information")
    for entry in feed.entries:
        id = entry.id
        article_title = entry.title
        arxiv_id = id.split("/abs/")[-1]
        authors = [author['name'] for author in entry.authors]
        entries[article_title] = {"arXiv ID": arxiv_id, "Authors": authors}
    return entries

def _resolve_port(arg_port: Optional[int]) -> int:
    if arg_port is not None:
        return arg_port
    for key in ("MCP_PORT", "PORT"):
        value = os.getenv(key)
        if value:
            try:
                return int(value)
            except ValueError:
                raise ValueError(f"Invalid integer for {key}: {value}")
    return 8081


def main():
    parser = argparse.ArgumentParser(description="Run the arXiv MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        help="Transport to use. Defaults to HTTP when PORT is set, otherwise STDIO.",
    )
    parser.add_argument("--host", help="Host to bind for HTTP transport (default 0.0.0.0).")
    parser.add_argument("--port", type=int, help="Port to bind for HTTP transport.")
    args = parser.parse_args()

    transport = args.transport or os.getenv("MCP_TRANSPORT")
    if not transport:
        transport = "http" if os.getenv("PORT") else "stdio"

    if transport == "http":
        host = args.host or os.getenv("MCP_HOST") or os.getenv("HOST") or "0.0.0.0"
        port = _resolve_port(args.port)
        # Configure FastMCP HTTP settings before starting SSE transport
        mcp.settings.host = host
        mcp.settings.port = port
        sse_path = os.getenv("MCP_SSE_PATH") or os.getenv("FASTMCP_SSE_PATH")
        if sse_path:
            mcp.settings.sse_path = sse_path
        message_path = os.getenv("MCP_MESSAGE_PATH") or os.getenv("FASTMCP_MESSAGE_PATH")
        if message_path:
            mcp.settings.message_path = message_path
        print(f"Starting arxiv-server via HTTP (SSE) on {host}:{port}")
        mcp.run(transport="sse")
    else:
        print("Starting arxiv-server via STDIO transport")
        mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
