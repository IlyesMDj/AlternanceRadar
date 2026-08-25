"""Détection des offres d'alternance.

C'est le cœur du filtrage : LinkedIn n'expose aucun type de contrat
« alternance », il faut donc le déduire du texte de l'offre.
"""

from __future__ import annotations

import re

from .models import Job, normalize

# Marqueurs positifs. « work study » / « apprenticeship » attrapent les
# offres rédigées en anglais par les filiales de groupes étrangers.
_MARQUEURS = re.compile(
    r"\b("
    r"alternance|alternant\w*|"
    r"apprentissage|apprenti\w*|"
    r"contrat de professionnalisation|contrat pro\b|"
    r"work.study|apprenticeship"
    r")\b"
)

# Faux amis : en français « apprentissage automatique/profond » = machine
# learning. Sans ça, toute offre d'IA serait classée comme alternance.
_FAUX_AMIS = re.compile(
    r"apprentissage (automatique|profond|machine|statistique|federe\w*|"
    r"supervis\w*|non supervis\w*|par renforcement|par transfert)"
)

# Rythmes d'alternance, pour affichage et pour le scoring.
_RYTHME = re.compile(r"\b(\d)\s*(?:semaines?|sem\.?|j(?:ours?)?)\s*[/\-]\s*(\d)\s*(?:semaines?|sem\.?|j(?:ours?)?)\b")


# Longueur de l'« accroche » : le début de la description, où une offre
# d'alternance annonce toujours la couleur. Au-delà, une mention du mot ne
# décrit plus le poste mais son contexte.
ACCROCHE = 250


def est_alternance(job: Job) -> bool:
    """Vrai si l'offre est une alternance.

    Le marqueur est exigé dans une **zone restreinte** : le type de contrat
    déclaré par la source, l'intitulé, ou l'accroche de la description.
    Jamais dans le corps du texte.

    Chercher partout produisait des faux positifs coûteux, tous vérifiés sur
    de vraies annonces :

    - « expérience de 3 ans minimale **hors stage alternance** » — un CDI qui
      exclut justement l'alternance de l'expérience demandée ;
    - « un **apprentissage continu** grâce à notre académie de formation » ;
    - « ouverture à l'**apprentissage** et à l'évolution » ;
    - « l'**apprentissage du code de la route** », chez un acteur de
      l'auto-école.

    Les deux offres les mieux notées de la base étaient ainsi des CDI
    réclamant trois ans d'expérience.
    """
    if job.contract_type and _MARQUEURS.search(normalize(job.contract_type)):
        return True

    intitule = _FAUX_AMIS.sub(" ", f" {normalize(job.title)} ")
    if _MARQUEURS.search(intitule):
        return True

    accroche = normalize(job.description or "")[:ACCROCHE]
    return bool(_MARQUEURS.search(_FAUX_AMIS.sub(" ", f" {accroche} ")))


def rythme_detecte(job: Job) -> str | None:
    """Extrait un rythme du type « 3 semaines / 1 semaine » s'il est annoncé."""
    m = _RYTHME.search(f" {job.haystack} ")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def niveau_detecte(job: Job) -> str | None:
    """Extrait le niveau d'études demandé (bac+3, bac+5, M1, M2...)."""
    texte = f" {job.haystack} "
    for motif, libelle in (
        (r"\bbac ?\+ ?5\b|\bm2\b|\bmaster 2\b", "Bac+5 / M2"),
        (r"\bbac ?\+ ?4\b|\bm1\b|\bmaster 1\b", "Bac+4 / M1"),
        (r"\bbac ?\+ ?3\b|\blicence\b|\bbut ?3\b", "Bac+3"),
    ):
        if re.search(motif, texte):
            return libelle
    return None


def annotate(job: Job) -> Job:
    """Renseigne les champs déduits du texte. Modifie et retourne le job.

    On n'écrit surtout PAS « Alternance » dans `contract_type` : depuis que
    `est_alternance` fait confiance à ce champ, le renseigner soi-même
    rendrait la détection auto-confirmante — une offre classée à tort le
    resterait à chaque re-scoring.
    """
    job.is_alternance = est_alternance(job)
    return job


def mots_cles_absents(job: Job, mots: list[str]) -> list[str]:
    """Utilitaire de debug : quels motifs ne sont PAS dans l'offre."""
    texte = f" {job.haystack} "
    return [m for m in mots if normalize(m) not in texte]
