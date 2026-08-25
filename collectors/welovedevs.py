"""Collecteur WeLoveDevs — recherche Algolia, filtre serveur sur le contrat.

Le site est une SPA Next.js dont les résultats de recherche ne sont jamais
rendus côté serveur : ils arrivent après coup, via un appel `POST` que le
navigateur adresse à `search.welovedevs.com/poc`, un relais maison devant un
index Algolia (« public_jobs »). Repéré en observant le trafic réseau d'un
Chrome piloté par Playwright le temps d'un seul chargement de page — aucune
clé n'y est nécessaire, le relais l'ajoute lui-même côté serveur, et l'appel
direct en `curl_cffi` rend exactement la même réponse.

`contractTypes:apprenticeship` est un **vrai filtre serveur**, la même
famille de garantie que Welcome to the Jungle ou PASS : `contract_type` vaut
« Alternance » par déclaration du site, jamais par déduction du texte.

Le volume est faible — 2 offres nationales mesurées — mais fiable : c'est le
site lui-même qui trie, pas un mot-clé de titre. Une offre sur les deux
portait `disabled: true` malgré un statut `JOB_PUBLISHED` : une annonce de
2018 toujours indexée mais dépubliée, à écarter explicitement plutôt que de
faire confiance au seul statut.
"""

from __future__ import annotations

import json
import logging
import time

from curl_cffi import requests as cffi

from core.models import Job, horodatage

log = logging.getLogger("welovedevs")

RECHERCHE = "https://search.welovedevs.com/poc"
INDEX = "public_jobs"
EMPREINTE = "chrome124"
PAR_PAGE = 100


def _description(hit: dict) -> str:
    corps = hit.get("mdDescription") or hit.get("rawDescription") or hit.get("descriptionPreview") or ""
    details = hit.get("details") or {}
    morceaux = [corps]

    competences = [c["name"] for c in (hit.get("skillsList") or []) if c.get("name")]
    if competences:
        morceaux.append("Compétences : " + ", ".join(competences))

    debut = details.get("start")
    if debut:
        morceaux.append(f"Début : {debut}")

    frequence = (details.get("remotePolicy") or {}).get("frequency")
    if frequence:
        morceaux.append(f"Télétravail : {frequence}")

    salaire = details.get("oldSalary")
    if salaire and salaire.strip().lower() not in ("non renseigné", "non renseigne"):
        morceaux.append(f"Salaire : {salaire}")

    return "\n".join(m for m in morceaux if m).strip()


class WeLoveDevs:
    def __init__(self, delai: float = 1.5):
        self.session = cffi.Session(impersonate=EMPREINTE, timeout=45)
        self.session.headers.update({
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": "https://welovedevs.com/",
        })
        self.delai = delai
        self._dernier = 0.0
        self.requetes = 0

    def _patienter(self) -> None:
        attente = self.delai - (time.monotonic() - self._dernier)
        if attente > 0:
            time.sleep(attente)
        self._dernier = time.monotonic()

    def _page(self, contrat: str, page: int) -> dict | None:
        self._patienter()
        self.requetes += 1
        corps = [{"indexName": INDEX, "params": {
            "clickAnalytics": False,
            "facetFilters": [[f"contractTypes:{contrat}"]],
            "hitsPerPage": PAR_PAGE,
            "page": page,
            "query": "",
        }}]
        r = self.session.post(RECHERCHE, data=json.dumps(corps))
        if r.status_code != 200:
            log.warning("HTTP %s sur la page %d", r.status_code, page)
            return None
        try:
            return r.json()["results"][0]
        except (ValueError, KeyError, IndexError):
            log.warning("réponse inattendue sur la page %d", page)
            return None

    def rechercher(self, contrat: str = "apprenticeship") -> list[Job]:
        jobs: list[Job] = []
        page = 0
        while page < 10:
            resultat = self._page(contrat, page)
            if not resultat:
                break

            for hit in resultat.get("hits", []):
                # Une annonce désactivée reste indexée quelque temps : la
                # rejeter ici évite de faire remonter une offre de 2018.
                if hit.get("disabled"):
                    continue

                titre = hit.get("title")
                identifiant = hit.get("objectID") or hit.get("id")
                if not (titre and identifiant):
                    continue

                lieux = hit.get("formattedPlaces") or []
                quand = (horodatage(hit.get("publishDate"))
                         or horodatage(hit.get("createdAt")))
                job = Job(
                    source="welovedevs",
                    external_id=str(identifiant),
                    title=titre,
                    company=(hit.get("smallCompany") or {}).get("companyName", ""),
                    location=lieux[0] if lieux else "",
                    url=f"https://welovedevs.com/app/jobs?jobId={hit.get('seoAlias', '')}",
                    posted_at=quand.date() if quand else None,
                    posted_ts=quand,
                    # Le facet `contractTypes` est un filtre serveur, pas une
                    # déduction : contrairement à LinkedIn ou DevITjobs, on
                    # affirme « Alternance » sans repasser par classify.py.
                    contract_type="Alternance",
                    description=_description(hit),
                )
                jobs.append(job)

            if page >= resultat.get("nbPages", 1) - 1:
                break
            page += 1

        log.info("%s → %d offres (%d requêtes)", contrat, len(jobs), self.requetes)
        return jobs

    def close(self) -> None:
        self.session.close()
