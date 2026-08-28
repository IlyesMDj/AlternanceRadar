#!/usr/bin/env python
"""alternance-radar — collecte, filtre et classe les offres d'alternance.

    python main.py collect              # run quotidien (fenêtre 24 h)
    python main.py collect --backfill   # premier remplissage (fenêtre 7 jours)
    python main.py report               # génère le digest HTML
    python main.py stats                # état de la base
    python main.py mark <uid> applied   # suivi de candidature
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

# Fenêtres de fraîcheur proposées par `--depuis`, en heures.
FENETRES = {
    "jour": 24,
    "3jours": 72,
    "semaine": 168,
    "2semaines": 336,
}

RACINE = Path(__file__).parent
sys.path.insert(0, str(RACINE))

from collectors.linkedin_guest import LinkedInGuest  # noqa: E402
from core.classify import annotate  # noqa: E402
from core.exclusions import Exclusions  # noqa: E402
from core.models import Job  # noqa: E402
from core.score import Scorer  # noqa: E402
from core.store import STATUTS, Store, sauver_brut  # noqa: E402

log = logging.getLogger("radar")

# Sources dont le site oppose un défi Cloudflare au navigateur piloté. Le
# collecteur les atteint sans peine — `curl_cffi` rejoue une empreinte TLS de
# Chrome et suffit aux contrôles PASSIFS — mais la candidature exige un vrai
# navigateur, et c'est là que le défi actif se déclenche. Constaté sur Indeed.
CLOUDFLARE = {"indeed", "glassdoor"}


def surveiller(store: Store, source: str, jobs: list,
               fenetre_jours: int | None = None) -> None:
    """Contrôle canari d'un lot fraîchement collecté, et trace le résultat.

    Appelé après chaque collecte : c'est ce qui transforme un « 0 offre »
    silencieux en alerte explicite.

    **Toujours sur le lot BRUT, avant tout filtre d'âge.** Ce canari mesure
    l'extraction, pas la fraîcheur : le contrôler après filtrage revenait à
    juger le parseur de La Bonne Alternance sur l'unique offre du jour, et à
    annoncer « parseur cassé » alors qu'il en avait correctement extrait 28.
    """
    from core.canari import COLLECTE, controler, derive, enregistrer

    constat = controler(source, jobs, fenetre_jours)
    enregistrer(store.db, constat, COLLECTE)
    chute = derive(store.db, constat, COLLECTE)

    if constat.indecis:
        # Un volume en retrait se regarde ; un échantillon trop petit, non.
        trace = log.warning if constat.raison == "volume" else log.info
        trace("canari %s : %s", source, constat.message)
    elif not constat.ok:
        log.error("CANARI %s — %s", source, constat.message)
    elif chute:
        log.warning("CANARI %s — %s", source, chute)
    else:
        log.info("canari %s : %s", source, constat.message)


def pipeline(cfg: dict):
    """Retourne la fonction qui prépare un job : classification, score, exclusion.

    Les trois étapes vont toujours ensemble et dans cet ordre — les regrouper
    évite qu'un collecteur en oublie une et laisse passer un secteur exclu.
    """
    scorer, exclusions = Scorer(cfg), Exclusions(cfg)

    def preparer(job: Job) -> Job:
        return exclusions.appliquer(scorer.score(annotate(job)))

    return preparer


def charger_config() -> dict:
    return yaml.safe_load((RACINE / "config.yaml").read_text(encoding="utf-8"))


def _ajouter_depuis(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--depuis", choices=list(FENETRES), default=None,
        help="fenêtre de fraîcheur : jour (24 h), 3jours, semaine, 2semaines. "
             "Par défaut, la valeur de config.yaml.",
    )


def fenetre_de(args, defaut_heures: int) -> tuple[int, int]:
    """Résout la fenêtre demandée. Retourne (heures, jours).

    Le pendant en jours sert aux filtres appliqués après coup, sur la date
    affichée par la source.
    """
    heures = FENETRES[args.depuis] if getattr(args, "depuis", None) else defaut_heures
    return heures, max(1, round(heures / 24))


def fenetre_affichage(args) -> int | None:
    """Fenêtre de fraîcheur du digest, en heures.

    `--depuis` l'emporte sur `--max-age` : il est plus précis (24 h contre
    « 1 jour ») et c'est le même vocabulaire que les commandes de collecte —
    collecter sur 24 h puis afficher deux semaines n'avait aucun sens.
    """
    if getattr(args, "depuis", None):
        return FENETRES[args.depuis]
    jours = getattr(args, "max_age", None)
    return None if jours is None else jours * 24


def filtrer_par_age(jobs: dict, age_max_jours: int, quoi: str = "offres",
                    heures: int | None = None) -> dict:
    """Écarte ce qui dépasse la fenêtre, d'après la date de publication.

    Indispensable : LinkedIn est laxiste sur son propre `f_TPR` et renvoie
    régulièrement des annonces plus anciennes que la fenêtre demandée. Les
    entrées sans date sont conservées — mieux vaut un faux positif qu'une
    bonne offre écartée sur une donnée manquante.

    `heures` active la précision horaire là où la source la fournit. Sans
    elle, « 24 h » signifiait en réalité « hier ou aujourd'hui », soit
    jusqu'à 48 h : une offre datée d'hier peut avoir une heure comme
    quarante-sept. PASS, Welcome to the Jungle, Indeed et JobTeaser publient
    un horodatage complet et sont donc filtrés à l'heure près ; LinkedIn,
    HelloWork et Glassdoor n'ont qu'une date et gardent la comparaison au
    jour, faute de mieux.
    """
    limite = date.today() - timedelta(days=age_max_jours)
    instant = (datetime.now() - timedelta(hours=heures)) if heures else None

    def garde(j) -> bool:
        if instant is not None and j.posted_ts is not None:
            return j.posted_ts >= instant
        return j.posted_at is None or j.posted_at >= limite

    retenus = {k: j for k, j in jobs.items() if garde(j)}
    ecartes = len(jobs) - len(retenus)
    if ecartes:
        borne = (instant.strftime("%d/%m %Hh") if instant else limite)
        log.info("%d %s écartées (publiées avant le %s)", ecartes, quoi, borne)
    return retenus


def plan_requetes(cfg: dict) -> list[tuple[str, str, bool]]:
    """Construit la liste des requêtes (mot-clé, lieu, remote).

    On évite le produit cartésien complet — 15 mots-clés x 11 villes serait
    inutilement long. Stratégie : filet large au national sur tous les
    mots-clés, puis approfondissement par ville sur les mots-clés prioritaires.
    """
    r = cfg["recherche"]
    prioritaires = r.get("mots_cles_villes") or r["mots_cles"][:5]
    plan: list[tuple[str, str, bool]] = []

    for kw in r["mots_cles"]:
        plan.append((kw, "France", False))

    if r.get("inclure_remote"):
        for kw in prioritaires:
            plan.append((kw, "France", True))

    for lieu in r["lieux"]:
        if lieu.strip().lower() == "france":
            continue
        for kw in prioritaires:
            plan.append((kw, lieu, False))

    return plan


def cmd_collect(args, cfg: dict, store: Store) -> None:
    r = cfg["recherche"]
    fenetre, age_max = fenetre_de(args, r.get("fenetre_heures", 336))
    preparer = pipeline(cfg)

    client = LinkedInGuest(
        delai=r.get("delai_entre_requetes", 3.0),
        max_pages=r.get("max_pages_par_requete", 8),
    )

    plan = plan_requetes(cfg)
    if args.limite_requetes:
        plan = plan[: args.limite_requetes]

    log.info("plan : %d requêtes, fenêtre %d h", len(plan), fenetre)

    # 1. Collecte des cartes (titre/entreprise/lieu, pas encore la description)
    cartes: dict[str, Job] = {}
    for i, (kw, lieu, remote) in enumerate(plan, 1):
        etiquette = f"{kw} @ {lieu}{' [remote]' if remote else ''}"
        try:
            lot = client.rechercher(kw, lieu, fenetre_heures=fenetre, remote=remote)
        except Exception as e:  # une requête qui casse ne doit pas tuer le run
            log.error("[%d/%d] %s — échec : %s", i, len(plan), etiquette, e)
            continue
        avant = len(cartes)
        for job in lot:
            cartes.setdefault(job.external_id, job)
        log.info("[%d/%d] %s → %d offres (%d nouvelles)",
                 i, len(plan), etiquette, len(lot), len(cartes) - avant)

    log.info("%d offres distinctes collectées (%d requêtes HTTP)",
             len(cartes), client.requetes)
    surveiller(store, "linkedin", list(cartes.values()), age_max)
    cartes = filtrer_par_age(cartes, age_max, heures=fenetre)

    # 2. On ne récupère la fiche détaillée que des offres jamais vues.
    #    C'est ce qui rend le run quotidien économe : le gros du volume est
    #    constitué d'offres déjà en base.
    connus = store.ids_connus("linkedin")
    nouveaux = [j for j in cartes.values() if j.external_id not in connus]
    log.info("%d nouvelles offres, %d déjà connues", len(nouveaux), len(cartes) - len(nouveaux))

    # Pré-scoring sur le seul titre : si le plafond de fiches est atteint,
    # autant avoir traité les plus prometteuses d'abord.
    for job in nouveaux:
        preparer(job)
    nouveaux.sort(key=lambda j: j.score, reverse=True)

    plafond = args.max_details or r.get("max_details_par_run", 250)
    if len(nouveaux) > plafond:
        log.warning("plafond atteint : %d fiches détaillées sur %d nouvelles offres "
                    "(les %d moins pertinentes sont enregistrées sans description)",
                    plafond, len(nouveaux), len(nouveaux) - plafond)

    # 3. Fiches détaillées, classification, scoring, stockage
    retenues = 0
    for i, job in enumerate(nouveaux, 1):
        if i <= plafond:
            job, html = client.detail(job)
            if html:
                sauver_brut(RACINE / "store" / "raw", "linkedin", job.external_id, html)
        preparer(job)
        if store.upsert(job) and job.is_alternance:
            retenues += 1
        if i % 25 == 0:
            log.info("  ... %d/%d fiches traitées", i, len(nouveaux))

    # 4. Rattrapage : si le plafond n'a pas été consommé, on complète les
    #    offres des runs précédents restées sans description. Sur plusieurs
    #    jours, le backlog se résorbe tout seul.
    budget_restant = plafond - min(len(nouveaux), plafond)
    if budget_restant > 0:
        backlog = store.sans_description("linkedin", budget_restant)
        if backlog:
            log.info("rattrapage : %d fiches en attente de description", len(backlog))
            for job in backlog:
                job, html = client.detail(job)
                if html:
                    sauver_brut(RACINE / "store" / "raw", "linkedin", job.external_id, html)
                store.maj_description(preparer(job))

    # Les offres déjà connues sont re-scorées (le config a pu changer)
    for job in cartes.values():
        if job.external_id in connus:
            store.upsert(preparer(job))

    client.close()
    log.info("terminé — %d nouvelles alternances retenues", retenues)
    cmd_stats(args, cfg, store)


def cmd_hellowork(args, cfg: dict, store: Store) -> None:
    from collectors.hellowork import HelloWork

    h = cfg.get("hellowork", {})
    preparer = pipeline(cfg)
    heures, age_max = fenetre_de(args, h.get("age_max_jours", 14) * 24)
    plafond = args.max_details or h.get("max_details_par_run", 150)

    client = HelloWork(delai=h.get("delai", 2.5), pages_max=h.get("pages_max", 6))
    cartes: dict[str, Job] = {}
    for kw in h.get("mots_cles", []):
        try:
            lot = client.rechercher(kw, age_max_jours=age_max)
        except Exception as e:
            log.error("« %s » — échec : %s", kw, e)
            continue
        avant = len(cartes)
        for job in lot:
            cartes.setdefault(job.external_id, job)
        log.info("« %s » → %d offres (%d nouvelles)", kw, len(lot), len(cartes) - avant)

    surveiller(store, "hellowork", list(cartes.values()), age_max)
    connus = store.ids_connus("hellowork")
    nouveaux = [j for j in cartes.values() if j.external_id not in connus]
    log.info("%d offres distinctes, %d nouvelles (%d requêtes)",
             len(cartes), len(nouveaux), client.requetes)

    for job in nouveaux:
        preparer(job)
    nouveaux.sort(key=lambda j: j.score, reverse=True)

    retenues = 0
    for i, job in enumerate(nouveaux, 1):
        if i <= plafond:
            job, html = client.detail(job)
            if html:
                sauver_brut(RACINE / "store" / "raw", "hellowork", job.external_id, html)
        preparer(job)
        if store.upsert(job):
            retenues += 1

    # Rattrapage identique à LinkedIn : le backlog se résorbe sur plusieurs runs.
    reste = plafond - min(len(nouveaux), plafond)
    if reste > 0:
        for job in store.sans_description("hellowork", reste):
            job, html = client.detail(job)
            if html:
                sauver_brut(RACINE / "store" / "raw", "hellowork", job.external_id, html)
            store.maj_description(preparer(job))

    client.close()
    log.info("terminé — %d nouvelles offres HelloWork", retenues)
    cmd_stats(args, cfg, store)


def cmd_indeed(args, cfg: dict, store: Store) -> None:
    from collectors.indeed import Indeed

    i = cfg.get("indeed", {})
    preparer = pipeline(cfg)
    heures, age_max = fenetre_de(args, i.get("age_max_jours", 14) * 24)
    plafond = getattr(args, "max_details", 0) or i.get("max_details_par_run", 120)

    from collectors.indeed import BlocageIndeed

    client = Indeed(delai=i.get("delai", 4.0), pages_max=i.get("pages_max", 5))
    cartes: dict[str, Job] = {}
    for kw in i.get("mots_cles", []):
        try:
            lot = client.rechercher(kw, age_max_jours=age_max,
                                    lieu=i.get("lieu", "France"))
        except BlocageIndeed as e:
            # On s'arrête net : continuer ne ferait qu'allonger le blocage.
            log.error("%s", e)
            break
        except Exception as e:
            log.error("« %s » — échec : %s", kw, e)
            continue
        avant = len(cartes)
        for job in lot:
            cartes.setdefault(job.external_id, job)
        log.info("« %s » → %d offres (%d nouvelles)", kw, len(lot), len(cartes) - avant)

    surveiller(store, "indeed", list(cartes.values()), age_max)
    store.maj_horodatages(cartes.values())
    connus = store.ids_connus("indeed")
    nouveaux = [j for j in cartes.values() if j.external_id not in connus]
    log.info("%d offres distinctes, %d nouvelles (%d requêtes)",
             len(cartes), len(nouveaux), client.requetes)

    for job in nouveaux:
        preparer(job)
    nouveaux.sort(key=lambda j: j.score, reverse=True)

    # Descriptions récupérées par lots de 20 : une requête pour vingt offres.
    completes = client.completer(nouveaux[:plafond])
    log.info("%d descriptions complétées en %d requêtes groupées",
             completes, -(-min(len(nouveaux), plafond) // 20))

    retenues = 0
    for job in nouveaux:
        preparer(job)
        if store.upsert(job):
            retenues += 1

    # Rattrapage du backlog des runs précédents, par lots également.
    reste = plafond - min(len(nouveaux), plafond)
    if reste > 0:
        backlog = store.sans_description("indeed", reste)
        if backlog:
            client.completer(backlog)
            for job in backlog:
                store.maj_description(preparer(job))

    client.close()
    log.info("terminé — %d nouvelles offres Indeed", retenues)
    cmd_stats(args, cfg, store)


def cmd_jobteaser(args, cfg: dict, store: Store) -> None:
    from collectors.jobteaser import BlocageJobTeaser, JobTeaser

    j = cfg.get("jobteaser", {})
    preparer = pipeline(cfg)
    heures, age_max = fenetre_de(args, j.get("age_max_jours", 14) * 24)
    plafond = getattr(args, "max_details", 0) or j.get("max_details_par_run", 120)

    client = JobTeaser(delai=j.get("delai", 3.0), pages_max=j.get("pages_max", 4))
    cartes: dict[str, Job] = {}
    for kw in j.get("mots_cles", []):
        try:
            lot = client.rechercher(kw)
        except BlocageJobTeaser as e:
            log.error("%s", e)
            break
        except Exception as e:
            log.error("« %s » — échec : %s", kw, e)
            continue
        avant = len(cartes)
        for job in lot:
            cartes.setdefault(job.external_id, job)
        log.info("« %s » → %d offres (%d nouvelles)", kw, len(lot), len(cartes) - avant)

    connus = store.ids_connus("jobteaser")
    nouveaux = [c for c in cartes.values() if c.external_id not in connus]
    log.info("%d offres distinctes, %d nouvelles (%d requêtes)",
             len(cartes), len(nouveaux), client.requetes)

    # Ici la fiche est indispensable : la page de liste ne donne ni entreprise,
    # ni lieu, ni date. Une offre non complétée est inexploitable.
    retenues = 0
    for n, job in enumerate(nouveaux[:plafond], 1):
        job, page = client.completer(job)
        if page:
            sauver_brut(RACINE / "store" / "raw", "jobteaser", job.external_id, page)
        preparer(job)
        if store.upsert(job):
            retenues += 1
        if n % 20 == 0:
            log.info("  ... %d/%d fiches", n, min(len(nouveaux), plafond))

    # Pas de fenêtre passée au canari : la recherche JobTeaser ne filtre pas
    # sur l'âge, son volume ne dépend donc pas de `--depuis`. Relâcher le
    # seuil ici masquerait une vraie chute au lieu d'en éviter une fausse.
    completes = [j for j in nouveaux[:plafond] if j.company]
    surveiller(store, "jobteaser", completes)
    completes = filtrer_par_age({j.external_id: j for j in completes},
                                age_max, heures=heures)

    reste = plafond - min(len(nouveaux), plafond)
    if reste > 0:
        for job in store.sans_description("jobteaser", reste):
            job, page = client.completer(job)
            if page:
                sauver_brut(RACINE / "store" / "raw", "jobteaser", job.external_id, page)
            store.maj_description(preparer(job))

    client.close()
    log.info("terminé — %d nouvelles offres JobTeaser", retenues)
    cmd_stats(args, cfg, store)


def cmd_wttj(args, cfg: dict, store: Store) -> None:
    """Welcome to the Jungle, via son index Algolia public.

    Seule source dont la recherche renvoie déjà tout sauf la description :
    titre, entreprise, lieu, date ET type de contrat. On peut donc filtrer
    l'âge AVANT de dépenser la moindre requête de détail — l'inverse de
    JobTeaser, où la fiche est indispensable pour savoir ce qu'on tient.
    """
    from collectors.wttj import BlocageWTTJ, WelcomeToTheJungle

    w = cfg.get("wttj", {})
    preparer = pipeline(cfg)
    heures, age_max = fenetre_de(args, w.get("age_max_jours", 14) * 24)
    plafond = getattr(args, "max_details", 0) or w.get("max_details_par_run", 150)

    client = WelcomeToTheJungle(delai=w.get("delai", 1.5),
                                pages_max=w.get("pages_max", 5))
    cartes: dict[str, Job] = {}
    for kw in w.get("mots_cles", []):
        try:
            lot = client.rechercher(kw)
        except BlocageWTTJ as e:
            log.error("%s", e)
            break
        except Exception as e:
            log.error("« %s » — échec : %s", kw, e)
            continue
        avant = len(cartes)
        for job in lot:
            cartes.setdefault(job.external_id, job)
        log.info("« %s » → %d offres (%d nouvelles)", kw, len(lot), len(cartes) - avant)

    if not cartes:
        client.close()
        log.error("aucune offre — index Algolia inaccessible ?")
        return

    surveiller(store, "wttj", list(cartes.values()))
    frais = filtrer_par_age(cartes, age_max, heures=heures)
    store.maj_horodatages(frais.values())

    connus = store.ids_connus("wttj")
    nouveaux = [c for c in frais.values() if c.external_id not in connus]
    log.info("%d offres distinctes, %d dans la fenêtre, %d nouvelles (%d requêtes)",
             len(cartes), len(frais), len(nouveaux), client.requetes)

    retenues = 0
    for job in nouveaux:
        preparer(job)
        if store.upsert(job):
            retenues += 1

    # La description arrive après coup : une offre sans elle reste utilisable
    # (le contrat vient de la facette serveur, pas du texte). Le plafond ne
    # fait donc perdre aucune offre, il étale seulement les requêtes.
    manquantes = store.sans_description("wttj", plafond)
    for n, job in enumerate(manquantes, 1):
        job, brut = client.completer(job)
        if brut:
            sauver_brut(RACINE / "store" / "raw", "wttj", job.external_id, brut)
        store.maj_description(preparer(job))
        if n % 25 == 0:
            log.info("  ... %d/%d descriptions", n, len(manquantes))

    client.close()
    log.info("terminé — %d nouvelles offres Welcome to the Jungle "
             "(%d descriptions récupérées)", retenues, len(manquantes))
    cmd_stats(args, cfg, store)


def cmd_pass(args, cfg: dict, store: Store) -> None:
    """PASS — flux RSS officiel de l'apprentissage dans la fonction publique.

    Le collecteur le plus simple du projet : une requête, aucune fiche à
    compléter. Le flux porte déjà les descriptions complètes, et jusqu'à
    l'adresse de candidature.
    """
    from collectors.pass_fp import PassFonctionPublique

    p = cfg.get("pass", {})
    preparer = pipeline(cfg)
    heures, age_max = fenetre_de(args, p.get("age_max_jours", 14) * 24)

    client = PassFonctionPublique(delai=p.get("delai", 2.0))
    cartes: dict[str, Job] = {}
    for flux in p.get("flux", ["apprentissage"]):
        try:
            for job in client.rechercher(flux):
                cartes.setdefault(job.external_id, job)
        except Exception as e:
            log.error("flux « %s » — échec : %s", flux, e)
    client.close()

    if not cartes:
        log.error("aucune offre — le flux RSS a-t-il changé de forme ?")
        return

    surveiller(store, "pass", list(cartes.values()))
    frais = filtrer_par_age(cartes, age_max, heures=heures)
    store.maj_horodatages(frais.values())

    connus = store.ids_connus("pass")
    nouveaux = [c for c in frais.values() if c.external_id not in connus]
    log.info("%d offres au flux, %d dans la fenêtre, %d nouvelles (%d requêtes)",
             len(cartes), len(frais), len(nouveaux), client.requetes)

    retenues = sum(bool(store.upsert(preparer(job))) for job in nouveaux)
    log.info("terminé — %d nouvelles offres PASS", retenues)
    cmd_stats(args, cfg, store)


def cmd_glassdoor(args, cfg: dict, store: Store) -> None:
    """Glassdoor, dans les limites de son robots.txt.

    Une page par mot-clé — la pagination est interdite. La profondeur vient
    donc du nombre de mots-clés, pas de la profondeur de pagination.
    """
    from collectors.glassdoor import BlocageGlassdoor, Glassdoor

    g = cfg.get("glassdoor", {})
    preparer = pipeline(cfg)
    heures, age_max = fenetre_de(args, g.get("age_max_jours", 14) * 24)
    plafond = getattr(args, "max_details", 0) or g.get("max_details_par_run", 60)

    client = Glassdoor(delai=g.get("delai", 5.0))
    cartes: dict[str, Job] = {}
    for kw in g.get("mots_cles", []):
        try:
            lot = client.rechercher(kw, g.get("lieu", "france"))
        except BlocageGlassdoor as e:
            log.error("%s", e)
            break
        except Exception as e:
            log.error("« %s » — échec : %s", kw, e)
            continue
        avant = len(cartes)
        for job in lot:
            cartes.setdefault(job.external_id, job)
        log.info("« %s » → %d offres (%d nouvelles)", kw, len(lot),
                 len(cartes) - avant)

    if not cartes:
        client.close()
        log.error("aucune offre — le slug de recherche a-t-il changé ?")
        return

    surveiller(store, "glassdoor", list(cartes.values()), age_max)
    frais = filtrer_par_age(cartes, age_max, heures=heures)

    connus = store.ids_connus("glassdoor")
    nouveaux = [c for c in frais.values() if c.external_id not in connus]
    log.info("%d offres distinctes, %d dans la fenêtre, %d nouvelles (%d requêtes)",
             len(cartes), len(frais), len(nouveaux), client.requetes)

    retenues = sum(bool(store.upsert(preparer(job))) for job in nouveaux)

    # Les fiches répondent 403 : le plafond est à 0 dans la configuration, et
    # cette garde évite d'entrer dans la boucle pour rien. Le code reste prêt
    # si Glassdoor rouvre ses fiches un jour.
    manquantes = store.sans_description("glassdoor", plafond) if plafond else []
    for n, job in enumerate(manquantes, 1):
        try:
            job, page = client.completer(job)
        except BlocageGlassdoor as e:
            log.error("%s", e)
            break
        if page:
            sauver_brut(RACINE / "store" / "raw", "glassdoor",
                        job.external_id, page)
        store.maj_description(preparer(job))
        if n % 20 == 0:
            log.info("  ... %d/%d fiches", n, len(manquantes))

    client.close()
    log.info("terminé — %d nouvelles offres Glassdoor (%d fiches complétées)",
             retenues, len(manquantes))
    cmd_stats(args, cfg, store)


def cmd_adopte1dev(args, cfg: dict, store: Store) -> None:
    """adopte1dev — job board 100 % développement, API WordPress ouverte.

    Une passe suffit : le filtre de contrat est côté serveur et la réponse
    porte déjà description, entreprise, ville et technologies.
    """
    from collectors.adopte1dev import Adopte1Dev

    a = cfg.get("adopte1dev", {})
    preparer = pipeline(cfg)
    heures, age_max = fenetre_de(args, a.get("age_max_jours", 14) * 24)

    client = Adopte1Dev(delai=a.get("delai", 1.5))
    cartes: dict[str, Job] = {}
    try:
        for contrat in a.get("contrats", ["Alternance"]):
            for job in client.rechercher(contrat):
                cartes.setdefault(job.external_id, job)
    except Exception as e:
        log.error("échec : %s", e)
    finally:
        client.close()

    if not cartes:
        log.error("aucune offre — la taxonomie du site a-t-elle changé ?")
        return

    surveiller(store, "adopte1dev", list(cartes.values()))
    frais = filtrer_par_age(cartes, age_max, heures=heures)
    store.maj_horodatages(frais.values())

    connus = store.ids_connus("adopte1dev")
    nouveaux = [c for c in frais.values() if c.external_id not in connus]
    log.info("%d offres, %d dans la fenêtre, %d nouvelles (%d requêtes)",
             len(cartes), len(frais), len(nouveaux), client.requetes)

    retenues = sum(bool(store.upsert(preparer(job))) for job in nouveaux)
    log.info("terminé — %d nouvelles offres adopte1dev", retenues)
    cmd_stats(args, cfg, store)


def cmd_devitjobs(args, cfg: dict, store: Store) -> None:
    """DevITjobs.fr — flux RSS complet, IT généraliste.

    Une passe suffit, comme pour PASS. Le site ne déclare pas l'alternance
    comme type de contrat : c'est `classify.py` qui tranche sur le titre.
    """
    from collectors.devitjobs import DevITJobs

    d = cfg.get("devitjobs", {})
    preparer = pipeline(cfg)
    heures, age_max = fenetre_de(args, d.get("age_max_jours", 14) * 24)

    client = DevITJobs(delai=d.get("delai", 1.5))
    try:
        cartes = {job.external_id: job for job in client.rechercher()}
    except Exception as e:
        log.error("échec : %s", e)
        cartes = {}
    finally:
        client.close()

    if not cartes:
        log.error("aucune offre — le flux RSS a-t-il changé de forme ?")
        return

    surveiller(store, "devitjobs", list(cartes.values()))
    frais = filtrer_par_age(cartes, age_max, heures=heures)
    store.maj_horodatages(frais.values())

    connus = store.ids_connus("devitjobs")
    nouveaux = [c for c in frais.values() if c.external_id not in connus]
    log.info("%d offres, %d dans la fenêtre, %d nouvelles (%d requêtes)",
             len(cartes), len(frais), len(nouveaux), client.requetes)

    retenues = sum(bool(store.upsert(preparer(job))) for job in nouveaux)
    log.info("terminé — %d nouvelles offres devitjobs", retenues)
    cmd_stats(args, cfg, store)


def cmd_welovedevs(args, cfg: dict, store: Store) -> None:
    """WeLoveDevs — recherche Algolia, filtre de contrat côté serveur.

    Comme Welcome to the Jungle, `contract_type` vient du facet du site :
    pas de repasse par classify.py, le volume est simplement faible.
    """
    from collectors.welovedevs import WeLoveDevs

    w = cfg.get("welovedevs", {})
    preparer = pipeline(cfg)
    heures, age_max = fenetre_de(args, w.get("age_max_jours", 14) * 24)

    client = WeLoveDevs(delai=w.get("delai", 1.5))
    cartes: dict[str, Job] = {}
    try:
        for contrat in w.get("contrats", ["apprenticeship"]):
            for job in client.rechercher(contrat):
                cartes.setdefault(job.external_id, job)
    except Exception as e:
        log.error("échec : %s", e)
    finally:
        client.close()

    if not cartes:
        log.error("aucune offre — le facet de contrat a-t-il changé de nom ?")
        return

    surveiller(store, "welovedevs", list(cartes.values()))
    frais = filtrer_par_age(cartes, age_max, heures=heures)
    store.maj_horodatages(frais.values())

    connus = store.ids_connus("welovedevs")
    nouveaux = [c for c in frais.values() if c.external_id not in connus]
    log.info("%d offres, %d dans la fenêtre, %d nouvelles (%d requêtes)",
             len(cartes), len(frais), len(nouveaux), client.requetes)

    retenues = sum(bool(store.upsert(preparer(job))) for job in nouveaux)
    log.info("terminé — %d nouvelles offres welovedevs", retenues)
    cmd_stats(args, cfg, store)


def cmd_lba(args, cfg: dict, store: Store) -> None:
    """La Bonne Alternance : offres publiées + marché caché."""
    from collectors.labonnealternance import LaBonneAlternance

    l = cfg.get("lba", {})
    preparer = pipeline(cfg)

    try:
        client = LaBonneAlternance(delai=l.get("delai", 1.0))
    except RuntimeError as e:
        log.error("%s", e)
        return

    niveaux = tuple(str(n) for n in l.get("niveaux", ["6", "7"]))
    bassins = l.get("bassins", [])
    romes = l.get("romes", ["M1805"])
    log.info("plan : %d bassins x %d ROME x %d niveaux",
             len(bassins), len(romes), len(niveaux))

    trouves: dict[str, Job] = {}
    try:
        for bassin in bassins:
            for rome in romes:
                try:
                    lot = client.rechercher(
                        rome, bassin["lat"], bassin["lon"],
                        rayon=bassin.get("rayon", 100), niveaux=niveaux,
                    )
                except RuntimeError as e:
                    log.error("%s", e)
                    return
                except Exception as e:
                    log.error("%s / %s — échec : %s", bassin["nom"], rome, e)
                    continue
                avant = len(trouves)
                for job in lot:
                    trouves.setdefault(job.uid, job)
                log.info("%s / %s → %d résultats (%d nouveaux) [quota restant : %s]",
                         bassin["nom"], rome, len(lot), len(trouves) - avant,
                         client.quota_restant or "?")
    finally:
        client.close()

    # Le canari D'ABORD, sur le lot brut : il mesure l'extraction, pas la
    # fraîcheur. L'inverse — filtrer puis contrôler — annonçait « parseur
    # cassé, 1 offre pour 5 attendues » un jour où le parseur en avait
    # correctement extrait 28, dont 27 hors de la fenêtre de 24 h.
    heures, age_max = fenetre_de(args, 336)
    for source in ("lba", "lba_entreprise"):
        surveiller(store, source,
                   [j for j in trouves.values() if j.source == source],
                   age_max if source == "lba" else None)

    # La fraîcheur ne s'applique qu'aux OFFRES : les entreprises du marché
    # caché forment une liste permanente, sans date de publication, et les
    # écarter sur ce critère les supprimerait toutes.
    if getattr(args, "depuis", None):
        offres_datees = {k: j for k, j in trouves.items() if j.source == "lba"}
        gardees = filtrer_par_age(offres_datees, age_max, heures=heures)
        trouves = {k: j for k, j in trouves.items()
                   if j.source != "lba" or k in gardees}

    offres = entreprises = 0
    for job in trouves.values():
        preparer(job)
        if store.upsert(job):
            if job.source == "lba_entreprise":
                entreprises += 1
            else:
                offres += 1

    log.info("terminé — %d offres et %d entreprises du marché caché (%d requêtes)",
             offres, entreprises, client.requetes)
    cmd_stats(args, cfg, store)


def cmd_login(args, cfg: dict, store: Store) -> None:
    """Établit la session LinkedIn, sans rien collecter.

    Séparé de `posts` à dessein : coupler la connexion à la collecte fait
    qu'une authentification un peu longue (double facteur, mot de passe à
    retrouver) fait échouer le run entier. Ici on ne fait qu'attendre.
    """
    from collectors.linkedin_posts import LinkedInPosts

    client = LinkedInPosts(RACINE / ".chrome-profile")
    client.ouvrir()
    try:
        if client.connecte():
            print("\n  Session déjà active — rien à faire.\n")
            return
        if client.attendre_connexion():
            print("  Tu peux maintenant lancer :  .\\radar.ps1 posts\n")
        else:
            log.error("aucune connexion détectée — relance la commande")
    finally:
        client.fermer()


JETON_CADENCE = ".derniere_collecte_posts"
# Où reprendre la rotation des mots-clés au run suivant (voir
# `_recherches_du_run`). Un simple entier, l'index du prochain départ.
JETON_ROTATION = ".rotation_mots_cles_posts"


def _cadence_posts(cfg: dict, force: bool) -> bool:
    """Interdit deux collectes de posts trop rapprochées.

    La recherche de contenu LinkedIn se coupe — résultats vides, sans erreur
    ni avertissement — quand elle est sollicitée trop souvent depuis la même
    session. Constaté le 13/08/2026 : quatre runs en vingt minutes ont suffi.
    Le blocage se lève seul en quelques heures, mais il rend l'outil inutile
    entre-temps, alors mieux vaut refuser de partir.
    """
    jeton = RACINE / JETON_CADENCE
    minimum = float(cfg.get("posts", {}).get("heures_entre_runs", 6))

    if jeton.exists() and not force:
        try:
            dernier = datetime.fromisoformat(jeton.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            dernier = None
        if dernier:
            ecoule = (datetime.now() - dernier).total_seconds() / 3600
            if ecoule < minimum:
                log.error(
                    "dernière collecte il y a %.1f h — minimum %.0f h. "
                    "LinkedIn coupe la recherche si on insiste. "
                    "Relance après %s, ou passe --force en connaissance de cause.",
                    ecoule, minimum,
                    (dernier + timedelta(hours=minimum)).strftime("%H:%M"),
                )
                return False

    jeton.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    return True


def _recherches_du_run(p: dict) -> list[str]:
    """Choisit les mots-clés du run, en TOURNANT d'un passage à l'autre.

    Sans rotation, `[:max_recherches_par_run]` prend toujours les mêmes
    premiers : sur dix mots-clés configurés et un plafond de cinq, les cinq
    derniers ne sont jamais lancés — jamais, quel que soit le nombre de runs.

    Le plafond, lui, ne bouge pas : c'est l'accumulation de recherches dans
    une même session qui fait couper LinkedIn, pas leur variété. Cinq
    recherches restent cinq recherches ; seules celles qu'on lance changent.

    Reste désactivé par défaut : l'ordre des mots-clés est délibéré, les plus
    rentables d'abord (« alternance développeur » a été mesuré à 11 posts
    retenus, les formulations en fin de liste à presque rien). Tourner élargit
    la couverture mais dépense des runs sur des formulations moins sûres —
    c'est un arbitrage, d'où le réglage explicite.
    """
    mots = [str(m).strip() for m in p.get("mots_cles", []) if str(m).strip()]
    plafond = max(1, int(p.get("max_recherches_par_run", 5)))
    if not p.get("rotation_mots_cles") or len(mots) <= plafond:
        return mots[:plafond]

    jeton = RACINE / JETON_ROTATION
    try:
        depart = int(jeton.read_text(encoding="utf-8").strip()) % len(mots)
    except (ValueError, OSError):
        depart = 0

    choix = [mots[(depart + i) % len(mots)] for i in range(plafond)]
    try:
        jeton.write_text(str((depart + plafond) % len(mots)), encoding="utf-8")
    except OSError as e:
        # Le tour est perdu, pas le run : on repartira de zéro au suivant.
        log.warning("rotation non mémorisée (%s)", e)
    log.info("rotation : mots-clés %d à %d sur %d",
             depart + 1, depart + plafond, len(mots))
    return choix


def cmd_posts(args, cfg: dict, store: Store) -> None:
    """Collecte les posts du fil LinkedIn (session requise)."""
    from collectors.linkedin_posts import LinkedInPosts

    if not _cadence_posts(cfg, getattr(args, "force", False)):
        return

    p = cfg.get("posts", {})
    preparer = pipeline(cfg)
    heures, age_max = fenetre_de(args, p.get("age_max_jours", 14) * 24)

    # LinkedIn ne propose que trois fenêtres sur la recherche de contenu.
    # On prend la plus proche par excès, puis on affine côté client sur la
    # date relative affichée (« il y a 3 j », « 2 sem. »).
    fenetre_li = ("past-24h" if age_max <= 1
                  else "past-week" if age_max <= 7
                  else "past-month")

    client = LinkedInPosts(RACINE / ".chrome-profile", delai=p.get("delai", 4.0))
    client.ouvrir()
    try:
        if not client.connecte() and not client.attendre_connexion():
            log.error("session LinkedIn absente — abandon (aucun login automatisé)")
            return

        total, avec_contact = 0, 0

        # Les comptes suivis d'abord : une page chacun, sans passer par le
        # moteur de recherche. S'il ne reste qu'un budget de requêtes avant
        # la coupure, autant le dépenser sur le canal le plus rentable.
        taches = [("compte", c) for c in p.get("comptes_suivis", [])]
        # Puis les recherches, plafonnées : c'est leur accumulation, bien
        # plus que le défilement, qui déclenche la coupure.
        recherches = _recherches_du_run(p)
        taches += [("recherche", kw) for kw in recherches]
        log.info("%d comptes suivis + %d recherches (sur %d configurées)",
                 len(p.get("comptes_suivis", [])), len(recherches),
                 len(p.get("mots_cles", [])))

        for genre, kw in taches:
            try:
                if genre == "compte":
                    lot = client.compte(kw, p.get("defilements", 6), age_max)
                else:
                    lot = client.rechercher(kw, fenetre_li,
                                            p.get("defilements", 6), age_max)
            except RuntimeError as e:
                # Page de vérification : on s'arrête net plutôt que d'insister.
                log.error("%s", e)
                break
            except Exception as e:
                log.error("« %s » — échec : %s", kw, e)
                continue

            # Lecture complète hors session : le texte des cartes est tronqué
            # et les adresses de contact sont presque toujours en fin de post.
            client.enrichir(lot)

            nouveaux = 0
            for post in lot:
                preparer(post)
                if store.upsert(post):
                    nouveaux += 1
                    if post.contacts:
                        avec_contact += 1
            total += nouveaux
            log.info("« %s » → %d posts retenus (%d nouveaux)", kw, len(lot), nouveaux)

        log.info("terminé — %d nouveaux posts, dont %d avec contact direct",
                 total, avec_contact)
    finally:
        client.fermer()
    cmd_stats(args, cfg, store)


def cmd_posts_web(args, cfg: dict, store: Store) -> None:
    """Posts LinkedIn trouvés par DuckDuckGo, sans passer par LinkedIn Search.

    Complète `posts` plutôt qu'il ne le remplace : celui-ci n'exige aucune
    session, ne peut pas déclencher la coupure de la recherche LinkedIn, et
    n'est donc pas soumis au délai de six heures. En revanche il ne voit que
    ce qu'un moteur a indexé — donc plutôt des posts déjà anciens de
    quelques jours, là où `posts` voit l'heure qui vient.
    """
    from collectors.posts_web import PostsWeb

    w = cfg.get("posts_web", {})
    preparer = pipeline(cfg)
    heures, age_max = fenetre_de(args, w.get("age_max_jours", 21) * 24)

    requetes = [str(r).strip() for r in w.get("requetes", []) if str(r).strip()]
    if not requetes:
        log.error("aucune requête dans `posts_web.requetes` de config.yaml")
        return

    client = PostsWeb(delai=w.get("delai", 2.5), pages=w.get("pages", 3))
    client.ouvrir()
    trouves: dict[str, Job] = {}
    try:
        for requete in requetes:
            urls = client.chercher(requete)
            for post in client.lire(urls, age_max):
                trouves.setdefault(post.external_id, post)
    except Exception as e:
        log.error("échec : %s", e)
    finally:
        client.fermer()

    if not trouves:
        log.error("aucun post retenu — DuckDuckGo a-t-il servi une page vide ? "
                  "(le mode sans interface ne fonctionne pas, voir posts_web.py)")
        return

    # Étiquette de canari distincte de `linkedin_post` : les deux collecteurs
    # alimentent la même source mais n'ont aucune raison d'avoir le même
    # volume, et les comparer entre eux ferait sonner l'alerte à chaque fois.
    surveiller(store, "posts_web", list(trouves.values()), age_max)

    connus = store.ids_connus("linkedin_post")
    nouveaux = [p for p in trouves.values() if p.external_id not in connus]
    log.info("%d posts retenus, %d nouveaux (%d recherches)",
             len(trouves), len(nouveaux), client.requetes)

    retenues = avec_contact = 0
    for post in nouveaux:
        if store.upsert(preparer(post)):
            retenues += 1
            avec_contact += bool(post.contacts)
    log.info("terminé — %d nouveaux posts, dont %d avec contact direct",
             retenues, avec_contact)
    cmd_stats(args, cfg, store)


def cmd_canari(args, cfg: dict, store: Store) -> None:
    """Sonde chaque collecteur avec une requête connue, et valide la donnée.

    Une requête par source, la plus banale possible. On ne regarde pas le
    code HTTP — un site cassé répond 200 tout aussi bien — mais ce qui sort
    du parseur : volume plausible et champs obligatoires réellement remplis.
    """
    from core.canari import (SONDE, Constat, controler, derive, enregistrer,
                             historique)

    r = cfg["recherche"]
    sondes: list[tuple[str, callable]] = []

    def sonde_linkedin():
        from collectors.linkedin_guest import LinkedInGuest
        c = LinkedInGuest(delai=r.get("delai_entre_requetes", 3.0), max_pages=1)
        try:
            return c.rechercher("alternance développeur", "France", fenetre_heures=336)
        finally:
            c.close()

    def sonde_hellowork():
        from collectors.hellowork import HelloWork
        h = cfg.get("hellowork", {})
        c = HelloWork(delai=h.get("delai", 2.5), pages_max=1)
        try:
            return c.rechercher("développeur", age_max_jours=30)
        finally:
            c.close()

    def sonde_indeed():
        from collectors.indeed import Indeed
        i = cfg.get("indeed", {})
        c = Indeed(delai=i.get("delai", 4.0), pages_max=1)
        try:
            return c.rechercher("alternance développeur", age_max_jours=14)
        finally:
            c.close()

    def sonde_lba():
        from collectors.labonnealternance import LaBonneAlternance
        l = cfg.get("lba", {})
        c = LaBonneAlternance(delai=l.get("delai", 1.2))
        try:
            return c.rechercher("M1805", 48.8566, 2.3522, rayon=60, niveaux=("7",))
        finally:
            c.close()

    def sonde_jobteaser():
        """La liste JobTeaser ne rend que des coquilles : sans compléter
        quelques fiches, la sonde validerait un parseur à moitié mort."""
        from collectors.jobteaser import JobTeaser
        j = cfg.get("jobteaser", {})
        c = JobTeaser(delai=j.get("delai", 3.0), pages_max=1)
        try:
            lot = c.rechercher("alternance développeur")[:10]
            return [c.completer(job)[0] for job in lot]
        finally:
            c.close()

    def sonde_pass():
        from collectors.pass_fp import PassFonctionPublique
        c = PassFonctionPublique(delai=cfg.get("pass", {}).get("delai", 2.0))
        try:
            return c.rechercher("apprentissage")
        finally:
            c.close()

    def sonde_glassdoor():
        from collectors.glassdoor import Glassdoor
        g = cfg.get("glassdoor", {})
        c = Glassdoor(delai=g.get("delai", 5.0))
        try:
            return c.rechercher("alternance développeur", g.get("lieu", "france"))
        finally:
            c.close()

    def sonde_adopte1dev():
        from collectors.adopte1dev import Adopte1Dev
        c = Adopte1Dev(delai=cfg.get("adopte1dev", {}).get("delai", 1.5))
        try:
            return c.rechercher("Alternance")
        finally:
            c.close()

    def sonde_wttj():
        """Une seule requête suffit : l'index Algolia rend déjà tout sauf la
        description, `contract_type` compris."""
        from collectors.wttj import WelcomeToTheJungle
        w = cfg.get("wttj", {})
        c = WelcomeToTheJungle(delai=w.get("delai", 1.5), pages_max=1)
        try:
            return c.rechercher("developpeur")
        finally:
            c.close()

    def sonde_devitjobs():
        from collectors.devitjobs import DevITJobs
        c = DevITJobs(delai=cfg.get("devitjobs", {}).get("delai", 1.5))
        try:
            return c.rechercher()
        finally:
            c.close()

    def sonde_welovedevs():
        from collectors.welovedevs import WeLoveDevs
        c = WeLoveDevs(delai=cfg.get("welovedevs", {}).get("delai", 1.5))
        try:
            return c.rechercher("apprenticeship")
        finally:
            c.close()

    sondes = [("linkedin", sonde_linkedin), ("hellowork", sonde_hellowork),
              ("indeed", sonde_indeed), ("jobteaser", sonde_jobteaser),
              ("wttj", sonde_wttj), ("pass", sonde_pass),
              ("adopte1dev", sonde_adopte1dev), ("devitjobs", sonde_devitjobs),
              ("welovedevs", sonde_welovedevs),
              ("glassdoor", sonde_glassdoor), ("lba", sonde_lba)]

    print()
    casses = indecis = 0
    for source, sonde in sondes:
        try:
            jobs = sonde()
        except Exception as e:
            print(f"  CASSÉ  {source:<12} sonde impossible : {type(e).__name__}: {e}")
            enregistrer(store.db,
                        Constat(source, False, 0, {}, f"exception {type(e).__name__}"),
                        SONDE)
            casses += 1
            continue

        # `lba` renvoie offres ET entreprises : on contrôle chaque population.
        groupes: dict[str, list] = {}
        for job in jobs:
            groupes.setdefault(job.source, []).append(job)
        if not groupes:
            groupes = {source: []}

        for vraie_source, lot in groupes.items():
            constat = controler(vraie_source, lot)
            enregistrer(store.db, constat, SONDE)
            chute = derive(store.db, constat, SONDE)
            # Un « ? » n'est pas un défaut : le code de sortie sert aux tâches
            # planifiées, et un indécis les ferait échouer sans qu'il y ait
            # quoi que ce soit à réparer.
            casses += not constat.ok and not constat.indecis
            indecis += constat.indecis
            detail = constat.message + (f" · {chute}" if chute else "")
            print(f"  {constat.symbole}  {vraie_source:<12} {detail}")
            if constat.champs:
                taux = " ".join(f"{c}={t:.0%}" for c, t in constat.champs.items())
                print(f"                      {taux}")

    reserve = f" ({indecis} sans verdict)" if indecis else ""
    print(f"\n  {casses} source(s) en défaut{reserve}.\n" if casses
          else f"\n  Toutes les sources répondent correctement{reserve}.\n")

    if args.historique:
        print("  Historique :")
        for h in historique(store.db, 25):
            etat = "OK " if h["ok"] else "KO "
            nature = (h["contexte"] or "?")[:8]
            print(f'    {h["quand"]}  {etat} {nature:<8} {h["source"]:<14} '
                  f'{h["nb"]:>5}  {h["message"][:48]}')
        print()

    if casses:
        sys.exit(1)


def cmd_tout(args, cfg: dict, store: Store) -> None:
    """Enchaîne toutes les sources sans session, puis génère le digest.

    Les posts LinkedIn en sont exclus : ils ouvrent une fenêtre Chrome et
    demandent une session, ils se lancent à la main.
    """
    import os

    heures, jours = fenetre_de(args, cfg["recherche"].get("fenetre_heures", 336))
    log.info("=== collecte complète — fenêtre de %d h (%d jour(s)) ===", heures, jours)

    args.limite_requetes = 0
    args.max_details = 0
    cmd_collect(args, cfg, store)
    cmd_hellowork(args, cfg, store)
    cmd_indeed(args, cfg, store)
    cmd_jobteaser(args, cfg, store)
    cmd_wttj(args, cfg, store)
    cmd_pass(args, cfg, store)
    cmd_adopte1dev(args, cfg, store)
    cmd_devitjobs(args, cfg, store)
    cmd_welovedevs(args, cfg, store)
    cmd_glassdoor(args, cfg, store)
    if os.environ.get("LBA_API_KEY"):
        cmd_lba(args, cfg, store)
    else:
        log.warning("LBA_API_KEY absente — La Bonne Alternance ignorée")

    # Les posts restent OPTIONNELS et jamais actifs par défaut : ils ouvrent
    # une fenêtre Chrome et consomment le budget de requêtes LinkedIn. Dans
    # une tâche planifiée, `tout` doit rester silencieux et sans session.
    if getattr(args, "avec_posts", False):
        args.force = False
        cmd_posts(args, cfg, store)

    # Le digest reflète la fenêtre demandée : sans ça, `tout --depuis jour`
    # afficherait aussi les offres des runs précédents, vieilles de 15 jours.
    args.score_min, args.statut, args.tri = 20, None, "score"
    args.max_age = jours          # `--depuis` reste posé : il primera
    cmd_report(args, cfg, store)


def cmd_rescore(args, cfg: dict, store: Store) -> None:
    """Re-score toute la base après modification de config.yaml.

    Aucune requête réseau : on itère sur les descriptions déjà stockées.
    C'est ce qui permet de calibrer les poids en quelques secondes.
    """
    preparer = pipeline(cfg)
    jobs = store.tous()
    for job in jobs:
        store.maj_score(preparer(job))
    store.db.commit()
    doublons = store.recalculer_doublons()
    log.info("%d offres re-scorées, %d doublons inter-sources", len(jobs), doublons)
    cmd_stats(args, cfg, store)


def cmd_report(args, cfg: dict, store: Store) -> None:
    from report import generer

    chemin = generer(store, cfg, RACINE / "digest.html",
                     score_min=args.score_min, statut=args.statut,
                     age_max_heures=fenetre_affichage(args),
                     tri=getattr(args, "tri", "score"))
    log.info("digest écrit : %s", chemin)
    print(f"\n  file:///{str(chemin).replace(chr(92), '/')}\n")


def cmd_serve(args, cfg: dict, store: Store) -> None:
    """Sert le digest en local, avec les cases à cocher actives.

    Même filtrage que `report` : seul le mode de consultation change.
    """
    from serveur import servir

    servir(store, cfg,
           {"score_min": args.score_min, "statut": args.statut,
            "age_max_heures": fenetre_affichage(args),
            "tri": getattr(args, "tri", "score")},
           port=args.port, ouvrir=not args.sans_navigateur)


def _demander(question: str) -> str:
    """`input()` qui ne casse pas quand il n'y a personne au clavier.

    `postuler` est interactif par nature, mais rien n'empêche de le lancer
    depuis un contexte sans entrée standard. Mieux vaut renoncer proprement
    qu'afficher une trace d'EOFError.
    """
    try:
        return input(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n     (pas d'entrée clavier — abandon)")
        return ""


def cmd_postuler(args, cfg: dict, store: Store) -> None:
    """Ouvre une offre et pré-remplit son formulaire. N'envoie JAMAIS.

    Trois temps, dont deux t'appartiennent : le programme ouvre, tu navigues
    jusqu'au formulaire ; le programme remplit, tu relis et tu envoies ; tu
    confirmes, le programme marque l'offre comme suivie.
    """
    from postuler import remplir

    trouves = store.resoudre(args.uid)
    if not trouves:
        log.error("aucune offre ne correspond à « %s »", args.uid)
        return
    if len(trouves) > 1:
        print(f"\n  {len(trouves)} offres correspondent — précise :\n")
        for o in trouves[:10]:
            print(f"    {o['uid']:<28} {(o['title'] or '')[:44]:<44} "
                  f"{(o['company'] or '')[:22]}")
        print()
        return

    job = trouves[0]
    cand = cfg.get("candidature", {})
    if not cand:
        log.error("aucun bloc `candidature` dans config.yaml")
        return

    # Ce qui manque se dit AVANT d'ouvrir le navigateur, pas une fois devant
    # le formulaire : un champ obligatoire vide arrête la candidature.
    for cle in ("cv", "lettre"):
        chemin = str(cand.get(cle) or "")
        if chemin and not Path(chemin).exists():
            log.warning("%s introuvable : %s", cle, chemin)
    manquants = [c for c in ("prenom", "nom", "email", "telephone")
                 if not str(cand.get(c) or "").strip()]
    if manquants:
        log.warning("champs vides dans config.yaml : %s — ils resteront à "
                    "remplir à la main sur chaque formulaire",
                    ", ".join(manquants))

    # Indeed et Glassdoor opposent un défi Cloudflare au navigateur piloté.
    # Il n'est pas contournable proprement — et leurs conditions interdisent
    # l'accès automatisé, donc on ne cherche pas à le forcer. Autant le dire
    # avant d'ouvrir Chrome plutôt que de laisser buter sur la page de
    # vérification. Les 9 autres sources ne posent pas ce problème.
    if job["source"] in CLOUDFLARE:
        print(f"\n  ⚠  {job['source']} oppose une vérification Cloudflare aux")
        print("     navigateurs pilotés. Le pré-remplissage n'aboutira pas.")
        print("\n     Ouvre l'offre à la main dans ton navigateur habituel :")
        print(f"     {job['url']}\n")
        if not _demander("     Ouvrir quand même Chrome ? [o/N] ")\
                .startswith("o"):
            return

    from collectors.linkedin_posts import LinkedInPosts

    navigateur = LinkedInPosts(RACINE / ".chrome-profile", headless=False)
    navigateur.ouvrir()
    page = navigateur.page or navigateur.ctx.new_page()

    print(f"\n  {job['title']}")
    print(f"  {job['company']} · {job['location']}")
    print(f"  {job['url']}\n")
    page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)

    try:
        _demander("  1/3 — Navigue jusqu'au FORMULAIRE de candidature,\n"
                  "        puis appuie sur Entrée pour le remplir. ")
        remplis, ignores = remplir(page, cand, job)

        print(f"\n  2/3 — {len(remplis)} champ(s) remplis :")
        for r in remplis:
            print(f"        ✓ {r}")
        if ignores:
            print(f"\n        {len(ignores)} laissé(s) de côté — à toi de voir :")
            for i in ignores[:12]:
                print(f"        · {i}")
        print("\n        RIEN N'A ÉTÉ ENVOYÉ. Relis, complète, puis clique")
        print("        sur le bouton d'envoi toi-même.\n")

        reponse = _demander("  3/3 — Candidature envoyée ? [o/N] ")
        if reponse.startswith("o"):
            store.set_status(job["uid"], "applied",
                             f"candidature envoyée le {date.today():%d/%m/%Y}")
            print("        marquée comme envoyée — elle sort de ta liste.\n")
        else:
            print("        laissée en l'état.\n")
    finally:
        navigateur.fermer()


def cmd_cv(args, cfg: dict, store: Store) -> None:
    """Génère un CV LaTeX (et son PDF si possible) adapté à une offre.

    Le contenu ne change jamais : `cv.py` réordonne `cv_source.yaml` selon
    les tags de l'offre — il n'invente ni ne reformule rien (à l'exception
    de l'accroche, optionnelle, voir `cv.py`).

    Accepte un `uid`, un bout de nom, OU un lien direct — y compris vers une
    offre jamais collectée. Dans ce dernier cas, `cv.depuis_url` la récupère
    à la volée par son balisage `JobPosting` (schema.org), et elle passe par
    le MÊME pipeline de classification/score que n'importe quel collecteur,
    pour des tags comparables à ceux d'une offre déjà en base.
    """
    import hashlib
    import json

    import cv as cvgen

    terme = args.uid.strip()
    trouves = store.resoudre(terme)

    if trouves and len(trouves) > 1:
        print(f"\n  {len(trouves)} offres correspondent — précise :\n")
        for o in trouves[:10]:
            print(f"    {o['uid']:<28} {(o['title'] or '')[:44]:<44} "
                  f"{(o['company'] or '')[:22]}")
        print()
        return

    if trouves:
        row = trouves[0]
        titre, entreprise, description = row["title"], row["company"], row["description"]
        tags = json.loads(row["tags"] or "[]")
        # Un hash court, jamais l'uid brut : certaines sources (DevITjobs...)
        # portent l'URL entière en guise d'external_id, pleine de « / » qui
        # créeraient une arborescence de dossiers au lieu d'un seul.
        identifiant = hashlib.sha1(row["uid"].encode()).hexdigest()[:16]
    elif terme.startswith(("http://", "https://")):
        job_brut = cvgen.depuis_url(terme)
        if not job_brut:
            log.error("impossible d'extraire l'offre depuis %s — voir le "
                      "message ci-dessus", terme)
            return
        job_note = pipeline(cfg)(job_brut)
        titre, entreprise, description = job_note.title, job_note.company, job_note.description
        tags = job_note.tags
        identifiant = hashlib.sha1(terme.encode()).hexdigest()[:16]
    else:
        log.error("aucune offre ne correspond à « %s »", terme)
        return

    chemin_source = RACINE / "cv_source.yaml"
    if not chemin_source.exists():
        log.error("cv_source.yaml introuvable — copie cv_source.example.yaml "
                  "et personnalise-le avec ton propre parcours")
        return

    cv_data = cvgen.charger(chemin_source)
    dossier = RACINE / "store" / "cv" / identifiant
    chemin_tex, chemin_pdf = cvgen.generer(
        tags, cv_data, cfg, dossier,
        job_titre=titre or "", job_entreprise=entreprise or "",
        job_description=description or "",
    )

    print(f"\n  {titre}")
    print(f"  {entreprise}")
    print(f"  tags retenus : {', '.join(tags) or '(aucun)'}\n")
    print(f"  .tex : {chemin_tex}")
    if chemin_pdf:
        print(f"  pdf  : {chemin_pdf}\n")
    else:
        print("  pdf  : échec — voir le .log dans le même dossier, ou compile\n"
              "         le .tex ailleurs (Overleaf...)\n")


def _slugifier(nom: str) -> str:
    """Devine le slug LinkedIn d'une entreprise à partir de son nom.

    Une SUPPOSITION, jamais présentée autrement : « Sopra Steria » donne
    bien « sopra-steria », mais « Dassault Systèmes » s'écrit
    « dassaultsystemes » sans tiret sur LinkedIn. Rien ne permet de trancher
    sans ouvrir la page.
    """
    from core.models import normalize

    return re.sub(r"\s+", "-", normalize(nom).strip())


def _compte_depuis_post(url: str, deja: set[str]) -> None:
    """Résout le compte à suivre à partir de l'URL d'un post.

    Le seul cas où le slug est CERTAIN : il est écrit dans l'adresse même
    (`/posts/<auteur>_…`). Partout ailleurs il faut le deviner ou le relever
    dans une offre — voir `cmd_comptes`.

    Le post est lu au passage, hors session, pour deux raisons : montrer ce
    qu'on s'apprête à suivre, et passer le texte au même discriminateur
    recruteur/candidat que la collecte. Suivre un candidat n'aurait aucun
    intérêt : c'est un concurrent, pas un employeur.
    """
    import httpx

    from collectors.linkedin_posts import (_LIEN_POST, est_offre_recruteur,
                                           extraire_emails, lire_post,
                                           normalize_accents)

    trouve = _LIEN_POST.search(url)
    if not trouve:
        log.error("ce lien n'est pas un post LinkedIn — attendu une adresse "
                  "de la forme /posts/<auteur>_…-activity-<id>-<code>")
        return

    auteur_slug = trouve.group(1)
    entete = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36",
              "Accept-Language": "fr-FR,fr;q=0.9"}
    with httpx.Client(follow_redirects=True, timeout=25, headers=entete) as client:
        post = lire_post(url.split("?", 1)[0], client)

    texte = post.get("texte", "")
    if not texte:
        log.warning("post illisible (supprimé, privé, ou LinkedIn a servi un "
                    "mur de connexion) — le compte reste suggérable")

    print(f"\n  {post.get('auteur') or auteur_slug}")
    if texte:
        extrait = " ".join(texte.split())
        print(f"  {extrait[:200]}{'…' if len(extrait) > 200 else ''}\n")
        if est_offre_recruteur(normalize_accents(texte)):
            print("  → offre de RECRUTEUR : le filtre de collecte la retient.")
        else:
            print("  → post de CANDIDAT selon le filtre : un concurrent, pas")
            print("    un employeur. Suivre ce compte n'a probablement aucun")
            print("    intérêt — vérifie avant de l'ajouter.")
        contacts = extraire_emails(texte)
        if contacts:
            print(f"  → contact direct : {', '.join(contacts)}")

    # `/posts/<slug>` ne dit pas si le slug est une personne ou une page
    # d'entreprise, et LinkedIn ne permet pas de le vérifier (999 en anonyme).
    # On propose la forme « in/ », de loin la plus fréquente pour un lien de
    # post partagé, en signalant l'autre.
    entree = f"in/{auteur_slug}"
    if entree.lower() in deja:
        print(f"\n  Déjà suivi : {entree}\n")
        return
    print("\n  À ajouter sous `posts.comptes_suivis` dans config.yaml :\n")
    print(f'    - "{entree}"')
    print(f'\n  (si c\'est une page d\'entreprise et non un profil, écris '
          f'plutôt "company/{auteur_slug}")\n')


def cmd_comptes(args, cfg: dict, store: Store) -> None:
    """Suggère des pages d'entreprise à surveiller dans `posts.comptes_suivis`.

    Le collecteur de posts sait déjà relever une page d'entreprise
    (`compte()`, appelé avant toute recherche parce qu'il coûte une page vue
    au lieu d'une requête au moteur de contenu). Ce qui manquait, c'est de
    savoir LESQUELLES suivre. On les déduit de la base : une entreprise qui
    a déjà publié dix alternances de développement en publiera d'autres.

    Aucune requête réseau — ni LinkedIn, ni ailleurs.
    """
    p = cfg.get("posts", {})
    deja = {c.strip().strip("/").lower() for c in p.get("comptes_suivis", [])}

    # Un post précis l'emporte : son auteur est le compte à suivre, et son
    # slug est le seul qu'on connaisse avec certitude.
    if getattr(args, "post", None):
        _compte_depuis_post(args.post.strip(), deja)
        return

    employeurs = store.employeurs_recurrents(
        score_min=args.score_min, minimum=args.minimum, limite=args.limite)
    if not employeurs:
        print(f"\n  Aucun employeur avec au moins {args.minimum} alternances "
              f"à score ≥ {args.score_min}.")
        print("  Collecte davantage, ou abaisse --score-min / --minimum.\n")
        return

    connus = store.slugs_linkedin()
    confirmes, devines = [], []
    for e in employeurs:
        slug = connus.get((e["company"] or "").strip().lower())
        entree = f"company/{slug or _slugifier(e['company'])}"
        if entree.lower() in deja:
            continue
        (confirmes if slug else devines).append((entree, e))

    print(f"\n  {len(confirmes) + len(devines)} employeurs à surveiller "
          f"(≥ {args.minimum} alternances, score ≥ {args.score_min})")
    print(f"  {len(deja)} déjà dans comptes_suivis\n")

    if confirmes:
        print("  SLUG CONFIRMÉ — relevé dans une offre déjà collectée\n")
        for entree, e in confirmes:
            print(f'    - "{entree}"'.ljust(46)
                  + f"# {e['offres']} offres, score moyen {e['moyenne']} "
                    f"· {e['company'][:32]}")
        print()

    if devines:
        print("  SLUG SUPPOSÉ — déduit du nom, À VÉRIFIER avant de l'ajouter :")
        print("  ouvre la page, le slug est dans l'adresse.\n")
        for entree, e in devines:
            print(f'    - "{entree}"'.ljust(46)
                  + f"# {e['offres']} offres, score moyen {e['moyenne']} "
                    f"· {e['company'][:32]}")
        print()

    print("  À recopier sous `posts.comptes_suivis` dans config.yaml.")
    print("  Une page d'entreprise coûte UNE page vue, là où une recherche")
    print("  sollicite le moteur de contenu — c'est lui qui fait couper.\n")


def cmd_contacts(args, cfg: dict, store: Store) -> None:
    """Toutes les pistes joignables DIRECTEMENT, lien et adresse en clair.

    C'est le gisement que le digest dilue : les posts LinkedIn portent l'adresse
    du recruteur, PASS celle du service, et le marché caché un téléphone. Les
    voir ensemble, triés, évite de les chercher carte par carte.
    """
    import json

    lignes = store.selection(score_min=args.score_min, alternance_seulement=True,
                             limite=400, age_max_heures=fenetre_affichage(args),
                             tri="date")
    # Le marché caché n'a pas de date : il sort du filtre de fraîcheur, mais
    # ses 900 téléphones sont justement l'essentiel de ce qu'on cherche ici.
    if not args.sans_marche_cache:
        lignes = list(lignes) + list(store.selection(
            score_min=args.score_min, alternance_seulement=True, limite=200,
            sources=["lba_entreprise"], tri="score"))

    mails, tels = [], []
    for o in lignes:
        for c in json.loads(o["contacts"] or "[]"):
            (mails if "@" in c else tels).append((c, o))

    if mails:
        print(f"\n  ADRESSES E-MAIL — {len(mails)} pistes\n")
        for adresse, o in mails:
            print(f"  {adresse}")
            print(f"     {(o['title'] or '')[:66]}")
            print(f"     {o['company']} · {o['posted_at'] or 'sans date'} "
                  f"· score {o['score']}")
            print(f"     {o['url']}\n")

    if tels and not args.mails_seulement:
        print(f"  TÉLÉPHONES — {len(tels)} pistes (marché caché)\n")
        for numero, o in tels[:args.limite_tel]:
            print(f"  {numero:<16} {(o['company'] or '')[:42]:<42} "
                  f"score {o['score']}")
        if len(tels) > args.limite_tel:
            print(f"  … et {len(tels) - args.limite_tel} autres "
                  f"(--limite-tel pour en voir plus)")
        print()

    if not mails and not tels:
        print("\n  Aucun contact direct dans cette fenêtre.")
        print("  Les posts LinkedIn en sont la meilleure source : "
              ".\\radar.ps1 posts\n")


def cmd_stats(args, cfg: dict, store: Store) -> None:
    s = store.stats()
    print(f"\n  {s['total']} offres en base · {s['alternances']} alternances confirmées")
    for statut, n in sorted(s["par_statut"].items()):
        print(f"    {statut:<12} {n}")
    print()


def cmd_mark(args, cfg: dict, store: Store) -> None:
    """Marque une offre, désignée par identifiant OU par un bout de son nom."""
    trouves = store.resoudre(args.uid)

    if not trouves:
        print(f"\n  Aucune offre ne correspond à « {args.uid} ».\n")
        return

    if len(trouves) > 1:
        print(f"\n  {len(trouves)} offres correspondent à « {args.uid} ». "
              f"Précise, ou reprends un identifiant :\n")
        for r in trouves:
            statut = "" if r["status"] == "new" else f"  [{r['status']}]"
            print(f'    {r["uid"]:<26} {r["score"]:>4}  '
                  f'{r["title"][:44]:<44} | {(r["company"] or "")[:20]}{statut}')
        print()
        return

    offre = trouves[0]
    store.set_status(offre["uid"], args.statut, args.notes)
    print(f'\n  {offre["title"][:60]}')
    print(f'  {offre["company"]} → {args.statut}')
    if args.notes:
        print(f"  note : {args.notes}")
    print()


def cmd_suivi(args, cfg: dict, store: Store) -> None:
    """Tableau de bord des candidatures."""
    etapes = [("shortlisted", "À traiter"), ("applied", "Candidature envoyée"),
              ("rejected", "Écartées"), ("seen", "Vues, non retenues")]

    for statut, libelle in etapes:
        lignes = store.selection(statut=statut, score_min=-999,
                                 alternance_seulement=False, limite=100,
                                 inclure_exclus=True)
        if not lignes:
            continue
        print(f"\n  {libelle} ({len(lignes)})")
        print("  " + "─" * 74)
        for r in lignes:
            print(f'    {r["score"]:>4}  {r["title"][:44]:<44} | '
                  f'{(r["company"] or "")[:22]}')
            if r["notes"]:
                print(f'          ↳ {r["notes"][:70]}')
            print(f'          {r["uid"]}')

    stats = store.stats()
    restant = stats["par_statut"].get("new", 0)
    print(f"\n  {restant} offres jamais traitées.\n")


def main() -> None:
    p = argparse.ArgumentParser(description="Radar d'offres d'alternance")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="commande", required=True)

    ca = sub.add_parser("canari",
                        help="vérifie que chaque collecteur ramène de la donnée")
    ca.add_argument("--historique", action="store_true",
                    help="affiche aussi les contrôles passés")
    ca.set_defaults(fn=cmd_canari)

    t = sub.add_parser("tout", help="toutes les sources d'un coup + digest")
    _ajouter_depuis(t)
    t.add_argument("--avec-posts", action="store_true",
                   help="ajoute les posts LinkedIn (ouvre Chrome, session "
                        "requise — à ne pas mettre en tâche planifiée)")
    t.set_defaults(fn=cmd_tout)

    c = sub.add_parser("collect", help="offres LinkedIn (endpoint invité)")
    _ajouter_depuis(c)
    c.add_argument("--limite-requetes", type=int, default=0,
                   help="ne lance que les N premières requêtes (test)")
    c.add_argument("--max-details", type=int, default=0,
                   help="plafonne le nb de fiches détaillées (surcharge le config)")
    c.set_defaults(fn=cmd_collect)

    r = sub.add_parser("report", help="génère le digest HTML")
    _ajouter_depuis(r)
    r.add_argument("--score-min", type=int, default=0)
    r.add_argument("--statut", choices=STATUTS, default=None)
    r.add_argument("--max-age", type=int, default=None, metavar="JOURS",
                   help="n'affiche que les offres publiées depuis N jours au "
                        "plus. Vérification stricte : une offre sans date est "
                        "écartée, puisque son âge est invérifiable.")
    r.add_argument("--tri", choices=list(Store.TRIS), default="score",
                   help="ordre d'affichage (défaut : score)")
    r.set_defaults(fn=cmd_report)

    sv = sub.add_parser("serve", help="sert le digest avec le suivi de "
                                      "candidature actif")
    _ajouter_depuis(sv)
    sv.add_argument("--score-min", type=int, default=20)
    sv.add_argument("--statut", choices=STATUTS, default=None)
    # 24 h par défaut, et non 14 jours : `serve` est l'outil quotidien, il
    # suit une collecte `--depuis jour`. Afficher deux semaines par défaut
    # faisait apparaître des annonces de huit jours dans ce qu'on croyait
    # être la vue du jour. La fenêtre s'élargit d'un clic dans le panneau.
    sv.add_argument("--max-age", type=int, default=1, metavar="JOURS")
    sv.add_argument("--tri", choices=list(Store.TRIS), default="score")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--sans-navigateur", action="store_true",
                    help="n'ouvre pas le navigateur automatiquement")
    sv.set_defaults(fn=cmd_serve)

    hw = sub.add_parser("hellowork", help="collecte les offres HelloWork")
    _ajouter_depuis(hw)
    hw.add_argument("--max-details", type=int, default=0)
    hw.set_defaults(fn=cmd_hellowork)

    ind = sub.add_parser("indeed", help="collecte les offres Indeed")
    _ajouter_depuis(ind)
    ind.add_argument("--max-details", type=int, default=0)
    ind.set_defaults(fn=cmd_indeed)

    jt = sub.add_parser("jobteaser", help="collecte les offres JobTeaser")
    _ajouter_depuis(jt)
    jt.add_argument("--max-details", type=int, default=0)
    jt.set_defaults(fn=cmd_jobteaser)

    wt = sub.add_parser("wttj", help="collecte les offres Welcome to the Jungle")
    _ajouter_depuis(wt)
    wt.add_argument("--max-details", type=int, default=0)
    wt.set_defaults(fn=cmd_wttj)

    pa = sub.add_parser("pass", help="PASS — apprentissage fonction publique")
    _ajouter_depuis(pa)
    pa.set_defaults(fn=cmd_pass)

    gd = sub.add_parser("glassdoor", help="collecte les offres Glassdoor")
    _ajouter_depuis(gd)
    gd.add_argument("--max-details", type=int, default=0)
    gd.set_defaults(fn=cmd_glassdoor)

    ad = sub.add_parser("adopte1dev", help="adopte1dev — job board 100 %% dev")
    _ajouter_depuis(ad)
    ad.set_defaults(fn=cmd_adopte1dev)

    dj = sub.add_parser("devitjobs", help="DevITjobs.fr — flux RSS, IT généraliste")
    _ajouter_depuis(dj)
    dj.set_defaults(fn=cmd_devitjobs)

    wd = sub.add_parser("welovedevs", help="WeLoveDevs — recherche Algolia, filtre alternance")
    _ajouter_depuis(wd)
    wd.set_defaults(fn=cmd_welovedevs)

    lb = sub.add_parser("lba", help="La Bonne Alternance (offres + marché caché)")
    _ajouter_depuis(lb)
    lb.set_defaults(fn=cmd_lba)

    lg = sub.add_parser("login", help="établit la session LinkedIn (une fois)")
    lg.set_defaults(fn=cmd_login)

    po = sub.add_parser("posts", help="collecte les posts du fil LinkedIn")
    _ajouter_depuis(po)
    po.add_argument("--force", action="store_true",
                    help="passe outre le délai minimum entre deux collectes "
                         "(risque de coupure de la recherche par LinkedIn)")
    po.set_defaults(fn=cmd_posts)

    pw = sub.add_parser("posts-web", help="posts LinkedIn via DuckDuckGo "
                                          "(sans session, sans risque de coupure)")
    _ajouter_depuis(pw)
    pw.set_defaults(fn=cmd_posts_web)

    rs = sub.add_parser("rescore", help="re-score la base après édition du config")
    rs.set_defaults(fn=cmd_rescore)

    po = sub.add_parser("postuler", help="ouvre une offre et pré-remplit son "
                                         "formulaire (n'envoie rien)")
    po.add_argument("uid", help="identifiant de l'offre, ou un bout de son nom")
    po.set_defaults(fn=cmd_postuler)

    cv = sub.add_parser("cv", help="génère un CV LaTeX adapté à une offre "
                                   "(uid, bout de nom, ou lien direct)")
    cv.add_argument("uid", help="identifiant de l'offre, un bout de son nom, "
                                "ou l'URL de l'offre (collectée ou non)")
    cv.set_defaults(fn=cmd_cv)

    cp = sub.add_parser("comptes", help="quels comptes surveiller "
                                        "dans posts.comptes_suivis")
    cp.add_argument("--post", metavar="URL", default=None,
                    help="l'URL d'un post : suggère d'en suivre l'AUTEUR "
                         "(son slug est certain, il est dans l'adresse)")
    cp.add_argument("--score-min", type=int, default=40,
                    help="score minimum d'une offre pour compter (défaut : 40)")
    cp.add_argument("--minimum", type=int, default=4, metavar="N",
                    help="nombre d'alternances pour retenir un employeur (défaut : 4)")
    cp.add_argument("--limite", type=int, default=30)
    cp.set_defaults(fn=cmd_comptes)

    ct = sub.add_parser("contacts", help="les pistes joignables directement : "
                                         "lien du post et adresse e-mail")
    _ajouter_depuis(ct)
    ct.add_argument("--score-min", type=int, default=0)
    ct.add_argument("--max-age", type=int, default=None, metavar="JOURS")
    ct.add_argument("--mails-seulement", action="store_true")
    ct.add_argument("--sans-marche-cache", action="store_true")
    ct.add_argument("--limite-tel", type=int, default=25)
    ct.set_defaults(fn=cmd_contacts)

    s = sub.add_parser("stats", help="état de la base")
    s.set_defaults(fn=cmd_stats)

    su = sub.add_parser("suivi", help="tableau de bord des candidatures")
    su.set_defaults(fn=cmd_suivi)

    m = sub.add_parser("mark", help="change le statut d'une offre")
    m.add_argument("uid", metavar="OFFRE",
                   help="identifiant (linkedin:123…) ou fragment de nom "
                        "d'entreprise / d'intitulé, ex. « wijin »")
    m.add_argument("statut", choices=STATUTS)
    m.add_argument("--notes", default=None)
    m.set_defaults(fn=cmd_mark)

    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = charger_config()
    store = Store(RACINE / "store" / "jobs.db")
    try:
        args.fn(args, cfg, store)
    finally:
        store.close()


if __name__ == "__main__":
    main()
