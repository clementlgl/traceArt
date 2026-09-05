"""Racine de la hiérarchie d'exceptions.

Le projet définissait neuf exceptions sans ancêtre commun : la moitié
dérivait de `ValueError`, l'autre de `RuntimeError`. Un appelant ne
pouvait donc pas les attraper toutes, et le CLI laissait effectivement
`StoreError` et `BasemapError` remonter en traceback nue.

La double base est délibérée : `UserError` reste un `ValueError`,
`UnavailableError` reste un `RuntimeError`. Tout `except ValueError` ou
`except RuntimeError` déjà écrit continue de fonctionner, et la migration
n'a demandé aucun changement de site d'appel.

Ce module n'importe rien — il doit rester en bas de la pile.
"""

from __future__ import annotations


class TraceArtError(Exception):
    """Toute erreur émise par TraceArt."""


class UserError(TraceArtError, ValueError):
    """Entrée ou option fautive : l'utilisateur peut corriger et réessayer."""


class MissingDataError(UserError):
    """Données de fond absentes du cache.

    Distincte d'une simple erreur de saisie : la réponse attendue est une
    invitation à télécharger, pas un reproche. Les attributs portent de
    quoi construire cette invitation sans avoir à analyser le message.
    """

    def __init__(
        self,
        message: str,
        *,
        datasets: tuple[str, ...] = (),
        scale: str | None = None,
        layers: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.datasets = datasets
        self.scale = scale
        self.layers = layers


class UnavailableError(TraceArtError, RuntimeError):
    """Ressource ou dépendance indisponible : réseau, disque, extra absent.

    Rien à corriger dans la demande elle-même.
    """
