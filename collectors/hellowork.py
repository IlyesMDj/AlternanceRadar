"""Collecteur HelloWork.

Contrairement à LinkedIn, HelloWork expose un vrai filtre de contrat
(`c=Alternance`) : le tri est fait en amont, pas de détection textuelle à
faire. Aucune protection anti-bot constatée (vérifié le 12/08/2026).

Les points d'accroche sont les attributs `data-cy` — des hooks de tests
end-to-end, bien plus stables que des classes CSS Tailwind générées.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from datetime import date, timedelta

import httpx
from bs4 import BeautifulSoup

from core.models import Job

log = logging.getLogger("hellowork")

BASE = "https://www.hellowork.com"
RECHERCHE = f"{BASE}/fr-fr/emploi/recherche.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

_ID = re.compile(r"/emplois/(\d+)\.html")
_AGE = re.compile(r"il y a\s+(\d+)\s*(heures?|jours?|semaines?|mois|ans?)", re.I)

# « Voir offre de TITRE à LIEU, chez ENTREPRISE, pour un CONTRAT, avec un
# salaire de SALAIRE, en temps plein, DUREE » — résumé complet et fiable,
# utilisé en repli si la structure interne du lien change.
_ARIA = re.compile(
    r"Voir offre de (?P<titre>.+?) à (?P<lieu>.+?), chez (?P<entreprise>.+?), "
    r"pour un (?P<contrat>[^,]+)"
)


def _texte(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _sans_balises(brut: str) -> str:
    """Le champ `description` du JSON-LD contient du HTML échappé."""
    texte = re.sub(r"<[^>]+>", " ", brut or "")
    return re.sub(r"\s+", " ", html.unescape(texte)).strip()


def _json_ld_offre(soup) -> dict:
    """Bloc schema.org `JobPosting` de la page de détail.

    Nettement plus fiable qu'un sélecteur CSS : c'est un format standardisé,
    et il ne contient que l'offre — ni menu, ni pied de page, ni suggestions.
    """
    for balise in soup.find_all("script", type="application/ld+json"):
        try:
            donnees = json.loads(balise.string or "{}")
        except (ValueError, TypeError):
            continue
        if isinstance(donnees, list):
            donnees = next((d for d in donnees if isinstance(d, dict)
                            and d.get("@type") == "JobPosting"), {})
        if isinstance(donnees, dict) and donnees.get("@type") == "JobPosting":
            return donnees
    return {}


def _age_en_jours(texte: str) -> int | None:
    m = _AGE.search(texte or "")
    if not m:
        return None
    n, unite = int(m.group(1)), m.group(2).lower()
    if unite.startswith("heure"):
        return 0
    if unite.startswith("jour"):
        return n
    if unite.startswith("semaine"):
        return n * 7
    if unite.startswith("mois"):
        return n * 30
    return n * 365


def _parse_cartes(html: str, age_max_jours: int) -> list[Job]:
    soup = BeautifulSoup(html, "lxml")
    jobs: list[Job] = []

    for carte in soup.select("[data-cy=serpCard]"):
        lien = carte.select_one("a[data-cy=offerTitle]")
        if not lien:
            continue
        m = _ID.search(lien.get("href", ""))
        if not m:
            continue

        # Le lien contient deux <p> : l'intitulé puis l'entreprise.
        paragraphes = lien.find_all("p")
        titre = _texte(paragraphes[0]) if paragraphes else ""
        entreprise = _texte(paragraphes[1]) if len(paragraphes) > 1 else ""

        aria = _ARIA.search(lien.get("aria-label", "") or "")
        if aria:
            titre = titre or aria.group("titre")
            entreprise = entreprise or aria.group("entreprise")

        # Dernier repli : l'attribut `title` du lien vaut « Intitulé - Entreprise ».
        brut = lien.get("title") or ""
        if not titre:
            titre = brut.split(" - ")[0].strip()
        if not entreprise and " - " in brut:
            entreprise = brut.rsplit(" - ", 1)[-1].strip()
        if not titre:
            continue

        lieu = _texte(carte.select_one("[data-cy=localisationCard]"))
        contrat = _texte(carte.select_one("[data-cy=contractCard]")) or "Alternance"
        duree = _texte(carte.select_one("[data-cy=contractTag]"))

        # La date vit dans le conteneur parent, pas dans la carte elle-même.
        contexte = carte.parent.get_text(" ", strip=True) if carte.parent else ""
        age = _age_en_jours(contexte)
        if age is not None and age > age_max_jours:
            continue

        jobs.append(
            Job(
                source="hellowork",
                external_id=m.group(1),
                title=titre,
                company=entreprise,
                location=lieu,
                url=f"{BASE}{lien['href']}",
                posted_at=(date.today() - timedelta(days=age)) if age is not None else None,
                contract_type=contrat,
                # La durée annoncée alimente le bonus « contrat 2 ans » : sur
                # un MSc en 2 ans, c'est un critère de tri majeur.
                description=f"Durée du contrat : {duree}. " if duree else "",
            )
        )

    return jobs


class HelloWork:
    def __init__(self, delai: float = 2.5, pages_max: int = 5):
        self.client = httpx.Client(headers=HEADERS, timeout=25.0, follow_redirects=True)
        self.delai = delai
        self.pages_max = pages_max
        self._dernier = 0.0
        self.requetes = 0

    def _patienter(self) -> None:
        attente = self.delai - (time.monotonic() - self._dernier) + random.uniform(0, 0.8)
        if attente > 0:
            time.sleep(attente)
        self._dernier = time.monotonic()

    def _get(self, url: str, params: dict | None = None, tentatives: int = 3) -> str | None:
        for essai in range(tentatives):
            self._patienter()
            self.requetes += 1
            try:
                r = self.client.get(url, params=params)
            except httpx.HTTPError as e:
                log.warning("réseau : %s", e)
                time.sleep(self.delai * (2**essai))
                continue
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
            if r.status_code in (429, 403):
                pause = self.delai * (2 ** (essai + 1)) + random.uniform(0, 4)
                log.warning("HTTP %s — pause %.0fs", r.status_code, pause)
                time.sleep(pause)
                continue
            log.warning("HTTP %s sur %s", r.status_code, url)
            return None
        return None

    def rechercher(self, mots_cles: str, age_max_jours: int = 14) -> list[Job]:
        trouves: dict[str, Job] = {}

        for page in range(1, self.pages_max + 1):
            html = self._get(RECHERCHE, {"k": mots_cles, "c": "Alternance", "p": page})
            if not html:
                break
            lot = _parse_cartes(html, age_max_jours)
            avant = len(trouves)
            for job in lot:
                trouves.setdefault(job.external_id, job)
            # Page sans rien de neuf : soit la fin, soit la pagination boucle.
            if len(trouves) == avant:
                break

        return list(trouves.values())

    def detail(self, job: Job) -> tuple[Job, str | None]:
        """Récupère la description complète — indispensable au scoring.

        La source est le bloc **JSON-LD `JobPosting`** de la page, pas le HTML
        rendu. C'est du schema.org : structuré, stable, et surtout limité à
        l'offre elle-même.

        La version précédente retombait sur `<main>` faute de sélecteur, et
        aspirait la page entière — 6971 caractères en moyenne, dont le
        formulaire d'alerte du pied de page. Son menu déroulant « type de
        contrat : CDI, CDD, intérim, stage, alternance » faisait exclure
        l'intégralité des offres HelloWork comme des CDI.
        """
        page = self._get(job.url)
        if not page:
            return job, None

        soup = BeautifulSoup(page, "lxml")
        annonce = _json_ld_offre(soup)
        if not annonce:
            log.warning("pas de JSON-LD sur %s — description non récupérée", job.url)
            return job, page

        job.description += _sans_balises(annonce.get("description", ""))
        organisation = (annonce.get("hiringOrganization") or {}).get("name")
        if organisation and not job.company:
            job.company = organisation
        return job, page

    def close(self) -> None:
        self.client.close()
