"""Extraction des adresses de candidature.

Le canal au meilleur retour du projet, et celui où une erreur se voit : le
digest affiche un bouton `mailto:` par adresse trouvée. Une adresse fausse
n'est pas un détail cosmétique, c'est une candidature perdue.
"""

import pytest

from core.contacts import extraire_emails


# --- cas nominal ---------------------------------------------------------

@pytest.mark.parametrize("texte, attendu", [
    ("Envoyez votre CV à recrutement@itrust.fr", ["recrutement@itrust.fr"]),
    ("Contact : carrieres@valtus.fr merci", ["carrieres@valtus.fr"]),
    ("CV à Marie.Dupont@ma-boite.com", ["marie.dupont@ma-boite.com"]),
    ("rh@sous.domaine.fr", ["rh@sous.domaine.fr"]),
])
def test_adresse_en_clair(texte, attendu):
    assert extraire_emails(texte) == attendu


@pytest.mark.parametrize("texte", [
    "contact [at] boite [dot] fr",
    "contact (at) boite (dot) fr",
    "contact at boite dot fr",
])
def test_desobfuscation(texte):
    """Les recruteurs obfusquent pour échapper aux robots ; le sens reste."""
    assert extraire_emails(texte) == ["contact@boite.fr"]


def test_doublons_fusionnes_et_tries():
    texte = "Ecrire à Zoe@x.fr ou zoe@x.fr, sinon alpha@x.fr"
    assert extraire_emails(texte) == ["alpha@x.fr", "zoe@x.fr"]


# --- la fin de phrase ne fait pas partie du domaine ----------------------

@pytest.mark.parametrize("texte, attendu", [
    ("Écrivez à rejoindre@monext.net. Dans notre équipe, vous serez...",
     "rejoindre@monext.net"),
    ("Merci d'écrire à rh@boite.fr. Nous reviendrons vers vous.",
     "rh@boite.fr"),
    ("adresse : jobs@societe.com.\nDans un second temps...", "jobs@societe.com"),
])
def test_le_point_final_ne_colle_pas_la_phrase_suivante(texte, attendu):
    """Le bug qui produisait « rejoindremonext@monext.net.dans » : le motif
    tolérait des espaces autour du point du domaine, et le nettoyage final
    les supprimait — collant le premier mot de la phrase suivante au TLD."""
    assert extraire_emails(texte) == [attendu]


# --- ce qu'on refuse d'écrire --------------------------------------------

@pytest.mark.parametrize("texte", [
    "Vos droits RGPD : dpo@grandgroupe.com",
    "Contact DPO : rgpd@boite.fr",
    "Ceci est un envoi automatique, noreply@boite.fr",
    "no-reply@boite.fr",
    "postmaster@boite.fr",
])
def test_les_boites_de_service_sont_ignorees(texte):
    """Un bouton `mailto:` vers le DPO n'est pas une candidature — et il est
    pire que rien, puisqu'il a l'air d'en être une."""
    assert extraire_emails(texte) == []


@pytest.mark.parametrize("texte", [
    "Format attendu : prenom.nom@example.com",
    "Envoyez à nom@votresociete.fr",
    "email@domaine.com",
])
def test_les_gabarits_sont_ignores(texte):
    assert extraire_emails(texte) == []


def test_le_vrai_contact_survit_a_cote_d_une_boite_de_service():
    """Cas fréquent en bas d'annonce : la mention RGPD suit l'adresse RH."""
    texte = ("Candidatures : recrutement@boite.fr — pour vos droits, "
             "écrivez à dpo@boite.fr")
    assert extraire_emails(texte) == ["recrutement@boite.fr"]


# --- robustesse ----------------------------------------------------------

@pytest.mark.parametrize("texte", [
    "", None, "aucune adresse ici", "arobase @ toute seule", "a@b",
    "visitez https://boite.fr/carrieres",
])
def test_ne_leve_jamais_et_n_invente_rien(texte):
    assert extraire_emails(texte) == []


def test_reste_compatible_avec_l_import_historique():
    """`collectors.linkedin_posts` réexporte la fonction : deux collecteurs et
    `main.py` l'importent encore par là."""
    from collectors.linkedin_posts import extraire_emails as depuis_posts
    assert depuis_posts is extraire_emails
