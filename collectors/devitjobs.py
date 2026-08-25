"""Collecteur DevITjobs.fr — flux RSS complet, une seule requête.

SPA React sans API ouverte visible, mais qui publie un flux RSS non paginé et
non filtrable (`?keyword=`, `?contract=`... testés, ignorés) qui rend
l'intégralité des offres en ligne avec leur description complète — 154
offres mesurées. Comme pour PASS, une requête suffit à toute une collecte.

Le site ne connaît pas l'« alternance » comme type de contrat : ses seules
catégories internes sont Freelance/Contract, Stage (Internship), Temps
partiel, Plein temps — vérifié dans son bundle JS. `contract_type` reste
donc vide, et c'est `classify.py` qui tranche sur le titre, comme pour
LinkedIn. Le rendement est pourtant très bon : 57 offres sur 154 mesurées,
la plupart republiées par des CFA (iscod) ou des cabinets (Direct Emploi).

Un item RSS ne porte ni ville ni entreprise en balise dédiée : tout est
compressé dans le titre, au format `<poste>[ - <ville>] @ <entreprise>
[<salaire>]`. Le tiret n'introduit une ville que si ce qui le précède
ressemble déjà à un intitulé complet (plusieurs mots) — un seul mot avant le
tiret (« Alternance - Ingénieur Cloud Privé OpenStack ») signale un tiret de
style, pas une frontière de lieu, et la ville est alors laissée vide plutôt
qu'inventée.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from core.models import Job

log = logging.getLogger("devitjobs")

BASE = "https://devitjobs.fr"
FLUX = f"{BASE}/rss"
EMPREINTE = "chrome124"

_TITRE = re.compile(r"^(?P<poste>.+?)\s*@\s*(?P<entreprise>.+?)\s*\[(?P<salaire>[^\]]*)\]\s*$")
_GENRE = re.compile(r"\(?\s*[hHfF]\s*/\s*[hHfF]\s*\)?")


def _propre(brut: str) -> str:
    if not brut:
        return ""
    texte = BeautifulSoup(brut, "lxml").get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", texte).strip()


def _texte(item, balise: str) -> str:
    e = item.find(balise)
    return e.get_text(strip=True) if e else ""


def _quand(valeur: str) -> datetime | None:
    """`pubDate` est au format RFC 2822 (« Mon, 24 Aug 2026 13:30:44 GMT »),
    hors du format ISO qu'attend `core.models.horodatage`."""
    if not valeur:
        return None
    try:
        quand = parsedate_to_datetime(valeur)
    except (TypeError, ValueError):
        return None
    return quand.astimezone().replace(tzinfo=None) if quand.tzinfo else quand


def _ville(poste: str) -> str:
    """Extrait la ville d'un titre, si le tiret qui la précède en est un.

    « Technicien Support H/F - Quintin » → « Quintin » (l'avant-tiret,
    « Technicien Support », est un intitulé à part entière). « Alternance -
    Ingénieur Cloud Privé OpenStack » → vide (l'avant-tiret, « Alternance »,
    est un seul mot : ce tiret sépare deux morceaux du même intitulé, pas
    une ville).
    """
    nettoye = re.sub(r"\s+", " ", _GENRE.sub(" ", poste)).strip()
    avant, separateur, apres = nettoye.rpartition(" - ")
    return apres.strip() if separateur and len(avant.split()) > 1 else ""


class DevITJobs:
    def __init__(self, delai: float = 1.5):
        self.session = cffi.Session(impersonate=EMPREINTE, timeout=45)
        self.session.headers.update({"Accept-Language": "fr-FR,fr;q=0.9"})
        self.delai = delai
        self._dernier = 0.0
        self.requetes = 0

    def _patienter(self) -> None:
        attente = self.delai - (time.monotonic() - self._dernier)
        if attente > 0:
            time.sleep(attente)
        self._dernier = time.monotonic()

    def rechercher(self) -> list[Job]:
        """Lit le flux entier. Aucune fiche à compléter ensuite."""
        self._patienter()
        self.requetes += 1
        r = self.session.get(FLUX)
        if r.status_code != 200:
            log.warning("HTTP %s sur %s", r.status_code, FLUX)
            return []

        soup = BeautifulSoup(r.text, "xml")
        jobs: list[Job] = []

        for item in soup.find_all("item"):
            m = _TITRE.match(_texte(item, "title"))
            lien = _texte(item, "link").split("?", 1)[0]
            if not (m and lien):
                continue

            poste = m.group("poste").strip()
            job = Job(
                source="devitjobs",
                external_id=lien,
                title=poste,
                company=html.unescape(m.group("entreprise").strip()),
                location=_ville(poste),
                url=lien,
                posted_ts=_quand(_texte(item, "pubDate")),
            )
            if job.posted_ts:
                job.posted_at = job.posted_ts.date()

            job.description = _propre(_texte(item, "encoded") or _texte(item, "description"))
            jobs.append(job)

        log.info("flux → %d offres", len(jobs))
        return jobs

    def close(self) -> None:
        self.session.close()
