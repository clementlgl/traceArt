"""Tests de la rasterisation PNG.

`svg_to_png_bytes` est la primitive : `svg_to_png` (écriture disque) et un
futur appel HTTP (réponse `image/png` sans fichier temporaire)
s'appuient tous deux dessus.
"""

from __future__ import annotations

import pytest

from traceart.render.png import (
    PngUnavailable,
    svg_to_png,
    svg_to_png_bytes,
    target_width_px,
)

_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    '<rect width="10" height="10" fill="red"/></svg>'
)


def test_svg_to_png_bytes_returns_a_valid_png():
    data = svg_to_png_bytes(_SVG, width_px=20)
    assert isinstance(data, bytes)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")


def test_svg_to_png_bytes_honours_requested_width():
    small = svg_to_png_bytes(_SVG, width_px=20)
    large = svg_to_png_bytes(_SVG, width_px=200)
    # Pas de décodage PNG ici : une image dix fois plus large pèse plus
    # lourd, c'est un signe suffisant que la largeur demandée est honorée.
    assert len(large) > len(small)


def test_svg_to_png_writes_the_same_bytes_to_disk(tmp_path):
    """`svg_to_png` ne doit être qu'un `svg_to_png_bytes` suivi d'une
    écriture — pas un second chemin de rasterisation."""
    target = tmp_path / "out.png"
    svg_to_png(_SVG, target, width_px=40)
    assert target.read_bytes() == svg_to_png_bytes(_SVG, width_px=40)


def test_svg_to_png_creates_parent_directories(tmp_path):
    target = tmp_path / "a" / "b" / "out.png"
    svg_to_png(_SVG, target, width_px=20)
    assert target.is_file()


def test_png_unavailable_when_cairosvg_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "cairosvg":
            raise ImportError("no cairosvg")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(PngUnavailable, match="traceart\\[png\\]"):
        svg_to_png_bytes(_SVG, width_px=20)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ("2000px", 2000),
        ("1000px", 1000),
    ],
)
def test_target_width_px_pixels_use_scale(size, expected):
    assert target_width_px(size, dpi=300, scale=1.0) == expected
    assert target_width_px(size, dpi=300, scale=2.0) == expected * 2


def test_target_width_px_physical_units_use_dpi():
    # 297 mm à 300 DPI ≈ format A4 grand côté.
    px = target_width_px("297mm", dpi=300, scale=1.0)
    assert 3500 < px < 3510
