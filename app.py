from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity, ActivityTypes
from flask import Flask, request, Response
import os
import asyncio
import sys
import traceback

# App y configuración del adaptador
APP = Flask(__name__)
PORT = os.environ.get("PORT", 3978)

# Configuración del bot
APP_ID = os.environ.get("MicrosoftAppId", "315d9eaa-47c1-40e6-8348-28397241e393")
APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")

# Configurar el adaptador con las credenciales
SETTINGS = BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD)
ADAPTER = BotFrameworkAdapter(SETTINGS)

# Manejador de errores
async def on_error(context: TurnContext, error: Exception):
    print(f"\n [on_turn_error] No controlado: {error}", file=sys.stderr)
    traceback.print_exc()
    
    # Enviar mensaje de error al usuario
    await context.send_activity("Lo siento, parece que algo salió mal.")

ADAPTER.on_turn_error = on_error

# Escuchar solicitudes entrantes en /api/messages
@APP.route("/api/messages", methods=["POST"])
def messages():
    # Añadir logs para depuración
    print("Solicitud recibida en /api/messages")
    print(f"Headers: {request.headers}")
    
    if "application/json" in request.headers["Content-Type"]:
        body = request.json
        print(f"Body: {body}")
    else:
        print("Contenido no es JSON")
        return Response(status=415)

    # Procesar la actividad
    async def process_activity():
        # Crear un contexto para la actividad entrante
        activity = Activity().deserialize(body)
        
        # Procesar la actividad
        async def turn_call(turn_context):
            print(f"Tipo de actividad: {turn_context.activity.type}")
            
            # Responder si es un mensaje
            if turn_context.activity.type == ActivityTypes.message:
                message_text = turn_context.activity.text
                print(f"Mensaje recibido: {message_text}")
                
                # Respuesta simple para probar
                await turn_context.send_activity(f"Recibí: '{message_text}'")
                print("Respuesta enviada")
            else:
                print(f"Otro tipo de actividad: {turn_context.activity.type}")
        
        # Procesar la actividad con el adaptador
        await ADAPTER.process_activity(activity, "", turn_call)
        
        return "OK"

    # Ejecutar la función asíncrona
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(process_activity())
    finally:
        loop.close()
    
    return Response(status=200)

# Ruta principal para verificar que el servicio está funcionando
@APP.route("/", methods=["GET"])
def ping():
    return "Bot running!"

# Iniciar el servidor Flask
if __name__ == "__main__":
    try:
        APP.run(host='0.0.0.0', port=PORT)
    except Exception as ex:
        print(f"Error al iniciar el servidor: {ex}")
