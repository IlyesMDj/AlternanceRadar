"""Collecteur Glassdoor.

Glassdoor répond en 200 à `curl_cffi` sans difficulté. La contrainte n'est pas
technique mais **déclarée** : son `robots.txt` interdit explicitement une
partie du site, et ce collecteur s'y tient. Vérifié avec
`urllib.robotparser`, groupes `User-agent` compris :

| chemin                              | robots.txt | ici        |
|-------------------------------------|------------|------------|
| `/Emploi/…-emplois-SRCH_….htm`       | autorisé   | utilisé    |
| `/job-listing/…JV_IC….htm`           | autorisé   | utilisé    |
| `/Emploi/…_IP2.htm` (pagination)     | **interdit** | jamais   |
| `/partner/jobListing.htm`            | **interdit** | jamais   |
| `/api/`, `/graph`                    | **interdit** | jamais   |

Conséquence assumée : **une seule page par recherche, soit 30 offres**. La
profondeur s'obtient en variant les mots-clés, pas en paginant.

L'URL de recherche n'accepte pas de paramètres — `?sc.keyword=` renvoie 404.
Elle encode le mot-clé dans un slug SEO dont les suffixes sont des **positions
de caractères** : `france-alternance-développeur-emplois-SRCH_IL.0,6_IN86_KO7,29`
se lit « le lieu occupe les caractères 0 à 6, le pays est 86 (France), le
mot-clé occupe les caractères 7 à 29 ». D'où `_url_recherche()`, vérifié à
l'identique contre l'URL produite par le site lui-même.

Le lien affiché sur la carte pointe vers `/partner/jobListing.htm`, interdit
et marqué `rel="nofollow"`. On lui préfère donc l'URL canonique du JSON-LD
`ItemList`, qui est autorisée — c'est aussi la seule qui reste valide dans le
temps, celle du partenaire portant un identifiant de session.

**Les fiches détaillées répondent 403, quoi qu'on fasse.** Mesuré sur une
session neuve, quatre fiches à quinze secondes d'intervalle : 403 chaque fois,
alors que la recherche répond 200 juste avant ET juste après. Le blocage vise
donc le chemin, pas notre adresse. `completer()` reste écrit — si Glassdoor
rouvre un jour, il suffira de remonter `max_details_par_run` — mais la
configuration le laisse à 0 : marteler un mur coûte trente secondes par run
pour rien.

Sans fiche, une offre Glassdoor se résume à son titre, son entreprise, son
lieu, son âge et l'extrait de la carte. Le digest la marque « score sur titre
seul », ce qui est exactement la bonne mise en garde.

**Recouvrement mesuré : 18 titres sur 30 étaient déjà en base.** Glassdoor
agrège d'autres sites ; son apport propre tourne autour de 40 %, et le
dédoublonnage inter-sources absorbe le reste. Sa fraîcheur est en outre
médiocre : sur 169 offres relevées, 14 seulement dataient de moins de quinze
jours — la première page privilégie les annonces sponsorisées et anciennes.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import unicodedata
import urllib.parse
from datetime import date, timedelta

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from core.models import Job

log = logging.getLogger("glassdoor")

BASE = "https://www.glassdoor.fr"
EMPREINTE = "chrome124"
PAYS_FRANCE = 86          # `IN86` dans le slug

# « 26j », « 3j », « 30j+ » sur la carte ; parfois « 24 h » pour le jour même.
_AGE = re.compile(r"(\d+)\s*(j|h)\b", re.I)
_ID_FICHE = re.compile(r"jl=(\d+)")


def _slug(texte: str) -> str:
    """Slug Glassdoor : minuscules, accents CONSERVÉS.

    Le site écrit bien « développeur » dans ses URL ; translittérer casserait
    les positions de caractères que les suffixes `KO` encodent.
    """
    t = unicodedata.normalize("NFC", texte.lower().strip())
    t = re.sub(r"[^\w\s-]", "", t, flags=re.UNICODE)
    return re.sub(r"[\s_]+", "-", t).strip("-")


def _url_recherche(mots_cles: str, lieu: str = "france") -> str:
    lieu_s, mot_s = _slug(lieu), _slug(mots_cles)
    debut = len(lieu_s) + 1        # +1 pour le tiret de séparation
    chemin = (f"/Emploi/{lieu_s}-{mot_s}-emplois"
              f"-SRCH_IL.0,{len(lieu_s)}_IN{PAYS_FRANCE}"
              f"_KO{debut},{debut + len(mot_s)}.htm")
    return BASE + urllib.parse.quote(chemin, safe="/,._-")


def _age_en_jours(texte: str) -> int | None:
    m = _AGE.search(texte or "")
    if not m:
        return None
    n, unite = int(m.group(1)), m.group(2).lower()
    return 0 if unite == "h" else n


def _canoniques(soup) -> dict[str, str]:
    """URL pérennes, relevées dans le JSON-LD `ItemList`, par titre.

    Le JSON-LD ne donne que `name` et `url` ; le reste (entreprise, lieu,
    date) vit dans le DOM. On les rapproche par le titre, seul champ commun.
    """
    liens: dict[str, str] = {}
    for balise in soup.find_all("script", type="application/ld+json"):
        try:
            donnees = json.loads(balise.string or "{}")
        except (ValueError, TypeError):
            continue
        if not (isinstance(donnees, dict)
                and donnees.get("@type") == "ItemList"):
            continue
        for element in donnees.get("itemListElement") or []:
            nom, url = element.get("name"), element.get("url")
            if nom and url:
                liens[nom.strip()] = url
    return liens


class Glassdoor:
    def __init__(self, delai: float = 5.0):
        self.session = cffi.Session(impersonate=EMPREINTE, timeout=45)
        self.session.headers.update({"Accept-Language": "fr-FR,fr;q=0.9"})
        self.delai = delai
        self._dernier = 0.0
        self.requetes = 0
        self._refus = 0

    def _patienter(self) -> None:
        attente = self.delai - (time.monotonic() - self._dernier) + random.uniform(0, 2)
        if attente > 0:
            time.sleep(attente)
        self._dernier = time.monotonic()

    def _get(self, url: str, tentatives: int = 3) -> str | None:
        for essai in range(tentatives):
            self._patienter()
            self.requetes += 1
            try:
                r = self.session.get(url)
            except Exception as e:
                # Glassdoor répond parfois par un silence complet plutôt
                # qu'un code d'erreur : le délai d'attente EST la réponse.
                log.warning("réseau (%s) : %s", type(e).__name__, e)
                time.sleep(self.delai * (2**essai))
                continue
            if r.status_code == 200:
                self._refus = 0
                return r.text
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429):
                self._refus += 1
                if self._refus >= 3:
                    raise BlocageGlassdoor(
                        "Glassdoor refuse les requêtes (3 refus consécutifs). "
                        "Arrêt du collecteur.")
                pause = self.delai * (2 ** (essai + 1)) + random.uniform(0, 6)
                log.warning("HTTP %s — pause %.0f s (refus %d/3)",
                            r.status_code, pause, self._refus)
                time.sleep(pause)
                continue
            log.warning("HTTP %s sur %s", r.status_code, url)
            return None
        return None

    def rechercher(self, mots_cles: str, lieu: str = "france") -> list[Job]:
        """Première page de résultats — la seule que robots.txt autorise."""
        corps = self._get(_url_recherche(mots_cles, lieu))
        if not corps:
            return []

        soup = BeautifulSoup(corps, "lxml")
        canoniques = _canoniques(soup)
        jobs: list[Job] = []

        for carte in soup.select('[data-test="jobListing"]'):
            identifiant = carte.get("data-jobid")
            titre_e = carte.select_one('[data-test="job-title"]')
            if not (identifiant and titre_e):
                continue
            titre = titre_e.get_text(" ", strip=True)

            def champ(selecteur: str) -> str:
                e = carte.select_one(selecteur)
                return e.get_text(" ", strip=True) if e else ""

            # L'âge n'est pas toujours affiché (cartes sponsorisées) : sans
            # date, l'offre restera invisible sous `--max-age`, ce qui est le
            # comportement voulu — on ne devine pas une fraîcheur.
            age = _age_en_jours(champ('[data-test="job-age"]'))

            url = canoniques.get(titre)
            if not url:
                # Repli : reconstruire depuis l'identifiant plutôt que
                # d'utiliser le lien `/partner/`, interdit par robots.txt.
                url = f"{BASE}/job-listing/index.htm?jl={identifiant}"

            job = Job(
                source="glassdoor",
                external_id=identifiant,
                title=titre,
                company=champ('[class*="EmployerProfile_compactEmployerName"]'),
                location=champ('[data-test="emp-location"]'),
                url=url,
                posted_at=(date.today() - timedelta(days=age))
                          if age is not None else None,
            )

            morceaux = [champ('[data-test="descSnippet"]')]
            salaire = champ('[data-test="detailSalary"]')
            if salaire:
                morceaux.append(f"Rémunération affichée : {salaire}")
            job.description = "\n".join(m for m in morceaux if m).strip()
            jobs.append(job)

        return jobs

    def completer(self, job: Job) -> tuple[Job, str | None]:
        """Description complète, depuis la fiche canonique (autorisée)."""
        if "/partner/" in job.url:      # ceinture : ce chemin est interdit
            return job, None
        page = self._get(job.url)
        if not page:
            return job, None

        soup = BeautifulSoup(page, "lxml")
        for balise in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(balise.string or "{}")
            except (ValueError, TypeError):
                continue
            if not (isinstance(d, dict) and d.get("@type") == "JobPosting"):
                continue
            texte = re.sub(r"<[^>]+>", " ", d.get("description") or "")
            texte = re.sub(r"\s+", " ", texte).strip()
            if len(texte) > len(job.description):
                job.description = texte
            org = d.get("hiringOrganization")
            if isinstance(org, dict) and org.get("name"):
                job.company = org["name"]
            if d.get("datePosted"):
                try:
                    job.posted_at = date.fromisoformat(d["datePosted"][:10])
                except ValueError:
                    pass
            break
        return job, page

    def close(self) -> None:
        self.session.close()


class BlocageGlassdoor(RuntimeError):
    """Glassdoor refuse durablement : s'arrêter plutôt qu'insister."""
