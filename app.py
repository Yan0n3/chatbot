import os
import json
import asyncio
import logging
import traceback
from flask import Flask, request, Response
from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity
from openai import AzureOpenAI

# Configurar logging primero para ver todos los errores
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("AzureBot")

# Variables para controlar la disponibilidad de servicios
cosmos_available = False
graph_available = False

# Inicializar componentes opcionales (Cosmos DB y Graph API)
try:
    from azure.cosmos import CosmosClient
    from azure.cosmos.exceptions import CosmosHttpResponseError

    # Configuración Cosmos DB
    COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT")
    COSMOS_KEY = os.environ.get("COSMOS_KEY")
    
    if not COSMOS_ENDPOINT or not COSMOS_KEY:
        logger.warning("Credenciales de Cosmos DB no configuradas correctamente")
    else:
        try:
            cosmos_client = CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY)
            database = cosmos_client.get_database_client("convenciones-db")
            event_container = database.get_container_client("Eventos")
            cosmos_available = True
            logger.info("Conexión a Cosmos DB establecida correctamente")
        except Exception as e:
            logger.error(f"Error al conectar con Cosmos DB: {e}")
except ImportError:
    logger.warning("Módulo azure.cosmos no disponible")

# Intentar inicializar MS Graph
try:
    from azure.identity import ClientSecretCredential
    from msgraph import GraphServiceClient
    
    # Configuración MS Graph
    TENANT_ID = os.environ.get("TENANT_ID")
    CLIENT_ID = os.environ.get("CLIENT_ID")
    CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
    
    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
        logger.warning("Credenciales de MS Graph no configuradas correctamente")
    else:
        try:
            credential = ClientSecretCredential(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
            graph_client = GraphServiceClient(credential)
            graph_available = True
            logger.info("Conexión a MS Graph establecida correctamente")
        except Exception as e:
            logger.error(f"Error al conectar con MS Graph: {e}")
except ImportError:
    logger.warning("Módulo msgraph no disponible")

# Crear app Flask
app = Flask(__name__)
PORT = int(os.environ.get("PORT", 3978))

# Almacenamiento temporal de preferencias (en producción usa Cosmos DB)
user_preferences = {}

# Credenciales de Bot Framework
APP_ID = os.environ.get("MicrosoftAppId", "")
APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")

# Configuración Azure OpenAI
AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_DEPLOYMENT_NAME = os.environ.get("AZURE_DEPLOYMENT_NAME", "gpt-4.1")
openai_available = False

# Cliente Azure OpenAI
if AZURE_OPENAI_KEY and AZURE_OPENAI_ENDPOINT:
    try:
        ai_client = AzureOpenAI(
            api_key=AZURE_OPENAI_KEY,
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_version=AZURE_OPENAI_API_VERSION,
        )
        openai_available = True
        logger.info("Conexión a Azure OpenAI establecida correctamente")
    except Exception as e:
        logger.error(f"Error al conectar con Azure OpenAI: {e}")
else:
    logger.warning("Credenciales de Azure OpenAI no configuradas correctamente")

# Configurar BotFramework Adapter
settings = BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD)
adapter = BotFrameworkAdapter(settings)

# Manejo de errores
async def on_error(context: TurnContext, error: Exception):
    logger.error(f"[on_turn_error] {error}")
    logger.error(traceback.format_exc())
    await context.send_activity("Lo siento, ha ocurrido un error interno.")

adapter.on_turn_error = on_error

# Función para procesar cada mensaje
async def process_message(turn_context: TurnContext):
    user_id = turn_context.activity.from_property.id
    user_text = (turn_context.activity.text or "").strip().lower()

    # Flujo de preferencias inicial
    if user_id not in user_preferences or user_preferences[user_id].get("estado") == "esperando_intereses":
        await turn_context.send_activity(
            "¡Hola! ¿Qué tipo de eventos te interesan? (Ej: IA, Marketing, Cloud)"
        )
        user_preferences[user_id] = {"estado": "esperando_intereses"}
        return

    # Guardar intereses
    if user_preferences[user_id]["estado"] == "esperando_intereses":
        intereses = [i.strip() for i in user_text.split(",") if i.strip()]
        user_preferences[user_id].update({"intereses": intereses, "estado": "listo"})
        await turn_context.send_activity("¡Genial! Ahora puedo recomendarte eventos.")
        return

    # Detectar respuesta sí/no si hay evento pendiente
    if "evento_pendiente" in user_preferences[user_id]:
        if user_text in ("sí", "si"):  # Agendar evento
            if not cosmos_available:
                await turn_context.send_activity("Lo siento, no puedo acceder a la base de datos de eventos en este momento.")
                user_preferences[user_id].pop("evento_pendiente", None)
                user_preferences[user_id].pop("evento_pendiente_sala", None)
                return
                
            pend = user_preferences[user_id]["evento_pendiente"]
            sala = user_preferences[user_id]["evento_pendiente_sala"]
            try:
                evento = event_container.read_item(item=pend, partition_key=sala)
                
                # Si MS Graph está disponible, crear evento en el calendario
                if graph_available:
                    new_event = {
                        "subject": evento["nombre"],
                        "start": {"dateTime": evento["hora"], "timeZone": "UTC"},
                        "end": {"dateTime": evento.get("hora_fin", evento["hora"]), "timeZone": "UTC"},
                        "location": {"displayName": evento["sala"]}
                    }
                    try:
                        await graph_client.me.calendar.events.create(new_event)
                        await turn_context.send_activity("¡Evento agendado en tu calendario!")
                    except Exception as e:
                        logger.error(f"Error al agendar en Graph: {e}")
                        await turn_context.send_activity("No pude agendar el evento en tu calendario.")
                else:
                    await turn_context.send_activity(
                        f"He registrado tu interés en: {evento['nombre']} en {evento['sala']} a las {evento['hora']}. " +
                        "Nota: La integración con el calendario está temporalmente deshabilitada.")
            except CosmosHttpResponseError as e:
                logger.error(f"Error al leer evento {pend}: {e}")
                await turn_context.send_activity("No pude recuperar el evento para agendar.")
                
            # Limpiar pendiente
            user_preferences[user_id].pop("evento_pendiente", None)
            user_preferences[user_id].pop("evento_pendiente_sala", None)
            return

        elif user_text in ("no", "nop"):  # Cancelar agendamiento
            await turn_context.send_activity("De acuerdo, no agendaré ese evento.")
            user_preferences[user_id].pop("evento_pendiente", None)
            user_preferences[user_id].pop("evento_pendiente_sala", None)
            return

    # Flujo de recomendaciones
    if "recomienda" in user_text:
        if not cosmos_available:
            await turn_context.send_activity(
                "Lo siento, el servicio de recomendación de eventos no está disponible en este momento.")
            return
            
        encontrados = []
        for interes in user_preferences[user_id].get("intereses", []):
            items = list(event_container.query_items(
                query="SELECT * FROM Eventos e WHERE ARRAY_CONTAINS(e.temas, @interes)",
                parameters=[{"name": "@interes", "value": interes}],
                enable_cross_partition_query=True
            ))
            encontrados.extend(items)

        if encontrados:
            evento = encontrados[0]
            # Guardar pendiente
            user_preferences[user_id]["evento_pendiente"] = evento["id"]
            user_preferences[user_id]["evento_pendiente_sala"] = evento["sala"]

            await turn_context.send_activity(
                f"Evento recomendado: {evento['nombre']} en {evento['sala']} a las {evento['hora']}. ¿Quieres agendarlo? (sí/no)"
            )
        else:
            await turn_context.send_activity("No hay eventos que coincidan con tus intereses.")
        return

    # Flujos libres: delegar a Azure OpenAI
    if openai_available:
        try:
            response = ai_client.chat.completions.create(
                model=AZURE_DEPLOYMENT_NAME,
                messages=[
                    {"role": "system", "content": "Eres un asistente útil."},
                    {"role": "user", "content": user_text}
                ],
                max_tokens=800,
                temperature=1.0,
                top_p=1.0,
                frequency_penalty=0.0,
                presence_penalty=0.0
            )
            bot_reply = response.choices[0].message.content
        except Exception as e:
            logger.error(f"Error en OpenAI: {e}")
            bot_reply = "Lo siento, no pude procesar tu solicitud en este momento."
    else:
        bot_reply = "Estoy en modo limitado y no puedo responder preguntas generales en este momento."

    logger.info(f"Respuesta bot: {bot_reply}")
    await turn_context.send_activity(bot_reply)

# Ruta para mensajes
@app.route("/api/messages", methods=["POST"])
def messages():
    logger.info("==== Nueva solicitud recibida ====")
    if "application/json" not in request.headers.get("Content-Type", ""):
        return Response(status=415)

    body = request.json
    logger.info(f"Body recibido: {body}")
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")

    task = adapter.process_activity(activity, auth_header, process_message)
    try:
        asyncio.run(task)
    except Exception as e:
        logger.error(f"Error al procesar actividad: {e}")
        return Response(status=500)

    return Response(status=200)

# Endpoint diagnóstico
@app.route("/", methods=["GET"])
def home():
    status = {
        "status": "running",
        "cosmos_db": "available" if cosmos_available else "unavailable",
        "msgraph": "available" if graph_available else "unavailable",
        "openai": "available" if openai_available else "unavailable"
    }
    return json.dumps(status), 200, {'Content-Type': 'application/json'}

# Iniciar servidor
if __name__ == "__main__":
    try:
        app.run(host='0.0.0.0', port=PORT)
    except Exception as ex:
        logger.error(f"Error al iniciar servidor: {ex}")
