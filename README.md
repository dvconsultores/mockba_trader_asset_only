# Mockba Trader Asset Only

Este proyecto es un bot de trading automatizado para Apolo Futures que utiliza señales de ML, análisis con LLM y gestión de posiciones.

## Requisitos Previos

- Python 3.8+ instalado en tu sistema
- Docker instalado (opcional, para despliegue en contenedores)
- Docker Compose instalado (opcional)
- Una cuenta en Apolo con API habilitada
- Una clave API de DeepSeek para análisis LLM
- Un bot de Telegram configurado (opcional, para notificaciones)

## Configuración

### 1. Archivo de Entorno (.env)

Crea un archivo `.env` en la raíz del proyecto con las siguientes variables de entorno:

```env
# Claves de Apolo
Apolo_API_KEY=tu_api_key_de_apolo
Apolo_SECRET_KEY=tu_secret_key_de_apolo

# Clave de DeepSeek para Análisis LLM
DEEP_SEEK_API_KEY=tu_clave_de_deepseek

# Configuración de Telegram (opcional)
API_TOKEN=tu_token_del_bot_de_telegram
TELEGRAM_CHAT_ID=tu_chat_id_de_telegram

# Configuración del Bot
BOT_LANGUAGE=en  # Idioma del bot (en, es, etc.)
REDIS_URL=tu_url_de_redis  # Opcional, para cachear traducciones
```

### 2. Archivo de Plantilla de Prompt LLM

El archivo `llm_prompt_template.txt` se encuentra en `futures_perps/trade/trade/` y contiene tu plantilla de prompt personalizada para el análisis LLM. Este archivo se puede editar sin reconstruir.

Ejemplo de plantilla básica:

```
Eres un trader experimentado. Analiza los datos y proporciona una recomendación.
```

### 3. Dependencias de Python

Instala los paquetes requeridos:

```bash
pip install -r requirements.txt
```

### 4. Inicialización de Base de Datos

El bot utiliza SQLite para configuraciones. Las tablas de la base de datos se inicializan automáticamente al ejecutar el bot de Telegram.

## Despliegue

### Opción 1: Ejecución Directa con Python

1. Asegúrate de que Python y las dependencias estén instaladas.

2. Navega al directorio del proyecto:

   ```bash
   cd mockba_trader_asset_only
   ```

3. Ejecuta el bot de Telegram:

   ```bash
   python telegram.py
   ```

4. Para el bot de trading, ejecuta:

   ```bash
   python futures_perps/trade/trade/main.py
   ```

### Opción 2: Despliegue con Docker

1. Asegúrate de que Docker y Docker Compose estén instalados y ejecutándose.

2. Navega al directorio del proyecto:

   ```bash
   cd mockba_trader_asset_only
   ```

3. Ejecuta el contenedor:

   ```bash
   docker compose -f docker-compose-mockba-apolo-asset.yml up -d
   ```

   Esto iniciará el bot y Watchtower para actualizaciones automáticas.

4. Para ver los logs:

   ```bash
   docker compose -f docker-compose-mockba-apolo-asset.yml logs -f
   ```

5. Para detener:

   ```bash
   docker compose -f docker-compose-mockba-apolo-asset.yml down
   ```

## Funcionalidades

- **Señales de ML**: Recibe señales de trading desde una API externa.
- **Análisis LLM**: Utiliza DeepSeek para analizar velas y libro de órdenes antes de ejecutar trades.
- **Gestión de Posiciones**: Monitorea posiciones abiertas y cierra cuando se alcanzan TP/SL.
- **Notificaciones Telegram**: Envía actualizaciones de posiciones al bot de Telegram.
- **Micro Backtesting**: Valida señales con backtesting rápido antes de ejecutar.
- **Persistencia de Liquidez**: Verifica consenso CEX/DEX antes de trades.

## Estructura del Proyecto

- `futures_perps/trade/trade/main.py`: Lógica principal del bot
- `telegram.py`: Bot de Telegram para control manual
- `db/db_ops.py`: Operaciones de base de datos SQLite
- `logs/`: Directorio de logs
- `data/`: Base de datos y archivos persistentes
- `requirements.txt`: Dependencias de Python
- `Dockerfile`: Definición de imagen Docker
- `docker-compose-mockba-apolo-asset.yml`: Configuración de Docker Compose

## Solución de Problemas

- **Error de Conexión a Apolo**: Verifica tus claves API y permisos.
- **Error de LLM**: Asegúrate de que DEEP_SEEK_API_KEY sea válida.
- **Archivo No Encontrado**: Asegúrate de que `llm_prompt_template.txt` exista en `futures_perps/trade/trade/`.
- **Errores de Importación de Python**: Ejecuta `pip install -r requirements.txt` y limpia `__pycache__` si es necesario.

# 🤖 Guía Completa de Configuración

Este documento te guiará paso a paso para desplegar tu propio **Mockba Trader Bot** en un VPS usando Docker, conectado a Apolo, DeepSeek y Telegram.

---

## 📋 Índice

1. [Crear un VPS en DigitalOcean](#-crear-un-vps-en-digitalocean)
2. [Configurar Bot de Telegram](#-configurar-bot-de-telegram)
3. [Obtener API Keys de Apolo](#-obtener-api-keys-de-apolo)
4. [Obtener API Key de DeepSeek](#-obtener-api-key-de-deepseek)
5. [⚙️ Configuración del Bot](#️-configuración-del-bot)

---

## 🖥️ Crear un VPS en DigitalOcean

### Paso a paso:

1. **Crear Cuenta en DigitalOcean**
   - Regístrate y obtén **$200 de crédito gratis por 60 días**.

2. **Crear Droplet**
   - Ve a **"Droplets" → "Create Droplet"**
   - **Choose an image**: Haz clic en **"Marketplace"** → busca **"Docker"** → selecciona **"Docker on Ubuntu"**
   - **Choose a plan**:
     - Plan: **Basic**
     - CPU Option: **Regular Intel with SSD**
     - Precio: **$6/mes** (suficiente para este bot)
   - **Authentication**:
     - Opción recomendada: **Password** (más fácil para principiantes)
     - Opción avanzada: **SSH Key** (más segura)
   - Haz clic en **"Create Droplet"**

3. **Acceder a tu VPS**
   - Espera 1–2 minutos a que el Droplet se cree.
   - **Opción 1 (consola web)**: Haz clic en **"Console"** desde el panel de DigitalOcean.
   - **Opción 2 (SSH)**:
     ```bash
     ssh root@TU_IP_DEL_DROPLET
     ```

---

## 🤖 Configurar Bot de Telegram

1. **Crear Bot con @BotFather**
   - Abre Telegram y busca **@BotFather**
   - Envía `/start` luego `/newbot`
   - Sigue las instrucciones:
     - **Nombre del bot** (visible para usuarios): `Mockba Trader Bot`
     - **Username del bot** (debe terminar en `bot`): `mockba_trader_bot`
   - **Guarda el API Token** proporcionado por BotFather.
     Ejemplo: `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`

2. **Obtener tu Chat ID**
   - Busca tu nuevo bot en Telegram y envía `/start`
   - Abre en tu navegador (reemplaza `<TU_TOKEN>`):
     ```
     https://api.telegram.org/bot<TU_TOKEN>/getUpdates
     ```
   - Busca el campo `"id"` en la respuesta JSON y copia el número.
   - **Alternativa rápida**: usa [@userinfobot](https://t.me/userinfobot) para obtener tu ID.

---

## 🔑 Obtener API Keys de Apolo

1. Inicia sesión en [Apolo](https://dex.apolopay.app/)
2. Haz clic en Portafolio → **"API Keys"**
3. **Crear nueva API**:
   - Nombre: `Mockba Trader Bot`
   - Confirmar
4. **Permisos recomendados**:
   - ✅ **Enable Reading**
   - ✅ **Enable Trading**
   - ✅ **Restrict IP optional**
5. **Guarda ambas claves**:
   - `API Key`: ej. `abc123def456`
   - `Secret Key`: cadena más larga (¡NO la compartas!)

---

## 🔮 Obtener API Key de DeepSeek

1. Ve a [DeepSeek](https://platform.deepseek.com/)
2. Regístrate o inicia sesión
3. Ve a **"API Management"** o **"API Keys"**
4. Crea una nueva clave
5. **Copia y guarda** la API Key generada

> ⚠️ Esta clave es necesaria para el análisis de señales con LLM.

---

## ⚙️ Configuración del Bot

Después de clonar e instalar el proyecto, edita el archivo de entorno:

nano /opt/mockba-trader/.env

---

## 📋 Requisitos del VPS

- **Sistema operativo**: Debian 13 (Trixie) o superior ✅
  _(Ubuntu también funciona, pero Debian 13+ es lo recomendado para estabilidad)_
- **Región**: Frankfurt (`FRA1`) u otra **fuera de EE.UU.**
- **RAM**: Mínimo 1 GB
- **Disco**: 25 GB SSD
- **Acceso**: `root` o usuario con `sudo`

> 💡 ¿Usas DigitalOcean? Selecciona **Debian 13** como imagen base (no uses "Docker on Ubuntu" si prefieres Debian).

---

## 🚀 Despliegue Automático (Recomendado)

Ejecuta este comando **una sola vez** en tu VPS recién creado:

# 1. Download
curl -fsSL https://raw.githubusercontent.com/Mockba-Bot/mockba_trader_asset_only/main/desplegar-mockba.sh -o desplegar-mockba.sh

# 2. Make it executable
chmod +x desplegar-mockba.sh

# 3. Run it (with sudo if writing to /opt/)
sudo ./desplegar-mockba.sh

## Licencia

Este proyecto es de código abierto. Úsalo bajo tu propio riesgo. Licencia MIT

