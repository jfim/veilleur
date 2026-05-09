"""Database layer.

Holds SQLAlchemy models, the engine/session factory, and Alembic migration
glue. Persists feed definitions and the full history of extracted items so
the REST API can serve historical entries beyond the latest scrape.
"""
