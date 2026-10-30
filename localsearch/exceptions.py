"""Исключения модуля LocalSearch.

Все ошибки библиотеки наследуются от :class:`LocalSearchError`,
чтобы внешний код мог перехватывать их одним ``except``.
"""


class LocalSearchError(Exception):
    """Базовое исключение библиотеки LocalSearch."""


class ExtractionError(LocalSearchError):
    """Ошибка извлечения текста или метаданных из файла/URL."""


class IndexError(LocalSearchError):
    """Ошибка построения, сохранения или загрузки индекса."""


class CrawlError(LocalSearchError):
    """Ошибка обхода директорий или веб-ссылок."""


class QueryParseError(LocalSearchError):
    """Ошибка разбора поискового запроса (например, незакрытая кавычка)."""
