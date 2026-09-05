"""Réglages de l'interface web.

Un dataclass frozen plutôt qu'un état de module : `create_app` prend un
`WebSettings` en paramètre, ce qui rend l'application testable sans
variables d'environnement (chaque test construit les réglages qu'il veut)
et sans singleton à réinitialiser entre deux tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from traceart.basemap.store import default_cache_dir


def _default_work_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "traceart" / "web"


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Tout ce qui distingue une instance de l'interface web d'une autre.

    `cache_dir` : données de fond (Natural Earth, extraits OSM) — le même
    cache que le CLI, sauf configuration explicite.
    `work_dir` : uploads en cours de session et artefacts rendus. Distinct
    du cache de fond : sa durée de vie est courte (`session_ttl_s`), pas
    permanente.
    `max_upload_bytes` : plafond vérifié pendant la copie de l'upload, pas
    sur un `Content-Length` que le client contrôle.
    `allow_fetch` : coupe-circuit de déploiement. Un serveur partagé sans
    contrôle sur qui peut déclencher un téléchargement de plusieurs
    centaines de Mo doit pouvoir désactiver la route sans redéployer.
    """

    cache_dir: Path = field(default_factory=default_cache_dir)
    work_dir: Path = field(default_factory=_default_work_dir)
    max_upload_bytes: int = 50 * 1024 * 1024
    session_ttl_s: float = 2 * 60 * 60
    allow_fetch: bool = True

    @classmethod
    def from_env(cls) -> WebSettings:
        """Réglages par défaut, avec `TRACEART_WEB_ALLOW_FETCH` en coupe-circuit.

        Les autres réglages ne sont volontairement pas pilotables par
        variable d'environnement : ce ne sont pas des préférences de
        déploiement, mais des choix de conception (voir docstring de la
        classe). N'exposer qu'un seul levier garde le contrat simple.
        """
        allow = os.environ.get("TRACEART_WEB_ALLOW_FETCH", "1") not in {"0", "false", "no"}
        return cls(allow_fetch=allow)
