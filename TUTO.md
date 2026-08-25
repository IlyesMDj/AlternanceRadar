# Tuto — alternance-radar

Guide d'utilisation. Pour le fonctionnement interne et les choix techniques,
voir `README.md`.

---

## 1. En 30 secondes

Ouvre PowerShell et va dans le projet :

```powershell
cd C:\Users\stageinfo\alternance-radar
```

Les trois commandes qui couvrent 90 % de l'usage :

```powershell
.\radar.ps1 tout --depuis 3jours    # collecte partout, génère le digest
.\radar.ps1 report                   # régénère le digest sans re-collecter
.\radar.ps1 stats                    # où en est la base
```

Puis ouvre `digest.html` dans ton navigateur.

> **Si PowerShell refuse d'exécuter `radar.ps1`** — une seule fois :
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```

---

## 2. Ce qui est déjà installé

Rien à refaire. L'état actuel :

- environnement Python isolé dans `.venv\`
- clé La Bonne Alternance persistée (`LBA_API_KEY`, valide jusqu'au 11/08/2027)
- base SQLite `store\jobs.db` : **3 693 entrées, 3 154 alternances**

Seule chose non configurée : la session LinkedIn pour les posts (voir §7).

---

## 3. Le rythme quotidien

Une fois par jour, le matin :

```powershell
.\radar.ps1 tout --depuis jour
```

Ça enchaîne LinkedIn, HelloWork et La Bonne Alternance, puis écrit `digest.html`.
Compter **20 à 35 minutes** — le programme s'impose des pauses entre chaque
requête pour ne pas se faire bloquer. Lance-le et va faire autre chose.

Les offres déjà vues ne sont **jamais recollectées** ni réaffichées comme
nouvelles : tu ne relis que ce qui vient d'arriver.

### Choisir la fenêtre

| Option | Fenêtre | Quand l'utiliser |
|---|---|---|
| `--depuis jour` | 24 h | Le passage quotidien |
| `--depuis 3jours` | 72 h | Après un week-end, ou 2-3 jours sans regarder |
| `--depuis semaine` | 7 jours | Reprise après une pause |
| `--depuis 2semaines` | 14 jours | Ratissage large (défaut si tu omets l'option) |

L'option marche aussi sur chaque source séparément :

```powershell
.\radar.ps1 hellowork --depuis semaine
```

---

## 4. Lire le digest

Deux façons de l'ouvrir, et elles ne se valent pas :

```powershell
.\radar.ps1 report      # écrit digest.html, à ouvrir au double-clic
.\radar.ps1 serve       # sert le digest et ouvre le navigateur
```

**Utilise `serve` au quotidien.** C'est le seul mode où la page est vivante :

- la case **« déjà postulé »** sous chaque offre enregistre vraiment en base.
  Un clic, et l'annonce sort de ta liste de travail pour rejoindre « Mes
  candidatures ». Plus besoin de recopier un `uid` dans `main.py mark` — c'est
  précisément pour ça que 9 637 offres étaient restées non marquées ;
- le **panneau de filtres** en haut : coche les plateformes voulues (le
  nombre d'offres retenues est affiché à côté de chacune), règle le score
  minimum, la fenêtre de fraîcheur et le tri. Le filtrage se fait en base,
  donc la page montre exactement ce que montrerait la même requête en ligne
  de commande. « tout afficher » remet la vue à zéro ;
- le bouton **« mettre à jour »** relance une collecte sans quitter la page :
  choisis la source et la fenêtre, le journal défile en direct. Recharge
  ensuite pour voir les nouvelles offres.

> Les **posts LinkedIn** ne sont volontairement pas dans la liste : ils
> ouvrent une fenêtre Chrome, exigent une session et imposent six heures entre
> deux passages. Un bouton les aurait rendus trop faciles à déclencher — ça
> reste `.\radar.ps1 posts`, à la main.

Une seule collecte tourne à la fois ; en relancer une pendant qu'une autre
tourne renvoie une erreur explicite plutôt que d'en démarrer deux.

### « 24 heures » veut dire 24 heures

`--depuis jour` fonctionne aussi sur `report` et `serve`, et il a désormais la
priorité sur `--max-age`. Collecter sur 24 h puis afficher deux semaines n'avait
aucun sens :

```powershell
.\radar.ps1 tout --depuis jour
.\radar.ps1 serve --depuis jour
```

La fenêtre est comptée **à l'heure près** sur les sources qui publient un
horodatage — PASS, Welcome to the Jungle, Indeed, adopte1dev. Auparavant
« 1 jour » signifiait « daté d'hier ou d'aujourd'hui », soit jusqu'à 48 h : une
offre d'hier 7 h du matin restait affichée le lendemain soir. Mesuré à la
correction : des annonces réellement vieilles de 25 h, 32 h et 39 h sortaient
d'une fenêtre « 24 h ».

LinkedIn, HelloWork et Glassdoor ne donnent qu'une date, sans heure. Pour
celles-là la comparaison reste au jour — c'est une limite de la source, pas du
programme, et elles sont de toute façon filtrées à 24 h par le site lui-même
au moment de la collecte.

Ouvert au double-clic, le digest reste parfaitement lisible, mais les filtres
disparaissent et les cases sont grisées, avec un bandeau qui l'explique : une
page en `file://` n'a aucun moyen d'écrire dans SQLite ni de re-interroger la
base. Mieux vaut une case désactivée qu'une case qui coche et perd
l'information.

Le serveur n'écoute que sur `127.0.0.1` — la base contient des contacts
directs et ton historique de candidatures, rien de tout ça ne sort de ta
machine. `Ctrl+C` dans la fenêtre PowerShell l'arrête.

`digest.html` a ensuite **deux sections**, à traiter très différemment.

### Section « Offres »

Des annonces classiques, triées par score. Le chiffre à gauche :

| Score | Couleur | Ce que ça veut dire |
|---|---|---|
| **≥ 40** | vert | Correspond fortement à ton profil — à traiter en priorité |
| **20 – 39** | orange | Correspondance partielle, à lire |
| **< 20** | gris | Faible — n'apparaît pas dans le digest par défaut |

Sous le titre, la ligne `+12 cloud · +9 devops_ci · +10 contrat 2 ans` **explique
le score**. Si un résultat te surprend, c'est là que tu vois pourquoi.

Les étiquettes à surveiller :

- `rythme 3/1` — le rythme est explicitement compatible avec SUPINFO
- `Bac+4 / M1` — l'offre vise ton niveau
- `⚠ score sur titre seul` — la description n'a pas encore été récupérée, le
  score est sous-évalué. Elle le sera au prochain run.
- `⚠ école/CFA` — c'est une école qui recrute ses étudiants, pas un employeur
- `⚠ hors cible` — l'intitulé n'est pas un poste technique

### Section « Marché caché »

**2 482 entreprises qui recrutent régulièrement en alternance sans publier
d'offre**, dont **904 avec un numéro de téléphone direct** (bouton ☎).

C'est le canal au meilleur taux de retour, parce que personne n'y postule.
Le tri se fait sur le code NAF, qui dit le métier réel de l'entreprise :
`Programmation informatique`, `Conseil en systèmes et logiciels`,
`Édition de logiciels` sont en tête.

Manière efficace de s'en servir : prends 5 entreprises par jour dans le haut de
la liste, appelle, demande la personne qui s'occupe de l'alternance. Tu as un
argumentaire court et solide — M1 MSc Cloud & Mobile, rythme **3 semaines
entreprise / 1 semaine école**, démarrage **mi-septembre 2026**, et un stage de
développement IA en cours.

### Trier et filtrer le digest

```powershell
.\radar.ps1 report --score-min 40            # seulement les meilleures
.\radar.ps1 report --max-age 1               # publiées dans les 24 h, vérifié
.\radar.ps1 report --tri date                # les plus récentes d'abord
.\radar.ps1 report --tri entreprise          # groupées par employeur
.\radar.ps1 report --statut shortlisted      # ta sélection uniquement
```

Les options se combinent :

```powershell
.\radar.ps1 report --max-age 1 --tri date --score-min 30
```

`--tri` accepte `score` (défaut), `date`, `entreprise` et `ville`.

**`--max-age` est une vérification stricte** : une offre sans date de
publication est écartée, précisément parce qu'on ne *peut pas* vérifier son
âge. Le nombre d'offres écartées pour cette raison est affiché en tête du
digest — pas de suppression silencieuse.

---

## 4 bis. Écarter des secteurs entiers

Banque, assurance et armement sont **exclus par défaut**. Ce ne sont pas de
simples malus : ces offres n'apparaissent pas du tout.

Pour changer la liste, ouvre `config.yaml` à la section `exclusions` :

```yaml
exclusions:
  secteurs:
    banque: ["banque", "bnp", "credit agricole", ...]
    assurance: ["axa", "allianz", "mutuelle", ...]
    armement: ["thales", "naval group", "dassault", ...]
  exceptions:
    - "cegedim"     # éditeur de logiciels POUR les assureurs
```

Puis `.\radar.ps1 rescore`.

Trois choses à savoir :

- **Rien n'est supprimé.** Le motif d'exclusion est stocké en base. Retire un
  secteur de la liste, relance `rescore`, et toutes ses offres reviennent.
- **Le filtre porte sur le nom de l'entreprise et son code NAF**, jamais sur le
  texte de l'offre — sinon une annonce de développeur citant « secteur
  bancaire » parmi ses clients serait écartée à tort.
- **Les `exceptions` l'emportent.** Toute liste sectorielle produit des faux
  positifs : « Banques Alimentaires » n'est pas une banque, « Cegedim
  Assurances » édite du logiciel pour les assureurs sans en être un. C'est là
  qu'on les rattrape.

`.\radar.ps1 stats` indique combien d'offres sont écartées et pourquoi.

---

## 5. Suivre tes candidatures

C'est ce qui évite de postuler deux fois à la même offre. **Pas besoin de
recopier un identifiant** : un bout de nom d'entreprise suffit.

```powershell
.\radar.ps1 mark wijin applied --notes "CV + LM envoyés le 15/08"
.\radar.ps1 mark "worldline devops lyon" shortlisted
.\radar.ps1 mark chanel rejected --notes "exige M2"
```

Si plusieurs offres correspondent, le programme les liste avec leur
identifiant — tu affines ou tu reprends celui qui va bien :

```
  2 offres correspondent à « wijin ». Précise, ou reprends un identifiant :
    linkedin:4451593679    66  Alternant Développeur Fullstack Java-Angular | Wijin
    lba_entreprise:6a00…   25  Recrute en alternance (spontanée)            | WIJIN
```

Une offre marquée `applied` ou `rejected` **disparaît de la liste de travail**
et reparaît en bas du digest, section « Mes candidatures », avec ta note. Pour
voir l'ensemble du pipeline :

```powershell
.\radar.ps1 suivi
```

Les cinq statuts :

| Statut | Sens |
|---|---|
| `new` | Jamais traitée (par défaut) |
| `seen` | Lue, pas retenue pour l'instant |
| `shortlisted` | À traiter, candidature à préparer |
| `applied` | Candidature envoyée |
| `rejected` | Écartée, ou réponse négative |

Ces statuts **survivent aux collectes** : un run quotidien ne les écrase jamais.

Pour retrouver ta liste de travail :

```powershell
.\radar.ps1 report --statut shortlisted
.\radar.ps1 suivi                            # pipeline complet + notes
.\radar.ps1 stats                            # compte par statut
```

### Les doublons

Tu ne verras jamais deux fois la même offre, même publiée sur plusieurs sites.
Le rapprochement se fait sur **entreprise + intitulé + ville**, avec l'intitulé
réduit à un ensemble de mots trié et débarrassé des marqueurs de genre — sinon
« Data scientist (F/H) - alternance » et « Data Scientist - Alternance H/F »
compteraient pour deux.

Sur la base actuelle : **359 doublons** neutralisés, dont des offres Valeo,
Thales et Worldline présentes à la fois sur LinkedIn et HelloWork.

---

## 6. Ajuster le scoring

Tout se règle dans `config.yaml`, et **aucun changement ne nécessite de
re-scraper** :

```powershell
notepad config.yaml
.\radar.ps1 rescore      # recalcule toute la base en quelques secondes
.\radar.ps1 report
```

Ce que tu voudras probablement toucher :

- **`scoring.competences`** — les poids par techno. Cloud et mobile sont à 12
  (spécialité du MSc), Python à 9, PHP à 2. Monte ce qui t'intéresse.
- **`scoring.bonus` / `malus`** — le rythme 3/1 vaut +12, un contrat de 2 ans
  +10, un « Bac+5 déjà validé exigé » −25.
- **`scoring.entreprise.hors_cible`** — la liste des écoles/CFA à déclasser.
  Si une école passe encore au travers, ajoute son nom ici.
- **`recherche.mots_cles`** — ce que le programme cherche sur LinkedIn.

Après un `rescore`, compare avec `report --score-min 40` pour voir l'effet.

---

## 7. Les posts LinkedIn (à configurer une fois)

Les posts de recruteurs donnent souvent une **adresse e-mail directe**, ce que
les annonces classiques ne donnent jamais.

```powershell
.\radar.ps1 posts                  # posts seuls
.\radar.ps1 tout --avec-posts      # tout en une commande : offres + posts
```

Un run relève deux choses :

1. **Les comptes suivis** (`posts.comptes_suivis` dans `config.yaml`) — des
   comptes qui publient régulièrement des offres d'alternance. `Adopte1Dev` en
   publie une par jour ouvré. C'est le canal le plus rentable *et* le plus sûr :
   une page consultée, aucune requête au moteur de recherche. Ajoutes-y ceux que
   tu repères dans le digest, au format `company/<slug>` ou `in/<slug>` tel
   qu'il apparaît dans l'URL LinkedIn.
2. **Les recherches par mots-clés**, plafonnées à 5 par run.

Les posts de **candidats sont écartés automatiquement** — ce sont tes
concurrents, pas des employeurs. La distinction se joue à un mot près :
« recherche une **alternance** » (il en cherche une) contre « recherche un
**alternant** » (il en recrute un). Sur un run réel : 11 offres retenues,
19 posts de candidats écartés.

**La première fois**, une fenêtre Chrome s'ouvre sur un profil dédié, séparé de
ton navigateur habituel. Connecte-toi à LinkedIn **à la main** — le programme ne
saisit aucun identifiant, c'est volontaire. Ensuite il reprend tout seul, et la
session est réutilisée aux fois suivantes.

Les adresses trouvées apparaissent en bouton `✉` dans le digest, y compris
lorsqu'elles sont camouflées : `contact [at] boite [dot] fr` est décodé
automatiquement.

**Pendant le run**, tu peux travailler ailleurs : le navigateur est lancé avec
les drapeaux qui désactivent la mise en veille des fenêtres en arrière-plan.
Évite quand même de changer d'onglet *dans cette fenêtre* ou de cliquer dans la
page — ça désynchronise l'extraction en cours.

**Ne mets jamais cette commande dans une tâche planifiée.** Un scraping du fil
qui se déclenche seul à heure fixe est le meilleur moyen de faire restreindre
ton compte. Lance-la à la main, quand tu es devant, une à deux fois par semaine.

### Le délai de 6 heures

Le programme **refuse** de relancer une collecte de posts moins de 6 heures
après la précédente :

```
dernière collecte il y a 0.4 h — minimum 6 h. Relance après 08:59.
```

Ce n'est pas de la prudence excessive. Vérifié le 13/08/2026 : **quatre
collectes en vingt minutes** ont suffi pour que LinkedIn coupe la recherche de
contenu — résultats vides, sans message d'erreur, pendant plusieurs heures. La
session restait valide et le compte intact, mais l'outil ne ramenait plus rien.

Chaque run se limite aussi aux **5 premiers mots-clés** (`max_recherches_par_run`),
parce que c'est l'accumulation de recherches, plus que le défilement, qui
déclenche la coupure. Réordonne la liste `posts.mots_cles` dans `config.yaml`
pour changer lesquels passent en premier.

`--force` outrepasse le délai. À n'utiliser qu'en sachant ce qu'on risque.

---

## 8. Automatiser la collecte

Pour que la collecte tourne toute seule à 7 h :

```powershell
schtasks /create /tn "AlternanceRadar" /sc daily /st 07:00 `
  /tr "powershell -File C:\Users\stageinfo\alternance-radar\run_quotidien.ps1"
```

`run_quotidien.ps1` enchaîne LinkedIn + HelloWork + LBA + digest. Les posts en
sont volontairement exclus (voir §7).

Pour arrêter : `schtasks /delete /tn "AlternanceRadar" /f`

---

## Postuler — pré-remplissage assisté

```powershell
.\radar.ps1 postuler explain
.\radar.ps1 postuler glassdoor:1010226330218
```

Tu peux donner l'identifiant complet **ou un bout du nom** de l'entreprise ou
du poste. Si plusieurs offres correspondent, elles s'affichent pour que tu
précises.

**Le programme n'envoie jamais rien.** Le déroulé est en trois temps, et tu en
tiens deux :

| | Qui | Quoi |
|---|---|---|
| 1 | le programme | ouvre l'offre dans Chrome |
| | **toi** | navigues jusqu'au formulaire, puis Entrée |
| 2 | le programme | remplit ce qu'il reconnaît, liste ce qu'il a laissé |
| | **toi** | relis, complètes, **cliques sur Envoyer** |
| 3 | **toi** | réponds « o » |
| | le programme | marque l'offre comme envoyée |

Ce qu'il remplit : prénom, nom, e-mail, téléphone, ville, code postal,
LinkedIn, CV, lettre de motivation, et le champ « message » à partir du
gabarit de `config.yaml` — rempli avec les données **réelles** de l'offre
(intitulé, entreprise, ville, compétences effectivement citées).

Ce qu'il ne touche **jamais** : les mots de passe, les cases à cocher (RGPD,
consentements), les boutons radio, les champs déjà remplis par le site, et
tout champ qu'il ne reconnaît pas — il te les signale à la place.

> **Relis toujours le message avant d'envoyer.** C'est un gabarit, pas une
> lettre écrite pour cette offre-là. C'est précisément ce qui sépare une
> candidature d'un publipostage, et les recruteurs font la différence.

### Indeed et Glassdoor : à faire à la main

Ces deux sites opposent une **vérification Cloudflare** au navigateur piloté.
Elle n'est pas contournable proprement, et leurs conditions interdisent
l'accès automatisé — le programme te prévient donc *avant* d'ouvrir Chrome et
te donne l'adresse à ouvrir toi-même :

```
  ⚠  indeed oppose une vérification Cloudflare aux
     navigateurs pilotés. Le pré-remplissage n'aboutira pas.

     Ouvre l'offre à la main dans ton navigateur habituel :
     https://fr.indeed.com/viewjob?jk=c3eaae969f79ab73
```

Ce n'est pas une grande perte : sur les 139 offres à score ≥ 30 des quinze
derniers jours, **10 seulement** viennent d'Indeed ou de Glassdoor. Les 129
autres — LinkedIn, HelloWork, JobTeaser, Welcome to the Jungle, PASS,
adopte1dev — se pré-remplissent normalement.

À noter : la **collecte** de ces deux sites fonctionne parfaitement. Elle
passe par `curl_cffi`, qui rejoue l'empreinte TLS de Chrome et suffit aux
contrôles passifs. C'est le défi *actif*, celui qui exécute du JavaScript
dans un vrai navigateur, qui bloque la candidature.

Réglages dans `config.yaml`, bloc `candidature` : ton identité, les chemins du
CV et de la lettre, et le gabarit du message. **Ton téléphone y est vide** —
beaucoup de formulaires l'exigent, pense à le compléter.

---

## CV — génération adaptée à l'offre

```powershell
.\radar.ps1 cv explain
```

Réordonne `cv_source.yaml` (compétences, puces d'expérience, projets) selon
les tags déjà calculés pour l'offre — jamais de contenu inventé, jamais
l'ordre chronologique des postes réécrit. Produit un `.tex` dans
`store/cv/<uid>/`, et un PDF si `pdflatex` (MiKTeX, TeX Live) est installé.

Première utilisation : copie `cv_source.example.yaml` en `cv_source.yaml` et
remplis-le avec ton propre parcours (jamais versionné, comme `config.yaml`).

Avec `$env:GEMINI_API_KEY` défini (comme `LBA_API_KEY`), un court paragraphe
d'accroche personnalisé s'ajoute en tête, écrit par Gemini à partir des
seuls faits fournis (formation, rythme, compétences qui recoupent l'offre).
Sans la clé, ou si l'appel échoue, le CV se génère normalement, juste sans
ce paragraphe.

---

## 9. Toutes les commandes

```powershell
.\radar.ps1 tout        [--depuis X]           # tout + digest
.\radar.ps1 collect     [--depuis X]           # LinkedIn (offres)
.\radar.ps1 hellowork   [--depuis X]           # HelloWork
.\radar.ps1 indeed      [--depuis X]           # Indeed
.\radar.ps1 jobteaser   [--depuis X]           # JobTeaser
.\radar.ps1 wttj        [--depuis X]           # Welcome to the Jungle
.\radar.ps1 pass        [--depuis X]           # PASS — apprentissage fonction publique
.\radar.ps1 glassdoor   [--depuis X]           # Glassdoor
.\radar.ps1 lba         [--depuis X]           # La Bonne Alternance + marché caché
.\radar.ps1 posts       [--depuis X]           # posts LinkedIn (session requise)
.\radar.ps1 report      [--score-min N] [--statut S] [--max-age JOURS]
                        [--tri score|date|entreprise|ville]
.\radar.ps1 serve       [--port N] [mêmes filtres que report]
                        # digest avec les cases « déjà postulé » actives
.\radar.ps1 rescore                            # recalcule sans re-collecter
.\radar.ps1 stats                              # état de la base
.\radar.ps1 mark <uid> <statut> [--notes "…"]
.\radar.ps1 postuler <uid>                     # pré-remplit le formulaire (n'envoie rien)
.\radar.ps1 cv <uid>                           # CV LaTeX adapté à l'offre
```

Options de test, utiles pour vérifier un changement sans lancer un run complet :

```powershell
.\radar.ps1 collect --limite-requetes 2 --max-details 5
```

Ajoute `-v` n'importe où pour le détail des requêtes.

---

## 9. Vérifier que le bot n'est pas cassé

```powershell
.\radar.ps1 canari
```

```
  OK     linkedin       10 offres, tous champs remplis
                        title=100% company=100% url=100%
  OK     hellowork      30 offres, tous champs remplis
  CASSÉ  indeed         0 offres, 10 attendues au minimum
  ?      lba            3 offres, 5 attendues — trop peu pour conclure
  OK     lba_entreprise 150 offres, tous champs remplis
```

Une requête par source, la plus banale possible. **On ne regarde pas le code
HTTP** — un site cassé répond 200 tout aussi bien — mais ce qui sort du
parseur : volume plausible, et champs obligatoires réellement remplis.

Trois verdicts, pas deux. **`?`** veut dire « je ne peux pas conclure » : la
source a rendu moins que prévu sans être vide, ou l'échantillon est trop petit
pour qu'un pourcentage ait un sens. Seul un **zéro** vaut un `CASSÉ`.

La distinction n'est pas cosmétique. Les seuils sont calibrés sur la fenêtre
pleine de 14 jours ; en `--depuis jour`, Indeed et HelloWork filtrent côté
serveur et rendent légitimement dix fois moins. Sans cette nuance, un run
quotidien affichait `CASSÉ indeed — 9 offres, 10 attendues` tous les matins.
Un moniteur qui crie au loup chaque jour finit ignoré, ce qui est pire que pas
de moniteur du tout — les seuils s'ajustent donc à la fenêtre demandée.

Pourquoi c'est nécessaire : le pire défaut d'un scraper n'est pas de tomber en
panne, c'est de **réussir en silence**. Trois pannes réelles de ce projet sont
passées inaperçues sur le moment — la refonte du DOM LinkedIn (« 0 carte »
pendant deux runs), les descriptions HelloWork qui aspiraient la page entière,
et les coquilles vides signées « S'identifier sur LinkedIn ».

Le contrôle tourne **automatiquement après chaque collecte**, et l'historique
permet de voir une dégradation progressive :

```powershell
.\radar.ps1 canari --historique
```

Il signale aussi une **chute de volume** de plus de 40 % par rapport au
passage précédent — le cas d'un sélecteur sur trois qui casse, invisible
autrement puisque les champs restants sont toujours corrects.

Sondes et collectes forment **deux séries distinctes**, et ne sont jamais
comparées entre elles : une sonde ne charge qu'une page, une collecte pagine
jusqu'au bout. Les mélanger produisait une fausse alerte à chaque sonde suivant
une collecte — 10 offres contre 3 280, soit « 100 % de moins ».

La commande renvoie un code d'erreur si une source est en défaut — utile pour
être alerté depuis une tâche planifiée.

---

## 10. Dépannage

**« Aucune offre ne correspond aux critères »**
Le seuil est trop haut ou tout est déjà traité. Essaie
`.\radar.ps1 report --score-min 0`.

**Beaucoup d'étiquettes `⚠ score sur titre seul`**
Normal après une grosse collecte : les descriptions n'ont pas toutes été
récupérées. Relance la même commande le lendemain, le rattrapage se fait tout
seul et les scores se corrigent.

**Le run est très long**
C'est voulu. Les pauses entre requêtes évitent le blocage. Lance-le en fond :
`Start-Process powershell -ArgumentList "-File .\run_quotidien.ps1"`.

**`LBA_API_KEY absente`**
La clé n'est pas visible dans la session en cours. Ferme et rouvre PowerShell,
ou refais : `setx LBA_API_KEY "ta-clé"`.

**`argument --notes: expected one argument`**
PowerShell avale les chaînes vides : `--notes ""` n'arrive jamais jusqu'au
programme. Pour effacer une note, écris `--notes " "` (un espace), ou repasse
simplement le statut sans l'option.

**LinkedIn ne renvoie plus rien**
Probablement un blocage temporaire d'IP. Arrête tout, attends quelques heures,
et augmente `recherche.delai_entre_requetes` dans `config.yaml`.

**Les posts demandent une vérification LinkedIn**
Le programme s'arrête de lui-même. N'insiste pas : attends au moins 24 h, et
espace davantage les lancements.

---

## 11. Aller plus loin

La base est un simple fichier SQLite, interrogeable directement :

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3;d=sqlite3.connect('store/jobs.db');print(d.execute('SELECT COUNT(*) FROM jobs WHERE status=\"applied\"').fetchone())"
```

Champs utiles de la table `jobs` : `source`, `title`, `company`, `location`,
`url`, `posted_at`, `score`, `score_detail`, `tags`, `contacts`, `status`,
`notes`, `is_alternance`, `duplicate_of`.

Le HTML brut de chaque offre est archivé dans `store\raw\` : si un site change
sa mise en page, l'historique reste ré-analysable sans re-scraper.
