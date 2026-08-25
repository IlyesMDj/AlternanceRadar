"""Modèle de données commun à toutes les sources.

Chaque collecteur produit des `Job` normalisés : le reste du pipeline
(dédoublonnage, classification, scoring, stockage) ne connaît que ce type.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime


def normalize(text: str) -> str:
    """Minuscules, sans accents, sans ponctuation.

    Sert à la fois au matching de mots-clés et aux clés de dédoublonnage,
    pour que « Développeur Full-Stack » et « developpeur full stack » soient
    vus comme identiques.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9+#.]+", " ", text.lower()).strip()


# Formes inclusives : « Chargé(e) », « Développeur(se) », « Apprenti·e ».
_INCLUSIF = re.compile(r"[(·•](?:e|s|se|ses|trice|trices|euse|euses|ne|nes|ive|ives|ère|ere)\)?",
                       re.IGNORECASE)


def normalize_intitule(titre: str) -> str:
    """Normalise un intitulé en neutralisant d'abord l'écriture inclusive.

    Sans ça, « Chargé(e) de Développement RH » devient « charge e de
    developpement rh » : le « e » isolé s'intercale et le motif « charge de »
    ne matche plus. Toute la liste d'exclusion métier sautait sur ce seul
    détail typographique.
    """
    return normalize(_INCLUSIF.sub("", titre or ""))


_DATE_NUE = re.compile(r"\d{4}-\d{2}-\d{2}")


def horodatage(valeur) -> datetime | None:
    """Convertit en `datetime` LOCAL et NAÏF, ou None.

    Naïf à dessein : la fenêtre de fraîcheur se compare à `datetime.now()`,
    lui-même naïf. Mélanger un instant avec fuseau et un instant sans lève
    `TypeError` — d'où la conversion vers l'heure locale avant de retirer le
    fuseau, plutôt qu'un simple `replace(tzinfo=None)` qui décalerait de deux
    heures les dates de PASS et de Welcome to the Jungle, toutes deux en +02:00.

    Accepte l'ISO 8601 (« 2026-08-18T13:25:23+0200 ») comme l'epoch en
    secondes ou en millisecondes — Indeed publie ce dernier.
    """
    if valeur is None or valeur == "":
        return None
    if isinstance(valeur, (int, float)) or (
            isinstance(valeur, str) and valeur.strip().isdigit()):
        n = float(valeur)
        # Au-delà de l'an 2001 en secondes, c'est forcément des millisecondes.
        if n > 1e11:
            n /= 1000
        try:
            return datetime.fromtimestamp(n)
        except (ValueError, OSError, OverflowError):
            return None
    brut = str(valeur).strip()
    # Une date nue ne porte AUCUNE heure : la lire comme minuit inventerait une
    # précision qu'on n'a pas, et minuit est justement le pire cas — l'offre
    # serait réputée vieille de 24 h de plus qu'elle ne l'est peut-être.
    if _DATE_NUE.fullmatch(brut):
        return None
    try:
        quand = datetime.fromisoformat(brut.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (quand.astimezone().replace(tzinfo=None) if quand.tzinfo
            else quand)


def compiler_motifs(motifs: list[str]) -> re.Pattern | None:
    """Compile une liste de motifs en une alternative à frontières de mot.

    Un simple `motif in texte` produirait des faux positifs coûteux :
    « automobile » contient « mobile », « Banque » contient « anque ».
    Les frontières sont définies sur l'alphabet post-normalisation (a-z0-9),
    ce qui laisse passer les motifs à ponctuation comme « c# » ou « ci/cd ».
    """
    alt = "|".join(re.escape(normalize(m)) for m in motifs if m and str(m).strip())
    if not alt:
        return None
    # Le `s?` final tolère le pluriel : sans lui, le motif « juridique »
    # laissait passer « Analyse de données juridiqueS », et « achat »
    # ne voyait pas « achatS ».
    return re.compile(rf"(?<![a-z0-9])(?:{alt})s?(?![a-z0-9])")


def city_of(location: str) -> str:
    """Extrait la ville d'une localisation, quel que soit le format de la source.

    LinkedIn écrit « Nanterre, Île-de-France, France », HelloWork
    « Nanterre - 92 ». Sans traiter les deux séparateurs, la même offre vue sur
    les deux sites produit deux clés de rapprochement différentes.

    Le tiret séparateur est cherché entouré d'espaces, pour ne pas amputer les
    villes composées (« Saint-Jean-de-Monts », « Noyelles-lès-Seclin »).
    Les numéros d'arrondissement sont retirés : « Lyon 2e » et « Lyon » sont
    la même ville.

    Le **code postal** l'est aussi, et pour la même raison : Indeed écrit
    « 75010 Paris » là où Glassdoor écrit « Paris ». Sans ce retrait, une
    offre vue sur les deux sites produisait deux clés distinctes et
    apparaissait deux fois — constaté sur l'alternance Full-Stack IA chez
    explain, présente en double avec des descriptions de 4 577 et 273
    caractères.

    Le code n'est retiré qu'accompagné d'un nom : « 75010 » seul reste tel
    quel, sinon toutes les offres sans ville se rapprocheraient entre elles.
    """
    tete = re.split(r"[,;]| - ", location or "")[0].strip()
    tete = re.sub(r"^\d{5}\s+(?=\S)", "", tete)          # « 75010 Paris »
    tete = re.sub(r"\s+\(?\d{5}\)?$", "", tete)          # « Paris (75010) »
    tete = re.sub(r"\s+\d+\s*(?:er|eme|ème|e)?$", "", tete, flags=re.I)
    return normalize(tete)


@dataclass
class Job:
    source: str
    external_id: str
    title: str
    company: str
    location: str
    url: str
    posted_at: date | None = None
    # Horodatage COMPLET quand la source le donne — PASS, WTTJ, Indeed et
    # JobTeaser publient une heure, LinkedIn, HelloWork et Glassdoor non.
    # Sans lui, « les dernières 24 h » ne peut pas être tenu : une offre
    # datée d'hier a entre 1 et 47 heures, et rien ne permet de trancher.
    posted_ts: datetime | None = None
    contract_type: str | None = None
    remote: str | None = None
    description: str = ""

    # Adresses e-mail trouvées dans le texte — l'intérêt principal des posts
    # de fil par rapport aux offres passant par un ATS.
    contacts: list[str] = field(default_factory=list)

    # Faux pour les posts de fil : leur première ligne n'est pas un intitulé
    # de poste, le gate métier de `score.py` s'y appliquerait au hasard.
    titre_est_intitule: bool = True

    # Renseignés par le pipeline, pas par les collecteurs
    tags: list[str] = field(default_factory=list)
    score: int = 0
    score_detail: str = ""
    is_alternance: bool = False

    # Motif d'exclusion sectorielle ("banque", "NAF 6512Z"...), vide sinon.
    # Stocké plutôt que supprimé : réversible via config.yaml + rescore.
    exclu: str = ""

    @property
    def uid(self) -> str:
        """Identifiant unique intra-source."""
        return f"{self.source}:{self.external_id}"

    @property
    def dedup_key(self) -> str:
        """Clé de rapprochement inter-sources.

        La même offre publiée sur LinkedIn et sur HelloWork n'a pas le même
        identifiant : on la reconnaît par entreprise + intitulé + ville.

        L'intitulé est réduit à un ensemble de mots trié, débarrassé des
        marqueurs de genre et d'écriture inclusive. Sans ça, « Data scientist
        (F/H) - alternance » et « Data Scientist - Alternance H/F » — la même
        offre AXA vue sur deux sites — produisaient deux clés distinctes.
        Les jetons d'une seule lettre disparaissent : c'est ce que deviennent
        « h », « f » et le « e » de « Apprenti(e) » après normalisation.
        """
        mots = sorted({m for m in normalize(self.title).split() if len(m) > 1})
        payload = f"{normalize(self.company)}|{' '.join(mots)}|{city_of(self.location)}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def haystack(self) -> str:
        """Texte normalisé sur lequel tournent classification et scoring."""
        return normalize(f"{self.title} {self.company} {self.description}")
