"""Collecteur Welcome to the Jungle.

La meilleure source du projet, et de loin — parce que c'est la seule dont le
**filtre de contrat est vérifiable et vrai**.

WTTJ ne scrape pas : il interroge un index Algolia public. Les identifiants
sont ceux que le navigateur expose à tout visiteur, interceptés sur la page
de recherche. La clé est restreinte par `Referer` : sans l'en-tête, Algolia
répond « Method not allowed with this referer ». C'est d'ailleurs ce message
qui a servi à trouver le bon nom d'index — un index inexistant répond
« Index not allowed with this API key », un index valide répond sur le
referer. Deux erreurs différentes, donc un oracle.

**Le filtre alternance est authentique**, contrairement à JobTeaser :
`contract_type:APPRENTICESHIP` fait tomber l'index de 88 210 à 2 287 offres,
et les 20 premières remontées portent toutes `APPRENTICESHIP`. On peut donc
renseigner `contract_type` — ce n'est pas une affirmation de ma part, c'est
le champ structuré de la plateforme, au même titre que le JSON-LD HelloWork.

Deux pièges mesurés, traités ici :

1. **Syndication.** La même offre est indexée une fois par job board qui la
   diffuse — `wttj_fr`, le board en marque blanche de l'entreprise, et des
   partenaires (`station-f-job-board`, `la-french-tech`, `hub-bpifrance`…).
   Sur 60 enregistrements : 60 `objectID` distincts pour 42 offres réelles.
   La clé stable est **`reference`**, l'identifiant ATS de l'employeur.

2. **La facette métier est inexploitable.** `profession.category.fr` n'est
   renseignée que sur 172 des 2 287 alternances. Filtrer « Tech » côté
   serveur ne rendait que 20 offres — 92 % du gisement jeté. Le tri métier
   reste donc en Python, où le scorer et les exclusions font déjà ce travail.

La description n'est pas dans l'index : elle vient de l'API publique
`api/v1/organizations/{org}/jobs/{slug}`, qui donne aussi `apply_url`,
`start_date`, `skills` et `tools`. La page publique, elle, répond 202 avec un
défi JavaScript — inutile d'essayer de la lire.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from datetime import date, datetime

from curl_cffi import requests as cffi

from core.models import Job, horodatage

log = logging.getLogger("wttj")

APP_ID = "CSEKHVMS53"
# Clé Algolia « search-only », publique par conception : le navigateur de
# n'importe quel visiteur la porte en clair. Elle ne donne aucun droit
# d'écriture et ne vaut que pour les index de recherche du site.
CLE_RECHERCHE = "4bd8f6215d0cc52b26430765769e65a0"
INDEX = "wk_cms_jobs_production"
RECHERCHE = f"https://{APP_ID.lower()}-dsn.algolia.net/1/indexes/{INDEX}/query"
DETAIL = "https://api.welcometothejungle.com/api/v1/organizations/{org}/jobs/{slug}"
FICHE = "https://www.welcometothejungle.com/fr/companies/{org}/jobs/{slug}"
EMPREINTE = "chrome124"

# Sans Referer/Origin, la clé est refusée : elle est restreinte au domaine.
HEADERS = {
    "x-algolia-api-key": CLE_RECHERCHE,
    "x-algolia-application-id": APP_ID,
    "content-type": "application/x-www-form-urlencoded",
    "Referer": "https://www.welcometothejungle.com/",
    "Origin": "https://www.welcometothejungle.com",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Filtrage serveur : ces deux facettes-là sont fiables (mesurées).
FACETTES = [["contract_type:APPRENTICESHIP"], ["offices.country_code:FR"]]

# Seuls les champs utiles : la réponse brute embarque la description complète
# de l'entreprise à chaque offre, soit des centaines de Ko pour rien.
CHAMPS = ["reference", "name", "slug", "organization", "offices", "office",
          "published_at", "contract_type", "remote", "profession",
          "education_level", "language", "website"]

PAR_PAGE = 200          # Algolia plafonne la pagination à 1000 résultats
BOARD_CANONIQUE = "wttj_fr"


# Clés porteuses de texte dans les objets de l'API ; le reste (identifiants,
# URLs d'images, drapeaux) n'a rien à faire dans une description.
_PARLANTES = ("title", "name", "description", "content", "text", "value")


def _aplatir(valeur) -> str:
    """Réduit en texte n'importe quelle forme rendue par l'API.

    Les champs de WTTJ ne sont pas d'un type stable : `description` est une
    chaîne HTML, `key_missions` une liste d'objets, `skills[].name` un
    dictionnaire multilingue. Une seule fonction pour les trois, plutôt que
    trois `isinstance` disséminés qui finiront par en oublier un.
    """
    if valeur is None or isinstance(valeur, bool):
        return ""
    if isinstance(valeur, str):
        return valeur
    if isinstance(valeur, (int, float)):
        return str(valeur)
    if isinstance(valeur, dict):
        for cle in ("fr", "en"):
            if isinstance(valeur.get(cle), str):
                return valeur[cle]
        retenues = [valeur[c] for c in _PARLANTES if c in valeur] or [
            v for v in valeur.values() if isinstance(v, str)]
        return "\n".join(p for p in map(_aplatir, retenues) if p)
    if isinstance(valeur, list):
        return "\n".join(p for p in map(_aplatir, valeur) if p)
    return ""


def _propre(brut) -> str:
    """Les champs texte de l'API sont du HTML."""
    brut = _aplatir(brut)
    if not brut:
        return ""
    texte = re.sub(r"<(?:br|/p|/li|/div|/h\d)[^>]*>", "\n", brut)
    texte = re.sub(r"<[^>]+>", " ", texte)
    texte = html.unescape(texte)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", texte)).strip()


def _date(valeur: str) -> date | None:
    if not valeur:
        return None
    try:
        return datetime.fromisoformat(valeur.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _ville(hit: dict) -> str:
    """La ville, et rien d'autre.

    `state` et `district` sont inexploitables : traduits en anglais
    (« Normandy », « Brittany », « Loire Region ») et parfois faux — pour une
    offre à Massy, WTTJ annonce state=Normandy et district=Seine-Maritime,
    alors que Massy est dans l'Essonne. Une région fausse vaut moins que pas
    de région du tout, et `city_of()` ne lit de toute façon que la ville.
    """
    bureaux = hit.get("offices") or ([hit["office"]] if hit.get("office") else [])
    for b in bureaux:
        if isinstance(b, dict) and b.get("city"):
            return str(b["city"]).strip()
    return ""


def _liste(valeur) -> str:
    """`skills` et `tools` : des listes, mais à plat sur une seule ligne."""
    if not isinstance(valeur, list):
        return ""
    noms = [_aplatir(v.get("name")) if isinstance(v, dict) else _aplatir(v)
            for v in valeur]
    return ", ".join(n.replace("\n", " ").strip() for n in noms if n)


class WelcomeToTheJungle:
    def __init__(self, delai: float = 1.5, pages_max: int = 5):
        self.session = cffi.Session(impersonate=EMPREINTE, timeout=45)
        self.session.headers.update(HEADERS)
        self.delai = delai
        self.pages_max = pages_max
        self._dernier = 0.0
        self.requetes = 0
        self._refus = 0

    def _patienter(self) -> None:
        attente = self.delai - (time.monotonic() - self._dernier) + random.uniform(0, 0.6)
        if attente > 0:
            time.sleep(attente)
        self._dernier = time.monotonic()

    def _appel(self, methode: str, url: str, **kw) -> dict | None:
        for essai in range(3):
            self._patienter()
            self.requetes += 1
            try:
                r = getattr(self.session, methode)(url, **kw)
            except Exception as e:
                log.warning("réseau : %s", e)
                time.sleep(self.delai * (2**essai))
                continue
            if r.status_code == 200:
                self._refus = 0
                try:
                    return r.json()
                except ValueError:
                    log.warning("réponse non-JSON sur %s", url)
                    return None
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429):
                self._refus += 1
                if self._refus >= 3:
                    raise BlocageWTTJ(
                        "Welcome to the Jungle refuse les requêtes (3 refus "
                        "consécutifs). La clé Algolia publique a peut-être été "
                        "changée : relancer l'interception réseau."
                    )
                pause = self.delai * (2 ** (essai + 1)) + random.uniform(0, 4)
                log.warning("HTTP %s — pause %.0f s (refus %d/3)",
                            r.status_code, pause, self._refus)
                time.sleep(pause)
                continue
            log.warning("HTTP %s sur %s", r.status_code, url)
            return None
        return None

    def rechercher(self, mots_cles: str) -> list[Job]:
        """Interroge l'index et déduplique la syndication.

        Retourne des coquilles : l'index n'a pas la description, `completer()`
        va la chercher. Tout le reste (titre, entreprise, lieu, date, contrat)
        est déjà là — une offre non complétée reste donc exploitable.
        """
        trouves: dict[str, tuple[Job, bool]] = {}

        for page in range(self.pages_max):
            corps = json.dumps({
                "query": mots_cles,
                "hitsPerPage": PAR_PAGE,
                "page": page,
                "facetFilters": FACETTES,
                "attributesToRetrieve": CHAMPS,
                "attributesToHighlight": [],
                "analytics": False,
            })
            reponse = self._appel("post", RECHERCHE, data=corps)
            if not reponse:
                break

            hits = reponse.get("hits") or []
            for hit in hits:
                reference = hit.get("reference")
                org = (hit.get("organization") or {}).get("slug")
                slug = hit.get("slug")
                if not (reference and org and slug):
                    continue

                canonique = ((hit.get("website") or {}).get("reference")
                             == BOARD_CANONIQUE)
                # Même offre déjà vue via un autre board : on ne garde que si
                # celle-ci vient du board officiel (URL plus stable).
                if reference in trouves and not canonique:
                    continue
                if reference in trouves and trouves[reference][1]:
                    continue

                trouves[reference] = (Job(
                    source="wttj",
                    external_id=reference,
                    title=hit.get("name") or "",
                    company=(hit.get("organization") or {}).get("name") or "",
                    location=_ville(hit),
                    url=FICHE.format(org=org, slug=slug),
                    posted_at=_date(hit.get("published_at") or ""),
                    posted_ts=horodatage(hit.get("published_at")),
                    # Facette serveur vérifiée : 88 210 → 2 287 offres, et
                    # 20/20 des remontées portent bien APPRENTICESHIP.
                    contract_type="Alternance",
                    remote=hit.get("remote") or None,
                ), canonique)

            if page + 1 >= (reponse.get("nbPages") or 0):
                break

        return [job for job, _ in trouves.values()]

    def completer(self, job: Job) -> tuple[Job, str | None]:
        """Va chercher la description sur l'API publique de l'offre."""
        m = re.search(r"/companies/([^/]+)/jobs/([^/?]+)", job.url)
        if not m:
            return job, None
        org, slug = m.group(1), m.group(2)

        donnees = self._appel("get", DETAIL.format(org=org, slug=slug),
                              headers={"Accept": "application/json"})
        if not donnees:
            return job, None
        fiche = donnees.get("job") or donnees

        morceaux = [_propre(fiche.get("description") or "")]
        for etiquette, champ in (("Profil recherché", "profile"),
                                 ("Missions", "key_missions"),
                                 ("Processus de recrutement", "recruitment_process")):
            texte = _propre(fiche.get(champ) or "")
            if texte:
                morceaux.append(f"\n{etiquette} :\n{texte}")

        # `start_date` compte beaucoup ici : une alternance qui démarre en
        # janvier ne sert à rien pour une rentrée de mi-septembre 2026.
        for etiquette, valeur in (("Début", _aplatir(fiche.get("start_date"))),
                                  ("Compétences", _liste(fiche.get("skills"))),
                                  ("Outils", _liste(fiche.get("tools")))):
            if valeur:
                morceaux.append(f"{etiquette} : {valeur}")

        duree = fiche.get("contract_duration_min") or fiche.get("contract_duration_max")
        if duree:
            morceaux.append(f"Durée du contrat : {duree} mois")
        if fiche.get("apply_url"):
            morceaux.append(f"Candidature : {fiche['apply_url']}")

        job.description = "\n".join(m for m in morceaux if m).strip()
        job.posted_at = _date(fiche.get("published_at") or "") or job.posted_at
        job.posted_ts = horodatage(fiche.get("published_at")) or job.posted_ts
        job.location = _ville(fiche) or job.location
        return job, json.dumps(donnees, ensure_ascii=False)

    def close(self) -> None:
        self.session.close()


class BlocageWTTJ(RuntimeError):
    """WTTJ refuse durablement : s'arrêter plutôt qu'insister."""
