"""Tests canari — détecter qu'un collecteur s'est cassé, sans attendre.

Le défaut le plus coûteux d'un scraper n'est pas de tomber en panne : c'est
de **réussir en silence**. Un site change son HTML, le parseur ne trouve plus
rien, et le programme rapporte tranquillement « 0 offre » — indiscernable
d'une journée sans publication.

Trois pannes réelles de ce projet, toutes passées inaperçues sur le moment :

- LinkedIn a refondu son DOM : « 0 carte » pendant deux runs complets ;
- HelloWork renvoyait la page entière au lieu de la description, ce qui a
  fait exclure ses 363 offres comme des CDI ;
- la page d'un compte n'exposant pas les mêmes attributs, on a enregistré
  des coquilles vides signées « S'identifier sur LinkedIn ».

D'où le principe, emprunté aux bonnes pratiques du domaine : **on ne valide
pas le code HTTP, on valide la donnée extraite**. Un volume plausible, et des
champs obligatoires réellement remplis.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS canari (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    quand    TEXT NOT NULL,
    source   TEXT NOT NULL,
    contexte TEXT NOT NULL DEFAULT '',
    ok       INTEGER NOT NULL,
    nb       INTEGER NOT NULL,
    champs   TEXT NOT NULL DEFAULT '{}',
    message  TEXT
);
CREATE INDEX IF NOT EXISTS idx_canari_source ON canari(source, contexte, quand DESC);
"""

# Deux natures de relevé, qui n'ont aucune raison d'avoir le même volume :
# une SONDE ne charge qu'une page, une COLLECTE pagine jusqu'au bout. Les
# comparer entre elles produisait une alerte « volume en chute » à chaque
# sonde suivant une collecte — 10 offres contre 3280, soit « 100 % de moins ».
SONDE = "sonde"
COLLECTE = "collecte"


def _preparer(db: sqlite3.Connection) -> None:
    """Crée la table et ajoute `contexte` sur une base antérieure."""
    db.executescript(SCHEMA)
    colonnes = {r[1] for r in db.execute("PRAGMA table_info(canari)")}
    if "contexte" not in colonnes:
        db.execute("ALTER TABLE canari ADD COLUMN contexte TEXT NOT NULL DEFAULT ''")
        db.commit()

# Ce qu'une source en bonne santé doit produire. `min` est délibérément bas :
# on cherche à distinguer « cassé » de « peu de résultats », pas à mesurer.
ATTENDUS: dict[str, dict] = {
    "linkedin":       {"min": 5,  "champs": ("title", "company", "url")},
    "hellowork":      {"min": 15, "champs": ("title", "company", "url", "posted_at")},
    "indeed":         {"min": 10, "champs": ("title", "company", "url", "posted_at",
                                             "contract_type")},
    "lba":            {"min": 5,  "champs": ("title", "company", "url")},
    "lba_entreprise": {"min": 20, "champs": ("title", "company", "url")},
    "linkedin_post":  {"min": 1,  "champs": ("title", "company", "description")},
    # La sonde complète 10 fiches : le seuil laisse une marge sous ce chiffre
    # pour ne pas crier au loup si une fiche a expiré entre-temps.
    "jobteaser":      {"min": 8,  "champs": ("title", "company", "url", "posted_at")},
    # WTTJ est contrôlé sur la recherche, pas après complétion : l'index
    # Algolia rend déjà le contrat, donc `contract_type` doit être plein à
    # 100 %. S'il chute, c'est que la facette serveur a changé de nom — et
    # c'est exactement le genre de panne silencieuse qu'on veut voir.
    "wttj":           {"min": 20, "champs": ("title", "company", "url",
                                             "posted_at", "contract_type")},
    # Le flux PASS publie 68 offres et ne pagine pas : le seuil vise la
    # rupture du flux, pas son volume, qui ne dépend pas de la fenêtre.
    "pass":           {"min": 25, "champs": ("title", "company", "url",
                                             "posted_at", "description")},
    # Glassdoor : 30 offres par mot-clé, une page autorisée. `posted_at` est
    # absent des cartes sponsorisées, d'où son exclusion des champs requis —
    # l'exiger ferait sonner le canari sur un comportement normal du site.
    "glassdoor":      {"min": 20, "champs": ("title", "company", "url")},
    # adopte1dev tient tout dans une réponse d'API : titre, entreprise,
    # date horodatée et contrat serveur. Tous les champs doivent être pleins.
    "adopte1dev":     {"min": 15, "champs": ("title", "company", "url",
                                             "posted_at", "contract_type")},
    # Flux RSS complet, non filtré : le volume mesure tout le board IT, pas
    # seulement l'alternance. `contract_type` en est absent par construction
    # (classify.py tranche sur le titre) et n'est donc pas exigé ici.
    "devitjobs":      {"min": 80, "champs": ("title", "company", "url",
                                             "posted_at")},
    # Facet serveur sur un board de niche : le volume national mesuré est de
    # 1 à 2 offres. Le seuil vise la rupture du filtre, pas un volume — en
    # dessous, `min` resterait honnête même à zéro nouvelle offre un jour donné.
    "welovedevs":     {"min": 1, "champs": ("title", "company", "url",
                                             "contract_type")},
}

# En-deçà, un champ obligatoire est considéré comme cassé plutôt que creux.
SEUIL_COMPLETUDE = 0.80

# Les seuils ci-dessus sont calibrés sur la fenêtre pleine (14 jours). Sur
# `--depuis jour`, Indeed et HelloWork filtrent côté serveur et rendent
# légitimement dix fois moins : juger au même seuil faisait sonner le canari
# tous les jours — 9 offres pour 10 attendues. Un moniteur qui crie au loup
# chaque matin finit ignoré, ce qui est pire que pas de moniteur du tout.
FENETRE_PLEINE = 14

# Sous cet effectif, un taux de complétude ne veut plus rien dire : sur 20
# offres, une seule annonce sponsorisée non datée déplace le taux de 5 points.
# Mesuré : HelloWork en fenêtre 24 h renvoie 20 offres dont 7 non datées, soit
# 65 % — non pas parce que le parseur casse, mais parce que HelloWork ne date
# pas ses cartes promues, et qu'une fenêtre étroite écarte les offres datées.
ECHANTILLON_MINIMAL = 25


@dataclass
class Constat:
    source: str
    ok: bool
    nb: int
    champs: dict[str, float] = field(default_factory=dict)
    message: str = ""
    # Un lot trop maigre pour conclure n'est pas un lot cassé. Les distinguer
    # est précisément ce que ce canari est censé faire.
    #
    # `raison` dit POURQUOI on ne conclut pas, et les deux cas n'ont pas la
    # même gravité : « volume » mérite un avertissement — la source a rendu
    # moins que prévu, ça se regarde ; « echantillon » ne mérite rien du tout
    # — le lot est simplement trop petit pour qu'un pourcentage ait un sens.
    indecis: bool = False
    raison: str = ""

    @property
    def symbole(self) -> str:
        if self.indecis:
            return "?   "
        return "OK  " if self.ok else "CASSÉ"


def _rempli(job: Job, champ: str) -> bool:
    valeur = getattr(job, champ, None)
    return bool(valeur) and (not isinstance(valeur, str) or valeur.strip() != "")


def _minimum(regle: dict, fenetre_jours: int | None) -> int:
    """Ramène le seuil de volume à la fenêtre réellement demandée."""
    if not fenetre_jours or fenetre_jours >= FENETRE_PLEINE:
        return regle["min"]
    return max(1, round(regle["min"] * fenetre_jours / FENETRE_PLEINE))


def controler(source: str, jobs: list[Job],
              fenetre_jours: int | None = None) -> Constat:
    """Valide un lot fraîchement collecté.

    `fenetre_jours` est la fenêtre de fraîcheur demandée au collecteur. Sans
    elle, un run quotidien serait jugé aux seuils d'un run de deux semaines.
    """
    regle = ATTENDUS.get(source, {"min": 1, "champs": ("title",)})
    nb = len(jobs)
    minimum = _minimum(regle, fenetre_jours)

    if nb < minimum:
        # Zéro offre est un verdict ; « moins que prévu » n'en est pas un.
        casse = nb == 0
        return Constat(source, False, nb, {},
                       f"{nb} offres, {minimum} attendues au minimum — "
                       + ("parseur probablement cassé, ou source bloquée"
                          if casse else
                          "trop peu pour conclure sur cette fenêtre"),
                       indecis=not casse, raison="" if casse else "volume")

    taux = {c: sum(_rempli(j, c) for j in jobs) / nb for c in regle["champs"]}
    detail = ", ".join(f"{c} rempli à {t:.0%}" for c, t in taux.items()
                       if t < SEUIL_COMPLETUDE)

    if detail and nb < ECHANTILLON_MINIMAL:
        return Constat(source, True, nb, taux,
                       f"{nb} offres — échantillon trop petit pour juger "
                       f"la complétude ({detail})",
                       indecis=True, raison="echantillon")
    if detail:
        return Constat(source, False, nb, taux,
                       "champs obligatoires incomplets : " + detail)

    return Constat(source, True, nb, taux, f"{nb} offres, tous champs remplis")


def enregistrer(db: sqlite3.Connection, constat: Constat,
                contexte: str = COLLECTE) -> None:
    _preparer(db)
    db.execute(
        "INSERT INTO canari (quand, source, contexte, ok, nb, champs, message) "
        "VALUES (?,?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), constat.source, contexte,
         int(constat.ok), constat.nb,
         json.dumps(constat.champs, ensure_ascii=False), constat.message),
    )
    db.commit()


def derive(db: sqlite3.Connection, constat: Constat, contexte: str = COLLECTE,
           chute: float = 0.4) -> str | None:
    """Compare au dernier passage sain **de même nature** : signale un
    effondrement du volume.

    Un parseur peut rester « valide » tout en ne ramenant plus qu'une bribe
    de ce qu'il trouvait — un sélecteur sur trois qui casse, par exemple.
    Le contrôle de complétude ne le voit pas ; la comparaison au passé, oui.

    Le filtre sur `contexte` est ce qui rend la comparaison honnête : une
    sonde se compare aux sondes, une collecte aux collectes.
    """
    _preparer(db)
    ligne = db.execute(
        "SELECT nb FROM canari WHERE source = ? AND contexte = ? AND ok = 1 "
        "ORDER BY quand DESC LIMIT 1 OFFSET 1",
        (constat.source, contexte),
    ).fetchone()
    if not ligne or not ligne[0]:
        return None
    precedent = ligne[0]
    if constat.nb < precedent * (1 - chute):
        return (f"volume en chute : {constat.nb} contre {precedent} au passage "
                f"précédent ({1 - constat.nb / precedent:.0%} de moins)")
    return None


def historique(db: sqlite3.Connection, limite: int = 20,
               contexte: str | None = None) -> list[sqlite3.Row]:
    _preparer(db)
    db.row_factory = sqlite3.Row
    if contexte:
        return db.execute(
            "SELECT * FROM canari WHERE contexte = ? ORDER BY quand DESC LIMIT ?",
            (contexte, limite),
        ).fetchall()
    return db.execute(
        "SELECT * FROM canari ORDER BY quand DESC LIMIT ?", (limite,)
    ).fetchall()
