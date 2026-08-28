"""Posts LinkedIn trouvés par un MOTEUR DE RECHERCHE, pas par LinkedIn.

Le collecteur `linkedin_posts` interroge la recherche de contenu de LinkedIn.
C'est le seul moyen de balayer tout le réseau, et c'est aussi ce qui se coupe
quand on insiste : quatre collectes en vingt minutes ont suffi, le 13/08/2026,
pour que la recherche rende des pages vides pendant des heures. D'où son
plafond de cinq requêtes par run, qui borne mécaniquement la couverture.

Ce collecteur-ci contourne le problème par l'extérieur, en trois temps :

1. **DuckDuckGo trouve les URL** — c'est lui qui a indexé LinkedIn, pas nous.
   Aucune requête n'atteint le moteur de contenu de LinkedIn, donc rien ne
   peut déclencher la coupure, et **aucune session n'est nécessaire** ;
2. **chaque post est lu hors session**, par un simple client HTTP, exactement
   comme le fait déjà `enrichir()` — l'URL « vanity » (/posts/…) est servie
   aux visiteurs anonymes ;
3. **le tri recruteur/candidat est celui du projet** (`est_offre_recruteur`),
   pas un second jugement maison : un post de candidat est un concurrent.

Trois contraintes constatées à la mesure, toutes structurantes :

- **le mode headless ne marche pas.** DuckDuckGo rend une page vide à un
  navigateur sans interface — 0 résultat contre 10 en mode visible. La
  fenêtre s'ouvre donc réellement, comme pour `linkedin_posts` ;
- **il faut passer par la racine `duckduckgo.com/?q=`.** Leur `robots.txt`
  interdit `/html` et `/lite` — les points d'entrée « légers » habituels —
  mais autorise explicitement `Allow: /?*`. La première tentative, sur
  `/html`, s'est fait servir un CAPTCHA ; la racine, non ;
- **la pagination rapporte gros** : 10 résultats à la première page, puis
  +15 par clic sur « Plus de résultats ». Mesuré : 55 posts pour une seule
  requête en quatre pages.

Ce que ce collecteur ne fait PAS, et qu'il ne faut pas lui demander : de la
fraîcheur. Ce qu'un moteur a indexé est vieux — âge médian 141 jours mesuré,
environ 13 % des posts trouvés ont moins de trois semaines. C'est un canal de
LARGEUR, gratuit en risque et sans plafond ; `linkedin_posts` reste le canal
de fraîcheur, qui voit l'heure qui vient. Les deux se complètent, et comme
ils partagent la clé d'activité, un post vu par les deux n'est compté qu'une
fois.

Les posts sont enregistrés sous la source `linkedin_post`, comme ceux de
l'autre collecteur : c'est le même objet, et l'identifiant d'activité sert de
clé — un post trouvé par les deux voies n'est donc pas compté deux fois.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from urllib.parse import quote, unquote

import httpx

from core.models import Job

from .linkedin_posts import (_LIEN_POST, date_de_activite, est_offre_recruteur,
                             extraire_emails, lire_post, normalize_accents)

log = logging.getLogger("posts-web")

RECHERCHE = "https://duckduckgo.com/?q={q}&kl=fr-fr"

# DuckDuckGo enveloppe certaines cibles dans un paramètre de redirection.
_REDIRECTION = re.compile(r"[?&](?:u|uddg)=([^&]+)")
_SUITE = "#more-results, button[id*='more-results']"

_ENTETE_LECTURE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


class PostsWeb:
    def __init__(self, delai: float = 2.5, pages: int = 3,
                 headless: bool = False):
        self.delai = delai
        self.pages = max(1, pages)
        # Laissé réglable, mais `True` ne rend rien : voir l'en-tête du module.
        self.headless = headless
        self._pw = None
        self.navigateur = None
        self.requetes = 0

    def ouvrir(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # Vrai Chrome, et surtout PAS le profil de `.chrome-profile` : ce
        # collecteur n'a besoin d'aucune session, autant ne pas exposer
        # celle de LinkedIn à un site tiers.
        self.navigateur = self._pw.chromium.launch(channel="chrome",
                                                   headless=self.headless)

    def _cibles(self, page) -> list[str]:
        """Les URL de posts présentes dans la page de résultats."""
        trouvees = []
        for lien in page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)"):
            cible = lien
            enveloppe = _REDIRECTION.search(lien)
            if enveloppe:
                cible = unquote(enveloppe.group(1))
            if "linkedin.com" in cible and "/posts/" in cible:
                trouvees.append(cible.split("?", 1)[0])
        return trouvees

    def chercher(self, requete: str) -> list[str]:
        """URL de posts pour une requête, pagination comprise."""
        page = self.navigateur.new_page(locale="fr-FR")
        vues: list[str] = []
        try:
            self.requetes += 1
            page.goto(RECHERCHE.format(q=quote(requete)),
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(int(self.delai * 1000))
            vues += self._cibles(page)

            for _ in range(self.pages - 1):
                bouton = page.query_selector(_SUITE)
                if not bouton:
                    break
                bouton.click()
                page.wait_for_timeout(int(self.delai * 1000))
                vues += self._cibles(page)
        except Exception as e:
            log.warning("« %s » — recherche interrompue : %s", requete, e)
        finally:
            page.close()

        uniques = list(dict.fromkeys(vues))
        log.info("« %s » → %d posts", requete, len(uniques))
        return uniques

    def lire(self, urls: list[str], age_max_jours: int = 14) -> list[Job]:
        """Lit et trie les posts. Aucune requête ne passe par LinkedIn Search.

        L'âge se lit dans l'URL, pas dans la page : l'identifiant d'activité
        encode l'horodatage de publication. Un post trop vieux est donc écarté
        AVANT d'être téléchargé — c'est gratuit, et ça évite d'aller chercher
        des annonces pourvues depuis des mois.
        """
        jobs: dict[str, Job] = {}
        vieux = candidats = illisibles = 0

        with httpx.Client(follow_redirects=True, timeout=25,
                          headers=_ENTETE_LECTURE) as client:
            for url in urls:
                lien = _LIEN_POST.search(url)
                if not lien:
                    continue
                activite = lien.group(2)
                if activite in jobs:
                    continue

                publie = date_de_activite(activite)
                if publie and (date.today() - publie).days > age_max_jours:
                    vieux += 1
                    continue

                contenu = lire_post(url, client)
                texte = contenu.get("texte", "")
                if not texte:
                    illisibles += 1
                    continue

                if not est_offre_recruteur(normalize_accents(texte)):
                    candidats += 1
                    continue

                premiere = next((l.strip() for l in texte.split("\n") if l.strip()), "")
                auteur = contenu.get("auteur") or lien.group(1)
                jobs[activite] = Job(
                    source="linkedin_post",
                    external_id=activite,
                    title=(premiere or auteur)[:160],
                    company=auteur[:120],
                    location="",
                    url=url,
                    posted_at=publie,
                    description=texte,
                    contacts=extraire_emails(texte),
                    # La première ligne d'un post n'est pas un intitulé de
                    # poste : sans ça, le gate métier de `score.py` s'y
                    # appliquerait au hasard.
                    titre_est_intitule=False,
                )
                time.sleep(self.delai / 2)

        if vieux or candidats or illisibles:
            log.info("écartés : %d hors fenêtre, %d candidats, %d illisibles",
                     vieux, candidats, illisibles)
        return list(jobs.values())

    def fermer(self) -> None:
        for fermeture in (getattr(self.navigateur, "close", None),
                          getattr(self._pw, "stop", None)):
            try:
                if fermeture:
                    fermeture()
            except Exception:
                pass
