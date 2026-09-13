"""Collecteur LinkedIn via l'endpoint « invité » (sans authentification).

LinkedIn expose une API publique non documentée pour les visiteurs non
connectés : elle renvoie des fragments HTML de cartes d'offres. C'est la voie
la plus sûre — aucun compte n'est engagé, donc rien à faire bannir. En
contrepartie : pas de filtre « alternance », un plafond d'environ 1000
résultats par requête, et des 429 rapides si on tape trop vite.

Le plafond des 1000 est contourné en découpant les requêtes
(mot-clé x lieu x fenêtre 24 h) : en régime quotidien il n'est jamais atteint.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from core.models import Job

from .http import ClientSource

log = logging.getLogger("linkedin")

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.linkedin.com/jobs",
    "X-Requested-With": "XMLHttpRequest",
}

_URN = re.compile(r"jobPosting:(\d+)")


def _texte(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _parse_cartes(html: str) -> list[Job]:
    """Extrait les offres d'une page de résultats.

    Les sélecteurs sont volontairement tolérants : on s'accroche d'abord à
    `data-entity-urn` (stable depuis des années) plutôt qu'aux classes CSS
    (qui changent).
    """
    soup = BeautifulSoup(html, "lxml")
    jobs: list[Job] = []

    for carte in soup.select("[data-entity-urn]"):
        m = _URN.search(carte.get("data-entity-urn", ""))
        if not m:
            continue
        job_id = m.group(1)

        titre = _texte(
            carte.select_one("h3.base-search-card__title") or carte.select_one("h3")
        )
        entreprise = _texte(
            carte.select_one("h4.base-search-card__subtitle a")
            or carte.select_one("h4.base-search-card__subtitle")
            or carte.select_one("h4")
        )
        lieu = _texte(
            carte.select_one("span.job-search-card__location")
            or carte.select_one("[class*=location]")
        )

        lien = carte.select_one("a.base-card__full-link") or carte.select_one(
            'a[href*="/jobs/view/"]'
        )
        # On coupe les paramètres de tracking : l'URL doit rester stable
        # d'un run à l'autre pour ne pas polluer la base.
        url = (lien.get("href", "").split("?")[0] if lien else
               f"https://www.linkedin.com/jobs/view/{job_id}")

        publie = None
        balise_date = carte.select_one("time[datetime]")
        if balise_date:
            try:
                publie = datetime.fromisoformat(balise_date["datetime"]).date()
            except (ValueError, KeyError):
                publie = None

        if not titre:
            continue

        jobs.append(
            Job(
                source="linkedin",
                external_id=job_id,
                title=titre,
                company=entreprise,
                location=lieu,
                url=url,
                posted_at=publie,
            )
        )

    return jobs


def _parse_detail(html: str) -> dict:
    """Extrait description et critères de la fiche détaillée."""
    soup = BeautifulSoup(html, "lxml")

    description = _texte(
        soup.select_one("div.show-more-less-html__markup")
        or soup.select_one("div.description__text")
        or soup.select_one("section.description")
    )

    criteres: dict[str, str] = {}
    for item in soup.select("li.description__job-criteria-item"):
        cle = _texte(item.select_one("h3, .description__job-criteria-subheader"))
        valeur = _texte(item.select_one("span.description__job-criteria-text, span"))
        if cle and valeur:
            criteres[cle.lower()] = valeur

    def critere(*noms: str) -> str | None:
        for cle, valeur in criteres.items():
            if any(n in cle for n in noms):
                return valeur
        return None

    return {
        "description": description,
        "type_emploi": critere("type d'emploi", "employment type"),
        "niveau": critere("niveau hiérarchique", "seniority"),
        "secteur": critere("secteur", "industries"),
    }


class LinkedInGuest:
    def __init__(self, delai: float = 3.0, max_pages: int = 8):
        # 999 est le code maison de LinkedIn pour « requête bloquée » : il se
        # traite comme un 429, pas comme un code inattendu. 400 rejoint 404 —
        # requête invalide ou offre expirée, insister ne changera rien.
        #
        # `refus_max=4` : ce collecteur n'avait pas de disjoncteur. C'est
        # pourtant la source la plus exposée du projet, et celle qu'on a le
        # moins envie de voir s'obstiner sous un 999.
        self.client = ClientSource("linkedin", delai, entetes=HEADERS,
                                   jitter=1.0, timeout=25.0, tentatives=4,
                                   codes_refus=(403, 429, 999),
                                   codes_abandon=(400, 404), refus_max=4)
        self.max_pages = max_pages

    # -- transport --------------------------------------------------------

    @property
    def requetes(self) -> int:
        return self.client.requetes

    def _get(self, url: str, params: dict | None = None,
             tentatives: int = 4) -> str | None:
        return self.client.get(url, params, tentatives)


    # -- collecte ---------------------------------------------------------

    def rechercher(self, mots_cles: str, lieu: str, fenetre_heures: int = 24,
                   remote: bool = False) -> list[Job]:
        """Parcourt les pages de résultats pour un couple mot-clé / lieu."""
        trouves: dict[str, Job] = {}
        curseur = 0

        for _ in range(self.max_pages):
            params = {
                "keywords": mots_cles,
                "location": lieu,
                "start": curseur,
                "sortBy": "DD",  # les plus récentes d'abord
            }
            if fenetre_heures:
                params["f_TPR"] = f"r{int(fenetre_heures * 3600)}"
            if remote:
                params["f_WT"] = 2  # 1=sur site, 2=à distance, 3=hybride

            html = self._get(SEARCH_URL, params)
            if not html or not html.strip():
                break  # corps vide = fin de pagination

            lot = _parse_cartes(html)
            if not lot:
                break

            avant = len(trouves)
            for job in lot:
                if remote:
                    job.remote = "À distance"
                trouves.setdefault(job.external_id, job)

            # L'endpoint invité renvoie un nombre variable de cartes par page
            # (10 le plus souvent, parfois 25) : impossible de déduire la fin
            # de la pagination d'un seuil fixe. On avance le curseur du nombre
            # réellement reçu, et on s'arrête dès qu'une page n'apporte rien.
            curseur += len(lot)
            if len(trouves) == avant:
                break

        return list(trouves.values())

    def detail(self, job: Job) -> tuple[Job, str | None]:
        """Complète l'offre avec sa description. Retourne aussi le HTML brut."""
        html = self._get(DETAIL_URL.format(job_id=job.external_id))
        if not html:
            return job, None

        d = _parse_detail(html)
        job.description = d["description"]
        job.contract_type = d["type_emploi"] or job.contract_type
        # Le niveau hiérarchique annoncé par LinkedIn est peu fiable, mais il
        # alimente utilement le scoring : on le concatène au texte analysé.
        if d["niveau"]:
            job.description += f"\n\nNiveau hiérarchique : {d['niveau']}"
        return job, html

    def close(self) -> None:
        self.client.close()
