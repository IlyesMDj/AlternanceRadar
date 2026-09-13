"""Extraction des adresses de candidature dans le texte d'une offre.

Un e-mail direct est le canal au meilleur retour du projet : il court-circuite
l'ATS, où une candidature d'alternance disparaît dans une file commune avec
les CDI. C'était jusqu'ici l'apanage des posts de fil LinkedIn, seuls à passer
par cette fonction — les dix autres sources livrent pourtant des descriptions
entières, dont certaines portent une adresse en clair.

Relevé sur les 1 067 offres retenues : 23 avaient un contact enregistré, et
**37 de plus en portaient un dans leur description** sans que personne ne
regarde. Le canal passe donc de 2 % à 6 % des offres pour zéro requête réseau.

Deux filtres, parce qu'une adresse trouvée n'est pas une adresse utile :

- les boîtes de **service** (`dpo@`, `rgpd@`, `noreply@`) ne mènent à aucun
  recruteur. Les écrire serait pire que de ne rien écrire : le digest
  afficherait un bouton `mailto:` qui n'aboutit nulle part ;
- les adresses **d'exemple** (`nom.prenom@`, `example.com`) sont des gabarits
  laissés dans le texte, pas des destinataires.
"""

from __future__ import annotations

import re

# Une adresse, éventuellement obfusquée pour échapper aux robots :
# « contact [at] boite [dot] fr ».
#
# Le point du domaine n'accepte AUCUNE espace autour de lui quand il est
# littéral — c'est ce qui manquait, et ça coûtait cher : avec `\s*` de part et
# d'autre, « …rejoindre@monext.net. Dans notre équipe… » se lisait comme
# l'adresse « rejoindre@monext.net.Dans », le nettoyage final collant les mots
# de la phrase suivante au domaine. Les espaces ne sont tolérées qu'autour des
# formes explicitement obfusquées, où elles font partie de la notation.
_EMAIL = re.compile(
    r"[a-zA-Z0-9._%+-]{2,}"
    r"(?:@|\s*(?:\(at\)|\[at\])\s*|\s+at\s+)"
    r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*"
    r"(?:\.|\s*(?:\(dot\)|\[dot\])\s*|\s+dot\s+)"
    r"[a-zA-Z]{2,12}(?![a-zA-Z])"
)

# Boîtes de service : juridiques, techniques, ou sans destinataire humain.
_SERVICE = re.compile(
    r"^(?:dpo|rgpd|gdpr|privacy|donnees[-.]personnelles|protection[-.]donnees"
    r"|noreply|no[-.]reply|ne[-.]pas[-.]repondre|postmaster|webmaster"
    r"|abuse|support|info|contact[-.]?presse|presse|newsletter)@")

# Gabarits laissés dans le texte plutôt qu'une vraie adresse.
_EXEMPLE = re.compile(
    r"@(?:example|exemple|domain|domaine|monentreprise|votresociete)\."
    r"|^(?:nom|prenom|prenom[.-]nom|nom[.-]prenom|votre[.-]?nom|email|mail)@")


def extraire_emails(texte: str) -> list[str]:
    """Adresses de candidature trouvées dans le texte, désobfusquées et triées.

    Rend une liste vide plutôt que None : le champ `contacts` d'un `Job` est
    toujours une liste, et un appelant ne devrait jamais avoir à tester.
    """
    trouves = []
    for brut in _EMAIL.findall(texte or ""):
        adresse = re.sub(r"\s*(?:\(at\)|\[at\]|\s+at\s+)\s*", "@", brut, flags=re.I)
        adresse = re.sub(r"\s*(?:\(dot\)|\[dot\]|\s+dot\s+)\s*", ".", adresse, flags=re.I)
        adresse = adresse.replace(" ", "").strip(".,;:").lower()
        if "@" not in adresse or "." not in adresse.split("@")[-1]:
            continue
        if _SERVICE.match(adresse) or _EXEMPLE.search(adresse):
            continue
        trouves.append(adresse)
    return sorted(set(trouves))
