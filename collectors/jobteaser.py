"""Collecteur JobTeaser.

JobTeaser est le réseau emploi des écoles et universités : beaucoup d'offres
d'alternance y sont exclusives, publiées par des entreprises qui ne diffusent
pas ailleurs. Le site répondait **403 à toute requête Python** — même cause
qu'Indeed, une détection d'empreinte TLS, et même remède : `curl_cffi`.

Le site est en React Server Components : ni JSON-LD ni API sur la page de
liste. En revanche **chaque fiche détaillée expose un JSON-LD `JobPosting`**
complet — titre, description, date, type de contrat, employeur, lieu. C'est
la même architecture que HelloWork : liste en HTML, détail en structuré.

⚠️ **`contract_type=apprenticeship` ne filtre RIEN.** Mesuré sur huit formes
(`contract_type`, `contract_type[]`, `contractType`, `filter[contract_type]`,
valeurs en majuscules, en français…) : toutes renvoient les mêmes 22 offres,
dont 2 alternances seulement. Le filtre est appliqué côté client par leur
application. S'y fier avait injecté en base des doctorats belges, un VIE et un
stage marketing allemand, tous étiquetés « alternance ».

Seul le paramètre **`q`** filtre réellement : `q=alternance developpeur`
renvoie 19 alternances sur 20. D'où deux règles ici — chercher avec
« alternance » dans les mots-clés, et **ne jamais affirmer le type de
contrat** : c'est `classify.py` qui tranche, sur le titre et l'accroche.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from datetime import date, datetime

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from core.models import Job, horodatage

log = logging.getLogger("jobteaser")

BASE = "https://www.jobteaser.com"
RECHERCHE = f"{BASE}/fr/job-offers"
EMPREINTE = "chrome124"

HEADERS = {"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}

# /fr/job-offers/<uuid>-<entreprise>-<intitulé-slugifié>
_LIEN = re.compile(r"/fr/job-offers/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                   r"-[0-9a-f]{4}-[0-9a-f]{12})")


def _propre(brut: str) -> str:
    """Le champ `description` du JSON-LD contient du HTML échappé."""
    texte = re.sub(r"<(?:br|/p|/li|/div)[^>]*>", "\n", brut or "")
    texte = re.sub(r"<[^>]+>", " ", texte)
    return re.sub(r"[ \t]+", " ", html.unescape(texte)).strip()


def _date(valeur: str) -> date | None:
    if not valeur:
        return None
    try:
        return datetime.fromisoformat(valeur.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _lieu(annonce: dict) -> str:
    """`jobLocation` est un objet schema.org, parfois une liste."""
    lieux = annonce.get("jobLocation") or {}
    if isinstance(lieux, list):
        lieux = lieux[0] if lieux else {}
    adresse = (lieux or {}).get("address") or {}
    if isinstance(adresse, str):
        return adresse
    parties = [adresse.get("addressLocality"), adresse.get("addressRegion")]
    return ", ".join(p for p in parties if p)


def _json_ld_offre(soup) -> dict:
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


class JobTeaser:
    def __init__(self, delai: float = 3.0, pages_max: int = 4):
        self.session = cffi.Session(impersonate=EMPREINTE, timeout=45)
        self.session.headers.update(HEADERS)
        self.delai = delai
        self.pages_max = pages_max
        self._dernier = 0.0
        self.requetes = 0
        self._refus = 0

    def _patienter(self) -> None:
        attente = self.delai - (time.monotonic() - self._dernier) + random.uniform(0, 1.2)
        if attente > 0:
            time.sleep(attente)
        self._dernier = time.monotonic()

    def _get(self, url: str, params: dict | None = None, tentatives: int = 3) -> str | None:
        for essai in range(tentatives):
            self._patienter()
            self.requetes += 1
            try:
                r = self.session.get(url, params=params)
            except Exception as e:
                log.warning("réseau : %s", e)
                time.sleep(self.delai * (2**essai))
                continue
            if r.status_code == 200:
                self._refus = 0
                return r.text
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429):
                # Même politique qu'Indeed : on s'arrête au lieu de marteler.
                self._refus += 1
                if self._refus >= 3:
                    raise BlocageJobTeaser(
                        "JobTeaser refuse les requêtes (3 refus consécutifs). "
                        "Arrêt du collecteur."
                    )
                pause = self.delai * (2 ** (essai + 1)) + random.uniform(0, 5)
                log.warning("HTTP %s — pause %.0f s (refus %d/3)",
                            r.status_code, pause, self._refus)
                time.sleep(pause)
                continue
            log.warning("HTTP %s sur %s", r.status_code, url)
            return None
        return None

    def rechercher(self, mots_cles: str) -> list[Job]:
        """Relève les identifiants d'offres, page par page.

        La carte de résultat ne porte que l'entreprise et l'intitulé ; tout le
        reste vient de la fiche. On ne crée donc ici que des coquilles, que
        `completer()` remplira.
        """
        trouves: dict[str, Job] = {}

        for page in range(1, self.pages_max + 1):
            # `q` est le SEUL paramètre qui filtre côté serveur.
            params = {"q": mots_cles}
            if page > 1:
                params["page"] = page

            corps = self._get(RECHERCHE, params)
            if not corps:
                break

            soup = BeautifulSoup(corps, "lxml")
            avant = len(trouves)
            for lien in soup.find_all("a", href=True):
                m = _LIEN.search(lien["href"])
                if not m:
                    continue
                identifiant = m.group(1)
                if identifiant in trouves:
                    continue
                trouves[identifiant] = Job(
                    source="jobteaser",
                    external_id=identifiant,
                    title=lien.get_text(" ", strip=True) or "(à compléter)",
                    company="",
                    location="",
                    url=BASE + lien["href"].split("?")[0],
                    # Surtout PAS de contract_type ici : rien ne le garantit,
                    # et l'affirmer ferait passer des CDI pour des alternances.
                )
            if len(trouves) == avant:
                break  # page sans rien de neuf : fin de la pagination

        return list(trouves.values())

    def completer(self, job: Job) -> tuple[Job, str | None]:
        """Remplit l'offre depuis le JSON-LD de sa fiche."""
        page = self._get(job.url)
        if not page:
            return job, None

        annonce = _json_ld_offre(BeautifulSoup(page, "lxml"))
        if not annonce:
            log.warning("pas de JSON-LD sur %s", job.url)
            return job, page

        job.title = annonce.get("title") or job.title
        job.company = ((annonce.get("hiringOrganization") or {}).get("name")
                       or job.company)
        job.location = _lieu(annonce) or job.location
        job.posted_at = _date(annonce.get("datePosted", ""))
        job.posted_ts = horodatage(annonce.get("datePosted"))
        job.description = _propre(annonce.get("description", ""))

        # `employmentType` est une liste schema.org (INTERN, FULL_TIME…) ; le
        # type réel vient du filtre de recherche, on ne l'écrase pas.
        niveau = annonce.get("educationRequirements")
        if isinstance(niveau, dict) and niveau.get("credentialCategory"):
            job.description += f"\nNiveau requis : {niveau['credentialCategory']}."
        return job, page

    def close(self) -> None:
        self.session.close()


class BlocageJobTeaser(RuntimeError):
    """JobTeaser refuse durablement : s'arrêter plutôt qu'insister."""
