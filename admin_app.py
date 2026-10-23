#!/usr/bin/env python3
"""CLI-обёртка админки профайлера.

Реализация — в модуле app/admin/ (app.py — Flask-приложение, templates/admin/*.html — шаблоны).
Запуск:
    python admin_app.py [--db postgresql+psycopg://user:pass@localhost:5682/profiling] [--port 8777]
или:
    python -m app.admin [--port 8777]
"""
from app.admin.app import main

if __name__ == "__main__":
    main()
