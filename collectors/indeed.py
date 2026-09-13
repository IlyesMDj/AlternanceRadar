"""Collecteur Indeed.

Indeed renvoyait **403 à toute requête Python**. Ce n'était pas un blocage
d'adresse IP ni un défi JavaScript, mais une détection d'**empreinte TLS** :
la bibliothèque `httpx` produit une signature de handshake reconnaissable
entre mille, que Cloudflare rejette avant même de lire les en-têtes.

`curl_cffi` rejoue l'empreinte TLS et HTTP/2 d'un vrai Chrome — même pile
BoringSSL. Vérifié le 15/08/2026 : 403 → 200, sans proxy ni service payant.

En prime, Indeed embarque ses résultats dans un bloc JSON structuré
(`mosaic-provider-jobcards`) : ni sélecteur CSS ni parsing HTML fragile, et
un champ `jobTypes` qui déclare le type de contrat à la source.
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import date, datetime

from core.models import Job, horodatage

from .http import EMPREINTE, Blocage, ClientSource

log = logging.getLogger("indeed")

BASE = "https://fr.indeed.com"
RECHERCHE = f"{BASE}/jobs"

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

_MOSAIC = re.compile(
    r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});',
    re.S,
)
_DESCRIPTION = re.compile(
    r'<div[^>]+id="jobDescriptionText"[^>]*>(.*?)</div>\s*(?:<div|</div>)', re.S
)


class BlocageIndeed(Blocage):
    """Indeed refuse durablement les requêtes : il faut s'arrêter, pas insister.

    Reste une classe propre à la source pour que `main.py` puisse la
    rattraper seule et continuer avec les autres collecteurs.
    """


# Valeurs que le filtre d'ancienneté d'Indeed accepte réellement. Hors de ce
# domaine, il répond **403** — indiscernable d'un blocage, et c'est ce qui a
# fait croire à un bannissement pendant sept heures : `fromage=30` était la
# seule valeur refusée de toutes celles testées.
FROMAGE_VALIDES = (1, 3, 7, 14)


def _fromage(jours: int) -> int | None:
    """Plus petite fenêtre native couvrant la demande, ou aucune.

    Au-delà de 14 jours on omet le paramètre : le filtrage par date est de
    toute façon refait côté client, et mieux vaut pas de filtre qu'un 403.
    """
    for valide in FROMAGE_VALIDES:
        if jours <= valide:
            return valide
    return None


def _sans_balises(brut: str) -> str:
    texte = re.sub(r"<(?:br|/p|/li|/div)[^>]*>", "\n", brut or "")
    texte = re.sub(r"<[^>]+>", " ", texte)
    return re.sub(r"[ \t]+", " ", html.unescape(texte)).strip()


def _date_de(ms) -> date | None:
    """`pubDate` est un horodatage en millisecondes — date exacte, pas relative."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _parse_resultats(page: str) -> list[dict]:
    """Extrait les offres du bloc JSON embarqué dans la page."""
    m = _MOSAIC.search(page)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        log.warning("bloc mosaic illisible")
        return []
    return (data.get("metaData", {})
                .get("mosaicProviderJobCardsModel", {})
                .get("results", []) or [])


class Indeed:
    def __init__(self, delai: float = 4.0, pages_max: int = 5):
        # `refus_max=3` : s'obstiner face à un refus l'aggrave. C'est en
        # enchaînant onze tentatives qu'on s'est fait couper le 15/08/2026.
        self.client = ClientSource("indeed", delai, empreinte=EMPREINTE,
                                   entetes=HEADERS, jitter=1.5,
                                   refus_max=3, blocage=BlocageIndeed)
        self.pages_max = pages_max

    @property
    def requetes(self) -> int:
        return self.client.requetes

    def _get(self, url: str, params: dict | None = None,
             tentatives: int = 3) -> str | None:
        return self.client.get(url, params, tentatives)

    def rechercher(self, mots_cles: str, age_max_jours: int = 14,
                   lieu: str = "France") -> list[Job]:
        trouves: dict[str, Job] = {}

        fenetre = _fromage(age_max_jours)

        for page in range(self.pages_max):
            params = {"q": mots_cles, "l": lieu, "start": page * 10, "sort": "date"}
            if fenetre:
                params["fromage"] = fenetre
            corps = self._get(RECHERCHE, params)
            if not corps:
                break

            resultats = _parse_resultats(corps)
            if not resultats:
                break

            avant = len(trouves)
            for brut in resultats:
                job = self._vers_job(brut)
                if job:
                    trouves.setdefault(job.external_id, job)
            if len(trouves) == avant:
                break  # page sans rien de neuf : fin utile de la pagination

        return list(trouves.values())

    @staticmethod
    def _vers_job(brut: dict) -> Job | None:
        cle = brut.get("jobkey")
        titre = brut.get("displayTitle") or brut.get("title")
        if not cle or not titre:
            return None

        # `jobTypes` déclare le contrat à la source : « Apprentissage »,
        # « Contrat pro »… C'est ce qui alimente la détection d'alternance,
        # sans avoir à la déduire du texte.
        types = brut.get("jobTypes") or []
        for taxo in brut.get("taxonomyAttributes") or []:
            if taxo.get("label") == "job-types-cc":
                types += [a.get("label", "") for a in taxo.get("attributes") or []]
        contrat = ", ".join(dict.fromkeys(t for t in types if t))

        morceaux = [_sans_balises(brut.get("snippet", ""))]
        salaire = (brut.get("salarySnippet") or {}).get("text")
        if salaire:
            morceaux.append(f"Salaire : {salaire}.")
        if brut.get("remoteLocation"):
            morceaux.append("Télétravail possible.")

        return Job(
            source="indeed",
            external_id=cle,
            title=_sans_balises(titre),
            company=brut.get("company") or "",
            location=brut.get("formattedLocation") or "",
            url=f"{BASE}/viewjob?jk={cle}",
            posted_at=_date_de(brut.get("pubDate")),
            posted_ts=horodatage(brut.get("pubDate")),
            contract_type=contrat or None,
            remote="À distance" if brut.get("remoteLocation") else None,
            description=" ".join(m for m in morceaux if m),
        )

    def completer(self, jobs: list[Job], par_lot: int = 20) -> int:
        """Complète les descriptions **par lots**, via l'API de chargement
        différé qu'utilise le front d'Indeed lui-même.

        `/viewjob` répond 403 même avec l'empreinte Chrome et les cookies de
        session — c'est la page la plus protégée du site. En revanche
        `/rpc/jobdescs?jks=cle1,cle2,…` renvoie un dictionnaire
        {identifiant: description} pour un lot entier : une requête au lieu
        de vingt, et sur un chemin qui n'est pas verrouillé.
        """
        completes = 0
        for debut in range(0, len(jobs), par_lot):
            lot = jobs[debut:debut + par_lot]
            corps = self._get(f"{BASE}/rpc/jobdescs",
                              {"jks": ",".join(j.external_id for j in lot)})
            if not corps:
                continue
            try:
                descriptions = json.loads(corps)
            except ValueError:
                log.warning("réponse jobdescs illisible")
                continue
            for job in lot:
                brut = descriptions.get(job.external_id)
                if brut:
                    job.description += "\n" + _sans_balises(brut)
                    completes += 1
        return completes

    def amorcer(self) -> None:
        """Charge une page de recherche pour que la session acquière les
        cookies d'Indeed, comme le ferait un navigateur avant tout appel
        à ses API internes."""
        self._get(RECHERCHE, {"q": "alternance", "l": "France"})

    def close(self) -> None:
        self.client.close()
