# Run quotidien — sources sans session uniquement.
#
# Les posts LinkedIn ne sont VOLONTAIREMENT pas ici : ils ouvrent une fenêtre
# Chrome et nécessitent une session. Un scraping du fil déclenché sans
# surveillance, à heure fixe, est précisément le motif qu'il ne faut pas
# produire. Lance `main.py posts` à la main, quand tu es devant.

$ErrorActionPreference = "Continue"
$racine = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $racine ".venv\Scripts\python.exe"
$app = Join-Path $racine "main.py"

$env:PYTHONIOENCODING = "utf-8"

Write-Output "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm') — collecte quotidienne ==="

& $py $app collect
& $py $app hellowork
& $py $app indeed
& $py $app jobteaser
& $py $app wttj
& $py $app pass
& $py $app adopte1dev
& $py $app devitjobs
& $py $app welovedevs
& $py $app glassdoor

# LBA n'a de sens que si la clé est disponible (persistée via setx).
if ($env:LBA_API_KEY) { & $py $app lba }
else { Write-Output "LBA_API_KEY absente — La Bonne Alternance ignorée" }

& $py $app report --score-min 20

Write-Output "=== terminé — digest : $(Join-Path $racine 'digest.html') ==="
