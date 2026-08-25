"""Collecteur adopte1dev.

Un job board **exclusivement dev**, sous WordPress, dont l'API REST est
ouverte : `robots.txt` ne bloque que l'admin et WooCommerce.

Rien à scraper. Le site déclare six taxonomies maison sur ses articles —
`type_de_contrat`, `societe`, `ville`, `technos_principales`, `experience`,
`domaine_de_techno` — et `type_de_contrat` est un **vrai filtre serveur** :
43 alternances sur 173 offres, au même titre que la facette de Welcome to the
Jungle. On peut donc renseigner `contract_type` sans rien affirmer soi-même.

Le rendement est excellent parce que le site est spécialisé : là où LinkedIn
noie les postes de développement sous le marketing et les RH, ici **tout est
du développement**, et le quart est en alternance — Institut Pasteur, MGEN,
TF1, Arkema, Berger-Levrault.

Deux détails de mise en œuvre :

- **l'identifiant du terme « Alternance » est résolu par son nom**, jamais
  codé en dur. Un identifiant WordPress change à la moindre réimportation de
  contenu, et le collecteur se serait tu sans rien signaler — la requête
  aurait répondu 200 avec zéro offre ;
- **`content.rendered` embarque tout le gabarit du site** — menu, pied de
  page, bloc « Infos de base » — soit 7 000 caractères dont l'annonce n'est
  qu'une partie. Le texte utile commence au marqueur « Le Job », qui sert donc
  de frontière ; le reste est reconstruit depuis les taxonomies, plus fiables
  que le HTML.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import date, datetime

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from core.models import Job, horodatage

log = logging.getLogger("adopte1dev")

BASE = "https://adopte1dev.com"
API = f"{BASE}/wp-json/wp/v2"
EMPREINTE = "chrome124"

# Frontière entre le gabarit du site et l'annonce elle-même.
_DEBUT_ANNONCE = re.compile(r"\bLe Job\b")

PAR_PAGE = 100


def _propre(brut: str) -> str:
    if not brut:
        return ""
    texte = BeautifulSoup(brut, "lxml").get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", texte).strip()


def _titre(brut: str) -> str:
    """WordPress rend les titres avec entités et tirets typographiques."""
    return re.sub(r"\s+", " ", html.unescape(
        re.sub(r"<[^>]+>", " ", brut or ""))).replace("–", "-").strip()


def _date(valeur) -> date | None:
    quand = horodatage(valeur)
    if quand:
        return quand.date()
    try:
        return datetime.fromisoformat(str(valeur)).date()
    except (ValueError, TypeError):
        return None


class Adopte1Dev:
    def __init__(self, delai: float = 1.5):
        self.session = cffi.Session(impersonate=EMPREINTE, timeout=45)
        self.session.headers.update({"Accept-Language": "fr-FR,fr;q=0.9"})
        self.delai = delai
        self._dernier = 0.0
        self.requetes = 0
        self._termes: dict[str, dict[int, str]] = {}

    def _patienter(self) -> None:
        attente = self.delai - (time.monotonic() - self._dernier)
        if attente > 0:
            time.sleep(attente)
        self._dernier = time.monotonic()

    def _get(self, chemin: str, params: dict | None = None):
        self._patienter()
        self.requetes += 1
        try:
            r = self.session.get(API + chemin, params=params)
        except Exception as e:
            log.warning("réseau sur %s : %s", chemin, e)
            return None
        if r.status_code != 200:
            log.warning("HTTP %s sur %s", r.status_code, chemin)
            return None
        try:
            return r.json()
        except ValueError:
            log.warning("réponse non-JSON sur %s", chemin)
            return None

    def termes(self, taxonomie: str) -> dict[int, str]:
        """Table identifiant → libellé, chargée une seule fois par run."""
        if taxonomie in self._termes:
            return self._termes[taxonomie]
        table: dict[int, str] = {}
        page = 1
        while page <= 5:      # `societe` compte un terme par entreprise
            lot = self._get(f"/{taxonomie}",
                            {"per_page": PAR_PAGE, "page": page})
            if not lot:
                break
            table.update({t["id"]: html.unescape(t["name"]) for t in lot})
            if len(lot) < PAR_PAGE:
                break
            page += 1
        self._termes[taxonomie] = table
        return table

    def _libelles(self, post: dict, taxonomie: str) -> list[str]:
        table = self.termes(taxonomie)
        return [table[i] for i in (post.get(taxonomie) or []) if i in table]

    def _id_contrat(self, libelle: str) -> int | None:
        """Résout « Alternance » vers son identifiant, par le nom."""
        for identifiant, nom in self.termes("type_de_contrat").items():
            if nom.strip().lower() == libelle.strip().lower():
                return identifiant
        log.error("terme de contrat « %s » introuvable — libellés connus : %s",
                  libelle, sorted(self.termes("type_de_contrat").values()))
        return None

    def rechercher(self, contrat: str = "Alternance") -> list[Job]:
        identifiant = self._id_contrat(contrat)
        if identifiant is None:
            return []

        jobs: list[Job] = []
        page = 1
        while page <= 5:
            lot = self._get("/posts", {"type_de_contrat": identifiant,
                                       "per_page": PAR_PAGE, "page": page})
            if not lot:
                break

            for post in lot:
                titre = _titre((post.get("title") or {}).get("rendered", ""))
                lien = post.get("link")
                if not (titre and lien):
                    continue

                societes = self._libelles(post, "societe")
                villes = self._libelles(post, "ville")

                job = Job(
                    source="adopte1dev",
                    external_id=str(post.get("id") or post.get("slug")),
                    title=titre,
                    company=societes[0] if societes else "",
                    location=villes[0] if villes else "",
                    url=lien,
                    posted_at=_date(post.get("date")),
                    posted_ts=horodatage(post.get("date")),
                    # Taxonomie du site, vérifiée : 43 offres sous ce terme
                    # contre 112 en CDI. Ce n'est pas une déduction.
                    contract_type=contrat,
                )

                corps = _propre((post.get("content") or {}).get("rendered", ""))
                # Tout ce qui précède « Le Job » est le gabarit du site.
                coupe = _DEBUT_ANNONCE.search(corps)
                if coupe:
                    corps = corps[coupe.end():].strip()

                morceaux = [corps]
                for etiquette, taxonomie in (
                        ("Technologies", "technos_principales"),
                        ("Expérience", "experience"),
                        ("Domaine", "domaine_de_techno"),
                        ("Télétravail", "teletravail")):
                    valeurs = self._libelles(post, taxonomie)
                    if valeurs:
                        morceaux.append(f"{etiquette} : {', '.join(valeurs)}")
                job.description = "\n".join(m for m in morceaux if m).strip()
                jobs.append(job)

            if len(lot) < PAR_PAGE:
                break
            page += 1

        log.info("%s → %d offres (%d requêtes)", contrat, len(jobs),
                 self.requetes)
        return jobs

    def close(self) -> None:
        self.session.close()
