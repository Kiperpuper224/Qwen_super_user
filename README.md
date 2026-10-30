# LocalSearch — локальная поисковая система (Python-библиотека)

Полностью локальный поиск по файлам `.txt`, `.md`, `.log`, `.csv`, `.html`, `.pdf`, `.docx`
и веб-страницам: краулер → инвертированный индекс → BM25-ранжирование → веб-интерфейс
в стиле браузерной строки поиска. Данные никуда не отправляются.

## Архитектура

```
┌──────────────────────────────────────────────────────────────────────┐
│                            app.py (UI)                               │
│   Flask / FastAPI · HTML/CSS/JS · индикатор прогресса (polling)      │
└───────────┬──────────────────────────────────────────┬───────────────┘
            │ LocalSearch (api.py, фасад библиотеки)   │
┌───────────▼─────────────┐              ┌─────────────▼───────────────┐
│  crawler.py  (паук)     │  документы   │  search_engine.py           │
│  os.walk + BFS по web   │─────────────▶│  парсер запросов, BM25,     │
│  глубина/фильтры/прогресс│   add_document│  фразы, regex, сниппеты    │
└───────────┬─────────────┘              └─────────────▲───────────────┘
            │                                          │ чтение
┌───────────▼─────────────────────────────────────────┴───────────────┐
│  extractors.py          indexer.py                                  │
│  txt/html/pdf/docx  →  инвертированный индекс + предложения,        │
│  метаданные+текест       SQLite / JSON персист                      │
└──────────────────────────────────────────────────────────────────────┘
```

* **crawler.py** — рекурсивный обход директорий и веб-ссылок с настраиваемой
  глубиной, фильтрами по расширению/размеру, колбэком прогресса `on_progress`,
  остановкой `stop()` и фоновым режимом `start_async()`.
* **indexer.py** — инвертированный индекс «термин → (doc_id, позиции)», индексация
  предложений, BM25-статистика; сохранение/загрузка из SQLite или JSON.
* **search_engine.py** — разбор запроса (`"фразы"`, `regex:...`, `ext:pdf`,
  `after:2024-01-01`, `minsize:1kb`, `-исключение`), ранжирование BM25,
  входящие предложения и подсветка `<mark>` в сниппетах.
* **app.py** — веб-интерфейс (Flask или FastAPI на выбор) + REST API.
* **api.py** — фасад `LocalSearch` для внешнего кода; **setup.py/__init__.py** —
  упаковка в импортируемую библиотеку.

## Установка

```bash
pip install -r requirements.txt
pip install -e .            # установка пакета localsearch
```

## Запуск веб-интерфейса

```bash
python -m localsearch.app --backend fastapi --port 8000
python -m localsearch.app --backend flask --port 5000 --crawl-root ~/Documents
# после установки пакета также:
localsearch --help
```

Откройте http://127.0.0.1:8000, укажите папку в панели «Сканировать» — прогресс
обновляется каждые 800 мс через `GET /api/status`.

### REST API

| Метод | Путь | Назначение |
|---|---|---|
| GET  | `/search?q=...&page=1` | HTML-результаты со сниппетами |
| GET  | `/doc/<id>` | метаданные + извлечённый текст |
| POST | `/api/crawl` `{target, depth}` | запуск фоновой индексации |
| GET  | `/api/status` | прогресс краулера (для индикатора) |
| POST | `/api/crawl/stop` | остановка обхода |
| POST | `/api/save` | сохранить индекс на диск |
| GET  | `/api/search?q=...&limit=20` | поиск в JSON |

## Использование как библиотеки

```python
from localsearch import LocalSearch

ls = LocalSearch(index_path="docs.db", extensions=["txt", "pdf", "docx", "html"],
                 max_depth=3, autosave=True)

progress = ls.crawl("/home/me/documents")          # синхронно
ls.crawl_async("https://example.com/docs")         # фон, прогресс в ls.crawl_progress

resp = ls.search('"machine learning" ext:pdf after:2024-01-01 -draft')
for hit in resp.hits:
    print(hit.meta["name"], hit.score, hit.snippet)

low_level = ls.search("regex:\\w{7,}")             # регулярные выражения
```

Низкоуровневый API тоже доступен:

```python
from localsearch import Index, Crawler, SearchEngine

idx = Index.load("docs.db")                # или Index().load_json("docs.json")
engine = SearchEngine(idx)
print(engine.stats())
```

## Синтаксис запросов

| Оператор | Пример | Значение |
|---|---|---|
| слова | `python индекс` | точные слова, И-семантика, BM25 |
| фраза | `"инвертированный индекс"` | соседство слов + входящие предложения |
| regex | `regex:\w+ing` | поиск по регулярному выражению |
| ext | `ext:pdf` | фильтр по расширению |
| name/author | `name:отчёт author:иван` | подстрока имени/автора/заголовка |
| даты | `after:2024-01-01 before:2026-01-01` | по дате изменения файла |
| размер | `minsize:1kb maxsize:5mb` | границы размера |
| минус | `-черновик` | исключить документы с термином |

## Извлекаемые метаданные

имя файла, путь, расширение, размер, дата создания/изменения, кодировка
(chardet + эвристики BOM), автор и заголовок (PDF info, DOCX core properties,
HTML `<title>`/meta author).

## Тесты

```bash
pytest tests/ -v
```

## Структура проекта

```
localsearch/
├── __init__.py        # публичный API библиотеки
├── api.py             # фасад LocalSearch
├── crawler.py         # краулер/паук + CrawlProgress
├── indexer.py         # инвертированный индекс (SQLite/JSON)
├── search_engine.py   # запросы, BM25, regex, сниппеты
├── extractors.py      # txt/html/pdf/docx
├── utils.py           # токенизация, кодировки
├── exceptions.py      # иерархия ошибок
├── app.py             # Flask/FastAPI веб-интерфейс + CLI
├── templates/         # index.html, doc.html
└── static/            # style.css, app.js
setup.py · requirements.txt · tests/test_localsearch.py
```

## Лицензия

GPL v3 (см. LICENSE).
