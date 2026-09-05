"""Invariants structurels — pas des tests de comportement.

Ce fichier verrouille les décisions de conception prises pendant la
préparation de l'interface web (étapes 0 à 2 du plan) : hiérarchie
d'exceptions, table de mapping config → `Options`, et direction des
dépendances entre paquets. Un changement qui les casse est une régression
architecturale, pas un simple détail d'implémentation — c'est
volontairement écrit ainsi pour que la dérive déjà observée une fois
(couche `relief` fantôme, `margin`/`cache_dir` absents de l'exemple de
config) ne puisse plus se reproduire sans qu'un test échoue.
"""

from __future__ import annotations

import ast
import inspect
import pkgutil
import tomllib
from dataclasses import fields
from importlib import import_module
from pathlib import Path

import pytest

import traceart
from traceart.config import SECTIONS
from traceart.errors import TraceArtError
from traceart.options import ADAPTER_ONLY, SPECS
from traceart.pipeline import Options

SRC = Path(traceart.__file__).resolve().parent
REPO_ROOT = SRC.parent.parent


# ---------------------------------------------------------- 1. table SPECS


def test_specs_cover_every_options_field():
    """Un champ ajouté à `Options` sans être déclaré dans `SPECS` (ou
    explicitement exempté) ne doit jamais rester invisible en silence."""
    covered = {s.field for s in SPECS if s.target == "options"} | ADAPTER_ONLY
    expected = {f.name for f in fields(Options)}
    assert covered == expected


def test_build_options_matches_the_dataclass_default():
    """Parité CLI/web : sans config ni override, `build_options` doit
    produire exactement les défauts du code."""
    from traceart.options import build_options

    assert build_options() == Options()


# ------------------------------------------------------- 2. config ↔ SPECS


def test_config_sections_match_specs_sections():
    assert set(SECTIONS) == {spec.section for spec in SPECS}


def test_example_config_documents_every_spec_key():
    """`traceart.toml.example` doit rester une documentation exhaustive
    des clés reconnues — `margin` et `cache_dir` y manquaient encore
    récemment alors que le CLI les reconnaissait déjà."""
    example_path = REPO_ROOT / "traceart.toml.example"
    example = tomllib.loads(example_path.read_text(encoding="utf-8"))
    missing = [
        f"[{spec.section}] {spec.key}"
        for spec in SPECS
        if example.get(spec.section, {}).get(spec.key) is None
    ]
    assert not missing, f"absentes de {example_path.name} : {missing}"


def test_ui_specs_are_a_labelled_subset():
    """`ui_specs()` ne doit exposer que des options visuelles, chacune
    avec un libellé prêt à afficher — c'est ce qui rend vérifiable la
    promesse « seulement l'essentiel visuel »."""
    from traceart.options import ui_specs

    exposed = ui_specs()
    assert exposed
    assert set(exposed) <= set(SPECS)
    assert all(spec.label for spec in exposed)


# ------------------------------------------------ 3. hiérarchie d'erreurs


def _project_exception_classes() -> list[type[BaseException]]:
    """Toute classe d'exception définie quelque part sous `traceart`."""
    found: list[type[BaseException]] = []
    for info in pkgutil.walk_packages(traceart.__path__, prefix="traceart."):
        try:
            module = import_module(info.name)
        except ImportError:
            # Extra non installé (cairosvg, pyogrio…) : hors sujet ici.
            continue
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                obj.__module__.startswith("traceart")
                and issubclass(obj, BaseException)
                and obj is not TraceArtError
            ):
                found.append(obj)
    # dédoublonne : une exception importée dans plusieurs modules ne doit
    # compter qu'une fois.
    return list(dict.fromkeys(found))


def test_every_project_exception_derives_from_traceart_error():
    offenders = [
        cls for cls in _project_exception_classes() if not issubclass(cls, TraceArtError)
    ]
    assert not offenders, f"n'héritent pas de TraceArtError : {offenders}"


# Second volet de la taxonomie fermée — « toute exception a un code HTTP
# dans la table du web » — reporté à l'étape 4 : `web/errors.py` n'existe
# pas encore. Quand il existera, un test symétrique devra vérifier que
# `_project_exception_classes()` est un sous-ensemble des clés de sa
# table exceptions → code HTTP.


@pytest.mark.parametrize(
    ("name", "expected_base"),
    [
        ("traceart.pipeline", "UserError"),
        ("traceart.core.gpx", "UserError"),
        ("traceart.render.theme", "UserError"),
        ("traceart.config", "UserError"),
        ("traceart.render.png", "UnavailableError"),
        ("traceart.basemap.store", "UnavailableError"),
        ("traceart.basemap.osm", "UnavailableError"),
        ("traceart.basemap.query", "UnavailableError"),
        ("traceart.basemap.download", "UnavailableError"),
    ],
)
def test_double_inheritance_preserves_builtin_except_clauses(name, expected_base):
    """La migration vers `TraceArtError` ne devait casser aucun
    `except ValueError` / `except RuntimeError` déjà écrit ailleurs."""
    from traceart.errors import UnavailableError, UserError

    module = import_module(name)
    exc_classes = [
        obj
        for _n, obj in inspect.getmembers(module, inspect.isclass)
        if obj.__module__ == name and issubclass(obj, BaseException)
    ]
    assert exc_classes, f"{name} ne définit aucune exception"
    base = {"UserError": UserError, "UnavailableError": UnavailableError}[expected_base]
    builtin = ValueError if expected_base == "UserError" else RuntimeError
    for cls in exc_classes:
        assert issubclass(cls, base)
        assert issubclass(cls, builtin)


# --------------------------------------------------- 4. direction des imports


def _module_level_imports(path: Path) -> set[str]:
    """Modules importés par des `import`/`from import` de premier niveau.

    Volontairement borné à `tree.body` (pas `ast.walk`) : un import
    paresseux dans le corps d'une fonction — `traceart serve` important
    `uvicorn`, par exemple — ne doit pas être vu par ce contrôle. C'est le
    mécanisme d'exemption documenté dans le plan, pas un cas particulier
    codé en dur.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            prefix = "." * node.level
            names.add(f"{prefix}{node.module}")
    return names


def _python_files_under(*parts: str) -> list[Path]:
    root = SRC.joinpath(*parts)
    return sorted(root.rglob("*.py")) if root.is_dir() else []


def _offending(
    imports: set[str],
    *,
    forbidden_exact: set[str],
    forbidden_prefixes: tuple[str, ...],
) -> set[str]:
    return {
        m
        for m in imports
        if m in forbidden_exact or any(m.startswith(p) for p in forbidden_prefixes)
    }


def test_core_never_imports_render_or_basemap():
    """`core/` est la couche la plus basse : elle ignore comment ses
    traces sont dessinées ou situées sur un fond de carte."""
    for path in _python_files_under("core"):
        offenders = _offending(
            _module_level_imports(path),
            forbidden_exact=set(),
            forbidden_prefixes=("traceart.render", "traceart.basemap"),
        )
        assert not offenders, f"{path.relative_to(SRC)} importe {offenders}"


def test_cli_never_imports_web_at_module_level():
    """Le CLI ne doit pas traîner FastAPI ; le futur `traceart serve`
    importera `uvicorn`/`traceart.web` en paresseux, dans le corps de la
    fonction — invisible à ce contrôle par construction."""
    for path in _python_files_under("cli"):
        offenders = _offending(
            _module_level_imports(path),
            forbidden_exact={"fastapi", "uvicorn"},
            forbidden_prefixes=("fastapi.", "traceart.web"),
        )
        assert not offenders, f"{path.relative_to(SRC)} importe {offenders}"


def test_web_never_imports_cli_click_or_rich():
    """Symétrique : le web ne doit pas dépendre de Click, de Rich, ni
    d'un quelconque module du CLI. Passe trivialement tant que `web/`
    n'existe pas — s'active de lui-même dès sa création."""
    for path in _python_files_under("web"):
        offenders = _offending(
            _module_level_imports(path),
            forbidden_exact={"click", "rich"},
            forbidden_prefixes=("click.", "rich.", "traceart.cli"),
        )
        assert not offenders, f"{path.relative_to(SRC)} importe {offenders}"
