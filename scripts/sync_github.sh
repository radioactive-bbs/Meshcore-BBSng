#!/usr/bin/env bash
# Sync des internen master-Stands auf den oeffentlichen GitHub-Branch (github/main).
#
# NUR fuer Maintainer, NICHT Teil der Laufzeitumgebung. Wird manuell ausgefuehrt,
# NACHDEM ein Release auf dem Live-Server verifiziert wurde:
#
#   dev (Feature/Fix) --pull/restart--> QA testen
#     --fast-forward--> master --pull/restart--> Live testen
#       --sync_github.sh--> github/main (oeffentlich)
#
# Nimmt den AKTUELLEN Baum von master (keine interne Commit-Historie, keine
# alten Betreiberdaten aus fruehen Commits) und erzeugt daraus einen einzelnen
# neuen, sauberen Commit auf dem oeffentlichen "github"-Remote. Kein Force-Push
# noetig - jeder Sync haengt sich normal an die bestehende oeffentliche Historie an.
#
# Voraussetzung: Remotes "origin" (intern) und "github" (oeffentlich) sind
# konfiguriert, z.B.:
#   git remote add github https://github.com/<org>/<repo>.git
#
# Aufruf: bash scripts/sync_github.sh ["optionale Release-Notiz"]

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

git remote get-url github >/dev/null 2>&1 || {
    echo "FEHLER: Remote 'github' nicht konfiguriert. Siehe Kommentar oben." >&2
    exit 1
}

echo "Hole aktuellen Stand von origin/master und github/main..."
git fetch origin master
git fetch github main

CURRENT_BRANCH="$(git branch --show-current)"
TMP_BRANCH="github-sync-tmp"
git branch -D "$TMP_BRANCH" >/dev/null 2>&1 || true

git checkout -q -b "$TMP_BRANCH" github/main

# Arbeitsbaum + Index 1:1 auf origin/master bringen - inkl. Loeschungen von
# Dateien, die in master nicht mehr existieren (checkout -- . wuerde das nicht tun).
git read-tree -u --reset origin/master
git add -A

MASTER_SHA="$(git rev-parse --short origin/master)"
NOTE="${1:-}"
MSG="Sync: internal master @ ${MASTER_SHA}"
[ -n "$NOTE" ] && MSG="${MSG}

${NOTE}"

if git diff --cached --quiet; then
    echo "Keine Aenderungen gegenueber github/main - nichts zu tun."
    git checkout -q "$CURRENT_BRANCH"
    git branch -D "$TMP_BRANCH"
    exit 0
fi

git commit -q -m "$MSG"
git push github "${TMP_BRANCH}:main"

git checkout -q "$CURRENT_BRANCH"
git branch -D "$TMP_BRANCH"

echo "github/main aktualisiert (master @ ${MASTER_SHA})."
