import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from ig_saves import db, server


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATA", tmp_path)
    conn = db.connect()
    rows = [
        # shortcode, type, caption, enriched_at, transcript, error
        ("photo", "GraphImage", "Recette de crêpes", "2026-01-01", None, None),
        ("reel_pret", "GraphVideo", "Astuce", "2026-01-01", "on parle de pâte à crêpes", None),
        ("reel_attente", "GraphVideo", "Vidéo pas encore transcrite", "2026-01-01", None, None),
        ("brut", "GraphImage", "Pas encore complété", None, None, None),
        ("supprime", "GraphImage", "Post disparu", None, None, "BadResponseException"),
    ]
    conn.executemany(
        "INSERT INTO posts (shortcode, type, caption, posted_at, enriched_at, transcript, error)"
        " VALUES (?, ?, ?, '2025-12-31', ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def codes(posts):
    return {post["shortcode"] for post in posts}


def test_search_ignores_accents_and_case_and_matches_prefixes(conn):
    assert codes(server.search_saves(query_text="CREPE")) == {"photo", "reel_pret"}
    assert codes(server.search_saves(query_text='crêpes "recette')) == {"photo"}


def test_unsorted_only_returns_posts_whose_content_is_complete(conn):
    assert codes(server.search_saves(unsorted=True)) == {"photo", "reel_pret", "supprime"}
    assert server.update_saves([{"shortcode": "photo", "category": "cuisine"}, {"shortcode": "inconnu"}]).startswith("1 ")
    assert codes(server.search_saves(unsorted=True)) == {"reel_pret", "supprime"}
    assert codes(server.search_saves(query_text="cuisine")) == {"photo"}  # l'index suit les mises à jour

    summary = server.overview()
    assert (summary["total"], summary["a_trier"], summary["en_attente_de_contenu"]) == (5, 2, 2)
    assert summary["categories"] == {"cuisine": 1}


def test_update_keeps_fields_that_are_not_given(conn):
    server.update_saves([{"shortcode": "photo", "category": "cuisine", "summary": "Crêpes faciles"}])
    server.update_saves([{"shortcode": "photo", "category": "desserts"}])
    post = server.get_saves(["photo"])[0]
    assert (post["category"], post["summary"]) == ("desserts", "Crêpes faciles")
    assert post["url"] == "https://www.instagram.com/p/photo/"


def test_images_are_served_only_for_known_saves(conn, tmp_path):
    folder = tmp_path / "media" / "photo"
    folder.mkdir(parents=True)
    for name in ("0.jpg", "1.webp", "1.mp4", "2.heic"):
        (folder / name).write_bytes(b"x")
    assert server.get_saves(["photo"])[0]["images"] == 2
    assert [image.path.name for image in server.get_images("photo")] == ["0.jpg", "1.webp"]
    with pytest.raises(ValueError):
        server.get_images("../photo")


def test_sync_now_refuses_a_second_cycle_within_the_hour(conn, monkeypatch):
    started = []
    monkeypatch.setattr(server, "run_cycle", lambda: started.append(1))

    def set_last(age):
        report = {"started": (datetime.now(UTC) - age).isoformat(timespec="seconds")}
        conn.execute("INSERT OR REPLACE INTO state VALUES ('last_cycle', ?)", (json.dumps(report),))
        conn.commit()

    set_last(timedelta(minutes=10))
    assert "attends" in server.sync_now()
    set_last(timedelta(hours=2))
    assert "lancé" in server.sync_now()
    assert started == [1]


def test_tools_are_exposed_through_the_protocol(conn):
    tools = asyncio.run(server.mcp.list_tools())
    assert {tool.name for tool in tools} == {
        "overview", "search_saves", "get_saves", "get_images", "update_saves", "sync_now",
    }
    result = asyncio.run(server.mcp.call_tool("search_saves", {"query_text": "crepe"}))
    assert "photo" in str(result)
