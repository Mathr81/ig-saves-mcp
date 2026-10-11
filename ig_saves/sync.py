"""Synchro des saves Instagram vers SQLite, en lecture seule.

uv run python -m ig_saves.sync import-session   # sur le PC : cookies Firefox -> data/session-<user>
uv run python -m ig_saves.sync                  # un cycle
"""
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import instaloader

from . import db
from .db import connect

FIREFOX_COOKIES = "AppData/Roaming/Mozilla/Firefox/Profiles/*/cookies.sqlite"
PAGE = 12                   # taille d'une page du flux des saves
BACKLOG_PER_CYCLE = 600     # posts d'historique indexés par cycle (1 requête pour 12)
ENRICH_PER_CYCLE = 150      # posts complétés par cycle (1 requête sans compte + téléchargements chacun)
ACCOUNT_FALLBACKS = 10      # posts par cycle qu'on accepte d'aller chercher avec le compte
DELAY = (5, 15)             # secondes, tirées au hasard avant chaque requête à l'API
MAX_CONSECUTIVE_ERRORS = 3

# Instaloader fait sinon une requête de plus par Reel pour un compteur de vues dont on ne se sert pas.
instaloader.Post._fetch_play_count_from_clips = staticmethod(lambda *args: None)


UNAVAILABLE = (
    instaloader.BadResponseException,
    instaloader.PostChangedException,
    instaloader.QueryReturnedNotFoundException,
)


class Discreet(instaloader.RateController):
    def wait_before_query(self, query_type: str) -> None:
        super().wait_before_query(query_type)
        time.sleep(random.uniform(*DELAY))


def make_loader() -> instaloader.Instaloader:
    # Sur le VPS, tout le trafic Instagram sort par le NAS. HTTPS_PROXY vaut pour toutes les sessions
    # qu'Instaloader crée, téléchargements compris ; si le proxy est injoignable la requête échoue, sans repli.
    proxy = os.environ.get("PROXY_URL")
    if proxy:
        os.environ["HTTPS_PROXY"] = proxy
    elif os.environ.get("REQUIRE_PROXY"):
        raise SystemExit("PROXY_URL manquant : refus de contacter Instagram en direct.")
    # iphone_support=False : pas d'appel à l'API mobile ; max_connection_attempts=1 : jamais de réessai
    return instaloader.Instaloader(
        quiet=True, iphone_support=False, max_connection_attempts=1, rate_controller=Discreet
    )


def firefox_cookies() -> dict[str, str]:
    for cookie_db in Path.home().glob(FIREFOX_COOKIES):
        # immutable=1 : lecture sans verrou, Firefox peut rester ouvert
        conn = sqlite3.connect(f"{cookie_db.as_uri()}?immutable=1", uri=True)
        cookies = dict(conn.execute("SELECT name, value FROM moz_cookies WHERE host LIKE '%instagram.com'"))
        conn.close()
        if "sessionid" in cookies:
            return cookies
    raise SystemExit("Aucune session Instagram dans Firefox : connecte-toi à instagram.com dans Firefox puis relance.")


def import_session() -> None:
    loader = make_loader()
    loader.context.update_cookies(firefox_cookies())
    username = loader.test_login()
    if not username:
        raise SystemExit("Cookie présent mais session refusée par Instagram.")
    loader.context.username = username
    loader.save_session_to_file(str(db.DATA / f"session-{username}"))
    print(f"Session de {username} enregistrée dans {db.DATA}.")


def load_session() -> instaloader.Instaloader:
    files = sorted(db.DATA.glob("session-*"))
    if not files:
        raise SystemExit("Pas de session : lance import-session sur le PC.")
    loader = make_loader()
    loader.load_session_from_file(files[0].name.removeprefix("session-"), str(files[0]))
    return loader


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row and row[0]


def insert(conn: sqlite3.Connection, post, newest: bool = False) -> bool:
    """Ajoute un post du flux, au-dessus de tous les autres (nouveau save) ou en dessous (historique)."""
    seq = "coalesce(max(seq), 0) + 1" if newest else "coalesce(min(seq), 0) - 1"
    # Ces quatre champs viennent du flux : les lire ne déclenche aucune requête.
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO posts (shortcode, type, caption, posted_at, seq) SELECT ?, ?, ?, ?, {seq} FROM posts",
        (post.shortcode, post.typename, post.caption, post.date_utc.isoformat(sep=" ")),
    )
    conn.commit()  # transactions courtes : le serveur MCP doit pouvoir écrire pendant un cycle
    return cursor.rowcount == 1


def index_head(conn: sqlite3.Connection, posts) -> int:
    """Nouveaux saves : s'arrête après une page entière de posts déjà connus."""
    new, known_run = [], 0
    for post in posts:
        if conn.execute("SELECT 1 FROM posts WHERE shortcode = ?", (post.shortcode,)).fetchone():
            known_run += 1
            if known_run >= PAGE:
                break
        else:
            new.append(post)
            known_run = 0
    for post in reversed(new):  # le flux commence par le plus récent, qui doit recevoir le plus grand seq
        insert(conn, post, newest=True)
    return len(new)


def index_backlog(conn: sqlite3.Connection, posts, budget: int) -> tuple[int, bool]:
    """Historique : indexe au plus `budget` posts. Renvoie (nouveaux, terminé)."""
    new = 0
    for count, post in enumerate(posts, 1):
        new += insert(conn, post)
        if count % (5 * PAGE) == 0:
            print(f"Historique : {count} posts lus", flush=True)
        if count >= budget:
            return new, False
    return new, True


def media_of(post) -> list[tuple[str | None, str | None]]:
    """(URL de l'image, URL de la vidéo ou None) pour chaque média du post."""
    if post.typename == "GraphSidecar":
        return [(node.display_url, node.video_url) for node in post.get_sidecar_nodes()]
    return [(post.url, post.video_url)]


def enrich(conn: sqlite3.Connection, anonymous, account, budget: int) -> int:
    """Complète les posts indexés : auteur, légende entière, images et vidéos dans data/media/<shortcode>/.

    Tout se fait sans compte. Seul un post introuvable ainsi (compte privé suivi, ou post supprimé) est
    redemandé avec la session, pour trancher.
    """
    pending = conn.execute(
        "SELECT shortcode FROM posts WHERE enriched_at IS NULL AND error IS NULL"
        " ORDER BY seq DESC LIMIT ?",
        (budget,),
    ).fetchall()
    done = fallbacks = 0
    failed: list[str] = []
    for (shortcode,) in pending:
        try:
            try:
                post = instaloader.Post.from_shortcode(anonymous, shortcode)
            except UNAVAILABLE:
                fallbacks += 1
                if fallbacks > ACCOUNT_FALLBACKS:
                    raise SystemExit("Trop de posts introuvables sans compte : accès anonyme sans doute bloqué.")
                post = instaloader.Post.from_shortcode(account, shortcode)
            folder = db.DATA / "media" / shortcode
            folder.mkdir(parents=True, exist_ok=True)
            for index, urls in enumerate(media_of(post)):
                for url in filter(None, urls):
                    anonymous.get_and_write_raw(url, str(folder / f"{index}{Path(urlparse(url).path).suffix}"))
            conn.execute(
                "UPDATE posts SET author = ?, caption = ?, enriched_at = CURRENT_TIMESTAMP WHERE shortcode = ?",
                (post.owner_username, post.caption, shortcode),
            )
            done += 1
            failed.clear()
            print(f"Complété {done}/{len(pending)} : {shortcode}", flush=True)
        except UNAVAILABLE as err:
            failed.append(shortcode)
            if len(failed) >= MAX_CONSECUTIVE_ERRORS:
                # Plusieurs échecs d'affilée : plutôt un blocage que des posts supprimés, on ne condamne rien.
                conn.executemany("UPDATE posts SET error = NULL WHERE shortcode = ?", [(code,) for code in failed])
                conn.commit()
                raise
            conn.execute("UPDATE posts SET error = ? WHERE shortcode = ?", (repr(err), shortcode))
            print(f"Indisponible : {shortcode}", flush=True)
        conn.commit()
    return done


def sync(conn: sqlite3.Connection, loader: instaloader.Instaloader) -> None:
    try:
        profile = instaloader.Profile.own_profile(loader.context)
    except TypeError:  # Instagram répond sans utilisateur : cookie expiré
        raise SystemExit("Session expirée : relance import-session sur le PC.") from None

    backlog = get_state(conn, "backlog")  # None : premier cycle ; JSON : reprise ; "done" : historique fini
    new = 0
    if backlog is not None:
        new += index_head(conn, profile.get_saved_posts())
        conn.commit()
    if backlog != "done":
        posts = profile.get_saved_posts()
        if backlog:
            posts.thaw(instaloader.FrozenNodeIterator(**json.loads(backlog)))
        added, finished = index_backlog(conn, posts, BACKLOG_PER_CYCLE)
        new += added
        conn.execute(
            "INSERT OR REPLACE INTO state VALUES ('backlog', ?)",
            ("done" if finished else json.dumps(posts.freeze()._asdict()),),
        )
        conn.commit()
        print(f"Historique : {'terminé' if finished else 'en cours'}, Instagram annonce {posts.count} saves.")

    enriched = enrich(conn, make_loader().context, loader.context, ENRICH_PER_CYCLE)
    total, pending = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE enriched_at IS NULL AND error IS NULL) FROM posts"
    ).fetchone()
    print(f"{new} nouveaux, {enriched} complétés, {pending} en attente sur {total}.")


def main() -> None:
    if sys.argv[1:] == ["import-session"]:
        import_session()
    else:
        sync(connect(), load_session())


if __name__ == "__main__":
    main()
