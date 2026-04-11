import os
import time
import requests
import logging
from datetime import datetime

# ============================================================
# CONFIGURACIÓN
# ============================================================
API_KEY = os.environ.get("API_FOOTBALL_KEY", "414095f59915ba82a818e44aa3b01fed")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8653122860:AAEd6mUAEp2oKKYla8lTFOadTDDiVtPm5Uo")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "364177709")

# Diferencia mínima de posiciones para considerar "underdog"
MIN_POSITION_DIFF = 7

# Minuto a partir del cual se activa la alerta de minuto 75+
LATE_GAME_MINUTE = 75

# Intervalo de consulta en segundos (cada 60 segundos)
POLL_INTERVAL = 60

# Ligas top para alerta de 4 tarjetas amarillas
TOP_LEAGUES = {
    "LaLiga": 140,
    "Premier League": 39,
    "Serie A": 135,
    "Ligue 1": 61,
    "Bundesliga": 78,
}

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ============================================================
# ESTADO (para no repetir alertas)
# ============================================================
alerted = {
    "goals": set(),        # fixture_id + score
    "red_underdog": set(), # fixture_id + team_id + player_id
    "double_red": set(),   # fixture_id + team_id
    "combo": set(),        # fixture_id
    "late_game": set(),    # fixture_id
    "yellow4": set(),      # player_id + season
}

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            log.error(f"Telegram error: {r.text}")
    except Exception as e:
        log.error(f"Telegram exception: {e}")

# ============================================================
# API-FOOTBALL
# ============================================================
HEADERS = {
    "x-apisports-key": API_KEY
}
BASE_URL = "https://v3.football.api-sports.io"

def api_get(endpoint: str, params: dict = {}):
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=15)
        if r.status_code == 200:
            return r.json().get("response", [])
        else:
            log.error(f"API error {r.status_code}: {r.text}")
            return []
    except Exception as e:
        log.error(f"API exception: {e}")
        return []

# ============================================================
# OBTENER PARTIDOS LIVE
# ============================================================
def get_live_fixtures():
    return api_get("fixtures", {"live": "all"})

# ============================================================
# OBTENER CLASIFICACIÓN DE UNA LIGA
# ============================================================
standings_cache = {}

def get_standings(league_id: int, season: int):
    key = (league_id, season)
    if key in standings_cache:
        return standings_cache[key]
    data = api_get("standings", {"league": league_id, "season": season})
    if data:
        try:
            table = data[0]["league"]["standings"][0]
            result = {team["team"]["id"]: team["rank"] for team in table}
            standings_cache[key] = result
            return result
        except Exception as e:
            log.error(f"Error parsing standings: {e}")
    return {}

# ============================================================
# OBTENER EVENTOS DE UN PARTIDO
# ============================================================
def get_fixture_events(fixture_id: int):
    return api_get("fixtures/events", {"fixture": fixture_id})

# ============================================================
# PROCESAR ALERTAS DE UN PARTIDO
# ============================================================
def process_fixture(fixture: dict):
    try:
        f = fixture["fixture"]
        fixture_id = f["id"]
        status = f["status"]["short"]
        minute = f["status"]["elapsed"] or 0

        # Solo partidos en juego
        if status not in ["1H", "HT", "2H", "ET", "P"]:
            return

        league = fixture["league"]
        league_id = league["id"]
        league_name = league["name"]
        season = league["season"]

        home = fixture["teams"]["home"]
        away = fixture["teams"]["away"]
        score_home = fixture["goals"]["home"] or 0
        score_away = fixture["goals"]["away"] or 0

        # Obtener clasificaciones
        standings = get_standings(league_id, season)
        pos_home = standings.get(home["id"])
        pos_away = standings.get(away["id"])

        # Obtener eventos del partido
        events = get_fixture_events(fixture_id)

        # Contar tarjetas rojas por equipo
        reds = {home["id"]: [], away["id"]: []}
        for ev in events:
            if ev.get("type") == "Card" and ev.get("detail") in ["Red Card", "Second Yellow"]:
                team_id = ev["team"]["id"]
                player = ev.get("player", {}).get("name", "Desconocido")
                ev_minute = ev.get("time", {}).get("elapsed", 0)
                if team_id in reds:
                    reds[team_id].append({"player": player, "minute": ev_minute})

        # --------------------------------------------------------
        # ALERTA 1: GOL DEL UNDERDOG
        # --------------------------------------------------------
        if pos_home and pos_away:
            diff = abs(pos_home - pos_away)
            if diff >= MIN_POSITION_DIFF:
                # Determinar underdog y favorito
                if pos_home > pos_away:
                    underdog, favorite = home, away
                    underdog_score, fav_score = score_home, score_away
                    underdog_pos, fav_pos = pos_home, pos_away
                else:
                    underdog, favorite = away, home
                    underdog_score, fav_score = score_away, score_home
                    underdog_pos, fav_pos = pos_away, pos_home

                # Underdog va ganando
                if underdog_score > fav_score:
                    goal_key = f"{fixture_id}_{score_home}_{score_away}"
                    if goal_key not in alerted["goals"]:
                        alerted["goals"].add(goal_key)
                        msg = (
                            f"⚽ <b>ALERTA TIPSTER — GOL UNDERDOG</b>\n"
                            f"🏆 {league_name}\n"
                            f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                            f"⬆️ {favorite['name']} es {diff} posiciones superior (#{fav_pos} vs #{underdog_pos})\n"
                            f"⏱️ Min {minute}"
                        )
                        send_telegram(msg)
                        log.info(f"ALERTA GOL: {msg}")

                # --------------------------------------------------------
                # ALERTA 2: TARJETA ROJA AL UNDERDOG
                # --------------------------------------------------------
                for red in reds[underdog["id"]]:
                    red_key = f"{fixture_id}_{underdog['id']}_{red['player']}"
                    if red_key not in alerted["red_underdog"]:
                        alerted["red_underdog"].add(red_key)
                        msg = (
                            f"🟥 <b>ALERTA TIPSTER — ROJA AL UNDERDOG</b>\n"
                            f"🏆 {league_name}\n"
                            f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                            f"⬆️ {favorite['name']} es {diff} posiciones superior (#{fav_pos} vs #{underdog_pos})\n"
                            f"👤 Expulsado: {red['player']} ({underdog['name']})\n"
                            f"⏱️ Min {red['minute']}"
                        )
                        send_telegram(msg)
                        log.info(f"ALERTA ROJA UNDERDOG: {msg}")

                # --------------------------------------------------------
                # ALERTA 5: COMBO GOL + ROJA AL UNDERDOG
                # --------------------------------------------------------
                has_goal = underdog_score > fav_score
                has_red = len(reds[underdog["id"]]) > 0
                combo_key = f"{fixture_id}_combo"
                if has_goal and has_red and combo_key not in alerted["combo"]:
                    alerted["combo"].add(combo_key)
                    msg = (
                        f"🔄 <b>ALERTA TIPSTER — COMBO GOL + ROJA</b>\n"
                        f"🏆 {league_name}\n"
                        f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                        f"⬆️ {favorite['name']} es {diff} posiciones superior\n"
                        f"⚠️ {underdog['name']} va ganando Y tiene expulsado\n"
                        f"⏱️ Min {minute}"
                    )
                    send_telegram(msg)
                    log.info(f"ALERTA COMBO: {msg}")

                # --------------------------------------------------------
                # ALERTA 4: MINUTO 75+ RESULTADO AJUSTADO
                # --------------------------------------------------------
                if minute >= LATE_GAME_MINUTE:
                    late_key = f"{fixture_id}_late"
                    score_diff = abs(score_home - score_away)
                    if score_diff <= 1 and late_key not in alerted["late_game"]:
                        alerted["late_game"].add(late_key)
                        winning = home["name"] if score_home > score_away else (away["name"] if score_away > score_home else "Empate")
                        msg = (
                            f"⏱️ <b>ALERTA TIPSTER — MINUTO 75+</b>\n"
                            f"🏆 {league_name}\n"
                            f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                            f"⬆️ {favorite['name']} es {diff} posiciones superior\n"
                            f"🎯 Resultado ajustado en el minuto {minute}\n"
                            f"{'⚖️ Empate' if winning == 'Empate' else f'🔵 Gana {winning}'}"
                        )
                        send_telegram(msg)
                        log.info(f"ALERTA LATE GAME: {msg}")

        # --------------------------------------------------------
        # ALERTA 3: DOBLE ROJA (independiente de tabla)
        # --------------------------------------------------------
        for team in [home, away]:
            if len(reds[team["id"]]) >= 2:
                double_key = f"{fixture_id}_{team['id']}_double"
                if double_key not in alerted["double_red"]:
                    alerted["double_red"].add(double_key)
                    msg = (
                        f"🟥🟥 <b>ALERTA TIPSTER — DOBLE ROJA</b>\n"
                        f"🏆 {league_name}\n"
                        f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                        f"⚠️ {team['name']} acumula {len(reds[team['id']])} expulsados\n"
                        f"⏱️ Min {minute}"
                    )
                    send_telegram(msg)
                    log.info(f"ALERTA DOBLE ROJA: {msg}")

    except Exception as e:
        log.error(f"Error procesando partido: {e}")

# ============================================================
# ALERTA PRE-PARTIDO: JUGADORES CON 4 AMARILLAS
# ============================================================
last_yellow_check = None

def check_yellow_cards():
    global last_yellow_check
    now = datetime.utcnow()

    # Solo una vez al día a las 9:00 UTC
    if last_yellow_check and last_yellow_check.date() == now.date():
        return
    if now.hour != 9:
        return

    last_yellow_check = now
    season = now.year

    log.info("Revisando jugadores con 4 tarjetas amarillas...")

    for league_name, league_id in TOP_LEAGUES.items():
        # Obtener próximos partidos de la liga
        fixtures = api_get("fixtures", {
            "league": league_id,
            "season": season,
            "next": 10
        })

        teams_playing = set()
        for f in fixtures:
            teams_playing.add(f["teams"]["home"]["id"])
            teams_playing.add(f["teams"]["away"]["id"])

        # Revisar jugadores con tarjetas
        for team_id in teams_playing:
            players = api_get("players", {
                "team": team_id,
                "league": league_id,
                "season": season
            })
            for p in players:
                try:
                    stats = p["statistics"][0]
                    yellows = stats["cards"]["yellow"]
                    player_name = p["player"]["name"]
                    player_id = p["player"]["id"]
                    team_name = stats["team"]["name"]

                    if yellows == 4:
                        key = f"{player_id}_{season}"
                        if key not in alerted["yellow4"]:
                            alerted["yellow4"].add(key)
                            msg = (
                                f"🟨🟨🟨🟨 <b>ALERTA PRE-PARTIDO — 4 AMARILLAS</b>\n"
                                f"🏆 {league_name}\n"
                                f"👤 {player_name} ({team_name})\n"
                                f"⚠️ Tiene 4 amarillas — puede forzar la quinta\n"
                                f"🔍 Revisa si juega hoy"
                            )
                            send_telegram(msg)
                            log.info(f"ALERTA 4 AMARILLAS: {player_name}")
                except Exception:
                    continue

# ============================================================
# BUCLE PRINCIPAL
# ============================================================
def main():
    log.info("🚀 TipsterBot arrancado!")
    send_telegram("🚀 <b>TipsterBot activado</b>\nMonitorizando partidos en tiempo real...")

    while True:
        try:
            # Alertas live
            fixtures = get_live_fixtures()
            log.info(f"Partidos live encontrados: {len(fixtures)}")
            for fixture in fixtures:
                process_fixture(fixture)

            # Alerta pre-partido (4 amarillas)
            check_yellow_cards()

        except Exception as e:
            log.error(f"Error en bucle principal: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
