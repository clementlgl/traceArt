"""Tests de `download_to` sur un `urlopen` simulé — jamais de réseau réel,
même localhost : un faux objet réponse suffit à exercer le flux et les
en-têtes."""

from __future__ import annotations

import hashlib

import pytest

from traceart.basemap.download import DownloadError, download_to


class _FakeResponse:
    def __init__(self, headers: dict[str, str], body: bytes) -> None:
        self.headers = headers
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self, n: int) -> bytes:
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk


def _patch_urlopen(monkeypatch, response: _FakeResponse) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: response
    )


def test_download_succeeds_on_a_binary_response(monkeypatch, tmp_path):
    body = b"contenu binaire quelconque"
    headers = {"Content-Type": "application/octet-stream", "Content-Length": str(len(body))}
    _patch_urlopen(monkeypatch, _FakeResponse(headers, body))
    target = tmp_path / "extrait.osm.pbf"
    digest = download_to("https://example.test/x.pbf", target, timeout=5.0)
    assert target.read_bytes() == body
    assert digest == hashlib.sha256(body).hexdigest()


def test_download_rejects_an_html_response(monkeypatch, tmp_path):
    """Un chemin de région Geofabrik erroné répond en `text/html` (page
    d'erreur ou de redirection) avec un statut 200 — sans ce garde-fou,
    la page HTML s'écrirait telle quelle dans le cache et l'échec ne
    surviendrait que bien plus tard, en plein import GDAL, avec un
    message bien moins clair."""
    _patch_urlopen(
        monkeypatch,
        _FakeResponse({"Content-Type": "text/html; charset=utf-8"}, b"<html>404</html>"),
    )
    target = tmp_path / "extrait.osm.pbf"
    with pytest.raises(DownloadError, match="HTML"):
        download_to("https://example.test/mauvaise-region.pbf", target, timeout=5.0)
    assert not target.exists()
    assert not target.with_suffix(target.suffix + ".part").exists()
