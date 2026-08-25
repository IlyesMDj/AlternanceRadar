"""Pré-remplissage assisté d'un formulaire de candidature.

**Ce module n'envoie jamais rien.** Il ouvre la page, remplit ce qu'il sait
remplir, dit ce qu'il a laissé de côté, et rend la main. Le clic final
t'appartient — c'est ta signature au bas d'une candidature, pas la mienne.

Ce n'est pas de la prudence de façade, c'est ce qui rend l'outil utilisable :

- **sans modèle de langage, toute réponse libre serait générique**, et une
  candidature d'alternance générique est celle qu'on écarte en premier. Le
  gabarit de `config.yaml` est rempli avec les données RÉELLES de l'offre —
  intitulé, entreprise, ville, technologies effectivement citées — mais il
  reste un gabarit, à relire ;
- **LinkedIn et Indeed interdisent l'envoi automatisé** dans leurs conditions,
  et ce compte a déjà été restreint une fois. Remplir un formulaire ouvert à
  l'écran n'est pas la même chose que soumettre en série ;
- **un formulaire mal rempli et envoyé est pire qu'un formulaire vide** :
  il ne se rattrape pas.

Le déroulé est donc explicitement en trois temps, et tu tiens deux d'entre eux :

    1. le programme ouvre l'offre        → tu navigues jusqu'au formulaire
    2. le programme remplit ce qu'il sait → tu relis, complètes, ENVOIES
    3. tu confirmes                      → le programme marque l'offre suivie

La détection des champs est volontairement générique : aucun sélecteur propre
à un site, donc rien à maintenir quand LinkedIn refait son DOM. Chaque champ
est identifié par une *signature* — son `name`, son `id`, son `placeholder`,
son `aria-label` et le texte de son `<label>` — comparée à des motifs ordonnés.
L'ordre compte : « prénom » doit être testé avant « nom », qui le contient.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

log = logging.getLogger("postuler")

# Champs qu'on ne touche sous aucun prétexte.
_INTOUCHABLES = ("password", "hidden", "submit", "button", "image", "reset")

# Motifs de reconnaissance, DANS L'ORDRE. Le premier qui matche gagne, d'où
# « prenom » avant « nom » : « nom » est un sous-mot de « prénom ».
# Chaque entrée : (clé de config, motif, faut-il un champ long ?)
_REGLES: list[tuple[str, str, bool]] = [
    ("prenom",      r"pr[ée]nom|first[\s_-]*name|given[\s_-]*name|firstname", False),
    ("nom",         r"nom de famille|last[\s_-]*name|surname|family[\s_-]*name"
                    r"|^nom$|\bnom\b(?!.*(utilisateur|societ|entrepr|fichier))", False),
    ("email",       r"e[\s_-]*mail|courriel|adresse[\s_-]*mail", False),
    ("telephone",   r"t[ée]l[ée]phone|\bt[ée]l\b|phone|mobile|portable|gsm", False),
    ("code_postal", r"code[\s_-]*postal|\bcp\b|zip|postal[\s_-]*code", False),
    ("ville",       r"\bville\b|\bcity\b|localit[ée]|commune", False),
    ("adresse",     r"^adresse$|\bstreet\b|\brue\b|address(?!.*mail)", False),
    ("linkedin",    r"linkedin", False),
    ("message",     r"message|motivation|lettre|pr[ée]sentez|parlez[\s_-]*nous"
                    r"|cover[\s_-]*letter|pourquoi|commentaire|informations"
                    r"[\s_-]*compl[ée]mentaires", True),
]

# Les fichiers, traités à part : ils passent par `set_input_files`.
_FICHIERS = [("cv", r"\bcv\b|r[ée]sum[ée]|curriculum"),
             ("lettre", r"lettre|motivation|cover")]


def _sansaccent(texte: str) -> str:
    plat = unicodedata.normalize("NFD", (texte or "").lower())
    return "".join(c for c in plat if unicodedata.category(c) != "Mn")


# Signature calculée DANS le navigateur : `HTMLInputElement.labels` résout
# nativement les deux formes d'étiquetage — `<label for="x">` et `<label>` qui
# englobe le champ — là où une recherche côté Python n'en voyait aucune.
_JS_SIGNATURE = """
e => {
  const bouts = [];
  for (const a of ['name', 'id', 'placeholder', 'aria-label',
                   'autocomplete', 'data-testid', 'title']) {
    const v = e.getAttribute(a);
    if (v) bouts.push(v);
  }
  if (e.labels) for (const l of e.labels) bouts.push(l.innerText);
  const englobant = e.closest('label');
  if (englobant) bouts.push(englobant.innerText);
  if (bouts.length === 0) {
    const p = e.parentElement;
    if (p) bouts.push((p.innerText || '').slice(0, 120));
  }
  return bouts.join(' ');
}
"""


def _signature(champ) -> str:
    """Tout ce qui, autour d'un champ, dit à quoi il sert.

    Un formulaire n'étiquette pas ses champs de façon régulière : certains
    n'ont qu'un `placeholder`, d'autres un `<label for>`, d'autres un `<label>`
    englobant, d'autres un `aria-label`. On les concatène tous plutôt que de
    parier sur une convention.
    """
    try:
        return _sansaccent(champ.evaluate(_JS_SIGNATURE) or "")
    except Exception:
        return ""


def _valeurs(cand: dict, job) -> dict[str, str]:
    """Les valeurs à écrire, gabarit du message compris."""
    valeurs = {cle: str(cand.get(cle) or "").strip()
               for cle, _, _ in _REGLES if cle != "message"}

    lieu = f" à {job['location']}" if job and job["location"] else ""
    technos = ""
    if job:
        # Uniquement les technologies RÉELLEMENT citées par l'offre, relevées
        # par le scorer. Rien n'est ajouté au CV qui n'y soit pas.
        #
        # Et uniquement celles qui ont un libellé lisible : les noms de
        # groupes internes (« web_front », « bdd ») trient très bien mais
        # feraient négligé dans une lettre, donc on les omet plutôt que de
        # les écrire tels quels.
        import json
        libelles = cand.get("libelles_technos") or {}
        noms = [libelles[t] for t in json.loads(job["tags"] or "[]")
                if t in libelles]
        if noms:
            liste = ", ".join(noms[:4])
            technos = (f"Les compétences attendues — {liste} — recoupent "
                       f"ce que je pratique.")

    gabarit = str(cand.get("message") or "")
    valeurs["message"] = gabarit.format(
        intitule=(job["title"] if job else ""),
        entreprise=(job["company"] if job else ""),
        lieu=lieu, technos=technos).strip()
    return valeurs


def remplir(page, cand: dict, job=None) -> tuple[list[str], list[str]]:
    """Remplit ce qui est reconnu. Retourne (remplis, laissés de côté)."""
    valeurs = _valeurs(cand, job)
    remplis: list[str] = []
    ignores: list[str] = []
    deja: set[str] = set()

    for champ in page.query_selector_all("input, textarea, select"):
        try:
            if not champ.is_visible() or not champ.is_editable():
                continue
            type_ = (champ.get_attribute("type") or "text").lower()
            if type_ in _INTOUCHABLES:
                continue
            # Cases à cocher et boutons radio : consentements RGPD, questions
            # fermées, engagements. Jamais cochés à ta place.
            if type_ in ("checkbox", "radio"):
                ignores.append(f"{type_} « {_signature(champ)[:44]} »")
                continue
            if champ.input_value().strip():
                continue                      # déjà rempli par le site
        except Exception:
            continue

        signature = _signature(champ)
        if type_ == "file":
            for cle, motif in _FICHIERS:
                chemin = str(cand.get(cle) or "")
                if chemin and re.search(motif, signature) and cle not in deja:
                    if not Path(chemin).exists():
                        ignores.append(f"{cle} introuvable : {chemin}")
                        break
                    try:
                        champ.set_input_files(chemin)
                        remplis.append(f"{cle} → {Path(chemin).name}")
                        deja.add(cle)
                    except Exception as e:
                        ignores.append(f"{cle} : {type(e).__name__}")
                    break
            else:
                # Un champ fichier non identifié : le CV est le pari le plus sûr.
                chemin = str(cand.get("cv") or "")
                if chemin and "cv" not in deja and Path(chemin).exists():
                    try:
                        champ.set_input_files(chemin)
                        remplis.append(f"cv → {Path(chemin).name}")
                        deja.add("cv")
                    except Exception:
                        ignores.append("champ fichier non identifié")
            continue

        for cle, motif, long_ in _REGLES:
            if cle in deja or not re.search(motif, signature):
                continue
            valeur = valeurs.get(cle, "")
            if not valeur:
                ignores.append(f"{cle} vide dans config.yaml")
                deja.add(cle)
                break
            # Un gabarit de plusieurs lignes n'a rien à faire dans un champ
            # d'une ligne : on ne tronque pas une lettre de motivation.
            if long_ and champ.evaluate("e => e.tagName") != "TEXTAREA":
                break
            try:
                champ.fill(valeur)
                apercu = valeur.replace("\n", " ")[:46]
                remplis.append(f"{cle} → {apercu}{'…' if len(valeur) > 46 else ''}")
                deja.add(cle)
            except Exception as e:
                ignores.append(f"{cle} : {type(e).__name__}")
            break

    # Ce qui reste vide et visible mérite d'être signalé : c'est là que se
    # jouent les questions propres à l'employeur.
    for champ in page.query_selector_all("input, textarea"):
        try:
            if (champ.is_visible() and champ.is_editable()
                    and not champ.input_value().strip()
                    and (champ.get_attribute("type") or "text").lower()
                    not in _INTOUCHABLES + ("checkbox", "radio", "file")):
                ignores.append(f"vide : « {_signature(champ)[:52]} »")
        except Exception:
            continue

    return remplis, ignores
