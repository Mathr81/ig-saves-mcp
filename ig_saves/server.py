"""Serveur MCP : expose les saves à Claude et pilote les cycles de synchro.

uv run python -m ig_saves.server                      # stdio (Claude Code, Claude Desktop)
MCP_SECRET=<64 caractères> python -m ig_saves.server  # HTTP sur /<secret>/mcp + cycles automatiques
"""
import json
import os
import random
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from mcp.server.mcpserver import Image, MCPServer

from . import db
from .db import connect

CYCLE_HOURS = (10, 14)              # intervalle aléatoire entre deux cycles automatiques
MIN_INTERVAL = timedelta(hours=1)   # entre deux cycles, quel que soit le déclencheur
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGES = 10
# Prêt à trier : complété et, pour une vidéo, transcrit. Un post devenu indisponible se trie sur sa légende.
READY = "((enriched_at IS NOT NULL AND (type != 'GraphVideo' OR transcript IS NOT NULL)) OR error IS NOT NULL)"

mcp = MCPServer(
    "ig-saves",
    instructions=(
        "Posts Instagram que l'utilisateur a enregistrés (saves), stockés localement avec légende, auteur et "
        "transcription audio des vidéos. Le tri est purement local : rien n'est modifié sur Instagram.\n"
        "Pour trier : appelle overview pour connaître les catégories existantes et réutilise-les, puis "
        "search_saves(unsorted=True), get_saves pour lire le contenu, et update_saves par lots.\n"
        "Le résumé d'un save dit en une ou deux phrases ce qu'il y a à en retenir concrètement : le nom de "
        "l'outil ou du site, l'astuce, la recette, l'idée. Il doit suffire à ne pas revoir le post. Si l'essentiel "
        "n'est ni dans la légende ni dans la transcription, dis-le dans le résumé plutôt que d'inventer.\n"
        "get_images coûte cher : ne l'utilise que si la légende et la transcription ne suffisent pas."
    ),
)
_cycle = threading.Lock()


def query(sql: str, params: tuple = ()) -> list[dict]:
    conn = connect()
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def last_cycle() -> dict | None:
    rows = query("SELECT value FROM state WHERE key = 'last_cycle'")
    return json.loads(rows[0]["value"]) if rows else None


def run_cycle() -> None:
    """Synchro puis transcription, en sous-processus pour ne pas garder Whisper en mémoire."""
    if not _cycle.acquire(blocking=False):
        return
    try:
        report = {"started": datetime.now(UTC).isoformat(timespec="seconds"), "running": True}
        save = "INSERT OR REPLACE INTO state VALUES ('last_cycle', ?)"
        conn = connect()
        conn.execute(save, (json.dumps(report),))
        conn.commit()
        log: deque[str] = deque(maxlen=12)
        codes = []
        for module in ("ig_saves.sync", "ig_saves.transcribe"):
            with subprocess.Popen(
                [sys.executable, "-X", "utf8", "-u", "-m", module],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            ) as process:
                for line in process.stdout:
                    print(line, end="", file=sys.stderr, flush=True)  # stdout est réservé au protocole en stdio
                    log.append(line.rstrip())
            codes.append(process.returncode)
        report |= {
            "running": False,
            "finished": datetime.now(UTC).isoformat(timespec="seconds"),
            "ok": not any(codes),
            "log": list(log),
        }
        conn.execute(save, (json.dumps(report),))
        conn.commit()
        conn.close()
    finally:
        _cycle.release()


def since_last_cycle() -> timedelta | None:
    last = last_cycle()
    return last and datetime.now(UTC) - datetime.fromisoformat(last["started"])


def scheduler() -> None:
    interval = timedelta(0)  # premier cycle dès que possible si aucun n'a jamais tourné
    while True:
        time.sleep(60)
        elapsed = since_last_cycle()
        if elapsed is None or elapsed >= max(interval, MIN_INTERVAL):
            run_cycle()
            interval = timedelta(hours=random.uniform(*CYCLE_HOURS))


@mcp.tool()
def overview() -> dict:
    """État de la collection : totaux, catégories existantes avec leur nombre de posts, dernier cycle de synchro."""
    counts = query(
        f"""SELECT count(*) AS total,
                   count(*) FILTER (WHERE category IS NULL AND {READY}) AS a_trier,
                   count(*) FILTER (WHERE NOT {READY}) AS en_attente_de_contenu,
                   count(*) FILTER (WHERE error IS NOT NULL) AS indisponibles
            FROM posts"""
    )[0]
    categories = query("SELECT category, count(*) AS n FROM posts WHERE category IS NOT NULL GROUP BY 1 ORDER BY 2 DESC")
    return {
        **counts,
        "categories": {row["category"]: row["n"] for row in categories},
        "cycle_en_cours": _cycle.locked(),
        "dernier_cycle": last_cycle(),
    }


@mcp.tool()
def search_saves(
    query_text: str = "", unsorted: bool = False, category: str = "", author: str = "", limit: int = 30, offset: int = 0
) -> list[dict]:
    """Liste des saves, du plus récemment enregistré au plus ancien, avec un extrait de légende et de transcription.

    query_text : mots à trouver (tous requis, préfixes acceptés, accents ignorés) dans la légende, la
    transcription, l'auteur, le résumé ou la catégorie. unsorted : seulement les posts sans catégorie dont le
    contenu est complet. Tous les filtres se combinent ; pagine avec limit et offset.
    """
    where, params = ["1"], []
    if query_text:
        # chaque mot entre guillemets : la syntaxe FTS5 de l'appelant ne peut pas casser la requête
        terms = " ".join('"' + word.replace('"', '""') + '"*' for word in query_text.split())
        where.append("shortcode IN (SELECT shortcode FROM posts_fts WHERE posts_fts MATCH ?)")
        params.append(terms)
    if unsorted:
        where.append(f"category IS NULL AND {READY}")
    if category:
        where.append("category = ?")
        params.append(category)
    if author:
        where.append("author = ?")
        params.append(author)
    return query(
        f"""SELECT shortcode, type, author, posted_at, category, summary,
                   substr(caption, 1, 200) AS caption, substr(transcript, 1, 200) AS transcript
            FROM posts WHERE {' AND '.join(where)}
            ORDER BY seq DESC LIMIT ? OFFSET ?""",
        (*params, min(limit, 100), offset),
    )


@mcp.tool()
def get_saves(shortcodes: list[str]) -> list[dict]:
    """Contenu complet de plusieurs saves : légende et transcription entières, lien, nombre d'images disponibles."""
    marks = ",".join("?" * len(shortcodes))
    posts = query(
        f"""SELECT shortcode, type, author, posted_at, first_seen, category, summary, caption, transcript, error
            FROM posts WHERE shortcode IN ({marks})""",
        tuple(shortcodes),
    )
    for post in posts:
        post["url"] = f"https://www.instagram.com/p/{post['shortcode']}/"
        post["images"] = len(image_paths(post["shortcode"]))
    return posts


def image_paths(shortcode: str) -> list:
    folder = db.DATA / "media" / shortcode
    return sorted(path for path in folder.glob("*") if path.suffix.lower() in IMAGE_SUFFIXES)


@mcp.tool()
def get_images(shortcode: str, offset: int = 0) -> list[Image]:
    """Images d'un save (photo, pages d'un carrousel, couverture d'une vidéo), 10 au plus par appel."""
    # Le shortcode sert à construire un chemin : on n'accepte que ceux présents en base.
    if not query("SELECT 1 FROM posts WHERE shortcode = ?", (shortcode,)):
        raise ValueError(f"Save inconnu : {shortcode}")
    return [Image(path=path) for path in image_paths(shortcode)[offset : offset + MAX_IMAGES]]


class SaveUpdate(TypedDict, total=False):
    shortcode: str
    category: str
    summary: str


@mcp.tool()
def update_saves(updates: list[SaveUpdate]) -> str:
    """Enregistre la catégorie et/ou le résumé de plusieurs saves. Un champ absent reste inchangé."""
    conn = connect()
    changed = sum(
        conn.execute(
            "UPDATE posts SET category = coalesce(?, category), summary = coalesce(?, summary) WHERE shortcode = ?",
            (update.get("category"), update.get("summary"), update["shortcode"]),
        ).rowcount
        for update in updates
    )
    conn.commit()
    conn.close()
    return f"{changed} saves mis à jour sur {len(updates)} demandés."


@mcp.tool()
def sync_now() -> str:
    """Lance en arrière-plan un cycle de synchro avec Instagram (nouveaux saves, puis transcription).

    Refusé si un cycle tourne ou a démarré il y a moins d'une heure, pour rester discret. Suivre avec overview.
    """
    if _cycle.locked():
        return "Un cycle est déjà en cours."
    elapsed = since_last_cycle()
    if elapsed is not None and elapsed < MIN_INTERVAL:
        return f"Dernier cycle il y a {int(elapsed.total_seconds() // 60)} min : attends une heure entre deux cycles."
    threading.Thread(target=run_cycle, daemon=True).start()
    return "Cycle lancé. Il peut durer plus de 20 minutes ; appelle overview pour voir le résultat."


def main() -> None:
    secret = os.environ.get("MCP_SECRET")
    if not secret:
        mcp.run()
        return
    if len(secret) < 32:
        raise SystemExit("MCP_SECRET trop court : il sert de mot de passe dans l'URL, 32 caractères au minimum.")
    threading.Thread(target=scheduler, daemon=True).start()
    # shortcut: le secret dans l'URL tient lieu d'authentification (un seul utilisateur) ; passer à OAuth
    # si le serveur est partagé ou si l'URL risque de fuiter.
    mcp.run(
        "streamable-http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
        streamable_http_path=f"/{secret}/mcp", stateless_http=True, json_response=True,
    )


if __name__ == "__main__":
    main()
