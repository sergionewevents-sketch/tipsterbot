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
POLL_INTERVAL = 90

TOP_LEAGUES = {
    "LaLiga": 140,
    "Premier League": 39,
    "Serie A": 135,
    "Ligue 1": 61,
    "Bundesliga": 78,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ============================================================
# ESTADO
# ============================================================
alerted = {
    "goals": set(),
    "red_underdog": set(),
    "double_red": set(),
    "combo": set(),
    "late_game": set(),
    "yellow4": set(),
}

pending_stats = {}
daily_stats = {}
weekly_stats = {}
resolved_fixtures = set()

ALERT_TYPES = ["goal_underdog", "red_underdog", "double_red", "late_game", "combo"]
ALERT_LABELS = {
    "goal_underdog": "⚽ GOL UNDERDOG",
    "red_underdog":  "🟥 ROJA AL UNDERDOG",
    "double_red":    "🟥🟥 DOBLE ROJA",
    "late_game":     "⏱️ MINUTO 75+",
    "combo":         "🔄 COMBO GOL + ROJA",
}

def init_stats(d):
    for t in ALERT_TYPES:
        if t not in d:
            d[t] = {"alertas": 0, "exitos": 0, "fallos": 0}

init_stats(daily_stats)
init_stats(weekly_stats)

last_daily_report = None
last_weekly_report = None

# Cache de clasificaciones
standings_cache = {}
standings_cache_time = {}
STANDINGS_TTL = 6 * 3600

# Cache de eventos — solo para partidos interesantes
events_cache = {}
events_cache_time = {}
EVENTS_TTL = 120

# Partidos ya clasificados
interesting_fixtures = set()   # tienen diferencia >= 7
uninteresting_fixtures = set() # no tienen diferencia

# Contador de llamadas
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
        if last_daily_report is None or last_daily_report.date() != now.date():
            last_daily_report = now
            send_telegram(format_stats_report(daily_stats, f"RESUMEN DIARIO — {now.strftime('%d/%m/%Y')}"))
            init_stats(daily_stats)
    if now.weekday() == 6 and now.hour == 23 and now.minute < 2:
        if last_weekly_report is None or (now - last_weekly_report).days >= 6:
            last_weekly_report = now
            send_telegram(format_stats_report(weekly_stats, f"RESUMEN SEMANAL — {now.strftime('%d/%m/%Y')}"))
            init_stats(weekly_stats)

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

        league = fixture["league"]
        league_id = league["id"]
        league_name = league["name"]
        country = league.get("country", "")
        season = league["season"]

        home = fixture["teams"]["home"]
        away = fixture["teams"]["away"]
        score_home = fixture["goals"]["home"] or 0
        score_away = fixture["goals"]["away"] or 0

        league_header = f"🏆 {league_name} | 🌍 {country}" if country else f"🏆 {league_name}"

        # ---- PASO 1: ¿Ya sabemos si este partido es interesante? ----
        # Si ya está en uninteresting, lo saltamos completamente
        if fixture_id in uninteresting_fixtures:
            return

        # ---- PASO 2: Si no lo sabemos, consultar standings (con caché) ----
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
                # Sin standings disponibles, saltamos
                return

        # ---- PASO 3: Solo llegamos aquí si el partido ES interesante ----
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

        # ---- PASO 4: Ahora sí pedimos eventos (solo partidos interesantes) ----
        events = get_fixture_events(fixture_id)

        reds = {home["id"]: [], away["id"]: []}
        for ev in events:
            if ev.get("type") == "Card" and ev.get("detail") in ["Red Card", "Second Yellow"]:
                team_id = ev["team"]["id"]
                player = ev.get("player", {}).get("name", "Desconocido")
                ev_minute = ev.get("time", {}).get("elapsed", 0)
                if team_id in reds:
                    reds[team_id].append({"player": player, "minute": ev_minute})

        # ALERTA 1: GOL UNDERDOG
        if underdog_score > fav_score:
            goal_key = f"{fixture_id}_{score_home}_{score_away}"
            if goal_key not in alerted["goals"]:
                alerted["goals"].add(goal_key)
                send_telegram(
                    f"⚽ <b>ALERTA TIPSTER — GOL UNDERDOG</b>\n"
                    f"{league_header}\n"
                    f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                    f"⬆️ {favorite['name']} es {diff} posiciones superior (#{fav_pos} vs #{underdog_pos})\n"
                    f"⏱️ Min {minute}"
                )
                register_alert(fixture_id, "goal_underdog", favorite["id"], underdog["id"], score_home, score_away, home["id"])

        # ALERTA 2: ROJA AL UNDERDOG
        for red in reds[underdog["id"]]:
            red_key = f"{fixture_id}_{underdog['id']}_{red['player']}"
            if red_key not in alerted["red_underdog"]:
                alerted["red_underdog"].add(red_key)
                send_telegram(
                    f"🟥 <b>ALERTA TIPSTER — ROJA AL UNDERDOG</b>\n"
                    f"{league_header}\n"
                    f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                    f"⬆️ {favorite['name']} es {diff} posiciones superior (#{fav_pos} vs #{underdog_pos})\n"
                    f"👤 Expulsado: {red['player']} ({underdog['name']})\n"
                    f"⏱️ Min {red['minute']}"
                )
                register_alert(fixture_id, "red_underdog", favorite["id"], underdog["id"], score_home, score_away, home["id"])

        # ALERTA 3: DOBLE ROJA
        for team in [home, away]:
            if len(reds[team["id"]]) >= 2:
                double_key = f"{fixture_id}_{team['id']}_double"
                if double_key not in alerted["double_red"]:
                    alerted["double_red"].add(double_key)
                    other = away if team["id"] == home["id"] else home
                    send_telegram(
                        f"🟥🟥 <b>ALERTA TIPSTER — DOBLE ROJA</b>\n"
                        f"{league_header}\n"
                        f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                        f"⚠️ {team['name']} acumula {len(reds[team['id']])} expulsados\n"
                        f"⏱️ Min {minute}"
                    )
                    register_alert(fixture_id, "double_red", other["id"], team["id"], score_home, score_away, home["id"])

        # ALERTA 4: MINUTO 75+
        if minute >= LATE_GAME_MINUTE:
            late_key = f"{fixture_id}_late"
            if abs(score_home - score_away) <= 1 and late_key not in alerted["late_game"]:
                alerted["late_game"].add(late_key)
                if score_home > score_away:
                    resultado = f"🔵 Gana {home['name']}"
                elif score_away > score_home:
                    resultado = f"🔵 Gana {away['name']}"
                else:
                    resultado = "⚖️ Empate"
                send_telegram(
                    f"⏱️ <b>ALERTA TIPSTER — MINUTO 75+</b>\n"
                    f"{league_header}\n"
                    f"{home['name']} {score_home} - {score_away} {away['name']}\n"
                    f"⬆️ {favorite['name']} es {diff} posiciones superior\n"
                    f"🎯 Resultado ajustado en el minuto {minute}\n"
                    f"{resultado}"
                )
                register_alert(fixture_id, "late_game", favorite["id"], underdog["id"], score_home, score_away, home["id"])

        # ALERTA 5: COMBO GOL + ROJA
        combo_key = f"{fixture_id}_combo"
        if underdog_score > fav_score and len(reds[underdog["id"]]) > 0 and combo_key not in alerted["combo"]:
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
    log.info("🚀 TipsterBot v4 arrancado!")
    send_telegram("🚀 <b>TipsterBot activado</b>\nMonitorizando partidos en tiempo real...")
    cycle = 0
    while True:
        try:
            cycle += 1
            fixtures = api_get("fixtures", {"live": "all"})
            live_ids = {f["fixture"]["id"] for f in fixtures}
            interesting = len([f for f in fixtures if f["fixture"]["id"] in interesting_fixtures])
            log.info(f"Ciclo {cycle} — Live: {len(fixtures)} | Interesantes: {interesting} | API calls: {api_calls_today}")

            for fixture in fixtures:
                process_fixture(fixture)

            resolve_finished_fixtures(live_ids)
            check_yellow_cards()
            check_reports()

            if cycle % 100 == 0:
                uninteresting_fixtures.clear()
                interesting_fixtures.clear()
                log.info("Caché de partidos limpiada")

        except Exception as e:
            log.error(f"Error en bucle principal: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
