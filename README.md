# TraceArt

Transforme un fichier GPX en carte dessinée minimaliste — SVG vectoriel
(calques nommés, retouchable dans Inkscape) et PNG optionnel.

```bash
uv run traceart data fetch --scale 10m  # une fois : données de fond (hors ligne ensuite)
uv run traceart mon-parcours.gpx        # un GPX, une image
uv run traceart examples/ALPES --separate --theme dark
uv run traceart mon-parcours.gpx --annotations --profile   # bandeau + profil
uv run traceart info examples/          # mesures, sans rien produire
uv run traceart themes                  # thèmes disponibles
uv run traceart data status             # état du cache
uv run traceart data osm fetch europe/france/auvergne   # Tier B, grand échelle
uv run traceart data osm list           # extraits OSM importés
uv run traceart serve                   # interface web, http://127.0.0.1:8000
```

## Pipeline

```
GPX → nettoyage → mesures → projection → mise en page → fond de carte
    → simplification → SVG
```

Les mesures (distance, D+, durée) sont calculées **avant** simplification :
Douglas-Peucker raccourcit la distance et écrête le dénivelé. Le dénivelé
est lissé avant accumulation — l'hystérésis seule laisse passer le bruit
altimétrique et fabrique des centaines de mètres de D+ fantôme.

## Fond de carte

Vectoriel et filtré par importance, jamais de tuiles raster. Source :
[Natural Earth](https://www.naturalearthdata.com/) (domaine public, aucune
clé API). Le cache vit dans `$XDG_CACHE_HOME/traceart`.

La résolution et les seuils de filtrage suivent l'étendue de la vue :

| Palier | Étendue | Natural Earth | `scalerank` max | Population min |
|---|---|---|---|---|
| monde | ≥ 40° | 110m | 3 | 3 000 000 |
| continent | ≥ 12° | 50m | 5 | 800 000 |
| région | ≥ 2,5° | 10m | 8 | 150 000 |
| local | < 2,5° | 10m | 12 | 15 000 |

Couches activables : `water`, `coastline`, `rivers`, `roads`, `borders`,
`boundaries`, `countries`, `labels` (`--layers water,rivers` ;
`--layers none` pour aucune). Par défaut :
`water,coastline,rivers,borders,boundaries`.

**`borders` et `boundaries` sont deux couches distinctes** : une
frontière nationale structure la carte, une limite de région la meuble.
Elles ne se lisent donc pas pareil — trait affirmé et tirets longs d'un
côté, trait fin et pointillé court de l'autre, avec la frontière dessinée
par-dessus là où les deux se superposent. Côté Natural Earth elles
viennent de deux jeux séparés (`admin_0_boundary_lines_land` et
`admin_1_states_provinces_lines`) ; côté OSM, de `admin_level` — 2 pour
la frontière, 3 à 8 pour l'interne.

**`countries`** pose le nom des pays au centre de leur part **visible** —
le centroïde du polygone entier tomberait hors cadre, le centre de la
France est loin d'une trace alpine. Les noms viennent de `NAME_FR`, avec
repli sur `NAME` pour les territoires non traduits, et le style suit la
convention cartographique : capitales espacées, pas de pastille de
localisation. Un pays occupant moins de 3 % du cadre est écarté, sinon
son nom se tasse contre le bord.

Ces noms restent **Natural Earth** même quand un extrait OSM couvre le
cadre : les relations `admin_level=2` d'OSM sont tronquées au bord d'un
extrait.

Trois limites de Natural Earth, à connaître :

- **Les lacs sont incomplets** (résolu par le Tier B ci-dessus).  `ne_10m_lakes` est mondial et ne couvre
  pas les lacs régionaux ; les suppléments `ne_10m_lakes_europe` et
  `ne_10m_lakes_north_america` (10m uniquement) sont donc inclus. Même
  ainsi, les retenues du Massif Central — Naussac, Villefort, Salagou,
  Bort — sont absentes du jeu à toutes les résolutions. Il faudra le
  Tier B pour les avoir. Les paléo-lacs (`lakes_pluvial`,
  `lakes_historic`) sont volontairement exclus : ils n'existent plus.

- **`labels` est clairsemé.** `ne_10m_populated_places` ne recense que
  7 342 villes dans le monde : les Cévennes ou l'Aubrac n'en ont aucune.
  Utile autour de Lyon ou Bordeaux, muet en zone rurale.
- **`roads` n'existe qu'en 10m.** Aux paliers monde et continent, la
  couche retombe sur le 10m et le filtrage par `scalerank` fait le tri.

Les lacs ne sont pas filtrés par `scalerank` : un lac est une surface,
son étendue à l'écran suffit à décider s'il est visible. Le rang monte
d'ailleurs à 9 dans `ne_10m_lakes`, au-delà du plafond du palier
« région » — filtrer par rang en écartait 35 %.

Les limites internes (départements, régions) ne sont dessinées qu'au
palier local : elles n'ont aucun champ d'importance, et
96 départements à l'échelle d'un pays saturent le fond.

`--basemap auto` (défaut) dessine le fond si le cache est rempli et rend
la trace seule sinon ; `--basemap on` exige les données ; `off` les ignore.

## Structure

| Module | Rôle |
|---|---|
| `core/gpx.py` | parsing streaming (`lxml.iterparse`) |
| `core/clean.py` | doublons, points aberrants, pauses |
| `core/stats.py` | distance géodésique, dénivelé, durée, profil |
| `core/project.py` | WGS84 → Lambert conforme / Mercator / UTM |
| `core/simplify.py` | Douglas-Peucker avec indices conservés |
| `render/layout.py` | bounding box → viewBox |
| `render/theme.py` | thèmes TOML fusionnés sur `light` |
| `render/label.py` | label de fond prêt à poser |
| `render/svg.py` | écriture SVG, calques Inkscape |
| `render/png.py` | rasterisation optionnelle (cairosvg) |
| `basemap/catalog.py` | jeux Natural Earth par couche et résolution |
| `basemap/tiers.py` | palier de zoom → résolution + seuils d'importance |
| `basemap/download.py` | téléchargement en flux, sha256, renommage atomique |
| `basemap/store.py` | cache Natural Earth, conversion FlatGeobuf, manifeste |
| `basemap/query.py` | requête bbox indexée, filtrage, découpe, reprojection |
| `basemap/osm.py` | Tier B : extraits Geofabrik, rangs synthétiques, import |
| `basemap/osmconf.ini` | config du pilote OSM de GDAL |
| `pipeline.py` | chaîne complète, réutilisable comme lib |
| `layers.py` | registre des couches : nom, ordre de dessin, libellé, défauts |
| `errors.py` | racine d'exceptions commune (`TraceArtError`) |
| `options.py` | table de mapping config → `Options`, partagée CLI/web |
| `config.py` | lecture `traceart.toml`, sans dépendance à Click ni FastAPI |
| `cli/` | façade Click sur le cœur |
| `web/` | façade FastAPI + HTMX sur le même cœur |

## Configuration

`./traceart.toml`, sinon `~/.config/traceart/config.toml` :

```toml
[defaults]
theme = "mono"
size = "297mm"
out_dir = "posters"

[clean]
pause_radius_m = 15.0

[annotations]
profile = true
```

Précédence : options CLI > fichier de config > défauts du code.

## Tier B — OpenStreetMap

Natural Earth s'arrête à la moyenne échelle. Pour un poster de rando, un
extrait régional [Geofabrik](https://download.geofabrik.de/) prend le
relais :

```bash
uv run traceart data osm fetch europe/france/auvergne   # 155 Mo
uv run traceart data osm import ~/extraits/lozere.osm.pbf
# gros extrait : découper à l'import est indispensable
uv run traceart data osm fetch europe/alps --around ma-trace.gpx
uv run traceart data osm import alps.osm.pbf --bbox 6.4,44.7,10.8,46.8
```

L'extrait est importé **une fois** en couches FlatGeobuf filtrées et
indexées ; les rendus qui suivent sont aussi rapides que sur Natural
Earth.

**Découper les gros extraits.** Les entités retenues arrivent en une
seule fois en mémoire : au-delà de quelques centaines de Mo, utilise
`--around trace.gpx` (marge de 25 % du grand axe, calculée pour couvrir
le cadre de sortie) ou `--bbox min_lon,min_lat,max_lon,max_lat`. Prévois
aussi ~6 Go d'espace disque **transitoire** : le pilote OSM de GDAL
construit un index des nœuds qu'il libère en fin d'import.

Mesuré sur `europe/france/auvergne` (148 Mo) : 100 923 entités retenues —
48 666 routes, 31 367 cours d'eau, **18 170 plans d'eau** là où Natural
Earth n'en a aucun. 65 Mo de FlatGeobuf, rendu en 2,6 s.

Sur `europe/alps` (2,2 Go) découpé autour d'une traversée des Alpes :
320 588 entités, rendu en 5,9 s.

**Comment OSM s'articule avec Natural Earth**

| Couche | Source quand un extrait couvre le cadre |
|---|---|
| `rivers`, `roads`, `boundaries`, `labels` | OSM remplace Natural Earth |
| `water` | OSM **plus** l'océan Natural Earth |
| `coastline`, `borders`, `countries` | Natural Earth |

La règle : **la structure reste Natural Earth, OSM apporte le détail.**
L'océan parce qu'OSM ne fournit aucun polygone océan, et la côte avec lui
— superposer une côte OSM détaillée à un océan Natural Earth plus
grossier laisserait voir le décalage sur tout le littoral. Les frontières
nationales et les noms de pays parce qu'une relation `admin_level=2` est
tronquée au bord d'un extrait régional : son contour dessinerait un
artefact rectiligne le long de la coupe, et son centroïde ne voudrait
rien dire.

Un extrait n'est retenu que s'il contient **entièrement** l'emprise
visible, et seulement aux paliers 10m (région, local). Une couverture
partielle dessinerait une moitié de carte en détail OSM et l'autre en
Natural Earth grossier. `--no-osm` force Natural Earth.

Aucun extrait ne couvre l'emprise à un palier où OSM s'appliquerait ?
Le rendu se replie sur Natural Earth sans échouer (`Result.osm_missing`),
et le CLI comme l'interface web le signalent — `traceart data osm fetch
<région Geofabrik>`, ou le bouton dédié sur `/data`.

**Notoriété synthétique.** À l'import, chaque entité reçoit un rang
calqué sur le `scalerank` de Natural Earth — `motorway` 1, `primary` 4,
`secondary` 7, `tertiary` 10 ; `river` 3, `canal` 6, `stream` 10 ; pays 1,
région 4, département 8, commune 11. Tout l'aval fonctionne alors sans
savoir d'où viennent les géométries. Ce qui n'a aucun rang dessinable
n'est même pas lu : écarter `residential` et `unclassified` retire à lui
seul la majorité des tronçons d'un extrait.

Les seuils OSM sont **distincts** de ceux de Natural Earth, dans les deux
sens :

- **Rang plus sévère** (5 en région, 8 en local, contre 8 et 12) : au
  même seuil, un extrait régional donnerait chaque ruisseau, chaque route
  communale et chaque limite de commune — une carte topographique, pas un
  poster.
- **Population plus basse** (25 000 en région, 3 000 en local, contre
  150 000 et 15 000) : `POP_MAX` de Natural Earth compte l'agglomération,
  le tag OSM `population` la commune. Bergame vaut 500 000 chez l'un et
  120 000 chez l'autre. Au seuil de Natural Earth, une traversée des
  Alpes n'affichait qu'un seul label.

## Annotations

**Rien par défaut** : la carte sort nue, trace et fond seulement. Aucune
hauteur n'est réservée en pied de page.

- `--annotations` ajoute le bandeau : titre, plage de dates, distance,
  dénivelé positif, durée.
- `--profile` ajoute le profil d'élévation. Indépendant du bandeau — un
  profil seul sous une carte nue est une composition valable, et il ne
  réserve alors pas la place d'un bloc de texte vide.
- `--title` / `--subtitle` surchargent les valeurs déduites du GPX.

## Sortie

SVG à calques nommés, lisibles dans le panneau Inkscape :

```
Fond · Fond·plans d'eau · Fond·côtes · Fond·cours d'eau
     · Fond·routes · Fond·limites administratives · Fond·frontières
Trace · halo · Trace · Trace · départ / arrivée
Labels · pays · Labels · villes
Annotations · filet · Profil d'élévation · Annotations · texte
```

Les trois derniers calques n'apparaissent qu'avec `--annotations` /
`--profile`.

Le grand côté du viewBox vaut toujours 1000 unités : les épaisseurs de
trait et tailles de texte des thèmes ne dépendent donc pas de l'étendue
géographique. `--size` ne change que la taille déclarée du document
(`2000px` par défaut, `297mm` pour l'impression), jamais le rendu.

PNG en option (`--png`, extra `traceart[png]`) : `--dpi` si `--size` est
une taille physique, `--png-scale` si elle est en pixels.

## Interface web

```bash
uv run traceart serve                        # http://127.0.0.1:8000
uv run traceart serve --host 0.0.0.0 --port 9000
```

Extra `traceart[web]` (FastAPI, uvicorn, Jinja2, python-multipart —
HTMX est vendoré dans `web/static/`, aucun CDN, licence 0BSD). Envoie un
GPX, règle thème / couleur de trace / couches de fond / annotations /
profil / format, et récupère le SVG ou un PNG (1000, 2000 ou 4000 px) —
sans ligne de commande. La couleur (`Theme.with_trace_color`, aussi
`--trace-color` en CLI) surcharge uniquement la palette de trace d'un
thème par ailleurs inchangé — fond, couches, typographie restent ceux du
thème choisi.

Volontairement hors du formulaire : tolérance Douglas-Peucker,
projection, seuils de nettoyage — ils gardent leurs défauts, comme sur
le CLI sans options. `--basemap on` n'est jamais proposé non plus : ce
mode échoue net sur un cache incomplet, ce que l'interface ne doit
jamais infliger à l'utilisateur ; le choix visible est
« aucun / automatique ».

**Villes ajoutées manuellement.** Un champ de recherche (Nominatim,
OpenStreetMap) propose des lieux par nom ; chaque résultat s'ajoute à une
liste, avec suppression individuelle. Contrairement aux labels
automatiques du fond de carte, une ville ajoutée ici **s'affiche
toujours**, quel que soit le palier de zoom qui l'aurait normalement
filtrée — c'est tout le sens de la fonctionnalité. Une ville hors du
cadre de la carte n'est pas dessinée (le SVG produit n'a pas de
`clip-path`) mais est signalée dans l'aperçu. La liste vit le temps de
la session, comme les GPX uploadés — un nouvel envoi la réinitialise.

C'est la seule fonctionnalité du projet qui appelle un service réseau au
moment de l'usage : le rendu lui-même (`pipeline.run`) n'en dépend à
aucun moment. Le CLI reste utilisable hors ligne via
`--label "Nom:lon,lat"` (répétable, coordonnées explicites — pas de
recherche par nom, pour ne pas casser l'usage hors ligne du CLI).

**Villes suggérées.** Dès l'envoi du GPX, l'interface propose les plus
grosses villes de l'emprise — données locales (Natural Earth / OSM),
sans le filtre de rang/population du fond automatique, ni appel réseau :
un simple clic les ajoute à la liste ci-dessus. Une ville déjà ajoutée ne
réapparaît pas dans les suggestions.

**`/data`** liste l'état du cache et propose de télécharger les données
manquantes (Natural Earth comme les extraits OSM), avec une barre de
progression qui s'interroge elle-même (`hx-trigger="every 1s"`, sans
WebSocket). Un garde-fou disque refuse un import OSM sous ~10 Go libres —
le pilote GDAL construit un index de nœuds temporaire qui peut peser
plusieurs Go.

Le rendu (0,3 à 1,1 s selon le fond) reste synchrone dans la requête ;
seuls les téléchargements (plusieurs minutes) passent par une tâche de
fond. Les routes de rendu sont déclarées `def`, pas `async def` :
Starlette les bascule dans son threadpool, ce qui laisse la boucle
d'événements répondre aux polls de progression pendant qu'un rendu
tourne.

## Déploiement

**Un seul worker.** `traceart serve` le code en dur, et `create_app`
refuse de démarrer si `WEB_CONCURRENCY > 1` : les sessions d'upload et
les tâches de téléchargement vivent en mémoire, par processus. Au-delà
d'un worker, `POST /data/fetch/…` sur le worker A et `GET
/data/jobs/{id}` round-robiné vers B/C/D donnent un 404 trois fois sur
quatre — la barre de progression reste figée pour de bon, pas
temporairement.

Un verrou fichier (`cache_dir/.fetch.lock`) protège malgré tout le cache
partagé lui-même : un CLI et un serveur web qui pointent vers le même
`cache_dir` ne peuvent pas écrire `manifest.json` ou importer un extrait
OSM en même temps, même sur des processus différents.

Pour dépasser un worker : pré-remplir le cache au build de l'image
(`traceart data fetch` + `traceart data osm fetch …`) et poser
`TRACEART_WEB_ALLOW_FETCH=0` (403 sur les routes de téléchargement, avec
la commande CLI à lancer soi-même à la place) ; ou remplacer
`web/jobs.py` par une file externe — c'est un `Protocol`
(`JobRunner`), pas une classe concrète imposée.

Le chemin de rendu lui-même (`/`, `/upload`, `/render`,
`/artifact/*`) ne garde aucun état partagé entre requêtes et passe à
plusieurs workers sans changement.

## Tests

```bash
uv run pytest        # 360 tests, sans réseau
uv run ruff check src tests
```

Les tests de fond de carte fabriquent des FlatGeobuf synthétiques portant
les noms et champs de Natural Earth ; ceux du Tier B écrivent des
extraits `.osm` XML, que le pilote GDAL lit exactement comme un `.pbf`.
Filtrage, découpe, reprojection, assemblage des relations et sélection de
source sont donc couverts sans télécharger quoi que ce soit.

Les tests web (`fastapi.testclient.TestClient`, aucun port ni
navigateur) réutilisent ce même cache synthétique et un
`InlineJobRunner` — un téléchargement de test s'exécute à l'appel, sans
thread ni `time.sleep()`. `tests/test_invariants.py` verrouille la
direction des dépendances par analyse AST (`core/` ignore `render/` et
`basemap/`, `web/` ignore `cli/`) et la fermeture de la hiérarchie
d'exceptions — vérifiées par sabotage contrôlé, pas seulement par
lecture du code.

## Licence

Le code est sous **MIT** — voir [LICENSE](LICENSE). L'interface web vendore [HTMX](https://htmx.org/) (licence 0BSD, `web/static/htmx.min.js`).

Les données de fond ont leurs propres licences, et elles ne se
comportent pas pareil :

| Source | Licence | Ce que ça implique pour tes cartes |
|---|---|---|
| [Natural Earth](https://www.naturalearthdata.com/) | domaine public | rien, aucune attribution requise |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) | ODbL 1.0 | attribution obligatoire |

Une carte produite à partir d'un extrait OSM est une *produced work* au
sens de l'ODbL. Si tu la diffuses — poster imprimé, image partagée,
publication — elle doit porter la mention :

> © les contributeurs OpenStreetMap

La clause de partage à l'identique de l'ODbL porte sur les *bases de
données* dérivées, pas sur l'image : tu n'as pas à publier quoi que ce
soit d'autre. En revanche, une carte rendue **sans** `--layers` OSM,
c'est-à-dire sur Natural Earth seul, n'est soumise à aucune obligation.

Le champ `fond` du rapport de rendu dit précisément d'où viennent les
géométries :

```
fond   région · OSM alpes (water, rivers, roads, boundaries, labels) + Natural Earth 10m
       └── attribution OSM requise
```
