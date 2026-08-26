"""Serveur local du digest — cases à cocher, filtres, et mise à jour.

Le digest est un fichier HTML statique : ouvert en `file://`, il ne peut rien
enregistrer ni rien re-filtrer. Cocher « déjà postulé » exigeait de recopier un
`uid` dans `main.py mark`, ce que personne ne fait — 9 637 offres en base,
aucune marquée.

Ce serveur comble ce trou, avec quatre partis pris :

- **bibliothèque standard uniquement.** `http.server` suffit pour un usage
  local mono-utilisateur ; ajouter Flask pour trois routes serait une
  dépendance de plus à installer et à maintenir ;
- **`HTTPServer` et non `ThreadingHTTPServer`.** Les requêtes sont traitées en
  série, dans le fil qui appelle `serve_forever()` — donc celui qui a ouvert
  SQLite. Un serveur multi-fils exigerait `check_same_thread=False` et un
  verrou, pour un gain nul sur une page consultée par une seule personne ;
- **écoute sur 127.0.0.1 seulement.** La base contient des contacts directs et
  l'historique de candidatures ; rien de tout cela n'a à sortir de la machine.
  Aucune authentification n'est nécessaire tant que rien n'écoute au-dehors ;
- **les collectes tournent en sous-processus**, pas dans un fil. Elles durent
  de trente secondes à plus d'une heure : les exécuter ici figerait la page, et
  les mettre dans un fil imposerait de partager la connexion SQLite. Un
  sous-processus a la sienne, isole les pannes, et réutilise exactement le
  chemin de code déjà éprouvé en ligne de commande.

Le HTML est reconstruit à chaque chargement : après avoir coché une case ou
changé un filtre, la page reflète l'état réel de la base.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import report
from core.store import STATUTS, Store

log = logging.getLogger("serveur")

HOTE = "127.0.0.1"
RACINE = Path(__file__).resolve().parent

# Ce qu'on accepte de lancer. Liste blanche stricte : le nom vient du
# navigateur et part dans un `subprocess`, il n'a rien à faire d'arbitraire.
# `posts` en est délibérément absent — fenêtre Chrome, session LinkedIn, six
# heures de battement obligatoire : ça se lance à la main, en connaissance.
COLLECTES_ADMISES = {"tout", "collect", "hellowork", "indeed",
                     "jobteaser", "wttj", "pass", "glassdoor",
                     "adopte1dev", "devitjobs", "welovedevs", "lba"}
FENETRES_ADMISES = {"jour", "3jours", "semaine", "2semaines"}

# Le journal montré dans la page. Assez pour suivre l'avancement, pas assez
# pour que la réponse JSON grossisse indéfiniment sur une collecte d'une heure.
LIGNES_JOURNAL = 120


class Collecte:
    """Une collecte en sous-processus, et son journal consultable."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lignes: deque[str] = deque(maxlen=LIGNES_JOURNAL)
        self.commande = ""

    @property
    def encours(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def lancer(self, source: str, depuis: str | None) -> None:
        if self.encours:
            raise RuntimeError("une collecte est déjà en cours")

        argv = [sys.executable, str(RACINE / "main.py"), source]
        if depuis:
            argv += ["--depuis", depuis]

        # PYTHONUNBUFFERED : sans lui, la sortie du sous-processus reste dans
        # son tampon et le journal n'affiche rien avant la toute fin.
        env = {**os.environ, "PYTHONUNBUFFERED": "1",
               "PYTHONIOENCODING": "utf-8"}

        self.lignes.clear()
        self.commande = f"{source}" + (f" --depuis {depuis}" if depuis else "")
        self.lignes.append(f"$ main.py {self.commande}")

        self.proc = subprocess.Popen(
            argv, cwd=RACINE, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._absorber, daemon=True).start()
        log.info("collecte lancée : %s", self.commande)

    def _absorber(self) -> None:
        """Lit la sortie du sous-processus au fil de l'eau."""
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        for ligne in proc.stdout:
            ligne = ligne.rstrip()
            # Une ligne par requête HTTP noierait tout le reste.
            if not ligne or "HTTP Request:" in ligne:
                continue
            self.lignes.append(ligne)
        code = proc.wait()
        self.lignes.append(f"— {self.commande} terminé (code {code})")
        log.info("collecte terminée : %s (code %s)", self.commande, code)

    def etat(self) -> dict:
        return {"encours": self.encours, "journal": list(self.lignes),
                "commande": self.commande}


def _entier(valeurs: dict, cle: str, defaut):
    """Lit un entier d'une query string, sans jamais lever."""
    brut = (valeurs.get(cle) or [""])[0].strip()
    if brut == "":
        return defaut
    try:
        return int(brut)
    except ValueError:
        return defaut


def fabriquer(store: Store, cfg: dict, defauts: dict, collecte: Collecte):
    """Construit la classe de traitement, fermée sur ses dépendances."""

    class Digest(BaseHTTPRequestHandler):
        server_version = "RadarAlternance"

        # Le journal par défaut écrit une ligne par requête sur stderr, ce qui
        # noie la sortie utile. On ne garde que ce qu'on décide de tracer.
        def log_message(self, format, *args):
            pass

        def _repondre(self, code: int, corps: bytes, type_mime: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", type_mime)
            self.send_header("Content-Length", str(len(corps)))
            # La page change à chaque candidature cochée et à chaque collecte :
            # la cacher ferait réafficher une liste périmée.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corps)

        def _texte(self, code: int, message: str) -> None:
            self._repondre(code, message.encode("utf-8"),
                           "text/plain; charset=utf-8")

        def _json(self, code: int, charge: dict) -> None:
            self._repondre(code, json.dumps(charge, ensure_ascii=False)
                           .encode("utf-8"), "application/json; charset=utf-8")

        def _options(self, requete: str) -> dict:
            """Fusionne les filtres d'URL avec ceux passés en ligne de commande."""
            q = parse_qs(requete, keep_blank_values=True)
            if not q:
                return {**defauts, "sources": None}

            sources = [s for s in q.get("src", []) if s.strip()]
            tri = (q.get("tri") or [defauts["tri"]])[0]
            return {
                "score_min": _entier(q, "score", defauts["score_min"]),
                "statut": defauts["statut"],
                # `age` est en HEURES et vide = sans limite : c'est une
                # valeur choisie, pas une absence, d'où `keep_blank_values`.
                "age_max_heures": _entier(q, "age", None) if "age" in q
                                  else defauts["age_max_heures"],
                "tri": tri if tri in Store.TRIS else defauts["tri"],
                "sources": sources or None,
            }

        def do_GET(self) -> None:
            url = urlparse(self.path)

            if url.path == "/api/collecte":
                self._json(200, collecte.etat())
                return

            if url.path not in ("/", "/index.html"):
                self._texte(404, "introuvable")
                return

            # « tout afficher » : on renvoie sur l'URL nue plutôt que de
            # bricoler un état vide, pour que l'adresse reflète la vue.
            if "raz" in parse_qs(url.query, keep_blank_values=True):
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            page = report.construire(store, cfg, servi=True,
                                     **self._options(url.query))
            self._repondre(200, page.encode("utf-8"),
                           "text/html; charset=utf-8")

        def _corps_json(self) -> dict | None:
            # Borne de lecture : sans elle, un corps arbitrairement long
            # tiendrait le serveur mono-fil indéfiniment.
            taille = int(self.headers.get("Content-Length") or 0)
            if taille <= 0 or taille > 4096:
                self._texte(400, "corps absent ou démesuré")
                return None
            try:
                return json.loads(self.rfile.read(taille))
            except ValueError as e:
                self._texte(400, f"requête illisible : {e}")
                return None

        def do_POST(self) -> None:
            chemin = urlparse(self.path).path
            if chemin == "/api/statut":
                self._statut()
            elif chemin == "/api/collecte":
                self._collecte()
            else:
                self._texte(404, "introuvable")

        def _statut(self) -> None:
            donnees = self._corps_json()
            if donnees is None:
                return
            try:
                uid, statut = str(donnees["uid"]), str(donnees["statut"])
            except (KeyError, TypeError) as e:
                self._texte(400, f"requête illisible : {e}")
                return

            if statut not in STATUTS:
                self._texte(400, f"statut « {statut} » inconnu")
                return

            # `set_status` retourne False si aucune ligne ne porte cet uid. Le
            # signaler importe : sans ça, la case resterait cochée à l'écran
            # alors que rien n'aurait été enregistré.
            if not store.set_status(uid, statut):
                self._texte(404, f"offre {uid} introuvable")
                return
            log.info("%s → %s", uid, statut)
            self._json(200, {"uid": uid, "statut": statut})

        def _collecte(self) -> None:
            donnees = self._corps_json()
            if donnees is None:
                return
            source = str(donnees.get("source", ""))
            depuis = str(donnees.get("depuis", "") or "")

            if source not in COLLECTES_ADMISES:
                self._texte(400, f"source « {source} » non lançable ici")
                return
            if depuis and depuis not in FENETRES_ADMISES:
                self._texte(400, f"fenêtre « {depuis} » inconnue")
                return

            try:
                collecte.lancer(source, depuis or None)
            except RuntimeError as e:
                self._texte(409, str(e))
                return
            except OSError as e:
                self._texte(500, f"lancement impossible : {e}")
                return
            self._json(202, collecte.etat())

    return Digest


class _Serveur(HTTPServer):
    def handle_error(self, request, client_address) -> None:
        """Le navigateur ferme parfois la connexion avant la fin de l'envoi —
        onglet fermé, page rechargée en plein chargement. Rien d'anormal
        pour un usage local mono-utilisateur, mais `socketserver` en
        afficherait par défaut la trace complète à chaque fois. Toute AUTRE
        exception continue de s'afficher normalement : seules celles-ci sont
        du bruit attendu, pas les vraies pannes."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def servir(store: Store, cfg: dict, options: dict,
           port: int = 8765, ouvrir: bool = True) -> None:
    collecte = Collecte()
    httpd = _Serveur((HOTE, port), fabriquer(store, cfg, options, collecte))
    adresse = f"http://{HOTE}:{httpd.server_port}/"

    print(f"\n  Digest servi sur {adresse}")
    print("  Cases « déjà postulé », filtres par source et mise à jour actifs.")
    print("  Ctrl+C pour arrêter.\n")

    if ouvrir:
        webbrowser.open(adresse)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Serveur arrêté.\n")
    finally:
        if collecte.encours and collecte.proc:
            print("  (collecte en cours interrompue)")
            collecte.proc.terminate()
        httpd.server_close()
