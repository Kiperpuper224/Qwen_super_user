"""search_engine.py — поисковый движок: разбор запроса, ранжирование, сниппеты.

Поддерживаемый синтаксис запроса (операторы можно комбинировать):

* ``слово1 слово2``          — поиск по точным словам (И, BM25-ранжирование);
* ``"точная фраза"``         — фразовый поиск (проверка соседства слов в индексе);
* ``regex:..\\w+ing``         — поиск по регулярному выражению;
* ``ext:pdf``                — фильтр по расширению;
* ``name:отчёт``             — фильтр по имени файла (подстрока);
* ``author:дос``             — фильтр по автору/заголовку;
* ``after:2024-01-01`` / ``before:2025-06-30`` — фильтр по дате изменения;
* ``minsize:1kb`` / ``maxsize:2mb``            — фильтр по размеру;
* ``-термин``                — исключить документы, содержащие термин.

Пример::

    engine = SearchEngine(index)
    resp = engine.search('python "data science" ext:pdf -draft', limit=10)
    for hit in resp.hits:
        print(hit.title, hit.score, hit.snippet_html)
"""

from __future__ import annotations

import html
import math
import os
import re
# shlex не используется: posix-режим терял обратные слеши в regex:
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .exceptions import QueryParseError
from .indexer import Index
from .utils import human_size, split_sentences, tokenize

#: Параметры BM25 (настраиваются на уровне SearchEngine).
DEFAULT_K1 = 1.5
DEFAULT_B = 0.75

#: Операторы вида key:value.
FILTER_KEYS = {"ext", "name", "author", "after", "before", "minsize", "maxsize"}

_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}


@dataclass
class ParsedQuery:
    """Результат разбора строки запроса на составляющие."""

    terms: List[str] = field(default_factory=list)          # обычные слова
    phrases: List[List[str]] = field(default_factory=list)  # фразы (списки токенов)
    regexes: List[str] = field(default_factory=list)        # регулярные выражения
    excludes: List[str] = field(default_factory=list)       # термины-исключения
    filters: Dict[str, str] = field(default_factory=dict)   # ext:, name:, ...
    raw: str = ""

    def is_empty(self) -> bool:
        return not (self.terms or self.phrases or self.regexes)


@dataclass
class SearchResult:
    """Одна выдача: документ + релевантность + сниппет."""

    doc_id: int
    meta: Dict[str, Any]
    score: float
    snippet: str          # чистый текст сниппета
    snippet_html: str     # сниппет с <mark> подсветкой (уже экранирован)
    matched_sentence: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-представление для REST API."""
        m = dict(self.meta)
        for key in ("created", "modified"):
            ts = m.get(key)
            if isinstance(ts, (int, float)):
                m[key + "_iso"] = datetime.fromtimestamp(
                    ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        m["size_human"] = human_size(m.get("size", 0))
        return {
            "doc_id": self.doc_id,
            "score": round(self.score, 3),
            "snippet": self.snippet,
            "snippet_html": self.snippet_html,
            "matched_sentence": self.matched_sentence,
            "meta": m,
        }


@dataclass
class SearchResponse:
    """Ответ поиска: список выдач + статистика."""

    query: str
    hits: List[SearchResult]
    total_found: int
    elapsed_ms: float
    parsed: ParsedQuery

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "total_found": self.total_found,
            "hits": [h.to_dict() for h in self.hits],
        }


def _parse_date(value: str) -> float:
    """Разбирает дату формата YYYY-MM-DD в unix timestamp."""
    try:
        dt = datetime.strptime(value.strip(), "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError as exc:
        raise QueryParseError(f"Некорректная дата '{value}', ожидалось ГГГГ-ММ-ДД") from exc


def _parse_size(value: str) -> int:
    """Разбирает размер вида '500kb'/'2mb'/'1024' в байты."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]{0,2})\s*", value)
    if not m:
        raise QueryParseError(f"Некорректный размер '{value}'")
    num = float(m.group(1))
    unit = (m.group(2) or "b").lower()
    if unit not in _SIZE_UNITS:
        raise QueryParseError(f"Неизвестная единица размера '{unit}'")
    return int(num * _SIZE_UNITS[unit])


def parse_query(query: str) -> ParsedQuery:
    """Разбирает строку запроса на термины, фразы, regex и фильтры.

    Двойные кавычки сохраняются как есть (внутри могут быть регулярные
    выражения с обратными слэшами); одинарные кавычки — для фраз.

    :raises QueryParseError: при незакрытой кавычке или неверном значении фильтра.
    """
    pq = ParsedQuery(raw=query)

    # 1. Фразы в двойных кавычках извлекаем напрямую из исходной строки,
    #    чтобы posix-режим shlex не схлопнул обратные слеши внутри них.
    masked = query
    for m in re.finditer(r'"([^"]+)"', query):
        inner = m.group(1)
        tokens = tokenize(inner)
        if len(tokens) > 1:
            pq.phrases.append(tokens)
        elif tokens:
            pq.terms.extend(tokens)
        masked = masked.replace(m.group(0), " ", 1)

    # 2. Остаток разбираем токенайзером по пробелам (shlex не используем, чтобы
    #    posix-режим не съедал обратные слеши в regex: и одиночных кавычках).
    for part in masked.split():
        low = part.lower()
        if ":" in part and low.split(":", 1)[0] in FILTER_KEYS | {"regex"}:
            key, value = part.split(":", 1)
            key = key.lower()
            if key == "regex":
                try:
                    re.compile(value)
                except re.error as exc:
                    raise QueryParseError(f"Некорректное регулярное выражение: {exc}") from exc
                pq.regexes.append(value)
            else:
                pq.filters[key] = value
            continue
        tokens = tokenize(part)
        if not tokens:
            continue
        if low.startswith("-") and len(part) > 1:
            pq.excludes.extend(tokens)
        else:
            pq.terms.extend(tokens)

    # 3. Незакрытая двойная кавычка — явная ошибка разбора.
    if masked.count('"') % 2 == 1:
        raise QueryParseError("Незакрытая кавычка в запросе")
    return pq


class SearchEngine:
    """Поисковый движок поверх :class:`Index`: BM25, фразы, regex, фильтры, сниппеты."""

    def __init__(self, index: Optional[Index] = None, k1: float = DEFAULT_K1,
                 b: float = DEFAULT_B) -> None:
        self.index = index if index is not None else Index()
        self.k1 = k1
        self.b = b

    # ------------------------------------------------------------------ #
    # Ядро поиска
    # ------------------------------------------------------------------ #
    def search(self, query: str, limit: int = 20, offset: int = 0) -> SearchResponse:
        """Выполняет поиск и возвращает отранжированные результаты со сниппетами."""
        t0 = time.perf_counter()
        pq = parse_query(query)
        idx = self.index

        if pq.is_empty():
            return SearchResponse(query, [], 0,
                                  (time.perf_counter() - t0) * 1000.0, pq)

        candidate_scores: Dict[int, float] = {}

        # --- 1. Терминный поиск (BM25) ---------------------------------
        N = max(idx.doc_count, 1)
        avg_len = (sum(idx.doc_lengths.values()) / N) if idx.doc_lengths else 1.0
        for term in set(pq.terms):
            postings = idx.get_postings(term)
            df = len(postings)
            if df == 0:
                candidate_scores = {}  # И-семантика: термина нет нигде
                break
            idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
            for doc_id in postings:
                tf = idx.term_freqs.get(doc_id, {}).get(term, 0)
                dl = idx.doc_lengths.get(doc_id, 1)
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * dl / avg_len)
                candidate_scores[doc_id] = candidate_scores.get(doc_id, 0.0) + idf * \
                    (numerator / denominator)

        # Если среди терминов есть отсутствующие — совпадений нет вовсе.
        if pq.terms and not candidate_scores:
            return SearchResponse(query, [], 0,
                                  (time.perf_counter() - t0) * 1000.0, pq)

        # --- 2. Фразовый поиск ------------------------------------------
        phrase_positions: Dict[int, int] = {}
        for words in pq.phrases:
            matches = idx.phrase_docs(words)
            docs_in_phrase = {d for d, _ in matches}
            if candidate_scores:
                allowed = set(candidate_scores) & docs_in_phrase
            else:
                allowed = docs_in_phrase
            if not allowed:
                return SearchResponse(query, [], 0,
                                      (time.perf_counter() - t0) * 1000.0, pq)
            for did, pos in matches:
                if did in allowed:
                    phrase_positions[did] = min(phrase_positions.get(did, pos), pos)
            candidate_scores = {d: s for d, s in candidate_scores.items() if d in allowed} \
                if candidate_scores else {d: 1.0 for d in allowed}
            for d in allowed:
                candidate_scores.setdefault(d, 1.0)
                candidate_scores[d] += 2.0  # буст за точную фразу

        # --- 3. Regex-поиск ---------------------------------------------
        if pq.regexes:
            compiled = [re.compile(rx, re.IGNORECASE) for rx in pq.regexes]
            matched_terms: Set[str] = set()
            for pattern in compiled:
                for term in idx.inverted_index.keys():
                    if pattern.search(term):
                        matched_terms.add(term)
            regex_docs: Set[int] = set()
            for term in matched_terms:
                regex_docs.update(idx.get_postings(term).keys())
            if candidate_scores:
                candidate_scores = {d: s for d, s in candidate_scores.items()
                                    if d in regex_docs}
            else:
                candidate_scores = {d: 1.0 for d in regex_docs}
            if not candidate_scores:
                return SearchResponse(query, [], 0,
                                      (time.perf_counter() - t0) * 1000.0, pq)

        # --- 4. Исключения (-term) --------------------------------------
        for term in pq.excludes:
            excluded = set(idx.get_postings(term).keys())
            candidate_scores = {d: s for d, s in candidate_scores.items()
                                if d not in excluded}

        # --- 5. Фильтры по метаданным -----------------------------------
        filtered = []
        for doc_id, score in candidate_scores.items():
            meta = idx.documents.get(doc_id, {})
            if self._passes_filters(meta, pq.filters):
                filtered.append((doc_id, score))

        filtered.sort(key=lambda x: (-x[1], x[0]))
        total_found = len(filtered)
        page = filtered[offset:offset + max(1, limit)]

        # --- 6. Сниппеты --------------------------------------------------
        highlights = list(set(pq.terms))
        for words in pq.phrases:
            highlights.extend(words)
        results = [self._build_result(doc_id, score, highlights, pq)
                   for doc_id, score in page]

        elapsed = (time.perf_counter() - t0) * 1000.0
        return SearchResponse(query, results, total_found, elapsed, pq)

    @staticmethod
    def _passes_filters(meta: Dict[str, Any], filters: Dict[str, str]) -> bool:
        """Проверяет соответствие метаданных фильтрам ext/name/author/dates/size."""
        for key, value in filters.items():
            v = value.lower()
            if key == "ext":
                if str(meta.get("extension", "")).lower() != v.lstrip("."):
                    return False
            elif key == "name":
                if v not in str(meta.get("name", "")).lower():
                    return False
            elif key == "author":
                hay = f"{meta.get('author') or ''} {meta.get('title') or ''}".lower()
                if v not in hay:
                    return False
            elif key == "after":
                if float(meta.get("modified", 0)) < _parse_date(value):
                    return False
            elif key == "before":
                if float(meta.get("modified", 0)) > _parse_date(value):
                    return False
            elif key == "minsize":
                if int(meta.get("size", 0)) < _parse_size(value):
                    return False
            elif key == "maxsize":
                if int(meta.get("size", 0)) > _parse_size(value):
                    return False
        return True

    # ------------------------------------------------------------------ #
    # Сниппеты и подсветка
    # ------------------------------------------------------------------ #
    def _build_result(self, doc_id: int, score: float,
                      highlight_words: List[str], pq: ParsedQuery) -> SearchResult:
        """Формирует одну выдачу: выбирает лучшее предложение/окно текста, делает сниппет."""
        text = self.index.doc_text.get(doc_id, "")
        meta = self.index.documents.get(doc_id, {})

        # Ключевая подстрока для «входящих предложений»: первая фраза или первое слово.
        needle = " ".join(pq.phrases[0]) if pq.phrases else (
            pq.terms[0] if pq.terms else (pq.regexes[0] if pq.regexes else ""))
        needle_clean = needle.removeprefix("regex:")

        sentences = self.index.sentence_matches(doc_id, needle_clean) if needle_clean else []
        best_sentence = sentences[0] if sentences else None

        if best_sentence:
            window = best_sentence
        else:
            window = self._context_window(text, highlight_words, pq)

        snippet = window if len(window) <= 300 else window[:300].rsplit(" ", 1)[0] + "…"
        snippet_html = highlight_snippet(snippet, highlight_words, pq)
        return SearchResult(doc_id=doc_id, meta=meta, score=score,
                            snippet=snippet, snippet_html=snippet_html,
                            matched_sentence=best_sentence)

    def _context_window(self, text: str, words: List[str],
                        pq: ParsedQuery) -> str:
        """Окно ±120 символов вокруг первого вхождения ключевого слова/regex."""
        lowered = text.lower()
        best_pos = -1
        for w in words:
            pos = lowered.find(w.lower())
            if pos != -1 and (best_pos == -1 or pos < best_pos):
                best_pos = pos
        if best_pos == -1:
            for rx in pq.regexes:
                try:
                    m = re.search(rx, text, re.IGNORECASE)
                except re.error:
                    continue
                if m:
                    best_pos = m.start()
                    break
        if best_pos == -1:
            return text[:300].strip()
        start = max(0, best_pos - 120)
        end = min(len(text), best_pos + 180)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return prefix + text[start:end].strip() + suffix


def highlight_snippet(snippet: str, words: List[str], pq: ParsedQuery) -> str:
    """Экранирует HTML и оборачивает найденные слова/regex в ``<mark>``."""
    escaped = html.escape(snippet)
    patterns: List[str] = [re.escape(w) for w in words if w]
    patterns += [rx for rx in getattr(pq, "regexes", [])]
    if not patterns:
        return escaped
    combined = "|".join(f"(?:{p})" for p in patterns)
    try:
        return re.sub(combined, lambda m: f"<mark>{m.group(0)}</mark>",
                      escaped, flags=re.IGNORECASE)
    except re.error:
        return escaped


# ---------------------------------------------------------------------- #
# Обработка файлов вне индекса (просмотр содержимого, «открыть как есть»)
# ---------------------------------------------------------------------- #
def read_document_text(engine: "SearchEngine", doc_id: int,
                       max_chars: int = 100_000) -> Optional[str]:
    """Возвращает сохранённый текст документа по doc_id (для страницы предпросмотра)."""
    text = engine.index.doc_text.get(doc_id)
    if text is None:
        return None
    return text[:max_chars]
