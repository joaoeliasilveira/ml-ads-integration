from flask import Flask, request
import requests
import os

app = Flask(__name__)

CLIENT_ID     = os.environ.get("ML_CLIENT_ID")
CLIENT_SECRET = os.environ.get("ML_CLIENT_SECRET")
REDIRECT_URI  = os.environ.get("ML_REDIRECT_URI")

@app.route("/")
def home():
    auth_url = (
        f"https://auth.mercadolivre.com.br/authorization"
        f"?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
    )
    return f'<a href="{auth_url}">Clique aqui para autorizar o Mercado Livre</a>'

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Erro: código não recebido", 400

    response = requests.post(
        "https://api.mercadolibre.com/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI
        }
    )

    token_data = response.json()

    # Mostra a resposta completa para debug
    return f"""
        <h2>Resposta completa do ML:</h2>
        <pre>{token_data}</pre>
    """

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
