"""Le garde-fou « n'enregistrer que les alternances ».

Réglage global, dérogeable par source. Ce qui se teste ici n'est pas le
scoring — c'est la décision binaire de créer, ou non, une ligne en base.
"""

import pytest

import main
from core.models import Job


def _job(alternance: bool) -> Job:
    return Job(source="test", external_id="1",
               title="Alternance Développeur" if alternance else "Développeur CDI",
               company="Orange", location="Paris", url="",
               is_alternance=alternance)


# --- résolution du réglage -----------------------------------------------

def test_actif_par_defaut():
    """Aucune configuration : on trie. C'est le comportement voulu, et il ne
    doit pas dépendre de la présence d'une clé."""
    assert main.alternance_seulement({}, "devitjobs") is True


def test_reglage_global_respecte():
    cfg = {"recherche": {"stocker_alternance_seulement": False}}
    assert main.alternance_seulement(cfg, "devitjobs") is False


@pytest.mark.parametrize("global_, source_, attendu", [
    (True, False, False),      # la source déroge pour tout garder
    (False, True, True),       # ...ou pour trier alors que le global ne trie pas
    (True, None, True),        # sans dérogation, le global s'applique
    (False, None, False),
])
def test_la_source_peut_deroger(global_, source_, attendu):
    cfg = {"recherche": {"stocker_alternance_seulement": global_}}
    if source_ is not None:
        cfg["devitjobs"] = {"stocker_alternance_seulement": source_}
    assert main.alternance_seulement(cfg, "devitjobs") is attendu


def test_la_derogation_ne_deborde_pas_sur_les_autres_sources():
    cfg = {"recherche": {"stocker_alternance_seulement": True},
           "devitjobs": {"stocker_alternance_seulement": False}}
    assert main.alternance_seulement(cfg, "devitjobs") is False
    assert main.alternance_seulement(cfg, "linkedin") is True


# --- décision de stockage ------------------------------------------------

def test_une_alternance_est_toujours_gardee():
    for cfg in ({}, {"recherche": {"stocker_alternance_seulement": False}}):
        assert main.garder(_job(True), "devitjobs", cfg) is True


def test_une_non_alternance_est_ecartee_quand_le_tri_est_actif():
    """Le cas DevITjobs : 1 802 lignes stockées pour 109 alternances."""
    assert main.garder(_job(False), "devitjobs", {}) is False


def test_une_non_alternance_est_gardee_si_la_source_deroge():
    cfg = {"devitjobs": {"stocker_alternance_seulement": False}}
    assert main.garder(_job(False), "devitjobs", cfg) is True


# --- cohérence avec la configuration livrée ------------------------------

def test_la_config_du_projet_active_bien_le_tri():
    """Garde-fou contre une désactivation accidentelle : c'est ce réglage qui
    empêche la base de se remplir de CDI."""
    cfg = main.charger_config()
    for source in ("linkedin", "devitjobs", "jobteaser", "glassdoor", "indeed"):
        assert main.alternance_seulement(cfg, source) is True, source
