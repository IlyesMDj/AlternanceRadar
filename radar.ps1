# Raccourci d'invocation.
#
#   .\radar.ps1 tout --depuis jour
#
# ...au lieu de :
#
#   .\.venv\Scripts\python.exe main.py tout --depuis jour
#
# Fonctionne depuis n'importe quel dossier : les chemins sont résolus par
# rapport à l'emplacement du script, pas au répertoire courant.

$racine = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONIOENCODING = "utf-8"

$py = Join-Path $racine ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Error "Environnement Python absent. Recrée-le :`n  python -m venv `"$racine\.venv`""
    exit 1
}

& $py (Join-Path $racine "main.py") @args
