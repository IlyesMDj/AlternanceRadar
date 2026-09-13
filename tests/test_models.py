"""Normalisation, dédoublonnage et horodatage — le socle du pipeline.

Chaque cas vient d'une docstring de `core/models.py` : ce sont des pannes
réellement constatées sur des offres réelles, pas des exemples inventés.
"""

from datetime import datetime

import pytest

from core.models import (Job, city_of, compiler_motifs, horodatage, normalize,
                         normalize_intitule)


# --- normalize -----------------------------------------------------------

@pytest.mark.parametrize("brut, attendu", [
    ("Développeur Full-Stack", "developpeur full stack"),
    ("developpeur full stack", "developpeur full stack"),
    ("  ALTERNANCE   Cloud  ", "alternance cloud"),
    ("", ""),
])
def test_normalize_rend_les_variantes_identiques(brut, attendu):
    assert normalize(brut) == attendu


def test_normalize_preserve_les_caracteres_de_techno():
    """« c# », « c++ » et « node.js » perdraient leur identité sans ça."""
    assert normalize("C# et C++ avec Node.js") == "c# et c++ avec node.js"


def test_normalize_tolere_none():
    assert normalize(None) == ""


# --- normalize_intitule --------------------------------------------------

def test_ecriture_inclusive_ne_coupe_plus_les_motifs():
    """« Chargé(e) de Développement » devenait « charge e de developpement » :
    le « e » isolé faisait sauter le motif « charge de »."""
    assert normalize_intitule("Chargé(e) de Développement RH") == \
        "charge de developpement rh"


@pytest.mark.parametrize("titre", [
    "Développeur(se) Python",
    "Développeur·se Python",
    "Développeur(euse) Python",
])
def test_formes_inclusives_variees(titre):
    assert normalize_intitule(titre) == "developpeur python"


# --- compiler_motifs -----------------------------------------------------

def test_frontieres_de_mot_automobile_ne_matche_pas_mobile():
    """Le faux positif nommément cité dans le README."""
    regex = compiler_motifs(["mobile"])
    assert regex.search("developpeur mobile")
    assert not regex.search("secteur automobile")


def test_les_motifs_a_ponctuation_passent_les_frontieres():
    regex = compiler_motifs(["c#", "ci/cd"])
    assert regex.search(normalize("Maîtrise de C# et de la CI/CD"))


def test_pluriel_tolere():
    """« juridique » laissait passer « données juridiqueS »."""
    regex = compiler_motifs(["juridique", "achat"])
    assert regex.search("analyse de donnees juridiques")
    assert regex.search("service achats")


def test_liste_vide_donne_none():
    assert compiler_motifs([]) is None
    assert compiler_motifs(["", "  "]) is None


# --- city_of -------------------------------------------------------------

@pytest.mark.parametrize("brut, attendu", [
    ("Nanterre, Île-de-France, France", "nanterre"),   # forme LinkedIn
    ("Nanterre - 92", "nanterre"),                     # forme HelloWork
    ("75010 Paris", "paris"),                          # forme Indeed
    ("Paris (75010)", "paris"),
    ("Paris", "paris"),                                # forme Glassdoor
    ("Lyon 2e", "lyon"),                               # arrondissement
    ("Lyon", "lyon"),
])
def test_city_of_rapproche_les_formats_de_source(brut, attendu):
    assert city_of(brut) == attendu


@pytest.mark.parametrize("ville", [
    "Saint-Jean-de-Monts",
    "Noyelles-lès-Seclin",
])
def test_city_of_n_ampute_pas_les_villes_composees(ville):
    """Le tiret séparateur est cherché ENTOURÉ D'ESPACES, justement pour ça."""
    assert city_of(ville) == normalize(ville)


def test_code_postal_seul_reste_intact():
    """Sinon toutes les offres sans ville se rapprocheraient entre elles."""
    assert city_of("75010") == "75010"


# --- horodatage ----------------------------------------------------------

def test_iso_avec_fuseau_devient_local_naif():
    quand = horodatage("2026-08-18T13:25:23+02:00")
    assert quand is not None and quand.tzinfo is None


def test_date_nue_refusee_faute_d_heure():
    """La lire comme minuit inventerait une précision qu'on n'a pas — et
    minuit est le pire cas : l'offre paraîtrait vieille de 24 h de plus."""
    assert horodatage("2026-08-18") is None


def test_epoch_secondes_et_millisecondes():
    secondes = horodatage(1_755_000_000)
    millis = horodatage(1_755_000_000_000)          # forme Indeed
    assert secondes == millis == datetime.fromtimestamp(1_755_000_000)


@pytest.mark.parametrize("valeur", [None, "", "pas une date", "99999999999999999999"])
def test_horodatage_ne_leve_jamais(valeur):
    assert horodatage(valeur) is None


# --- dedup_key -----------------------------------------------------------

def _job(**kw) -> Job:
    base = dict(source="test", external_id="1", title="", company="",
                location="", url="")
    return Job(**{**base, **kw})


def test_meme_offre_deux_sites_meme_cle():
    """L'alternance AXA vue sur deux sites, cas cité dans le README."""
    a = _job(source="linkedin", title="Data scientist (F/H) - alternance",
             company="AXA", location="Nanterre, Île-de-France, France")
    b = _job(source="hellowork", title="Data Scientist - Alternance H/F",
             company="AXA", location="Nanterre - 92")
    assert a.dedup_key == b.dedup_key


def test_offre_explain_vue_sur_indeed_et_glassdoor():
    """Le doublon réel qui a motivé le retrait du code postal."""
    a = _job(source="indeed", title="Alternance Full-Stack IA",
             company="explain", location="75010 Paris")
    b = _job(source="glassdoor", title="Alternance Full-Stack IA",
             company="explain", location="Paris")
    assert a.dedup_key == b.dedup_key


def test_deux_offres_differentes_gardent_des_cles_distinctes():
    a = _job(title="Alternance Développeur Cloud", company="Orange", location="Paris")
    b = _job(title="Alternance Développeur Mobile", company="Orange", location="Paris")
    assert a.dedup_key != b.dedup_key


def test_entreprises_differentes_ne_se_confondent_pas():
    a = _job(title="Alternance Développeur", company="Orange", location="Paris")
    b = _job(title="Alternance Développeur", company="Thales", location="Paris")
    assert a.dedup_key != b.dedup_key


def test_uid_est_intra_source():
    assert _job(source="linkedin", external_id="4268624962").uid == \
        "linkedin:4268624962"
