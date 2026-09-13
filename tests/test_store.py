"""Persistance et suivi — ce qu'une régression ferait perdre pour de bon.

Le suivi de candidature est la vraie valeur de l'outil : un `upsert` qui
écrase `status` détruit un travail que rien ne peut reconstituer.
"""

from datetime import date, datetime, timedelta

import pytest

from core.models import Job
from core.store import Store, _sans_suivi


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _job(uid_ext="1", source="test", titre="Alternance Développeur Cloud",
         company="Orange", location="Paris", url="https://ex.fr/1",
         **kw) -> Job:
    base = dict(source=source, external_id=uid_ext, title=titre,
                company=company, location=location, url=url,
                is_alternance=True, score=50)
    return Job(**{**base, **kw})


# --- upsert --------------------------------------------------------------

def test_upsert_signale_la_nouveaute(store):
    assert store.upsert(_job()) is True
    assert store.upsert(_job()) is False


def test_upsert_preserve_le_suivi(store):
    """Le test qui compte : un run ne doit JAMAIS écraser une candidature."""
    store.upsert(_job())
    store.set_status("test:1", "applied", notes="relancé le 12/09")

    store.upsert(_job(score=99))          # l'offre est revue, re-scorée

    ligne = store.db.execute(
        "SELECT status, notes, score FROM jobs WHERE uid='test:1'").fetchone()
    assert ligne["status"] == "applied"
    assert ligne["notes"] == "relancé le 12/09"
    assert ligne["score"] == 99           # les champs calculés, eux, suivent


def test_upsert_rafraichit_last_seen(store):
    store.upsert(_job())
    avant = store.db.execute(
        "SELECT first_seen, last_seen FROM jobs WHERE uid='test:1'").fetchone()
    store.upsert(_job())
    apres = store.db.execute(
        "SELECT first_seen, last_seen FROM jobs WHERE uid='test:1'").fetchone()
    assert apres["first_seen"] == avant["first_seen"]


# --- statuts -------------------------------------------------------------

def test_set_status_sur_uid_inconnu_retourne_false(store):
    """Sans ça, la case du digest resterait cochée alors que rien n'est
    enregistré."""
    assert store.set_status("test:inexistant", "applied") is False


def test_set_status_accepte_les_statuts_connus(store):
    store.upsert(_job())
    for statut in ("seen", "shortlisted", "applied", "rejected"):
        assert store.set_status("test:1", statut) is True


# --- dédoublonnage -------------------------------------------------------

def test_doublon_inter_sources_marque(store):
    """La même offre AXA vue sur LinkedIn et HelloWork."""
    store.upsert(_job(source="linkedin", uid_ext="a",
                      titre="Data scientist (F/H) - alternance", company="AXA",
                      location="Nanterre, Île-de-France, France"))
    store.upsert(_job(source="hellowork", uid_ext="b",
                      titre="Data Scientist - Alternance H/F", company="AXA",
                      location="Nanterre - 92"))
    assert store.recalculer_doublons() == 1

    retenues = store.db.execute(
        "SELECT COUNT(*) n FROM jobs WHERE duplicate_of IS NULL").fetchone()["n"]
    assert retenues == 1


def test_deux_offres_distinctes_ne_sont_pas_dedoublonnees(store):
    store.upsert(_job(uid_ext="a", titre="Alternance Développeur Cloud"))
    store.upsert(_job(uid_ext="b", titre="Alternance Développeur Mobile"))
    assert store.recalculer_doublons() == 0


# --- sélection -----------------------------------------------------------

def test_selection_ecarte_exclus_doublons_et_non_alternances(store):
    store.upsert(_job(uid_ext="ok"))
    store.upsert(_job(uid_ext="exclu", titre="Alternance Dev Banque",
                      exclu="banque"))
    store.upsert(_job(uid_ext="pas_alt", titre="Développeur Cloud CDI",
                      is_alternance=False))

    uids = {r["uid"] for r in store.selection()}
    assert uids == {"test:ok"}


def test_selection_respecte_le_score_min(store):
    store.upsert(_job(uid_ext="fort", score=80))
    store.upsert(_job(uid_ext="faible", titre="Alternance Dev Web", score=10))
    uids = {r["uid"] for r in store.selection(score_min=50)}
    assert uids == {"test:fort"}


def test_selection_par_age_ecarte_les_offres_sans_date(store):
    """Vérification stricte : sans date, on ne peut justement PAS vérifier."""
    store.upsert(_job(uid_ext="datee", posted_at=date.today()))
    store.upsert(_job(uid_ext="sans_date", titre="Alternance Dev Web"))
    uids = {r["uid"] for r in store.selection(age_max_jours=7)}
    assert uids == {"test:datee"}


def test_selection_a_l_heure_pres_quand_la_source_le_permet(store):
    """PASS, WTTJ, Indeed et JobTeaser publient un horodatage complet : pour
    eux « 24 h » veut vraiment dire 24 h."""
    hier = datetime.now() - timedelta(hours=30)
    store.upsert(_job(uid_ext="recent", posted_at=date.today(),
                      posted_ts=datetime.now() - timedelta(hours=2)))
    store.upsert(_job(uid_ext="vieux", titre="Alternance Dev Web",
                      posted_at=hier.date(), posted_ts=hier))
    uids = {r["uid"] for r in store.selection(age_max_heures=24)}
    assert uids == {"test:recent"}


# --- ids_connus / backlog ------------------------------------------------

def test_ids_connus_par_source(store):
    store.upsert(_job(source="linkedin", uid_ext="a"))
    store.upsert(_job(source="hellowork", uid_ext="b"))
    assert store.ids_connus("linkedin") == {"a"}


def test_sans_description_alimente_le_backlog(store):
    """Le critère est un SEUIL de longueur, pas « vide » : une description
    tronquee de deux lignes ne vaut pas mieux que pas de description."""
    store.upsert(_job(uid_ext="vide", description=""))
    store.upsert(_job(uid_ext="troncon", titre="Alternance Dev Web",
                      description="Trop court pour scorer."))
    store.upsert(_job(uid_ext="pleine", titre="Alternance Dev Data",
                      description="x" * (Store.SEUIL_DESCRIPTION + 1)))
    manquantes = {j.external_id for j in store.sans_description("test", 10)}
    assert manquantes == {"vide", "troncon"}


def test_le_backlog_abandonne_apres_trois_echecs(store):
    """Offre expirée : elle sort de la file plutôt que d'être reprise sans
    fin à chaque run."""
    store.upsert(_job(uid_ext="perdue", description=""))
    store.db.execute("UPDATE jobs SET detail_essais = 3 WHERE uid = 'test:perdue'")
    assert store.sans_description("test", 10) == []


# --- normalisation d'URL -------------------------------------------------

def test_sans_suivi_retire_les_parametres_utm():
    a = _sans_suivi("https://ex.fr/offre?utm_source=mail&id=42")
    b = _sans_suivi("https://ex.fr/offre?id=42")
    assert a == b


def test_sans_suivi_ne_touche_jamais_a_l_identite_de_l_offre():
    """`jk` (Indeed) est justement ce qui distingue une offre d'une autre."""
    a = _sans_suivi("https://indeed.fr/viewjob?jk=aaa")
    b = _sans_suivi("https://indeed.fr/viewjob?jk=bbb")
    assert a != b


# --- migration -----------------------------------------------------------

def test_une_base_existante_se_migre_sans_perte(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` n'ajoute pas les colonnes manquantes."""
    chemin = tmp_path / "ancienne.db"
    premier = Store(chemin)
    premier.upsert(_job())
    premier.set_status("test:1", "applied")
    premier.close()

    second = Store(chemin)                       # rouvre, remigre
    ligne = second.db.execute(
        "SELECT status FROM jobs WHERE uid='test:1'").fetchone()
    assert ligne["status"] == "applied"
    second.close()


# --- complétion différée -------------------------------------------------

def test_maj_description_persiste_contacts_et_exclusion(store):
    """Une fiche récupérée en second temps peut TOUT révéler : l'adresse de
    candidature comme le fait que le poste soit un CDI. Ne réécrire que le
    texte laissait la base incohérente jusqu'au prochain `rescore`."""
    store.upsert(_job(uid_ext="tardive", description=""))

    complet = _job(uid_ext="tardive",
                   description="Type de contrat : CDI. Écrire à rh@boite.fr")
    complet.contacts = ["rh@boite.fr"]
    complet.exclu = "contrat cdi"
    store.maj_description(complet)

    ligne = store.db.execute(
        "SELECT contacts, exclu, detail_essais FROM jobs WHERE uid='test:tardive'"
    ).fetchone()
    assert ligne["exclu"] == "contrat cdi"
    assert "rh@boite.fr" in ligne["contacts"]
    assert ligne["detail_essais"] == 1


def test_le_backlog_rend_les_contacts_deja_connus(store):
    """Sinon `maj_description` écraserait une adresse acquise ailleurs."""
    job = _job(uid_ext="avec_contact", description="")
    job.contacts = ["deja@connu.fr"]
    store.upsert(job)

    repris = store.sans_description("test", 10)
    assert repris[0].contacts == ["deja@connu.fr"]


def test_rescore_recalcule_aussi_les_contacts(store):
    """`contacts` est dérivé de la description, comme le score : sans ça,
    améliorer l'extraction n'aurait aucun effet sur la base existante."""
    store.upsert(_job(uid_ext="c", description="Écrire à rh@boite.fr"))

    recalcule = _job(uid_ext="c", description="Écrire à rh@boite.fr")
    recalcule.contacts = ["rh@boite.fr"]
    store.maj_score(recalcule)

    ligne = store.db.execute(
        "SELECT contacts FROM jobs WHERE uid='test:c'").fetchone()
    assert "rh@boite.fr" in ligne["contacts"]
