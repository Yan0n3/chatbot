import os
import json
import asyncio
import logging
import traceback
from flask import Flask, request, Response
from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity, ActivityTypes
from openai import AzureOpenAI

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("AzureBot")

# Crear app Flask
app = Flask(__name__)
PORT = int(os.environ.get("PORT", 3978))

# Credenciales de Bot Framework
APP_ID = os.environ.get("MicrosoftAppId", "")
APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")

# Configuración Azure OpenAI
AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_DEPLOYMENT_NAME = os.environ.get("AZURE_DEPLOYMENT_NAME", "gpt-4.1")

# Cliente Azure OpenAI
ai_client = AzureOpenAI(
    api_key=AZURE_OPENAI_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version=AZURE_OPENAI_API_VERSION,
)

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
def process_message(turn_context: TurnContext):
    async def _inner():
        if turn_context.activity.type == ActivityTypes.message and turn_context.activity.text:
            user_text = turn_context.activity.text
            logger.info(f"Mensaje recibido: {user_text}")

            # Llamada a Azure OpenAI
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

            logger.info(f"Respuesta bot: {bot_reply}")
            await turn_context.send_activity(bot_reply)
    return _inner()

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

    # Ejecutar la actividad en evento async
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
    return "Bot está corriendo!", 200

# Iniciar servidor
if __name__ == "__main__":
    try:
        app.run(host='0.0.0.0', port=PORT)
    except Exception as ex:
        logger.error(f"Error al iniciar servidor: {ex}")
