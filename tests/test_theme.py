from __future__ import annotations

import pytest

from traceart.render.theme import ThemeError, available_themes, load_theme


def test_all_shipped_themes_load():
    names = available_themes()
    assert {"light", "dark", "mono", "blueprint"} <= set(names)
    for name in names:
        theme = load_theme(name)
        assert theme.color("page.background").startswith("#")
        assert theme.num("trace.width") > 0
        assert theme.trace_colors()


def test_partial_theme_inherits_base():
    dark = load_theme("dark")
    # `dark.toml` ne redéfinit pas la typographie complète.
    assert dark.num("typography.title_size") == load_theme("light").num(
        "typography.title_size"
    )
    assert dark.color("page.background") == "#14171a"


def test_auto_color_resolves_to_page_background():
    light = load_theme("light")
    assert light.get("trace.halo_color") == "auto"
    assert light.color("trace.halo_color") == light.color("page.background")


def test_trace_color_cycles_through_palette():
    theme = load_theme("light")
    palette = theme.trace_colors()
    assert theme.trace_color(0) == palette[0]
    assert theme.trace_color(len(palette)) == palette[0]


def test_with_trace_color_overrides_only_the_palette():
    """Le reste du thème (fond, couches, typographie) doit rester
    intact : c'est une surcharge ponctuelle, pas un nouveau thème."""
    theme = load_theme("dark")
    customized = theme.with_trace_color("#00ff00")

    assert customized.trace_colors() == ["#00ff00"]
    assert customized.trace_color(0) == "#00ff00"
    assert customized.trace_color(7) == "#00ff00"  # boucle sur une palette à 1 élément

    assert customized.color("page.background") == theme.color("page.background")
    assert customized.layer("water") == theme.layer("water")
    assert customized.label == theme.label


def test_with_trace_color_does_not_mutate_the_original():
    theme = load_theme("light")
    original_colors = theme.trace_colors()
    theme.with_trace_color("#123456")
    assert theme.trace_colors() == original_colors


def test_user_theme_file_merges_over_base(tmp_path):
    path = tmp_path / "custom.toml"
    path.write_text(
        '[meta]\nname = "custom"\n[trace]\nwidth = 9.5\n', encoding="utf-8"
    )
    theme = load_theme(str(path))
    assert theme.name == "custom"
    assert theme.num("trace.width") == 9.5
    # Hérité de light, absent du fichier utilisateur.
    assert theme.layer("water")["fill"] == "#dde6ec"
    assert theme.num("page.long_edge") == 1000.0


def test_unknown_theme_lists_alternatives():
    with pytest.raises(ThemeError, match="disponibles"):
        load_theme("neon")


def test_missing_theme_file_rejected(tmp_path):
    with pytest.raises(ThemeError, match="introuvable"):
        load_theme(str(tmp_path / "absent.toml"))


def test_invalid_toml_rejected(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text("[trace\nwidth =", encoding="utf-8")
    with pytest.raises(ThemeError, match="TOML invalide"):
        load_theme(str(path))
