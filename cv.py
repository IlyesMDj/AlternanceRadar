"""Génération d'un CV LaTeX adapté à une offre.

Le contenu vient intégralement de `cv_source.yaml` : rien n'est écrit ni
reformulé ici, seul l'ORDRE change. Les groupes de compétences, les puces de
chaque expérience et l'ordre des projets sont triés par recoupement avec les
tags de l'offre visée (`job.tags`, calculés par `core/score.py` — les mêmes
clés que `scoring.competences` dans config.yaml, réutilisées telles quelles).

Une expérience sans recoupement garde sa place : l'ordre chronologique des
postes n'est jamais réécrit, seules les puces qui les décrivent le sont —
réordonner l'historique de carrière selon l'offre serait malhonnête.

Seule exception au « sans LLM » : le paragraphe d'accroche en tête du CV,
écrit par Gemini (`GEMINI_API_KEY`, comme `LBA_API_KEY` ailleurs dans le
projet) à partir des SEULS faits fournis — formation, rythme, compétences
qui recoupent l'offre. Optionnel : sans clé, ou si l'appel échoue, le CV se
génère normalement, juste sans ce paragraphe.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import yaml

log = logging.getLogger("cv")

_MODELE_GEMINI = "gemini-3.6-flash"

_ECHAPPEMENTS = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def _e(texte: str | None) -> str:
    """Échappe les caractères spéciaux LaTeX. Jamais de HTML/Markdown ici :
    le texte de `cv_source.yaml` est déjà la forme finale à afficher."""
    return "".join(_ECHAPPEMENTS.get(c, c) for c in (texte or ""))


def charger(chemin: Path) -> dict:
    return yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}


def _pertinence(tags: list[str] | None, cibles: set[str]) -> int:
    return len(set(tags or []) & cibles)


def _pertinence_projet(projet: dict, cibles: set[str]) -> int:
    tags: set[str] = set()
    for b in projet.get("bullets", []):
        tags.update(b.get("tags") or [])
    return len(tags & cibles)


def adapter(cv: dict, tags_offre: list[str]) -> dict:
    """Réordonne le contenu du CV selon les tags de l'offre. N'ajoute et ne
    supprime jamais une ligne — seul le tri change."""
    cibles = set(tags_offre or [])
    adapte = dict(cv)

    # Groupes de compétences non vides, ceux qui recoupent l'offre d'abord.
    # Le tri est stable : à égalité, l'ordre du fichier source est conservé.
    groupes = [(nom, valeurs) for nom, valeurs in (cv.get("competences") or {}).items()
              if valeurs]
    groupes.sort(key=lambda g: g[0] not in cibles)
    adapte["competences"] = groupes

    experiences = []
    for exp in cv.get("experiences") or []:
        e = dict(exp)
        e["bullets"] = sorted(exp.get("bullets") or [],
                              key=lambda b: -_pertinence(b.get("tags"), cibles))
        experiences.append(e)
    adapte["experiences"] = experiences

    # Les projets, eux, n'ont pas d'ordre chronologique à préserver : le plus
    # pertinent pour l'offre remonte en tête.
    projets = []
    for p in sorted(cv.get("projets") or [],
                    key=lambda p: -_pertinence_projet(p, cibles)):
        pr = dict(p)
        pr["bullets"] = sorted(p.get("bullets") or [],
                               key=lambda b: -_pertinence(b.get("tags"), cibles))
        projets.append(pr)
    adapte["projets"] = projets

    return adapte


def _libelle(nom_groupe: str, libelles: dict) -> str:
    return libelles.get(nom_groupe, nom_groupe)


def _section_competences(groupes: list[tuple[str, list[str]]],
                         hors_groupes: list[str], libelles: dict) -> str:
    lignes = [
        r"\item \textbf{%s :} %s" % (_e(_libelle(nom, libelles)),
                                     ", ".join(_e(v) for v in valeurs))
        for nom, valeurs in groupes
    ]
    if hors_groupes:
        lignes.append(r"\item \textbf{Autres :} %s" %
                      ", ".join(_e(v) for v in hors_groupes))
    return "\n".join(lignes)


def _section_experiences(experiences: list[dict]) -> str:
    blocs = []
    for exp in experiences:
        puces = "\n".join(f"    \\item {_e(b['texte'])}" for b in exp.get("bullets", []))
        blocs.append(
            r"\poste{%s}{%s}{%s, %s}" % (_e(exp["titre"]), _e(exp["periode"]),
                                         _e(exp["entreprise"]), _e(exp["lieu"])) +
            "\n\\begin{itemize}[leftmargin=*,itemsep=1pt,topsep=3pt]\n" +
            puces + "\n\\end{itemize}"
        )
    return "\n\\smallskip\n".join(blocs)


def _section_projets(projets: list[dict]) -> str:
    blocs = []
    for p in projets:
        entete = r"\textbf{%s}" % _e(p["titre"])
        if p.get("description"):
            entete += r" --- %s" % _e(p["description"])
        puces = "\n".join(f"    \\item {_e(b['texte'])}" for b in p.get("bullets", []))
        blocs.append(entete + "\n\\begin{itemize}[leftmargin=*,itemsep=1pt,topsep=2pt]\n"
                     + puces + "\n\\end{itemize}")
    return "\n\\smallskip\n".join(blocs)


def _section_formation(formation: list[dict]) -> str:
    lignes = [
        r"\poste{%s}{%s}{%s}" % (_e(f["titre"]), _e(f["periode"]), _e(f["lieu"]))
        for f in formation
    ]
    return "\n\\smallskip\n".join(lignes)


_GABARIT = r"""\documentclass[10.5pt,a4paper]{article}
% `cmap` avant `fontenc` : sans lui, pdfTeX rend « fi »/« fl » correctement
% à l'écran mais les fait disparaître du texte extrait du PDF — copier-coller
% ET lecture par un ATS y perdent « profil », « Harfleur », « fichier ».
\usepackage{cmap}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage[french]{babel}
\usepackage[margin=1.8cm]{geometry}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage{hyperref}
\usepackage{xcolor}

\pagestyle{empty}
\setlength{\parindent}{0pt}
\hypersetup{colorlinks=true, urlcolor=black, linkcolor=black}

\titleformat{\section}{\normalsize\bfseries}{}{0em}{}[\vspace{1pt}\titlerule]
\titlespacing{\section}{0pt}{8pt}{4pt}

% #1 intitulé, #2 période, #3 employeur/lieu (ou lieu seul, en formation)
\newcommand{\poste}[3]{\textbf{#1} \hfill \textit{#2}\\ #3\par}

\begin{document}

{\Huge \textbf{@@NOM@@}}\\[1pt]
{\large @@TITRE@@}\\[3pt]
@@CONTACT@@

@@ACCROCHE@@

\section*{Compétences}
\begin{itemize}[leftmargin=*,itemsep=1pt,topsep=2pt]
@@COMPETENCES@@
\end{itemize}

\section*{Expériences professionnelles}
@@EXPERIENCES@@

\section*{Projets académiques}
@@PROJETS@@

\section*{Formation}
@@FORMATION@@

\section*{Langues}
@@LANGUES@@

\end{document}
"""


def generer_accroche(profil: dict, tags_matches: list[str], libelles: dict,
                     job_titre: str, job_entreprise: str,
                     job_description: str) -> str | None:
    """Paragraphe d'accroche personnalisé, écrit par Gemini.

    Optionnel : sans `GEMINI_API_KEY`, ou si l'appel échoue (quota, réseau,
    modèle retiré...), retourne None plutôt que de bloquer la génération —
    exactement comme LBA_API_KEY ailleurs dans ce projet.

    Le prompt ne fournit que des faits déjà présents dans `config.yaml`
    (`profil`) et dans les tags de compétences qui recoupent l'offre : rien
    d'autre n'est laissé au modèle à inventer.
    """
    cle = os.environ.get("GEMINI_API_KEY")
    if not cle:
        log.info("GEMINI_API_KEY absente — CV généré sans accroche")
        return None

    competences = ", ".join(_libelle(t, libelles) for t in tags_matches) or "aucune en particulier"
    prompt = (
        "Tu rédiges un paragraphe d'accroche pour un CV de candidature en "
        "alternance.\n\n"
        "Faits RÉELS à utiliser, et RIEN D'AUTRE — n'invente aucune "
        "expérience, aucun diplôme, aucune compétence absente de cette "
        "liste :\n"
        f"- Formation en cours : {profil.get('niveau', '')} à {profil.get('ecole', '')}\n"
        f"- Rythme d'alternance : {profil.get('rythme', '')}\n"
        f"- Disponible à partir de : {profil.get('demarrage', '')}\n"
        f"- Compétences qui recoupent cette offre précise : {competences}\n\n"
        f"Offre visée : « {job_titre} » chez {job_entreprise}.\n"
        f"Extrait de l'offre : {(job_description or '')[:500]}\n\n"
        "Écris 2 à 3 phrases en français, factuelles et sobres, montrant en "
        "quoi ce profil correspond à CETTE offre précise. Aucun superlatif "
        "creux (« passionné », « dynamique »...), aucun markdown, aucun "
        "guillemet autour du texte. Réponds uniquement par le paragraphe, "
        "rien d'autre."
    )

    try:
        from google import genai
        client = genai.Client(api_key=cle)
        reponse = client.models.generate_content(model=_MODELE_GEMINI, contents=prompt)
        return (reponse.text or "").strip() or None
    except Exception as e:
        log.warning("Gemini indisponible (%s) — CV généré sans accroche", e)
        return None


def rendre(cv_adapte: dict, libelles: dict, accroche: str | None = None) -> str:
    """Assemble le gabarit par remplacement de jetons `@@...@@`, jamais par
    `%`/`.format()` : le document produit est plein de `%` (commentaires
    LaTeX) et d'accolades, qui entreraient en collision avec ces syntaxes."""
    identite = cv_adapte["identite"]
    contact = " \\textbar\\ ".join(filter(None, [
        _e(identite.get("email")),
        _e(identite.get("telephone")),
        _e(identite.get("ville")),
        (r"\href{%s}{Portfolio}" % identite["portfolio"]) if identite.get("portfolio") else "",
        (r"\href{https://github.com/%s}{GitHub}" % identite["github"]) if identite.get("github") else "",
        _e(identite.get("permis")),
    ]))
    langues = ", ".join(f"{_e(l['nom'])} ({_e(l['niveau'])})" for l in cv_adapte.get("langues", []))

    jetons = {
        "@@NOM@@": _e(identite["nom"]),
        "@@TITRE@@": _e(identite.get("titre", "")),
        "@@CONTACT@@": contact,
        "@@COMPETENCES@@": _section_competences(cv_adapte["competences"],
                                                cv_adapte.get("competences_hors_groupes", []),
                                                libelles),
        "@@EXPERIENCES@@": _section_experiences(cv_adapte["experiences"]),
        "@@PROJETS@@": _section_projets(cv_adapte["projets"]),
        "@@FORMATION@@": _section_formation(cv_adapte["formation"]),
        "@@LANGUES@@": langues,
        "@@ACCROCHE@@": _e(accroche) if accroche else "",
    }
    source = _GABARIT
    for jeton, valeur in jetons.items():
        source = source.replace(jeton, valeur)
    return source


def compiler(source_tex: str, dossier: Path, nom_base: str = "cv") -> Path | None:
    """Compile en PDF via `pdflatex` (MiKTeX/TeX Live). Deux passes : la
    première seule laisse parfois des références internes non résolues.

    Retourne le chemin du PDF, ou None si `pdflatex` est introuvable ou que
    la compilation échoue — le `.tex` reste sur disque dans les deux cas,
    compilable ailleurs (Overleaf...).
    """
    dossier.mkdir(parents=True, exist_ok=True)
    chemin_tex = dossier / f"{nom_base}.tex"
    chemin_tex.write_text(source_tex, encoding="utf-8")

    try:
        for _ in range(2):
            resultat = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                 f"{nom_base}.tex"],
                # Généreux à dessein : la toute première compilation laisse
                # MiKTeX télécharger les paquets manquants (babel-french...) à
                # la volée, largement au-delà du temps d'une passe normale.
                cwd=dossier, capture_output=True, text=True, timeout=180,
            )
            if resultat.returncode != 0:
                log.error("pdflatex a échoué — voir %s", dossier / f"{nom_base}.log")
                return None
    except subprocess.TimeoutExpired:
        log.error("pdflatex a dépassé 180 s — relance la commande, les paquets "
                  "manquants sont mis en cache après la première tentative")
        return None
    except FileNotFoundError:
        log.error("pdflatex introuvable — installe une distribution LaTeX "
                  "(MiKTeX, TeX Live) ou compile %s ailleurs", chemin_tex)
        return None

    pdf = dossier / f"{nom_base}.pdf"
    return pdf if pdf.exists() else None


def generer(tags_offre: list[str], cv_data: dict, cfg: dict, dossier: Path,
           job_titre: str = "", job_entreprise: str = "",
           job_description: str = "") -> tuple[Path, Path | None]:
    """Point d'entrée : adapte le CV aux tags de l'offre, tente une accroche
    Gemini, écrit le `.tex`, tente le PDF. `tags_offre` vient de la colonne
    `tags` de l'offre en base (JSON déjà décodé par l'appelant) — les mêmes
    clés que `scoring.competences` dans config.yaml.

    Retourne (chemin_tex, chemin_pdf_ou_None).
    """
    libelles = (cfg.get("candidature") or {}).get("libelles_technos", {})
    adapte = adapter(cv_data, tags_offre)
    accroche = generer_accroche(cfg.get("profil", {}), tags_offre, libelles,
                                job_titre, job_entreprise, job_description)
    source = rendre(adapte, libelles, accroche)
    nom_base = "cv"
    pdf = compiler(source, dossier, nom_base)
    return dossier / f"{nom_base}.tex", pdf
