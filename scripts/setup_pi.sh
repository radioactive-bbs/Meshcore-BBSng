#!/usr/bin/env bash
# Meshcore BBSng – Raspberry Pi Ersteinrichtung
# Getestet auf: Raspberry Pi OS Lite 64-bit (Bookworm), Pi 3B+, Pi Zero 2 W
#
# Voraussetzungen:
#   - Raspberry Pi OS Lite 64-bit frisch installiert
#   - SSH-Zugang aktiv (z.B. als Benutzer "pi")
#   - Pi kann REPO_URL (unten) erreichen
#
# Aufruf (als pi oder anderer sudo-Benutzer):
#   bash setup_pi.sh

set -euo pipefail

# ── Konfiguration (bei Bedarf anpassen) ──────────────────────────────────────
REPO_URL="https://github.com/DEIN-USER/Meshcore-BBSng.git"   # eigene Repo-URL eintragen
BRANCH="main"                                                 # ggf. auf eigenen Branch anpassen
SERVICE_USER="coreadmin"
INSTALL_DIR="/home/${SERVICE_USER}/nnp-bbs"
SERVICE_NAME="nnp-bbs"

# Heltec WiFi LoRa 32 v4 – anpassen wenn anderes MeshCore-Geraet.
# UDEV_SERIAL: Seriennummer des eigenen Geraets ermitteln mit
#   udevadm info -a -n /dev/ttyACM0 | grep '{serial}' | head -1
UDEV_VENDOR="303a"
UDEV_PRODUCT="0002"
UDEV_SERIAL="AAAAAAAAAAAA"

# ── Ausgabe-Hilfsfunktionen ───────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
info() { echo -e "${YELLOW}[..]${NC}   $*"; }
err()  { echo -e "${RED}[ERR]${NC}  $*" >&2; exit 1; }

echo ""
echo "══════════════════════════════════════════════"
echo "  Meshcore BBSng Raspberry Pi Setup"
echo "══════════════════════════════════════════════"
echo ""

# ── Grundvoraussetzungen ──────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] || err "Nicht als root ausführen – sudo wird intern verwendet."
command -v sudo &>/dev/null || err "sudo nicht gefunden."

# ── Systempakete ──────────────────────────────────────────────────────────────
info "Paketliste aktualisieren..."
sudo apt-get update -q

info "Systempakete installieren (git, python3, python3-venv, Build-Header fuer cryptography)..."
# libssl-dev/libffi-dev/python3-dev/build-essential: falls PyPI kein passendes
# vorgebautes Wheel fuer cryptography (Pip-Abhaengigkeit, siehe requirements.txt)
# fuer die jeweilige Architektur/OS-Version hat, muss es aus dem Quellcode
# gebaut werden - ohne diese Header schlaegt der pip install dann fehl.
sudo apt-get install -y -q git python3 python3-venv python3-pip \
    libssl-dev libffi-dev python3-dev build-essential
ok "Systempakete bereit."

# ── Benutzer coreadmin anlegen ────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Benutzer $SERVICE_USER anlegen..."
    sudo useradd -m -s /bin/bash "$SERVICE_USER"
    ok "Benutzer $SERVICE_USER angelegt."
    echo ""
    echo -e "${YELLOW}HINWEIS:${NC} Passwort für $SERVICE_USER setzen (optional, für direkten Login):"
    echo "         sudo passwd $SERVICE_USER"
    echo ""
else
    ok "Benutzer $SERVICE_USER bereits vorhanden."
fi

# ── dialout-Gruppe (serielle Ports) ──────────────────────────────────────────
if ! groups "$SERVICE_USER" | grep -q dialout; then
    info "Benutzer $SERVICE_USER zur Gruppe dialout hinzufügen..."
    sudo usermod -aG dialout "$SERVICE_USER"
    ok "Gruppe dialout hinzugefügt."
else
    ok "Benutzer $SERVICE_USER bereits in Gruppe dialout."
fi

# ── Repository klonen / aktualisieren ────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Repository vorhanden – stelle sicher dass ${BRANCH}-Branch aktiv ist..."
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" fetch origin
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" checkout "$BRANCH"
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull origin "$BRANCH"
    ok "Repository aktualisiert."
else
    info "Klone Repository nach $INSTALL_DIR..."
    sudo -u "$SERVICE_USER" git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    ok "Repository geklont."
fi

# ── Python-Umgebung ───────────────────────────────────────────────────────────
VENV="$INSTALL_DIR/.venv"
if [[ ! -d "$VENV" ]]; then
    info "Virtuelle Umgebung anlegen..."
    sudo -u "$SERVICE_USER" python3 -m venv "$VENV"
fi

info "Python-Abhängigkeiten installieren..."
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" "$VENV/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
ok "Python-Umgebung bereit."

# ── Datenverzeichnis ──────────────────────────────────────────────────────────
sudo -u "$SERVICE_USER" mkdir -p "$INSTALL_DIR/data"
ok "Datenverzeichnis: $INSTALL_DIR/data"

# ── secrets.yaml ─────────────────────────────────────────────────────────────
SECRETS="$INSTALL_DIR/config/secrets.yaml"
if [[ ! -f "$SECRETS" ]]; then
    sudo -u "$SERVICE_USER" cp "$INSTALL_DIR/config/secrets.yaml.example" "$SECRETS"
    echo ""
    echo -e "${YELLOW}ACHTUNG:${NC} $SECRETS wurde aus dem Template erstellt."
    echo "         HA-Token eintragen:"
    echo "         sudo -u $SERVICE_USER nano $SECRETS"
    echo ""
else
    ok "secrets.yaml bereits vorhanden."
fi

# ── config.local.yaml (Betreiberdaten: Rufzeichen, QTH, MeshCore-Kanal, ...) ──
CONFIG_LOCAL="$INSTALL_DIR/config/config.local.yaml"
if [[ ! -f "$CONFIG_LOCAL" ]]; then
    sudo -u "$SERVICE_USER" cp "$INSTALL_DIR/config/config.local.yaml.example" "$CONFIG_LOCAL"
    echo ""
    echo -e "${YELLOW}ACHTUNG:${NC} $CONFIG_LOCAL wurde aus dem Template erstellt."
    echo "         Eigene Betreiberdaten eintragen (Rufzeichen, QTH, MeshCore-Kanal/-Kontakte):"
    echo "         sudo -u $SERVICE_USER nano $CONFIG_LOCAL"
    echo ""
else
    ok "config.local.yaml bereits vorhanden."
fi

# ── At-Rest-Schluessel (systemd Encrypted Credential) ────────────────────────
# Schluessel fuer die Verschluesselung privater Nachrichten. Bevorzugt als
# host-gebundenes, verschluesseltes systemd-Credential (kein Klartext auf der
# Platte, nicht im Repo/Backup, beim Start automatisch entschluesselt). Faellt auf
# secrets.yaml zurueck, falls systemd-creds fehlt oder dort schon ein Key steht.
CRED_DIR="/etc/${SERVICE_NAME}"
CRED_FILE="${CRED_DIR}/messages_key.cred"
LOAD_CRED_LINE=""
SECRETS_HAS_KEY=0
if [[ -f "$SECRETS" ]] && grep -qE '^\s*messages_key\s*:\s*\S' "$SECRETS"; then
    SECRETS_HAS_KEY=1
fi
if [[ -f "$CRED_FILE" ]]; then
    LOAD_CRED_LINE="LoadCredentialEncrypted=messages_key:${CRED_FILE}"
    ok "At-Rest-Schluessel: verschluesseltes systemd-Credential vorhanden."
elif [[ "$SECRETS_HAS_KEY" == "1" ]]; then
    ok "At-Rest-Schluessel: bereits in secrets.yaml (Legacy) konfiguriert."
elif command -v systemd-creds >/dev/null 2>&1; then
    info "At-Rest-Schluessel erzeugen und als systemd-Credential verschluesseln..."
    sudo mkdir -p "$CRED_DIR"
    NEW_KEY="$(sudo -u "$SERVICE_USER" "$VENV/bin/python" -c 'from core import crypto; print(crypto.generate_key())')"
    if printf '%s' "$NEW_KEY" | sudo systemd-creds encrypt --name=messages_key - "$CRED_FILE"; then
        sudo chmod 600 "$CRED_FILE"; sudo chown root:root "$CRED_FILE"
        LOAD_CRED_LINE="LoadCredentialEncrypted=messages_key:${CRED_FILE}"
        ok "At-Rest-Schluessel als verschluesseltes Credential abgelegt ($CRED_FILE)."
    else
        err "systemd-creds encrypt fehlgeschlagen – bitte Schluessel manuell setzen."
    fi
    unset NEW_KEY
else
    echo -e "${YELLOW}Hinweis:${NC} systemd-creds nicht verfuegbar – ohne Schluessel werden"
    echo "         private Nachrichten UNVERSCHLUESSELT gespeichert. Schluessel setzen:"
    echo "         $VENV/bin/python -c 'from core import crypto; print(crypto.generate_key())'"
    echo "         und als storage.messages_key in $SECRETS eintragen."
fi

# ── Web-Admin-Passwort ────────────────────────────────────────────────────────
# Wird als gesalzener scrypt-Hash in config/webconfig.yaml abgelegt (nie Klartext).
# Leere Eingabe erzeugt ein sicheres Zufallspasswort, das genau einmal angezeigt wird.
WEBCONFIG="$INSTALL_DIR/config/webconfig.yaml"
if [[ ! -f "$WEBCONFIG" ]] || ! grep -q "password_hash" "$WEBCONFIG"; then
    info "Web-Admin-Passwort setzen (Enter = Zufallspasswort generieren)..."
    read -rs -p "         Passwort: " WEB_PW || true; echo
    GENERATED=""
    if [[ -z "$WEB_PW" ]]; then
        WEB_PW="$(sudo -u "$SERVICE_USER" "$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(12))')"
        GENERATED=1
    fi
    if printf '%s' "$WEB_PW" | sudo -u "$SERVICE_USER" "$VENV/bin/python" "$INSTALL_DIR/scripts/set_web_password.py"; then
        if [[ -n "$GENERATED" ]]; then
            echo ""
            echo -e "         ${YELLOW}Generiertes Web-Admin-Passwort:${NC} ${WEB_PW}"
            echo "         >>> JETZT NOTIEREN – wird nicht erneut angezeigt! <<<"
            echo ""
        fi
        ok "Web-Admin-Passwort gesetzt (scrypt-Hash in webconfig.yaml)."
    else
        err "Web-Admin-Passwort konnte nicht gesetzt werden."
    fi
    unset WEB_PW
else
    ok "Web-Admin-Passwort bereits konfiguriert."
fi

# ── udev-Regel für /dev/meshcore ─────────────────────────────────────────────
UDEV_RULE="/etc/udev/rules.d/99-meshcore.rules"
if [[ ! -f "$UDEV_RULE" ]]; then
    info "udev-Regel für /dev/meshcore anlegen..."
    sudo tee "$UDEV_RULE" > /dev/null <<EOF
SUBSYSTEM=="tty", ATTRS{idVendor}=="${UDEV_VENDOR}", ATTRS{idProduct}=="${UDEV_PRODUCT}", ATTRS{serial}=="${UDEV_SERIAL}", SYMLINK+="meshcore"
EOF
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    ok "udev-Regel aktiv → /dev/meshcore (MeshCore-Node muss eingesteckt sein)."
else
    ok "udev-Regel bereits vorhanden ($UDEV_RULE)."
fi

# ── journald: Kernel-Flut (dwc_otg USB-Warnungen) abfedern ───────────────────
# Verhindert, dass dwc_otg-Kernelwarnungen das Journal fluten und die BBS-Logs
# verdraengen (journalctl -u nnp-bbs zeigte sonst "No entries" trotz laufendem Dienst).
JOURNALD_DROPIN="/etc/systemd/journald.conf.d/nnp-bbs.conf"
if [[ ! -f "$JOURNALD_DROPIN" ]]; then
    info "journald-Drop-in fuer Logstabilitaet anlegen..."
    sudo mkdir -p /etc/systemd/journald.conf.d
    sudo tee "$JOURNALD_DROPIN" > /dev/null <<EOF
[Journal]
# Hoher Burst, damit BBS-Logs auch bei Kernel-Fluten nicht verworfen werden
RateLimitIntervalSec=30s
RateLimitBurst=10000
# Journal gross genug gegen schnelle Rotation, aber begrenzt
SystemMaxUse=200M
EOF
    sudo systemctl restart systemd-journald
    ok "journald-Drop-in aktiv ($JOURNALD_DROPIN)."
else
    ok "journald-Drop-in bereits vorhanden ($JOURNALD_DROPIN)."
fi

# ── systemd-Service ───────────────────────────────────────────────────────────
info "systemd-Service einrichten..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=Meshcore BBSng MeshCore/Telnet BBS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
${LOAD_CRED_LINE}
ExecStart=${VENV}/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
ok "systemd-Service aktiviert (Autostart beim Boot)."

# ── sudo NOPASSWD für Service-Steuerung und Deployment ───────────────────────
SUDOERS_FILE="/etc/sudoers.d/nnp-bbs"
if [[ ! -f "$SUDOERS_FILE" ]]; then
    info "sudo NOPASSWD konfigurieren..."
    # coreadmin darf den Service steuern
    # Der aktuelle Login-User darf als coreadmin git pull ausführen + Service neu starten
    CALLER="$(whoami)"
    sudo tee "$SUDOERS_FILE" > /dev/null <<EOF
# Meshcore BBSng Service-Steuerung
${SERVICE_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart ${SERVICE_NAME}, /usr/bin/systemctl start ${SERVICE_NAME}, /usr/bin/systemctl stop ${SERVICE_NAME}, /usr/bin/systemctl status ${SERVICE_NAME}

# Deployment vom Login-User aus
${CALLER} ALL=(${SERVICE_USER}) NOPASSWD: /usr/bin/git -C ${INSTALL_DIR} pull
${CALLER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart ${SERVICE_NAME}, /usr/bin/systemctl start ${SERVICE_NAME}, /usr/bin/systemctl stop ${SERVICE_NAME}, /usr/bin/systemctl status ${SERVICE_NAME}
EOF
    sudo chmod 440 "$SUDOERS_FILE"
    ok "sudo NOPASSWD konfiguriert (${SERVICE_USER} + $(whoami))."
else
    ok "sudoers-Regel bereits vorhanden."
fi

# ── Abschluss ─────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════"
echo "  Setup abgeschlossen!"
echo "══════════════════════════════════════════════"
echo ""
echo "Nächste Schritte:"
echo "  1. HA-Token eintragen:  sudo -u ${SERVICE_USER} nano ${SECRETS}"
echo "  2. MeshCore-Node per USB anschließen"
echo "  3. BBS starten:         sudo systemctl start ${SERVICE_NAME}"
echo ""
echo "Logs verfolgen:       journalctl -fu ${SERVICE_NAME}"
echo ""
echo "Update deployen (als $(whoami)):"
echo "  sudo -u ${SERVICE_USER} git -C ${INSTALL_DIR} pull && sudo systemctl restart ${SERVICE_NAME}"
echo ""
