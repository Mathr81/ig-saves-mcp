import os
import sqlite3
from pathlib import Path

DATA = Path(os.environ.get("IG_SAVES_DATA", "data"))

# Dates en UTC, format SQLite (YYYY-MM-DD HH:MM:SS). Lien du post : instagram.com/p/<shortcode>/
SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    shortcode   TEXT PRIMARY KEY,
    type        TEXT NOT NULL,  -- GraphImage | GraphVideo | GraphSidecar
    caption     TEXT,
    posted_at   TEXT NOT NULL,
    first_seen  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    seq         INTEGER,        -- ordre d'enregistrement sur Instagram : plus grand = save plus récent
    author      TEXT,
    enriched_at TEXT,           -- NULL : auteur et médias pas encore récupérés
    error       TEXT,           -- post devenu indisponible
    transcript  TEXT,
    category    TEXT,
    summary     TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);

-- Recherche plein texte, insensible à la casse et aux accents, tenue à jour par les triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(shortcode UNINDEXED, author, caption, transcript, summary, category);
CREATE TRIGGER IF NOT EXISTS posts_fts_insert AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts VALUES (new.shortcode, new.author, new.caption, new.transcript, new.summary, new.category);
END;
CREATE TRIGGER IF NOT EXISTS posts_fts_update AFTER UPDATE ON posts BEGIN
    DELETE FROM posts_fts WHERE shortcode = old.shortcode;
    INSERT INTO posts_fts VALUES (new.shortcode, new.author, new.caption, new.transcript, new.summary, new.category);
END;
INSERT INTO posts_fts SELECT shortcode, author, caption, transcript, summary, category FROM posts
    WHERE shortcode NOT IN (SELECT shortcode FROM posts_fts);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    if path is None:
        DATA.mkdir(parents=True, exist_ok=True)
        path = DATA / "saves.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")  # le serveur MCP lit pendant que la synchro écrit
    conn.executescript(SCHEMA)
    return conn
