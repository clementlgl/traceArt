"""Écriture du SVG.

Pas de bibliothèque tierce : on veut un contrôle exact sur la structure du
document, en particulier sur des calques nommés lisibles dans Inkscape
(`inkscape:groupmode="layer"` + `inkscape:label`), puisque le SVG produit
est destiné à être retouché à la main.

Toutes les coordonnées reçues ici sont déjà en unités de viewBox.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

import numpy as np

from traceart.core.model import Track
from traceart.core.stats import TrackStats
from traceart.render.label import CITY, COUNTRY, Label
from traceart.render.layout import Layout
from traceart.render.theme import LABEL_STYLES, LAYER_LABELS, LAYER_ORDER, Theme

# Géométrie de fond : (coordonnées, fermée ou non).
Geometry = tuple[np.ndarray, bool]
BasemapLayers = dict[str, list[Geometry]]

# 2 décimales = précision sub-pixel jusqu'à un rendu 100 000 px de large,
# et un `d=` deux fois plus court qu'en pleine précision flottante.
_COORD_DECIMALS = 2
# Espace vertical entre les blocs du bandeau d'annotations.
_BAND_GAP = 14.0
# Espace entre le filet et le sommet du profil d'élévation.
_PROFILE_TOP_GAP = 10.0
# Colonnes de mesures, de la droite vers la gauche.
_STAT_COLUMN_WIDTH = 108.0

_SVG_NS = "http://www.w3.org/2000/svg"
_INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
_SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"


@dataclass(frozen=True, slots=True)
class Annotation:
    """Textes libres du bandeau. Les mesures viennent des `TrackStats`."""

    title: str | None = None
    subtitle: str | None = None
    # Rien par défaut : le bandeau se demande explicitement, sinon
    # `Annotation(show_profile=True)` réserverait la place d'un bloc de
    # texte vide.
    show_stats: bool = False
    show_profile: bool = False


@dataclass(slots=True)
class RenderRequest:
    tracks: list[Track]
    layout: Layout
    theme: Theme
    stats: list[TrackStats] = field(default_factory=list)
    basemap: BasemapLayers = field(default_factory=dict)
    annotation: Annotation = field(default_factory=Annotation)
    labels: list[Label] = field(default_factory=list)


def annotation_band_height(theme: Theme, ann: Annotation) -> float:
    """Hauteur à réserver en pied de page pour le bandeau.

    Appelée avant `fit_layout` : la mise en page doit connaître cette
    hauteur pour ne pas faire passer la trace sous le texte.
    """
    has_text = bool(ann.title or ann.subtitle or ann.show_stats)
    has_profile = bool(ann.show_profile) and bool(theme.get("profile.enabled", True))
    if not (has_text or has_profile):
        return 0.0

    # `page.annotation_band` couvre le filet et le bloc de texte. Sans
    # texte, réserver ces 104 unités laisserait une bande vide sous le
    # profil : on ne compte alors que les espacements réellement utilisés.
    height = (
        theme.num("page.annotation_band", 104.0)
        if has_text
        else _BAND_GAP + _PROFILE_TOP_GAP
    )
    if has_profile:
        height += theme.num("profile.height", 58.0) + _BAND_GAP
    return height


def _f(value: float) -> str:
    """Nombre compact et déterministe : pas de `1.0`, pas de `-0.00`."""
    text = f"{value:.{_COORD_DECIMALS}f}"
    if text in {"-0.00", "0.00"}:
        return "0"
    return text.rstrip("0").rstrip(".")


# Troncature des décimales inutiles sur un `d=` déjà assemblé : `12.50` →
# `12.5`, `12.00` → `12`. Une passe de regex sur la chaîne entière coûte
# bien moins qu'un appel de fonction Python par coordonnée.
_TRIM_ZEROS = re.compile(r"(\.\d*?)0+(?=[,LMZ]|$)")
_TRIM_DOT = re.compile(r"\.(?=[,LMZ]|$)")
_MINUS_ZERO = re.compile(r"(?<![\d.])-0(?=[,LMZ]|$)")


def _path_d(coords: np.ndarray, closed: bool = False) -> str:
    """Attribut `d` d'un chemin SVG, à deux décimales.

    Assemblé en une seule interpolation `%` plutôt qu'un formatage par
    coordonnée : sur un rendu chargé, cette fonction est appelée des
    milliers de fois pour des centaines de milliers de points, et la
    version naïve représentait à elle seule un tiers du temps de rendu.
    """
    count = len(coords)
    if count == 0:
        return ""
    template = "M%.2f,%.2f" + "L%.2f,%.2f" * (count - 1)
    body = template % tuple(coords.ravel().tolist())
    body = _MINUS_ZERO.sub("0", _TRIM_DOT.sub("", _TRIM_ZEROS.sub(r"\1", body)))
    return body + ("Z" if closed else "")


def _attrs(pairs: dict[str, object]) -> str:
    """Attributs XML, clés triées pour une sortie reproductible."""
    out = []
    for key in sorted(pairs):
        value = pairs[key]
        if value is None or value == "":
            continue
        out.append(f"{key}={quoteattr(str(value))}")
    return (" " + " ".join(out)) if out else ""


def _open_layer(layer_id: str, label: str, extra: dict[str, object] | None = None) -> str:
    attrs = {
        "id": layer_id,
        "inkscape:label": label,
        "inkscape:groupmode": "layer",
    }
    attrs.update(extra or {})
    return f"  <g{_attrs(attrs)}>"


def _format_km(metres: float) -> str:
    km = metres / 1000.0
    text = f"{km:,.0f}" if km >= 100 else f"{km:,.1f}"
    return text.replace(",", " ") + " km"


def _format_metres(metres: float) -> str:
    rounded = round(metres)
    # round(-0.4) vaut -0, qui s'affiche « -0 m ».
    text = "0" if rounded == 0 else f"{rounded:,.0f}"
    return text.replace(",", " ") + " m"


def _aggregate(stats: list[TrackStats]) -> dict[str, str]:
    """Mesures affichables, cumulées sur toutes les traces rendues."""
    if not stats:
        return {}
    distance = sum(s.distance_m for s in stats)
    ascent = sum(s.ascent_m for s in stats)
    duration = sum(s.duration_s for s in stats)
    out = {"DISTANCE": _format_km(distance)}
    if ascent > 0:
        out["DÉNIVELÉ +"] = _format_metres(ascent)
    if duration > 0:
        out["DURÉE"] = stats[0].format_duration(duration)
    return {k: v for k, v in out.items() if v}


def _date_range(stats: list[TrackStats]) -> str | None:
    starts = [s.start_time for s in stats if s.start_time]
    ends = [s.end_time for s in stats if s.end_time]
    if not starts:
        return None
    first = min(starts).strftime("%d.%m.%Y")
    last = max(ends).strftime("%d.%m.%Y") if ends else first
    return first if first == last else f"{first} — {last}"


def _render_basemap(req: RenderRequest) -> list[str]:
    """Un calque par thème de fond, dans l'ordre de dessin du thème."""
    lines: list[str] = []
    for name in LAYER_ORDER:
        geoms = req.basemap.get(name)
        if not geoms:
            continue
        style = req.theme.layer(name)
        group_attrs = {
            "fill": style.get("fill", "none"),
            "stroke": style.get("stroke", "none"),
            "stroke-width": style.get("width", 0.0) or None,
            "stroke-dasharray": style.get("dash"),
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
            "opacity": style.get("opacity", 1.0) if style.get("opacity", 1.0) != 1.0 else None,
        }
        lines.append(
            _open_layer(
                f"basemap-{name}",
                f"Fond · {LAYER_LABELS.get(name, name)}",
                group_attrs,
            )
        )
        for coords, closed in geoms:
            d = _path_d(coords, closed)
            if d:
                lines.append(f'    <path d="{d}"/>')
        lines.append("  </g>")
    return lines


def _render_labels(req: RenderRequest) -> list[str]:
    """Labels de fond, dessinés par-dessus la trace et cernés d'un halo.

    Un calque par nature de label : les noms de pays passent dessous, en
    capitales espacées et sans point de localisation — un pays n'est pas
    un point ; les villes par-dessus, avec leur pastille.

    Deux `<text>` superposés plutôt que `paint-order="stroke"` : l'ordre
    de peinture n'est pas honoré par tous les rasteriseurs, alors qu'un
    contour dessiné puis un remplissage par-dessus l'est partout.
    """
    if not req.labels:
        return []
    lines: list[str] = []
    # Pays d'abord : ils servent de fond aux noms de villes.
    for kind, title in ((COUNTRY, "Labels · pays"), (CITY, "Labels · villes")):
        group = [label for label in req.labels if label.kind == kind]
        if group:
            lines.extend(_render_label_group(req.theme, group, kind, title))
    return lines


def _render_label_group(
    theme: Theme, labels: list[Label], kind: str, title: str
) -> list[str]:
    style = theme.layer(LABEL_STYLES.get(kind, "labels"))
    color = str(style.get("color", "#7a7367"))
    halo = theme.color("page.background", "#ffffff")
    radius = float(style.get("dot_radius", 1.6))
    halo_width = float(style.get("halo", 2.6))
    upper = bool(style.get("uppercase", False))
    # Sans pastille, le texte se centre sur la position au lieu de se
    # poser à sa droite.
    anchor = "middle" if radius <= 0 else "start"

    lines = [
        _open_layer(
            f"labels-{kind}",
            title,
            {
                "font-family": theme.get("typography.family"),
                "font-size": style.get("size", 11.0),
                "font-weight": style.get("weight", 400),
                "letter-spacing": style.get("letter_spacing", 1.2),
                "text-anchor": None if anchor == "start" else anchor,
                "stroke-linejoin": "round",
            },
        )
    ]
    for label in labels:
        text = escape(label.text.upper() if upper else label.text)
        tx = _f(label.x + (radius * 2.5 if radius > 0 else 0.0))
        ty = _f(label.y + radius)
        if radius > 0:
            lines.append(
                f'    <circle cx="{_f(label.x)}" cy="{_f(label.y)}" '
                f'r="{_f(radius + halo_width / 2)}" fill="{halo}"/>'
            )
        lines.append(
            f'    <text x="{tx}" y="{ty}" fill="none" stroke="{halo}" '
            f'stroke-width="{_f(halo_width)}">{text}</text>'
        )
        if radius > 0:
            lines.append(
                f'    <circle cx="{_f(label.x)}" cy="{_f(label.y)}" '
                f'r="{_f(radius)}" fill="{color}"/>'
            )
        lines.append(f'    <text x="{tx}" y="{ty}" fill="{color}">{text}</text>')
    lines.append("  </g>")
    return lines


def _trace_paths(track: Track) -> list[str]:
    return [_path_d(seg.coords) for seg in track.segments if len(seg) >= 2]


def _render_traces(req: RenderRequest) -> list[str]:
    theme = req.theme
    halo_width = theme.num("trace.halo", 0.0)
    width = theme.num("trace.width", 3.0)
    cap = theme.get("trace.cap", "round")
    join = theme.get("trace.join", "round")

    lines: list[str] = []

    # Halo : un seul calque sous toutes les traces, sinon la trace n° 2
    # verrait son halo effacer la trace n° 1 à chaque croisement.
    if halo_width > 0:
        lines.append(
            _open_layer(
                "trace-halo",
                "Trace · halo",
                {
                    "fill": "none",
                    "stroke": theme.color("trace.halo_color", "#ffffff"),
                    "stroke-width": halo_width,
                    "stroke-linecap": cap,
                    "stroke-linejoin": join,
                },
            )
        )
        for track in req.tracks:
            lines.extend(f'    <path d="{d}"/>' for d in _trace_paths(track))
        lines.append("  </g>")

    lines.append(
        _open_layer(
            "trace",
            "Trace",
            {
                "fill": "none",
                "stroke-width": width,
                "stroke-linecap": cap,
                "stroke-linejoin": join,
                "opacity": theme.num("trace.opacity", 1.0)
                if theme.num("trace.opacity", 1.0) != 1.0
                else None,
            },
        )
    )
    for index, track in enumerate(req.tracks):
        color = theme.trace_color(index)
        paths = _trace_paths(track)
        if not paths:
            continue
        lines.append(f'    <g id="trace-{index}" stroke="{color}">')
        lines.extend(f'      <path d="{d}"/>' for d in paths)
        lines.append("    </g>")
    lines.append("  </g>")

    lines.extend(_render_markers(req))
    return lines


def _render_markers(req: RenderRequest) -> list[str]:
    theme = req.theme
    show_start = bool(theme.get("trace.marker.start", True))
    show_end = bool(theme.get("trace.marker.end", True))
    if not (show_start or show_end):
        return []

    radius = theme.num("trace.marker.radius", 5.0)
    ring = theme.color("trace.marker.stroke", "#ffffff")
    ring_width = theme.num("trace.marker.stroke_width", 2.0)

    lines = [_open_layer("trace-markers", "Trace · départ / arrivée")]
    for index, track in enumerate(req.tracks):
        usable = [s for s in track.segments if len(s)]
        if not usable:
            continue
        color = theme.trace_color(index)
        if show_start:
            x, y = usable[0].coords[0]
            lines.append(
                f'    <circle cx="{_f(x)}" cy="{_f(y)}" r="{_f(radius)}" '
                f'fill="{color}" stroke="{ring}" stroke-width="{_f(ring_width)}"/>'
            )
        if show_end:
            x, y = usable[-1].coords[-1]
            lines.append(
                f'    <circle cx="{_f(x)}" cy="{_f(y)}" r="{_f(radius)}" '
                f'fill="{ring}" stroke="{color}" stroke-width="{_f(ring_width)}"/>'
            )
    lines.append("  </g>")
    return lines


def _render_profile(req: RenderRequest, top: float) -> list[str]:
    """Profil d'élévation, en aire pleine sur toute la largeur utile."""
    theme = req.theme
    usable = [s for s in req.stats if s.has_elevation]
    if not usable:
        return []

    height = theme.num("profile.height", 58.0)
    layout = req.layout
    left = layout.margins.left
    right = layout.width - layout.margins.right
    span = right - left
    if span <= 0 or height <= 0:
        return []

    # Chaque trace porte une distance cumulée qui repart de zéro : sans
    # décalage, les profils se superposent et la courbe revient en arrière
    # d'une trace à la suivante.
    cursor = 0.0
    shifted = []
    for stat in usable:
        shifted.append(stat.profile_dist + cursor)
        cursor += stat.distance_m
    dist = np.concatenate(shifted)
    ele = np.concatenate([s.profile_ele for s in usable])
    finite = np.isfinite(dist) & np.isfinite(ele)
    dist, ele = dist[finite], ele[finite]
    if len(dist) < 2:
        return []

    d_span = float(dist.max() - dist.min()) or 1.0
    e_min, e_max = float(ele.min()), float(ele.max())
    e_span = (e_max - e_min) or 1.0

    px = left + (dist - dist.min()) / d_span * span
    py = top + height - (ele - e_min) / e_span * height
    pts = np.column_stack([px, py])

    # Sous-échantillonnage : au-delà d'un point par unité de viewBox, le
    # profil ne gagne rien en lisibilité et alourdit le fichier.
    if len(pts) > int(span):
        idx = np.linspace(0, len(pts) - 1, int(span)).astype(int)
        pts = pts[idx]

    floor = _f(top + height)
    area = (
        _path_d(pts)
        + f"L{_f(pts[-1, 0])},{floor}L{_f(pts[0, 0])},{floor}Z"
    )
    baseline = theme.color("profile.baseline", "#cccccc")
    return [
        _open_layer("profile", "Profil d'élévation"),
        f'    <path d="{area}" fill="{theme.color("profile.fill", "none")}" stroke="none"/>',
        f'    <path d="{_path_d(pts)}" fill="none" '
        f'stroke="{theme.color("profile.stroke", "#000000")}" '
        f'stroke-width="{_f(theme.num("profile.width", 1.4))}" '
        'stroke-linecap="round" stroke-linejoin="round"/>',
        f'    <line x1="{_f(left)}" y1="{_f(top + height)}" x2="{_f(right)}" '
        f'y2="{_f(top + height)}" stroke="{baseline}" stroke-width="0.6"/>',
        f'    <text x="{_f(left)}" y="{_f(top - 4)}" '
        f'font-family={quoteattr(str(theme.get("typography.family")))} '
        f'font-size="{_f(theme.num("typography.stat_label_size", 9.5))}" '
        f'fill="{theme.color("typography.muted", "#888888")}">'
        f"{escape(_format_metres(e_min))} – {escape(_format_metres(e_max))}</text>",
        "  </g>",
    ]


def _render_annotations(req: RenderRequest) -> list[str]:
    ann = req.annotation
    theme = req.theme
    layout = req.layout
    band = annotation_band_height(theme, ann)
    if band <= 0:
        return []

    left = layout.margins.left
    right = layout.width - layout.margins.right
    band_top = layout.height - band

    # Structure de calques constante quel que soit le contenu du bandeau :
    # filet, puis profil, puis texte. Sans cela le panneau Inkscape change
    # de forme d'un rendu à l'autre.
    lines: list[str] = [
        _open_layer("annotations-rule", "Annotations · filet"),
        f'    <line x1="{_f(left)}" y1="{_f(band_top)}" x2="{_f(right)}" y2="{_f(band_top)}" '
        f'stroke="{theme.color("rule.color", "#cccccc")}" '
        f'stroke-width="{_f(theme.num("rule.width", 0.8))}"/>',
        "  </g>",
    ]

    cursor = band_top + _BAND_GAP
    if ann.show_profile and theme.get("profile.enabled", True):
        profile = _render_profile(req, cursor + _PROFILE_TOP_GAP)
        if profile:
            lines.extend(profile)
            cursor += theme.num("profile.height", 58.0) + _BAND_GAP + _PROFILE_TOP_GAP

    if not (ann.title or ann.subtitle or ann.show_stats):
        return lines

    lines.append(_open_layer("annotations", "Annotations · texte"))

    family = str(theme.get("typography.family"))
    title_size = theme.num("typography.title_size", 42.0)
    baseline = cursor + title_size * 0.85

    if ann.title:
        lines.append(
            f'    <text x="{_f(left)}" y="{_f(baseline)}" '
            f"font-family={quoteattr(family)} "
            f'font-size="{_f(title_size)}" '
            f'font-weight="{theme.get("typography.title_weight", 600)}" '
            f'letter-spacing="{_f(theme.num("typography.title_letter_spacing", 1.5))}" '
            f'fill="{theme.color("typography.color", "#000000")}">{escape(ann.title)}</text>'
        )
    if ann.subtitle:
        subtitle_y = baseline + theme.num("typography.subtitle_size", 15.0) + 8
        lines.append(
            f'    <text x="{_f(left)}" y="{_f(subtitle_y)}" '
            f"font-family={quoteattr(family)} "
            f'font-size="{_f(theme.num("typography.subtitle_size", 15.0))}" '
            f'fill="{theme.color("typography.muted", "#888888")}">{escape(ann.subtitle)}</text>'
        )

    if ann.show_stats:
        measures = _aggregate(req.stats)
        label_size = theme.num("typography.stat_label_size", 9.5)
        value_size = theme.num("typography.stat_value_size", 17.0)
        for column, (label, value) in enumerate(reversed(list(measures.items()))):
            x = right - column * _STAT_COLUMN_WIDTH
            lines.append(
                f'    <text x="{_f(x)}" y="{_f(baseline - value_size - 4)}" text-anchor="end" '
                f"font-family={quoteattr(family)} "
                f'font-size="{_f(label_size)}" '
                f'letter-spacing="{_f(theme.num("typography.stat_label_letter_spacing", 1.8))}" '
                f'fill="{theme.color("typography.muted", "#888888")}">{escape(label)}</text>'
            )
            lines.append(
                f'    <text x="{_f(x)}" y="{_f(baseline)}" text-anchor="end" '
                f"font-family={quoteattr(family)} "
                f'font-size="{_f(value_size)}" '
                f'fill="{theme.color("typography.color", "#000000")}">{escape(value)}</text>'
            )

    lines.append("  </g>")
    return lines


def render_svg(req: RenderRequest, *, size: str = "2000px") -> str:
    """Sérialise la requête de rendu en document SVG complet."""
    layout = req.layout
    theme = req.theme
    width_attr, height_attr = _document_size(size, layout)

    header = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        "<svg"
        f' xmlns="{_SVG_NS}"'
        f' xmlns:inkscape="{_INKSCAPE_NS}"'
        f' xmlns:sodipodi="{_SODIPODI_NS}"'
        f' width="{width_attr}" height="{height_attr}"'
        f' viewBox="0 0 {_f(layout.width)} {_f(layout.height)}"'
        ' version="1.1">',
        f"  <title>{escape(req.annotation.title or 'TraceArt')}</title>",
        f'  <desc>Généré par TraceArt · thème {escape(theme.name)}</desc>',
        _open_layer("background", "Fond"),
        f'    <rect x="0" y="0" width="{_f(layout.width)}" height="{_f(layout.height)}" '
        f'fill="{theme.color("page.background", "#ffffff")}"/>',
        "  </g>",
    ]

    body: list[str] = []
    body.extend(_render_basemap(req))
    body.extend(_render_traces(req))
    # Après la trace : un nom de ville sous un trait de 3 unités est
    # illisible.
    body.extend(_render_labels(req))
    body.extend(_render_annotations(req))

    return "\n".join([*header, *body, "</svg>", ""])


def _document_size(size: str, layout: Layout) -> tuple[str, str]:
    """`size` = grand côté du document, ex. `2000px`, `297mm`, `24cm`.

    Le viewBox reste inchangé : seule la taille physique déclarée varie,
    ce qui rend le SVG imprimable sans toucher au rendu.
    """
    text = size.strip().lower()
    unit = ""
    for candidate in ("px", "mm", "cm", "in", "pt"):
        if text.endswith(candidate):
            unit = candidate
            text = text[: -len(candidate)]
            break
    try:
        long_edge = float(text)
    except ValueError as exc:
        raise ValueError(f"taille invalide : {size!r} (attendu ex. 2000px, 297mm)") from exc
    if long_edge <= 0:
        raise ValueError(f"taille invalide : {size!r}")

    ratio = layout.width / layout.height
    if ratio >= 1.0:
        width, height = long_edge, long_edge / ratio
    else:
        width, height = long_edge * ratio, long_edge
    return f"{_f(width)}{unit}", f"{_f(height)}{unit}"
