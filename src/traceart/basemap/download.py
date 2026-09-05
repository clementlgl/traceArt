"""Téléchargement en flux, partagé par les deux caches.

`Store` (Natural Earth) et `OsmStore` (extraits Geofabrik) faisaient la
même chose à 65 % : flux, sha256 au fil de l'eau, écriture dans un
`.part` puis renommage atomique. Un Ctrl-C ne doit jamais laisser une
archive tronquée que le lancement suivant prendrait pour valide.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

USER_AGENT = "traceart/0.4 (+https://github.com/)"
CHUNK = 1 << 16


class DownloadError(RuntimeError):
    """Le téléchargement a échoué ; aucun fichier partiel n'est laissé."""


def download_to(
    url: str,
    target: Path,
    *,
    timeout: float,
    on_progress: Callable[[int, int], None] | None = None,
) -> str:
    """Télécharge `url` vers `target`. Renvoie le sha256 du contenu.

    `on_progress(reçu, total)` est appelé au fil du flux ; `total` vaut 0
    quand le serveur n'annonce pas de `Content-Length`.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,
            tmp.open("wb") as fh,
        ):
            total = int(response.headers.get("Content-Length") or 0)
            seen = 0
            while chunk := response.read(CHUNK):
                digest.update(chunk)
                fh.write(chunk)
                seen += len(chunk)
                if on_progress:
                    on_progress(seen, total)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"{url} : téléchargement impossible ({exc})") from exc

    tmp.replace(target)
    return digest.hexdigest()
