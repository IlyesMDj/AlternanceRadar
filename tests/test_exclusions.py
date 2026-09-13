"""Exclusions sectorielles — la règle la plus risquée du projet.

Une exclusion fait DISPARAÎTRE, là où un malus déclasse. Les deux garde-fous
de conception se testent directement : l'exclusion porte sur le champ
entreprise et le code NAF, jamais sur le texte — sauf l'unique dérogation du
courtage scolaire, qui exige deux marqueurs simultanés.
"""

import pytest

from core.exclusions import Exclusions
from core.models import Job

CONFIG = {
    "exclusions": {
        "secteurs": {
            "banque": ["banque", "credit agricole", "bnp"],
            "assurance": ["assurance", "axa"],
            "armement": ["dassault aviation", "thales"],
        },
        "naf_exclus": ["64", "65"],
        "metiers": {
            "commercial": {"motifs": ["commercial", "business developer"]},
            "rh": {"motifs": ["rh", "ressources humaines", "recrutement"]},
        },
        # « stage » n'est volontairement PAS dans cette liste — un motif
        # brut écarterait aussi « Stage / Alternance ». Règle dédiée.
        "contrats": {"exclus": ["cdi", "freelance", "vie"]},
        "courtage_ecole": {
            "marqueurs_ecole": ["cfa", "ecole", "centre de formation"],
            "marqueurs_courtage": ["entreprises partenaires",
                                   "pour l une de ses entreprises"],
        },
        "exceptions": ["banques alimentaires", "cegedim assurances"],
    }
}


@pytest.fixture
def exclusions():
    return Exclusions(CONFIG)


def _job(titre="Alternance Développeur", company="", description="",
         titre_est_intitule=True) -> Job:
    return Job(source="test", external_id="1", title=titre, company=company,
               location="Paris", url="", description=description,
               titre_est_intitule=titre_est_intitule)


# --- secteurs, sur le champ entreprise -----------------------------------

@pytest.mark.parametrize("entreprise, motif", [
    ("BNP Paribas", "banque"),
    ("AXA France", "assurance"),
    ("Thales", "armement"),
])
def test_secteur_exclu_sur_l_entreprise(exclusions, entreprise, motif):
    assert exclusions.motif(_job(company=entreprise)) == motif


def test_le_secteur_cite_dans_le_TEXTE_n_exclut_pas(exclusions):
    """« Une annonce de développeur qui mentionne "secteur bancaire" parmi
    ses clients n'est pas une offre de banque. »"""
    job = _job(company="Sopra Steria",
               description="Vous interviendrez pour des clients du secteur "
                           "banque et assurance.")
    assert exclusions.motif(job) == ""


@pytest.mark.parametrize("entreprise", ["Banques Alimentaires",
                                        "Cegedim Assurances"])
def test_les_exceptions_priment_sur_tout(exclusions, entreprise):
    assert exclusions.motif(_job(company=entreprise)) == ""


# --- métiers et contrats, sur l'intitulé ---------------------------------

def test_metier_exclu_sur_l_intitule(exclusions):
    assert exclusions.motif(_job(titre="Alternance Commercial B2B")) == "commercial"


def test_contrat_exclu_sur_l_intitule(exclusions):
    assert exclusions.motif(_job(titre="CDI Développeur")) == \
        "contrat hors alternance"


def test_les_posts_de_fil_echappent_au_gate_intitule(exclusions):
    """Leur « titre » est la première ligne du post, pas un intitulé."""
    job = _job(titre="Notre équipe recrutement cherche un alternant dev",
               titre_est_intitule=False)
    assert exclusions.motif(job) == ""


# --- contrat déclaré dans la description ---------------------------------

def test_contrat_declare_explicitement(exclusions):
    job = _job(description="Type de contrat : CDI\nLieu : Paris")
    assert exclusions.motif(job).startswith("contrat cdi")


def test_un_cdi_a_l_issue_de_l_alternance_reste(exclusions):
    """C'est un argument de vente, pas un type de contrat — et il décrit
    justement ce qu'on veut garder."""
    job = _job(description="À l'issue du contrat, un CDI vous sera proposé.")
    assert exclusions.motif(job) == ""


def test_une_liste_de_choix_n_est_pas_une_declaration(exclusions):
    """Le menu déroulant d'un formulaire d'alerte. Cette confusion avait fait
    exclure les 363 offres HelloWork d'un coup."""
    job = _job(description="Type de contrat : CDI CDD Intérim Stage Alternance")
    assert exclusions.motif(job) == ""


# --- courtage scolaire : l'unique dérogation -----------------------------

def test_deux_marqueurs_simultanes_declenchent(exclusions):
    job = _job(company="AURLOM",
               description="Le CFA AURLOM recrute pour l'une de ses "
                           "1 500 entreprises partenaires un alternant.")
    assert exclusions.motif(job) == "ecoles (courtage)"


@pytest.mark.parametrize("description", [
    "Notre école interne forme vos futurs collègues.",
    "Nous travaillons avec de nombreuses entreprises partenaires.",
])
def test_un_marqueur_isole_ne_declenche_rien(exclusions, description):
    assert exclusions.motif(_job(company="Orange", description=description)) == ""


def test_le_courtage_ne_se_lit_que_dans_l_en_tete(exclusions):
    """Au-delà de 1 500 caractères on lit le poste, pas l'employeur — et le
    bas de page charrie mentions légales et listes de partenaires."""
    job = _job(company="Orange",
               description="x" * 1600 + " notre CFA et ses entreprises partenaires")
    assert exclusions.motif(job) == ""


# --- NAF -----------------------------------------------------------------

def test_naf_exclu_par_prefixe(exclusions):
    """« 64 » couvre tout 64.xx."""
    job = _job(company="Une société", description="Secteur : Activités des "
                                                  "sociétés holding (6420Z).")
    assert exclusions.motif(job) == "NAF 6420Z"


def test_naf_hors_liste_conserve(exclusions):
    job = _job(company="Une société",
               description="Secteur : Programmation informatique (6201Z).")
    assert exclusions.motif(job) == ""


# --- appliquer -----------------------------------------------------------

def test_appliquer_stocke_le_motif_sans_supprimer(exclusions):
    """Le motif est stocké, la ligne jamais supprimée : retirer un secteur de
    config.yaml et relancer `rescore` les fait toutes revenir."""
    job = exclusions.appliquer(_job(company="BNP Paribas"))
    assert job.exclu == "banque"
    assert job.title == "Alternance Développeur"


def test_config_vide_n_exclut_rien():
    vides = Exclusions({})
    assert vides.actif is False
    assert vides.motif(_job(company="BNP Paribas")) == ""


# --- stage sans alternance -----------------------------------------------

@pytest.mark.parametrize("titre", [
    "Stage - Développeur Java",
    "Stagiaire développeur Python",
    "Internship Software Engineer",
    "Développeuse / Développeur DevOps - STAGE",
])
def test_un_stage_pur_est_ecarte(exclusions, titre):
    """29 offres de la base portaient « Stage » sans mention d'alternance, et
    occupaient les QUATRE premières places du digest (103, 97, 78, 70)."""
    assert exclusions.motif(_job(titre=titre)) == "stage (sans alternance)"


@pytest.mark.parametrize("titre", [
    "Stage / Alternance Développeur",
    "Stage ou alternance - Dev Web",
    "Développeur en apprentissage - stage possible",
    "Alternance Développeur",
])
def test_une_offre_double_est_conservee(exclusions, titre):
    """33 offres annoncent les deux : ce sont de vraies doubles opportunités."""
    assert exclusions.motif(_job(titre=titre)) == ""


def test_backstage_n_est_pas_un_stage(exclusions):
    """Frontières de mot : même famille de faux positif qu'« automobile »
    contenant « mobile »."""
    assert exclusions.motif(_job(titre="Développeur Backstage H/F")) == ""


def test_le_contract_type_de_la_source_ne_sauve_pas_un_stage(exclusions):
    """Indeed recopie dans `contract_type` le menu de filtres coché par
    l'employeur — « stage apprentissage contrat d'apprentissage » — et c'est
    par là que les 29 passaient. L'intitulé, lui, dit ce qui est recruté."""
    job = _job(titre="Stage - Développeur Java",
               description="De nombreuses opportunités en alternance ou en "
                           "CDI peuvent vous attendre à l'issue de ce stage.")
    job.contract_type = "Stage, Apprentissage, Contrat d'apprentissage"
    assert exclusions.motif(job) == "stage (sans alternance)"


def test_la_regle_stage_est_desactivable():
    """Réversible comme toute exclusion : un réglage, puis `rescore`."""
    sans = Exclusions({"exclusions": {
        "contrats": {"exclus": ["cdi"], "stage_sans_alternance": False}}})
    assert sans.motif(_job(titre="Stage - Développeur Java")) == ""


def test_la_regle_stage_ne_reveille_pas_un_filtre_eteint():
    """Sans configuration d'exclusions, on n'exclut rien — pas même un stage.
    Même contrat que `_CONTRAT_DECLARE`, l'autre règle codée en dur."""
    assert Exclusions({}).motif(_job(titre="Stage - Développeur Java")) == ""
