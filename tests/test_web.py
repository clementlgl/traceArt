"""Tests de l'interface web, sans réseau ni navigateur.

`fastapi.testclient.TestClient` exécute l'application ASGI en processus :
aucun port, aucun serveur, aucun navigateur. Les fixtures `web_app` /
`client` / `empty_store_app` / `empty_client` (tests/conftest.py)
réutilisent le cache Natural Earth synthétique `store` et un
`InlineJobRunner`, pour ne jamais dépendre d'un thread réel ni d'un
`time.sleep()`.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import make_gpx, trkpt

_SIMPLE_GPX = make_gpx(
    "<trk><trkseg>"
    + trkpt(3.0, 44.0, 100.0)
    + trkpt(3.05, 44.05, 200.0)
    + trkpt(3.1, 44.1, 150.0)
    + "</trkseg></trk>"
)


def _upload(client, filename: str = "Trace Alpine.gpx", body: str = _SIMPLE_GPX):
    return client.post("/upload", files={"files": (filename, body.encode())})


def _artifact_token(html: str) -> str:
    match = re.search(r"/artifact/([a-f0-9]+)\.svg", html)
    assert match, f"aucun lien d'artefact dans le fragment : {html[:200]!r}"
    return match.group(1)


# --------------------------------------------------------------- pages nues


def test_index_page_loads(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "TraceArt" in r.text


def test_health_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_static_assets_are_served(client):
    css = client.get("/static/app.css")
    assert css.status_code == 200
    htmx = client.get("/static/htmx.min.js")
    assert htmx.status_code == 200
    assert len(htmx.content) > 1000


def test_index_lists_every_layer_from_the_registry(client):
    """Le registre des couches (`traceart.layers`) doit être la seule
    source des cases à cocher — pas une copie recopiée dans le gabarit.
    Le formulaire d'options n'apparaît qu'après un envoi (flux en deux
    temps : upload, puis réglages)."""
    from markupsafe import escape

    from traceart.layers import LAYERS

    html = _upload(client).text
    for spec in LAYERS:
        assert f'value="{spec.name}"' in html
        # Jinja2 échappe l'apostrophe de « plans d'eau » en HTML.
        assert str(escape(spec.label)) in html


# -------------------------------------------------------------------- upload


def test_upload_confirms_the_filename(client):
    r = _upload(client)
    assert r.status_code == 200
    assert "Trace Alpine.gpx" in r.text


def test_upload_sets_a_session_cookie(client):
    r = _upload(client)
    assert "traceart_session" in r.cookies


def test_upload_rejects_non_gpx_extension(client):
    r = client.post("/upload", files={"files": ("photo.jpg", b"pas un gpx")})
    assert r.status_code == 422


def test_upload_replaces_the_previous_batch(client):
    _upload(client, "premier.gpx")
    r = _upload(client, "second.gpx")
    assert "second.gpx" in r.text
    assert "premier.gpx" not in r.text


def test_upload_enforces_the_size_limit(web_app):
    from fastapi.testclient import TestClient

    web_app.state.settings = type(web_app.state.settings)(
        cache_dir=web_app.state.settings.cache_dir,
        work_dir=web_app.state.settings.work_dir,
        max_upload_bytes=10,
    )
    client = TestClient(web_app)
    r = _upload(client)
    assert r.status_code == 422


# -------------------------------------------------------------------- render


def test_render_without_upload_is_rejected(client):
    r = client.post("/render", data={"theme": "light"})
    assert r.status_code == 422


def test_render_produces_a_preview_with_artifact_link(client):
    _upload(client)
    r = client.post(
        "/render",
        data={"theme": "light", "basemap": "off", "aspect": "trace", "stats": "true"},
    )
    assert r.status_code == 200
    _artifact_token(r.text)


def test_index_offers_a_trace_color_picker(client):
    html = _upload(client).text
    assert 'name="trace_color"' in html
    assert 'type="color"' in html


def test_render_with_custom_trace_color_reaches_the_artifact(client):
    _upload(client)
    r = client.post(
        "/render",
        data={
            "theme": "dark",
            "trace_color": "#00ff00",
            "basemap": "off",
            "aspect": "trace",
        },
    )
    assert r.status_code == 200
    token = _artifact_token(r.text)
    svg = client.get(f"/artifact/{token}.svg").text
    assert 'stroke="#00ff00"' in svg


def test_render_without_trace_color_uses_the_theme_default(client):
    _upload(client)
    r = client.post(
        "/render",
        data={"theme": "dark", "basemap": "off", "aspect": "trace"},
    )
    assert r.status_code == 200
    token = _artifact_token(r.text)
    svg = client.get(f"/artifact/{token}.svg").text
    from traceart.render.theme import load_theme

    assert f'stroke="{load_theme("dark").trace_color(0)}"' in svg


def test_render_honours_the_filename_derived_title(client):
    """Le test le plus précieux du lot : `Track.source.stem` alimente
    `default_title()`, un nom de fichier temporaire donnerait un titre
    absurde — ce test garantit que le nom d'origine survit jusqu'au
    rendu."""
    _upload(client, "Trace Alpine.gpx")
    r = client.post(
        "/render",
        data={"theme": "light", "basemap": "off", "aspect": "trace", "annotations": "true"},
    )
    assert r.status_code == 200
    assert "Trace Alpine" in r.text


def test_render_never_lets_the_ui_request_basemap_on(client):
    """basemap="on" échouerait net sur un cache incomplet : le serveur
    doit retomber sur "auto" plutôt que de laisser passer la valeur."""
    _upload(client)
    r = client.post(
        "/render",
        data={"theme": "light", "basemap": "on", "aspect": "trace"},
    )
    assert r.status_code == 200


def test_render_reports_basemap_source_when_cache_has_data(client):
    # Coordonnées dans la zone couverte par le fixture `store` (Cévennes,
    # ~3,2-3,8 E / 44,0-44,3 N) : la trace précédente (3,0-3,1 E) tombe
    # hors de l'emprise synthétique et ne dessine donc aucun fond.
    body = "<trk><trkseg>" + "".join(
        trkpt(lon, lat) for lon, lat in ((3.2, 44.0), (3.5, 44.15), (3.8, 44.1))
    ) + "</trkseg></trk>"
    _upload(client, "cevennes.gpx", make_gpx(body))
    r = client.post(
        "/render",
        data={
            "theme": "light",
            "basemap": "auto",
            "aspect": "trace",
            "layers": ["water", "coastline"],
        },
    )
    assert r.status_code == 200
    assert "Natural Earth" in r.text


def test_render_reports_missing_data_on_empty_cache(empty_client):
    """Cache vide : le rendu réussit quand même (mode auto), avec un
    bandeau qui pointe vers /data plutôt qu'une erreur brute."""
    _upload(empty_client)
    r = empty_client.post(
        "/render",
        data={
            "theme": "light",
            "basemap": "auto",
            "aspect": "trace",
            "layers": ["water"],
        },
    )
    assert r.status_code == 200
    assert "/data" in r.text


def test_unknown_layer_is_rejected(client):
    _upload(client)
    r = client.post(
        "/render",
        data={"theme": "light", "aspect": "trace", "layers": ["montagnes"]},
    )
    assert r.status_code == 422


def test_unknown_theme_is_rejected(client):
    _upload(client)
    r = client.post("/render", data={"theme": "neon", "aspect": "trace"})
    assert r.status_code == 422


# ----------------------------------------------------------------- artefacts


def test_artifact_svg_is_served(client):
    _upload(client)
    html = client.post(
        "/render", data={"theme": "light", "basemap": "off", "aspect": "trace"}
    ).text
    token = _artifact_token(html)
    r = client.get(f"/artifact/{token}.svg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.text.startswith("<?xml")


def test_artifact_png_is_rasterized_on_demand(client):
    _upload(client)
    html = client.post(
        "/render", data={"theme": "light", "basemap": "off", "aspect": "trace"}
    ).text
    token = _artifact_token(html)
    r = client.get(f"/artifact/{token}.png", params={"width": 500})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")


def test_unknown_artifact_token_is_rejected(client):
    r = client.get("/artifact/0000000000000000.svg")
    assert r.status_code == 422


def test_artifact_token_rejects_path_traversal(client):
    r = client.get("/artifact/..%2f..%2fetc%2fpasswd.svg")
    assert r.status_code in {404, 422}


# --------------------------------------------------------------- fond / data


def test_data_page_lists_layer_status(client):
    r = client.get("/data")
    assert r.status_code == 200
    assert "Natural Earth" in r.text


def test_data_fetch_disabled_returns_403(web_app):
    from fastapi.testclient import TestClient

    settings = web_app.state.settings
    web_app.state.settings = type(settings)(
        cache_dir=settings.cache_dir, work_dir=settings.work_dir, allow_fetch=False
    )
    client = TestClient(web_app)
    r = client.post("/data/fetch/natural-earth", data={"scale": "10m"})
    assert r.status_code == 422


def test_unknown_scale_is_rejected(client):
    r = client.post("/data/fetch/natural-earth", data={"scale": "9999m"})
    assert r.status_code == 422


def test_unknown_job_id_returns_error(client):
    r = client.get("/data/jobs/does-not-exist")
    assert r.status_code == 422


@pytest.mark.parametrize("flag_name", ["annotations", "profile", "stats"])
def test_boolean_form_flags_default_to_false_when_absent(client, flag_name):
    """Les cases à cocher HTML n'envoient rien quand elles sont
    décochées : l'absence du champ doit valoir False, pas planter."""
    _upload(client)
    r = client.post("/render", data={"theme": "light", "aspect": "trace"})
    assert r.status_code == 200


# --------------------------------------------------- recherche de villes


def test_no_network_fixture_actually_blocks_the_real_search_call(client):
    """Preuve, pas supposition : sans monkeypatch explicite dans le test,
    une recherche doit échouer sur l'assertion du filet `no_network`
    (conftest.py), pas partir sur le vrai réseau. `routes.py` importe
    `search_places` par valeur — patcher seulement le module d'origine
    laisserait ce filet inefficace, d'où le test."""
    with pytest.raises(AssertionError, match="réseau"):
        client.get("/places/search", params={"q": "lyon"})


def _fake_results(*results):
    """Résultats canés, jamais de vrai réseau — voir le filet `no_network`
    (autouse) qui bloque `search_places` par défaut ; ces tests le
    remplacent délibérément par une fonction déterministe."""

    def fake(query, limit=8):
        return list(results)

    return fake


def test_places_search_renders_results(client, monkeypatch):
    from traceart.web.geocode import GeocodeResult

    monkeypatch.setattr(
        "traceart.web.routes.search_places",
        _fake_results(GeocodeResult("Lyon", 45.75, 4.83)),
    )
    r = client.get("/places/search", params={"q": "lyon"})
    assert r.status_code == 200
    assert "Lyon" in r.text


def test_places_search_empty_query_shows_no_results(client):
    r = client.get("/places/search", params={"q": "   "})
    assert r.status_code == 200
    assert "Aucun résultat" not in r.text  # pas de recherche lancée du tout


def test_places_search_no_match(client, monkeypatch):
    monkeypatch.setattr("traceart.web.routes.search_places", _fake_results())
    r = client.get("/places/search", params={"q": "xyzintrouvable"})
    assert r.status_code == 200
    assert "Aucun résultat" in r.text


def test_places_search_service_unavailable_is_shown_inline(client, monkeypatch):
    """Un service de géocodage en panne ne doit pas casser la page —
    juste un message dans le fragment, pas une erreur HTTP générique
    pour une simple recherche interactive."""
    from traceart.web.geocode import GeocodeError

    def failing(query, limit=8):
        raise GeocodeError("Nominatim indisponible (test)")

    monkeypatch.setattr("traceart.web.routes.search_places", failing)
    r = client.get("/places/search", params={"q": "lyon"})
    assert r.status_code == 200
    assert "indisponible" in r.text


def test_places_add_appears_in_the_list(client):
    _upload(client)
    r = client.post("/places/add", data={"name": "Lyon", "lat": 45.75, "lon": 4.83})
    assert r.status_code == 200
    assert "Lyon" in r.text
    assert "Supprimer" in r.text


def test_places_add_deduplicates_by_name(client):
    _upload(client)
    client.post("/places/add", data={"name": "Lyon", "lat": 45.75, "lon": 4.83})
    r = client.post("/places/add", data={"name": "Lyon", "lat": 45.76, "lon": 4.84})
    # "Lyon" apparaît deux fois par ville dans le fragment (le texte
    # affiché + la valeur cachée du formulaire de suppression) : compter
    # les boutons "Supprimer" donne le vrai nombre d'entrées.
    assert r.text.count("Supprimer") == 1


def test_places_remove_disappears_from_the_list(client):
    _upload(client)
    client.post("/places/add", data={"name": "Lyon", "lat": 45.75, "lon": 4.83})
    r = client.post("/places/remove", data={"name": "Lyon"})
    assert r.status_code == 200
    assert "Lyon" not in r.text
    assert "Aucune ville" in r.text


def test_places_add_without_session_is_rejected(client):
    r = client.post("/places/add", data={"name": "Lyon", "lat": 45.75, "lon": 4.83})
    assert r.status_code == 422


def test_new_upload_clears_the_previous_place_list(client):
    """Une nouvelle trace change le contexte géographique : repartir à
    zéro sur les villes ajoutées est délibéré, pas un oubli."""
    _upload(client, "premier.gpx")
    client.post("/places/add", data={"name": "Lyon", "lat": 45.75, "lon": 4.83})
    r = _upload(client, "second.gpx")
    assert "Lyon" not in r.text


def test_added_city_is_drawn_regardless_of_zoom_tier(client):
    """Le test le plus important du lot : une ville ajoutée manuellement
    doit se retrouver dans le SVG produit, quel que soit le palier de
    zoom qui l'aurait normalement filtrée."""
    _upload(client)
    # Coordonnée à l'intérieur du cadre de _SIMPLE_GPX (3.0-3.1 E, 44.0-44.1 N).
    client.post("/places/add", data={"name": "MonHameau", "lat": 44.05, "lon": 3.05})
    r = client.post(
        "/render", data={"theme": "light", "basemap": "off", "aspect": "trace"}
    )
    assert r.status_code == 200
    token = _artifact_token(r.text)
    svg = client.get(f"/artifact/{token}.svg").text
    assert "MonHameau" in svg


def test_added_city_outside_the_frame_is_reported_not_drawn(client):
    _upload(client)
    client.post("/places/add", data={"name": "TropLoin", "lat": 10.0, "lon": 100.0})
    r = client.post(
        "/render", data={"theme": "light", "basemap": "off", "aspect": "trace"}
    )
    assert r.status_code == 200
    assert "TropLoin" in r.text  # signalé dans la note...
    token = _artifact_token(r.text)
    svg = client.get(f"/artifact/{token}.svg").text
    assert "TropLoin" not in svg  # ...mais pas dessiné
