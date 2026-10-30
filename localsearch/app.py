"""app.py — веб-интерфейс LocalSearch (Flask или FastAPI на выбор).

Запуск из командной строки::

    python -m localsearch.app                       # FastAPI по умолчанию, порт 8000
    python -m localsearch.app --backend flask --port 5000
    python -m localsearch.app --index my.db --crawl-root /home/user/docs

Страницы и API:

* ``GET  /``                — главная со строкой поиска;
* ``GET  /search?q=...``    — результаты (сниппеты с подсветкой, пагинация);
* ``GET  /doc/<id>``        — метаданные и извлечённый текст документа;
* ``POST /api/crawl``       — запуск фоновой индексации {target, depth};
* ``GET  /api/status``      — прогресс краулера для JS-индикатора;
* ``POST /api/crawl/stop``  — остановка обхода;
* ``POST /api/save``        — сохранение индекса на диск;
* ``GET  /api/search``      — поиск в JSON (для внешних клиентов).

Модуль также экспортирует фабрику :func:`create_app`, удобную для тестов
и встраивания (``from localsearch.app import create_app``).
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import Optional

from . import __version__
from .api import LocalSearch
from .exceptions import LocalSearchError, QueryParseError
from .search_engine import read_document_text
from .utils import human_size

logger = logging.getLogger(__name__)

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(PKG_DIR, "templates")
STATIC_DIR = os.path.join(PKG_DIR, "static")
PER_PAGE = 10


def _make_service(index_path: Optional[str], crawl_root: Optional[str]) -> LocalSearch:
    """Создаёт сервис; при указанном --crawl-root сразу запускает первую индексацию."""
    ls = LocalSearch(index_path=index_path or "localsearch_index.db", autosave=True)
    if crawl_root:
        logger.info("Первичная индексация %s ...", crawl_root)
        ls.crawl_async(crawl_root)
    return ls


# ---------------------------------------------------------------------- #
# Flask-приложение
# ---------------------------------------------------------------------- #
def create_flask_app(service: LocalSearch):
    """Фабрика Flask-приложения поверх готового сервиса :class:`LocalSearch`."""
    from flask import Flask, jsonify, render_template, request

    app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)

    def _search_context(q: str, page: int):
        ctx = {"query": q, "version": __version__,
               "stats": service.stats(), "elapsed_ms": None,
               "hits": [], "total_found": 0, "page": page, "per_page": PER_PAGE,
               "error": None}
        if not q:
            return ctx
        try:
            resp = service.search(q, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
        except QueryParseError as exc:
            ctx["error"] = f"Ошибка запроса: {exc}"
            return ctx
        ctx["elapsed_ms"] = resp.elapsed_ms
        ctx["total_found"] = resp.total_found
        ctx["hits"] = [h.to_dict() for h in resp.hits]
        for h in ctx["hits"]:
            h["snippet_html"] = h["snippet_html"]  # уже экранирован движком
        return ctx

    @app.route("/")
    def index():
        return render_template("index.html", **_search_context("", 1))

    @app.route("/search")
    def search():
        q = request.args.get("q", "").strip()
        page = max(1, int(request.args.get("page", 1) or 1))
        return render_template("index.html", **_search_context(q, page))

    @app.route("/doc/<int:doc_id>")
    def doc_view(doc_id: int):
        meta = service.index.documents.get(doc_id)
        if not meta:
            return "Документ не найден", 404
        d = dict(meta)
        for key in ("created", "modified"):
            import datetime
            ts = d.get(key)
            if isinstance(ts, (int, float)):
                d[key + "_iso"] = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        d["size_human"] = human_size(d.get("size", 0))
        text = read_document_text(service.engine, doc_id)
        return render_template("doc.html", doc=d, text=text)

    @app.get("/api/status")
    def api_status():
        return jsonify({"progress": service.crawl_progress.to_dict(),
                        "stats": service.stats()})

    @app.post("/api/crawl")
    def api_crawl():
        data = request.get_json(silent=True) or {}
        target = (data.get("target") or "").strip()
        if not target:
            return jsonify({"error": "Не указан target"}), 400
        depth = data.get("depth")
        started = service.crawl_async(target, depth=depth)
        if not started:
            return jsonify({"error": "Краулер уже запущен"}), 409
        return jsonify({"ok": True})

    @app.post("/api/crawl/stop")
    def api_crawl_stop():
        service.stop_crawl()
        return jsonify({"ok": True})

    @app.post("/api/save")
    def api_save():
        try:
            path = service.save()
            return jsonify({"ok": True, "path": path})
        except LocalSearchError as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/search")
    def api_search():
        q = request.args.get("q", "").strip()
        try:
            limit = min(100, max(1, int(request.args.get("limit", 20))))
            resp = service.search(q, limit=limit)
        except QueryParseError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(resp.to_dict())

    return app


# ---------------------------------------------------------------------- #
# FastAPI-приложение
# ---------------------------------------------------------------------- #
def create_fastapi_app(service: LocalSearch):
    """Фабрика FastAPI-приложения поверх готового сервиса :class:`LocalSearch`."""
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
    from jinja2 import Environment, FileSystemLoader

    from markupsafe import Markup

    app = FastAPI(title="LocalSearch", version=__version__)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    # snippet_html генерируется движком (html.escape + <mark>) — помечаем его safe,
    # чтобы Jinja не экранировала подсветку повторно.
    env.filters["safe"] = lambda s: Markup(s) if not hasattr(s, "__html__") else s

    class CrawlRequest(BaseModel):
        target: str
        depth: Optional[int] = None

    def _render(name: str, context: dict) -> HTMLResponse:
        tmpl = env.get_template(name)
        # В шаблонах используется Jinja-синтаксис {{ x | safe }}; our filter passes through.
        return HTMLResponse(tmpl.render(**context))

    def _search_context(q: str, page: int) -> dict:
        ctx = {"query": q, "version": __version__, "stats": service.stats(),
               "elapsed_ms": None, "hits": [], "total_found": 0,
               "page": page, "per_page": PER_PAGE, "error": None}
        if not q:
            return ctx
        try:
            resp = service.search(q, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)
        except QueryParseError as exc:
            ctx["error"] = f"Ошибка запроса: {exc}"
            return ctx
        ctx["elapsed_ms"] = resp.elapsed_ms
        ctx["total_found"] = resp.total_found
        ctx["hits"] = [h.to_dict() for h in resp.hits]
        return ctx

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _render("index.html", _search_context("", 1))

    @app.get("/search", response_class=HTMLResponse)
    def search(q: str = "", page: int = Query(1, ge=1)):
        return _render("index.html", _search_context(q.strip(), page))

    @app.get("/doc/{doc_id}", response_class=HTMLResponse)
    def doc_view(doc_id: int):
        meta = service.index.documents.get(doc_id)
        if not meta:
            raise HTTPException(404, "Документ не найден")
        import datetime
        d = dict(meta)
        for key in ("created", "modified"):
            ts = d.get(key)
            if isinstance(ts, (int, float)):
                d[key + "_iso"] = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        d["size_human"] = human_size(d.get("size", 0))
        return _render("doc.html", {"doc": d,
                                    "text": read_document_text(service.engine, doc_id)})

    @app.get("/api/status")
    def api_status():
        return JSONResponse({"progress": service.crawl_progress.to_dict(),
                             "stats": service.stats()})

    @app.post("/api/crawl")
    def api_crawl(body: CrawlRequest):
        target = body.target.strip()
        if not target:
            raise HTTPException(400, "Не указан target")
        if not service.crawl_async(target, depth=body.depth):
            raise HTTPException(409, "Краулер уже запущен")
        return {"ok": True}

    @app.post("/api/crawl/stop")
    def api_crawl_stop():
        service.stop_crawl()
        return {"ok": True}

    @app.post("/api/save")
    def api_save():
        try:
            return {"ok": True, "path": service.save()}
        except LocalSearchError as exc:
            raise HTTPException(500, str(exc))

    @app.get("/api/search")
    def api_search(q: str = "", limit: int = Query(20, ge=1, le=100)):
        try:
            resp = service.search(q, limit=limit)
        except QueryParseError as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse(resp.to_dict())

    return app


def create_app(index_path: Optional[str] = None, crawl_root: Optional[str] = None,
               backend: str = "fastapi"):
    """Универсальная фабрика: возвращает приложение Flask или FastAPI."""
    service = _make_service(index_path, crawl_root)
    if backend == "flask":
        return create_flask_app(service)
    return create_fastapi_app(service)


def main() -> None:
    """Точка входа CLI: ``python -m localsearch.app``."""
    parser = argparse.ArgumentParser(description="Веб-интерфейс LocalSearch")
    parser.add_argument("--backend", choices=["fastapi", "flask"], default="fastapi")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--index", default=None, help="Путь к файлу индекса (.db/.json)")
    parser.add_argument("--crawl-root", default=None,
                        help="Папка или URL для первичной индексации при старте")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.backend == "flask":
        app = create_app(args.index, args.crawl_root, backend="flask")
        print(f" * Flask: http://{args.host}:{args.port}")
        app.run(host=args.host, port=args.port)
    else:
        import uvicorn
        app = create_app(args.index, args.crawl_root, backend="fastapi")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
