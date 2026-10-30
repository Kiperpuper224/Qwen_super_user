"""api.py — высокоуровневый фасад библиотеки (удобный API для внешнего кода).

Класс :class:`LocalSearch` объединяет краулер, индексатор и поисковый движок
и позволяет работать с системой одной строкой::

    from localsearch import LocalSearch

    ls = LocalSearch(index_path="my_index.db")   # загрузит индекс, если файл есть
    ls.crawl("/home/user/documents", depth=3)    # синхронный обход + индексация
    results = ls.search("python 'data science' ext:pdf")
    for hit in results.hits:
        print(hit.meta["name"], hit.score, hit.snippet)
    ls.save()                                     # персист в SQLite
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from .crawler import Crawler, CrawlProgress
from .exceptions import LocalSearchError
from .indexer import Index
from .search_engine import SearchEngine, SearchResponse

logger = logging.getLogger(__name__)


class LocalSearch:
    """Фасад локальной поисковой системы: краулер + индекс + движок."""

    def __init__(self, index_path: Optional[str] = None,
                 storage_format: str = "sqlite",
                 extensions: Optional[List[str]] = None,
                 max_depth: int = 3,
                 max_size_bytes: int = 20 * 1024 * 1024,
                 min_size_bytes: int = 0,
                 autosave: bool = False) -> None:
        """
        :param index_path: путь к файлу индекса (.db/.sqlite или .json); при
            существовании файла индекс автоматически загружается.
        :param storage_format: ``"sqlite"`` или ``"json"`` (если у index_path нет расширения).
        :param extensions: ограничить форматы, например ["txt","pdf","docx","html"].
        :param max_depth: глубина рекурсии/паутины для краулера.
        :param max_size_bytes/min_size_bytes: границы размера файлов.
        :param autosave: сохранять индекс на диске после каждого crawl.
        """
        self.index_path = index_path
        self.storage_format = storage_format if storage_format in ("sqlite", "json") else "sqlite"
        self.autosave = autosave

        self.index = Index()
        if index_path and os.path.exists(index_path):
            try:
                # Загружаем поверх существующего объекта, чтобы ссылки на него
                # (созданные ниже crawler/engine) оставались валидными.
                if index_path.lower().endswith(".json"):
                    self.index.load_json(index_path)
                else:
                    self.index.load_sqlite(index_path)
                logger.info("Загружен индекс из %s (%d документов)",
                            index_path, self.index.doc_count)
            except LocalSearchError:
                logger.exception("Не удалось загрузить индекс — начинаем с чистого")

        self.crawler = Crawler(self.index, extensions=extensions, max_depth=max_depth,
                               max_size_bytes=max_size_bytes, min_size_bytes=min_size_bytes)
        self.engine = SearchEngine(self.index)

    # ------------------------------------------------------------------ #
    # Сбор данных
    # ------------------------------------------------------------------ #
    def crawl(self, target: str, depth: Optional[int] = None, **kwargs) -> CrawlProgress:
        """Синхронный обход директории или URL (auto-определение по схеме http)."""
        if depth is not None:
            self.crawler.max_depth = int(depth)
        if target.startswith(("http://", "https://")):
            progress = self.crawler.crawl_web(target, **kwargs)
        else:
            progress = self.crawler.crawl_directory(target, **kwargs)
        if self.autosave and self.index_path:
            self.save()
        return progress

    def crawl_async(self, target: str, depth: Optional[int] = None,
                    on_progress=None, **kwargs) -> bool:
        """Фоновый обход; возвращает False, если краулер уже запущен."""
        if depth is not None:
            self.crawler.max_depth = int(depth)
        if on_progress is not None:
            self.crawler.on_progress = on_progress
        return self.crawler.start_async(target, **kwargs)

    def stop_crawl(self) -> None:
        """Останавливает фоновый обход."""
        self.crawler.stop()

    @property
    def crawl_progress(self) -> CrawlProgress:
        """Текущий статус/прогресс краулера (для индикатора)."""
        return self.crawler.get_progress()

    # ------------------------------------------------------------------ #
    # Поиск
    # ------------------------------------------------------------------ #
    def search(self, query: str, limit: int = 20, offset: int = 0) -> SearchResponse:
        """Выполняет поисковый запрос (см. синтаксис в search_engine.parse_query)."""
        return self.engine.search(query, limit=limit, offset=offset)

    def stats(self) -> Dict[str, Any]:
        """Статистика индекса и состояние краулера."""
        s = self.index.stats()
        p = self.crawler.get_progress()
        s["crawler_status"] = p.status
        s["index_path"] = self.index_path
        return s

    # ------------------------------------------------------------------ #
    # Персистентность
    # ------------------------------------------------------------------ #
    def _resolve_path(self, path: Optional[str]) -> str:
        p = path or self.index_path
        if not p:
            p = f"localsearch_index.{self.storage_format}"
        if not os.path.splitext(p)[1]:
            p += ".db" if self.storage_format == "sqlite" else ".json"
        return p

    def save(self, path: Optional[str] = None) -> str:
        """Сохраняет индекс; возвращает фактический путь файла."""
        p = self._resolve_path(path)
        self.index.save(p)
        self.index_path = p
        return p

    def load(self, path: Optional[str] = None) -> str:
        """Загружает индекс из файла поверх текущего; возвращает путь."""
        p = self._resolve_path(path)
        # Загружаем поверх текущего объекта (Index.load — classmethod, здесь
        # используются методы экземпляра load_json/load_sqlite).
        if p.lower().endswith(".json"):
            self.index.load_json(p)
        else:
            self.index.load_sqlite(p)
        self.index_path = p
        return p
