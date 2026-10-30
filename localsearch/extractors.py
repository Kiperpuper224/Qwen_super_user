"""extractors.py — извлечение текста и метаданных из файлов разных форматов.

Поддерживаемые форматы: ``.txt``, ``.md``, ``.log``, ``.csv``, ``.html``/``.htm``,
``.pdf``, ``.docx``. Для PDF/DOCX используются необязательные зависимости
(pypdf, python-docx, beautifulsoup4); при их отсутствии соответствующий формат
просто пропускается с записью в лог ошибок краулера.

Каждый экстрактор возвращает словарь::

    {
        "text": str,          # извлечённый текст
        "title": str | None,  # заголовок (H1/TITLE для HTML, метаданные для PDF/DOCX)
        "author": str | None, # автор, если найден в метаданных
    }
"""

from __future__ import annotations

import io
import logging
import os
import re
from typing import Callable, Dict, Optional

from .exceptions import ExtractionError
from .utils import decode_bytes, detect_encoding

logger = logging.getLogger(__name__)

#: Поддерживаемые расширения (без точки, в нижнем регистре).
SUPPORTED_EXTENSIONS = {"txt", "md", "log", "csv", "html", "htm", "pdf", "docx"}


def extract_txt(path: str) -> Dict[str, Optional[str]]:
    """Читает текстовый файл, определяя кодировку автоматически."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ExtractionError(f"Не удалось прочитать файл {path}: {exc}") from exc
    encoding = detect_encoding(data)
    text = decode_bytes(data, encoding)
    # Первая непустая строка часто служит заголовком.
    title = next((ln.strip()[:200] for ln in text.splitlines() if ln.strip()), None)
    return {"text": text, "title": title, "author": None}


def extract_html(path: str) -> Dict[str, Optional[str]]:
    """Извлекает видимый текст и метаданные из HTML-файла (BeautifulSoup)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ExtractionError(f"Не удалось прочитать файл {path}: {exc}") from exc
    html = decode_bytes(raw)
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - зависимость в requirements.txt
        logger.warning("beautifulsoup4 не установлен — HTML читается как текст")
        return extract_txt(path)
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.find("title")
    title = title_el.get_text(strip=True) if title_el else None
    author = None
    meta_author = soup.find("meta", attrs={"name": re.compile("^author$", re.I)})
    if meta_author and meta_author.get("content"):
        author = meta_author["content"].strip()
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    h1 = soup.find("h1")
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    return {"text": text, "title": title or (h1.get_text(strip=True) if h1 else None),
            "author": author}


def extract_pdf(path: str) -> Dict[str, Optional[str]]:
    """Извлекает текст и метаданные из PDF (pypdf)."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("Для чтения PDF установите пакет pypdf") from exc
    try:
        reader = PdfReader(path)
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception as page_exc:  # повреждённая страница не должна рвать файл
                logger.debug("Пропущена страница PDF %s: %s", path, page_exc)
        meta = reader.metadata or {}
        title = (meta.title or "").strip() or None
        author = (meta.author or "").strip() or None
        return {"text": "\n".join(parts), "title": title, "author": author}
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Ошибка разбора PDF {path}: {exc}") from exc


def extract_docx(path: str) -> Dict[str, Optional[str]]:
    """Извлекает текст и метаданные из DOCX (python-docx)."""
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("Для чтения DOCX установите пакет python-docx") from exc
    try:
        document = docx.Document(path)
        paragraphs = [p.text for p in document.paragraphs]
        # Добавляем текст из таблиц, чтобы индекс был полнее.
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    paragraphs.append(cell.text)
        cp = document.core_properties
        return {
            "text": "\n".join(paragraphs),
            "title": (cp.title or "").strip() or None,
            "author": (cp.author or "").strip() or None,
        }
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Ошибка разбора DOCX {path}: {exc}") from exc


#: Реестр экстракторов: расширение -> функция(path) -> dict
EXTRACTORS: Dict[str, Callable[[str], Dict[str, Optional[str]]]] = {
    "txt": extract_txt,
    "md": extract_txt,
    "log": extract_txt,
    "csv": extract_txt,
    "html": extract_html,
    "htm": extract_html,
    "pdf": extract_pdf,
    "docx": extract_docx,
}


def extract_from_path(path: str) -> Dict[str, Optional[str]]:
    """Извлекает текст+метаданные по расширению файла.

    :raises ExtractionError: если формат не поддерживается или файл повреждён.
    """
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    extractor = EXTRACTORS.get(ext)
    if extractor is None:
        raise ExtractionError(f"Неподдерживаемое расширение: .{ext}")
    return extractor(path)


def extract_from_bytes(data: bytes, source: str, ext: str) -> Dict[str, Optional[str]]:
    """Извлекает текст из байтов (например, загруженных из веба), сохраняя в ``source``.

    Для текстовых форматов декодирует напрямую; для бинарных пишет временный файл
    и вызывает обычный экстрактор по пути.
    """
    ext = ext.lower().lstrip(".")
    if ext not in EXTRACTORS:
        raise ExtractionError(f"Неподдерживаемый веб-формат: .{ext}")
    if ext in ("txt", "md", "log", "csv"):
        encoding = detect_encoding(data)
        text = decode_bytes(data, encoding)
        title = next((ln.strip()[:200] for ln in text.splitlines() if ln.strip()), None)
        return {"text": text, "title": title, "author": None}
    # Бинарные форматы: пишем во временный файл с нужным расширением.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        return EXTRACTORS[ext](tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
