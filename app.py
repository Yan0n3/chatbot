import os
import json
from flask import Flask, request, jsonify, abort
from openai import AzureOpenAI
import jwt
import requests
from jwt.algorithms import RSAAlgorithm

app = Flask(__name__)

# 🔐 Configuración desde variables de entorno
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_DEPLOYMENT_NAME = os.getenv("AZURE_DEPLOYMENT_NAME", "gpt-4.1")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
MICROSOFT_APP_ID = os.getenv("MICROSOFT_APP_ID")

# 🎯 Cliente de Azure OpenAI
client = AzureOpenAI(
    api_key=AZURE_OPENAI_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version=AZURE_OPENAI_API_VERSION,
)

# 🔒 OpenID config para Bot Framework
MICROSOFT_OPENID_CONFIG = "https://login.botframework.com/v1/.well-known/openidconfiguration"
jwks_cache = {}

def get_microsoft_jwks():
    if not jwks_cache:
        openid_config = requests.get(MICROSOFT_OPENID_CONFIG).json()
        jwks_uri = openid_config["jwks_uri"]
        keys = requests.get(jwks_uri).json()["keys"]
        for key in keys:
            kid = key["kid"]
            jwks_cache[kid] = RSAAlgorithm.from_jwk(json.dumps(key))
    return jwks_cache

def validate_jwt_from_request():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        abort(401, "Missing or invalid Authorization header")

    token = auth_header.split(" ")[1]
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")
    jwks = get_microsoft_jwks()

    if kid not in jwks:
        abort(401, "Invalid token key")

    public_key = jwks[kid]

    try:
        decoded = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=MICROSOFT_APP_ID,
            issuer="https://api.botframework.com"
        )
        return decoded
    except jwt.ExpiredSignatureError:
        abort(401, "Token expired")
    except jwt.InvalidTokenError as e:
        abort(401, f"Invalid token: {str(e)}")

@app.route("/api/messages", methods=["POST"])
def chat():
    try:
        validate_jwt_from_request()  # ✅ Autenticación

        print("✅ Recibido POST de Web Chat")
        print("📦 JSON recibido:", request.json)
        
        data = request.json
        if data.get("type") != "message" or "text" not in data:
            return jsonify({"type": "message", "text": "No puedo procesar este tipo de mensaje."})

        user_input = data["text"]

        response = client.chat.completions.create(
            model=AZURE_DEPLOYMENT_NAME,
            messages=[
                {"role": "system", "content": "Eres un asistente útil."},
                {"role": "user", "content": user_input}
            ],
            max_tokens=800,
            temperature=1.0,
            top_p=1.0,
            frequency_penalty=0.0,
            presence_penalty=0.0
        )

        reply = response.choices[0].message.content

        print("📤 Enviando respuesta a Web Chat:", reply)

        return jsonify({
            "type": "message",
            "text": "Hola desde el bot en Render!",
            "from": {"id": "bot", "name": "Bot"}
            })


    except Exception as e:
        return jsonify({
            "type": "message",
            "text": f"Ocurrió un error: {str(e)}"
        }), 500

@app.route("/", methods=["GET"])
def home():
    return "Bot is running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
