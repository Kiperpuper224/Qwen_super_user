"""Вспомогательные функции: токенизация, определение кодировки, форматирование."""

from __future__ import annotations

import re
from typing import List

# Регулярное выражение для токенов: слова (в т.ч. с дефисами/подчёркиваниями) и числа.
TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+(?:['\-][A-Za-zА-Яа-яЁё0-9_]+)*")

# Предложения: грубое разбиение по границам ". ! ?" с переводом строк.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Кандидаты кодировок по умолчанию (если chardet недоступен/не уверен).
DEFAULT_ENCODINGS = ("utf-8", "cp1251", "latin-1")


def tokenize(text: str) -> List[str]:
    """Разбивает текст на нижнерегистровые токены (слова/числа).

    :param text: произвольный текст.
    :return: список токенов; пустые токены отбрасываются.
    """
    if not text:
        return []
    return [t.lower() for t in TOKEN_RE.findall(text)]


def split_sentences(text: str) -> List[str]:
    """Разбивает текст на предложения (для фразового поиска по предложениям).

    Пустые и слишком короткие фрагменты (< 3 символов) отбрасываются.
    """
    if not text:
        return []
    parts = SENTENCE_SPLIT_RE.split(text.strip())
    return [p.strip() for p in parts if p and len(p.strip()) >= 3]


def detect_encoding(data: bytes) -> str:
    """Определяет кодировку байтового содержимого.

    Сначала пробуется BOM/строгие декодирования, затем chardet (если установлен),
    затем список кодировок по умолчанию.

    :param data: сырые байты файла.
    :return: имя кодировки, например ``"utf-8"`` или ``"cp1251"``.
    """
    if not data:
        return "utf-8"
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        import chardet  # локальный импорт: зависимость опциональна

        guess = chardet.detect(data[:65536]) or {}
        enc = (guess.get("encoding") or "").lower()
        conf = guess.get("confidence") or 0.0
        if enc and conf >= 0.6:
            return enc
    except Exception:
        pass
    for enc in DEFAULT_ENCODINGS[1:]:
        try:
            data.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1"  # latin-1 декодирует любые байты


def decode_bytes(data: bytes, encoding: str | None = None) -> str:
    """Декодирует байты в строку; при неудаче — с заменой недопустимых последовательностей."""
    enc = encoding or detect_encoding(data)
    try:
        return data.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return data.decode("utf-8", errors="replace")


def human_size(num: float) -> str:
    """Форматирует размер в байтах человекочитаемо (КБ, МБ, ГБ)."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= step
    return f"{num:.1f} PB"
