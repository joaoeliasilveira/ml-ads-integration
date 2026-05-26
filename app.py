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
    access_token  = token_data.get("access_token", "Erro")
    refresh_token = token_data.get("refresh_token", "")
    user_id       = token_data.get("user_id", "")

    return f"""
        <h2>✅ Autorização concluída!</h2>
        <p><b>User ID:</b> {user_id}</p>
        <p><b>Access Token:</b> {access_token}</p>
        <p><b>Refresh Token:</b> {refresh_token}</p>
    """

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
