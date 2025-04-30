import os
import json
import asyncio
import logging
import traceback
from flask import Flask, request, Response
from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity
from openai import AzureOpenAI
from azure.cosmos import CosmosClient, PartitionKey
from azure.cosmos.exceptions import CosmosHttpResponseError

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("AzureBot")

# Inicializar servicios
cosmos_available = False
graph_available = False

# Configuración Cosmos DB
try:
    COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT")
    COSMOS_KEY = os.environ.get("COSMOS_KEY")
    if COSMOS_ENDPOINT and COSMOS_KEY:
        cosmos_client = CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY)
        database = cosmos_client.get_database_client("convenciones-db")
        
        # Crear contenedor para estados de usuario
        try:
            database.create_container_if_not_exists(
                id="UserStates",
                partition_key=PartitionKey(path="/user_id"),
                offer_throughput=400
            )
            logger.info("Contenedor UserStates creado/verificado")
        except CosmosHttpResponseError as e:
            logger.error(f"Error al crear contenedor UserStates: {e}")
        
        user_state_container = database.get_container_client("UserStates")
        cosmos_available = True
    else:
        logger.warning("Credenciales de Cosmos DB no configuradas")
except Exception as e:
    logger.error(f"Error en configuración de Cosmos DB: {e}")

# Configuración MS Graph
try:
    from azure.identity import ClientSecretCredential
    from msgraph import GraphServiceClient

    TENANT_ID = os.environ.get("TENANT_ID")
    CLIENT_ID = os.environ.get("CLIENT_ID")
    CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
    
    if all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
        credential = ClientSecretCredential(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
        graph_client = GraphServiceClient(credential)
        graph_available = True
    else:
        logger.warning("Credenciales de MS Graph no configuradas")
except ImportError:
    logger.warning("Módulo msgraph no disponible")

# Configuración Azure OpenAI
AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_DEPLOYMENT_NAME = os.environ.get("AZURE_DEPLOYMENT_NAME", "gpt-4.1")

openai_available = False
if AZURE_OPENAI_KEY and AZURE_OPENAI_ENDPOINT:
    try:
        ai_client = AzureOpenAI(
            api_key=AZURE_OPENAI_KEY,
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_version=AZURE_OPENAI_API_VERSION,
        )
        openai_available = True
    except Exception as e:
        logger.error(f"Error en Azure OpenAI: {e}")
else:
    logger.warning("Credenciales de Azure OpenAI no configuradas")

# Crear app Flask
app = Flask(__name__)
PORT = int(os.environ.get("PORT", 3978))

# Configurar BotFramework Adapter
settings = BotFrameworkAdapterSettings(
    os.environ.get("MicrosoftAppId", ""),
    os.environ.get("MicrosoftAppPassword", "")
)
adapter = BotFrameworkAdapter(settings)

async def on_error(context: TurnContext, error: Exception):
    logger.error(f"[on_turn_error] {error}")
    logger.error(traceback.format_exc())
    await context.send_activity("Lo siento, ha ocurrido un error interno.")
adapter.on_turn_error = on_error

async def get_user_state(user_id: str) -> dict:
    """Obtener estado del usuario desde Cosmos DB"""
    if not cosmos_available:
        return {}
    try:
        item = user_state_container.read_item(
            item=user_id,
            partition_key=user_id
        )
        return item.get('state', {})
    except CosmosHttpResponseError as e:
        if e.status_code == 404:
            return {}
        raise

async def save_user_state(user_id: str, state: dict):
    """Guardar estado del usuario en Cosmos DB"""
    if not cosmos_available:
        return
    try:
        await user_state_container.upsert_item({
            'id': user_id,
            'partition_key': user_id,
            'state': state
        })
    except Exception as e:
        logger.error(f"Error guardando estado: {e}")

async def process_message(turn_context: TurnContext):
    user_id = turn_context.activity.from_property.id
    user_text = (turn_context.activity.text or "").strip().lower()
    
    # Obtener estado actual
    user_state = await get_user_state(user_id)
    
    # Flujo inicial de preferencias
    if not user_state.get("intereses"):
        await turn_context.send_activity(
            "¡Hola! ¿Qué tipo de eventos te interesan? (Ej: IA, Marketing, Cloud)"
        )
        user_state.update({
            "estado": "esperando_intereses",
            "intereses": []
        })
        await save_user_state(user_id, user_state)
        return

    # Guardar intereses
    if user_state.get("estado") == "esperando_intereses":
        intereses = [i.strip() for i in user_text.split(",") if i.strip()]
        user_state.update({
            "intereses": intereses,
            "estado": "listo"
        })
        await save_user_state(user_id, user_state)
        await turn_context.send_activity("¡Genial! Ahora puedo recomendarte eventos.")
        return

    # Manejo de evento pendiente
    if "evento_pendiente" in user_state:
        if user_text in ("sí", "si"):
            if not cosmos_available:
                await turn_context.send_activity(
                    "No puedo acceder a la base de datos en este momento."
                )
                user_state.pop("evento_pendiente")
                await save_user_state(user_id, user_state)
                return

            # Obtener evento de Cosmos DB
            try:
                evento = database.get_container_client("Eventos").read_item(
                    item=user_state["evento_pendiente"],
                    partition_key=user_state["evento_pendiente_sala"]
                )
            except CosmosHttpResponseError as e:
                logger.error(f"Error leyendo evento: {e}")
                await turn_context.send_activity("No pude recuperar el evento.")
                user_state.pop("evento_pendiente")
                await save_user_state(user_id, user_state)
                return

            # Agendar en calendario
            if graph_available:
                new_event = {
                    "subject": evento["nombre"],
                    "start": {"dateTime": evento["hora"], "timeZone": "UTC"},
                    "end": {"dateTime": evento.get("hora_fin", evento["hora"]), "timeZone": "UTC"},
                    "location": {"displayName": evento["sala"]}
                }
                try:
                    await graph_client.me.calendar.events.create(new_event)
                    await turn_context.send_activity("¡Evento agendado!")
                except Exception as e:
                    logger.error(f"Error en MS Graph: {e}")
                    await turn_context.send_activity("No pude agendar el evento.")
            else:
                await turn_context.send_activity(
                    f"Evento '{evento['nombre']}' registrado. Nota: Integración de calendario desactivada."
                )

            user_state.pop("evento_pendiente")
            await save_user_state(user_id, user_state)
            return

        elif user_text in ("no", "nop"):
            user_state.pop("evento_pendiente")
            await save_user_state(user_id, user_state)
            await turn_context.send_activity("Evento no agendado.")
            return

    # Flujo de recomendaciones
    if "recomienda" in user_text:
        if not cosmos_available:
            await turn_context.send_activity("Servicio de eventos no disponible.")
            return

        # Buscar eventos
        query = "SELECT * FROM Eventos e WHERE ARRAY_CONTAINS(@intereses, e.temas)"
        params = [{"name": "@intereses", "value": user_state["intereses"]}]
        
        try:
            eventos = list(database.get_container_client("Eventos").query_items(
                query=query,
                parameters=params,
                enable_cross_partition_query=True
            ))
        except CosmosHttpResponseError as e:
            logger.error(f"Error buscando eventos: {e}")
            await turn_context.send_activity("No pude buscar eventos.")
            return

        if eventos:
            evento = eventos[0]
            user_state.update({
                "evento_pendiente": evento["id"],
                "evento_pendiente_sala": evento["sala"]
            })
            await save_user_state(user_id, user_state)
            await turn_context.send_activity(
                f"Evento: {evento['nombre']} en {evento['sala']} a las {evento['hora']}. ¿Agendar? (sí/no)"
            )
        else:
            await turn_context.send_activity("No hay eventos disponibles.")
        return

    # Respuesta por defecto con OpenAI
    if openai_available:
        try:
            response = ai_client.chat.completions.create(
                model=AZURE_DEPLOYMENT_NAME,
                messages=[
                    {"role": "system", "content": "Eres un asistente útil."},
                    {"role": "user", "content": user_text}
                ],
                max_tokens=800,
                temperature=0.7
            )
            bot_reply = response.choices[0].message.content
        except Exception as e:
            logger.error(f"Error en OpenAI: {e}")
            bot_reply = "No pude procesar tu solicitud."
    else:
        bot_reply = "Estoy en modo limitado y no puedo responder esto."

    await turn_context.send_activity(bot_reply)

# Rutas Flask
@app.route("/api/messages", methods=["POST"])
def messages():
    if "application/json" not in request.headers.get("Content-Type", ""):
        return Response(status=415)
    
    body = request.json
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")
    
    task = adapter.process_activity(activity, auth_header, process_message)
    try:
        asyncio.run(task)
    except Exception as e:
        logger.error(f"Error procesando actividad: {e}")
        return Response(status=500)
    
    return Response(status=200)

@app.route("/", methods=["GET"])
def home():
    return json.dumps({
        "status": "running",
        "cosmos_db": "available" if cosmos_available else "unavailable",
        "msgraph": "available" if graph_available else "unavailable",
        "openai": "available" if openai_available else "unavailable"
    }), 200, {'Content-Type': 'application/json'}

if __name__ == "__main__":
    try:
        app.run(host='0.0.0.0', port=PORT)
    except Exception as ex:
        logger.error(f"Error al iniciar servidor: {ex}")
