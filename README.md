# alternance-radar

Radar d'offres d'alternance en développement / cloud / mobile / IA, calibré sur
le profil d'Ilyés Medjdoub (M1 — MSc Cloud & Mobile, SUPINFO Paris, rythme 3/1,
démarrage mi-septembre 2026, mobilité France entière).

L'outil collecte, dédoublonne, classe et **suit** les offres. Le suivi est
l'essentiel : ne jamais revoir deux fois la même offre, et savoir en un coup
d'œil où en est chaque candidature.

## Installation

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install httpx curl_cffi beautifulsoup4 lxml pyyaml playwright google-genai
```

## Utilisation

**Démarrage et usage quotidien : [`DEMARRAGE.md`](DEMARRAGE.md).**
**Détail de chaque réglage : [`TUTO.md`](TUTO.md).**
Ce README documente le fonctionnement interne et les choix techniques.

`radar.ps1` évite de retaper le chemin de l'interpréteur, depuis n'importe quel
dossier :

```powershell
.\radar.ps1 tout --depuis jour
```

```powershell
$py = ".venv\Scripts\python.exe"

& $py main.py tout                 # toutes les sources d'un coup + digest
& $py main.py collect              # offres LinkedIn (endpoint invité)
& $py main.py hellowork            # offres HelloWork
& $py main.py lba                  # La Bonne Alternance + marché caché
& $py main.py posts                # posts du fil LinkedIn (session requise)
& $py main.py canari               # contrôle que les collecteurs fonctionnent
& $py main.py suivi                # tableau de bord des candidatures
& $py main.py rescore              # re-score après édition de config.yaml
& $py main.py report               # génère digest.html
& $py main.py stats                # état de la base
& $py main.py mark linkedin:4268624962 applied --notes "candidature envoyée"
```

### Fenêtre de fraîcheur

`--depuis` s'applique à `tout`, `collect`, `hellowork`, `lba` et `posts` :

```powershell
& $py main.py tout --depuis jour        # publiées aujourd'hui / dernières 24 h
& $py main.py tout --depuis 3jours      # 72 h
& $py main.py tout --depuis semaine     # 7 jours
& $py main.py tout --depuis 2semaines   # 14 jours (défaut du config)
```

Chaque source traduit la fenêtre dans son propre langage — `f_TPR=r<secondes>`
pour LinkedIn, `datePosted` pour les posts (qui ne connaît que 24 h / semaine /
mois, d'où un affinage côté client sur « il y a 3 j »). Et dans **tous** les
cas, un rejet après coup sur la date de publication : LinkedIn est laxiste sur
son propre filtre et renvoie régulièrement plus ancien que demandé.

Deux exceptions assumées : les entrées sans date sont conservées (mieux vaut un
faux positif qu'une bonne offre écartée sur une donnée manquante), et les
entreprises du marché caché ne sont jamais filtrées par âge — c'est une liste
permanente, sans date de publication, que ce filtre viderait entièrement.

Statuts disponibles : `new`, `seen`, `shortlisted`, `applied`, `rejected`.

Options utiles pour tester sans lancer un run complet :
`--limite-requetes N` (n'exécute que les N premières requêtes),
`--max-details N` (plafonne les fiches détaillées).

## Comment ça marche

LinkedIn **n'a aucun filtre « alternance »** : les offres sont publiées sous des
types de contrat incohérents. La seule méthode fiable est donc de requêter
large puis de filtrer localement sur les marqueurs textuels (`alternance`,
`alternant`, `apprentissage`, `contrat de professionnalisation`, `work-study`…).

```
collectors/linkedin_guest.py   offres LinkedIn, sans authentification
collectors/hellowork.py        offres HelloWork, filtre c=Alternance natif
collectors/linkedin_posts.py   posts du fil, session Chrome dédiée
        ↓  Job normalisé
core/classify.py               détection alternance + rythme + niveau
core/score.py                  score de pertinence vs config.yaml
core/store.py                  SQLite + suivi de candidature
report.py                      digest HTML trié par score
```

### Sources

| Source | Accès | Rendement |
|---|---|---|
| **LinkedIn Jobs** | endpoint invité, aucune clé | 680 offres / 216 requêtes |
| **HelloWork** | libre, filtre `c=Alternance` natif | 400 offres / **33** requêtes |
| **LinkedIn Posts** | session Chrome dédiée, login manuel unique | contacts e-mail directs |
| **La Bonne Alternance** | clé gratuite, usage non lucratif | offres **+ marché caché** |
| **Indeed** | débloqué via `curl_cffi` — bloc JSON structuré | 60 offres / 19 requêtes |
| **JobTeaser** | `curl_cffi` + JSON-LD sur chaque fiche | 368 offres, 25/25 justes |
| **Welcome to the Jungle** | index Algolia public, clé interceptée | **362 offres / 15 requêtes** |
| **PASS (fonction publique)** | flux RSS officiel, sans clé | **68 offres / 1 requête**, 45 avec e-mail |
| **adopte1dev** | API WordPress ouverte, taxonomie de contrat | **43 alternances / 12 requêtes**, 100 % dev |
| **Glassdoor** | recherche autorisée par robots.txt | 169 offres / 8 requêtes, sans description |
| ~~ATS d'entreprise~~ | endpoints ouverts, mais **1 alternance sur ~900 offres** (voir ci-dessous) | abandonné |
| ~~Facebook~~ | **fermé** — HTTP 400/404 sans session, jusque sur une page publique | — |
| ~~X / Twitter~~ | **fermé** — HTTP 200 mais coquille SPA vide, contenu chargé après authentification | — |

**Welcome to the Jungle** est le meilleur rendement du projet, et la seule
source dont le filtre de contrat a été *vérifié* : `contract_type:APPRENTICESHIP`
fait tomber l'index de 88 210 à 2 287 offres, et 20/20 des remontées portent
bien la facette. On peut donc renseigner `contract_type` sans mentir.

Le site n'est pas scrapé : il interroge un index Algolia dont les identifiants
sont publics par conception. La clé est restreinte par `Referer` — et c'est ce
qui a permis de trouver le nom de l'index, car Algolia distingue ses refus : un
index inexistant répond « Index not allowed with this API key », un index
valide répond « Method not allowed with this referer ». Deux messages
différents, donc un oracle pour tester des noms.

**Les ATS d'entreprise** (Greenhouse, Lever, Ashby) ont été mesurés puis
écartés : sur ~900 offres réelles chez neuf entreprises — Datadog 424, Doctolib
137, Pennylane 125, Alan 90 — **une seule alternance**, chez BlaBlaCar. Ces
ATS sont américains et servent les recrutements permanents internationaux ;
l'alternance étant un contrat administratif français, elle passe par les
canaux français, déjà couverts ici. Quatre des huit slugs français testés
renvoyaient en plus un 404.

**PASS** est le meilleur rapport du projet : **une requête pour 68 offres**,
descriptions complètes incluses. C'est le flux RSS que l'État publie lui-même,
sans clé ni quota, et son `robots.txt` n'interdit rien. Drupal y expose des
champs Dublin Core que personne d'autre ne donne — `creator` porte **l'adresse
de candidature** (45 offres sur 46 en ont une), `type` le niveau requis,
`date` la prise de poste, `format` la durée. Deux limites assumées : le flux
ne pagine pas et se limite aux 68 offres les plus récentes, et l'offre
publique est majoritairement administrative — 12 titres sur 68 relèvent de la
tech. C'est une source de fraîcheur, qui prend sa valeur en tournant chaque
jour.

**Glassdoor** n'a posé aucune difficulté technique — c'est son `robots.txt`
qui borne le collecteur, et celui-ci s'y tient. La pagination (`_IP2`), les
liens `/partner/` et l'API sont interdits ; la recherche et les fiches
`/job-listing/` sont autorisées. D'où **une page de 30 offres par mot-clé**,
la profondeur venant du nombre de mots-clés.

L'URL de recherche mérite un mot : elle n'accepte aucun paramètre — `?sc.keyword=`
renvoie 404 — et encode le mot-clé dans un slug dont les suffixes sont des
**positions de caractères**. `france-alternance-développeur-emplois-SRCH_IL.0,6_IN86_KO7,29`
signifie « lieu en 0–6, pays 86, mot-clé en 7–29 ». La formule est reproduite
dans `_url_recherche()`, vérifiée à l'identique contre l'URL du site.

En revanche les fiches répondent **403 quoi qu'on fasse** : mesuré sur session
neuve, quatre fiches à quinze secondes d'intervalle, alors que la recherche
répond 200 juste avant et juste après — le blocage vise le chemin, pas nous.
Les offres Glassdoor n'ont donc pas de description et sont marquées « score
sur titre seul ». Ajouté à un recouvrement de 18 titres sur 30 avec la base
existante et à une fraîcheur médiocre (14 offres sur 169 de moins de quinze
jours), c'est la source la plus marginale du lot — mais elle reste honnête.

HelloWork a un bien meilleur rendement par requête que LinkedIn, et son filtre
de contrat natif dispense de toute détection textuelle.

### Filtrage — entièrement déterministe

Aucune dépendance à un modèle de langage : mots-clés pondérés, gates sur
l'intitulé et sur l'entreprise, exclusions sectorielles, dédoublonnage par
empreinte. Gratuit, instantané, reproductible — le même corpus donne toujours
le même classement, et chaque décision se lit dans `config.yaml`.

La contrepartie est connue : le filtrage lexical n'a pas accès au sens.
« Kaischool » passait le gate écoles, parce que les frontières de mot refusent
de matcher `school` à l'intérieur de `kaischool` — la même règle qui évite par
ailleurs quantité de faux positifs. Le remède est d'ajouter le motif exact,
pas de deviner.

#### Le courtage scolaire — la seule exclusion qui lit la description

Une liste de noms ne suffisait pas. AURLOM, Scholia, Wijin, l'ESG et Walter
Learning publiaient **192 offres** sous des raisons sociales qu'aucun mot-clé
ne devinait, alors que leur propre texte annonçait « le CFA Scholia », « L'école
Walter Learning », « 1 500 entreprises partenaires ». Ces annonces exigent de
s'inscrire chez elles — sans objet quand on est déjà à SUPINFO.

D'où une dérogation unique au principe « ne jamais exclure sur le texte » :
`exclusions.courtage_ecole` exige **deux marqueurs simultanés** dans les 1 500
premiers caractères — un marqueur scolaire (`cfa`, `école`, `centre de
formation`) **et** un marqueur de courtage (`entreprises partenaires`, `pour
l'une de ses entreprises`). Une mention isolée ne déclenche rien.

Mesuré sur 5 268 alternances : **205 offres écartées, aucune libérée à tort**,
et un seul faux positif au premier jet — GALILEO NETWORK, éditeur de logiciels
de 3-5 personnes, pris par le motif de nom `galileo` et non par la règle de
courtage. Corrigé en exigeant le nom complet, exactement comme « dassault » qui
emportait Dassault Systèmes.

### Indeed — le 403 n'était qu'une empreinte TLS

Indeed renvoyait **403 à toute requête `httpx`**. Diagnostic initial erroné :
« Cloudflare, hors de portée sans proxy payant ». En réalité ni IP bannie, ni
défi JavaScript — une simple détection d'**empreinte TLS**. `httpx` produit une
signature de handshake reconnaissable au premier octet ; `curl_cffi` rejoue
celle d'un vrai Chrome, même pile BoringSSL. Vérifié le 15/08/2026 :
**403 → 200**, sans proxy ni service payant.

À retenir : cela ne passe que les contrôles **passifs**. Un vrai « Checking
your browser » avec Turnstile exécute du JavaScript et resterait fermé.

Deux propriétés font d'Indeed la source la plus efficace du lot :

- les résultats sont dans un **bloc JSON** (`mosaic-provider-jobcards`), pas
  dans le HTML : aucun sélecteur CSS à casser, et un champ `jobTypes`
  (« Apprentissage », « Contrat pro ») qui déclare le contrat à la source ;
- les descriptions se récupèrent **par lots** via `/rpc/jobdescs?jks=a,b,c`,
  l'API de chargement différé du front d'Indeed — 56 descriptions en
  3 requêtes. `/viewjob`, lui, reste en 403 même avec les cookies de session.

Le collecteur **abandonne après 3 refus consécutifs** au lieu de réessayer :
s'obstiner face à un refus ne fait que l'aggraver.

#### Le piège du paramètre `fromage`

Indeed répond **403 à une valeur hors domaine**, pas 400. Seules `1`, `3`, `7`
et `14` sont acceptées ; `fromage=30` est refusé — et un 403 ressemble trait
pour trait à un bannissement. Sept heures ont été perdues à croire à un
blocage d'adresse IP avant que le test profil par profil ne montre que
`chrome124` passait parfaitement, et que la seule variable était ce paramètre.

`_fromage()` ramène donc toute demande à la plus petite fenêtre native qui la
couvre, et **omet le paramètre au-delà de 14 jours** : le filtrage par date est
de toute façon refait côté client, et mieux vaut pas de filtre qu'un 403.

Leçon générale, valable pour les autres collecteurs : **un 403 ne prouve pas
un blocage.** Avant de conclure, faire varier un paramètre à la fois.

### La Bonne Alternance — le marché caché

`GET /api/job/v1/search` renvoie **trois blocs** :

```json
{ "jobs": [...], "recruiters": [...], "warnings": [...] }
```

`recruiters` est ce qu'aucune autre source ne donne : des entreprises
identifiées comme recrutant régulièrement en alternance **sans avoir publié
d'offre** (400 000 en 2025), chacune avec nom, SIRET, effectif, site web et un
contact direct (téléphone + URL de candidature). En alternance, la candidature
spontanée sur cette liste a un bien meilleur retour qu'une annonce publique.

Les `jobs` mappent directement sur le scoring déjà en place : `contract.duration`
→ bonus 2 ans, `contract.start` → démarrage septembre 2026, `target_diploma` →
niveau, `contract.remote` → télétravail.

```powershell
$env:LBA_API_KEY = "ta-clé"     # compte gratuit sur api.apprentissage.beta.gouv.fr
& $py main.py lba
```

L'API est **gratuite mais réservée aux usages non lucratifs** — une recherche
personnelle entre dans ce cadre, la revente de données non.

Mesuré sur l'API réelle : **quota de 60 requêtes / 60 s** (en-têtes
`x-ratelimit-*`), et une seule requête parisienne renvoie **2 offres pour 150
entreprises du marché caché**. Le délai est réglé à 1,2 s — 1,0 tomberait pile
sur la limite. Sur 429, le collecteur respecte `retry-after` au lieu de deviner.

Deux conséquences dans le code :

- **Le NAF porte tout le tri du marché caché.** Sans offre publiée, le code
  d'activité (`6201Z — Programmation informatique`) est le seul signal du métier
  réel de l'entreprise : d'où un bonus de 25 points, le plus élevé du barème.
- **Le digest a deux sections.** Une entreprise sans description de poste ne
  peut pas concourir au score contre une annonce détaillée : elle serait
  systématiquement enterrée, alors que c'est le canal au meilleur retour.

Les dates sont converties avant scoring : `contract.start = "2026-09-01"` devient
« septembre 2026 » et `contract.duration = 24` devient « contrat sur 2 ans » —
servis en ISO, les bonus correspondants ne se déclencheraient jamais. Le filtrage
se fait sur `offer.status` et `publication.expiration`, pas sur l'âge : LBA garde
des offres actives plusieurs semaines et l'expiration fait autorité.

### Posts du fil LinkedIn — première utilisation

Les posts de recruteurs donnent un **contact e-mail direct**, ce que les offres
passant par un ATS ne donnent jamais. Mais la recherche de contenu exige une
session : il n'existe pas d'endpoint invité pour le fil.

```powershell
& $py main.py posts     # la 1re fois : une fenêtre Chrome s'ouvre, connecte-toi
```

Ce que fait le collecteur pour ne **jamais** toucher à ta session principale :

- profil Chrome **dédié** dans `.chrome-profile/`, séparé de ton profil habituel —
  ton navigateur de tous les jours n'est ni lu, ni ouvert, ni modifié ;
- **aucun login automatisé** : tu te connectes à la main, une fois. C'est
  précisément le login programmatique que LinkedIn détecte le mieux ;
- vrai Chrome (`channel="chrome"`), `navigator.webdriver` neutralisé ;
- navigation en **lecture seule** — aucun like, aucun follow, aucun clic social ;
- défilement volontairement limité à 6 crans : un scroll infini se repère ;
- **arrêt immédiat** si une page de vérification apparaît.

Les adresses sont désobfusquées au passage : `contact [at] boite [dot] fr`
devient `contact@boite.fr`, et s'affiche en bouton `mailto:` dans le digest.

#### Architecture en deux temps

La session ne sert qu'à **récolter les URL**. La lecture se fait ensuite par un
client HTTP anonyme, hors navigateur — moins de temps passé dans la session
authentifiée, donc moins d'exposition, et un texte complet plutôt que tronqué.

Deux propriétés mesurées sur un post réel rendent ça possible :

- **Seule l'URL « vanity »** (`/posts/auteur_hashtags-share-ID-code`) sert le
  contenu à un visiteur anonyme. L'URL canonique
  `/feed/update/urn:li:activity:ID` ne renvoie que la page marketing de
  LinkedIn — 203 caractères de « Plus de 500 millions de membres ». Le
  collecteur relève donc l'URL du lien au lieu d'en reconstruire une.
- **La date exacte se déduit de l'identifiant d'activité**, sans rien
  télécharger : LinkedIn encode l'horodatage de création dans ses 41 bits de
  poids fort, `id >> 22` donne les millisecondes depuis l'epoch. C'est la seule
  façon de tenir une vérification stricte à 24 h — « il y a 2 sem. » lu dans le
  DOM ne le permettrait jamais.

Trois détails de conception qui comptent :

- **Endpoint invité, pas de session connectée.** Aucun compte LinkedIn n'est
  engagé, donc rien à faire bannir. En contrepartie : ~1000 résultats max par
  requête, contournés en découpant par mot-clé × lieu × fenêtre de 24 h.
- **Archivage du HTML brut** dans `store/raw/`. Le jour où LinkedIn change son
  markup — ça arrivera — on re-parse l'historique sans re-scraper.
- **Rattrapage du backlog.** Un run plafonné à `max_details_par_run` laisse des
  offres sans description, scorées sur leur seul titre. Elles sont reprises au
  run suivant ; après trois échecs (offre expirée) elles sortent de la file.

### Pièges déjà traités

| Piège | Traitement |
|---|---|
| « apprentissage automatique » = *machine learning*, pas alternance | exclusion des faux amis dans `classify.py` |
| « Bac+5 » veut souvent dire *préparant un* Bac+5 → c'est le profil | malus uniquement sur « Bac+5 validé/obtenu » |
| « auto**mobile** » matchait « mobile » | matching à frontières de mot dans `score.py` |
| L'endpoint renvoie 10 cartes/page, pas 25 | curseur de pagination sur le nombre réellement reçu |
| Un « Chargé de marketing digital » citant Data/IT/cloud battait un vrai poste de dév | gate métier sur l'**intitulé seul** (`scoring.titre`) |
| `f_WT` (télétravail) est **ignoré** par l'endpoint invité | passe remote supprimée ; le télétravail est détecté dans le texte |
| Les CFA et écoles (EPSI, Studi, Sup de Vinci…) publient sur HelloWork pour recruter **leurs** étudiants | gate sur le **champ entreprise seul** (`scoring.entreprise`) — pas sur le texte global, où « formation » est légitime |
| HelloWork garantit l'alternance par son filtre, mais 25 offres n'avaient aucun marqueur textuel | `est_alternance` fait foi au `contract_type` de la source |
| …et faire confiance à ce champ le rendait auto-confirmant | `annotate` n'écrit plus jamais dans `contract_type` |
| `Data scientist (F/H) - alternance` ≠ `Data Scientist - Alternance H/F` : la même offre AXA en double | clé de dédoublonnage sur l'ensemble **trié** des mots, jetons d'une lettre retirés (`h`, `f`, le `e` d'`Apprenti(e)`) |
| LinkedIn écrit `Nanterre, Île-de-France`, HelloWork `Nanterre - 92` | `city_of` découpe sur les deux séparateurs et retire les arrondissements |

Le dédoublonnage inter-sources est passé de 5 à **45 doublons** grâce à ces deux
dernières corrections. `rescore` recalcule aussi les clés et re-marque les
doublons — pas besoin de repartir d'une base vide après un changement de règle.

`rescore` permet de calibrer les poids sans relancer une seule requête : la
base garde les descriptions, seuls les champs calculés sont recalculés.

## Configuration

Tout est dans `config.yaml` : mots-clés, villes, poids par technologie, bonus et
malus. Le scoring est calibré sur le CV **et** sur la spécialité du MSc — cloud
et mobile pèsent le plus lourd, puis les différenciants rares chez un alternant
(Playwright, TDD, Jenkins, Clean Archi).

Modifier les poids ne nécessite aucun re-scraping : le prochain `collect`
re-score toute la base.

## Automatisation quotidienne

Planificateur de tâches Windows, tous les jours à 7 h :

`run_quotidien.ps1` enchaîne les sources sans session, puis génère le digest.
Les posts LinkedIn restent **manuels** : ils ouvrent une fenêtre Chrome, et un
scraping du fil déclenché sans surveillance est exactement ce qu'il ne faut pas
automatiser.

```powershell
schtasks /create /tn "AlternanceRadar" /sc daily /st 07:00 ^
  /tr "powershell -File C:\Users\stageinfo\alternance-radar\run_quotidien.ps1"
```

## À venir

- **API France Travail** (`natureContrat` apprentissage / professionnalisation) —
  officielle et gratuite, nécessite un compte sur francetravail.io.
- **La Bonne Alternance** — apporte le *marché caché* : les entreprises qui
  recrutent régulièrement en alternance sans publier d'offre. En alternance, la
  candidature spontanée sur cette liste a un bon taux de retour.
- Scoring par LLM comparant la description au CV (score de fit + arguments à
  reprendre en lettre de motivation).

## Cadre d'usage

Les CGU LinkedIn interdisent le scraping automatisé. La jurisprudence *hiQ v.
LinkedIn* a établi que collecter de la donnée publique n'est pas du piratage,
mais cela reste une violation contractuelle : LinkedIn peut bloquer l'IP.

Cet outil vise un usage personnel, à volume modeste, sur des données publiques
non authentifiées, sans revente ni rediffusion. Le throttling (3 s entre
requêtes, jitter, backoff exponentiel sur 429/999) est là pour rester dans des
volumes raisonnables. Les noms de recruteurs ne sont pas collectés.
