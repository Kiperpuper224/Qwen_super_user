"""LocalSearch — локальная поисковая система для файлов и веб-страниц.

Библиотека предоставляет:

* :class:`LocalSearch` — высокоуровневый фасад (crawl/search/save/load);
* :class:`Crawler` — рекурсивный обход директорий и веб-ссылок с прогрессом;
* :class:`Index` — инвертированный индекс с сохранением в SQLite/JSON;
* :class:`SearchEngine` — BM25, фразы, regex, фильтры по метаданным, сниппеты;
* экстракторы текста для .txt/.md/.log/.csv/.html/.pdf/.docx.

Быстрый старт::

    from localsearch import LocalSearch

    ls = LocalSearch(index_path="docs_index.db", extensions=["txt", "pdf", "docx", "html"])
    ls.crawl("/path/to/documents", depth=3)
    resp = ls.search('"machine learning" ext:pdf after:2024-01-01')
    for hit in resp.hits:
        print(hit.meta["name"], hit.score, hit.snippet)
    ls.save()

Веб-интерфейс запускается отдельно: ``python -m localsearch.app --backend fastapi``.
"""

from .api import LocalSearch
from .crawler import Crawler, CrawlProgress
from .exceptions import (CrawlError, ExtractionError, IndexError,
                         LocalSearchError, QueryParseError)
from .extractors import (SUPPORTED_EXTENSIONS, extract_docx, extract_from_path,
                         extract_html, extract_pdf, extract_txt)
from .indexer import Index
from .search_engine import (ParsedQuery, SearchEngine, SearchResponse,
                            SearchResult, parse_query)
from .utils import detect_encoding, human_size, split_sentences, tokenize

__version__ = "1.0.0"
__author__ = "LocalSearch Project"
__all__ = [
    "LocalSearch",
    "Crawler", "CrawlProgress", "CrawlError",
    "Index", "IndexError",
    "SearchEngine", "SearchResult", "SearchResponse", "ParsedQuery", "parse_query",
    "QueryParseError", "ExtractionError", "LocalSearchError",
    "extract_txt", "extract_html", "extract_pdf", "extract_docx",
    "extract_from_path", "SUPPORTED_EXTENSIONS",
    "tokenize", "split_sentences", "detect_encoding", "human_size",
    "__version__",
]
