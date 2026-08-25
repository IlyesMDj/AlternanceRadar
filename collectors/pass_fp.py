"""Collecteur PASS — Place de l'Apprentissage et des Stages (fonction publique).

La source la mieux structurée du projet, et la seule officielle : l'État
publie lui-même un **flux RSS** de ses offres d'apprentissage, sans clé, sans
quota, et `robots.txt` n'interdit rien.

Le flux dispense entièrement de récupérer les fiches : chaque entrée porte
déjà la description complète (350 à 2 400 caractères mesurés). Une requête
suffit donc pour une collecte entière — le meilleur rapport du projet, devant
Welcome to the Jungle et ses quinze.

Drupal y expose des champs Dublin Core que personne d'autre ne donne :

| balise RSS    | contenu réel                        | usage ici            |
|---------------|-------------------------------------|----------------------|
| `author`      | « Établissement Public du Château » | entreprise           |
| `creator`     | `candidatures@chateauversailles.fr` | **contact direct**   |
| `type`        | « Niveau 7 – (Bac+5 et plus) »      | niveau requis        |
| `date`        | `2026-10-15`                        | **date de début**    |
| `format`      | « Entre 12 et 24 mois »             | durée du contrat     |
| `contributor` | « Ministère de la Culture »         | tutelle              |
| `coverage`    | « VERSAILLES »                      | ville                |

Le `creator` vaut à lui seul le détour : c'est l'adresse de candidature, la
même valeur que les posts LinkedIn, mais obtenue sans session ni navigateur.

**Deux limites mesurées, assumées.** Le flux ne rend que les 68 offres les
plus récentes et ne pagine pas — `?page=2` renvoie les mêmes. C'est sans
conséquence en usage quotidien, la fenêtre couvrant largement une journée,
mais un premier run ne remonte pas l'historique. Et l'offre publique est
majoritairement administrative : 12 titres sur 68 relèvent de la tech. Le
scorer et les exclusions font le tri, comme partout ailleurs.
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

log = logging.getLogger("pass")

BASE = "https://www.pass.fonction-publique.gouv.fr"
# `offres` = apprentissage ; `offres_stages` = stages, hors sujet ici mais
# laissé configurable : `classify.py` tranchera si on l'active un jour.
FLUX = {"apprentissage": "/flux/offres", "stages": "/flux/offres_stages"}
EMPREINTE = "chrome124"

_MAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# « ven 14/08/2026 - 16:29 » — format du flux stages, différent de l'ISO
# 8601 du flux apprentissage. Les deux coexistent, il faut lire les deux.
_DATE_FR = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def _propre(brut: str) -> str:
    texte = re.sub(r"<(?:br|/p|/li|/div|/h\d)[^>]*>", "\n", brut or "")
    texte = re.sub(r"<[^>]+>", " ", texte)
    texte = html.unescape(texte).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", texte)).strip()


def _texte(item, balise: str) -> str:
    """Texte d'une balise, entités comprises.

    Le flux double-échappe certains champs : `author` arrive en
    « Département de l&#039;Eure », que l'analyseur XML rend tel quel puisque
    l'entité est elle-même échappée dans la source. Sans ce `unescape`, les
    noms d'employeurs s'affichent avec leur code HTML dans le digest.
    """
    e = item.find(balise)
    return html.unescape(e.get_text(strip=True)) if e else ""


def _date(valeur: str) -> date | None:
    if not valeur:
        return None
    try:
        return datetime.fromisoformat(valeur.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    m = _DATE_FR.search(valeur)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


class PassFonctionPublique:
    def __init__(self, delai: float = 2.0):
        self.session = cffi.Session(impersonate=EMPREINTE, timeout=60)
        self.session.headers.update({"Accept-Language": "fr-FR,fr;q=0.9"})
        self.delai = delai
        self._dernier = 0.0
        self.requetes = 0

    def _patienter(self) -> None:
        attente = self.delai - (time.monotonic() - self._dernier)
        if attente > 0:
            time.sleep(attente)
        self._dernier = time.monotonic()

    def rechercher(self, flux: str = "apprentissage") -> list[Job]:
        """Lit un flux entier. Aucune fiche à compléter ensuite."""
        chemin = FLUX.get(flux)
        if not chemin:
            raise ValueError(f"flux inconnu : {flux} (attendu : {sorted(FLUX)})")

        self._patienter()
        self.requetes += 1
        r = self.session.get(BASE + chemin)
        if r.status_code != 200:
            log.warning("HTTP %s sur %s", r.status_code, chemin)
            return []

        # Analyseur XML : en mode HTML, lxml renomme les balises Dublin Core
        # et `creator`, `coverage` ou `format` deviennent introuvables.
        soup = BeautifulSoup(r.text, "xml")
        jobs: list[Job] = []

        for item in soup.find_all("item"):
            lien = _texte(item, "link")
            identifiant = (_texte(item, "guid") or _texte(item, "identifier")
                           or lien)
            titre = _texte(item, "title")
            if not (lien and titre):
                continue

            job = Job(
                source="pass",
                external_id=identifiant,
                title=titre,
                company=_texte(item, "author") or _texte(item, "publisher"),
                location=_texte(item, "coverage"),
                url=lien,
                posted_at=_date(_texte(item, "pubDate")),
                posted_ts=horodatage(_texte(item, "pubDate")),
                # Le flux « apprentissage » ne contient que des contrats
                # d'apprentissage : c'est sa raison d'être, pas une déduction.
                contract_type="Alternance" if flux == "apprentissage" else None,
            )

            morceaux = [_propre(_texte(item, "description"))]
            for etiquette, balise in (("Niveau requis", "type"),
                                      ("Durée", "format"),
                                      ("Tutelle", "contributor")):
                valeur = _texte(item, balise)
                if valeur:
                    morceaux.append(f"{etiquette} : {valeur}")
            # Le champ `date` est la prise de poste, pas la publication : à
            # mi-septembre 2026 près, c'est le critère qui décide.
            debut = _date(_texte(item, "date"))
            if debut:
                morceaux.append(f"Début : {debut.isoformat()}")
            job.description = "\n".join(m for m in morceaux if m).strip()

            # `creator` porte l'adresse de candidature, parfois suivie de
            # ponctuation résiduelle (« ... - »).
            job.contacts = sorted(set(_MAIL.findall(
                f"{_texte(item, 'creator')} {_texte(item, 'description')}")))
            jobs.append(job)

        log.info("flux %s → %d offres", flux, len(jobs))
        return jobs

    def close(self) -> None:
        self.session.close()
