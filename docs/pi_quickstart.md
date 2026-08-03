# Quickstart Raspberry Pi — `sc-vehicule-1`

Toutes les commandes ci-dessous sont a lancer dans le terminal du Pi avec
l'utilisateur `jeandard`.

Executer les commandes une par une et attendre le retour du prompt avant de
passer a la suivante. Apres `sudo reboot`, la connexion SSH se ferme : attendre
le redemarrage du Pi, puis se reconnecter avant de continuer.

## 1. Verifier le Pi

```bash
whoami
hostname
```

Resultat attendu : `jeandard` puis `sc-vehicule-1`.

Option — corriger le hostname :

```bash
sudo hostnamectl set-hostname sc-vehicule-1
echo 'preserve_hostname: true' | sudo tee /etc/cloud/cloud.cfg.d/99-preserve-hostname.cfg
sudo reboot
```

## 2. Installer les paquets

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y git python3 python3-venv python3-pip nginx avahi-daemon curl i2c-tools
sudo systemctl enable --now avahi-daemon
sudo usermod -aG dialout,i2c,video jeandard
sudo raspi-config nonint do_i2c 0
```

Redemarrer seulement quand toutes les commandes precedentes sont terminees :

```bash
sudo reboot
```

Option — camera USB :

```bash
sudo apt install -y fswebcam v4l-utils
```

## 3. Cloner le repo en HTTPS

```bash
git config --global credential.helper store
cd /home/jeandard
git clone https://github.com/regenbox-team/Cycle-Analyst-App.git Cycle-Analyst-App
cd /home/jeandard/Cycle-Analyst-App
```

Quand GitHub le demande :

- `Username` : le compte GitHub autorise ;
- `Password` : un Personal Access Token avec `Contents: Read-only`.

Option — cloner avec une cle SSH deja configuree :

```bash
git clone github:regenbox-team/Cycle-Analyst-App.git Cycle-Analyst-App
```

## 4. Installer l'application

```bash
cd /home/jeandard/Cycle-Analyst-App
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -c "import flask; print('Flask OK:', flask.__version__)"
mkdir -p var var/session_metrics var/live_photo var/pending_photos
mkdir -p /home/jeandard/Documents
cp -n cycle-analyst.env.example cycle-analyst.env
nano cycle-analyst.env
```

Dans `nano` : enregistrer avec `Ctrl+O`, valider avec `Entree`, puis quitter
avec `Ctrl+X`.

Verifier les valeurs principales :

```bash
grep -nE '^(MONITOR_DEVICE_ID|APP_PMTILES_PATH|MONITOR_URL)=' cycle-analyst.env
```

Pour ce Pi, `MONITOR_DEVICE_ID=` peut rester vide : le hostname
`sc-vehicule-1` sera utilise automatiquement.

## 5. Installer et demarrer les services

```bash
cd /home/jeandard/Cycle-Analyst-App
python3 scripts/setup_pi_services.py
python3 scripts/setup_pi_services.py --apply
```

Verifier les services et nginx :

```bash
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
systemctl status cycle-recorder.service cycle-photo.service cycle-analyst.service --no-pager
systemctl status nginx --no-pager
curl http://127.0.0.1:5050/metrics
curl http://127.0.0.1/
curl http://sc-vehicule-1.local
```

Interface :

```text
http://sc-vehicule-1.local
```

## Options de test

Cycle Analyst et GPS :

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
timeout 5 cat /dev/ttyACM0
```

Capteur solaire INA228 :

```bash
i2cdetect -y 1
cd /home/jeandard/Cycle-Analyst-App
source .venv/bin/activate
python scripts/ina228_debug.py --addr 0 --interval 1 --once
```

Camera :

```bash
lsusb
v4l2-ctl --list-devices
fswebcam -d /dev/video0 -q -S 10 --palette YUYV -r 640x480 --jpeg 70 --no-banner /tmp/cycle-test.jpg
ls -lh /tmp/cycle-test.jpg
```

Logs des services :

```bash
journalctl -u cycle-recorder.service -f
journalctl -u cycle-photo.service -f
journalctl -u cycle-analyst.service -f
```

Test manuel de l'application :

```bash
cd /home/jeandard/Cycle-Analyst-App
source .venv/bin/activate
set -a
. ./cycle-analyst.env
set +a
python cycle_server.py
```

Arreter le test manuel avec `Ctrl+C`.

## Mise a jour

```bash
cd /home/jeandard/Cycle-Analyst-App
git pull --ff-only
source .venv/bin/activate
python -m pip install -r requirements.txt
python -c "import flask; print('Flask OK:', flask.__version__)"
python3 scripts/setup_pi_services.py --apply
sudo systemctl restart cycle-recorder.service cycle-photo.service cycle-analyst.service
sudo systemctl status cycle-recorder.service cycle-photo.service cycle-analyst.service --no-pager
```
