"""Tâches de fond pour les téléchargements de données.

Le rendu (0,27 à 1,11 s, mesuré) reste synchrone dans la requête — le
mettre en tâche serait de la complexité gratuite payée à chaque clic.
Seuls les téléchargements (`Store.fetch`, `OsmStore.download` : minutes)
en ont besoin.

`JobRunner` est un `Protocol` : `ThreadJobRunner` sert en local,
`InlineJobRunner` (exécution synchrone à la soumission) sert aux tests,
qui n'ont ainsi jamais besoin de `time.sleep()` pour attendre un thread.

État en mémoire, par processus — voir le README, section Déploiement.
Ce module ne fonctionne correctement qu'avec un seul worker : le
registre de tâches ne survit pas à un `uvicorn --workers > 1`, et deux
`Store.fetch` concurrents se disputeraient le renommage atomique du
cache. `create_app` refuse de démarrer au-delà d'un worker (voir
`web/__init__.py`).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from traceart.errors import UserError

# Report(progress, message) : progress dans [0, 1], ou -1 quand l'étape
# n'a pas de mesure fiable (import OSM : trois étapes, dont une de
# plusieurs minutes sans retour intermédiaire).
Report = Callable[[float, str], None]

# Un seul job à la fois, pas par confort : Store.fetch réécrit
# manifest.json en fin de course et déplace un fichier de mise en scène
# vers layers/ ; deux téléchargements concurrents corrompraient le cache.
_MAX_CONCURRENT = 1
_MAX_RETAINED = 10


@dataclass(slots=True)
class Job:
    id: str
    kind: str
    state: str = "pending"  # pending | running | done | failed
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    created: float = field(default_factory=time.time)
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def wait(self, timeout: float | None = None) -> bool:
        """Bloque jusqu'à un changement d'état. Pour les tests — jamais
        de `time.sleep()` face à un thread réel."""
        return self._event.wait(timeout)

    def _notify(self) -> None:
        self._event.set()
        self._event = threading.Event()


class JobRunner(Protocol):
    def submit(self, kind: str, work: Callable[[Report], None]) -> Job: ...
    def get(self, job_id: str) -> Job | None: ...
    def running(self) -> Job | None: ...


class ThreadJobRunner:
    """Un thread daemon par tâche, un seul job actif à la fois."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._running_id: str | None = None

    def submit(self, kind: str, work: Callable[[Report], None]) -> Job:
        with self._lock:
            if self._running_id is not None:
                current = self._jobs.get(self._running_id)
                if current is not None and current.state == "running":
                    raise JobBusyError(current)
            job = Job(id=uuid.uuid4().hex, kind=kind)
            self._jobs[job.id] = job
            self._running_id = job.id
            while len(self._jobs) > _MAX_RETAINED:
                self._jobs.popitem(last=False)

        thread = threading.Thread(
            target=self._run, args=(job, work), daemon=True, name=f"traceart-job-{job.id[:8]}"
        )
        thread.start()
        return job

    def _run(self, job: Job, work: Callable[[Report], None]) -> None:
        job.state = "running"
        job._notify()

        def report(progress: float, message: str) -> None:
            job.progress = progress
            job.message = message
            job._notify()

        try:
            work(report)
        except Exception as exc:  # noqa: BLE001 - un job qui échoue ne doit jamais mourir en silence
            job.state = "failed"
            job.error = str(exc)
        else:
            job.state = "done"
            job.progress = 1.0
        finally:
            job._notify()
            with self._lock:
                if self._running_id == job.id:
                    self._running_id = None

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def running(self) -> Job | None:
        with self._lock:
            if self._running_id is None:
                return None
            return self._jobs.get(self._running_id)


class InlineJobRunner:
    """Exécute la tâche à la soumission, dans le thread appelant.

    Réservé aux tests : aucune tâche de fond réelle, donc aucun besoin de
    polling ni de synchronisation pour observer le résultat.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def submit(self, kind: str, work: Callable[[Report], None]) -> Job:
        job = Job(id=uuid.uuid4().hex, kind=kind, state="running")
        self._jobs[job.id] = job

        def report(progress: float, message: str) -> None:
            job.progress = progress
            job.message = message

        try:
            work(report)
        except Exception as exc:  # noqa: BLE001
            job.state = "failed"
            job.error = str(exc)
        else:
            job.state = "done"
            job.progress = 1.0
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def running(self) -> Job | None:
        return next((j for j in self._jobs.values() if j.state == "running"), None)


class JobBusyError(UserError):
    """Un job est déjà en cours ; porte le job actif pour que l'appelant
    puisse rediriger vers son suivi plutôt que d'en démarrer un second."""

    def __init__(self, current: Job) -> None:
        super().__init__(f"une tâche est déjà en cours : {current.id}")
        self.current = current
