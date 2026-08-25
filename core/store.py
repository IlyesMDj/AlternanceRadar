"""Persistance SQLite et suivi de candidature.

Le suivi est la vraie valeur de l'outil : ne jamais revoir deux fois la même
offre, et savoir en un coup d'œil où on en est sur chacune.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Job

STATUTS = ("new", "seen", "shortlisted", "applied", "rejected")

# Paramètres de SUIVI uniquement — jamais un paramètre qui pourrait porter
# l'identité de l'offre. `jk` (Indeed), par exemple, n'y figure pas : c'est
# justement ce paramètre-là qui distingue une offre d'une autre sur ce site.
_PARAMS_SUIVI = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                 "utm_content", "tk", "from", "ref", "referrer"}


def _sans_suivi(url: str) -> str:
    """Normalise une URL pour comparaison : retire les paramètres de suivi
    connus, trie le reste. Deux copies du même lien, l'une avec `utm_*`
    l'autre sans, doivent produire la même chaîne — mais deux offres
    différentes du même site ne doivent JAMAIS produire la même chaîne."""
    partes = urlsplit((url or "").strip())
    requete = urlencode(sorted(
        (cle, val) for cle, val in parse_qsl(partes.query, keep_blank_values=True)
        if cle.lower() not in _PARAMS_SUIVI
    ))
    return urlunsplit((partes.scheme, partes.netloc, partes.path, requete, ""))

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid            TEXT PRIMARY KEY,
    dedup_key      TEXT NOT NULL,
    duplicate_of   TEXT,
    source         TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    title          TEXT NOT NULL,
    company        TEXT,
    location       TEXT,
    url            TEXT,
    posted_at      TEXT,
    posted_ts      TEXT,
    contract_type  TEXT,
    remote         TEXT,
    description    TEXT,
    tags           TEXT DEFAULT '[]',
    contacts       TEXT DEFAULT '[]',
    score          INTEGER DEFAULT 0,
    score_detail   TEXT,
    is_alternance  INTEGER DEFAULT 0,
    status         TEXT DEFAULT 'new',
    notes          TEXT DEFAULT '',
    exclu          TEXT DEFAULT '',
    detail_essais  INTEGER DEFAULT 0,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup  ON jobs(dedup_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score DESC);
"""


class Store:
    def __init__(self, chemin: Path):
        chemin.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(chemin)
        self.db.row_factory = sqlite3.Row
        # WAL : le serveur du digest lit pendant qu'une collecte écrit. En
        # journal classique, l'écrivain prend un verrou exclusif et toute
        # lecture échoue sur « database is locked » — la page serait
        # inconsultable pendant les minutes que dure une collecte. En WAL,
        # lecteurs et écrivain avancent en parallèle.
        self.db.execute("PRAGMA journal_mode=WAL")
        # Filet pour les rares moments où un verrou est réellement pris
        # (bascule de WAL, checkpoint) : attendre plutôt qu'échouer.
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)
        self._migrer()
        self.db.commit()

    def _migrer(self) -> None:
        """Ajoute les colonnes manquantes sur une base créée par une version
        antérieure — `CREATE TABLE IF NOT EXISTS` ne le fait pas."""
        colonnes = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for nom, definition in (("detail_essais", "INTEGER DEFAULT 0"),
                                ("contacts", "TEXT DEFAULT '[]'"),
                                ("exclu", "TEXT DEFAULT ''"),
                                ("posted_ts", "TEXT")):
            if nom not in colonnes:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {nom} {definition}")

    # -- écriture ---------------------------------------------------------

    def upsert(self, job: Job) -> bool:
        """Insère l'offre. Retourne True si c'est une nouveauté.

        Sur une offre déjà connue, seuls `last_seen` et les champs recalculés
        sont mis à jour : `status` et `notes` sont préservés, sinon un run
        écraserait le suivi de candidature.
        """
        maintenant = datetime.now().isoformat(timespec="seconds")
        existe = self.db.execute(
            "SELECT 1 FROM jobs WHERE uid = ?", (job.uid,)
        ).fetchone()

        if existe:
            self.db.execute(
                # `posted_ts` est rafraîchi comme les champs calculés : c'est
                # une donnée de la source, pas du suivi. Sans ça, les offres
                # déjà en base resteraient sans horodatage pour toujours et ne
                # bénéficieraient jamais du filtrage à l'heure près.
                # COALESCE : ne jamais écraser un horodatage connu par un vide.
                """UPDATE jobs SET last_seen = ?, score = ?, score_detail = ?,
                                   tags = ?, is_alternance = ?,
                                   posted_ts = COALESCE(?, posted_ts)
                   WHERE uid = ?""",
                (maintenant, job.score, job.score_detail,
                 json.dumps(job.tags, ensure_ascii=False),
                 int(job.is_alternance),
                 job.posted_ts.isoformat(timespec="seconds")
                 if job.posted_ts else None,
                 job.uid),
            )
            self.db.commit()
            return False

        # Même offre déjà vue via une autre source ?
        jumeau = self.db.execute(
            "SELECT uid FROM jobs WHERE dedup_key = ? LIMIT 1", (job.dedup_key,)
        ).fetchone()

        self.db.execute(
            """INSERT INTO jobs (uid, dedup_key, duplicate_of, source, external_id,
                                 title, company, location, url, posted_at,
                                 posted_ts, contract_type, remote, description,
                                 tags, contacts,
                                 score, score_detail, is_alternance, exclu,
                                 first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job.uid, job.dedup_key, jumeau["uid"] if jumeau else None,
             job.source, job.external_id, job.title, job.company, job.location,
             job.url, job.posted_at.isoformat() if job.posted_at else None,
             job.posted_ts.isoformat(timespec="seconds") if job.posted_ts else None,
             job.contract_type, job.remote, job.description,
             json.dumps(job.tags, ensure_ascii=False),
             json.dumps(job.contacts, ensure_ascii=False), job.score,
             job.score_detail, int(job.is_alternance), job.exclu,
             maintenant, maintenant),
        )
        self.db.commit()
        return jumeau is None  # un doublon inter-sources n'est pas une nouveauté

    def maj_horodatages(self, jobs) -> int:
        """Complète `posted_ts` sur les offres déjà en base.

        Les collecteurs n'appellent `upsert` que sur les offres NOUVELLES :
        sans ce complément, une offre déjà connue n'aurait jamais d'horodatage
        et resterait filtrée au jour pour toujours. Les upserter toutes serait
        pire — leur score serait recalculé depuis une coquille de recherche
        sans description, et s'effondrerait.

        On ne touche donc qu'à `posted_ts`, et seulement s'il est vide.
        """
        lignes = [(j.posted_ts.isoformat(timespec="seconds"), j.uid)
                  for j in jobs if j.posted_ts is not None]
        if not lignes:
            return 0
        cur = self.db.executemany(
            "UPDATE jobs SET posted_ts = ? WHERE uid = ? AND posted_ts IS NULL",
            lignes)
        self.db.commit()
        return cur.rowcount

    def resoudre(self, terme: str, limite: int = 12) -> list[sqlite3.Row]:
        """Retrouve une offre par identifiant exact, par URL, ou par fragment
        de texte.

        Recopier « linkedin:4450246254 » depuis le digest était assez pénible
        pour que le suivi ne soit jamais utilisé : 9 637 offres, aucune
        marquée. On accepte donc aussi un bout de nom d'entreprise ou
        d'intitulé — « wijin », « worldline devops » — et, plus utile encore,
        le LIEN de l'offre tel quel : c'est ce qu'on a sous la main en
        arrivant depuis le site plutôt que depuis le digest.
        """
        terme = terme.strip()
        exact = self.db.execute(
            "SELECT * FROM jobs WHERE uid = ?", (terme,)
        ).fetchone()
        if exact:
            return [exact]

        if terme.startswith(("http://", "https://")):
            # Filtre SQL large sur le chemin seul (rapide, imprécis), suivi
            # d'une comparaison EXACTE en Python après avoir retiré les seuls
            # paramètres de suivi CONNUS (utm_*...). Ne JAMAIS trancher toute
            # la requête au premier « ? », comme une version antérieure : sur
            # Indeed, l'identifiant réel de l'offre est lui-même en paramètre
            # (`jk=...`) — le couper faisait matcher la première offre Indeed
            # venue en base, pas celle demandée. Une donnée fausse qui a
            # l'air correcte est le pire résultat possible ici.
            partes = urlsplit(terme)
            prefixe = f"{partes.scheme}://{partes.netloc}{partes.path}"
            cible = _sans_suivi(terme)
            candidats = self.db.execute(
                "SELECT * FROM jobs WHERE url LIKE ? ESCAPE '\\'",
                (prefixe.replace("%", "\\%").replace("_", "\\_") + "%",),
            ).fetchall()
            for c in candidats:
                if _sans_suivi(c["url"] or "") == cible:
                    return [c]
            return []

        mots = [m for m in terme.split() if m]
        if not mots:
            return []
        # Tous les mots doivent apparaître, dans le titre ou l'entreprise.
        conditions = " AND ".join(
            "(title LIKE ? OR company LIKE ?)" for _ in mots
        )
        params: list = []
        for mot in mots:
            params.extend([f"%{mot}%", f"%{mot}%"])
        params.append(limite)
        return self.db.execute(
            f"SELECT * FROM jobs WHERE duplicate_of IS NULL AND {conditions} "
            f"ORDER BY score DESC LIMIT ?",
            params,
        ).fetchall()

    def set_status(self, uid: str, statut: str, notes: str | None = None) -> bool:
        if statut not in STATUTS:
            raise ValueError(f"statut inconnu : {statut} (attendu : {STATUTS})")
        cur = (
            self.db.execute("UPDATE jobs SET status = ?, notes = ? WHERE uid = ?",
                            (statut, notes, uid))
            if notes is not None
            else self.db.execute("UPDATE jobs SET status = ? WHERE uid = ?",
                                 (statut, uid))
        )
        self.db.commit()
        return cur.rowcount > 0

    # -- lecture ----------------------------------------------------------

    def ids_connus(self, source: str) -> set[str]:
        """Identifiants déjà en base pour une source — évite de refetcher."""
        rows = self.db.execute(
            "SELECT external_id FROM jobs WHERE source = ?", (source,)
        ).fetchall()
        return {r["external_id"] for r in rows}

    # En-deçà de ce nombre de caractères, la fiche détaillée n'a pas été
    # récupérée : HelloWork pré-remplit la description avec la seule durée
    # du contrat, un test « description vide » les manquerait toutes.
    SEUIL_DESCRIPTION = 200

    def sans_description(self, source: str, limite: int) -> list[Job]:
        """Offres enregistrées dont la fiche détaillée n'a jamais été récupérée.

        Quand un run bute sur `max_details_par_run`, les offres excédentaires
        sont stockées scorées sur leur seul titre. Elles reviennent ici pour
        être complétées au run suivant : rien n'est définitivement perdu.
        """
        rows = self.db.execute(
            """SELECT * FROM jobs
               WHERE source = ? AND length(COALESCE(description, '')) < ?
                 AND detail_essais < 3
               ORDER BY score DESC LIMIT ?""",
            (source, self.SEUIL_DESCRIPTION, limite),
        ).fetchall()
        return [
            Job(source=r["source"], external_id=r["external_id"], title=r["title"],
                company=r["company"] or "", location=r["location"] or "",
                url=r["url"] or "", contract_type=r["contract_type"])
            for r in rows
        ]

    def tous(self) -> list[Job]:
        """Toutes les offres en base, descriptions comprises.

        Permet de re-scorer l'intégralité de la base après modification de
        `config.yaml`, sans relancer la moindre requête vers LinkedIn.
        """
        return [
            Job(source=r["source"], external_id=r["external_id"], title=r["title"],
                company=r["company"] or "", location=r["location"] or "",
                url=r["url"] or "", contract_type=r["contract_type"],
                description=r["description"] or "",
                contacts=json.loads(r["contacts"] or "[]"),
                titre_est_intitule=not r["source"].endswith("_post"))
            for r in self.db.execute("SELECT * FROM jobs")
        ]

    def recalculer_doublons(self) -> int:
        """Recalcule les clés de rapprochement et re-marque les doublons.

        Indispensable après tout changement de `Job.dedup_key` : les clés déjà
        stockées l'ont été avec l'ancienne formule, et `duplicate_of` a été
        figé à l'insertion. Dans chaque groupe on conserve la ligne vue en
        premier ; les autres pointent vers elle.
        """
        for job in self.tous():
            self.db.execute("UPDATE jobs SET dedup_key = ? WHERE uid = ?",
                            (job.dedup_key, job.uid))

        canonique = ("SELECT j2.uid FROM jobs j2 WHERE j2.dedup_key = jobs.dedup_key "
                     "ORDER BY j2.first_seen, j2.uid LIMIT 1")
        self.db.execute("UPDATE jobs SET duplicate_of = NULL")
        cur = self.db.execute(
            f"UPDATE jobs SET duplicate_of = ({canonique}) WHERE uid <> ({canonique})"
        )
        self.db.commit()
        return cur.rowcount

    def maj_score(self, job: Job) -> None:
        """Met à jour uniquement les champs calculés."""
        self.db.execute(
            "UPDATE jobs SET score = ?, score_detail = ?, tags = ?, "
            "is_alternance = ?, exclu = ? WHERE uid = ?",
            (job.score, job.score_detail, json.dumps(job.tags, ensure_ascii=False),
             int(job.is_alternance), job.exclu, job.uid),
        )

    def maj_description(self, job: Job) -> None:
        """Complète une offre déjà en base après récupération de sa fiche.

        Le compteur d'essais est incrémenté même en cas d'échec : une offre
        expirée (404) sort du backlog au bout de trois tentatives au lieu de
        le bloquer indéfiniment.
        """
        self.db.execute(
            """UPDATE jobs SET description = ?, contract_type = ?, tags = ?,
                               score = ?, score_detail = ?, is_alternance = ?,
                               last_seen = ?, detail_essais = detail_essais + 1
               WHERE uid = ?""",
            (job.description, job.contract_type,
             json.dumps(job.tags, ensure_ascii=False), job.score, job.score_detail,
             int(job.is_alternance), datetime.now().isoformat(timespec="seconds"),
             job.uid),
        )
        self.db.commit()

    TRIS = {
        "score": "score DESC, posted_at DESC",
        "date": "posted_at DESC, score DESC",
        "entreprise": "company COLLATE NOCASE ASC, score DESC",
        "ville": "location COLLATE NOCASE ASC, score DESC",
    }

    def selection(self, statut: str | None = None, score_min: int = 0,
                  alternance_seulement: bool = True, limite: int = 200,
                  sources: list[str] | None = None,
                  exclure_sources: list[str] | None = None,
                  age_max_jours: int | None = None,
                  age_max_heures: int | None = None,
                  tri: str = "score",
                  inclure_exclus: bool = False,
                  exclure_statuts: list[str] | None = None) -> list[sqlite3.Row]:
        conditions = ["duplicate_of IS NULL", "score >= ?"]
        params: list = [score_min]
        if statut:
            conditions.append("status = ?")
            params.append(statut)
        if alternance_seulement:
            conditions.append("is_alternance = 1")
        if not inclure_exclus:
            conditions.append("COALESCE(exclu, '') = ''")
        if exclure_statuts:
            conditions.append(
                f"status NOT IN ({','.join('?' * len(exclure_statuts))})")
            params.extend(exclure_statuts)
        if sources:
            conditions.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)
        if exclure_sources:
            conditions.append(f"source NOT IN ({','.join('?' * len(exclure_sources))})")
            params.extend(exclure_sources)
        if age_max_heures is not None:
            # Fenêtre à l'heure près, quand la source la permet.
            #
            # PASS, WTTJ, Indeed et JobTeaser publient un horodatage complet :
            # pour eux « 24 h » veut dire 24 h. LinkedIn, HelloWork et
            # Glassdoor ne donnent qu'une date, et une offre datée d'hier a
            # entre 1 et 47 heures — impossible de trancher. On leur applique
            # donc la comparaison au jour, en repli explicite.
            depuis = datetime.now() - timedelta(hours=age_max_heures)
            conditions.append(
                "posted_at IS NOT NULL AND ("
                "  (posted_ts IS NOT NULL AND posted_ts >= ?)"
                "  OR (posted_ts IS NULL AND posted_at >= ?))"
            )
            params.extend([depuis.isoformat(timespec="seconds"),
                           depuis.date().isoformat()])
        elif age_max_jours is not None:
            # Vérification stricte : une offre sans date de publication est
            # écartée, puisqu'on ne peut justement PAS vérifier son âge.
            limite_date = (date.today() - timedelta(days=age_max_jours)).isoformat()
            conditions.append("posted_at IS NOT NULL AND posted_at >= ?")
            params.append(limite_date)
        params.append(limite)
        return self.db.execute(
            f"""SELECT * FROM jobs WHERE {' AND '.join(conditions)}
                ORDER BY {self.TRIS.get(tri, self.TRIS['score'])} LIMIT ?""",
            params,
        ).fetchall()

    def sources_disponibles(self) -> list[tuple[str, int]]:
        """Sources présentes en base, avec le nombre d'alternances retenues.

        Alimente les filtres du digest : proposer une case pour une source
        absente, ou sans rien de retenu, ne ferait qu'encombrer.
        """
        return [(r["source"], r["n"]) for r in self.db.execute(
            """SELECT source, COUNT(*) AS n FROM jobs
                WHERE is_alternance = 1 AND COALESCE(exclu, '') = ''
                  AND duplicate_of IS NULL
                GROUP BY source ORDER BY n DESC""")]

    def compte_sans_date(self, age_max_jours: int) -> int:
        """Offres réellement écartées faute de date vérifiable.

        Les entreprises du marché caché sont hors du compte : elles n'ont pas
        de date de publication par nature, ne sont jamais filtrées par âge, et
        s'affichent dans leur propre section. Les inclure ferait annoncer
        plus de 2 400 « écartées » alors qu'elles sont toutes présentes.
        """
        return self.db.execute(
            "SELECT COUNT(*) c FROM jobs WHERE duplicate_of IS NULL "
            "AND is_alternance = 1 AND COALESCE(exclu,'') = '' "
            "AND source <> 'lba_entreprise' AND posted_at IS NULL"
        ).fetchone()["c"]

    def exclusions_par_motif(self) -> dict[str, int]:
        return {
            r["exclu"]: r["c"]
            for r in self.db.execute(
                "SELECT exclu, COUNT(*) c FROM jobs WHERE COALESCE(exclu,'') <> '' "
                "AND duplicate_of IS NULL GROUP BY exclu ORDER BY c DESC"
            )
        }

    def stats(self) -> dict:
        total = self.db.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
        alt = self.db.execute(
            "SELECT COUNT(*) c FROM jobs WHERE is_alternance = 1 "
            "AND duplicate_of IS NULL AND COALESCE(exclu,'') = ''"
        ).fetchone()["c"]
        exclus = self.db.execute(
            "SELECT COUNT(*) c FROM jobs WHERE duplicate_of IS NULL "
            "AND COALESCE(exclu,'') <> ''"
        ).fetchone()["c"]
        par_statut = {
            r["status"]: r["c"]
            for r in self.db.execute(
                "SELECT status, COUNT(*) c FROM jobs WHERE duplicate_of IS NULL GROUP BY status"
            )
        }
        return {"total": total, "alternances": alt, "exclus": exclus,
                "par_statut": par_statut}

    def close(self) -> None:
        self.db.close()


def sauver_brut(racine: Path, source: str, identifiant: str, contenu: str) -> None:
    """Archive la réponse brute.

    Quand LinkedIn changera son HTML — ça arrivera — on pourra re-parser
    l'historique sans avoir à re-scraper.
    """
    dossier = racine / source / date.today().isoformat()
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / f"{identifiant}.html").write_text(contenu, encoding="utf-8")
