"""setup.py — упаковка LocalSearch в устанавливаемую Python-библиотеку.

Установка в режиме разработчика::

    pip install -e .

Сборка дистрибутива::

    python setup.py sdist bdist_wheel
"""

from setuptools import find_packages, setup

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="localsearch",
    version="1.0.0",
    author="LocalSearch Project",
    description="Локальная поисковая система по файлам (.txt/.pdf/.docx/.html) "
                "с краулером, инвертированным индексом и веб-интерфейсом.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/example/localsearch",
    packages=find_packages(exclude=("tests", "tests.*")),
    include_package_data=True,
    package_data={
        "localsearch": ["templates/*.html", "static/*.css", "static/*.js"],
    },
    python_requires=">=3.9",
    install_requires=[
        "requests>=2.28",
        "beautifulsoup4>=4.12",
        "pypdf>=3.9",
        "python-docx>=1.0",
        "chardet>=5.0",
    ],
    extras_require={
        "web": [
            "flask>=2.3",
            "fastapi>=0.100",
            "uvicorn>=0.23",
            "jinja2>=3.1",
            "python-multipart>=0.0.6",
        ],
        "dev": ["pytest>=7.0"],
    },
    entry_points={
        "console_scripts": [
            "localsearch=localsearch.app:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.12",
        "Topic :: Text Processing :: Indexing",
        "Topic :: Internet :: WWW/HTTP :: Indexing/Search",
    ],
    keywords="search, crawler, indexer, full-text, bm25, local files, pdf, docx",
    zip_safe=False,
)
