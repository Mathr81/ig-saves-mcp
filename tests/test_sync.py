from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import instaloader
import pytest

from ig_saves import db, sync


def feed_post(code):
    return SimpleNamespace(shortcode=code, typename="GraphImage", caption=f"légende {code}", date_utc=datetime(2026, 1, 1))


class FakeContext:
    def __init__(self):
        self.downloads = []

    def get_and_write_raw(self, url, filename):
        self.downloads.append(Path(filename).name)


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATA", tmp_path)
    return db.connect(":memory:")


def test_head_stops_after_a_full_page_of_known_posts(conn):
    known = [feed_post(f"k{i}") for i in range(30)]
    for post in known:
        sync.insert(conn, post)
    read = []

    def feed():
        # un post connu remonté en tête (re-save) ne doit pas arrêter la lecture
        for post in [feed_post("new1"), known[0], feed_post("new2"), *known[1:]]:
            read.append(post.shortcode)
            yield post

    assert sync.index_head(conn, feed()) == 2
    assert len(read) == 3 + sync.PAGE
    newest_first = [code for (code,) in conn.execute("SELECT shortcode FROM posts ORDER BY seq DESC LIMIT 4")]
    assert newest_first == ["new1", "new2", "k0", "k1"]


def test_backlog_respects_budget_and_reports_the_end(conn):
    posts = iter([feed_post(f"p{i}") for i in range(5)])
    assert sync.index_backlog(conn, posts, 3) == (3, False)
    assert sync.index_backlog(conn, posts, 3) == (2, True)


def test_enrich_downloads_media_and_skips_deleted_posts(conn, monkeypatch):
    for code in ("reel", "gone", "carousel", "prive"):
        sync.insert(conn, feed_post(code))
    private = SimpleNamespace(typename="GraphImage", owner_username="carol", caption=None, url="https://cdn/p.jpg", video_url=None)
    full = {
        "reel": SimpleNamespace(
            typename="GraphVideo", owner_username="alice", caption="légende entière",
            url="https://cdn/a/cover.jpg?sig=1", video_url="https://cdn/a/video.mp4?sig=1",
        ),
        "carousel": SimpleNamespace(
            typename="GraphSidecar", owner_username="bob", caption=None,
            get_sidecar_nodes=lambda: [
                SimpleNamespace(display_url="https://cdn/1.jpg", video_url=None),
                SimpleNamespace(display_url="https://cdn/2.jpg", video_url="https://cdn/2.mp4"),
            ],
        ),
    }

    context, account = FakeContext(), FakeContext()
    asked_with_account = []

    def fetch(used, shortcode):
        if used is account:
            asked_with_account.append(shortcode)
            if shortcode == "prive":
                return private
        if shortcode not in full:
            raise instaloader.BadResponseException("Fetching Post metadata failed.")
        return full[shortcode]

    monkeypatch.setattr(instaloader.Post, "from_shortcode", fetch)

    assert sync.enrich(conn, context, account, 10) == 3
    assert sorted(asked_with_account) == ["gone", "prive"]  # le compte ne sert que pour les introuvables
    assert account.downloads == []
    assert dict(conn.execute("SELECT shortcode, author FROM posts WHERE enriched_at IS NOT NULL")) == {
        "reel": "alice", "carousel": "bob", "prive": "carol",
    }
    assert conn.execute("SELECT caption FROM posts WHERE shortcode = 'reel'").fetchone()[0] == "légende entière"
    assert conn.execute("SELECT error FROM posts WHERE shortcode = 'gone'").fetchone()[0]
    assert sorted(context.downloads) == ["0.jpg", "0.jpg", "0.jpg", "0.mp4", "1.jpg", "1.mp4"]
    assert sync.enrich(conn, context, account, 10) == 0  # rien n'est retenté


def test_enrich_aborts_on_consecutive_failures_without_condemning_posts(conn, monkeypatch):
    for code in ("a", "b", "c", "d"):
        sync.insert(conn, feed_post(code))

    def fetch(context, shortcode):
        raise instaloader.BadResponseException("Fetching Post metadata failed.")

    monkeypatch.setattr(instaloader.Post, "from_shortcode", fetch)

    with pytest.raises(instaloader.BadResponseException):
        sync.enrich(conn, FakeContext(), FakeContext(), 10)
    assert conn.execute("SELECT count(*) FROM posts WHERE error IS NOT NULL").fetchone()[0] == 0


def test_enrich_stops_when_the_account_would_replace_anonymous_access(conn, monkeypatch):
    for index in range(sync.ACCOUNT_FALLBACKS + 5):
        sync.insert(conn, feed_post(f"p{index}"))
    anonymous, account = FakeContext(), FakeContext()
    with_account = []

    def fetch(used, shortcode):
        if used is anonymous:
            raise instaloader.BadResponseException("Fetching Post metadata failed.")
        with_account.append(shortcode)
        return SimpleNamespace(typename="GraphImage", owner_username="a", caption=None, url=None, video_url=None)

    monkeypatch.setattr(instaloader.Post, "from_shortcode", fetch)

    with pytest.raises(SystemExit):
        sync.enrich(conn, anonymous, account, 100)
    assert len(with_account) == sync.ACCOUNT_FALLBACKS


def test_refuses_direct_access_when_proxy_is_required(monkeypatch):
    monkeypatch.delenv("PROXY_URL", raising=False)
    monkeypatch.setenv("REQUIRE_PROXY", "1")
    with pytest.raises(SystemExit):
        sync.make_loader()


def test_transcribes_then_deletes_videos_and_keeps_images(conn, tmp_path):
    from ig_saves.transcribe import transcribe_pending

    for code in ("reel", "muet", "casse"):
        sync.insert(conn, feed_post(code))
        (tmp_path / "media" / code).mkdir(parents=True)
        (tmp_path / "media" / code / "0.mp4").touch()
    (tmp_path / "media" / "reel" / "0.jpg").touch()
    (tmp_path / "media" / "reel" / "1.mp4").touch()

    def transcribe(video):
        if video.parent.name == "casse":
            raise ValueError("pas de piste audio")
        return "" if video.parent.name == "muet" else f"parole {video.name}"

    assert transcribe_pending(conn, transcribe) == 2
    assert dict(conn.execute("SELECT shortcode, transcript FROM posts")) == {
        "reel": "parole 0.mp4\n\nparole 1.mp4", "muet": "", "casse": None,
    }
    assert sorted(path.name for path in (tmp_path / "media" / "reel").iterdir()) == ["0.jpg"]
    assert (tmp_path / "media" / "casse" / "0.mp4").exists()
