"""Client HTTP partagé — la politique de refus, pas le réseau.

Aucun test ne sort sur Internet : une suite qui dépend de LinkedIn échoue
les jours où LinkedIn tousse, et devient alors du bruit qu'on apprend à
ignorer. La session est remplacée par un double qui rejoue une séquence de
codes décidée par le test.

`patienter()` est neutralisé partout : sans ça, un backoff exponentiel réel
ferait durer la suite plusieurs minutes pour vérifier une logique qui, elle,
tient en microsecondes.
"""

import pytest

from collectors.http import Blocage, ClientSource


class _Reponse:
    def __init__(self, code, corps=""):
        self.status_code = code
        self.text = corps

    def json(self):
        import json
        return json.loads(self.text)


class _SessionSimulee:
    """Rejoue une séquence de codes, puis répète le dernier indéfiniment."""

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.appels = 0
        self.fermee = False

    def get(self, url, params=None):
        self.appels += 1
        item = (self.sequence.pop(0) if len(self.sequence) > 1
                else self.sequence[0])
        if isinstance(item, Exception):
            raise item
        code, corps = item if isinstance(item, tuple) else (item, "ok")
        return _Reponse(code, corps)

    def close(self):
        self.fermee = True


def _client(sequence, **kw) -> ClientSource:
    c = ClientSource("essai", delai=0.0, jitter=0.0,
                     session=_SessionSimulee(sequence), **kw)
    c.patienter = lambda: None            # pas de sommeil réel dans les tests
    c._pause_de_refus = _sans_sommeil(c)
    return c


def _sans_sommeil(client):
    """Garde la logique du disjoncteur, retire le `time.sleep`."""
    def pause(code, essai):
        client._refus += 1
        if client.refus_max is not None and client._refus >= client.refus_max:
            raise client.blocage(f"{client.nom} refuse ({client._refus})")
    return pause


# --- cas nominal ---------------------------------------------------------

def test_200_rend_le_corps():
    c = _client([(200, "<html>bonjour</html>")])
    assert c.get("https://ex.fr") == "<html>bonjour</html>"
    assert c.requetes == 1


def test_get_json_decode():
    c = _client([(200, '{"jobs": [1, 2]}')])
    assert c.get_json("https://ex.fr") == {"jobs": [1, 2]}


def test_get_json_sur_du_html_rend_none():
    """Une page d'erreur servie en 200 doit se lire comme une absence."""
    c = _client([(200, "<html>oups</html>")])
    assert c.get_json("https://ex.fr") is None


# --- abandon immédiat ----------------------------------------------------

def test_404_abandonne_sans_reessayer():
    """Offre expirée : insister ne la fera pas revenir."""
    c = _client([404], tentatives=4)
    assert c.get("https://ex.fr") is None
    assert c.session.appels == 1


def test_codes_abandon_configurables():
    """LinkedIn ajoute 400 : requête invalide, inutile d'insister non plus."""
    c = _client([400], codes_abandon=(400, 404))
    assert c.get("https://ex.fr") is None
    assert c.session.appels == 1


def test_code_inattendu_abandonne():
    c = _client([500])
    assert c.get("https://ex.fr") is None
    assert c.session.appels == 1


# --- refus et retry ------------------------------------------------------

def test_un_refus_puis_un_succes():
    c = _client([429, (200, "enfin")])
    assert c.get("https://ex.fr") == "enfin"
    assert c.session.appels == 2


def test_tentatives_epuisees_rendent_none():
    c = _client([403], tentatives=3)
    assert c.get("https://ex.fr") is None
    assert c.session.appels == 3


def test_999_de_linkedin_traite_comme_un_refus():
    """999 = code maison LinkedIn pour « requête bloquée »."""
    c = _client([999, (200, "ok")], codes_refus=(403, 429, 999))
    assert c.get("https://ex.fr") == "ok"


def test_erreur_reseau_reessaye():
    c = _client([ConnectionError("silence"), (200, "ok")])
    assert c.get("https://ex.fr") == "ok"


# --- disjoncteur ---------------------------------------------------------

def test_le_disjoncteur_leve_apres_n_refus():
    """S'obstiner face à un refus ne fait que l'aggraver."""
    c = _client([403], tentatives=10, refus_max=3)
    with pytest.raises(Blocage):
        c.get("https://ex.fr")
    assert c.session.appels == 3


def test_sans_refus_max_aucun_disjoncteur():
    """Le comportement par défaut : on épuise les tentatives, sans lever."""
    c = _client([403], tentatives=3)
    assert c.get("https://ex.fr") is None


def test_un_succes_remet_le_compteur_a_zero():
    """Ce qu'on surveille est une série ININTERROMPUE de refus."""
    c = _client([403, (200, "ok")], refus_max=3)
    c.get("https://ex.fr")
    assert c._refus == 0


def test_la_classe_de_blocage_est_specialisable():
    """Chaque source garde son exception, pour que main.py puisse la
    rattraper séparément."""
    class BlocageEssai(Blocage):
        pass

    c = _client([403], tentatives=5, refus_max=2, blocage=BlocageEssai)
    with pytest.raises(BlocageEssai):
        c.get("https://ex.fr")


# --- comptage et fin de vie ----------------------------------------------

def test_le_compteur_de_requetes_compte_les_tentatives():
    """C'est le volume envoyé à la source qui compte, pas les succès."""
    c = _client([403], tentatives=3)
    c.get("https://ex.fr")
    assert c.requetes == 3


def test_close_ferme_la_session():
    c = _client([200])
    c.close()
    assert c.session.fermee is True


def test_utilisable_en_gestionnaire_de_contexte():
    c = _client([200])
    with c:
        c.get("https://ex.fr")
    assert c.session.fermee is True


# --- POST ----------------------------------------------------------------

class _SessionVerbes(_SessionSimulee):
    """Retient le verbe employé, pour vérifier qu'il est bien transmis."""

    def __init__(self, sequence):
        super().__init__(sequence)
        self.verbes = []
        self.kw = []

    def get(self, url, params=None):
        self.verbes.append("get")
        return super().get(url, params)

    def post(self, url, **kw):
        self.verbes.append("post")
        self.kw.append(kw)
        return super().get(url)


def test_post_json_utilise_bien_le_verbe_post():
    """L'index Algolia de Welcome to the Jungle ne répond qu'en POST."""
    c = ClientSource("essai", delai=0.0, jitter=0.0,
                     session=_SessionVerbes([(200, '{"hits": []}')]))
    c.patienter = lambda: None
    assert c.post_json("https://algolia", json={"query": "alternance"}) == {"hits": []}
    assert c.session.verbes == ["post"]
    assert c.session.kw == [{"json": {"query": "alternance"}}]


def test_post_subit_la_meme_politique_de_refus():
    c = ClientSource("essai", delai=0.0, jitter=0.0, tentatives=5, refus_max=3,
                     session=_SessionVerbes([403]))
    c.patienter = lambda: None
    c._pause_de_refus = _sans_sommeil(c)
    with pytest.raises(Blocage):
        c.post_json("https://algolia", json={})
    assert c.session.verbes == ["post"] * 3
