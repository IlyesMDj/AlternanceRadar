"""Client HTTP commun aux collecteurs.

Dix collecteurs réécrivaient le même `_patienter()` et sept le même `_get()`,
à des variantes près qui tenaient dans une poignée de constantes : amplitude
du jitter, nombre de tentatives, codes qui valent un abandon immédiat, codes
qui valent une pause. Le coût de cette duplication n'était pas la longueur —
c'était que **les corrections ne circulaient pas**. Le disjoncteur « 3 refus
consécutifs », écrit pour Indeed après s'être fait couper le 15/08/2026,
n'avait jamais atteint HelloWork ni LinkedIn, qui s'obstinaient encore.

Deux axes de variation, et deux seulement :

- **le transport.** `curl_cffi` rejoue une empreinte TLS de Chrome et passe
  les contrôles passifs (Indeed, Glassdoor, JobTeaser, WTTJ…) ; `httpx` suffit
  aux sources qui n'en opposent aucun (LinkedIn, HelloWork, La Bonne
  Alternance). Passer `empreinte=` choisit le premier, l'omettre le second ;
- **la politique de refus.** Chaque source a ses codes et sa patience. Ils
  sont des paramètres, pas des `if` recopiés.

Ce que le client NE fait pas, volontairement : il ne parse rien et ne connaît
aucun `Job`. Il rend du texte ou du JSON, et c'est au collecteur de savoir ce
qu'il en fait — c'est ce qui permet à chaque parseur de rester lisible seul.

**Une source reste dehors : La Bonne Alternance.** C'est une API authentifiée
qui coopère — elle publie son quota en en-tête, dicte ses pauses par
`retry-after`, et son 403 veut dire « clé refusée » et non « refus
temporaire ». Trois règles incompatibles avec celles d'ici ; les faire entrer
de force rendrait l'un des deux collecteurs faux. Le détail est dans le
docstring de `labonnealternance.py`.
"""

from __future__ import annotations

import logging
import random
import time

log = logging.getLogger("radar.http")

# Empreinte TLS rejouée par curl_cffi. Une seule valeur pour tout le projet :
# la voir dériver de collecteur en collecteur ne servait personne, et huit
# copies à mettre à jour le jour où ce profil vieillit, si.
EMPREINTE = "chrome124"

_ACCEPT_LANGUE = {"Accept-Language": "fr-FR,fr;q=0.9"}


class Blocage(RuntimeError):
    """La source refuse durablement : le collecteur doit s'arrêter.

    Distincte d'un simple échec de requête. S'obstiner face à un refus répété
    ne le lève pas, il l'aggrave — c'est en enchaînant onze tentatives
    qu'Indeed nous a coupés le 15/08/2026. Le collecteur qui la reçoit rend ce
    qu'il a déjà collecté ; main.py la rattrape et poursuit avec les autres
    sources.
    """


class ClientSource:
    """Session HTTP throttlée, avec retry, backoff et disjoncteur.

    `delai` est le creux MINIMUM entre deux requêtes, `jitter` l'aléa ajouté
    par-dessus : un rythme parfaitement régulier est ce qui se repère le plus
    facilement, et ne coûte rien à casser.
    """

    def __init__(self, nom: str, delai: float, *,
                 empreinte: str | None = None,
                 entetes: dict | None = None,
                 jitter: float = 1.0,
                 tentatives: int = 3,
                 timeout: float = 45.0,
                 suivre_redirections: bool = True,
                 codes_refus: tuple[int, ...] = (403, 429),
                 codes_abandon: tuple[int, ...] = (404,),
                 refus_max: int | None = None,
                 blocage: type = Blocage,
                 session=None) -> None:
        self.nom = nom
        self.delai = delai
        self.jitter = jitter
        self.tentatives = tentatives
        self.codes_refus = tuple(codes_refus)
        self.codes_abandon = tuple(codes_abandon)
        self.refus_max = refus_max
        self.blocage = blocage

        self.requetes = 0
        self._dernier = 0.0
        self._refus = 0

        # Session fournie : le transport est alors le choix de l'appelant.
        # Sert aux tests, qui rejouent une séquence de codes sans toucher au
        # réseau — construire un vrai client TLS pour le remplacer aussitôt
        # coûtait une demi-seconde par test, et une suite lente est une suite
        # qu'on finit par ne plus lancer.
        if session is not None:
            self.session = session
        elif empreinte:
            from curl_cffi import requests as cffi
            self.session = cffi.Session(impersonate=empreinte, timeout=timeout)
            self.session.headers.update({**_ACCEPT_LANGUE, **(entetes or {})})
        else:
            import httpx
            self.session = httpx.Client(
                headers={**_ACCEPT_LANGUE, **(entetes or {})},
                timeout=timeout, follow_redirects=suivre_redirections)

    # -- throttling -------------------------------------------------------

    def patienter(self) -> None:
        attente = (self.delai - (time.monotonic() - self._dernier)
                   + random.uniform(0, self.jitter))
        if attente > 0:
            time.sleep(attente)
        self._dernier = time.monotonic()

    # -- requêtes ---------------------------------------------------------

    def get(self, url: str, params: dict | None = None,
            tentatives: int | None = None, **kw) -> str | None:
        """Corps de la réponse en texte, ou None.

        None couvre trois cas que le collecteur n'a aucune raison de
        distinguer : ressource absente, code inattendu, tentatives épuisées.
        Un refus DURABLE, lui, lève `Blocage` — c'est la seule distinction qui
        change ce qu'il y a à faire ensuite.

        Les `**kw` restants partent tels quels au transport : un `headers=`
        propre à UNE requête n'a pas à polluer les en-têtes de la session.
        """
        reponse = self._demander(url, params, tentatives, **kw)
        return None if reponse is None else reponse.text

    def get_json(self, url: str, params: dict | None = None,
                 tentatives: int | None = None, **kw):
        """Réponse décodée en JSON, ou None — y compris si le corps n'en est
        pas. Une page d'erreur HTML servie en 200 est un cas réel, pas une
        hypothèse : elle doit se lire comme une absence, pas exploser."""
        return self._json(self._demander(url, params, tentatives, **kw), url)

    def post_json(self, url: str, tentatives: int | None = None, **kw):
        """POST, réponse décodée en JSON. Welcome to the Jungle interroge un
        index Algolia, qui ne répond qu'à ce verbe — le throttling, le backoff
        et le disjoncteur restent exactement les mêmes."""
        return self._json(
            self._demander(url, None, tentatives, methode="post", **kw), url)

    def _json(self, reponse, url: str):
        if reponse is None:
            return None
        try:
            return reponse.json()
        except Exception:
            log.warning("[%s] réponse non-JSON sur %s", self.nom, url)
            return None

    def _demander(self, url: str, params: dict | None,
                  tentatives: int | None, methode: str = "get", **kw):
        essais = self.tentatives if tentatives is None else tentatives
        if params is not None:
            kw["params"] = params
        for essai in range(essais):
            self.patienter()
            self.requetes += 1
            try:
                r = getattr(self.session, methode)(url, **kw)
            except Exception as e:
                # Certaines sources répondent par un silence complet plutôt
                # que par un code : le délai d'attente EST la réponse.
                log.warning("[%s] réseau (%s) : %s", self.nom,
                            type(e).__name__, e)
                time.sleep(self.delai * (2 ** essai))
                continue

            if r.status_code == 200:
                self._refus = 0
                return r
            if r.status_code in self.codes_abandon:
                return None
            if r.status_code in self.codes_refus:
                self._pause_de_refus(r.status_code, essai)
                continue

            log.warning("[%s] HTTP %s inattendu sur %s",
                        self.nom, r.status_code, url)
            return None

        log.warning("[%s] abandon après %d tentatives : %s",
                    self.nom, essais, url)
        return None

    def _pause_de_refus(self, code: int, essai: int) -> None:
        """Backoff exponentiel, et disjoncteur si la source a un seuil.

        Le compteur est remis à zéro par le premier 200 : ce qu'on surveille
        est une série ININTERROMPUE de refus, pas leur total sur le run.
        """
        self._refus += 1
        if self.refus_max is not None and self._refus >= self.refus_max:
            raise self.blocage(
                f"{self.nom} refuse les requêtes ({self._refus} refus "
                f"consécutifs). Arrêt du collecteur — réessaie plus tard.")

        pause = self.delai * (2 ** (essai + 1)) + random.uniform(0, self.delai)
        compteur = (f" (refus {self._refus}/{self.refus_max})"
                    if self.refus_max else "")
        log.warning("[%s] HTTP %s — pause de %.0f s%s",
                    self.nom, code, pause, compteur)
        time.sleep(pause)

    # -- fin de vie -------------------------------------------------------

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self) -> "ClientSource":
        return self

    def __exit__(self, *_) -> None:
        self.close()
