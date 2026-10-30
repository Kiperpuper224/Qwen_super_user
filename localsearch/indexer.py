"""indexer.py — построение и хранение инвертированного индекса.

Модуль реализует класс :class:`Index`, который:

* хранит документы (метаданные + полный текст) и их предложения;
* строит инвертированный индекс «термин -> [(doc_id, позиция), ...]» для
  быстрого поиска по отдельным словам и фразам (проверка соседства позиций);
* поддерживает индексацию предложений (по doc_id сохраняется список токенов
  каждого предложения), что позволяет искать «входящие предложения»;
* умеет сохранять/загружать состояние в SQLite (основной формат) или JSON;
* считает частоты терминов и документную длину для BM25-ранжирования
  (ранжирование выполняет search_engine.py).

Пример::

    idx = Index()
    idx.add_document({"path": "a.txt", "name": "a.txt", "extension": "txt"},
                     "Hello world. Hello again.")
    idx.save("index.db")            # SQLite
    idx2 = Index.load("index.db")   # загрузка
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .exceptions import IndexError as LocalSearchIndexError
from .utils import split_sentences, tokenize

logger = logging.getLogger(__name__)

#: Версия схемы хранения — для совместимости будущих релизов.
SCHEMA_VERSION = 1


class Index:
    """Инвертированный индекс документов с поддержкой SQLite/JSON-хранилища."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        #: doc_id -> метаданные документа (path, name, size, mtime, ext, encoding, ...)
        self.documents: Dict[int, Dict[str, Any]] = {}
        #: doc_id -> полный извлечённый текст
        self.doc_text: Dict[int, str] = {}
        #: doc_id -> список предложений (исходный текст каждого предложения)
        self.doc_sentences: Dict[int, List[str]] = {}
        #: термин -> {doc_id: [позиции вхождений]}
        self.inverted_index: Dict[str, Dict[int, List[int]]] = {}
        #: doc_id -> длина документа в токенах (для BM25)
        self.doc_lengths: Dict[int, int] = {}
        #: doc_id -> {термин: частота} (для BM25)
        self.term_freqs: Dict[int, Dict[str, int]] = {}
        self._next_id: int = 1

    # ------------------------------------------------------------------ #
    # Построение индекса
    # ------------------------------------------------------------------ #
    @property
    def doc_count(self) -> int:
        """Число проиндексированных документов."""
        return len(self.documents)

    def add_document(self, meta: Dict[str, Any], text: str,
                     doc_id: Optional[int] = None) -> int:
        """Индексирует один документ.

        :param meta: словарь метаданных (обязателен ключ ``path`` или ``url``).
        :param text: извлечённый текст документа.
        :param doc_id: явный идентификатор (используется при загрузке из хранилища).
        :return: присвоенный doc_id.
        """
        with self._lock:
            if doc_id is None:
                doc_id = self._next_id
                self._next_id += 1
            else:
                self._next_id = max(self._next_id, doc_id + 1)

            text = text or ""
            self.documents[doc_id] = dict(meta)
            self.doc_text[doc_id] = text
            self.doc_sentences[doc_id] = split_sentences(text)

            tokens = tokenize(text)
            postings: Dict[int, List[int]] = {}
            freqs: Dict[str, int] = {}
            for pos, tok in enumerate(tokens):
                postings.setdefault(tok, []).append(pos)
                freqs[tok] = freqs.get(tok, 0) + 1
            for term, positions in postings.items():
                self.inverted_index.setdefault(term, {})[doc_id] = positions
            self.doc_lengths[doc_id] = len(tokens)
            self.term_freqs[doc_id] = freqs
            return doc_id

    def remove_by_path(self, path: str) -> bool:
        """Удаляет документ по пути/URL (используется при re-crawl изменённых файлов)."""
        with self._lock:
            for did, meta in list(self.documents.items()):
                if meta.get("path") == path or meta.get("url") == path:
                    del self.documents[did]
                    self.doc_text.pop(did, None)
                    self.doc_sentences.pop(did, None)
                    self.doc_lengths.pop(did, None)
                    self.term_freqs.pop(did, None)
                    for term_postings in self.inverted_index.values():
                        term_postings.pop(did, None)
                    self.inverted_index = {t: p for t, p in self.inverted_index.items() if p}
                    return True
            return False

    def clear(self) -> None:
        """Полностью очищает индекс (без сброса блокировки)."""
        with self._lock:
            self.clear_tmp_state()

    # ------------------------------------------------------------------ #
    # Запросы к индексу
    # ------------------------------------------------------------------ #
    def get_postings(self, term: str) -> Dict[int, List[int]]:
        """Возвращает {doc_id: [позиции]} для термина (в нижнем регистре)."""
        return self.inverted_index.get(term.lower(), {})

    def phrase_docs(self, words: Iterable[str]) -> List[Tuple[int, int]]:
        """Находит документы, содержащие фразу из слов как последовательность.

        :return: список пар ``(doc_id, позиция_начала_фразы)``.
        """
        words = [w.lower() for w in words if w]
        if not words:
            return []
        with self._lock:
            first = self.get_postings(words[0])
            results: List[Tuple[int, int]] = []
            for doc_id, start_positions in first.items():
                for pos in start_positions:
                    ok = True
                    for offset, word in enumerate(words[1:], start=1):
                        positions = self.inverted_index.get(word, {}).get(doc_id)
                        if positions is None or (pos + offset) not in set(positions):
                            ok = False
                            break
                    if ok:
                        results.append((doc_id, pos))
            return results

    def sentence_matches(self, doc_id: int, needle: str) -> List[str]:
        """Возвращает предложения документа, содержащие подстроку ``needle`` (без учёта регистра)."""
        needle_l = needle.lower()
        out = []
        for sent in self.doc_sentences.get(doc_id, []):
            if needle_l in sent.lower():
                out.append(sent)
        return out

    def stats(self) -> Dict[str, Any]:
        """Сводная статистика индекса (для отладки и API)."""
        with self._lock:
            avg_len = (sum(self.doc_lengths.values()) / len(self.doc_lengths)
                       if self.doc_lengths else 0.0)
            return {
                "documents": self.doc_count,
                "terms": len(self.inverted_index),
                "postings": sum(len(p) for p in self.inverted_index.values()),
                "avg_doc_length": round(avg_len, 1),
            }

    # ------------------------------------------------------------------ #
    # Сохранение/загрузка: SQLite
    # ------------------------------------------------------------------ #
    _SQL_SCHEMA = """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS documents (
        doc_id INTEGER PRIMARY KEY,
        meta TEXT NOT NULL,
        text TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS postings (
        term TEXT NOT NULL,
        doc_id INTEGER NOT NULL,
        positions TEXT NOT NULL,
        PRIMARY KEY (term, doc_id)
    );
    CREATE INDEX IF NOT EXISTS idx_postings_term ON postings(term);
    """

    def save_sqlite(self, db_path: str) -> None:
        """Сохраняет индекс в файл SQLite (атомарно через временную БД)."""
        try:
            tmp_path = db_path + ".tmp"
            conn = sqlite3.connect(tmp_path)
            try:
                conn.executescript(self._SQL_SCHEMA)
                conn.execute("DELETE FROM documents")
                conn.execute("DELETE FROM postings")
                conn.execute("DELETE FROM meta")
                with self._lock:
                    conn.execute(
                        "INSERT INTO meta VALUES ('version', ?)", (str(SCHEMA_VERSION),))
                    conn.execute("INSERT INTO meta VALUES ('next_id', ?)",
                                 (str(self._next_id),))
                    conn.executemany(
                        "INSERT INTO documents VALUES (?, ?, ?)",
                        [(did, json.dumps(m, ensure_ascii=False), self.doc_text[did])
                         for did, m in self.documents.items()],
                    )
                    rows = []
                    for term, docs in self.inverted_index.items():
                        for did, positions in docs.items():
                            rows.append((term, did, json.dumps(positions)))
                    conn.executemany("INSERT INTO postings VALUES (?, ?, ?)", rows)
                    conn.commit()
            finally:
                conn.close()
            os.replace(tmp_path, db_path)
            logger.info("Индекс сохранён в SQLite: %s (%d документов)", db_path, self.doc_count)
        except sqlite3.Error as exc:
            raise LocalSearchIndexError(f"Ошибка сохранения SQLite-индекса: {exc}") from exc

    def load_sqlite(self, db_path: str) -> "Index":
        """Загружает индекс из файла SQLite (возвращает self для цепочки вызовов)."""
        if not os.path.exists(db_path):
            raise LocalSearchIndexError(f"Файл индекса не найден: {db_path}")
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                version = conn.execute(
                    "SELECT value FROM meta WHERE key='version'").fetchone()
                if version and int(version[0]) > SCHEMA_VERSION:
                    raise LocalSearchIndexError(
                        f"Версия индекса {version[0]} новее поддерживаемой {SCHEMA_VERSION}")
                with self._lock:
                    self.clear_tmp_state()
                    nxt = conn.execute("SELECT value FROM meta WHERE key='next_id'").fetchone()
                    self._next_id = int(nxt[0]) if nxt else 1
                    for did, meta_json, text in conn.execute(
                            "SELECT doc_id, meta, text FROM documents"):
                        self.documents[did] = json.loads(meta_json)
                        self.doc_text[did] = text
                        self.doc_sentences[did] = split_sentences(text)
                    for term, did, pos_json in conn.execute(
                            "SELECT term, doc_id, positions FROM postings"):
                        self.inverted_index.setdefault(term, {})[did] = json.loads(pos_json)
                    # Восстанавливаем производные структуры из текстов.
                    for did, text in self.doc_text.items():
                        tokens = tokenize(text)
                        freqs: Dict[str, int] = {}
                        for tok in tokens:
                            freqs[tok] = freqs.get(tok, 0) + 1
                        self.doc_lengths[did] = len(tokens)
                        self.term_freqs[did] = freqs
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise LocalSearchIndexError(f"Ошибка загрузки SQLite-индекса: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LocalSearchIndexError(f"Повреждённые данные индекса: {exc}") from exc
        logger.info("Индекс загружен из SQLite: %s (%d документов)", db_path, self.doc_count)
        return self

    def clear_tmp_state(self) -> None:
        """Сбрасывает внутреннее состояние (используется при загрузке)."""
        self.documents.clear()
        self.doc_text.clear()
        self.doc_sentences.clear()
        self.inverted_index.clear()
        self.doc_lengths.clear()
        self.term_freqs.clear()
        self._next_id = 1

    # ------------------------------------------------------------------ #
    # Сохранение/загрузка: JSON
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        """Сериализует индекс в plain-dict (для JSON)."""
        with self._lock:
            return {
                "version": SCHEMA_VERSION,
                "next_id": self._next_id,
                "documents": {str(k): v for k, v in self.documents.items()},
                "texts": {str(k): v for k, v in self.doc_text.items()},
                "inverted_index": {
                    term: {str(did): pos for did, pos in docs.items()}
                    for term, docs in self.inverted_index.items()
                },
            }

    def save_json(self, json_path: str) -> None:
        """Сохраняет индекс в JSON-файл."""
        try:
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, ensure_ascii=False)
            logger.info("Индекс сохранён в JSON: %s", json_path)
        except OSError as exc:
            raise LocalSearchIndexError(f"Ошибка сохранения JSON-индекса: {exc}") from exc

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Index":
        """Восстанавливает индекс из словаря, полученного :meth:`to_dict`."""
        idx = cls()
        version = int(data.get("version", 1))
        if version > SCHEMA_VERSION:
            raise LocalSearchIndexError(
                f"Версия индекса {version} новее поддерживаемой {SCHEMA_VERSION}")
        idx._next_id = int(data.get("next_id", 1))
        for did_s, meta in data.get("documents", {}).items():
            did = int(did_s)
            text = data.get("texts", {}).get(did_s, "")
            idx.documents[did] = meta
            idx.doc_text[did] = text
            idx.doc_sentences[did] = split_sentences(text)
            tokens = tokenize(text)
            freqs: Dict[str, int] = {}
            for tok in tokens:
                freqs[tok] = freqs.get(tok, 0) + 1
            idx.doc_lengths[did] = len(tokens)
            idx.term_freqs[did] = freqs
        for term, docs in data.get("inverted_index", {}).items():
            idx.inverted_index[term] = {int(did): pos for did, pos in docs.items()}
        return idx

    def load_json(self, json_path: str) -> "Index":
        """Загружает индекс из JSON-файла (заполняет self)."""
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError as exc:
            raise LocalSearchIndexError(f"Файл индекса не найден: {json_path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalSearchIndexError(f"Ошибка чтения JSON-индекса: {exc}") from exc
        restored = self.from_dict(data)
        with self._lock:
            self.clear_tmp_state()
            self.__dict__.update(restored.__dict__)
        logger.info("Индекс загружен из JSON: %s (%d документов)", json_path, self.doc_count)
        return self

    # ------------------------------------------------------------------ #
    # Универсальный интерфейс
    # ------------------------------------------------------------------ #
    def save(self, path: str) -> None:
        """Сохраняет индекс; формат определяется по расширению (.db/.sqlite/.json)."""
        if path.lower().endswith(".json"):
            self.save_json(path)
        else:
            self.save_sqlite(path)

    @classmethod
    def load(cls, path: str) -> "Index":
        """Статическая загрузка индекса из файла в новый объект."""
        idx = cls()
        if path.lower().endswith(".json"):
            return idx.load_json(path)
        return idx.load_sqlite(path)
