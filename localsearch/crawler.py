"""crawler.py — рекурсивный обход локальных директорий и веб-ссылок (паук).

Класс :class:`Crawler` выполняет обход в отдельном потоке и складывает
извлечённые документы в индекс (:class:`localsearch.indexer.Index`).

Возможности:

* рекурсивный обход локальных директорий с настраиваемой глубиной;
* обход веб-ссылок с переходом по <a href> в пределах домена (depth limit);
* фильтры по расширениям (``exts``) и размеру файла (``min_size``/``max_size``);
* пропуск скрытых/служебных директорий, защита от циклов и symlink-петель;
* колбэк прогресса ``on_progress(CrawlProgress)`` для индикатора в реальном времени;
* корректная остановка по ``stop()``.

Пример::

    idx = Index()
    crawler = Crawler(idx, extensions=["pdf", "docx", "txt", "html"], max_depth=2)
    crawler.on_progress = lambda p: print(p.percent)
    crawler.crawl_directory("/home/user/docs")   # синхронно
    crawler.crawl_web("https://example.com", max_pages=50)  # синхронно
    crawler.start_async("/home/user/docs")       # в фоновом потоке
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set
from urllib.parse import urldefrag, urljoin, urlparse

from .exceptions import CrawlError, ExtractionError
from .extractors import SUPPORTED_EXTENSIONS, extract_from_bytes, extract_from_path
from .indexer import Index
from .utils import detect_encoding

logger = logging.getLogger(__name__)

#: Директории, которые никогда не обходятся.
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode"}


@dataclass
class CrawlProgress:
    """Снимок состояния краулера — передаётся в колбэк on_progress."""

    status: str = "idle"          # idle | running | done | stopped | error
    current: str = ""             # путь/URL, обрабатываемый сейчас
    processed: int = 0            # файлов обработано
    indexed: int = 0              # файлов добавлено в индекс
    skipped: int = 0              # пропущено (фильтры/неподдерживаемые)
    errors: int = 0               # число ошибок извлечения
    total_estimated: int = 0      # оценка общего числа файлов (для local-обхода)
    started_at: float = 0.0
    elapsed: float = 0.0

    @property
    def percent(self) -> float:
        """Прогресс в процентах (0..100); если оценка неизвестна — -1."""
        if self.total_estimated <= 0:
            return -1.0
        return min(100.0, round(100.0 * self.processed / self.total_estimated, 1))

    def to_dict(self) -> Dict[str, object]:
        """JSON-совместимое представление (для REST API и JS-индикатора)."""
        d = self.__dict__.copy()
        d["percent"] = self.percent
        d["elapsed"] = round(time.time() - self.started_at, 1) if self.started_at else 0.0
        return d


class Crawler:
    """Паук локальных директорий и веб-страниц, наполняющий :class:`Index`."""

    def __init__(self, index: Optional[Index] = None,
                 extensions: Optional[List[str]] = None,
                 max_depth: int = 3,
                 max_size_bytes: int = 20 * 1024 * 1024,
                 min_size_bytes: int = 0,
                 user_agent: str = "LocalSearchBot/1.0",
                 request_timeout: float = 10.0) -> None:
        #: индекс, в который складываются документы
        self.index = index if index is not None else Index()
        self.extensions = {e.lower().lstrip(".") for e in
                           (extensions or sorted(SUPPORTED_EXTENSIONS))}
        self.max_depth = max(0, int(max_depth))
        self.max_size_bytes = int(max_size_bytes)
        self.min_size_bytes = int(min_size_bytes)
        self.user_agent = user_agent
        self.request_timeout = float(request_timeout)

        #: колбэк прогресса: fn(CrawlProgress)
        self.on_progress: Optional[Callable[[CrawlProgress], None]] = None

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self.progress = CrawlProgress(status="idle")

    # ------------------------------------------------------------------ #
    # Управление состоянием/прогрессом
    # ------------------------------------------------------------------ #
    def _emit(self, **changes) -> None:
        """Обновляет поля прогресса и вызывает колбэк on_progress (поток краулера)."""
        with self._lock:
            for key, value in changes.items():
                setattr(self.progress, key, value)
            self.progress.elapsed = (time.time() - self.progress.started_at
                                      if self.progress.started_at else 0.0)
            snapshot = CrawlProgress(**{k: v for k, v in self.progress.__dict__.items()})
        cb = self.on_progress
        if cb is not None:
            try:
                cb(snapshot)
            except Exception:  # колбэк не должен ронять краулер
                logger.exception("Ошибка в колбэке on_progress")

    def get_progress(self) -> CrawlProgress:
        """Потокобезопасное чтение текущего прогресса (для UI/API)."""
        with self._lock:
            return CrawlProgress(**dict(self.progress.__dict__))

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        """Запрашивает остановку активного обхода."""
        self._stop_flag.set()

    def _begin_run(self) -> None:
        self._stop_flag.clear()
        self.progress = CrawlProgress(status="running", started_at=time.time())
        self._emit()

    def _accept_file(self, path_or_url: str, size: int) -> bool:
        """Фильтр: расширение + размер + флаг остановки."""
        ext = os.path.splitext(urlparse(path_or_url).path)[1].lower().lstrip(".")
        if ext not in self.extensions:
            return False
        if size > self.max_size_bytes or size < self.min_size_bytes:
            return False
        return True

    def _store(self, meta: Dict[str, object], text: str) -> None:
        """Класть документ в индекс (заменяя старую версию того же пути)."""
        key = meta.get("path") or meta.get("url")
        if key:
            self.index.remove_by_path(str(key))
        self.index.add_document(meta, text)

    # ------------------------------------------------------------------ #
    # Локальный рекурсивный обход
    # ------------------------------------------------------------------ #
    def count_files(self, root: str) -> int:
        """Предварительная оценка количества подходящих файлов (для %.
        Использует ту же логику фильтров, но без чтения содержимого)."""
        total = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                if self._accept_file(full, size):
                    total += 1
        return total

    def crawl_directory(self, root: str) -> CrawlProgress:
        """Синхронный рекурсивный обход локальной директории.

        :param root: корневая папка.
        :raises CrawlError: если корень не существует или это не директория.
        :return: итоговый прогресс.
        """
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            raise CrawlError(f"Директория не найдена: {root}")
        self._begin_run()
        root_depth = root.rstrip(os.sep).count(os.sep)
        self._emit(total_estimated=self.count_files(root))

        visited_real: Set[str] = set()  # защита от symlink-петель
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if self._stop_flag.is_set():
                break
            try:
                depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
            except ValueError:
                depth = 0
            # Фильтрация директорий: скрытые/служебные/глубже max_depth.
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")
                           and (self.max_depth <= 0 or depth + 1 <= self.max_depth)]
            for name in sorted(filenames):
                if self._stop_flag.is_set():
                    break
                full = os.path.join(dirpath, name)
                real = os.path.realpath(full)
                if real in visited_real:
                    continue
                visited_real.add(real)
                self._process_local_file(full, name, root)

        final_status = "stopped" if self._stop_flag.is_set() else "done"
        self._emit(status=final_status, current="")
        return self.get_progress()

    def _process_local_file(self, full: str, name: str, root: str) -> None:
        """Читает один локальный файл, извлекает текст/метаданные и кладёт в индекс."""
        self._emit(current=full, processed=self.progress.processed + 0)
        try:
            stat = os.stat(full)
            size = stat.st_size
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if not self._accept_file(full, size):
                self._emit(processed=self.progress.processed + 1,
                           skipped=self.progress.skipped + 1)
                return
            try:
                with open(full, "rb") as fh:
                    head = fh.read(4096)
                encoding = detect_encoding(head)
                payload = extract_from_path(full)
            except ExtractionError as exc:
                logger.warning("Пропущен файл %s: %s", full, exc)
                self._emit(processed=self.progress.processed + 1,
                           errors=self.progress.errors + 1)
                return
            meta = {
                "path": full,
                "relative_path": os.path.relpath(full, root),
                "name": name,
                "extension": ext,
                "size": size,
                "created": stat.st_ctime,
                "modified": stat.st_mtime,
                "encoding": encoding,
                "title": payload.get("title"),
                "author": payload.get("author"),
                "source": "local",
            }
            self._store(meta, payload["text"] or "")
            self._emit(processed=self.progress.processed + 1,
                       indexed=self.progress.indexed + 1)
        except OSError as exc:
            logger.warning("Ошибка доступа к %s: %s", full, exc)
            self._emit(processed=self.progress.processed + 1,
                       errors=self.progress.errors + 1)

    # ------------------------------------------------------------------ #
    # Веб-обход (паук)
    # ------------------------------------------------------------------ #
    def crawl_web(self, start_url: str, max_pages: int = 100,
                  same_domain_only: bool = True) -> CrawlProgress:
        """Обход веб-ссылок с переходом по HTML-ссылкам (BFS) до ``max_depth``.

        :param start_url: стартовая страница (http/https).
        :param max_pages: ограничение на число загруженных страниц.
        :param same_domain_only: не покидать домен старта.
        :raises CrawlError: при недоступности библиотеки requests или ошибке сети.
        """
        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise CrawlError("Для веб-обхода установите пакет requests") from exc

        self._begin_run()
        start_url, _ = urldefrag(start_url)
        parsed_start = urlparse(start_url)
        if parsed_start.scheme not in ("http", "https"):
            raise CrawlError(f"Неподдерживаемая схема URL: {start_url}")

        session = requests.Session()
        session.headers.update({"User-Agent": self.user_agent})

        queue: List[tuple] = [(start_url, 0)]
        seen: Set[str] = set()
        pages_done = 0

        while queue and pages_done < max_pages:
            if self._stop_flag.is_set():
                break
            url, depth = queue.pop(0)
            url, _ = urldefrag(url)
            if url in seen:
                continue
            seen.add(url)
            self._emit(current=url, total_estimated=min(max_pages, len(seen) + len(queue)))
            try:
                resp = session.get(url, timeout=self.request_timeout)
                resp.raise_for_status()
            except Exception as exc:
                logger.warning("Не удалось загрузить %s: %s", url, exc)
                self._emit(processed=self.progress.processed + 1,
                           errors=self.progress.errors + 1)
                continue

            ctype = resp.headers.get("Content-Type", "").lower()
            path_ext = os.path.splitext(urlparse(url).path)[1].lower().lstrip(".")
            meta = {
                "path": url, "url": url,
                "name": os.path.basename(urlparse(url).path) or url,
                "extension": path_ext or ("html" if "html" in ctype else "txt"),
                "size": len(resp.content),
                "created": time.time(), "modified": time.time(),
                "encoding": resp.encoding or detect_encoding(resp.content[:4096]),
                "source": "web",
            }
            accepted = self._accept_file(url if path_ext else f"{url}.{meta['extension']}",
                                         meta["size"])
            if not accepted:
                self._emit(processed=self.progress.processed + 1,
                           skipped=self.progress.skipped + 1)
                continue
            try:
                payload = extract_from_bytes(resp.content, url, meta["extension"])
                self._store(meta, payload["text"] or "")
                self._emit(processed=self.progress.processed + 1,
                           indexed=self.progress.indexed + 1)
            except ExtractionError as exc:
                logger.warning("Извлечение из %s не удалось: %s", url, exc)
                self._emit(processed=self.progress.processed + 1,
                           errors=self.progress.errors + 1)
            pages_done += 1

            # Расстановка ссылок для следующего уровня глубины.
            if depth < self.max_depth and ("html" in ctype or path_ext in ("html", "htm")):
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for a in soup.find_all("a", href=True):
                        link, _ = urldefrag(urljoin(url, a["href"]))
                        p = urlparse(link)
                        if p.scheme not in ("http", "https"):
                            continue
                        if same_domain_only and p.netloc != parsed_start.netloc:
                            continue
                        if link not in seen:
                            queue.append((link, depth + 1))
                except Exception:
                    logger.debug("Не удалось распарсить ссылки на %s", url, exc_info=True)

        final_status = "stopped" if self._stop_flag.is_set() else "done"
        self._emit(status=final_status, current="")
        return self.get_progress()

    # ------------------------------------------------------------------ #
    # Асинхронный запуск (для веб-интерфейса)
    # ------------------------------------------------------------------ #
    def start_async(self, target: str, kind: str = "auto", **kwargs) -> bool:
        """Запускает обход в фоновом потоке.

        :param target: путь к директории или URL.
        :param kind: ``"dir"`` | ``"web"`` | ``"auto"`` (по схеме ``http(s)://``).
        :return: True, если поток запущен; False, если краулер уже занят.
        """
        if self.is_running:
            return False
        is_url = kind == "web" or (kind == "auto" and target.startswith(("http://", "https://")))
        fn = (lambda: self.crawl_web(target, **kwargs)) if is_url \
            else (lambda: self.crawl_directory(target, **kwargs))

        def runner() -> None:
            try:
                fn()
            except CrawlError as exc:
                self._emit(status="error", current=str(exc),
                           errors=self.progress.errors + 1)
            except Exception as exc:  # страховка от неожиданного падения потока
                logger.exception("Краулер аварийно остановился")
                self._emit(status="error", current=str(exc),
                           errors=self.progress.errors + 1)

        self._thread = threading.Thread(target=runner, name="localsearch-crawler", daemon=True)
        self._thread.start()
        return True
