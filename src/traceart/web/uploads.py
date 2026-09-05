"""Cycle de vie des fichiers GPX uploadés.

Chaque session écrit ses GPX dans `work_dir/<session>/`. Le nom de
fichier compte : `Track.source.stem` alimente `default_title()`
(pipeline.py) — un nom généré (type `tmpXXXXXX`) donnerait un titre
absurde sur le rendu. `safe_stem` dérive donc un nom de fichier sûr
directement du nom d'origine plutôt que d'en fabriquer un.
"""

from __future__ import annotations

import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Protocol

from traceart.errors import UserError

_UNSAFE = re.compile(r"[^\w\séèàêâîôûçäëïöüñ-]", re.UNICODE)
_MAX_STEM_LENGTH = 100


class SupportsRead(Protocol):
    """Protocole minimal : un objet dont on ne demande que `.read(n)`.

    Évite de coupler ce module au type `UploadFile` de Starlette — la
    fonction de sauvegarde reste testable avec un simple `io.BytesIO`.
    """

    def read(self, size: int) -> bytes: ...


class UploadError(UserError):
    """Fichier uploadé refusé : absent, trop gros, ou pas un .gpx."""


def safe_stem(filename: str | None) -> str:
    """Nom de fichier sûr, dérivé du nom d'origine.

    `Path(filename).name` neutralise toute traversée de chemin
    (`../../etc/x.gpx` devient `x.gpx`) avant même l'assainissement des
    caractères. Un nom vide ou entièrement composé de caractères
    interdits retombe sur "trace" plutôt que de produire un fichier sans
    nom.
    """
    stem = Path(filename or "").stem
    cleaned = _UNSAFE.sub("_", stem).strip("_ ")
    return (cleaned or "trace")[:_MAX_STEM_LENGTH]


def new_session_id() -> str:
    return uuid.uuid4().hex


def session_dir(work_dir: Path, session_id: str) -> Path:
    return work_dir / "sessions" / session_id


def save(
    work_dir: Path,
    session_id: str,
    uploads: list[tuple[str | None, SupportsRead]],
    *,
    max_bytes: int,
) -> list[Path]:
    """Écrit un lot d'uploads dans le dossier de la session.

    Un nouvel upload **remplace** le lot précédent de la même session :
    un second essai après correction ne doit pas laisser de traces de
    l'échec précédent.

    Le plafond est vérifié **pendant** la copie (par blocs), jamais sur
    un `Content-Length` déclaré par le client — celui-ci ne contrôle que
    l'en-tête, pas le flux réel.
    """
    if not uploads:
        raise UploadError("aucun fichier reçu")

    target_dir = session_dir(work_dir, session_id)
    shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    used_names: set[str] = set()
    for filename, stream in uploads:
        if filename and not filename.lower().endswith(".gpx"):
            raise UploadError(f"extension non supportée : {filename!r} (attendu .gpx)")
        stem = safe_stem(filename)
        name = stem
        suffix = 2
        while name in used_names:
            name = f"{stem}-{suffix}"
            suffix += 1
        used_names.add(name)

        target = target_dir / f"{name}.gpx"
        _copy_bounded(stream, target, max_bytes=max_bytes)
        written.append(target)
    return written


def _copy_bounded(stream: SupportsRead, target: Path, *, max_bytes: int) -> None:
    seen = 0
    chunk_size = 1 << 16
    with target.open("wb") as fh:
        while chunk := stream.read(chunk_size):
            seen += len(chunk)
            if seen > max_bytes:
                fh.close()
                target.unlink(missing_ok=True)
                raise UploadError(
                    f"fichier trop volumineux (> {max_bytes / 1e6:.0f} Mo)"
                )
            fh.write(chunk)


def sweep_expired_sessions(work_dir: Path, *, ttl_s: float) -> int:
    """Supprime les sessions dont le dernier accès dépasse `ttl_s`.

    Appelé en tête de requête plutôt que par un scheduler : le coût d'un
    balayage est négligeable devant celui d'un rendu, et ça évite tout
    thread de fond dédié au ménage.
    """
    root = work_dir / "sessions"
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    for entry in root.iterdir():
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age > ttl_s:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


