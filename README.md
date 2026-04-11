# 🤖 TipsterBot — Guía de despliegue en Railway

## ¿Qué hace este bot?

Monitoriza partidos de fútbol en tiempo real y te envía alertas a Telegram cuando:

1. ⚽ **Gol del underdog** — El equipo inferior (≥7 posiciones en tabla) se pone por delante
2. 🟥 **Roja al underdog** — Le expulsan a un jugador del equipo inferior
3. 🟥🟥 **Doble roja** — Un equipo acumula 2 expulsados (independiente de tabla)
4. ⏱️ **Minuto 75+** — Resultado ajustado (0 o 1 gol de diferencia) entre equipos con gran diferencia en tabla
5. 🔄 **Combo gol + roja** — Al underdog le marcan Y tiene expulsado en el mismo partido
6. 🟨🟨🟨🟨 **4 amarillas** — Jugador con 4 amarillas en ligas top con partido próximo (alerta diaria 9:00 UTC)

---

## Despliegue en Railway (paso a paso)

### 1. Crear cuenta en Railway
- Ve a **railway.app**
- Regístrate con tu cuenta de GitHub (necesitarás una cuenta de GitHub gratuita)

### 2. Subir el código a GitHub
- Ve a **github.com** y crea una cuenta gratuita si no tienes
- Crea un repositorio nuevo llamado `tipsterbot`
- Sube los 3 archivos: `main.py`, `requirements.txt`, `Procfile`

### 3. Crear proyecto en Railway
- En Railway, haz clic en **"New Project"**
- Selecciona **"Deploy from GitHub repo"**
- Conecta tu repositorio `tipsterbot`

### 4. Configurar variables de entorno (MUY IMPORTANTE)
En Railway, ve a tu proyecto → **Variables** y añade:

```
API_FOOTBALL_KEY = tu_api_key
TELEGRAM_TOKEN = tu_token_del_bot
TELEGRAM_CHAT_ID = tu_chat_id
```

### 5. Arrancar el bot
- Railway detectará el `Procfile` automáticamente
- El bot arrancará y te llegará un mensaje a Telegram confirmando que está activo

---

## Variables de configuración (en main.py)

| Variable | Valor actual | Descripción |
|----------|-------------|-------------|
| MIN_POSITION_DIFF | 7 | Diferencia mínima de posiciones |
| LATE_GAME_MINUTE | 75 | Minuto para alerta de final de partido |
| POLL_INTERVAL | 60 | Segundos entre consultas |

---

## Ligas monitorizadas para alertas de 4 amarillas
- LaLiga (España)
- Premier League (Inglaterra)
- Serie A (Italia)
- Ligue 1 (Francia)
- Bundesliga (Alemania)

Los partidos live cubren **todas las ligas apostables** del mundo.
