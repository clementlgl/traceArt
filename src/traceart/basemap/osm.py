"""Tier B : fond de carte grand échelle depuis des extraits OpenStreetMap.

Natural Earth s'arrête à la moyenne échelle : pas de retenue du Massif
Central, pas de route départementale, 7 342 villes dans le monde. Pour un
poster de rando, il faut OSM.

Stratégie : un extrait régional Geofabrik (`.osm.pbf`) est importé **une
fois** en couches FlatGeobuf filtrées et indexées. L'import est l'étape
coûteuse ; les rendus qui suivent ne lisent qu'une requête par bounding
box, aussi vite que sur Natural Earth.

La lecture du `.pbf` passe par le pilote OSM de GDAL, avec un
`osmconf.ini` maison — le défaut n'expose ni `boundary` ni `admin_level`
en colonnes.

Point clé de conception : chaque entité reçoit à l'import un **rang de
notoriété** synthétique dans une colonne `rank`, calqué sur le
`scalerank` de Natural Earth. Tout l'aval — filtrage par palier de zoom,
découpe, mise en page — fonctionne alors sans savoir d'où viennent les
géométries.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from traceart.basemap.download import DownloadError, download_to, fetch_lock
from traceart.basemap.store import multi_geometry_type
from traceart.errors import UnavailableError

GEOFABRIK_BASE = "https://download.geofabrik.de"
INDEX_NAME = "index.json"
CONFIG_FILE = Path(__file__).resolve().parent / "osmconf.ini"
DOWNLOAD_TIMEOUT_S = 600

# --------------------------------------------------------------------- rangs
#
# Les seuils des paliers de zoom (`tiers.py`) valent 3 / 5 / 8 / 12. Les
# rangs ci-dessous sont calibrés dessus : au palier « région » (8) on
# obtient le réseau structurant, au palier « local » (12) le détail.

ROAD_RANKS: dict[str, int] = {
    "motorway": 1,
    "trunk": 2,
    "motorway_link": 3,
    "trunk_link": 3,
    "primary": 4,
    "primary_link": 5,
    "secondary": 7,
    "secondary_link": 8,
    "tertiary": 10,
    "tertiary_link": 11,
}

WATERWAY_RANKS: dict[str, int] = {
    "river": 3,
    "canal": 6,
    "stream": 10,
}

# `admin_level` OSM → rang. En France : 2 pays, 4 région, 6 département,
# 8 commune.
ADMIN_RANKS: dict[str, int] = {
    "2": 1,
    "3": 3,
    "4": 4,
    "5": 6,
    "6": 8,
    "7": 10,
    "8": 11,
}

PLACE_RANKS: dict[str, int] = {"city": 2, "town": 6, "village": 10}

# Population de repli quand le tag `population` manque — fréquent en
# OSM. Sans elle, le filtrage par population supprimerait toute commune
# non renseignée.
PLACE_POPULATION: dict[str, int] = {"city": 200_000, "town": 20_000, "village": 1_500}

WATER_NATURAL = ("water",)
WATER_LANDUSE = ("reservoir", "basin")
WATER_WATERWAY = ("riverbank",)


class OsmError(UnavailableError):
    """Extrait OSM illisible, ou région introuvable dans le cache."""


@dataclass(frozen=True, slots=True)
class OsmLayer:
    """Couche dérivée d'un extrait, alignée sur la nomenclature des thèmes."""

    name: str
    source_layer: str  # couche GDAL : points | lines | multipolygons
    closed: bool
    rank_field: str | None = "rank"
    label_field: str | None = None
    population_field: str | None = None
    # Rang plancher : `boundaries` écarte `admin_level=2` (rang 1), qui
    # relève de la couche `borders` — laquelle reste sur Natural Earth.
    rank_min: int = 0


OSM_LAYERS: tuple[OsmLayer, ...] = (
    # Pas de rang sur l'eau : c'est une surface, sa taille à l'écran
    # décide de sa visibilité — même règle que pour les lacs Natural Earth.
    OsmLayer("water", "multipolygons", closed=True, rank_field=None),
    # Pas de couche `coastline` : le polygone océan reste Natural Earth
    # (OSM ne le fournit pas, il faudrait le jeu water-polygons à part).
    # Dessiner une côte OSM détaillée par-dessus un océan Natural Earth
    # plus grossier ferait apparaître le décalage sur tout le littoral.
    OsmLayer("rivers", "lines", closed=False),
    OsmLayer("roads", "lines", closed=False),
    # Les limites internes viennent des relations, donc de
    # `multipolygons`. Le thème leur donne `fill = "none"` : elles se
    # dessinent en contour.
    #
    # `rank_min=3` exclut `admin_level=2` : les frontières nationales
    # restent sur Natural Earth. Une relation de pays est tronquée au
    # bord d'un extrait régional, et son contour dessinerait un artefact
    # rectiligne le long de la coupe — très visible sur un poster.
    OsmLayer("boundaries", "multipolygons", closed=True, rank_min=3),
    OsmLayer(
        "labels",
        "points",
        closed=False,
        label_field="name",
        population_field="population",
    ),
)

def layer_by_name(name: str) -> OsmLayer | None:
    return next((layer for layer in OSM_LAYERS if layer.name == name), None)


@dataclass(frozen=True, slots=True)
class OsmRegion:
    """Un extrait importé, avec son emprise et ce qu'il contient."""

    slug: str
    source: str
    sha256: str
    bounds: tuple[float, float, float, float]
    counts: dict[str, int]
    imported: str

    def covers(self, bbox: tuple[float, float, float, float]) -> bool:
        """Vrai si l'emprise demandée est entièrement dans la région.

        On exige l'inclusion totale : une couverture partielle
        dessinerait la moitié de la carte en OSM détaillé et l'autre en
        Natural Earth grossier, ce qui saute aux yeux.
        """
        return (
            self.bounds[0] <= bbox[0]
            and self.bounds[1] <= bbox[1]
            and self.bounds[2] >= bbox[2]
            and self.bounds[3] >= bbox[3]
        )

    @property
    def area(self) -> float:
        return (self.bounds[2] - self.bounds[0]) * (self.bounds[3] - self.bounds[1])

    @property
    def features(self) -> int:
        return sum(self.counts.values())

    def to_json(self) -> dict[str, object]:
        return {
            "source": self.source,
            "sha256": self.sha256,
            "bounds": list(self.bounds),
            "counts": dict(sorted(self.counts.items())),
            "imported": self.imported,
        }


def slugify_region(value: str) -> str:
    """`europe/france/languedoc-roussillon` → `europe-france-languedoc-roussillon`."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned or "region"


def geofabrik_url(region_path: str) -> str:
    """Chemin Geofabrik → URL du `.osm.pbf`."""
    path = region_path.strip().strip("/")
    if path.endswith(".osm.pbf"):
        return f"{GEOFABRIK_BASE}/{path}"
    return f"{GEOFABRIK_BASE}/{path}-latest.osm.pbf"


def _parse_population(values: np.ndarray, places: np.ndarray) -> np.ndarray:
    """Tag `population` → entier, avec repli sur le type de lieu.

    Le tag est du texte libre : « 12 345 », « ~5000 », « 3.200 » se
    rencontrent tous. Illisible ou absent, on retombe sur une valeur
    typique du type de lieu.
    """
    out = np.zeros(len(values), dtype=np.float64)
    for index, raw in enumerate(values):
        digits = re.sub(r"[^0-9]", "", str(raw)) if raw is not None else ""
        if digits:
            out[index] = float(digits)
        else:
            out[index] = float(PLACE_POPULATION.get(str(places[index]), 0))
    return out


def _rank_from(values: np.ndarray, table: dict[str, int], default: int = 99) -> np.ndarray:
    return np.fromiter(
        (table.get(str(v), default) for v in values), dtype=np.int32, count=len(values)
    )


def _column(meta: dict, field_data: list[np.ndarray], name: str) -> np.ndarray:
    """Colonne par nom, insensible à la casse. Vide si absente."""
    fields = [str(f).lower() for f in meta["fields"]]
    target = name.lower()
    if target not in fields:
        return np.array([None] * len(field_data[0]) if field_data else [], dtype=object)
    return np.asarray(field_data[fields.index(target)], dtype=object)


def _sql_in(values) -> str:
    inner = ", ".join(f"'{v}'" for v in sorted(values))
    return f"({inner})"


def lines_where() -> str:
    """Filtre SQL de la couche `lines`.

    Généré depuis les tables de rangs, pour que le filtre et les rangs ne
    puissent pas diverger : ce qui n'a pas de rang dessinable n'est
    jamais lu. Écarter `residential` et `unclassified` retire à lui seul
    la majorité des tronçons d'un extrait.
    """
    return (
        f"highway IN {_sql_in(ROAD_RANKS)}"
        f" OR waterway IN {_sql_in(WATERWAY_RANKS)}"
    )


def multipolygons_where() -> str:
    return (
        f"natural IN {_sql_in(WATER_NATURAL)}"
        f" OR landuse IN {_sql_in(WATER_LANDUSE)}"
        f" OR waterway IN {_sql_in(WATER_WATERWAY)}"
        f" OR (boundary = 'administrative' AND admin_level IN {_sql_in(ADMIN_RANKS)})"
    )


def points_where() -> str:
    return f"place IN {_sql_in(PLACE_RANKS)}"


class OsmStore:
    """Extraits OSM importés, sous `<cache>/osm/`.

    Un dossier par région, une couche FlatGeobuf par thème, et un
    `index.json` qui mémorise l'emprise et le sha256 de la source.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root) / "osm"

    # ------------------------------------------------------------------ index

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_NAME

    def region_dir(self, slug: str) -> Path:
        return self.root / slug

    def layer_path(self, slug: str, layer: str) -> Path:
        return self.region_dir(slug) / f"{layer}.fgb"

    def read_index(self) -> dict[str, OsmRegion]:
        if not self.index_path.is_file():
            return {}
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OsmError(f"{self.index_path} : index illisible ({exc})") from exc
        out: dict[str, OsmRegion] = {}
        for slug, item in raw.get("regions", {}).items():
            bounds = item.get("bounds") or [0.0, 0.0, 0.0, 0.0]
            out[slug] = OsmRegion(
                slug=slug,
                source=str(item.get("source", "")),
                sha256=str(item.get("sha256", "")),
                bounds=(
                    float(bounds[0]),
                    float(bounds[1]),
                    float(bounds[2]),
                    float(bounds[3]),
                ),
                counts={k: int(v) for k, v in (item.get("counts") or {}).items()},
                imported=str(item.get("imported", "")),
            )
        return out

    def write_index(self, regions: dict[str, OsmRegion]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "regions": {slug: regions[slug].to_json() for slug in sorted(regions)},
        }
        self.index_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def regions(self) -> list[OsmRegion]:
        return [self.read_index()[slug] for slug in sorted(self.read_index())]

    def covering(self, bbox: tuple[float, float, float, float]) -> OsmRegion | None:
        """Région importée la plus petite qui contienne l'emprise.

        La plus petite gagne : à couverture égale, l'extrait le plus
        serré est le plus détaillé et le plus rapide à interroger.
        """
        candidates = [r for r in self.regions() if r.covers(bbox)]
        if not candidates:
            return None
        return min(candidates, key=lambda r: (r.area, r.slug))

    def has_layer(self, slug: str, layer: str) -> bool:
        return self.layer_path(slug, layer).is_file()

    def remove(self, slug: str) -> bool:
        regions = self.read_index()
        if slug not in regions and not self.region_dir(slug).exists():
            return False
        shutil.rmtree(self.region_dir(slug), ignore_errors=True)
        regions.pop(slug, None)
        self.write_index(regions)
        return True

    def total_size(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    # ---------------------------------------------------------- téléchargement

    @staticmethod
    def bbox_around(
        bounds: tuple[float, float, float, float], pad_ratio: float = 0.25
    ) -> tuple[float, float, float, float]:
        """Emprise élargie autour d'une bounding box de trace.

        La marge couvre ce que le cadre de sortie découvre au-delà de la
        trace : marges de page, bandeau, ratio forcé, fond à fond perdu.

        Elle est calculée sur le **grand** axe et appliquée aux deux.
        Padder chaque axe de sa propre étendue sous-pad le petit axe, or
        c'est celui-là que le cadre étire : une trace de 2,9° sur 0,6°
        gagne 0,2° en latitude, soit 35 % de son étendue verticale.
        """
        span = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
        pad = max(span * pad_ratio, 0.05)
        return (
            max(bounds[0] - pad, -180.0),
            max(bounds[1] - pad, -90.0),
            min(bounds[2] + pad, 180.0),
            min(bounds[3] + pad, 90.0),
        )

    def download(self, region_path: str, *, on_progress=None) -> tuple[Path, str]:
        """Télécharge un extrait Geofabrik. Renvoie (chemin, sha256)."""
        url = geofabrik_url(region_path)
        target = self.root / f"{slugify_region(region_path)}.osm.pbf"
        try:
            digest = download_to(
                url, target, timeout=DOWNLOAD_TIMEOUT_S, on_progress=on_progress
            )
        except DownloadError as exc:
            raise OsmError(str(exc)) from exc
        return target, digest

    # ------------------------------------------------------------------ import

    @staticmethod
    def _write_layer(
        path: Path,
        wkb: np.ndarray,
        fields: dict[str, np.ndarray],
        geometry_type: str,
    ) -> int:
        """Écrit une couche FlatGeobuf. Renvoie le nombre d'entités."""
        from pyogrio.raw import write

        # L'index spatial de FlatGeobuf refuse les géométries NULL, et un
        # extrait OSM en produit dès qu'une relation est incomplète —
        # cas normal au bord d'un découpage régional.
        keep = np.fromiter(
            (item is not None and len(item) > 0 for item in wkb),
            dtype=bool,
            count=len(wkb),
        )
        wkb = wkb[keep]
        if len(wkb) == 0:
            return 0
        fields = {name: values[keep] for name, values in fields.items()}

        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent) as tmpdir:
            staging = Path(tmpdir) / path.name
            write(
                str(staging),
                geometry=wkb,
                field_data=list(fields.values()),
                fields=np.array(list(fields), dtype=object),
                geometry_type=multi_geometry_type(geometry_type),
                crs="EPSG:4326",
                driver="FlatGeobuf",
                promote_to_multi=True,
            )
            shutil.move(str(staging), path)
        return len(wkb)

    def _read_source(
        self,
        pbf: Path,
        source_layer: str,
        columns: list[str],
        where: str,
        bbox: tuple[float, float, float, float] | None = None,
    ):
        from pyogrio.raw import read

        try:
            return read(
                str(pbf),
                layer=source_layer,
                columns=columns,
                where=where,
                bbox=bbox,
                force_2d=True,
                read_geometry=True,
                CONFIG_FILE=str(CONFIG_FILE),
            )
        except Exception as exc:  # pyogrio remonte des erreurs GDAL variées
            raise OsmError(f"{pbf.name} : lecture de la couche {source_layer} ({exc})") from exc

    def import_extract(
        self,
        pbf: str | Path,
        *,
        slug: str | None = None,
        sha256: str = "",
        bbox: tuple[float, float, float, float] | None = None,
        on_progress=None,
    ) -> OsmRegion:
        """Importe un `.osm.pbf` (ou `.osm`) en couches FlatGeobuf filtrées.

        Trois lectures seulement — `lines`, `multipolygons`, `points` —
        d'où l'on dérive cinq thèmes. Le pilote OSM reconstruit son cache
        de nœuds à chaque ouverture du fichier : lire une fois par thème
        multiplierait le coût de l'import.

        `bbox` restreint l'import à une emprise. Indispensable au-delà de
        quelques centaines de Mo : sans découpe, les entités retenues
        arrivent en une seule fois en mémoire, et l'arc alpin entier en
        compte plus d'un million.
        """
        pbf = Path(pbf)
        if not pbf.is_file():
            raise OsmError(f"extrait introuvable : {pbf}")
        slug = slug or slugify_region(pbf.name.removesuffix(".osm.pbf").removesuffix(".osm"))

        # Verrou inter-processus : un CLI et un serveur web partageant le
        # même cache ne doivent jamais écrire les FlatGeobuf ou
        # `index.json` en même temps.
        with fetch_lock(self.root):
            counts: dict[str, int] = {}
            shutil.rmtree(self.region_dir(slug), ignore_errors=True)

            for stage, handler in (
                ("lines", self._import_lines),
                ("multipolygons", self._import_multipolygons),
                ("points", self._import_points),
            ):
                if on_progress:
                    on_progress(stage)
                counts.update(handler(pbf, slug, bbox))

            bounds = self._union_bounds(slug, counts)
            if bbox is not None:
                # Le filtre spatial de GDAL renvoie les entités qui
                # *intersectent* l'emprise : une autoroute qui la
                # traverse s'étend bien au-delà. Prendre l'union brute
                # ferait croire à une couverture qui n'existe pas.
                bounds = (
                    max(bounds[0], bbox[0]),
                    max(bounds[1], bbox[1]),
                    min(bounds[2], bbox[2]),
                    min(bounds[3], bbox[3]),
                )
            region = OsmRegion(
                slug=slug,
                source=str(pbf),
                sha256=sha256,
                bounds=bounds,
                counts={k: v for k, v in counts.items() if v},
                imported=datetime.now(UTC).isoformat(timespec="seconds"),
            )
            regions = self.read_index()
            regions[slug] = region
            self.write_index(regions)
        return region

    def _union_bounds(
        self, slug: str, counts: dict[str, int]
    ) -> tuple[float, float, float, float]:
        """Emprise de la région : union des emprises des couches écrites.

        Volontairement mesurée sur les données et non déclarée : elle
        sous-estime légèrement l'extrait, ce qui est le bon sens de
        l'erreur — mieux vaut retomber sur Natural Earth que dessiner une
        demi-carte en OSM détaillé.
        """
        from pyogrio import read_info

        boxes = []
        for layer, count in counts.items():
            if not count:
                continue
            info = read_info(str(self.layer_path(slug, layer)))
            box = info.get("total_bounds")
            if box is not None and all(np.isfinite(box)):
                boxes.append(tuple(float(v) for v in box))
        if not boxes:
            raise OsmError(f"{slug} : aucune géométrie exploitable dans l'extrait")
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )

    def _import_lines(
        self, pbf: Path, slug: str, bbox: tuple[float, float, float, float] | None
    ) -> dict[str, int]:
        meta, _fids, wkb, field_data = self._read_source(
            pbf, "lines", ["highway", "waterway", "name"], lines_where(), bbox
        )
        if wkb is None or len(wkb) == 0:
            return {"rivers": 0, "roads": 0}

        highway = _column(meta, field_data, "highway")
        waterway = _column(meta, field_data, "waterway")

        out: dict[str, int] = {}
        for name, values, table in (
            ("rivers", waterway, WATERWAY_RANKS),
            ("roads", highway, ROAD_RANKS),
        ):
            mask = np.fromiter(
                (str(v) in table for v in values), dtype=bool, count=len(wkb)
            )
            out[name] = self._write_layer(
                self.layer_path(slug, name),
                wkb[mask],
                {"rank": _rank_from(values[mask], table)},
                "MultiLineString",
            )
        return out

    def _import_multipolygons(
        self, pbf: Path, slug: str, bbox: tuple[float, float, float, float] | None
    ) -> dict[str, int]:
        meta, _fids, wkb, field_data = self._read_source(
            pbf,
            "multipolygons",
            ["natural", "landuse", "waterway", "boundary", "admin_level", "name"],
            multipolygons_where(),
            bbox,
        )
        if wkb is None or len(wkb) == 0:
            return {"water": 0, "boundaries": 0}

        natural = _column(meta, field_data, "natural")
        landuse = _column(meta, field_data, "landuse")
        waterway = _column(meta, field_data, "waterway")
        boundary = _column(meta, field_data, "boundary")
        admin = _column(meta, field_data, "admin_level")
        count = len(wkb)

        water = np.fromiter(
            (
                str(natural[i]) in WATER_NATURAL
                or str(landuse[i]) in WATER_LANDUSE
                or str(waterway[i]) in WATER_WATERWAY
                for i in range(count)
            ),
            dtype=bool,
            count=count,
        )
        limits = np.fromiter(
            (
                str(boundary[i]) == "administrative" and str(admin[i]) in ADMIN_RANKS
                for i in range(count)
            ),
            dtype=bool,
            count=count,
        )
        return {
            "water": self._write_layer(
                self.layer_path(slug, "water"), wkb[water], {}, "MultiPolygon"
            ),
            "boundaries": self._write_layer(
                self.layer_path(slug, "boundaries"),
                wkb[limits],
                {"rank": _rank_from(admin[limits], ADMIN_RANKS)},
                "MultiPolygon",
            ),
        }

    def _import_points(
        self, pbf: Path, slug: str, bbox: tuple[float, float, float, float] | None
    ) -> dict[str, int]:
        meta, _fids, wkb, field_data = self._read_source(
            pbf, "points", ["place", "name", "population"], points_where(), bbox
        )
        if wkb is None or len(wkb) == 0:
            return {"labels": 0}

        place = _column(meta, field_data, "place")
        name = _column(meta, field_data, "name")
        population = _column(meta, field_data, "population")

        # Une ville sans nom n'est pas un label.
        named = np.fromiter(
            (v is not None and str(v).strip() != "" for v in name),
            dtype=bool,
            count=len(wkb),
        )
        return {
            "labels": self._write_layer(
                self.layer_path(slug, "labels"),
                wkb[named],
                {
                    "rank": _rank_from(place[named], PLACE_RANKS),
                    "name": np.asarray([str(v) for v in name[named]], dtype=object),
                    "population": _parse_population(population[named], place[named]),
                },
                "Point",
            )
        }
