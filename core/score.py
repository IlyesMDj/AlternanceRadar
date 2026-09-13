"""Scoring des offres par rapport au profil défini dans config.yaml.

Le score n'est pas une note absolue : il sert uniquement à trier le digest
quotidien pour que les offres les plus pertinentes remontent en tête.
"""

from __future__ import annotations

from .classify import (niveau_detecte, rythme_compatible,
                       rythme_detecte)
from .models import (Job, compiler_motifs as _compiler, normalize,
                     normalize_intitule)


class Scorer:
    def __init__(self, config: dict):
        bloc = config.get("scoring", {})

        self.competences = {
            nom: {"poids": int(regle["poids"]), "regex": _compiler(regle["motifs"])}
            for nom, regle in bloc.get("competences", {}).items()
        }
        self.bonus = self._charger(bloc.get("bonus", []))
        self.malus = self._charger(bloc.get("malus", []))

        titre = bloc.get("titre", {})
        cible = titre.get("cible", {})
        hors = titre.get("hors_cible", {})
        self.titre_cible = _compiler(cible.get("motifs", []))
        self.poids_cible = int(cible.get("poids", 0))
        self.titre_hors = _compiler(hors.get("motifs", []))
        self.poids_hors = int(hors.get("poids", 0))

        ecole = bloc.get("entreprise", {}).get("hors_cible", {})
        self.entreprise_hors = _compiler(ecole.get("motifs", []))
        self.poids_ecole = int(ecole.get("poids", 0))

    @staticmethod
    def _charger(regles: list[dict]) -> list[dict]:
        return [
            {
                "points": int(r["points"]),
                "libelle": r["libelle"],
                "regex": _compiler(r["motifs"]),
            }
            for r in regles
        ]

    def score(self, job: Job) -> Job:
        """Calcule score, tags et justification. Modifie et retourne le job."""
        texte = job.haystack
        total = 0
        tags: list[str] = []
        detail: list[str] = []

        # Une compétence ne compte qu'une fois, même citée dix fois.
        for nom, regle in self.competences.items():
            if regle["regex"] and regle["regex"].search(texte):
                total += regle["poids"]
                tags.append(nom)
                detail.append(f"+{regle['poids']} {nom}")

        for regle in self.bonus + self.malus:
            if regle["regex"] and regle["regex"].search(texte):
                total += regle["points"]
                signe = "+" if regle["points"] > 0 else ""
                detail.append(f"{signe}{regle['points']} {regle['libelle']}")

        # Gate métier, sur l'intitulé seul. Les poids de compétences ci-dessus
        # mesurent la densité technique de la DESCRIPTION : un poste de chargé
        # de marketing qui cite Data, IT et cloud les cumule sans être un poste
        # de développement. Seul le titre dit ce qu'on va réellement faire.
        # L'exclusion prime sur l'inclusion (« Chargé de projet développement »).
        # Sauté pour les posts de fil : leur première ligne n'est pas un
        # intitulé, l'exclusion se déclencherait sur des mots de contexte.
        titre = normalize_intitule(job.title) if job.titre_est_intitule else ""
        if self.titre_hors and self.titre_hors.search(titre):
            total += self.poids_hors
            detail.append(f"{self.poids_hors} intitulé hors cible")
            tags.append("⚠ hors cible")
        elif self.titre_cible and self.titre_cible.search(titre):
            total += self.poids_cible
            detail.append(f"+{self.poids_cible} poste technique")

        # Gate écoles, sur le champ entreprise seul. Un CFA qui publie pour
        # recruter ses propres étudiants n'est pas un employeur — et suppose
        # de s'inscrire chez lui plutôt qu'à SUPINFO.
        if self.entreprise_hors and self.entreprise_hors.search(normalize(job.company)):
            total += self.poids_ecole
            detail.append(f"{self.poids_ecole} école/CFA, pas un employeur")
            tags.append("⚠ école/CFA")

        # Le rythme est le critère le plus discriminant du profil : le cursus
        # impose des blocs de trois semaines, et une offre qui ne peut pas les
        # accueillir ne se rattrapera pas sur sa pile technique.
        #
        # Les deux règles de `config.yaml` qui doublaient ce bloc ont été
        # retirées : la positive matchait « 3 semaines » n'importe où (donc
        # « 3 semaines de congés »), et n'apportait que 3 offres au-delà de
        # celles vues ici — toutes douteuses. Le poids qu'elles portaient est
        # reversé ici, où la détection est structurée.
        #
        # L'ABSENCE de rythme annoncé ne vaut rien, ni bonus ni malus : c'est
        # le cas de 1 087 offres sur 1 139, et ne rien dire n'est pas dire non.
        rythme = rythme_detecte(job)
        compatible = rythme_compatible(job)
        if compatible:
            total += 15
            detail.append("+15 rythme 3/1 confirmé")
            tags.append("rythme 3/1")
        elif compatible is False:
            total -= 15
            detail.append(f"-15 rythme {rythme} incompatible")
            tags.append(f"⚠ rythme {rythme}")

        niveau = niveau_detecte(job)
        if niveau == "Bac+4 / M1":
            total += 8
            detail.append("+8 niveau M1")
        if niveau:
            tags.append(niveau)

        # Une offre sans marqueur d'alternance reste collectée mais sort du haut
        # du classement : elle n'est probablement pas exploitable.
        if not job.is_alternance:
            total -= 40
            detail.append("-40 alternance non confirmée")

        job.score = total
        job.tags = sorted(set(tags))
        job.score_detail = " · ".join(detail)
        return job
