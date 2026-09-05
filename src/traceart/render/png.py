"""Export PNG, optionnel.

`cairosvg` est une dépendance extra (`traceart[png]`) : le SVG est la
sortie de référence, le PNG un confort. L'import est donc paresseux, pour
que l'outil reste utilisable sans Cairo installé.
"""

from __future__ import annotations

from pathlib import Path

_UNIT_TO_INCH = {"px": None, "mm": 25.4, "cm": 2.54, "in": 1.0, "pt": 72.0}


class PngUnavailable(RuntimeError):
    """cairosvg absent ou Cairo non installé sur le système."""


def target_width_px(size: str, *, dpi: int, scale: float) -> int:
    """Largeur de rendu en pixels pour le grand côté déclaré du document.

    Une taille physique (`297mm`) est convertie via le DPI ; une taille
    déjà en pixels est simplement multipliée par `scale`.
    """
    text = size.strip().lower()
    unit = "px"
    for candidate in _UNIT_TO_INCH:
        if text.endswith(candidate):
            unit = candidate
            text = text[: -len(candidate)]
            break
    value = float(text)
    divisor = _UNIT_TO_INCH[unit]
    if divisor is None:
        return max(1, round(value * scale))
    return max(1, round(value / divisor * dpi))


def svg_to_png(svg: str, out_path: Path, *, width_px: int) -> Path:
    """Rasterise une chaîne SVG. Lève `PngUnavailable` si Cairo manque."""
    try:
        import cairosvg
    except (ImportError, OSError) as exc:
        raise PngUnavailable(
            "export PNG indisponible : installe `traceart[png]` "
            "(et libcairo2 côté système)"
        ) from exc

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cairosvg.svg2png(
        bytestring=svg.encode("utf-8"),
        write_to=str(out_path),
        output_width=width_px,
    )
    return out_path
