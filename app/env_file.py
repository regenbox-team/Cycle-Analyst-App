from __future__ import annotations

import os
import re
import shlex
import socket
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = ROOT / "cycle-analyst.env"
ENV_LINE_RE = re.compile(r"^\s*(#\s*)?([A-Z0-9_]+)\s*=\s*(.*)$")


@dataclass(frozen=True)
class EnvSetting:
    key: str
    label: str
    default: str
    description: str
    group: str
    placeholder: str = ""
    input_type: str = "text"
    enabled_by_default: bool = True
    secret: bool = False
    detail: str = ""
    choices: tuple[tuple[str, str], ...] = ()


BOOLEAN_CHOICES = (("0", "Disabled"), ("1", "Enabled"))
GPS_PORT_CHOICES = (
    ("/dev/ttyACM0", "/dev/ttyACM0 - common USB GPS"),
    ("/dev/ttyACM1", "/dev/ttyACM1"),
    ("/dev/ttyUSB0", "/dev/ttyUSB0"),
    ("/dev/ttyUSB1", "/dev/ttyUSB1"),
)
GPS_BAUDRATE_CHOICES = (
    ("4800", "4800"),
    ("9600", "9600 - common NMEA"),
    ("38400", "38400"),
    ("115200", "115200"),
)
SOLAR_SENSOR_CHOICES = (("ina228", "INA228"),)
I2C_BUS_CHOICES = (("1", "1 - Raspberry Pi default"), ("0", "0"))
I2C_ADDR_CHOICES = (
    ("0x45", "0x45 - current default"),
    ("0x44", "0x44"),
    ("0x41", "0x41"),
    ("0", "0 - auto probe"),
)
SOLAR_INVERT_CHOICES = (("true", "Invert current sign"), ("false", "Keep current sign"))
MONITOR_CHUNK_CHOICES = (("250", "250"), ("500", "500"), ("1000", "1000"), ("2000", "2000"))
MONITOR_CHUNK_BYTES_CHOICES = (
    ("131072", "128 KB"),
    ("262144", "256 KB"),
    ("524288", "512 KB"),
    ("1048576", "1 MB"),
)

CAMERA_CHOICES = {
    "program": (("fswebcam", "fswebcam - USB webcam"), ("libcamera-still", "libcamera-still - Pi camera")),
    "device": (("/dev/video0", "/dev/video0"), ("/dev/video1", "/dev/video1"), ("/dev/video2", "/dev/video2")),
    "skip_frames": (("0", "0"), ("10", "10"), ("30", "30"), ("60", "60 - stable exposure"), ("90", "90")),
    "palette": (("YUYV", "YUYV - avoids grey frames"), ("MJPEG", "MJPEG"), ("RGB3", "RGB3")),
    "resolution": (("640x480", "640x480"), ("800x600", "800x600"), ("1280x720", "1280x720"), ("1920x1080", "1920x1080")),
    "jpeg_quality": (("70", "70"), ("85", "85 - default"), ("90", "90"), ("95", "95")),
}


SETTINGS: tuple[EnvSetting, ...] = (
    EnvSetting(
        "APP_VAR_DIR",
        "Runtime directory",
        "var",
        "Dossier local ou l'app ecrit les donnees runtime.",
        "Runtime",
        detail="Contient les bases SQLite, les sessions en cours, les profils utilisateurs, les photos locales, le GPX et les snapshots de metriques. Sur le Pi installe, garde generalement var pour rester dans le repo.",
    ),
    EnvSetting(
        "APP_START_READER",
        "Start reader on import",
        "0",
        "Demarre le reader Cycle Analyst au moment de l'import Python.",
        "Runtime",
        enabled_by_default=False,
        detail="Laisse desactive si l'app tourne avec python cycle_server.py. Active seulement pour un lancement WSGI/gunicorn ou un contexte ou le reader ne demarre pas autrement.",
        choices=BOOLEAN_CHOICES,
    ),
    EnvSetting(
        "APP_GPS_PORT",
        "GPS port",
        "/dev/ttyACM0",
        "Port serie du GPS NMEA USB.",
        "GPS and map",
        detail="Les dongles VK-162 apparaissent souvent en /dev/ttyACM0. Si /gps_status reste vide, teste les autres ports visibles avec ls -l /dev/ttyUSB* /dev/ttyACM*.",
        choices=GPS_PORT_CHOICES,
    ),
    EnvSetting(
        "APP_GPS_BAUDRATE",
        "GPS baudrate",
        "9600",
        "Vitesse serie du GPS.",
        "GPS and map",
        detail="9600 est la valeur la plus courante pour un flux NMEA standard. Change seulement si le module GPS documente une autre vitesse.",
        choices=GPS_BAUDRATE_CHOICES,
    ),
    EnvSetting(
        "APP_PMTILES_PATH",
        "PMTiles path",
        str(Path.home() / "Documents" / "tiles.pmtiles"),
        "Fichier .pmtiles offline servi par /tiles/basemap.pmtiles.",
        "GPS and map",
        detail="Chemin absolu recommande sur le Pi, par exemple /home/danieldilg/Documents/tiles.pmtiles. Ce champ reste libre parce que le nom et la zone de carte changent selon le vehicule.",
    ),
    EnvSetting(
        "APP_SOLAR_PANEL_MAX_W",
        "Solar panel max W",
        "590",
        "Puissance max panneaux utilisee pour l'estimation solaire.",
        "Solar and battery",
        input_type="number",
        detail="Valeur en watts crete. Elle influence uniquement l'estimation d'autonomie solaire, pas la mesure live du capteur.",
    ),
    EnvSetting(
        "APP_SOLAR_LAT",
        "Solar latitude",
        "48.8566",
        "Latitude par defaut pour l'estimation solaire.",
        "Solar and battery",
        input_type="number",
        detail="Utilisee quand la position GPS exacte n'est pas encore disponible. Mets la latitude du depart ou une valeur moyenne de la zone de course.",
    ),
    EnvSetting(
        "APP_SOLAR_LON",
        "Solar longitude",
        "2.3522",
        "Longitude par defaut pour l'estimation solaire.",
        "Solar and battery",
        input_type="number",
        detail="Utilisee avec la latitude pour estimer l'angle solaire. Le GPS live prendra le relais quand il fournit une position.",
    ),
    EnvSetting(
        "APP_SOLAR_PROFILE_FILE",
        "Imported solar profile",
        "var/solar_profile.json",
        "Fichier JSON optionnel de profil solaire importe.",
        "Solar and battery",
        detail="Quand ce fichier existe, son profil de production prend le pas sur l'estimation solaire theorique. Supprime le profil depuis Settings pour revenir au calcul automatique.",
    ),
    EnvSetting(
        "APP_BATTERY_NOMINAL_VOLTAGE",
        "Battery nominal voltage",
        "48.1",
        "Tension nominale batterie pour les calculs Wh.",
        "Solar and battery",
        input_type="number",
        detail="Tension de reference de la batterie, par exemple 48.1 V. Sert aux calculs de capacite energetique quand la courbe batterie ne suffit pas.",
    ),
    EnvSetting(
        "APP_BATTERY_DISCHARGE_CURVE_FILE",
        "Battery curve file",
        "var/battery_curve_lg_mh1_from_trip.json",
        "Fichier JSON optionnel de courbe voltage/SOC.",
        "Solar and battery",
        detail="Chemin vers une courbe de decharge reconstruite. Laisse vide si tu veux rester sur le calcul standard par Ah et tension.",
    ),
    EnvSetting(
        "APP_SOLAR_SENSOR",
        "Solar sensor",
        "ina228",
        "Type de capteur solaire I2C.",
        "INA228 solar sensor",
        enabled_by_default=False,
        detail="Active cette variable seulement si le Pi a un INA228 branche. Sinon, l'app ignore la mesure solaire live et garde seulement le mode solaire manuel.",
        choices=SOLAR_SENSOR_CHOICES,
    ),
    EnvSetting(
        "APP_SOLAR_I2C_BUS",
        "I2C bus",
        "1",
        "Bus I2C du capteur INA228.",
        "INA228 solar sensor",
        enabled_by_default=False,
        detail="Sur Raspberry Pi, le bus I2C principal est generalement 1. Verifie avec i2cdetect -y 1.",
        choices=I2C_BUS_CHOICES,
    ),
    EnvSetting(
        "APP_SOLAR_I2C_ADDR",
        "I2C address",
        "0x45",
        "Adresse I2C du capteur INA228.",
        "INA228 solar sensor",
        enabled_by_default=False,
        detail="0x45 est la valeur utilisee par defaut. La valeur 0 lance une detection parmi les adresses connues 0x41, 0x44 et 0x45.",
        choices=I2C_ADDR_CHOICES,
    ),
    EnvSetting(
        "APP_SOLAR_SHUNT_OHMS",
        "Shunt ohms",
        "0.0002",
        "Valeur du shunt INA228.",
        "INA228 solar sensor",
        input_type="number",
        enabled_by_default=False,
        detail="Exprimee en ohms. Elle doit correspondre au shunt physique, sinon le courant mesure sera proportionnellement faux.",
    ),
    EnvSetting(
        "APP_SOLAR_MAX_AMPS",
        "Max amps",
        "204.8",
        "Courant pleine echelle pour CURRENT_LSB.",
        "INA228 solar sensor",
        input_type="number",
        enabled_by_default=False,
        detail="Valeur en amperes utilisee pour calibrer la resolution du capteur. 204.8 A convient au setup actuel.",
    ),
    EnvSetting(
        "APP_SOLAR_CURRENT_GAIN",
        "Current gain",
        "1.0",
        "Correction multiplicative du courant solaire.",
        "INA228 solar sensor",
        input_type="number",
        enabled_by_default=False,
        detail="Facteur d'ajustement applique apres mesure. Garde 1.0 sauf si une comparaison avec un amperemetre montre un ecart constant.",
    ),
    EnvSetting(
        "APP_SOLAR_CURRENT_OFFSET",
        "Current offset A",
        "0.0",
        "Offset courant solaire en amperes.",
        "INA228 solar sensor",
        input_type="number",
        enabled_by_default=False,
        detail="Ajoute ou retire un courant fixe. Utile pour corriger un decalage residuel autour de zero.",
    ),
    EnvSetting(
        "APP_SOLAR_CURRENT_DEADBAND_A",
        "Current deadband A",
        "0.15",
        "Zone morte des petits courants.",
        "INA228 solar sensor",
        input_type="number",
        enabled_by_default=False,
        detail="Courants plus petits que cette valeur sont arrondis a zero pour eviter le bruit de mesure.",
    ),
    EnvSetting(
        "APP_SOLAR_INVERT_SIGN",
        "Invert solar sign",
        "true",
        "Sens du courant solaire.",
        "INA228 solar sensor",
        enabled_by_default=False,
        detail="Active l'inversion si la puissance solaire apparait negative quand les panneaux produisent.",
        choices=SOLAR_INVERT_CHOICES,
    ),
    EnvSetting(
        "APP_CAMERA_COMMAND",
        "Camera command",
        "fswebcam -d /dev/video0 -q -S 10 --palette YUYV -r 640x480 --jpeg 70 --no-banner {output}",
        "Commande de capture photo.",
        "Camera",
        detail="La page decompose cette commande en champs simples puis reconstruit APP_CAMERA_COMMAND. Le marqueur {output} est ajoute automatiquement a la fin.",
    ),
    EnvSetting(
        "MONITOR_DEVICE_ID",
        "Vehicle name",
        socket.gethostname(),
        "Nom libre et stable du vehicule envoye au monitor_server.",
        "Monitor sync",
        detail="Champ libre: mets le nom exact que tu veux voir dans la colonne Device du monitor. Si la variable reste vide, l'app utilise automatiquement le hostname du Pi.",
    ),
    EnvSetting(
        "MONITOR_URL",
        "Monitor URL",
        "http://91.134.243.157:8080",
        "URL du monitor_server.",
        "Monitor sync",
        placeholder="http://91.134.243.157:8080",
        detail="URL du monitor_server de production. Laisse vide seulement si tu veux desactiver la synchronisation sur un Pi donne.",
    ),
    EnvSetting(
        "MONITOR_USER",
        "Monitor user",
        "",
        "Login Basic Auth du monitor_server.",
        "Monitor sync",
        detail="Doit correspondre a MONITOR_USER cote monitor_server. Laisse vide si le monitor n'impose pas d'identifiants.",
    ),
    EnvSetting(
        "MONITOR_PASS",
        "Monitor password",
        "",
        "Mot de passe Basic Auth du monitor_server.",
        "Monitor sync",
        secret=True,
        detail="Stocke dans cycle-analyst.env. Si tu mets un vrai mot de passe, protege le fichier sur le Pi.",
    ),
    EnvSetting(
        "MONITOR_UPLOAD_CHUNK_SIZE",
        "Upload chunk size",
        "1000",
        "Nombre de lignes envoyees par paquet au monitor_server.",
        "Monitor sync",
        detail="1000 est un bon compromis. Diminue si le reseau est lent ou instable; augmente si les uploads sont trop nombreux.",
        choices=MONITOR_CHUNK_CHOICES,
    ),
    EnvSetting(
        "MONITOR_UPLOAD_CHUNK_MAX_BYTES",
        "Upload chunk max bytes",
        "262144",
        "Taille JSON non compressee maximale d'un paquet de session.",
        "Monitor sync",
        detail="Le Pi decoupe aussi par taille reelle, pas seulement par nombre de lignes. 256 KB evite les timeouts sur les reseaux lents.",
        choices=MONITOR_CHUNK_BYTES_CHOICES,
    ),
    EnvSetting(
        "MONITOR_UPLOAD_GZIP",
        "Compress session uploads",
        "1",
        "Compresse les uploads de sessions avant envoi au monitor_server.",
        "Monitor sync",
        detail="Active par defaut. Reduit fortement le temps d'envoi des sessions car le JSON des logs se compresse bien.",
        choices=(("1", "On"), ("0", "Off")),
    ),
    EnvSetting(
        "MONITOR_RESPONSE_GZIP",
        "Compress monitor responses",
        "1",
        "Compresse les pages et reponses JSON du monitor_server.",
        "Monitor sync",
        detail="Utile sur le VPS: la page monitor est plus petite a envoyer et resiste mieux aux connexions lentes.",
        choices=(("1", "On"), ("0", "Off")),
    ),
    EnvSetting(
        "MONITOR_AUTO_UPLOAD_SESSIONS",
        "Auto-upload sessions",
        "0",
        "Upload automatique des sessions terminees.",
        "Monitor sync",
        detail="Desactive par defaut: les heartbeats, utilisateurs et photos restent synchronises, mais les sessions doivent etre uploadees manuellement depuis la liste des sessions.",
        choices=(("0", "Off"), ("1", "On")),
    ),
)


def env_file_path() -> Path:
    return Path(os.getenv("APP_ENV_FILE") or DEFAULT_ENV_PATH)


def _format_value(value: str) -> str:
    value = value.replace("\r", "").replace("\n", "")
    if not value:
        return ""
    if value[0].isspace() or value[-1].isspace():
        return f'"{value}"'
    return value


def _camera_token_after(tokens: list[str], flag: str) -> str:
    try:
        index = tokens.index(flag)
    except ValueError:
        return ""
    if index + 1 >= len(tokens):
        return ""
    return tokens[index + 1]


def parse_camera_command(command: str) -> dict[str, str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = []
    if not tokens:
        tokens = shlex.split(next(s.default for s in SETTINGS if s.key == "APP_CAMERA_COMMAND"))

    known_value_flags = {"-d", "-S", "--palette", "-r", "--jpeg", "-o", "--width", "--height", "--quality"}
    known_standalone_flags = {"-q", "--no-banner", "-n"}
    output_tokens = {"{output}"}
    extra: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in known_value_flags:
            index += 2
            continue
        if token in known_standalone_flags or token in output_tokens:
            index += 1
            continue
        extra.append(token)
        index += 1

    return {
        "program": tokens[0],
        "device": _camera_token_after(tokens, "-d"),
        "skip_frames": _camera_token_after(tokens, "-S"),
        "palette": _camera_token_after(tokens, "--palette"),
        "resolution": _camera_token_after(tokens, "-r"),
        "jpeg_quality": _camera_token_after(tokens, "--jpeg"),
        "quiet": "1" if "-q" in tokens else "",
        "no_banner": "1" if "--no-banner" in tokens else "",
        "extra_args": " ".join(shlex.quote(token) for token in extra),
    }


def compose_camera_command(form) -> str:
    program = str(form.get("APP_CAMERA_COMMAND__program", "")).strip() or "fswebcam"
    device = str(form.get("APP_CAMERA_COMMAND__device", "")).strip()
    skip_frames = str(form.get("APP_CAMERA_COMMAND__skip_frames", "")).strip()
    palette = str(form.get("APP_CAMERA_COMMAND__palette", "")).strip()
    resolution = str(form.get("APP_CAMERA_COMMAND__resolution", "")).strip()
    jpeg_quality = str(form.get("APP_CAMERA_COMMAND__jpeg_quality", "")).strip()
    extra_args = str(form.get("APP_CAMERA_COMMAND__extra_args", "")).strip()
    tokens = [program]

    if device:
        tokens.extend(["-d", device])
    if form.get("APP_CAMERA_COMMAND__quiet") == "1":
        tokens.append("-q")
    if skip_frames:
        tokens.extend(["-S", skip_frames])
    if palette:
        tokens.extend(["--palette", palette])
    if resolution:
        tokens.extend(["-r", resolution])
    if jpeg_quality:
        tokens.extend(["--jpeg", jpeg_quality])
    if form.get("APP_CAMERA_COMMAND__no_banner") == "1":
        tokens.append("--no-banner")
    if extra_args:
        try:
            tokens.extend(shlex.split(extra_args))
        except ValueError:
            tokens.extend(extra_args.split())
    tokens.append("{output}")
    return " ".join(shlex.quote(token) if " " in token else token for token in tokens)


def _choices_with_current(
    choices: tuple[tuple[str, str], ...], current: str
) -> list[dict[str, str]]:
    if not choices:
        return []
    options = [{"value": value, "label": label} for value, label in choices]
    if current and current not in {option["value"] for option in options}:
        options.append({"value": current, "label": f"{current} - current custom value"})
    return options


def _strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            if index == 0 or value[index - 1].isspace():
                return value[:index].rstrip()
    return value.strip()


def _unquote(value: str) -> str:
    value = _strip_inline_comment(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def default_env_text() -> str:
    lines = [
        "# Cycle Analyst App - editable app configuration",
        "# This file is loaded automatically by cycle_server.py and by systemd.",
        "# Lines starting with '# KEY=' are available in the UI but disabled.",
        "",
    ]
    group = ""
    for setting in SETTINGS:
        if setting.group != group:
            group = setting.group
            lines.extend(["", f"# {group}"])
        lines.append(f"# {setting.description}")
        prefix = "" if setting.enabled_by_default else "# "
        lines.append(f"{prefix}{setting.key}={_format_value(setting.default)}")
    lines.append("")
    return "\n".join(lines)


def ensure_env_file(path: Path | None = None) -> Path:
    target = path or env_file_path()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(default_env_text(), encoding="utf-8")
    return target


def parse_env_file(path: Path | None = None) -> dict[str, dict[str, str | bool]]:
    target = ensure_env_file(path)
    parsed: dict[str, dict[str, str | bool]] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        match = ENV_LINE_RE.match(line)
        if not match:
            continue
        disabled, key, value = match.groups()
        parsed[key] = {
            "value": _unquote(value),
            "enabled": disabled is None,
        }
    return parsed


def load_env_file(path: Path | None = None) -> None:
    for key, data in parse_env_file(path).items():
        if data["enabled"]:
            os.environ.setdefault(key, str(data["value"]))


def grouped_settings() -> list[dict[str, object]]:
    parsed = parse_env_file()
    groups: list[dict[str, object]] = []
    by_group: dict[str, list[dict[str, object]]] = {}
    for setting in SETTINGS:
        current = parsed.get(setting.key, {})
        value = str(current.get("value", setting.default))
        enabled = bool(current.get("enabled", setting.enabled_by_default))
        item = {
            "key": setting.key,
            "label": setting.label,
            "description": setting.description,
            "value": value,
            "enabled": enabled,
            "input_type": "password" if setting.secret else setting.input_type,
            "placeholder": setting.placeholder or setting.default,
            "process_value": os.getenv(setting.key, ""),
            "detail": setting.detail,
            "choices": _choices_with_current(setting.choices, value),
        }
        if setting.key == "APP_CAMERA_COMMAND":
            camera = parse_camera_command(value)
            item["camera"] = camera
            item["camera_choices"] = {
                field: _choices_with_current(choices, camera.get(field, ""))
                for field, choices in CAMERA_CHOICES.items()
            }
        by_group.setdefault(setting.group, []).append(item)

    for group in dict.fromkeys(setting.group for setting in SETTINGS):
        groups.append({"name": group, "settings": by_group.get(group, [])})
    return groups


def save_settings(form) -> None:
    lines = [
        "# Cycle Analyst App - editable app configuration",
        "# Updated from the Cycle Analyst settings page.",
        "# Restart cycle-analyst.service for startup-only variables to take effect.",
        "",
    ]
    group = ""
    for setting in SETTINGS:
        if setting.group != group:
            group = setting.group
            lines.extend(["", f"# {group}"])
        if setting.key == "APP_CAMERA_COMMAND":
            value = compose_camera_command(form)
        else:
            value = str(form.get(setting.key, "")).strip()
        enabled = form.get(f"enabled_{setting.key}") == "1"
        lines.append(f"# {setting.description}")
        prefix = "" if enabled else "# "
        lines.append(f"{prefix}{setting.key}={_format_value(value)}")
        if enabled:
            os.environ[setting.key] = value
        elif setting.key in os.environ:
            os.environ.pop(setting.key, None)
    lines.append("")
    target = ensure_env_file()
    target.write_text("\n".join(lines), encoding="utf-8")


def current_device_hint() -> str:
    return os.getenv("MONITOR_DEVICE_ID") or socket.gethostname()
