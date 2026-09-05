from __future__ import annotations

import pytest
from click.testing import CliRunner

from traceart.cli.main import _collect_gpx, _slugify, app

runner = CliRunner()


def test_render_writes_svg(gpx_file, tmp_path):
    result = runner.invoke(app, ["render", str(gpx_file), "--out-dir", str(tmp_path / "o")])
    assert result.exit_code == 0, result.output
    assert list((tmp_path / "o").glob("*.svg"))


def test_explicit_output_path(gpx_file, tmp_path):
    target = tmp_path / "poster.svg"
    result = runner.invoke(app, ["render", str(gpx_file), "-o", str(target)])
    assert result.exit_code == 0, result.output
    assert target.is_file()
    assert target.read_text(encoding="utf-8").startswith("<?xml")


def test_separate_writes_one_file_per_gpx(gpx_file, tmp_path):
    second = tmp_path / "deux.gpx"
    second.write_text(gpx_file.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "o"
    result = runner.invoke(
        app, ["render", str(gpx_file), str(second), "--separate", "--out-dir", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert len(list(out.glob("*.svg"))) == 2


def test_separate_rejects_single_output(gpx_file, tmp_path):
    result = runner.invoke(
        app, ["render", str(gpx_file), "--separate", "-o", str(tmp_path / "x.svg")]
    )
    assert result.exit_code != 0


def test_directory_input_is_expanded(gpx_file, tmp_path):
    out = tmp_path / "o"
    result = runner.invoke(app, ["render", str(gpx_file.parent), "--out-dir", str(out)])
    assert result.exit_code == 0, result.output


def test_info_reports_measures(gpx_file):
    result = runner.invoke(app, ["info", str(gpx_file)])
    assert result.exit_code == 0, result.output
    assert "km" in result.output


def test_themes_lists_shipped_themes():
    result = runner.invoke(app, ["themes"])
    assert result.exit_code == 0
    for name in ("light", "dark", "mono", "blueprint"):
        assert name in result.output


def test_unknown_theme_fails_cleanly(gpx_file, tmp_path):
    result = runner.invoke(
        app, ["render", str(gpx_file), "--theme", "neon", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code != 0


def test_missing_path_reported(tmp_path):
    result = runner.invoke(app, ["render", str(tmp_path / "absent.gpx")])
    assert result.exit_code != 0


def test_config_file_supplies_defaults(gpx_file, tmp_path, monkeypatch):
    config = tmp_path / "traceart.toml"
    config.write_text('[defaults]\ntheme = "blueprint"\n', encoding="utf-8")
    out = tmp_path / "o"
    result = runner.invoke(
        app,
        ["render", str(gpx_file), "--config", str(config), "--out-dir", str(out)],
    )
    assert result.exit_code == 0, result.output
    svg = next(out.glob("*.svg")).read_text(encoding="utf-8")
    assert "#0f3057" in svg


def test_cli_option_overrides_config(gpx_file, tmp_path):
    config = tmp_path / "traceart.toml"
    config.write_text('[defaults]\ntheme = "blueprint"\n', encoding="utf-8")
    out = tmp_path / "o"
    result = runner.invoke(
        app,
        [
            "render", str(gpx_file), "--config", str(config),
            "--theme", "mono", "--out-dir", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "#f4f1ea" in next(out.glob("*.svg")).read_text(encoding="utf-8")


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "traceart" in result.output


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PARTIE_1", "partie-1"),
        ("Cévennes / Auvergne", "cévennes-auvergne"),
        ("  ", "traceart"),
    ],
)
def test_slugify(raw, expected):
    assert _slugify(raw) == expected


def test_collect_gpx_is_deduplicated_and_sorted(gpx_file):
    files = _collect_gpx([gpx_file, gpx_file.parent, gpx_file])
    assert files == [gpx_file.resolve()]


def test_collect_gpx_rejects_empty_directory(tmp_path):
    (tmp_path / "vide").mkdir()
    with pytest.raises(Exception, match="aucun fichier"):
        _collect_gpx([tmp_path / "vide"])


def test_data_layers_lists_layers():
    result = runner.invoke(app, ["data", "layers"])
    assert result.exit_code == 0, result.output
    for layer in ("water", "coastline", "rivers", "roads", "boundaries", "labels"):
        assert layer in result.output


def test_data_status_on_empty_cache(tmp_path):
    result = runner.invoke(app, ["data", "status", "--cache-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "absent" in result.output


def test_layers_option_parsing():
    from traceart.cli.main import _parse_layers

    # L'ordre de cette liste est indifférent : le SVG suit LAYER_ORDER.
    assert set(_parse_layers(None)) == {
        "water", "coastline", "rivers", "borders", "boundaries",
    }
    assert _parse_layers("water, rivers,water") == ("water", "rivers")
    assert _parse_layers(["roads", "labels"]) == ("roads", "labels")
    assert _parse_layers("none") == ()
    with pytest.raises(Exception, match="inconnue"):
        _parse_layers("montagnes")


def test_no_basemap_renders_without_cache(gpx_file, tmp_path):
    result = runner.invoke(
        app,
        ["render", str(gpx_file), "--layers", "none", "--out-dir", str(tmp_path / "o")],
    )
    assert result.exit_code == 0, result.output


def test_basemap_on_without_cache_fails(gpx_file, tmp_path):
    result = runner.invoke(
        app,
        [
            "render", str(gpx_file), "--basemap", "on",
            "--cache-dir", str(tmp_path / "vide"), "--out-dir", str(tmp_path / "o"),
        ],
    )
    assert result.exit_code != 0


def test_bbox_option_parsing():
    from traceart.cli.main import _resolve_bbox

    assert _resolve_bbox("3,44,4,45", None) == (3.0, 44.0, 4.0, 45.0)
    assert _resolve_bbox(" 3.5 , 44.1 , 4.2 , 45.0 ", None) == (3.5, 44.1, 4.2, 45.0)
    assert _resolve_bbox(None, None) is None


@pytest.mark.parametrize(
    "raw", ["3,44,4", "3,44,4,45,46", "a,b,c,d", "4,44,3,45", "3,45,4,44"]
)
def test_invalid_bbox_rejected(raw):
    from traceart.cli.main import _resolve_bbox

    with pytest.raises(Exception, match="bbox"):
        _resolve_bbox(raw, None)


def test_bbox_derived_from_gpx(gpx_file):
    from traceart.cli.main import _resolve_bbox

    box = _resolve_bbox(None, [gpx_file])
    # Le GPX de test va de (3.0, 44.0) à (3.08, 44.06), plus 25 % de marge.
    assert box[0] < 3.0 and box[2] > 3.08
    assert box[1] < 44.0 and box[3] > 44.06


def test_osm_import_with_bbox_via_cli(tmp_path):
    osm = tmp_path / "mini.osm"
    osm.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6">\n'
        '<node id="1" lat="44.0" lon="3.0"/><node id="2" lat="44.2" lon="3.4"/>\n'
        '<way id="3"><nd ref="1"/><nd ref="2"/><tag k="highway" v="primary"/></way>\n'
        "</osm>\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "data", "osm", "import", str(osm),
            "--name", "mini", "--bbox", "2.9,43.9,3.5,44.3",
            "--cache-dir", str(tmp_path / "cache"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "découpe" in result.output

    listed = runner.invoke(
        app, ["data", "osm", "list", "--cache-dir", str(tmp_path / "cache")]
    )
    assert "mini" in listed.output

    removed = runner.invoke(
        app, ["data", "osm", "remove", "mini", "--cache-dir", str(tmp_path / "cache")]
    )
    assert removed.exit_code == 0, removed.output


@pytest.mark.parametrize(
    ("flag", "section", "key"),
    [
        ("--bleed", "defaults", "bleed"),
        ("--osm", "defaults", "osm"),
        ("--annotations", "annotations", "enabled"),
        ("--profile", "annotations", "profile"),
        ("--stats", "annotations", "stats"),
        ("--clean", "clean", "enabled"),
    ],
)
def test_positive_flag_overrides_a_false_in_config(flag, section, key):
    """Un drapeau tapé explicitement doit primer sur le fichier de config.

    Le piège : déclarer l'option avec `default=True` rend « non passée »
    et « passée en positif » indiscernables, et le fichier gagnait.
    """
    from traceart.cli.config import resolve

    cfg = {section: {key: False}}
    assert resolve(cfg, section, key, True, True) is True
    assert resolve(cfg, section, key, None, True) is False
    assert resolve(cfg, section, key, False, True) is False


def test_bleed_flag_declared_tristate():
    """Les six drapeaux réglables par config doivent valoir None par défaut."""
    from traceart.cli.main import render

    tristate = {"bleed", "osm", "annotations", "profile", "stats", "clean"}
    for param in render.params:
        if param.name in tristate:
            assert param.default is None, param.name
