"""Détection d'alternance — le filtre le plus coûteux à se tromper.

Les faux positifs testés ici sont ceux cités dans `core/classify.py` : ils
viennent d'annonces réelles, et deux d'entre eux avaient placé des CDI
réclamant trois ans d'expérience en tête de la base.
"""

import pytest

from core.classify import (annotate, est_alternance, niveau_detecte,
                           rythme_compatible, rythme_detecte)
from core.models import Job


def _job(titre="", description="", contract_type=None, company="") -> Job:
    return Job(source="test", external_id="1", title=titre, company=company,
               location="Paris", url="", description=description,
               contract_type=contract_type)


# --- vrais positifs ------------------------------------------------------

@pytest.mark.parametrize("titre", [
    "Alternance Développeur Cloud",
    "Alternant développeur Python",
    "Développeur Web en apprentissage",
    "Apprenti ingénieur logiciel",
    "Contrat de professionnalisation - Data",
    "Work-study Software Engineer",
    "Apprenticeship - Backend Developer",
])
def test_marqueurs_dans_l_intitule(titre):
    assert est_alternance(_job(titre=titre))


def test_marqueur_dans_l_accroche():
    assert est_alternance(_job(
        titre="Développeur Full-Stack",
        description="Nous recherchons un alternant pour rejoindre l'équipe."))


def test_contract_type_de_la_source_fait_foi():
    """HelloWork garantit l'alternance par son filtre natif, et 25 offres
    n'avaient aucun marqueur textuel."""
    assert est_alternance(_job(titre="Développeur Java",
                               contract_type="Alternance"))


# --- faux positifs, tous constatés sur de vraies annonces ----------------

@pytest.mark.parametrize("description", [
    "Expérience de 3 ans minimale hors stage alternance exigée.",
    "Un apprentissage continu grâce à notre académie de formation interne.",
    "Nous valorisons l'ouverture à l'apprentissage et à l'évolution.",
    "L'apprentissage du code de la route se fait avec nos moniteurs.",
])
def test_le_corps_du_texte_ne_declenche_pas(description):
    """Le marqueur n'est cherché QUE dans le type de contrat, l'intitulé ou
    l'accroche — jamais dans le corps."""
    corps = "Poste de développeur confirmé. " * 20 + description
    assert not est_alternance(_job(titre="Développeur Senior", description=corps))


@pytest.mark.parametrize("titre", [
    "Ingénieur en apprentissage automatique",
    "Data Scientist - apprentissage profond",
    "Expert apprentissage par renforcement",
    "Ingénieur apprentissage supervisé",
])
def test_faux_amis_du_machine_learning(titre):
    """En français, « apprentissage automatique » = machine learning. Sans
    cette exclusion, toute offre d'IA serait classée alternance."""
    assert not est_alternance(_job(titre=titre))


def test_faux_ami_dans_l_accroche_aussi():
    assert not est_alternance(_job(
        titre="Ingénieur R&D",
        description="Vous travaillerez sur l'apprentissage profond appliqué."))


# --- rythme --------------------------------------------------------------

@pytest.mark.parametrize("texte, attendu", [
    ("Rythme 3 semaines / 1 semaine", "3/1"),
    ("rythme de 3 sem/1 sem en entreprise", "3/1"),
    ("rythme : 4 jours / 1 jour", "4/1"),
    ("3 semaines en entreprise et 1 semaine à l'école", "3/1"),
    ("Aucun rythme annoncé ici", None),
])
def test_rythme_detecte(texte, attendu):
    assert rythme_detecte(_job(titre="Alternance dev", description=texte)) == attendu


def test_le_rythme_ecrit_ecole_d_abord_est_redresse():
    """Une annonce sur trois écrit l'école en premier. « 1 semaine de
    formation, 3 semaines en entreprise » est un 3/1, pas un 1/3 — sans
    redressement, le meilleur rythme du profil était compté comme le pire."""
    for texte in ("rythme d'alternance : 1 semaine formation théorique, "
                  "3 semaines en entreprise",
                  "un rythme de 1 semaine école 3 semaines entreprise"):
        assert rythme_detecte(_job(titre="Alternance dev",
                                   description=texte)) == "3/1", texte


def test_le_meilleur_rythme_propose_l_emporte():
    """Six offres de la base écrivent « 4 jours / 1 jour OU 3 semaines /
    1 semaine » : ne lire que la première les classait en 4/1 alors que le
    rythme voulu est bien disponible."""
    job = _job(titre="Alternance dev",
               description="rythme 4 jours / 1 jour ou 3 semaines / 1 semaine")
    assert rythme_detecte(job) == "3/1"
    assert rythme_compatible(job) is True


@pytest.mark.parametrize("texte", [
    # Sans marqueur de contexte, deux durées voisines ne font pas un rythme.
    "1 jour de télétravail par semaine, congés 1 jour par mois",
    "contrat de 3 semaines minimum, prime de 1 semaine de salaire",
])
def test_deux_durees_voisines_ne_font_pas_un_rythme(texte):
    """Le prix de la tolérance aux mots intercalés : sans garde, « 1 jour de
    télétravail par semaine, congés 1 jour par mois » se lisait 1/1."""
    assert rythme_detecte(_job(titre="Alternance dev", description=texte)) is None


# --- compatibilité : l'unité décide -------------------------------------

def test_un_rythme_journalier_est_incompatible():
    """Relevé sur la base : « 3/1 » est toujours écrit en semaines, « 4/1 »
    toujours en jours. Un rythme journalier suppose d'être à l'école chaque
    semaine, ce qu'un cursus en blocs de trois semaines ne permet pas."""
    assert rythme_compatible(_job(titre="Alternance dev",
                                  description="rythme 4 jours / 1 jour")) is False


def test_rythme_absent_ne_vaut_ni_oui_ni_non():
    """1 087 offres sur 1 139 n'annoncent rien : ne rien dire n'est pas dire
    non, et les pénaliser viderait le digest."""
    assert rythme_compatible(_job(titre="Alternance dev",
                                  description="Poste passionnant.")) is None


# --- niveau --------------------------------------------------------------

@pytest.mark.parametrize("texte, attendu", [
    ("Profil recherché : Bac+5 / M2", "Bac+5 / M2"),
    ("Étudiant en Master 1", "Bac+4 / M1"),
    ("Niveau bac + 3 ou licence", "Bac+3"),
    ("Aucun niveau précisé", None),
])
def test_niveau_detecte(texte, attendu):
    assert niveau_detecte(_job(titre="Alternance", description=texte)) == attendu


# --- annotate ------------------------------------------------------------

def test_annotate_n_ecrit_jamais_dans_contract_type():
    """Sinon la détection deviendrait auto-confirmante : une offre classée à
    tort le resterait à chaque re-scoring."""
    job = annotate(_job(titre="Alternance Développeur"))
    assert job.is_alternance is True
    assert job.contract_type is None
