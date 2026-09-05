"""Label de fond de carte, prêt à poser sur la page.

Type de présentation : il vit dans `render/` et non dans `basemap/`,
pour que la construction du fond n'ait rien à savoir du rendu. Le
renderer, lui, a le droit de connaître ses propres types.
"""

from __future__ import annotations

from dataclasses import dataclass

CITY = "city"
COUNTRY = "country"


@dataclass(frozen=True, slots=True)
class Label:
    """Texte de fond à poser, en unités viewBox.

    `kind` sélectionne le style : `city` pour une ville, `country` pour
    un nom de pays — conventionnellement plus grand, espacé et en
    capitales.
    """

    x: float
    y: float
    text: str
    kind: str = CITY
