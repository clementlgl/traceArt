from __future__ import annotations

import re
from xml.etree import ElementTree

import numpy as np
import pytest

from tests.conftest import segment
from traceart.basemap.query import Label
from traceart.core.model import Track
from traceart.core.stats import compute_stats
from traceart.render.layout import fit_layout
from traceart.render.svg import (
    Annotation,
    RenderRequest,
    annotation_band_height,
    render_svg,
)
from traceart.render.theme import load_theme

SVG_NS = "{http://www.w3.org/2000/svg}"
INKSCAPE_NS = "{http://www.inkscape.org/namespaces/inkscape}"


def _request(**kwargs) -> RenderRequest:
    track = Track(segments=[segment([(100.0, 100.0), (400.0, 300.0), (700.0, 150.0)])])
    layout = fit_layout((0.0, 0.0, 800.0, 400.0), long_edge=1000.0, margin=50.0)
    defaults = {
        "tracks": [track],
        "layout": layout,
        "theme": load_theme("light"),
        "annotation": Annotation(title="Test"),
    }
    defaults.update(kwargs)
    return RenderRequest(**defaults)


def _parse(svg: str) -> ElementTree.Element:
    return ElementTree.fromstring(svg)


def test_output_is_well_formed_xml():
    assert _parse(render_svg(_request())).tag == f"{SVG_NS}svg"


def test_layers_are_named_for_inkscape():
    root = _parse(render_svg(_request()))
    layers = {
        g.get(f"{INKSCAPE_NS}label")
        for g in root.iter(f"{SVG_NS}g")
        if g.get(f"{INKSCAPE_NS}groupmode") == "layer"
    }
    assert {"Fond", "Trace", "Annotations · filet", "Annotations · texte"} <= layers


def test_viewbox_matches_layout():
    req = _request()
    root = _parse(render_svg(req))
    assert root.get("viewBox") == f"0 0 {req.layout.width:.0f} {req.layout.height:.0f}"


def test_document_size_units_preserved():
    root = _parse(render_svg(_request(), size="297mm"))
    assert root.get("width").endswith("mm")
    assert root.get("height").endswith("mm")


def test_invalid_size_rejected():
    with pytest.raises(ValueError, match="taille invalide"):
        render_svg(_request(), size="grand")


def test_rendering_is_deterministic():
    assert render_svg(_request()) == render_svg(_request())


def test_halo_is_a_single_layer_under_all_traces():
    a = Track(segments=[segment([(0.0, 0.0), (500.0, 500.0)])])
    b = Track(segments=[segment([(500.0, 0.0), (0.0, 500.0)])])
    root = _parse(render_svg(_request(tracks=[a, b])))
    halos = [g for g in root.iter(f"{SVG_NS}g") if g.get("id") == "trace-halo"]
    assert len(halos) == 1
    # Les deux traces sont dans ce calque unique : sinon le halo de la
    # seconde effacerait la première à chaque croisement.
    assert len(list(halos[0].iter(f"{SVG_NS}path"))) == 2


def test_each_track_gets_its_palette_colour():
    a = Track(segments=[segment([(0.0, 0.0), (500.0, 500.0)])])
    b = Track(segments=[segment([(500.0, 0.0), (0.0, 500.0)])])
    theme = load_theme("light")
    root = _parse(render_svg(_request(tracks=[a, b], theme=theme)))
    # `trace-0`, `trace-1`… uniquement : `trace-halo` porte aussi un
    # stroke, celui de la couleur de page.
    strokes = [
        g.get("stroke")
        for g in root.iter(f"{SVG_NS}g")
        if re.fullmatch(r"trace-\d+", g.get("id") or "")
    ]
    assert strokes == theme.trace_colors()[:2]


def test_basemap_layer_uses_theme_style():
    ring = np.asarray([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 0.0)])
    root = _parse(render_svg(_request(basemap={"water": [(ring, True)]})))
    water = next(g for g in root.iter(f"{SVG_NS}g") if g.get("id") == "basemap-water")
    assert water.get("fill") == "#dde6ec"
    # Nom lisible dans le panneau Inkscape, pas la clé technique.
    assert water.get(f"{INKSCAPE_NS}label") == "Fond · plans d'eau"
    assert next(water.iter(f"{SVG_NS}path")).get("d").endswith("Z")


def test_annotation_band_grows_with_profile():
    theme = load_theme("light")
    without = annotation_band_height(theme, Annotation(title="x"))
    with_profile = annotation_band_height(theme, Annotation(title="x", show_profile=True))
    assert with_profile > without
    assert annotation_band_height(theme, Annotation(show_stats=False)) == 0.0


def test_profile_advances_monotonically_across_tracks():
    # Deux traces successives : le profil doit se dérouler bout à bout,
    # jamais revenir en arrière.
    tracks, stats = [], []
    for lon in (3.0, 5.0):
        seg = segment(
            [(lon, 44.0), (lon + 0.1, 44.05), (lon + 0.2, 44.1)],
            ele=[100.0, 300.0, 200.0],
        )
        wgs = Track(segments=[seg])
        tracks.append(wgs)
        stats.append(compute_stats(wgs))
    svg = render_svg(_request(stats=stats, annotation=Annotation(show_profile=True)))
    profile = next(
        g for g in _parse(svg).iter(f"{SVG_NS}g") if g.get("id") == "profile"
    )
    # Le premier path est l'aire, qui se referme sur son point de
    # départ : c'est la ligne du profil qu'on inspecte.
    line = next(p for p in profile.iter(f"{SVG_NS}path") if p.get("fill") == "none")
    d = line.get("d")
    xs = [float(chunk.split(",")[0]) for chunk in d.replace("M", "L").split("L") if chunk]
    assert xs == sorted(xs)


def test_text_is_xml_escaped():
    svg = render_svg(_request(annotation=Annotation(title="Alpes & <Cévennes>")))
    assert "&amp;" in svg and "&lt;Cévennes&gt;" in svg
    assert _parse(svg) is not None


def test_coordinates_are_trimmed():
    track = Track(segments=[segment([(1.0, 2.0), (3.456789, 4.123456)])])
    svg = render_svg(_request(tracks=[track]))
    # 2 décimales max, pas de zéros de queue.
    assert ".456789" not in svg


def test_labels_are_drawn_after_the_trace():
    svg = render_svg(_request(labels=[Label(100.0, 100.0, "Lyon")]))
    # Un nom de ville sous un trait de trace est illisible.
    assert svg.index('id="trace"') < svg.index('id="labels-city"')


def test_labels_carry_a_halo_under_the_fill():
    theme = load_theme("light")
    svg = render_svg(_request(labels=[Label(100.0, 100.0, "Lyon")], theme=theme))
    root = _parse(svg)
    layer = next(g for g in root.iter(f"{SVG_NS}g") if g.get("id") == "labels-city")
    texts = list(layer.iter(f"{SVG_NS}text"))
    assert len(texts) == 2
    # Le contour d'abord, le remplissage par-dessus.
    assert texts[0].get("stroke") == theme.color("page.background")
    assert texts[0].get("fill") == "none"
    assert texts[1].get("fill") == theme.layer("labels")["color"]
    assert texts[1].get("stroke") is None


def test_country_labels_drawn_under_city_labels():
    svg = render_svg(
        _request(
            labels=[
                Label(400.0, 200.0, "Italie", kind="country"),
                Label(100.0, 100.0, "Lugano"),
            ]
        )
    )
    assert svg.index('id="labels-country"') < svg.index('id="labels-city"')
    # Et les deux après la trace.
    assert svg.index('id="trace"') < svg.index('id="labels-country"')


def test_country_labels_are_uppercase_and_centred():
    theme = load_theme("light")
    svg = render_svg(_request(labels=[Label(400.0, 200.0, "Italie", kind="country")], theme=theme))
    root = _parse(svg)
    layer = next(g for g in root.iter(f"{SVG_NS}g") if g.get("id") == "labels-country")
    assert layer.get("text-anchor") == "middle"
    texts = [t.text for t in layer.iter(f"{SVG_NS}text")]
    assert texts == ["ITALIE", "ITALIE"]
    # Un pays n'est pas un point : pas de pastille de localisation.
    assert not list(layer.iter(f"{SVG_NS}circle"))


def test_country_style_comes_from_its_own_theme_key():
    theme = load_theme("light")
    assert theme.layer("countries")["color"] != theme.layer("labels")["color"]
    assert theme.layer("countries")["size"] > theme.layer("labels")["size"]
