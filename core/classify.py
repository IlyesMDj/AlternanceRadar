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
#
# Trois propriétés, chacune payée par un cas réel de la base :
#
# 1. **Le séparateur est optionnel.** Ce motif tourne sur `job.haystack`, donc
#    sur du texte déjà passé par `normalize()` — qui remplace « / » et « - »
#    par une espace. En l'exigeant, le motif ne matchait AUCUNE offre :
#    « Rythme 3 semaines / 1 semaine » arrive ici en « rythme 3 semaines
#    1 semaine ». 0 offre taguée sur 4 171, alors que 37 l'annonçaient.
#
# 2. **Jusqu'à cinq mots entre les deux membres.** La formulation réelle n'est
#    presque jamais « 3 semaines 1 semaine » mais « 3 semaines EN ENTREPRISE
#    1 semaine À L'ÉCOLE ». Exiger l'adjacence ne voyait que 3 offres en 3/1 ;
#    la tolérance en voit 24.
#
# 3. **Un marqueur de contexte est exigé** — « rythme », « alternance »… dans
#    les 60 caractères qui précèdent, ou entre les deux membres. C'est ce qui
#    empêche la tolérance de dériver : sans lui, « 1 jour de télétravail par
#    semaine, congés 1 jour par mois » était lu comme un rythme 1/1.
_UNITE = r"(?:semaines?|sem\.?|s|j(?:ours?)?)"

# Sens de lecture. Une annonce sur trois écrit l'école en premier — « 1 semaine
# de formation théorique, 3 semaines en entreprise » est un 3/1, pas un 1/3.
# Sans ce redressement, le meilleur rythme du profil était compté comme le pire.
_ECOLE = re.compile(r"\b(?:ecole|formation|cours|universite|fac|centre|"
                    r"theorique|campus|cfa)\b")
_ENTREPRISE = re.compile(r"\b(?:entreprise|societe|agence|terrain|"
                         r"operationnel|mission|bureau)\b")
_CONTEXTE = re.compile(r"\brythmes?\b")

_RYTHME = re.compile(
    rf"\b(\d)\s*{_UNITE}\b(?P<milieu>(?:\W+\w+){{0,5}}?)\W+\b(\d)\s*{_UNITE}\b"
    # `apres` est une ANTICIPATION : il regarde sans consommer. Consommer
    # trois mots faisait avaler le « ou 3 semaines » de « 4 jours / 1 jour ou
    # 3 semaines / 1 semaine », et le second rythme — le bon — disparaissait
    # du balayage.
    rf"(?=(?P<apres>(?:\W+\w+){{0,3}}))")

# Ordre de préférence quand une annonce en propose plusieurs. Six offres de la
# base écrivent « 4 jours / 1 jour OU 3 semaines / 1 semaine » : ne lire que la
# première les classait en 4/1 alors que le rythme voulu est disponible.
_PREFERENCE = {"3/1": 0, "4/1": 1}

# L'unité n'est pas un détail d'affichage : elle décide de la compatibilité.
# Relevé sur la base, le clivage est net — « 3/1 » est TOUJOURS écrit en
# semaines (26 occurrences), « 4/1 » et « 3/2 » TOUJOURS en jours (20 et 8).
# Or un rythme journalier suppose d'être à l'école chaque semaine, ce qu'un
# cursus organisé en blocs de trois semaines ne permet pas. Une offre qui
# n'annonce qu'un rythme en jours n'est donc pas jouable, même si le ratio
# ressemble à celui qu'on cherche.
_JOURS = re.compile(r"\d\s*j")


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


def _rythmes_annonces(texte: str) -> list[tuple[str, str]]:
    """Tous les rythmes annoncés, en (ratio, unité), dans l'ordre de lecture.

    Chaque occurrence est redressée pour que le premier membre soit toujours
    l'entreprise : « 1 semaine école 3 semaines entreprise » ressort en 3/1.
    """
    trouves: list[tuple[str, str]] = []
    for m in _RYTHME.finditer(f" {texte} "):
        a, b = int(m.group(1)), int(m.group(3))
        milieu, apres = m.group("milieu"), m.group("apres")
        avant = texte[max(0, m.start() - 60):m.start()]
        if not (_CONTEXTE.search(avant)
                or _ECOLE.search(milieu) or _ENTREPRISE.search(milieu)
                or _ECOLE.search(apres) or _ENTREPRISE.search(apres)):
            continue
        if _ECOLE.search(milieu) and (_ENTREPRISE.search(apres)
                                      or not _ENTREPRISE.search(milieu)):
            a, b = b, a
        unite = "jours" if _JOURS.search(m.group(0)) else "semaines"
        trouves.append((f"{a}/{b}", unite))
    return trouves


def rythme_detecte(job: Job) -> str | None:
    """Le rythme le plus favorable annoncé par l'offre, s'il y en a un.

    « Le plus favorable » et non « le premier » : une annonce qui propose
    « 4 jours / 1 jour ou 3 semaines / 1 semaine » offre bien le 3/1, et le
    classer en 4/1 sur le seul ordre des mots serait une perte sèche.
    """
    trouves = [r for r, _ in _rythmes_annonces(job.haystack)]
    if not trouves:
        return None
    return min(trouves, key=lambda r: (_PREFERENCE.get(r, 9), trouves.index(r)))


def rythme_compatible(job: Job) -> bool | None:
    """Le rythme annoncé est-il tenable avec un cursus en blocs de 3 semaines ?

    None quand l'offre n'annonce rien — l'immense majorité, et surtout pas une
    raison de pénaliser : ne rien dire n'est pas dire non.
    """
    annonces = _rythmes_annonces(job.haystack)
    if not annonces:
        return None
    return any(ratio == "3/1" and unite == "semaines" for ratio, unite in annonces)


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
