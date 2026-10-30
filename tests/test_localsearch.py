"""Интеграционные тесты библиотеки LocalSearch.

Запуск: ``pytest tests/ -v`` (нужен установленный pytest).
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from localsearch import (Crawler, Index, LocalSearch, QueryParseError,
                         SearchEngine, parse_query)


@pytest.fixture()
def docs_dir(tmp_path):
    """Создаёт набор тестовых файлов разных форматов."""
    (tmp_path / "a.txt").write_text(
        "Machine Learning Notes\n\n"
        "Python builds an inverted index for full text search. "
        "Machine learning models need data science skills.\n",
        encoding="utf-8")
    (tmp_path / "b.txt").write_text(
        "Черновик отчёта. Черновик черновик. Здесь нет полезного текста про python.",
        encoding="utf-8")
    html = tmp_path / "c.html"
    html.write_bytes(("<html><head><title>Поиск по документам</title>"
                      "<meta name='author' content='Иван Петров'></head>"
                      "<body><h1>Привет</h1><p>Python и полнотекстовый поиск. "
                      "Индексирование документов выполняется быстро.</p></body></html>"
                      ).encode("utf-8"))
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.txt").write_text("Deep file about indexing and crawling algorithms.",
                               encoding="utf-8")
    return str(tmp_path)


def test_tokenize_and_encoding():
    from localsearch.utils import detect_encoding, tokenize
    assert tokenize("Hello, World!") == ["hello", "world"]
    cp1251 = "Привет мир".encode("cp1251")
    assert detect_encoding(cp1251) in ("cp1251", "koi8-r", "windows-1251")


def test_parse_query_operators():
    pq = parse_query('"data science" python ext:pdf after:2024-01-01 -draft regex:\\w+ing')
    assert pq.phrases == [["data", "science"]]
    assert pq.terms == ["python"]
    assert pq.filters["ext"] == "pdf"
    assert pq.excludes == ["draft"]
    assert pq.regexes == [r"\w+ing"]


def test_bad_quotes_raise():
    with pytest.raises(QueryParseError):
        parse_query('"незакрытая фраза')


def test_index_save_load_sqlite_json(tmp_path):
    idx = Index()
    idx.add_document({"path": "x.txt", "extension": "txt", "size": 10,
                      "modified": time.time()}, "alpha beta alpha gamma")
    assert idx.get_postings("alpha")[1] == [0, 2]
    db = str(tmp_path / "i.db")
    idx.save(db)
    loaded = Index.load(db)
    assert loaded.doc_count == 1
    assert loaded.get_postings("beta") == {1: [1]}
    js = str(tmp_path / "i.json")
    idx.save(js)
    loaded2 = Index.load(js)
    assert loaded2.doc_count == 1


def test_crawler_and_search(docs_dir, tmp_path):
    idx = Index()
    crawler = Crawler(idx, max_depth=3)
    events = []
    crawler.on_progress = lambda p: events.append(p.status)
    progress = crawler.crawl_directory(docs_dir)
    assert progress.indexed >= 4
    assert progress.status == "done"
    assert "running" in events

    engine = SearchEngine(idx)
    resp = engine.search("python")
    assert resp.total_found >= 2
    assert "<mark>" in resp.hits[0].snippet_html

    phrase = engine.search('"machine learning"')
    assert phrase.total_found == 1
    assert "Machine Learning" in phrase.hits[0].matched_sentence

    excluded = engine.search("python -черновик")
    names = [os.path.basename(h.meta["path"]) for h in excluded.hits]
    assert "b.txt" not in names

    filtered = engine.search("поиск ext:html")
    assert filtered.total_found == 1
    meta = filtered.hits[0].meta
    assert meta["author"] == "Иван Петров"
    assert meta["title"] == "Поиск по документам"

    rx = engine.search(r"regex:index\w+")
    assert rx.total_found >= 1


def test_size_and_ext_filters(docs_dir):
    idx = Index()
    crawler = Crawler(idx, extensions=["txt"], min_size_bytes=50)
    crawler.crawl_directory(docs_dir)
    exts = {m["extension"] for m in idx.documents.values()}
    assert exts == {"txt"}
    assert all(m["size"] >= 50 for m in idx.documents.values())


def test_facade_and_autosave(docs_dir, tmp_path):
    db = str(tmp_path / "facade.db")
    ls = LocalSearch(index_path=db, autosave=True)
    ls.crawl(docs_dir, depth=2)
    assert os.path.exists(db)
    resp = ls.search("indexing")
    assert resp.total_found >= 1
    # Пересоздание фасада должно подхватить сохранённый индекс.
    ls2 = LocalSearch(index_path=db)
    assert ls2.index.doc_count == ls.index.doc_count


def test_web_ui_flask_smoke(docs_dir, tmp_path):
    pytest.importorskip("flask")
    from localsearch.app import create_flask_app

    ls = LocalSearch(index_path=None)
    ls.crawl(docs_dir)
    app = create_flask_app(ls)
    client = app.test_client()
    assert client.get("/").status_code == 200
    r = client.get("/search?q=python")
    assert r.status_code == 200 and b"<mark>" in r.data
    status = client.get("/api/status").get_json()
    assert status["stats"]["documents"] >= 4
    saved = client.post("/api/save").get_json()
    assert os.path.exists(saved["path"])
    stop_r = client.post("/api/crawl/stop")
    assert stop_r.status_code == 200
