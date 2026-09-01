"""Collecteur des POSTS du fil LinkedIn (pas les offres du module Jobs).

Pourquoi les posts plutôt que les offres : un recruteur qui publie « on cherche
un alternant dev, CV à contact@boite.fr » donne un contact direct. Pas d'ATS,
pas de formulaire, beaucoup moins de concurrence.

Contrainte incontournable : la recherche de contenu (`/search/results/content/`)
exige une session authentifiée — il n'existe aucun endpoint invité pour le fil,
contrairement aux offres.

Choix de conception pour ne JAMAIS toucher à la session principale :

- profil Chrome **dédié**, dans `.chrome-profile/`, distinct du profil habituel ;
- connexion **manuelle**, une seule fois, par un humain — aucun login automatisé,
  c'est précisément ce que LinkedIn détecte le mieux ;
- vrai Chrome (`channel="chrome"`), pas le Chromium de Playwright ;
- marqueurs d'automatisation neutralisés (`navigator.webdriver`) ;
- navigation en lecture seule : aucun like, aucun follow, aucun clic social ;
- rythme humain, défilement limité, arrêt immédiat sur page de vérification.
"""

from __future__ import annotations

import hashlib
import html
import logging
import random
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from core.models import Job

log = logging.getLogger("li-posts")

RECHERCHE = (
    "https://www.linkedin.com/search/results/content/"
    "?keywords={kw}&datePosted=%22{fenetre}%22&sortBy=%22date_posted%22"
)

# Une adresse e-mail dans un post de recrutement, éventuellement obfusquée
# (« contact [at] boite [dot] fr ») pour échapper aux robots.
_EMAIL = re.compile(
    r"[a-zA-Z0-9._%+-]+\s*(?:@|\(at\)|\[at\]|\s+at\s+)\s*"
    r"[a-zA-Z0-9.-]+\s*(?:\.|\(dot\)|\[dot\]|\s+dot\s+)\s*[a-zA-Z]{2,}"
)

# Dates relatives affichées par LinkedIn, en français comme en anglais.
_AGE = re.compile(r"(\d+)\s*(minutes?|min|heures?|h|jours?|j|semaines?|sem|mois|mo|ans?|yr|w|d|m)\b",
                  re.IGNORECASE)

_CHALLENGE = ("/checkpoint/", "/authwall", "/uas/login", "captcha")

# Sélecteurs du DOM de recherche (refonte « SDUI » de LinkedIn, constatée le
# 13/08/2026). Les anciens `data-urn` / `feed-shared-update-v2` ont disparu ;
# `data-view-name` est le nouveau point d'accroche.
# Le lien « vanity » d'un post, seule forme que LinkedIn sert à un visiteur
# anonyme : /posts/{auteur}_{slug}-activity-{id}-{code}. Le groupe 2 isole
# l'identifiant d'activité, qui encode aussi la date de publication.
#
# `share` et `ugcPost` valent `activity` — mêmes trois formes que l'URN plus
# bas, qui les acceptait déjà. Ne reconnaître qu'`activity` faisait retomber
# ces posts sur l'URL canonique /feed/update/…, que `lire_post()` ne sait
# justement PAS lire hors session : leur texte complet, donc les adresses de
# contact qui en sont l'intérêt principal, était perdu en silence. Vérifié
# sur un post en `-share-` : lisible anonymement, et son identifiant décode
# vers une date plausible exactement comme un `activity`.
# Le groupe 1 s'arrête avant l'underscore, qui sépare le slug de l'AUTEUR de
# celui du contenu : un identifiant public LinkedIn n'en contient jamais.
# Avec `\w` (qui inclut « _ ») il capturait « jade-aubry-495739142_alternance
# -apprentissage-developpement » au lieu du seul auteur.
_LIEN_POST = re.compile(
    r"(?:https://[a-z]{2,3}\.linkedin\.com)?/posts/([a-zA-Z0-9\-%.]+)"
    r"[^\"'\s]*?-(?:activity|share|ugcPost)-(\d{15,25})-[\w_-]+")

# Repli : l'identifiant d'activité nu, sous n'importe quel emballage —
# attribut de suivi, JSON embarqué, menu « copier le lien ».
_URN_ACTIVITE = re.compile(r"urn:li:(?:activity|ugcPost|share):(\d{15,25})")

_CARTE = '[data-view-name="feed-full-update"]'
_AUTEUR = '[data-view-name="feed-actor"]'
_TEXTE = '[data-view-name="feed-commentary"]'
_OFFRE_JOINTE = '[data-view-name="feed-job-card-entity"]'
_DEPLIER = '[data-testid="expandable-text-button"]'

# Âge affiché dans la ligne d'auteur (« 10 h • », « 1 j • », « 2 sem • »).
# Le point médian sert d'ancre : sans lui, « Future L3 MIAGE » ou un nombre
# de commentaires se feraient prendre pour une date.
_AGE_ANCRE = re.compile(r"\b(\d+)\s*(min|h|j|sem|mois|an|w|d|mo|yr)\b\s*•", re.I)

# Un post de CANDIDAT n'a aucun intérêt : c'est un concurrent, pas un employeur.
# La distinction se joue à un mot près — « recherche une ALTERNANCE » (il en
# cherche une) contre « recherche un ALTERNANT » (il en recrute un).
_CANDIDAT = re.compile(
    r"(?:\bje (?:suis|recherche|cherche|souhaite|serai|termine)\b"
    r"|\bmon (?:cv|profil|parcours|fils|alternance)\b"
    r"|\bma (?:candidature|fille|formation)\b"
    r"|\brecherche\s+(?:activement\s+)?(?:une?\s+|d'une?\s+|de\s+)?alternance"
    r"|\ba la recherche d'|\ben recherche d'"
    r"|\bqui recherche\b|\bj'ai besoin de vous\b"
    r"|\bmon reseau\b|\bje partage\b"
    r"|\bdisponible (?:des|a partir)\b|\bjeune diplome\b)",
    re.I,
)
_RECRUTEUR = re.compile(
    r"(?:\b(?:nous|on|je)\s+recrut\w+"
    r"|\brecrut\w+\s+(?:un|une|des|nos|notre|plusieurs)\b"
    r"|\bposte a pourvoir\b|\boffre d'(?:alternance|emploi)\b"
    r"|\bune offre\s*:|\bnos offres\b|\bopportunite d'alternance\b"
    r"|\brejoign\w+|\brejoins(?:-| )nous\b"
    r"|\benvoyez\s+(?:votre|vos|nous)\b|\bpostulez\b|\bcandidature[s]?\s+a\b"
    r"|\bnous recherchons\b|\bnotre client\b)",
    re.I,
)


def est_offre_recruteur(texte: str, offre_jointe: bool = False) -> bool:
    """Vrai si le post PROPOSE un poste, faux s'il en cherche un.

    Trois règles, dans cet ordre :

    1. une offre d'emploi attachée au post tranche seule — aucun candidat
       n'en joint une ;
    2. un marqueur explicite de candidature rejette ;
    3. à défaut, il faut une **preuve** de recrutement. Accepter par défaut,
       comme le faisait la première version, laissait passer tout le bruit du
       fil : citations motivantes, publicités d'agence, posts d'étudiants.

    Réglé sur les onze posts réellement remontés par une recherche : 11/11.
    """
    if offre_jointe:
        return True
    depouille = normalize_accents(texte)
    if _CANDIDAT.search(depouille):
        return False
    return bool(_RECRUTEUR.search(depouille))


# En-tête fixe d'une carte de post, à écarter ligne à ligne. La page d'un
# compte n'expose ni `feed-actor` ni `feed-commentary` — contrairement à la
# recherche — et c'est le seul moyen d'y isoler le contenu.
_ENTETE = re.compile(
    r"^(?:post du fil d.actualite numero \d+"
    r"|[\d\s]+ abonne?e?s?"
    r"|\d+\s*(?:min|h|j|sem|mois|ans?)\s*•?"
    r"|il y a .{0,70}"
    r"|suivre|s.abonner|se connecter|s.identifier.*"
    r"|voir la traduction|…plus|\.\.\.plus|voir plus"
    r"|j.aime|commenter|republier|envoyer|modifie|edited"
    r"|visible de tous.*|•|\W{0,3})$",
    re.I,
)


def _depuis_brut(brut: str) -> tuple[str, str]:
    """Sépare auteur et corps à partir du seul texte de la carte.

    Repli employé quand `feed-commentary` est absent : le contenu est bien
    présent, précédé d'un en-tête d'accessibilité (« Post du fil d'actualité
    numéro 1 »), du nom du compte répété deux fois, du nombre d'abonnés, de
    l'âge et d'une mention de visibilité.
    """
    lignes = [re.sub(r"[  ]", " ", l).strip()
              for l in (brut or "").split("\n")]
    lignes = [l for l in lignes if l]

    auteur, corps, precedente = "", [], None
    for ligne in lignes:
        if _ENTETE.match(normalize_accents(ligne)):
            continue
        if ligne == precedente:      # le nom du compte apparaît deux fois
            continue
        precedente = ligne
        if not auteur:
            auteur = ligne
            continue
        corps.append(ligne)
    return auteur, "\n".join(corps)


def normalize_accents(texte: str) -> str:
    """Minuscules sans accents, ponctuation et espaces conservés.

    Les apostrophes typographiques sont ramenées à l'apostrophe droite :
    LinkedIn écrit « recherche d’une alternance » avec U+2019, sur lequel un
    motif contenant `d'une` ne matcherait jamais.
    """
    decompose = unicodedata.normalize("NFKD", texte or "")
    sans_accents = "".join(c for c in decompose if not unicodedata.combining(c))
    for typographique in ("’", "‘", "ʼ", "`"):
        sans_accents = sans_accents.replace(typographique, "'")
    return sans_accents.lower()

# En-têtes du client non authentifié servant à lire les posts hors session.
_HEADERS_LECTURE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


def date_de_activite(activite: str) -> date | None:
    """Date exacte de publication, déduite de l'identifiant d'activité.

    LinkedIn encode l'horodatage de création dans les 41 bits de poids fort de
    l'identifiant : `id >> 22` donne les millisecondes depuis l'epoch. C'est
    exact, gratuit, et infiniment plus fiable que de lire « il y a 2 sem. »
    dans le DOM — seule façon de tenir une vérification stricte à 24 h.
    """
    try:
        ms = int(activite) >> 22
    except (TypeError, ValueError):
        return None
    if not (1_000_000_000_000 < ms < 4_000_000_000_000):
        return None  # hors plage plausible : l'identifiant n'est pas une activité
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def lire_post(url: str, client: httpx.Client) -> dict:
    """Lit un post depuis son URL publique, SANS authentification.

    Seule l'URL « vanity » (/posts/auteur_hashtags-share-ID-code) sert le
    contenu à un visiteur anonyme : l'URL canonique
    /feed/update/urn:li:activity:ID ne renvoie que la page marketing de
    LinkedIn. D'où l'architecture en deux temps — la session ne sert qu'à
    récolter les URL, la lecture se fait hors session.
    """
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        log.debug("lecture impossible (%s) : %s", e, url)
        return {}
    if r.status_code != 200:
        return {}

    soup = BeautifulSoup(r.text, "lxml")

    texte = ""
    bloc = soup.select_one(
        "div.attributed-text-segment-list__container, "
        "p.attributed-text-segment-list__content, "
        "div.feed-shared-update-v2__description"
    )
    if bloc:
        texte = bloc.get_text(" ", strip=True)
    if len(texte) < 80:
        # Repli : og:description, tronqué mais toujours présent.
        og = soup.find("meta", property="og:description")
        texte = (og.get("content", "") if og else "") or texte

    auteur = ""
    og_titre = soup.find("meta", property="og:title")
    if og_titre:
        # « #hashtags… | Prénom Nom | 10 commentaires »
        morceaux = [m.strip() for m in og_titre.get("content", "").split("|")]
        candidats = [m for m in morceaux
                     if m and not m.startswith("#") and "commentaire" not in m.lower()]
        auteur = candidats[0] if candidats else ""

    return {"texte": html.unescape(texte), "auteur": auteur}


def _unite_en_jours(n: int, unite: str) -> int:
    unite = unite.lower()
    if unite.startswith(("minute", "min", "heure")) or unite == "h":
        return 0
    if unite.startswith("jour") or unite in ("j", "d"):
        return n
    if unite.startswith(("semaine", "sem")) or unite == "w":
        return n * 7
    if unite.startswith("mois") or unite in ("mo", "m"):
        return n * 30
    return n * 365


def _age_en_jours(texte: str) -> int | None:
    """Convertit « 2 sem. », « 3 j », « 1w » en nombre de jours.

    Le motif ancré au point médian est essayé d'abord : sans lui, « Future L3
    MIAGE » dans l'accroche d'un auteur, ou un nombre de commentaires, se
    ferait prendre pour une date de publication.
    """
    m = _AGE_ANCRE.search(texte or "") or _AGE.search(texte or "")
    return _unite_en_jours(int(m.group(1)), m.group(2)) if m else None


def extraire_emails(texte: str) -> list[str]:
    """Extrait et désobfusque les adresses e-mail d'un post."""
    trouves = []
    for brut in _EMAIL.findall(texte or ""):
        adresse = re.sub(r"\s*(?:\(at\)|\[at\]|\s+at\s+)\s*", "@", brut, flags=re.I)
        adresse = re.sub(r"\s*(?:\(dot\)|\[dot\]|\s+dot\s+)\s*", ".", adresse, flags=re.I)
        adresse = adresse.replace(" ", "").strip(".,;:")
        if "@" in adresse and "." in adresse.split("@")[-1]:
            trouves.append(adresse.lower())
    return sorted(set(trouves))


class LinkedInPosts:
    def __init__(self, profil: Path, delai: float = 4.0, headless: bool = False):
        self.profil = profil
        self.delai = delai
        self.headless = headless
        self.ctx = None
        self.page = None

    # -- session ----------------------------------------------------------

    def ouvrir(self) -> None:
        from playwright.sync_api import sync_playwright

        self.profil.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profil),
            channel="chrome",           # vrai Chrome, pas le Chromium embarqué
            headless=self.headless,
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                # Sans ces trois drapeaux, réduire la fenêtre ou la recouvrir
                # suffit à casser la collecte : Chrome met en veille les pages
                # cachées (minuteurs ralentis, rendu suspendu) et le fil
                # LinkedIn charge ses posts au défilement — qui ne déclenche
                # alors plus rien. Avec eux, la fenêtre peut rester en fond.
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )
        # `navigator.webdriver` est le marqueur d'automatisation le plus lu.
        self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def _cookie_session(self) -> bool:
        """Présence du cookie `li_at`, marqueur de session LinkedIn.

        Bien plus fiable qu'un test sur l'URL : selon le parcours suivi, une
        connexion réussie peut aboutir sur /feed, sur une page de vérification
        en deux étapes, ou dans un autre onglet — l'URL de la page pilotée ne
        dit alors rien. Le cookie, lui, est posé dans tous les cas.
        """
        try:
            return any(c.get("name") == "li_at" for c in self.ctx.cookies())
        except Exception:
            return False

    def connecte(self) -> bool:
        """Vérifie que la session est active, sans jamais tenter de login."""
        if self._cookie_session():
            self.page.goto("https://www.linkedin.com/feed/",
                           wait_until="domcontentloaded")
            self._pause()
            return not any(m in self.page.url for m in _CHALLENGE)

        self.page.goto("https://www.linkedin.com/login",
                       wait_until="domcontentloaded")
        return False

    def attendre_connexion(self, delai_max: int = 900) -> bool:
        """Laisse l'utilisateur se connecter à la main, puis reprend.

        Aucun identifiant n'est saisi par le programme : c'est un login humain
        dans un profil persistant, indiscernable d'un usage normal.

        L'attente est volontairement longue. Une double authentification par
        SMS, un mot de passe à retrouver, et cinq minutes sont dépassées — or
        abandonner à cet instant gâche tout le run alors que la session vient
        justement d'être établie.
        """
        print("\n  Connecte-toi à LinkedIn dans la fenêtre Chrome ouverte.")
        print(f"  J'attends jusqu'à {delai_max // 60} minutes et je reprends seul.\n")
        debut = time.monotonic()
        dernier_point = 0.0
        while time.monotonic() - debut < delai_max:
            time.sleep(3)
            if self._cookie_session():
                print("  Session détectée. Elle sera réutilisée aux prochains runs.\n")
                self.page.goto("https://www.linkedin.com/feed/",
                               wait_until="domcontentloaded")
                self._pause()
                return True
            ecoule = time.monotonic() - debut
            if ecoule - dernier_point >= 60:
                dernier_point = ecoule
                log.info("toujours en attente de connexion (%d min)", int(ecoule // 60))
        return False

    # -- collecte ---------------------------------------------------------

    def _pause(self) -> None:
        time.sleep(self.delai + random.uniform(0, 2.5))

    def _deplier(self) -> None:
        """Clique les « …voir plus » : le texte complet contient les contacts."""
        boutons = self.page.locator(_DEPLIER).all()[:30]
        if not boutons:  # repli si l'attribut de test disparaît à son tour
            boutons = [b for libelle in ("voir plus", "see more")
                       for b in self.page.locator(f'button:has-text("{libelle}")').all()[:20]]
        for bouton in boutons:
            try:
                bouton.click(timeout=1500)
                time.sleep(random.uniform(0.2, 0.6))
            except Exception:
                pass  # bouton disparu ou masqué : sans importance

    def rechercher(self, mots_cles: str, fenetre: str = "past-month",
                   defilements: int = 6, age_max_jours: int = 14) -> list[Job]:
        url = RECHERCHE.format(kw=quote(mots_cles, safe=""), fenetre=fenetre)
        return self._moissonner(url, f"« {mots_cles} »", defilements, age_max_jours)

    def compte(self, chemin: str, defilements: int = 4,
               age_max_jours: int = 14) -> list[Job]:
        """Relève les publications récentes d'un compte suivi.

        Beaucoup moins risqué qu'une recherche : une page consultée, aucune
        requête au moteur de contenu — or c'est l'accumulation de recherches
        qui déclenche la coupure. Certains comptes publient une offre
        d'alternance par jour ouvré, ce qui en fait un filon à eux seuls.
        """
        chemin = chemin.strip("/")
        url = (f"https://www.linkedin.com/{chemin}/posts/"
               if chemin.startswith("company/")
               else f"https://www.linkedin.com/{chemin}/recent-activity/all/")
        return self._moissonner(url, f"compte {chemin}", defilements, age_max_jours)

    def _pourquoi_rien(self, etiquette: str) -> None:
        """Relève ce qui distingue les causes d'une page sans carte.

        Trois causes possibles, trois remèdes opposés :

        - **session expirée** — LinkedIn sert un mur de connexion. Remède :
          `main.py login`.
        - **conteneur renommé** — le contenu est là mais sous un autre
          `data-view-name`. Remède : changer `_CARTE`. On compte donc les
          candidats plausibles pour voir lequel a pris la relève.
        - **zéro résultat** — la recherche ne rend rien pour ces mots-clés.
          Remède : d'autres mots-clés. Rien n'est cassé.

        Sans ce relevé, les trois se ressemblent dans le journal.
        """
        try:
            url, titre = self.page.url, (self.page.title() or "")[:70]
        except Exception:
            return
        log.warning("  diagnostic %s : url=%s", etiquette, url[:110])
        log.warning("  titre de page : %s", titre)

        mur = 0
        for selecteur in ("input[name=session_key]", "form.login__form",
                          "a[href*='/login']", "[data-id='sign-in-form']"):
            try:
                mur += self.page.locator(selecteur).count()
            except Exception:
                pass
        if mur:
            log.error("  -> mur de connexion détecté : relance `main.py login`")
            return

        # Quel conteneur porte le contenu, si ce n'est plus le nôtre ?
        candidats = {
            "feed-full-update (attendu)": _CARTE,
            "search-result": '[data-view-name="search-result"]',
            "search-content-update": '[data-view-name="search-content-update"]',
            "occludable-update": "div.occludable-update",
            "article role": "[role=article]",
            "liens de post": "a[href*='-activity-']",
        }
        trouves = []
        for nom, selecteur in candidats.items():
            try:
                n = self.page.locator(selecteur).count()
            except Exception:
                n = 0
            if n:
                trouves.append(f"{nom}={n}")
        if trouves:
            log.error("  -> conteneurs présents : %s", ", ".join(trouves))
            log.error("     le contenu est là mais sous un autre nom : "
                      "ajuster `_CARTE`")
        else:
            # Signature de la COUPURE, documentée dans config.yaml le
            # 13/08/2026 : la page se rend, la session reste valide, les pages
            # de compte continuent de répondre, mais la recherche de contenu
            # ne renvoie plus rien — sans erreur ni message. C'est la sanction
            # d'un excès de recherches, et elle dure plusieurs heures.
            #
            # Ne PAS conclure « zéro résultat » : « alternance développeur »
            # sur une semaine en a forcément. Confondre les deux conduit à
            # relancer, donc à prolonger la coupure.
            log.error("  -> page rendue, session valide, mais aucun post.")
            log.error("     C'est la signature de la COUPURE de recherche "
                      "(voir config.yaml, garde-fous).")
            log.error("     Remède : attendre plusieurs heures. Les comptes "
                      "suivis, eux, continuent de fonctionner.")

    def _moissonner(self, url: str, etiquette: str, defilements: int,
                    age_max_jours: int) -> list[Job]:
        """Charge une page de fil, défile, et extrait à chaque palier."""
        # (le diagnostic d'absence de carte vit dans `_pourquoi_rien`)
        self.page.goto(url, wait_until="domcontentloaded")

        if any(marqueur in self.page.url for marqueur in _CHALLENGE):
            raise RuntimeError("LinkedIn demande une vérification — arrêt immédiat")

        # Attendre l'apparition réelle des cartes plutôt qu'une durée fixe :
        # le rendu du fil est asynchrone et sa latence varie beaucoup.
        try:
            self.page.wait_for_selector(_CARTE, timeout=20000)
        except Exception:
            # « Aucune carte » ne dit rien de la CAUSE, et les causes
            # demandent des remèdes opposés : une session expirée se règle par
            # `login`, un conteneur renommé par un changement de sélecteur, un
            # zéro résultat par d'autres mots-clés. On relève donc de quoi
            # trancher, plutôt que de laisser deviner au prochain run.
            log.warning("aucune carte après 20 s pour %s", etiquette)
            self._pourquoi_rien(etiquette)
            return []

        # Extraction INCRÉMENTALE, à chaque palier de défilement.
        # LinkedIn virtualise la liste : les posts sortis de l'écran sont
        # démontés du DOM. Défiler puis extraire à la fin ne ramenait rien —
        # le haut de page avait disparu, le bas n'était pas encore monté.
        trouves: dict[str, Job] = {}

        def relever() -> None:
            self._deplier()
            for job in self._extraire(age_max_jours):
                trouves.setdefault(job.external_id, job)

        relever()

        # Le curseur est placé au CENTRE de la fenêtre avant de faire tourner
        # la molette. `mouse.wheel` agit là où le curseur se trouve, et
        # Playwright le laisse en (0, 0) tant qu'on ne l'a pas bougé : depuis
        # le coin supérieur gauche, l'événement peut atterrir sur la barre
        # latérale — qui a son propre défilement — au lieu du fil.
        taille = self.page.viewport_size or {"width": 1280, "height": 800}
        milieu = (taille["width"] // 2, taille["height"] // 2)

        steriles = 0
        paliers = 1
        for _ in range(defilements):
            avant, hauteur = len(trouves), self.page.evaluate("window.scrollY")

            self.page.mouse.move(*milieu)
            # Pas plus d'un écran à la fois, pour ne rien enjamber.
            self.page.mouse.wheel(0, random.randint(500, 800))
            time.sleep(random.uniform(1.4, 2.6))
            relever()
            paliers += 1

            # Rien de neuf ET la page n'a pas bougé : on est au bas de ce que
            # LinkedIn a chargé. On lui laisse une seconde chance — le fil
            # charge la suite en différé, un premier palier stérile ne veut
            # pas dire que c'est fini — puis on s'arrête. Continuer à faire
            # défiler une liste épuisée ne rapporte rien et ressemble
            # précisément à ce qu'un détecteur cherche.
            if len(trouves) == avant and self.page.evaluate("window.scrollY") <= hauteur:
                steriles += 1
                if steriles >= 2:
                    log.info("%s → bas de liste atteint au palier %d",
                             etiquette, paliers)
                    break
                time.sleep(random.uniform(1.5, 2.5))
            else:
                steriles = 0

        log.info("%s → %d posts retenus après %d paliers",
                 etiquette, len(trouves), paliers)
        return list(trouves.values())

    def _extraire(self, age_max_jours: int) -> list[Job]:
        """Extrait les posts du DOM de recherche.

        Refonte du 13/08/2026 : LinkedIn a remplacé `data-urn` /
        `feed-shared-update-v2` par des `data-view-name`, et n'expose plus
        d'URN d'activité. Conséquence directe — plus d'identifiant natif,
        donc plus de date déductible ni d'URL de post. On se rabat sur
        l'empreinte du contenu et sur l'âge affiché.
        """
        cartes = self.page.locator(_CARTE).all()
        log.debug("%d cartes dans le DOM à ce palier", len(cartes))

        posts: dict[str, Job] = {}
        candidats = trop_vieux = vides = 0

        for carte in cartes:
            try:
                brut = (carte.inner_text() or "").strip()
                if len(brut) < 60:
                    continue

                # La recherche expose `feed-commentary` ; la page d'un compte,
                # non. On retombe alors sur l'analyse du texte brut.
                blocs = carte.locator(_TEXTE).all()
                auteur_brut = ""
                if blocs:
                    texte = (blocs[0].inner_text() or "").strip()
                else:
                    auteur_brut, texte = _depuis_brut(brut)

                # Une carte réduite à son en-tête n'a rien à donner : sans ce
                # garde-fou, on enregistrait « Post du fil d'actualité numéro 1 »
                # signé « S'identifier sur LinkedIn ».
                if len(texte) < 60:
                    vides += 1
                    continue

                # Offre d'emploi jointe au post : intitulé, entreprise, lieu.
                # Relevée avant le tri, car sa seule présence prouve qu'on a
                # affaire à un recruteur.
                titre_offre = url_offre = ""
                for jointe in carte.locator(_OFFRE_JOINTE).all()[:1]:
                    titre_offre = (jointe.inner_text() or "").strip()
                    url_offre = (jointe.get_attribute("href") or "").split("?")[0]

                # Un post de candidat est un concurrent, pas une piste.
                if not est_offre_recruteur(texte, offre_jointe=bool(titre_offre)):
                    candidats += 1
                    continue

                age = _age_en_jours(brut)
                if age is not None and age > age_max_jours:
                    trop_vieux += 1
                    continue
                publie = (date.today() - timedelta(days=age)) if age is not None else None

                auteur, lien_auteur = auteur_brut, ""
                for a in carte.locator(_AUTEUR).all()[:1]:
                    auteur = (a.inner_text() or "").split("•")[0].strip()
                    lien_auteur = (a.get_attribute("href") or "").split("?")[0]

                # Le lien vers LE post, et non vers la page de son auteur.
                #
                # LinkedIn a retiré l'attribut `data-urn` lors de sa refonte,
                # et le chercher par un sélecteur précis reviendrait à parier
                # sur le prochain nom qu'ils choisiront. On ratisse donc tout
                # le HTML de la carte : l'identifiant d'activité y figure
                # forcément quelque part — lien de partage, menu « copier le
                # lien », attribut de suivi — quel que soit son emballage.
                html_carte = ""
                try:
                    html_carte = carte.inner_html() or ""
                except Exception:
                    pass

                # Deux formes d'URL, et elles ne servent pas à la même chose :
                # la « vanity » (/posts/auteur_slug-activity-ID-code) est la
                # SEULE que LinkedIn sert à un visiteur anonyme, donc la seule
                # que `lire_post()` puisse exploiter ; la canonique
                # (/feed/update/urn:li:activity:ID) reste un lien valide pour
                # qui est connecté. On préfère la première, on garde la
                # seconde en repli.
                vanity = _LIEN_POST.search(html_carte)
                urn = _URN_ACTIVITE.search(html_carte)
                activite = vanity.group(2) if vanity else (urn.group(1) if urn else "")

                if vanity:
                    url = vanity.group(0)
                    if url.startswith("/"):
                        url = "https://www.linkedin.com" + url
                elif activite:
                    url = ("https://www.linkedin.com/feed/update/"
                           f"urn:li:activity:{activite}/")
                else:
                    url = url_offre or lien_auteur or self.page.url
                    if url.startswith("/"):
                        url = "https://www.linkedin.com" + url

                # L'identifiant d'activité est stable d'un run à l'autre, là où
                # une empreinte de contenu change dès que l'auteur corrige une
                # faute de frappe. On ne retombe sur l'empreinte que sans URN.
                empreinte = activite or hashlib.sha1(
                    f"{auteur}|{texte[:200]}".encode("utf-8")
                ).hexdigest()[:16]
                if empreinte in posts:
                    continue

                # La date déduite de l'URN est exacte à la milliseconde près,
                # quand le DOM ne dit que « il y a 1 sem. ».
                exacte = date_de_activite(activite) if activite else None
                if exacte:
                    publie = exacte

                premiere = next((l.strip() for l in texte.split("\n") if l.strip()), "")

                posts[empreinte] = Job(
                    source="linkedin_post",
                    external_id=empreinte,
                    title=((titre_offre.split("\n")[0] if titre_offre else premiere)
                           or auteur)[:160],
                    company=auteur[:120],
                    location="",
                    url=url,
                    posted_at=publie,
                    description=texte + (f"\n{titre_offre}" if titre_offre else ""),
                    contacts=extraire_emails(texte),
                    # Vrai uniquement si le titre vient d'une offre jointe :
                    # c'est alors un vrai intitulé de poste, pas la première
                    # phrase d'un message.
                    titre_est_intitule=bool(titre_offre),
                )
            except Exception as e:
                log.debug("carte ignorée : %s", e)

        if candidats or trop_vieux or vides:
            log.info("écartés : %d candidats, %d hors fenêtre, %d sans contenu",
                     candidats, trop_vieux, vides)
        return list(posts.values())

    def enrichir(self, jobs: list[Job], delai: float = 2.0) -> list[Job]:
        """Complète chaque post par lecture publique, HORS session.

        Deux gains. D'abord la qualité : le texte des cartes de résultats est
        tronqué par LinkedIn, la page du post donne le message entier — donc
        les adresses de contact, qui sont presque toujours en fin de message.
        Ensuite la sûreté : cette lecture passe par un simple client HTTP
        anonyme, elle n'occupe pas le navigateur authentifié et n'expose
        donc pas la session.
        """
        lus = 0
        with httpx.Client(headers=_HEADERS_LECTURE, timeout=30.0,
                          follow_redirects=True) as client:
            for job in jobs:
                # Une URL canonique n'est pas lisible par un anonyme : inutile
                # de dépenser une requête pour la page marketing de LinkedIn.
                if "/posts/" not in job.url:
                    continue
                time.sleep(delai + random.uniform(0, 1.0))
                lu = lire_post(job.url, client)
                if not lu.get("texte"):
                    continue
                lus += 1
                if len(lu["texte"]) > len(job.description):
                    job.description = lu["texte"]
                if lu.get("auteur"):
                    job.company = lu["auteur"][:120]
                job.title = (job.description.split("\n")[0] or job.title)[:160]
                job.contacts = sorted(
                    set(job.contacts) | set(extraire_emails(lu["texte"]))
                )
        log.info("%d posts complétés hors session", lus)
        return jobs

    def fermer(self) -> None:
        if self.ctx:
            self.ctx.close()
        if getattr(self, "_pw", None):
            self._pw.stop()
