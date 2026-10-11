"""Transcrit les vidéos téléchargées puis les supprime. Aucun contact avec Instagram.

uv run python -m ig_saves.transcribe
"""
import sqlite3
from itertools import groupby
from pathlib import Path
from typing import Callable

from . import db
from .db import connect

MODEL = "small"  # multilingue ; "medium" est plus juste en français mais environ 3 fois plus lent


def whisper() -> Callable[[Path], str]:
    import av  # imports lourds, inutiles quand il n'y a rien à transcrire
    from faster_whisper import WhisperModel

    model = WhisperModel(MODEL, compute_type="int8")

    def transcribe(video: Path) -> str:
        with av.open(str(video)) as container:
            if not container.streams.audio:  # vidéo muette, fréquent dans les carrousels
                return ""
        # vad_filter : sans lui, Whisper invente des phrases sur la musique
        segments, _ = model.transcribe(str(video), vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments)

    return transcribe


def transcribe_pending(conn: sqlite3.Connection, transcribe: Callable[[Path], str]) -> int:
    """Une transcription par post ; "" veut dire transcrit mais sans parole."""
    done = 0
    videos = sorted((db.DATA / "media").glob("*/*.mp4"))
    for folder, group in groupby(videos, key=lambda video: video.parent):
        group = list(group)
        try:
            text = "\n\n".join(filter(None, map(transcribe, group)))
        except Exception as err:
            # shortcut: une vidéo illisible est retentée à chaque cycle, à marquer en base si ça arrive souvent
            print(f"Échec {folder.name} : {err!r}", flush=True)
            continue
        conn.execute("UPDATE posts SET transcript = ? WHERE shortcode = ?", (text, folder.name))
        conn.commit()
        for video in group:
            video.unlink()
        done += 1
        print(f"Transcrit {folder.name} ({len(text)} caractères)", flush=True)
    return done


def main() -> None:
    if not any((db.DATA / "media").glob("*/*.mp4")):
        print("Rien à transcrire.")
        return
    print(f"{transcribe_pending(connect(), whisper())} posts transcrits.")


if __name__ == "__main__":
    main()
