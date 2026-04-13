import os
import time
import requests
import logging
from datetime import datetime, timezone

# ============================================================
# CONFIGURACIÓN
# ============================================================
API_KEY = os.environ.get("API_FOOTBALL_KEY", "414095f59915ba82a818e44aa3b01fed")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8653122860:AAEd6mUAEp2oKKYla8lTFOadTDDiVtPm5Uo")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "364177709")

MIN_POSITION_DIFF = 7
LATE_GAME_MINUTE = 75
MIN_ALERT_MINUTE = 20
POLL_INTERVAL = 90

# Ligas para alerta de pocas amarillas
LOW_YELLOW_LEAGUES = {
    140,  # LaLiga
    135,  # Serie A
    61,   # Ligue 1
    78,   # Bundesliga
}

# Competiciones donde juegan equipos españoles (Champions, Europa League, Copa del Rey, etc.)
SPANISH_CUPS = {
    "Copa del Rey", "Supercopa de España",
    "UEFA Champions League", "UEFA Europa League", "UEFA Conference League",
}

# Equipos de LaLiga (se rellena dinámicamente)
laliga_teams = set()
laliga_teams_loaded = False

TOP_LEAGUES = {
    "LaLiga": 140,
    "Premier League": 39,
    "Serie A": 135,
    "Ligue 1": 61,
    "Bundesliga": 78,
}

# ============================================================
# FILTRO DE LIGAS POR PAÍS Y TIPO
# ============================================================

# España: todas las divisiones (sin límite de tipo)
SPAIN_COUNTRIES = {"Spain"}

# Países con 1ª y 2ª división permitidas
# (el bot filtra por type=="League" y rank<=2 dinámicamente)
ALLOWED_COUNTRIES = {
    # Europa
    "England", "Italy", "France", "Germany", "Portugal",
    "Netherlands", "Belgium", "Scotland", "Greece", "Ukraine",
    "Austria", "Switzerland", "Croatia", "Serbia", "Czech Republic",
    "Poland", "Denmark", "Sweden", "Norway", "Slovakia", "Romania",
    "Bulgaria", "Hungary", "Turkey", "Russia", "Northern Ireland",
    "North Macedonia", "Latvia", "Lithuania", "Malta", "Wales",
    "Kyrgyzstan", "San Marino",
    # Resto del mundo
    "South Africa", "United Arab Emirates", "Uruguay", "Paraguay",
}

# Cache de ligas permitidas por país (se rellena dinámicamente)
# fixture_id -> True/False
league_country_cache = {}  # league_id -> bool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ============================================================
# ESTADO
# ============================================================
alerted = {
    "red_underdog": set(),
    "double_red": set(),
    "combo": set(),
    "late_game": set(),
    "yellow4": set(),
    "low_yellows": set(),
}

pending_stats = {}
daily_stats = {}
weekly_stats = {}
resolved_fixtures = set()

ALERT_TYPES = ["red_underdog", "double_red", "late_game", "combo", "low_yellows"]
ALERT_LABELS = {
    "red_underdog":  "🟥 ROJA AL UNDERDOG",
    "double_red":    "🟥🟥 DOBLE ROJA",
    "late_game":     "⏱️ MINUTO 75+",
    "combo":         "🔄 COMBO GOL + ROJA",
    "low_yellows":   "🟨 POCAS AMARILLAS",
}

def init_stats(d):
    for t in ALERT_TYPES:
        if t not in d:
            d[t] = {"alertas": 0, "exitos": 0, "fallos": 0}

init_stats(daily_stats)
init_stats(weekly_stats)

last_daily_report = None
last_weekly_report = None

standings_cache = {}
standings_cache_time = {}
STANDINGS_TTL = 6 * 3600

events_cache = {}
events_cache_time = {}
EVENTS_TTL = 120

interesting_fixtures = set()
uninteresting_fixtures = set()

api_calls_today = 0
api_calls_date = None

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            log.error(f"Telegram error: {r.text}")
    except Exception as e:
        log.error(f"Telegram exception: {e}")

last_update_id = None

def check_telegram_commands():
    """Escucha comandos entrantes de Telegram como /status."""
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"timeout": 0, "limit": 5}
        if last_update_id:
            params["offset"] = last_update_id + 1
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return
        updates = r.json().get("result", [])
        for update in updates:
            last_update_id = update["update_id"]
            message = update.get("message", {})
            text = message.get("text", "").strip().lower()
            chat_id = message.get("chat", {}).get("id")
            if str(chat_id) != str(TELEGRAM_CHAT_ID):
                continue
            if text == "/status":
                send_status()
    except Exception as e:
        log.error(f"Error leyendo comandos Telegram: {e}")

def send_status():
    """Envía un resumen del estado actual del bot."""
    now = datetime.now(timezone.utc)
    total_alertas_hoy = sum(daily_stats[t]["alertas"] for t in ALERT_TYPES)
    total_alertas_semana = sum(weekly_stats[t]["alertas"] for t in ALERT_TYPES)

    msg = (
        f"🤖 <b>ESTADO DEL BOT</b>\n\n"
        f"✅ Activo y funcionando\n"
        f"🕐 Hora UTC: {now.strftime('%H:%M:%S')}\n"
        f"📡 API calls hoy: {api_calls_today}/7500\n\n"
        f"📊 <b>Alertas hoy:</b> {total_alertas_hoy}\n"
        f"📊 <b>Alertas esta semana:</b> {total_alertas_semana}\n\n"
    )

    # Desglose por tipo si hay alertas
    if total_alertas_hoy > 0:
        msg += "<b>Desglose de hoy:</b>\n"
        for t in ALERT_TYPES:
            s = daily_stats[t]
            if s["alertas"] > 0:
                msg += f"  {ALERT_LABELS[t]}: {s['alertas']} alertas\n"

    send_telegram(msg)

# ============================================================
# API
# ============================================================
HEADERS = {"x-apisports-key": API_KEY}
BASE_URL = "https://v3.football.api-sports.io"

def api_get(endpoint: str, params: dict = {}):
    global api_calls_today, api_calls_date
    today = datetime.now(timezone.utc).date()
    if api_calls_date != today:
        api_calls_today = 0
        api_calls_date = today
        log.info("Contador de llamadas API reiniciado")
    if api_calls_today >= 7400:
        log.warning("Límite de API cercano, saltando llamada")
        return []
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}", headers=HEADERS, params=params, timeout=15)
        api_calls_today += 1
        if r.status_code == 200:
            return r.json().get("response", [])
        else:
            log.error(f"API error {r.status_code}: {r.text}")
            return []
    except Exception as e:
        log.error(f"API exception: {e}")
        return []

def get_standings(league_id: int, season: int):
    key = (league_id, season)
    now = time.time()
    if key in standings_cache and now - standings_cache_time.get(key, 0) < STANDINGS_TTL:
        return standings_cache[key]
    data = api_get("standings", {"league": league_id, "season": season})
    if data:
        try:
            table = data[0]["league"]["standings"][0]
            result = {team["team"]["id"]: team["rank"] for team in table}
            standings_cache[key] = result
            standings_cache_time[key] = now
            return result
        except Exception as e:
            log.error(f"Error parsing standings: {e}")
    return {}

def get_fixture_events(fixture_id: int):
    now = time.time()
    if fixture_id in events_cache and now - events_cache_time.get(fixture_id, 0) < EVENTS_TTL:
        return events_cache[fixture_id]
    data = api_get("fixtures/events", {"fixture": fixture_id})
    events_cache[fixture_id] = data
    events_cache_time[fixture_id] = now
    return data

# ============================================================
# ESTADÍSTICAS
# ============================================================
def register_alert(fixture_id, alert_type, favorite_id, underdog_id, score_home, score_away, home_id):
    if fixture_id not in pending_stats:
        pending_stats[fixture_id] = []
    pending_stats[fixture_id].append({
        "type": alert_type,
        "favorite_id": favorite_id,
        "home_id": home_id,
    })
    daily_stats[alert_type]["alertas"] += 1
    weekly_stats[alert_type]["alertas"] += 1

def resolve_finished_fixtures(live_fixture_ids: set):
    finished = set(pending_stats.keys()) - live_fixture_ids - resolved_fixtures
    for fixture_id in finished:
        resolved_fixtures.add(fixture_id)
        alerts = pending_stats.pop(fixture_id, [])
        if not alerts:
            continue
        result_data = api_get("fixtures", {"id": fixture_id})
        if not result_data:
            continue
        try:
            f = result_data[0]
            final_home = f["goals"]["home"] or 0
            final_away = f["goals"]["away"] or 0
            home_id = f["teams"]["home"]["id"]
        except Exception as e:
            log.error(f"Error resultado final: {e}")
            continue
        for alert in alerts:
            try:
                favorite_id = alert["favorite_id"]
                alert_type = alert["type"]
                if favorite_id == home_id:
                    fav_final, und_final = final_home, final_away
                else:
                    fav_final, und_final = final_away, final_home
                success = fav_final >= und_final
                if success:
                    daily_stats[alert_type]["exitos"] += 1
                    weekly_stats[alert_type]["exitos"] += 1
                else:
                    daily_stats[alert_type]["fallos"] += 1
                    weekly_stats[alert_type]["fallos"] += 1
                log.info(f"Stat resuelta: {alert_type} {'✅' if success else '❌'}")
            except Exception as e:
                log.error(f"Error resolviendo stat: {e}")

def format_stats_report(stats, titulo):
    lines = [f"📊 <b>{titulo}</b>\n"]
    total = sum(stats[t]["alertas"] for t in ALERT_TYPES)
    if total == 0:
        lines.append("Sin alertas registradas en este período.")
        return "\n".join(lines)
    for t in ALERT_TYPES:
        s = stats[t]
        if s["alertas"] == 0:
            continue
        resueltas = s["exitos"] + s["fallos"]
        tasa = f"{round(s['exitos']/resueltas*100)}%" if resueltas > 0 else "pendiente"
        lines.append(f"{ALERT_LABELS[t]}\nAlertas: {s['alertas']} | ✅ {s['exitos']} | ❌ {s['fallos']} | Tasa: {tasa}\n")
    return "\n".join(lines)

def check_reports():
    global last_daily_report, last_weekly_report
    now = datetime.now(timezone.utc)

    if now.hour == 23 and now.minute < 2:
        is_sunday = now.weekday() == 6

        # El domingo: primero enviamos el semanal (incluye el día de hoy) y luego el diario
        if is_sunday and (last_weekly_report is None or (now - last_weekly_report).days >= 6):
            last_weekly_report = now
            send_telegram(format_stats_report(weekly_stats, f"RESUMEN SEMANAL — {now.strftime('%d/%m/%Y')}"))
            init_stats(weekly_stats)

        # Informe diario (todos los días incluido el domingo)
        if last_daily_report is None or last_daily_report.date() != now.date():
            last_daily_report = now
            send_telegram(format_stats_report(daily_stats, f"RESUMEN DIARIO — {now.strftime('%d/%m/%Y')}"))
            init_stats(daily_stats)

# ============================================================
# FILTRO DE LIGA
# ============================================================
def is_allowed_league(league_id: int, country: str, league: dict) -> bool:
    if league_id in league_country_cache:
        return league_country_cache[league_id]

    # España: todas las divisiones
    if country in SPAIN_COUNTRIES:
        league_country_cache[league_id] = True
        return True

    # Resto de países permitidos: solo ligas tipo "League" (no Copa, Supercopa, etc.)
    if country in ALLOWED_COUNTRIES:
        league_type = league.get("type", "")
        allowed = league_type == "League"
        league_country_cache[league_id] = allowed
        return allowed

    league_country_cache[league_id] = False
    return False

# ============================================================
# PROCESAR PARTIDO
# ============================================================
def process_fixture(fixture: dict):
    try:
        f = fixture["fixture"]
        fixture_id = f["id"]
        status = f["status"]["short"]
        minute = f["status"]["elapsed"] or 0

        if status not in ["1H", "HT", "2H", "ET", "P"]:
            return

        # No alertar en los primeros minutos
        if minute < MIN_ALERT_MINUTE:
            return

        league = fixture["league"]
        league_id = league["id"]
        league_name = league["name"]
        country = league.get("country", "")
        season = league["season"]

        # ---- FILTRO DE LIGA ----
        if not is_allowed_league(league_id, country, league):
            return

        home = fixture["teams"]["home"]
        away = fixture["teams"]["away"]
        score_home = fixture["goals"]["home"] or 0
        score_away = fixture["goals"]["away"] or 0

        league_header = f"🏆 {league_name} | 🌍 {country}" if country else f"🏆 {league_name}"

        # ---- FILTRO DE TABLA ----
        if fixture_id in uninteresting_fixtures:
            return

        if fixture_id not in interesting_fixtures:
            standings = get_standings(league_id, season)
            pos_home = standings.get(home["id"])
            pos_away = standings.get(away["id"])
            if pos_home and pos_away:
                diff = abs(pos_home - pos_away)
                if diff >= MIN_POSITION_DIFF:
                    interesting_fixtures.add(fixture_id)
                else:
                    uninteresting_fixtures.add(fixture_id)
                    return
            else:
                return

        standings = get_standings(league_id, season)
        pos_home = standings.get(home["id"])
        pos_away = standings.get(away["id"])
        if not pos_home or not pos_away:
            return

        diff = abs(pos_home - pos_away)

        if pos_home > pos_away:
            underdog, favorite = home, away
            underdog_score, fav_score = score_home, score_away
            underdog_pos, fav_pos = pos_home, pos_away
        else:
            underdog, favorite = away, home
            underdog_score, fav_score = score_away, score_home
            underdog_pos, fav_pos = pos_away, pos_home

        events = get_fixture_events(fixture_id)

        reds = {home["id"]: [], away["id"]: []}
        yellows = {home["id"]: 0, away["id"]: 0}
        for ev in events:
            if ev.get("type") == "Card":
                detail = ev.get("detail", "")
                team_id = ev["team"]["id"]
                if detail in ["Red Card", "Second Yellow"]:
                    player = ev.get("player", {}).get("name", "Desconocido")
                    ev_minute = ev.get("time", {}).get("elapsed", 0)
                    if team_id in reds:
                        reds[team_id].append({"player": player, "minute": ev_minute})
                elif detail == "Yellow Card":
                    if team_id in yellows:
                        yellows[team_id] += 1

        # ALERTA 1: ROJA AL UNDERDOG
        # Solo si el favorito empata o va perdiendo Y no tiene más rojas que el underdog
        fav_no_gana_roja = fav_score <= underdog_score
        fav_reds = len(reds[favorite["id"]])
        und_reds = len(reds[underdog["id"]])
        fav_no_mas_rojas = fav_reds <= und_reds
        for red in reds[underdog["id"]]:
            red_key = f"{fixture_id}_{underdog['id']}_{red['player']}"
            if red_key not in alerted["red_underdog"] and fav_no_gana_roja and fav_no_mas_rojas:
                alerted["red_underdog"].add(red_key)
                situacion = f"🔴 {favorite['name']} va PERDIENDO" if fav_score < underdog_score else "⚖️ EMPATE"
                send_telegram(
                    f"🟥 <b>ALERTA TIPSTER — ROJA AL UNDERDOG</b>\n"
                    f"{league_header}\n"
                    f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                    f"⬆️ {favorite['name']} es {diff} posiciones superior (#{fav_pos} vs #{underdog_pos})\n"
                    f"👤 Expulsado: {red['player']} ({underdog['name']})\n"
                    f"📊 {situacion}\n"
                    f"⏱️ Min {red['minute']}"
                )
                register_alert(fixture_id, "red_underdog", favorite["id"], underdog["id"], score_home, score_away, home["id"])

        # ALERTA 2: DOBLE ROJA
        # Solo si hay diferencia de 2 o más rojas entre equipos
        reds_home = len(reds[home["id"]])
        reds_away = len(reds[away["id"]])
        red_diff = abs(reds_home - reds_away)
        if red_diff >= 2:
            team_more = home if reds_home > reds_away else away
            team_less = away if reds_home > reds_away else home
            double_key = f"{fixture_id}_{team_more['id']}_double"
            if double_key not in alerted["double_red"]:
                alerted["double_red"].add(double_key)
                send_telegram(
                    f"🟥🟥 <b>ALERTA TIPSTER — DOBLE ROJA</b>\n"
                    f"{league_header}\n"
                    f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                    f"⚠️ {team_more['name']}: {len(reds[team_more['id']])} expulsados | {team_less['name']}: {len(reds[team_less['id']])}\n"
                    f"⏱️ Min {minute}"
                )
                register_alert(fixture_id, "double_red", team_less["id"], team_more["id"], score_home, score_away, home["id"])

        # ALERTA 3: MINUTO 75+
        # Solo si favorito empata o va perdiendo Y no tiene más rojas que el underdog
        if minute >= LATE_GAME_MINUTE:
            late_key = f"{fixture_id}_late"
            fav_no_gana = fav_score <= underdog_score
            score_ajustado = abs(score_home - score_away) <= 1
            fav_no_mas_rojas_late = len(reds[favorite["id"]]) <= len(reds[underdog["id"]])
            if fav_no_gana and score_ajustado and fav_no_mas_rojas_late and late_key not in alerted["late_game"]:
                alerted["late_game"].add(late_key)
                resultado = f"🔴 {favorite['name']} va PERDIENDO" if fav_score < underdog_score else "⚖️ EMPATE"
                send_telegram(
                    f"⏱️ <b>ALERTA TIPSTER — MINUTO 75+</b>\n"
                    f"{league_header}\n"
                    f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                    f"⬆️ {favorite['name']} es {diff} posiciones superior\n"
                    f"🎯 Min {minute} — {resultado}"
                )
                register_alert(fixture_id, "late_game", favorite["id"], underdog["id"], score_home, score_away, home["id"])

        # ALERTA 4: COMBO GOL + ROJA
        combo_key = f"{fixture_id}_combo"
        if underdog_score > fav_score and und_reds > 0 and combo_key not in alerted["combo"]:
            alerted["combo"].add(combo_key)
            send_telegram(
                f"🔄 <b>ALERTA TIPSTER — COMBO GOL + ROJA</b>\n"
                f"{league_header}\n"
                f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                f"⬆️ {favorite['name']} es {diff} posiciones superior\n"
                f"⚠️ {underdog['name']} va ganando Y tiene expulsado\n"
                f"⏱️ Min {minute}"
            )
            register_alert(fixture_id, "combo", favorite["id"], underdog["id"], score_home, score_away, home["id"])

    except Exception as e:
        log.error(f"Error procesando partido: {e}")

# ============================================================
# ALERTA POCAS AMARILLAS (proceso separado — todas las ligas permitidas)
# ============================================================
def process_low_yellows(fixture: dict):
    """Alerta de pocas amarillas para LaLiga, Serie A, Ligue 1, Bundesliga
    y competiciones europeas/copas con equipos españoles."""
    try:
        f = fixture["fixture"]
        fixture_id = f["id"]
        status = f["status"]["short"]
        minute = f["status"]["elapsed"] or 0

        if status not in ["1H", "HT", "2H", "ET", "P"]:
            return
        if minute < 63:
            return

        league = fixture["league"]
        league_id = league["id"]
        league_name = league["name"]
        country = league.get("country", "")
        season = league["season"]

        home = fixture["teams"]["home"]
        away = fixture["teams"]["away"]
        score_home = fixture["goals"]["home"] or 0
        score_away = fixture["goals"]["away"] or 0

        # Filtro: solo si resultado es empate o diferencia de 1
        if abs(score_home - score_away) > 1:
            return

        league_header = f"🏆 {league_name} | 🌍 {country}" if country else f"🏆 {league_name}"

        # Determinar si aplica esta alerta
        is_top_league = league_id in LOW_YELLOW_LEAGUES
        is_spanish_cup = league_name in SPANISH_CUPS

        # Para copas españolas, verificar si algún equipo es de LaLiga
        if not is_top_league and not is_spanish_cup:
            return

        # Obtener eventos para contar amarillas
        events = get_fixture_events(fixture_id)
        total_yellows = 0
        for ev in events:
            if ev.get("type") == "Card" and ev.get("detail") == "Yellow Card":
                total_yellows += 1

        # Solo alertar si hay 0 o 1 amarillas en total
        if total_yellows > 1:
            return

        low_yellow_key = f"{fixture_id}_lowyellow"
        if low_yellow_key not in alerted["low_yellows"]:
            alerted["low_yellows"].add(low_yellow_key)
            if score_home == score_away:
                marcador_texto = "⚖️ Empate"
            else:
                lider = home["name"] if score_home > score_away else away["name"]
                marcador_texto = f"🔵 Gana {lider}"
            send_telegram(
                f"🟨 <b>ALERTA TIPSTER — POCAS AMARILLAS</b>\n"
                f"{league_header}\n"
                f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                f"📋 Total amarillas: {total_yellows}\n"
                f"📊 {marcador_texto}\n"
                f"⏱️ Min {minute}"
            )
            register_alert(fixture_id, "low_yellows", home["id"], away["id"], score_home, score_away, home["id"])

# ============================================================
# 4 AMARILLAS
# ============================================================
last_yellow_check = None

def check_yellow_cards():
    global last_yellow_check
    now = datetime.now(timezone.utc)
    if last_yellow_check and last_yellow_check.date() == now.date():
        return
    if now.hour != 9:
        return
    last_yellow_check = now
    season = now.year
    log.info("Revisando 4 amarillas...")
    for league_name, league_id in TOP_LEAGUES.items():
        fixtures = api_get("fixtures", {"league": league_id, "season": season, "next": 10})
        teams_playing = set()
        for f in fixtures:
            teams_playing.add(f["teams"]["home"]["id"])
            teams_playing.add(f["teams"]["away"]["id"])
        for team_id in list(teams_playing)[:10]:
            players = api_get("players", {"team": team_id, "league": league_id, "season": season})
            for p in players:
                try:
                    stats = p["statistics"][0]
                    if stats["cards"]["yellow"] == 4:
                        player_id = p["player"]["id"]
                        key = f"{player_id}_{season}"
                        if key not in alerted["yellow4"]:
                            alerted["yellow4"].add(key)
                            send_telegram(
                                f"🟨🟨🟨🟨 <b>ALERTA PRE-PARTIDO — 4 AMARILLAS</b>\n"
                                f"🏆 {league_name}\n"
                                f"👤 {p['player']['name']} ({stats['team']['name']})\n"
                                f"⚠️ Tiene 4 amarillas — puede forzar la quinta\n"
                                f"🔍 Revisa si juega hoy"
                            )
                except Exception:
                    continue

# ============================================================
# BUCLE PRINCIPAL
# ============================================================
def main():
    log.info("🚀 TipsterBot v6 arrancado!")
    send_telegram("🚀 <b>TipsterBot activado</b>\nMonitorizando partidos en tiempo real...")
    cycle = 0
    while True:
        try:
            cycle += 1
            fixtures = api_get("fixtures", {"live": "all"})
            live_ids = {f["fixture"]["id"] for f in fixtures}

            allowed = [f for f in fixtures if is_allowed_league(f["league"]["id"], f["league"].get("country",""), f["league"])]
            interesting = len([f for f in allowed if f["fixture"]["id"] in interesting_fixtures])
            log.info(f"Ciclo {cycle} — Live: {len(fixtures)} | Europa/España: {len(allowed)} | Interesantes: {interesting} | API calls: {api_calls_today}")

            for fixture in fixtures:
                process_fixture(fixture)
                process_low_yellows(fixture)

            resolve_finished_fixtures(live_ids)
            check_yellow_cards()
            check_reports()
            check_telegram_commands()

            if cycle % 100 == 0:
                uninteresting_fixtures.clear()
                interesting_fixtures.clear()

        except Exception as e:
            log.error(f"Error en bucle principal: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
