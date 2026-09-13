"""Scoring — surtout les GATES, qui sont ce qui décide du haut du digest.

Le scoring lui-même est une somme pondérée sans surprise. Ce qui mérite un
test, ce sont les trois règles qui n'additionnent pas : le gate métier sur
l'intitulé seul, le gate école sur l'entreprise seule, et le cas des posts de
fil dont le « titre » n'est pas un intitulé.
"""

import pytest

from core.classify import annotate
from core.models import Job
from core.score import Scorer

CONFIG = {
    "scoring": {
        "competences": {
            "cloud": {"poids": 12, "motifs": ["cloud", "aws", "kubernetes"]},
            "mobile": {"poids": 12, "motifs": ["mobile", "flutter", "android"]},
            "python": {"poids": 9, "motifs": ["python", "fastapi"]},
        },
        "bonus": [{"points": 10, "motifs": ["teletravail"], "libelle": "télétravail"}],
        "malus": [{"points": -20, "motifs": ["bac+5 valide"], "libelle": "Bac+5 exigé"}],
        "titre": {
            "cible": {"poids": 20, "motifs": ["developpeur", "ingenieur logiciel"]},
            "hors_cible": {"poids": -30, "motifs": ["charge de", "commercial"]},
        },
        "entreprise": {
            "hors_cible": {"poids": -25, "motifs": ["cfa", "studi", "epsi"]},
        },
    }
}


@pytest.fixture
def scorer():
    return Scorer(CONFIG)


def _job(titre="", description="", company="", titre_est_intitule=True) -> Job:
    return Job(source="test", external_id="1", title=titre, company=company,
               location="Paris", url="", description=description,
               titre_est_intitule=titre_est_intitule)


# --- compétences ---------------------------------------------------------

def test_une_competence_ne_compte_qu_une_fois(scorer):
    """Même citée dix fois : sinon un mot-clé répété gonflerait le score."""
    une = scorer.score(annotate(_job(titre="Alternance Développeur",
                                     description="python")))
    dix = scorer.score(annotate(_job(titre="Alternance Développeur",
                                     description="python " * 10)))
    assert une.score == dix.score
    assert une.tags.count("python") == 1


def test_les_competences_s_additionnent(scorer):
    job = scorer.score(annotate(_job(
        titre="Alternance Développeur",
        description="Cloud AWS et Flutter mobile en Python")))
    assert set(job.tags) >= {"cloud", "mobile", "python"}


# --- gate métier sur l'intitulé seul -------------------------------------

def test_le_gate_metier_ne_lit_que_l_intitule(scorer):
    """Un chargé de marketing qui cite Data, IT et cloud cumulait les poids
    de compétences sans être un poste de développement."""
    job = scorer.score(annotate(_job(
        titre="Alternance Chargé de marketing digital",
        description="Vous travaillerez avec les équipes cloud et python.")))
    assert "⚠ hors cible" in job.tags


def test_l_exclusion_prime_sur_l_inclusion(scorer):
    """« Chargé de projet développement » contient les deux motifs."""
    job = scorer.score(annotate(_job(titre="Chargé de projet développement")))
    assert "⚠ hors cible" in job.tags
    assert "+20 poste technique" not in job.score_detail


def test_intitule_cible_donne_le_bonus(scorer):
    job = scorer.score(annotate(_job(titre="Alternance Développeur Cloud")))
    assert "+20 poste technique" in job.score_detail


def test_ecriture_inclusive_ne_fait_pas_perdre_le_bonus(scorer):
    """La régression corrigée dans `normalize_intitule` : « Développeur(se) »
    devenait « developpeure » et ne matchait plus « developpeur »."""
    for titre in ["Alternance Développeur Cloud",
                  "Alternance Développeur(se) Cloud",
                  "Alternance Développeur(euse) Cloud",
                  "Alternance Développeur·se Cloud"]:
        job = scorer.score(annotate(_job(titre=titre)))
        assert "+20 poste technique" in job.score_detail, titre


def test_gate_metier_saute_sur_les_posts_de_fil(scorer):
    """La première ligne d'un post n'est pas un intitulé : l'exclusion s'y
    déclencherait sur des mots de contexte."""
    job = scorer.score(annotate(_job(
        titre="Chargé de recrutement, je recherche un alternant développeur",
        titre_est_intitule=False)))
    assert "⚠ hors cible" not in job.tags


# --- gate école sur l'entreprise seule -----------------------------------

def test_gate_ecole_sur_le_champ_entreprise(scorer):
    job = scorer.score(annotate(_job(titre="Alternance Développeur",
                                     company="CFA Informatique")))
    assert "⚠ école/CFA" in job.tags


def test_le_mot_formation_dans_le_texte_est_legitime(scorer):
    """Le gate ne porte QUE sur l'entreprise : « formation » dans le corps
    d'une annonce est normal."""
    job = scorer.score(annotate(_job(
        titre="Alternance Développeur", company="Orange",
        description="Un parcours de formation interne vous accompagne.")))
    assert "⚠ école/CFA" not in job.tags


# --- rythme et niveau ----------------------------------------------------

def test_rythme_3_1_confirme_donne_le_bonus(scorer):
    """Régression : ce bonus n'a jamais pu se déclencher, `_RYTHME` exigeant
    un « / » que `normalize()` supprime."""
    job = scorer.score(annotate(_job(
        titre="Alternance Développeur",
        description="Rythme 3 semaines / 1 semaine en entreprise.")))
    assert "rythme 3/1" in job.tags
    assert "+15 rythme 3/1 confirmé" in job.score_detail


def test_rythme_incompatible_penalise(scorer):
    job = scorer.score(annotate(_job(
        titre="Alternance Développeur",
        description="Rythme 2 semaines / 3 semaines.")))
    assert "-15 rythme 2/3 incompatible" in job.score_detail
    assert "⚠ rythme 2/3" in job.tags


def test_un_rythme_journalier_est_penalise(scorer):
    """Même un ratio proche du 3/1 ne passe pas s'il est en jours : le cursus
    est organisé en blocs de trois semaines."""
    job = scorer.score(annotate(_job(
        titre="Alternance Développeur",
        description="Rythme : 4 jours en entreprise / 1 jour à l'école.")))
    assert "-15 rythme 4/1 incompatible" in job.score_detail


def test_aucun_rythme_annonce_ne_change_rien(scorer):
    """Le cas de 95 % des offres : ni bonus ni malus."""
    job = scorer.score(annotate(_job(titre="Alternance Développeur",
                                     description="Poste en Python.")))
    assert "rythme" not in job.score_detail


def test_niveau_m1_donne_le_bonus(scorer):
    job = scorer.score(annotate(_job(titre="Alternance Développeur",
                                     description="Vous préparez un Master 1.")))
    assert "+8 niveau M1" in job.score_detail
    assert "Bac+4 / M1" in job.tags


# --- non-alternance ------------------------------------------------------

def test_une_non_alternance_est_lourdement_declassee(scorer):
    avec = scorer.score(annotate(_job(titre="Alternance Développeur Cloud")))
    sans = scorer.score(annotate(_job(titre="Développeur Cloud")))
    assert avec.score - sans.score == 40
    assert "-40 alternance non confirmée" in sans.score_detail


# --- déterminisme --------------------------------------------------------

def test_le_scoring_est_reproductible(scorer):
    """La promesse centrale du projet : le même corpus donne toujours le même
    classement."""
    faire = lambda: scorer.score(annotate(_job(
        titre="Alternance Développeur Cloud",
        description="AWS, Flutter, Python, télétravail partiel."))).score
    assert len({faire() for _ in range(5)}) == 1
