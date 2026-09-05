# Prompt — refonte de l'interface web

Prompt prêt à donner à un agent frontend spécialisé pour revoir
l'interface web de TraceArt.

---

Tu es un agent frontend spécialisé (design + implémentation). Charge le
skill `frontend-design` en premier si disponible dans ta session. Ta
mission : revoir toute l'interface web de TraceArt.

## Le projet

TraceArt transforme un GPX en carte dessinée minimaliste (SVG/PNG), à la
manière d'un poster. Le cœur du produit — projection, nettoyage de
trace, fond de carte, thèmes — est en Python (`src/traceart/`). Ce qui
t'intéresse est l'interface web, un sous-ensemble volontairement simple :

- **Stack** : FastAPI + Jinja2 + HTMX. Zéro framework JS, zéro build
  step, zéro dépendance CDN au runtime — `htmx.min.js` est vendoré en
  local (`src/traceart/web/static/htmx.min.js`). C'est un choix de
  philosophie du projet (fonctionne hors ligne, pas de pipeline de
  build) : **ne l'introduis pas**, même si c'est tentant pour un
  redesign. CSS vanilla, un seul fichier
  (`src/traceart/web/static/app.css`, 156 lignes actuellement).
- **Templates** (Jinja2, tous dans `src/traceart/web/templates/`) :
  `index.html` (page racine), `_uploaded.html` (formulaire de rendu,
  106 lignes), `_preview.html` (résultat + stats + téléchargements),
  `_place_suggestions.html`/`_place_results.html`/`_place_list.html`
  (recherche/ajout de villes), `data.html`/`_data_status.html` (gestion
  du cache de fond de carte), `_job.html` (barre de progression),
  `_error.html`, `_empty_workspace.html`.
- **Routes** : `src/traceart/web/routes.py` (333 lignes) et
  `src/traceart/web/data.py`. Pattern HTMX constant dans tout le
  projet : chaque form POST/GET renvoie un **fragment HTML complet**
  qui remplace une cible (`hx-target` + `hx-swap="innerHTML"`), jamais
  de JS client custom, jamais d'état côté navigateur au-delà de ça
  (une exception : un `<input type="color">` togglé par un `onchange`
  inline dans `_uploaded.html`).

## Le flux actuel (à connaître avant de le changer)

1. `index.html` : formulaire d'upload GPX en haut.
2. Après upload, `_uploaded.html` s'affiche : suggestions de villes
   (auto-détectées), recherche/ajout manuel de villes, puis un long
   formulaire de réglages (thème, couleur, format, fond de carte,
   couches à cocher, annotations, titre/sous-titre).
3. Le bouton "Rendre" envoie `/render`, qui remplace `#preview`
   (une `<section>` **vide, en bas de page**, après tout le formulaire)
   par `_preview.html` : l'image SVG, les stats, les liens de
   téléchargement, et des notices conditionnelles (fond manquant,
   extrait OSM manquant avec bouton de téléchargement auto, labels
   hors cadre).

## Faiblesses déjà identifiées (à corriger, pas juste à repeindre)

- **L'aperçu est enterré.** Le produit entier, c'est une image. Elle
  n'apparaît qu'après avoir scrollé un formulaire de ~10 champs, dans
  une section vide au chargement. Aucune vue live/side-by-side pendant
  qu'on règle les options.
- **Esthétique "formulaire admin".** Cartes blanches empilées, pas de
  hiérarchie visuelle forte, alors que le produit vend une identité
  graphique soignée (4 thèmes de sortie : clair, sombre, monochrome,
  blueprint — voir `src/traceart/themes/*.toml` pour la palette de
  référence). L'interface devrait ressembler à une extension de ce
  système visuel, pas à un formulaire générique.
- Vérifie aussi la cohérence responsive (mobile), les états de
  chargement (`.htmx-indicator` existe déjà, utilise-le mieux), et
  l'accessibilité (le `aria-live="polite"` sur `#preview` est déjà là,
  ne le perds pas).

## Contraintes dures

- Tout le texte reste en **français**, ton actuel : direct, technique,
  pas de blabla marketing (regarde le texte existant pour le calibrer).
- Ne change pas les noms de champs de formulaire ni les `id`
  (`#preview`, `#place-results`, `#place-list`, `#osm-auto-job`, etc.)
  sans adapter en même temps `routes.py`/`data.py` — ce sont les cibles
  `hx-target` que le backend connaît. Si tu as besoin d'un nouvel
  endpoint ou d'un nouveau champ, dis-le, ne le fabrique pas côté
  template seul.
- Reste sur le pattern "fragment HTML complet en réponse" — pas de
  state management JS, pas de fetch() custom, HTMX suffit à tout ce
  qui existe.
- Le rendu réel (SVG) doit rester la vedette : toute maquette doit être
  vérifiée avec une vraie image produite (`uv run traceart serve`,
  uploader un GPX réel, comparer).

## Livrable attendu

Vu l'ampleur ("toute l'interface"), commence par une proposition
(layout, hiérarchie, direction visuelle — mockup ou description) avant
de réécrire les 8 templates et le CSS. Je veux valider l'approche avant
l'exécution complète.
