"""Exclusion de secteurs entiers.

Volontairement distinct du scoring. Un malus déclasse — une exclusion fait
disparaître. Quand on ne veut pas d'un secteur, le voir en bas de liste ne
sert à rien, il doit sortir du digest.

Deux garde-fous de conception :

- l'exclusion porte sur le **champ entreprise** et sur le **code NAF**, jamais
  sur le texte de l'offre. Une annonce de développeur qui mentionne « secteur
  bancaire » parmi ses clients n'est pas une offre de banque ;
- le motif est **stocké**, la ligne n'est jamais supprimée. Retirer un secteur
  de `config.yaml` et relancer `rescore` les fait toutes revenir.

**Une seule dérogation au premier garde-fou : le courtage scolaire.** Une
école qui recrute « pour l'une de ses entreprises partenaires » ne porte pas
son statut dans sa raison sociale — AURLOM, Scholia, Wijin et l'ESG publiaient
57, 12, 3 et 10 offres sous quatre noms qu'aucune liste ne devinait, alors que
« CFA » figurait noir sur blanc dans leur description. Ces offres exigent de
s'inscrire chez elles, ce qui est sans objet quand on est déjà à SUPINFO.

La dérogation reste étroite : il faut **deux marqueurs simultanés**, l'un
scolaire et l'autre de courtage, dans l'en-tête de l'annonce. Une mention
isolée — « nos campus », « entreprises partenaires » — ne déclenche rien.
"""

from __future__ import annotations

import re

from .models import Job, compiler_motifs, normalize, normalize_intitule

# « Secteur : Programmation informatique (6201Z). » — écrit par le collecteur
# La Bonne Alternance, seule source à fournir le code d'activité.
_NAF = re.compile(r"\((\d{4}[A-Z])\)")

# Fenêtre de lecture pour le courtage scolaire. Mesuré sur les quatre cas
# réels : AURLOM annonce ses « 1 500 entreprises partenaires » au caractère
# 120, Scholia son CFA au caractère 4, Wijin son partenariat au caractère 30.
# La présentation ouvre l'annonce ; au-delà, on lit le poste, pas l'employeur.
ENTETE = 1500

# Déclaration EXPLICITE d'un type de contrat. Volontairement exigeante : le
# simple mot « CDI » dans une description ne prouve rien — « possibilité de CDI
# à l'issue de l'alternance » est un argument de vente, pas un type de contrat.
# Le libellé doit être explicite : « contrat » tout court déclencherait sur
# « à l'issue du contrat, un CDI vous sera proposé » — qui décrit justement
# une alternance avec perspective d'embauche, exactement ce qu'on veut garder.
_CONTRAT_DECLARE = re.compile(
    r"(?:type de contrat|nature du contrat|contrat propose|type d emploi"
    r"|type de poste|statut du poste)\s*:?\s*(?:un |une )?"
    r"(cdi|contrat a duree indeterminee|freelance|interim|portage salarial)"
    r"(?![a-z])"
    # Un CDI suivi des AUTRES types est une liste de choix, pas une
    # déclaration : c'est le menu déroulant d'un formulaire d'alerte. Cette
    # confusion avait fait exclure les 363 offres HelloWork d'un coup.
    r"(?!\s*(?:cdd|interim|stage|alternance|independant|franchise|associe))"
)


class Exclusions:
    def __init__(self, config: dict):
        bloc = config.get("exclusions", {})
        self.secteurs = {
            nom: compiler_motifs(motifs)
            for nom, motifs in (bloc.get("secteurs") or {}).items()
        }
        # Préfixes NAF : « 64 » couvre tout 64.xx (services financiers).
        self.naf = tuple(str(c) for c in (bloc.get("naf_exclus") or []))
        # Métiers écartés, testés sur l'intitulé seul.
        self.metiers = {
            nom: compiler_motifs(regle.get("motifs", []))
            for nom, regle in (bloc.get("metiers") or {}).items()
        }
        # Types de contrat écartés.
        self.contrats = compiler_motifs(
            (bloc.get("contrats") or {}).get("exclus") or []
        )
        # Courtage scolaire : deux familles de marqueurs, exigées ensemble.
        courtage = bloc.get("courtage_ecole") or {}
        self.ecole_dit = compiler_motifs(courtage.get("marqueurs_ecole") or [])
        self.courtage_dit = compiler_motifs(courtage.get("marqueurs_courtage") or [])
        # Contre-exemples, prioritaires sur toute exclusion.
        self.exceptions = compiler_motifs(bloc.get("exceptions") or [])
        self.actif = bool(self.secteurs or self.naf or self.metiers
                          or self.contrats or self.ecole_dit)

    def motif(self, job: Job) -> str:
        """Motif d'exclusion, ou chaîne vide si l'offre est conservée."""
        if not self.actif:
            return ""

        entreprise = normalize(job.company)

        # Une exception l'emporte sur tout le reste. Indispensable : les listes
        # sectorielles produisent inévitablement des faux positifs — « Banques
        # Alimentaires » n'est pas une banque, « Cegedim Assurances » édite du
        # logiciel POUR les assureurs sans en être un.
        if entreprise and self.exceptions and self.exceptions.search(entreprise):
            return ""

        if entreprise:
            for nom, regex in self.secteurs.items():
                if regex and regex.search(entreprise):
                    return nom

        # Courtage scolaire. Lu sur l'EN-TÊTE seulement : la présentation de
        # l'école ouvre toujours l'annonce, alors que le bas de page charrie
        # des mentions légales et des listes de partenaires qui feraient
        # déclencher la règle à tort.
        if self.ecole_dit and self.courtage_dit:
            entete = normalize(job.description or "")[:ENTETE]
            if self.ecole_dit.search(entete) and self.courtage_dit.search(entete):
                return "ecoles (courtage)"

        # Métier et contrat se lisent dans l'intitulé. Sauté pour les posts de
        # fil et les entreprises du marché caché, dont le « titre » n'est pas
        # un intitulé de poste : l'exclusion s'y déclencherait au hasard.
        if job.titre_est_intitule:
            titre = normalize_intitule(job.title)
            for nom, regex in self.metiers.items():
                if regex and regex.search(titre):
                    return nom
            if self.contrats and self.contrats.search(titre):
                return "contrat hors alternance"

        # Type de contrat déclaré explicitement dans la description.
        declare = _CONTRAT_DECLARE.search(normalize(job.description or ""))
        if declare:
            return f"contrat {declare.group(1)[:24]}"

        if self.naf:
            trouve = _NAF.search(job.description or "")
            if trouve and trouve.group(1).startswith(self.naf):
                return f"NAF {trouve.group(1)}"

        return ""

    def appliquer(self, job: Job) -> Job:
        job.exclu = self.motif(job)
        return job
