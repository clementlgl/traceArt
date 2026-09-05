"""Recherche de lieux par nom, via Nominatim (OpenStreetMap).

Écart assumé à la philosophie « hors ligne » du reste du projet : c'est
la seule fonctionnalité de TraceArt qui appelle un service réseau au
moment de l'usage plutôt qu'au moment d'un téléchargement explicite. Le
rendu lui-même (`pipeline.run`) n'en dépend à aucun moment — seule la
recherche interactive côté web en a besoin, pour convertir un nom de
ville tapé par l'utilisateur en coordonnées.

Réservé au web : le CLI reste utilisable hors ligne (`--label
"Nom:lon,lat"` prend des coordonnées explicites, voir `pipeline.py`).

Ce module n'est jamais importé par `core/`, `render/`, `basemap/` ni
`cli/` — l'invariant de direction des dépendances
(`tests/test_invariants.py`) le vérifie.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from traceart.errors import UnavailableError

# Nominatim exige un User-Agent identifiant l'application (politique
# d'usage : https://operations.osmfoundation.org/policies/nominatim/).
# Volontairement générique — jamais l'e-mail ou l'identité de
# l'utilisateur final.
USER_AGENT = "traceart/0.5 (+https://github.com/)"
ENDPOINT = "https://nominatim.openstreetmap.org/search"
TIMEOUT_S = 8.0


class GeocodeError(UnavailableError):
    """Nominatim indisponible, ou réponse inexploitable."""


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    """Un lieu trouvé par la recherche, prêt à devenir un label."""

    name: str
    lat: float
    lon: float


def search_places(query: str, *, limit: int = 8) -> list[GeocodeResult]:
    """Cherche des lieux par nom. Renvoie au plus `limit` résultats.

    Une seule fonction, sans état : facile à substituer dans les tests
    (`monkeypatch.setattr(".search_places", ...)`) sans avoir à
    instancier quoi que ce soit.
    """
    query = query.strip()
    if not query:
        return []

    params = urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": max(1, min(limit, 20)), "accept-language": "fr"}
    )
    request = urllib.request.Request(
        f"{ENDPOINT}?{params}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GeocodeError(f"recherche indisponible : {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GeocodeError("réponse de recherche illisible") from exc

    if not isinstance(payload, list):
        raise GeocodeError("réponse de recherche inattendue")

    results: list[GeocodeResult] = []
    for entry in payload:
        try:
            lat = float(entry["lat"])
            lon = float(entry["lon"])
            display_name = str(entry["display_name"])
        except (KeyError, TypeError, ValueError):
            continue
        # `name` (jsonv2) est le nom court du lieu lui-même ; `display_name`
        # est l'adresse complète ("Lyon, Métropole de Lyon, Rhône, ...").
        # Un label de carte veut le nom court, pas l'adresse.
        name = str(entry.get("name") or "").strip() or display_name.split(",")[0].strip()
        if name:
            results.append(GeocodeResult(name=name, lat=lat, lon=lon))
    return results
