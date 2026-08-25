"""Collecteur La Bonne Alternance (API Apprentissage, mission-apprentissage).

C'est la seule source qui expose le **marché caché** : la réponse contient un
bloc `recruiters` — des entreprises identifiées comme recrutant régulièrement
en alternance **sans avoir publié la moindre offre** (400 000 en 2025). Chacune
vient avec nom, SIRET, taille, site web et un contact direct (téléphone + URL).

L'API est gratuite mais **réservée aux usages non lucratifs** : une recherche
d'alternance personnelle entre pleinement dans ce cadre, la revente de données
non.

Clé d'API : compte à créer sur https://api.apprentissage.beta.gouv.fr, puis
jeton à placer dans la variable d'environnement `LBA_API_KEY`.

    $env:LBA_API_KEY = "..."        # PowerShell, session courante
    setx LBA_API_KEY "..."          # ...ou de façon permanente

NOTE : écrit d'après la spec OpenAPI officielle (`/api/documentation/json`),
mais jamais exécuté contre l'API réelle faute de jeton. Les formes exactes de
`workplace.location` et `offer.publication` sont donc lues défensivement.
"""

from __future__ import annotations

import html
import logging
import os
import time
from datetime import date, datetime

import httpx

from core.models import Job

log = logging.getLogger("lba")

BASE = "https://api.apprentissage.beta.gouv.fr/api"
RECHERCHE = f"{BASE}/job/v1/search"


def _propre(texte: str) -> str:
    """Décode les entités HTML et les échappements de saut de ligne.

    L'API renvoie du texte partiellement encodé : « Outils &amp; Projets » et
    des « \\n » littéraux à deux caractères. Sans ça, les entités s'affichent
    telles quelles dans le digest.
    """
    return html.unescape(texte).replace("\\n", " ").replace("\\t", " ").strip()


def _chaine(valeur, *cles: str) -> str:
    """Lit une valeur qui peut être une chaîne ou un objet, selon la source."""
    if valeur is None:
        return ""
    if isinstance(valeur, str):
        return _propre(valeur)
    if isinstance(valeur, dict):
        for cle in cles:
            trouve = valeur.get(cle)
            if isinstance(trouve, str) and trouve:
                return _propre(trouve)
        # Repli : première valeur textuelle non vide
        for v in valeur.values():
            if isinstance(v, str) and v:
                return _propre(v)
    return str(valeur)


_MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
         "août", "septembre", "octobre", "novembre", "décembre")


def _date(valeur) -> date | None:
    brut = _chaine(valeur, "creation", "start", "date")
    if not brut:
        return None
    try:
        return datetime.fromisoformat(brut.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _mois_fr(valeur) -> str:
    """« 2026-09-01T00:00:00Z » → « septembre 2026 ».

    Le bonus de démarrage cherche « septembre 2026 » en toutes lettres : servi
    en ISO, il ne se déclencherait jamais.
    """
    d = _date(valeur)
    return f"{_MOIS[d.month - 1]} {d.year}" if d else ""


def _naf(lieu: dict) -> tuple[str, str]:
    """Code et libellé NAF — le secteur réel d'activité de l'entreprise.

    Déterminant pour le marché caché : sans description d'offre, c'est le seul
    élément qui dit si l'entreprise fait vraiment de l'informatique.
    """
    naf = ((lieu.get("domain") or {}).get("naf")) or {}
    return _chaine(naf.get("code")), _chaine(naf.get("label"))


class LaBonneAlternance:
    def __init__(self, cle: str | None = None, delai: float = 1.0):
        self.cle = cle or os.environ.get("LBA_API_KEY", "")
        if not self.cle:
            raise RuntimeError(
                "Clé d'API absente. Crée un compte sur "
                "https://api.apprentissage.beta.gouv.fr puis :\n"
                '    $env:LBA_API_KEY = "ta-clé"'
            )
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {self.cle}",
                     "Accept": "application/json"},
            timeout=45.0,
        )
        self.delai = delai
        self.requetes = 0
        self.quota_restant: str | None = None

    def _get(self, params: dict, tentatives: int = 3) -> dict | None:
        for essai in range(tentatives):
            time.sleep(self.delai)
            self.requetes += 1
            try:
                r = self.client.get(RECHERCHE, params=params)
            except httpx.HTTPError as e:
                log.warning("réseau : %s", e)
                continue

            # L'API documente son quota dans les en-têtes : on les suit plutôt
            # que de deviner un rythme au hasard.
            self.quota_restant = r.headers.get("x-ratelimit-remaining")

            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                pause = float(r.headers.get("retry-after", 30)) + 1
                log.warning("quota atteint — pause de %.0fs", pause)
                time.sleep(pause)
                continue
            if r.status_code in (401, 403):
                raise RuntimeError(
                    f"HTTP {r.status_code} — clé d'API refusée ou insuffisante. "
                    "Vérifie LBA_API_KEY."
                )
            log.warning("HTTP %s : %s", r.status_code, r.text[:200])
            return None
        return None

    def rechercher(self, rome: str, latitude: float, longitude: float,
                   rayon: int = 100, niveaux: tuple[str, ...] = ("6", "7")) -> list[Job]:
        """Une recherche géolocalisée pour un code ROME.

        L'API raisonne en rayon autour d'un point : couvrir la France demande
        de balayer plusieurs bassins (voir `config.yaml`).
        """
        jobs: list[Job] = []

        for niveau in niveaux:
            data = self._get({
                "romes": rome,
                "latitude": latitude,
                "longitude": longitude,
                "radius": rayon,
                "target_diploma_level": niveau,
            })
            if not data:
                continue

            for avertissement in data.get("warnings") or []:
                log.warning("API : %s", _chaine(avertissement, "message"))

            jobs.extend(self._offres(data.get("jobs") or []))
            jobs.extend(self._entreprises(data.get("recruiters") or []))

        return jobs

    def _offres(self, bruts: list[dict]) -> list[Job]:
        resultats = []
        for o in bruts:
            offre = o.get("offer") or {}
            lieu = o.get("workplace") or {}
            contrat = o.get("contract") or {}
            postuler = o.get("apply") or {}

            identifiant = _chaine(o.get("identifier"), "id", "partner_job_id")
            if not identifiant:
                continue

            # Une offre expirée ou suspendue n'a aucun intérêt. Ici on filtre
            # sur le statut, pas sur l'âge de publication : LBA maintient des
            # offres actives plusieurs semaines, et `expiration` fait autorité
            # bien mieux qu'une date de création.
            if _chaine(offre.get("status")).lower() not in ("", "active"):
                continue
            fin = _date(offre.get("publication") or {})
            expiration = _chaine((offre.get("publication") or {}).get("expiration"))
            if expiration:
                fin = _date({"date": expiration})
                if fin and fin < date.today():
                    continue

            # Durée, compétences et dates alimentent directement le scoring :
            # on les concatène au texte analysé.
            morceaux = [_chaine(offre.get("description"))]
            for cle in ("desired_skills", "to_be_acquired_skills", "access_conditions"):
                val = offre.get(cle)
                if isinstance(val, list):
                    morceaux.append(" ".join(str(v) for v in val))
                elif val:
                    morceaux.append(str(val))

            duree = contrat.get("duration")
            if duree:
                # Rendu en mois ET en années : le bonus MSc cherche « 2 ans ».
                morceaux.append(f"Durée du contrat : {duree} mois.")
                if int(duree) >= 22:
                    morceaux.append("Contrat sur 2 ans.")
            debut = _mois_fr(contrat.get("start"))
            if debut:
                morceaux.append(f"Démarrage : {debut}.")

            code_naf, libelle_naf = _naf(lieu)
            if libelle_naf:
                morceaux.append(f"Secteur : {libelle_naf} ({code_naf}).")
            if lieu.get("size"):
                morceaux.append(f"Effectif : {lieu['size']}.")

            contacts = [t for t in [_chaine(postuler.get("phone"))] if t]

            resultats.append(Job(
                source="lba",
                external_id=identifiant,
                title=_chaine(offre.get("title")) or "Offre en alternance",
                company=_chaine(lieu.get("name"), "brand", "legal_name"),
                location=_chaine(lieu.get("location"), "address", "city", "label"),
                url=_chaine(postuler.get("url")),
                posted_at=_date(offre.get("publication")),
                # La source ne renvoie que de l'alternance : on l'affirme, ce
                # qui évite à `est_alternance` de le re-déduire du texte.
                contract_type="Alternance",
                remote=_chaine(contrat.get("remote")) or None,
                description=" ".join(m for m in morceaux if m),
                contacts=contacts,
            ))
        return resultats

    def _entreprises(self, bruts: list[dict]) -> list[Job]:
        """Le marché caché : entreprises recrutant sans offre publiée."""
        resultats = []
        for e in bruts:
            lieu = e.get("workplace") or {}
            postuler = e.get("apply") or {}
            identifiant = _chaine(e.get("identifier"), "id", "siret")
            nom = _chaine(lieu.get("name"), "brand", "legal_name")
            if not identifiant or not nom:
                continue

            contacts = [t for t in [_chaine(postuler.get("phone"))] if t]
            code_naf, libelle_naf = _naf(lieu)
            details = " ".join(filter(None, [
                _chaine(lieu.get("description")),
                # Sans offre publiée, le NAF est le seul indice du métier réel
                # de l'entreprise — c'est lui qui porte tout le tri.
                f"Secteur : {libelle_naf} ({code_naf})." if libelle_naf else "",
                f"Effectif : {lieu.get('size')}." if lieu.get("size") else "",
                f"SIRET : {lieu.get('siret')}." if lieu.get("siret") else "",
                f"Site : {_chaine(lieu.get('website'))}." if lieu.get("website") else "",
            ]))

            resultats.append(Job(
                source="lba_entreprise",
                external_id=identifiant,
                title=f"Recrute en alternance (candidature spontanée) — {nom}",
                company=nom,
                location=_chaine(lieu.get("location"), "address", "city", "label"),
                url=_chaine(postuler.get("url")),
                contract_type="Alternance",
                description=details or "Entreprise identifiée comme recrutant "
                                       "régulièrement en alternance.",
                contacts=contacts,
                # Ce n'est pas un intitulé de poste : le gate métier de
                # `score.py` s'y appliquerait au hasard.
                titre_est_intitule=False,
            ))
        return resultats

    def close(self) -> None:
        self.client.close()
