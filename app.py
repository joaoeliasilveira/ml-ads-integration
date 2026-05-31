from flask import Flask, request, jsonify, render_template, redirect, session, abort
import requests
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone, timedelta
import hashlib
import json as json_lib
import functools

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "petina-dashboard-2026-secret")

CLIENT_ID     = os.environ.get("ML_CLIENT_ID")
CLIENT_SECRET = os.environ.get("ML_CLIENT_SECRET")
REDIRECT_URI  = os.environ.get("ML_REDIRECT_URI")
DATABASE_URL  = os.environ.get("DATABASE_URL")

TOKEN_TTL = timedelta(hours=5, minutes=30)

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sellers (
            user_id TEXT PRIMARY KEY,
            nickname TEXT,
            access_token TEXT,
            refresh_token TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    # Tabela de usuários do dashboard
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_users (
            username    TEXT PRIMARY KEY,
            password    TEXT NOT NULL,
            role        TEXT NOT NULL DEFAULT 'secondary',
            permissions JSONB DEFAULT '{}'::jsonb,
            created_at  TIMESTAMP DEFAULT NOW()
        )
    """)
    # Criar usuário master padrão se não existir
    cur.execute("SELECT COUNT(*) as cnt FROM dashboard_users WHERE role='master'")
    if cur.fetchone()['cnt'] == 0:
        import hashlib
        default_pwd = hashlib.sha256("petina2026".encode()).hexdigest()
        cur.execute(
            "INSERT INTO dashboard_users (username, password, role) VALUES (%s, %s, 'master') ON CONFLICT DO NOTHING",
            ("master", default_pwd)
        )
    conn.commit()
    cur.close()
    conn.close()

try:
    init_db()
except Exception as e:
    print("Erro ao inicializar banco: " + str(e))

# ─── AUTH HELPERS ─────────────────────────────────────────────────
def hash_pwd(pwd):
    return hashlib.sha256(pwd.encode()).hexdigest()

def get_current_user():
    return session.get("dash_user")

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("dash_user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

def master_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = session.get("dash_user")
        if not user or user.get("role") != "master":
            if request.path.startswith("/api/"):
                return jsonify({"error": "forbidden"}), 403
            return redirect("/")
        return f(*args, **kwargs)
    return decorated

def can_see_seller(user, seller_id):
    if not user: return False
    if user.get("role") == "master": return True
    perms = user.get("permissions", {})
    sellers = perms.get("sellers", [])
    return not sellers or str(seller_id) in [str(s) for s in sellers]

def can_see_tab(user, tab):
    if not user: return False
    if user.get("role") == "master": return True
    perms = user.get("permissions", {})
    tabs  = perms.get("tabs", [])
    return not tabs or tab in tabs

def check_seller_access(user_id):
    """Verifica se usuário logado tem acesso ao seller."""
    user = get_current_user()
    if not user:
        return False, (jsonify({"error": "unauthorized"}), 401)
    if not can_see_seller(user, user_id):
        return False, (jsonify({"error": "forbidden", "message": "Acesso nao autorizado a este seller"}), 403)
    return True, None

# ─── AUTH ROUTES ───────────────────────────────────────────────────
@app.route("/login", methods=["GET","POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","").strip()
        try:
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT * FROM dashboard_users WHERE username=%s", (username,))
            user = cur.fetchone()
            cur.close(); conn.close()
            if user and user["password"] == hash_pwd(password):
                session["dash_user"] = {
                    "username":    user["username"],
                    "role":        user["role"],
                    "permissions": user["permissions"] or {}
                }
                return redirect("/")
            error = "Usuário ou senha incorretos."
        except Exception as e:
            error = "Erro ao autenticar: " + str(e)
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ─── ADMIN ROUTES ──────────────────────────────────────────────────
@app.route("/admin")
@master_required
def admin():
    return render_template("admin.html")

@app.route("/api/admin/users", methods=["GET"])
@master_required
def admin_list_users():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT username, role, permissions, created_at FROM dashboard_users ORDER BY role, username")
    users = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(users)

@app.route("/api/admin/users", methods=["POST"])
@master_required
def admin_create_user():
    data = request.json or {}
    username = data.get("username","").strip()
    password = data.get("password","").strip()
    role     = data.get("role","secondary")
    perms    = data.get("permissions", {})
    if not username or not password:
        return jsonify({"error": "username e password obrigatorios"}), 400
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO dashboard_users (username, password, role, permissions) VALUES (%s,%s,%s,%s)",
            (username, hash_pwd(password), role, json_lib.dumps(perms))
        )
        conn.commit(); cur.close(); conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/admin/users/<username>", methods=["PUT"])
@master_required
def admin_update_user(username):
    data    = request.json or {}
    updates = []
    params  = []
    if "permissions" in data:
        updates.append("permissions=%s")
        params.append(json_lib.dumps(data["permissions"]))
    if data.get("password"):
        updates.append("password=%s")
        params.append(hash_pwd(data["password"]))
    if not updates:
        return jsonify({"ok": True})
    params.append(username)
    conn = get_db(); cur = conn.cursor()
    cur.execute(f"UPDATE dashboard_users SET {','.join(updates)} WHERE username=%s", params)
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/admin/users/<username>", methods=["DELETE"])
@master_required
def admin_delete_user(username):
    if username == "master":
        return jsonify({"error": "nao pode deletar o master"}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM dashboard_users WHERE username=%s", (username,))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/admin/me")
@login_required
def admin_me():
    return jsonify(session.get("dash_user", {}))

# ─── HOME ──────────────────────────────────────────────────────────
@app.route("/")
@login_required
def home():
    return render_template("index.html")

@app.route("/auth")
def auth():
    auth_url = (
        "https://auth.mercadolivre.com.br/authorization"
        "?response_type=code"
        "&client_id=" + CLIENT_ID +
        "&redirect_uri=" + REDIRECT_URI +
        "&scope=read_ads+offline_access"
    )
    return redirect(auth_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Erro: codigo nao recebido", 400

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
    access_token  = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    user_id       = str(token_data.get("user_id"))

    if not access_token:
        return "Erro ao obter token: " + str(token_data), 400

    user_resp = requests.get(
        "https://api.mercadolibre.com/users/" + user_id,
        headers={"Authorization": "Bearer " + access_token}
    )
    nickname = user_resp.json().get("nickname", "Seller " + user_id)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sellers (user_id, nickname, access_token, refresh_token, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET access_token = EXCLUDED.access_token,
            refresh_token = EXCLUDED.refresh_token,
            nickname = EXCLUDED.nickname,
            updated_at = NOW()
    """, (user_id, nickname, access_token, refresh_token))
    conn.commit()
    cur.close()
    conn.close()

    return redirect("/?seller_added=" + nickname)

@app.route("/api/sellers")
@login_required
def get_sellers():
    user = get_current_user()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, nickname FROM sellers ORDER BY nickname")
    all_s = [dict(s) for s in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify([s for s in all_s if can_see_seller(user, s["user_id"])])

@app.route("/api/sellers/<user_id>", methods=["DELETE"])
def delete_seller(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sellers WHERE user_id = %s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True})

def refresh_token_if_needed(user_id, access_token, refresh_token, updated_at):
    now = datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    if now - updated_at < TOKEN_TTL:
        return access_token
    resp = requests.post(
        "https://api.mercadolibre.com/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token
        }
    )
    new_tokens = resp.json()
    new_access  = new_tokens.get("access_token", access_token)
    new_refresh = new_tokens.get("refresh_token", refresh_token)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE sellers SET access_token=%s, refresh_token=%s, updated_at=NOW()
        WHERE user_id=%s
    """, (new_access, new_refresh, user_id))
    conn.commit()
    cur.close()
    conn.close()
    return new_access

def get_seller_token(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sellers WHERE user_id = %s", (user_id,))
    seller = cur.fetchone()
    cur.close()
    conn.close()
    if not seller:
        return None, None
    token = refresh_token_if_needed(user_id, seller["access_token"], seller["refresh_token"], seller["updated_at"])
    return token, seller

def get_advertiser_id(user_id, token):
    resp = requests.get(
        "https://api.mercadolibre.com/advertising/advertisers",
        params={"product_id": "PADS"},
        headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
    )
    if not resp.ok:
        return None
    data = resp.json()
    advertisers = data if isinstance(data, list) else data.get("advertisers", data.get("results", []))
    if advertisers and len(advertisers) > 0:
        return str(advertisers[0].get("id", advertisers[0].get("advertiser_id", "")))
    return None

def get_campaigns(user_id, token):
    advertiser_id = get_advertiser_id(user_id, token)
    aid = advertiser_id if advertiser_id else user_id

    endpoints = [
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/campaigns",
    ]
    for url in endpoints:
        resp = requests.get(url, headers={"Authorization": "Bearer " + token, "Api-Version": "1"})
        try:
            data = resp.json()
        except Exception:
            continue
        if not resp.ok:
            print("[ADS][ERRO] GET " + url + " HTTP " + str(resp.status_code))
            continue
        if isinstance(data, list):
            return data, url, aid
        if isinstance(data, dict) and "results" in data:
            return data["results"], url, aid
    return [], None, aid

def get_campaign_metrics(advertiser_id, camp_id, token, date_from, date_to, base_url):
    url = "https://api.mercadolibre.com/advertising/MLB/product_ads/campaigns/" + str(camp_id)
    resp = requests.get(url, params={
        "date_from": date_from,
        "date_to": date_to,
        "metrics": "clicks,prints,cost,direct_amount,indirect_amount,total_amount"
    }, headers={"Authorization": "Bearer " + token, "Api-Version": "2"})
    if not resp.ok:
        print("[METRICS][ERRO] " + url + " HTTP " + str(resp.status_code) + " " + resp.text[:200])
        return {}
    try:
        return resp.json()
    except Exception:
        return {}

@app.route("/api/ads/<user_id>")
def get_ads(user_id):
    ok, err = check_seller_access(user_id)
    if not ok: return err
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")
    campaigns, base_url, aid = get_campaigns(user_id, token)

    result = []
    total_spend = total_revenue = total_clicks = total_impressions = 0

    for camp in campaigns[:10]:
        camp_id = camp.get("id")
        metrics = get_campaign_metrics(aid, camp_id, token, date_from, date_to, base_url or "")

        # Formato: response tem objeto "metrics" dentro
        if isinstance(metrics, dict) and "metrics" in metrics:
            m = metrics["metrics"]
        elif isinstance(metrics, list) and len(metrics) > 0:
            m = metrics[0].get("metrics", metrics[0])
        else:
            m = metrics if isinstance(metrics, dict) else {}

        spend   = m.get("cost", 0)
        revenue = m.get("total_amount", m.get("direct_amount", m.get("revenue", 0)))
        clicks  = m.get("clicks", 0)
        imps    = m.get("prints", m.get("impressions", 0))

        roas = round(revenue / spend, 2) if spend > 0 else 0
        acos = round((spend / revenue) * 100, 1) if revenue > 0 else 0
        total_spend += spend
        total_revenue += revenue
        total_clicks += clicks
        total_impressions += imps
        result.append({
            "id": camp_id,
            "name": camp.get("name", "Campanha " + str(camp_id)),
            "status": camp.get("status", "unknown"),
            "spend": round(spend, 2),
            "revenue": round(revenue, 2),
            "clicks": clicks,
            "impressions": imps,
            "roas": roas,
            "acos": acos
        })

    return jsonify({
        "seller_id": user_id,
        "nickname": seller["nickname"],
        "summary": {
            "spend": round(total_spend, 2),
            "revenue": round(total_revenue, 2),
            "clicks": total_clicks,
            "impressions": total_impressions,
            "roas": round(total_revenue / total_spend, 2) if total_spend > 0 else 0,
            "acos": round((total_spend / total_revenue) * 100, 1) if total_revenue > 0 else 0
        },
        "campaigns": result
    })

@app.route("/api/ads/<user_id>/daily")
def get_ads_daily(user_id):
    ok, err = check_seller_access(user_id)
    if not ok: return err
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")
    campaigns, base_url, aid = get_campaigns(user_id, token)
    daily_map = {}

    for camp in campaigns[:10]:
        camp_id = camp.get("id")
        metrics = get_campaign_metrics(aid, camp_id, token, date_from, date_to, base_url or "")
        if isinstance(metrics, list):
            for day in metrics:
                date = day.get("date", "")
                if not date:
                    continue
                if date not in daily_map:
                    daily_map[date] = {"date": date, "cost": 0, "revenue": 0, "clicks": 0, "impressions": 0}
                daily_map[date]["cost"]        += day.get("cost", 0)
                daily_map[date]["revenue"]     += day.get("revenue", 0)
                daily_map[date]["clicks"]      += day.get("clicks", 0)
                daily_map[date]["impressions"] += day.get("impressions", 0)

    days_list = sorted(daily_map.values(), key=lambda x: x["date"])
    for d in days_list:
        d["cost"]    = round(d["cost"], 2)
        d["revenue"] = round(d["revenue"], 2)

    return jsonify({
        "seller_id": user_id,
        "nickname": seller["nickname"],
        "date_from": date_from,
        "date_to": date_to,
        "days": days_list
    })

@app.route("/api/reputation/<user_id>")
def get_reputation(user_id):
    ok, err = check_seller_access(user_id)
    if not ok: return err
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    rep_resp = requests.get(
        "https://api.mercadolibre.com/users/" + user_id,
        headers={"Authorization": "Bearer " + token}
    )
    rep_data = rep_resp.json() if rep_resp.ok else {}
    reputation = rep_data.get("seller_reputation", {})
    metrics_rep = reputation.get("metrics", {})
    transactions = reputation.get("transactions", {})

    return jsonify({
        "seller_id": user_id,
        "nickname": seller["nickname"],
        "reputation": {
            "level": reputation.get("level_id", ""),
            "power_seller": rep_data.get("power_seller_status", ""),
            "cancellations": metrics_rep.get("cancellations", {}).get("rate", 0),
            "claims": metrics_rep.get("claims", {}).get("rate", 0),
            "delayed": metrics_rep.get("delayed_handling_time", {}).get("rate", 0),
            "sales_completed": transactions.get("completed", 0),
            "ratings": reputation.get("ratings", {})
        }
    })

@app.route("/api/sales/<user_id>")
def get_sales(user_id):
    ok, err = check_seller_access(user_id)
    if not ok: return err
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")

    from datetime import date as ddate, timedelta as tdelta

    # Status que o ML considera como "Quantidade de vendas" no painel de Negocios
    # Removidos cancelled e pending_cancel pois o ML nao os conta como vendas
    SALE_STATUSES = {"confirmed", "payment_required", "payment_in_process", "paid", "partially_refunded", "partially_paid", "pending_cancel"}

    def fetch_all_orders(uid, tok, dfrom, dto, max_pages=200):
        """Busca todos os pedidos sem filtro de status e separa localmente."""
        all_orders = []
        offset = 0
        limit = 50
        total = None
        while True:
            r = requests.get(
                "https://api.mercadolibre.com/orders/search",
                params={
                    "seller": uid,
                    "order.date_created.from": dfrom + "T00:00:00.000-00:00",
                    "order.date_created.to":   dto + "T23:59:59.000-00:00",
                    "limit": limit,
                    "offset": offset,
                    "sort": "date_asc"
                },
                headers={"Authorization": "Bearer " + tok}
            )
            if not r.ok or not r.text:
                break
            data = r.json()
            results = data.get("results", [])
            if total is None:
                total = data.get("paging", {}).get("total", 0)
            all_orders.extend(results)
            offset += limit
            if offset >= (total or 0) or not results:
                break
        # Filtra localmente por status
        sale_orders = [o for o in all_orders if o.get("status") in SALE_STATUSES]
        return sale_orders, len(sale_orders)

    # Periodo atual
    orders, _ = fetch_all_orders(user_id, token, date_from, date_to)
    # Valor de uma order (total_amount como campo principal)
    def order_value(o):
        val = o.get("total_amount") or 0
        if val:
            return val
        payments = o.get("payments", [])
        if payments:
            return sum(p.get("total_paid_amount", 0) or 0 for p in payments)
        return o.get("paid_amount") or 0

    # Qtd vendas: agrupa por pack_id (pack com N itens = 1 venda)
    def count_sales(order_list):
        packs_seen = set()
        count = 0
        for o in order_list:
            pack_id = o.get("pack_id")
            if pack_id:
                if pack_id not in packs_seen:
                    packs_seen.add(pack_id)
                    count += 1
            else:
                count += 1
        return count

    # Vendas brutas: soma agrupando packs para evitar dupla contagem
    def sum_gmv(order_list):
        packs_seen = set()
        total = 0
        for o in order_list:
            pack_id = o.get("pack_id")
            if pack_id:
                if pack_id not in packs_seen:
                    packs_seen.add(pack_id)
                    pack_orders = [x for x in order_list if x.get("pack_id") == pack_id]
                    total += sum(order_value(x) for x in pack_orders)
            else:
                total += order_value(o)
        return total

    total_orders = count_sales(orders)
    gmv = sum_gmv(orders)

    # Periodo anterior
    d_from = ddate.fromisoformat(date_from)
    d_to   = ddate.fromisoformat(date_to)
    delta  = (d_to - d_from).days + 1
    prev_from = (d_from - tdelta(days=delta)).isoformat()
    prev_to   = (d_from - tdelta(days=1)).isoformat()
    prev_orders, _ = fetch_all_orders(user_id, token, prev_from, prev_to)
    prev_total = count_sales(prev_orders)
    prev_gmv = sum_gmv(prev_orders)

    # Visitas — endpoint time_window (correto para apps de terceiros)
    try:
        from datetime import date as _ddate
        _d_from = _ddate.fromisoformat(date_from)
        _d_to   = _ddate.fromisoformat(date_to)
        _days   = (_d_to - _d_from).days + 1
        visits_resp = requests.get(
            "https://api.mercadolibre.com/users/" + user_id + "/items_visits/time_window",
            params={"last": _days, "unit": "day", "ending": date_to},
            headers={"Authorization": "Bearer " + token}
        )
        visits_data  = visits_resp.json() if visits_resp.ok and visits_resp.text else {}
        total_visits = visits_data.get("total_visits", visits_data.get("visits", 0))
    except Exception:
        total_visits = 0

    # Vendas diarias
    daily_map = {}
    for o in orders:
        day = o.get("date_created", "")[:10]
        if not day:
            continue
        if day not in daily_map:
            daily_map[day] = {"date": day, "gmv": 0, "orders": 0}
        daily_map[day]["gmv"]    += order_value(o)
        daily_map[day]["orders"] += 1
    daily_sales = sorted(daily_map.values(), key=lambda x: x["date"])
    for d in daily_sales:
        d["gmv"] = round(d["gmv"], 2)

    # Ranking de produtos — começa pelos pedidos do período
    products_map = {}
    for o in orders:
        for item in o.get("order_items", []):
            title   = item.get("item", {}).get("title", "Produto")
            item_id = item.get("item", {}).get("id", "")
            qty     = item.get("quantity", 1)
            price   = item.get("unit_price", 0)
            key     = item_id or title
            if key not in products_map:
                products_map[key] = {"title": title, "item_id": item_id, "qty": 0, "revenue": 0, "ads_qty": 0, "status": "active"}
            products_map[key]["qty"]     += qty
            products_map[key]["revenue"] += qty * price

    # Busca TODOS os anúncios ativos do seller para incluir produtos sem venda no período
    try:
        all_item_ids = []
        scroll_id = None
        for _ in range(20):  # máx 20 páginas = 2000 itens
            params = {"limit": 100}
            if scroll_id:
                params["scroll_id"] = scroll_id
            r_items = requests.get(
                f"https://api.mercadolibre.com/users/{user_id}/items/search",
                params=params,
                headers={"Authorization": "Bearer " + token},
                timeout=10
            )
            if not r_items.ok:
                break
            items_data = r_items.json()
            batch = items_data.get("results", [])
            if not batch:
                break
            all_item_ids.extend(batch)
            scroll_id = items_data.get("scroll_id")
            if not scroll_id or len(batch) < 100:
                break

        # Busca dados de TODOS os itens (inclusive os que já têm vendas, para pegar stock)
        # Adiciona também itens do products_map que vieram dos pedidos
        all_ids_to_fetch = list(set(all_item_ids) | set(k for k in products_map if k.startswith('MLB')))
        for i in range(0, len(all_ids_to_fetch), 20):
            chunk = all_ids_to_fetch[i:i+20]
            r_t = requests.get(
                "https://api.mercadolibre.com/items",
                params={"ids": ",".join(chunk), "attributes": "id,title,price,status,available_quantity,shipping,fulfillment,catalog_product_id"},
                headers={"Authorization": "Bearer " + token},
                timeout=10
            )
            if not r_t.ok:
                continue
            for entry in r_t.json():
                body = entry.get("body", {})
                iid  = str(body.get("id", ""))
                _ff       = body.get("fulfillment") or {}
                _sh       = body.get("shipping") or {}
                is_full   = bool(
                    _ff.get("fulfillment_id") or
                    _ff.get("status") == "active" or
                    _sh.get("fulfillment") or
                    _sh.get("logistic_type") in ("fulfillment", "meli_fulfillment", "self_service_do")
                )
                avail_qty = body.get("available_quantity", 0) or 0
                catalog_id = body.get("catalog_product_id") or ""
                if iid and iid not in products_map:
                    products_map[iid] = {
                        "title":              body.get("title", iid),
                        "item_id":            iid,
                        "qty":                0,
                        "revenue":            0,
                        "ads_qty":            0,
                        "status":             body.get("status", "active"),
                        "stock_total":        avail_qty,
                        "is_full":            is_full,
                        "catalog_product_id": catalog_id,
                    }
                else:
                    if not products_map[iid].get("status"):
                        products_map[iid]["status"] = body.get("status", "active")
                    products_map[iid]["stock_total"]        = avail_qty
                    products_map[iid]["is_full"]            = is_full
                    products_map[iid]["catalog_product_id"] = catalog_id
    except Exception as e:
        print(f"[ITEMS] Erro ao buscar todos os anúncios: {e}")

    # Busca unidades vendidas via ADS por item (direct + indirect units)
    try:
        campaigns, base_url, aid = get_campaigns(user_id, token)
        ads_units_by_item = {}  # item_id -> ads_qty
        for camp in campaigns[:10]:
            camp_id = camp.get("id")
            # Busca itens da campanha com métricas de unidades
            r_items = requests.get(
                f"https://api.mercadolibre.com/advertising/MLB/product_ads/campaigns/{camp_id}/items",
                params={
                    "date_from": date_from,
                    "date_to":   date_to,
                    "metrics":   "direct_units,indirect_units",
                    "limit":     100
                },
                headers={"Authorization": "Bearer " + token, "Api-Version": "2"},
                timeout=8
            )
            if not r_items.ok:
                continue
            items_data = r_items.json()
            items_list = items_data if isinstance(items_data, list) else items_data.get("results", items_data.get("items", []))
            for it in items_list:
                iid = str(it.get("item_id", it.get("id", "")))
                if not iid:
                    continue
                m = it.get("metrics", it) if isinstance(it.get("metrics"), dict) else it
                direct   = m.get("direct_units", 0) or 0
                indirect = m.get("indirect_units", 0) or 0
                ads_units_by_item[iid] = ads_units_by_item.get(iid, 0) + direct + indirect
    except Exception:
        ads_units_by_item = {}

    # Aplica ads_qty no products_map
    for key, p in products_map.items():
        iid = p.get("item_id", "")
        ads_q = ads_units_by_item.get(str(iid), 0)
        p["ads_qty"]     = min(ads_q, p["qty"])  # nunca pode ser maior que o total
        p["organic_qty"] = p["qty"] - p["ads_qty"]

    # Busca vendas dos últimos 30 dias fixos para projeção de estoque
    try:
        today_dt   = ddate.fromisoformat(date_to)
        proj_from  = (today_dt - tdelta(days=29)).isoformat()
        proj_orders, _ = fetch_all_orders(user_id, token, proj_from, date_to)
        proj_map = {}
        for o in proj_orders:
            if o.get("status") not in SALE_STATUSES:
                continue
            for oi in o.get("order_items", []):
                iid = str(oi.get("item", {}).get("id", ""))
                if iid:
                    proj_map[iid] = proj_map.get(iid, 0) + oi.get("quantity", 1)
    except Exception:
        proj_map = {}

    # Calcula projeção de dias de estoque sempre com base em 30 dias
    for p in products_map.values():
        stock      = p.get("stock_total", 0) or 0
        iid        = p.get("item_id", "")
        qty_30d    = proj_map.get(str(iid), 0) if iid else 0
        daily_rate = qty_30d / 30 if qty_30d > 0 else 0
        p["daily_rate"] = round(daily_rate, 2)
        p["days_stock"] = round(stock / daily_rate) if daily_rate > 0 else None

    top_products = sorted(products_map.values(), key=lambda x: x["revenue"], reverse=True)
    for p in top_products:
        p["revenue"] = round(p["revenue"], 2)

    # Cancelamentos — busca separado com status=cancelled
    r_cancelled = requests.get(
        "https://api.mercadolibre.com/orders/search",
        params={
            "seller": user_id,
            "order.date_created.from": date_from + "T00:00:00.000-00:00",
            "order.date_created.to":   date_to + "T23:59:59.000-00:00",
            "order.status": "cancelled",
            "limit": 1,
            "offset": 0
        },
        headers={"Authorization": "Bearer " + token}
    )
    if r_cancelled.ok and r_cancelled.text:
        cancelled_data  = r_cancelled.json()
        total_cancelled = cancelled_data.get("paging", {}).get("total", 0)
        # Busca valor dos cancelados (primeiros 50)
        r_cancelled_val = requests.get(
            "https://api.mercadolibre.com/orders/search",
            params={
                "seller": user_id,
                "order.date_created.from": date_from + "T00:00:00.000-00:00",
                "order.date_created.to":   date_to + "T23:59:59.000-00:00",
                "order.status": "cancelled",
                "limit": 50
            },
            headers={"Authorization": "Bearer " + token}
        )
        cancelled_results = r_cancelled_val.json().get("results", []) if r_cancelled_val.ok and r_cancelled_val.text else []
        # ML conta apenas cancelamentos pos-pagamento (que tiveram valor pago)
        paid_cancelled = [o for o in cancelled_results if order_value(o) > 0]
        total_cancelled = len(paid_cancelled)
        value_cancelled = sum(order_value(o) for o in paid_cancelled)
    else:
        total_cancelled = 0
        value_cancelled = 0

    # Unidades vendidas e preco medio por unidade
    total_units = 0
    for o in orders:
        for item in o.get("order_items", []):
            total_units += item.get("quantity", 1)

    prev_units = 0
    for o in prev_orders:
        for item in o.get("order_items", []):
            prev_units += item.get("quantity", 1)

    avg_price_per_unit = round(gmv / total_units, 2) if total_units > 0 else 0
    prev_avg_unit      = round(prev_gmv / prev_units, 2) if prev_units > 0 else 0

    # Comparativo
    def var(curr, prev):
        if prev > 0:
            return round(((curr - prev) / prev * 100), 1)
        return 0

    return jsonify({
        "seller_id": user_id,
        "nickname": seller["nickname"],
        "period": {"date_from": date_from, "date_to": date_to},
        "summary": {
            "vendas_brutas":       round(gmv, 2),
            "unidades_vendidas":   total_units,
            "preco_medio_unidade": avg_price_per_unit,
            "qtd_vendas":          total_orders,
            "preco_medio_venda":   round(gmv / total_orders, 2) if total_orders > 0 else 0,
            "total_visits":        total_visits,
            "conversion":          round((total_orders / total_visits) * 100, 2) if total_visits > 0 else 0,
            "qtd_canceladas":      total_cancelled,
            "valor_canceladas":    round(value_cancelled, 2),
        },
        "comparison": {
            "prev_period":        {"date_from": prev_from, "date_to": prev_to},
            "prev_vendas_brutas": round(prev_gmv, 2),
            "prev_qtd_vendas":    prev_total,
            "prev_unidades":      prev_units,
            "prev_avg_unit":      prev_avg_unit,
            "var_vendas_brutas":  var(gmv, prev_gmv),
            "var_qtd_vendas":     var(total_orders, prev_total),
            "var_unidades":       var(total_units, prev_units),
            "var_avg_unit":       var(avg_price_per_unit, prev_avg_unit),
            "var_conversion":     var(
                round((total_orders / total_visits) * 100, 2) if total_visits > 0 else 0,
                0
            )
        },
        "daily_sales":  daily_sales,
        "top_products": top_products
    })

@app.route("/api/promotions/<user_id>")
def get_promotions(user_id):
    ok, err = check_seller_access(user_id)
    if not ok: return err
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    promotions = []
    status_info = {}

    # Endpoint correto conforme documentacao ML: app_version=v2
    r1 = requests.get(
        "https://api.mercadolibre.com/seller-promotions/users/" + user_id,
        params={"app_version": "v2"},
        headers={"Authorization": "Bearer " + token}
    )
    status_info["seller_promotions_v2"] = r1.status_code
    if r1.ok and r1.text:
        try:
            d = r1.json()
            raw = d if isinstance(d, list) else d.get("results", d.get("promotions", []))
            # Filtra apenas promos ativas
            promotions = [p for p in raw if p.get("status") in ("started", "active", "candidate")]
            if not promotions:
                promotions = raw  # mostra todas se nenhuma ativa
        except Exception:
            pass

    # Fallback: sem app_version
    if not promotions:
        r2 = requests.get(
            "https://api.mercadolibre.com/seller-promotions/users/" + user_id + "/promotions",
            params={"app_version": "v2"},
            headers={"Authorization": "Bearer " + token}
        )
        status_info["seller_promotions_path_v2"] = r2.status_code
        if r2.ok and r2.text:
            try:
                d = r2.json()
                promotions = d if isinstance(d, list) else d.get("results", d.get("promotions", []))
            except Exception:
                pass

    result = []
    for p in promotions[:20]:
        result.append({
            "id": str(p.get("id", p.get("deal_id", ""))),
            "name": p.get("name", p.get("description", p.get("deal_print_id", "Promocao"))),
            "type": p.get("type", p.get("deal_type", p.get("promotion_type", ""))),
            "status": p.get("status", ""),
            "discount": p.get("value", p.get("discount_meli_amount", p.get("percent_off", 0))),
            "start_date": str(p.get("start_date", p.get("from_date", p.get("from", ""))))[:10],
            "end_date": str(p.get("finish_date", p.get("to_date", p.get("to", ""))))[:10],
            "items_count": p.get("items_count", p.get("affected_items", 0))
        })

    return jsonify({
        "seller_id": user_id,
        "nickname": seller["nickname"],
        "total": len(result),
        "promotions": result,
        "status_info": status_info
    })

@app.route("/api/metrics/<user_id>")
def get_metrics(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    date_from = request.args.get("date_from", "2026-05-01")
    date_to   = request.args.get("date_to",   "2026-05-26")

    rep_resp = requests.get(
        "https://api.mercadolibre.com/users/" + user_id,
        headers={"Authorization": "Bearer " + token}
    )
    rep_data = rep_resp.json() if rep_resp.ok else {}
    reputation = rep_data.get("seller_reputation", {})
    metrics_rep = reputation.get("metrics", {})
    transactions = reputation.get("transactions", {})

    sales_resp = requests.get(
        "https://api.mercadolibre.com/orders/search",
        params={
            "seller": user_id,
            "order.date_created.from": date_from + "T00:00:00.000-00:00",
            "order.date_created.to":   date_to + "T23:59:59.000-00:00",
            "order.status": "paid",
            "limit": 50
        },
        headers={"Authorization": "Bearer " + token}
    )
    sales_data   = sales_resp.json() if sales_resp.ok else {}
    orders       = sales_data.get("results", [])
    total_orders = sales_data.get("paging", {}).get("total", 0)
    gmv          = sum(o.get("total_amount", 0) for o in orders)

    visits_resp = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/items_visits",
        params={"date_from": date_from, "date_to": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    visits_data  = visits_resp.json() if visits_resp.ok else {}
    total_visits = visits_data.get("total_visits", 0)

    return jsonify({
        "seller_id": user_id,
        "nickname": seller["nickname"],
        "period": {"date_from": date_from, "date_to": date_to},
        "sales": {
            "total_orders": total_orders,
            "gmv": round(gmv, 2),
            "avg_ticket": round(gmv / total_orders, 2) if total_orders > 0 else 0
        },
        "visits": {
            "total": total_visits,
            "conversion": round((total_orders / total_visits) * 100, 2) if total_visits > 0 else 0
        },
        "reputation": {
            "level": reputation.get("level_id", ""),
            "power_seller": rep_data.get("power_seller_status", ""),
            "cancellations": metrics_rep.get("cancellations", {}).get("rate", 0),
            "claims": metrics_rep.get("claims", {}).get("rate", 0),
            "delayed": metrics_rep.get("delayed_handling_time", {}).get("rate", 0),
            "sales_completed": transactions.get("completed", 0),
            "ratings": reputation.get("ratings", {})
        }
    })

@app.route("/api/debug-advertisers/<user_id>")
def debug_advertisers(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    results = {}
    for product_id in ["PADS", "SPA", "PDA", "DISPLAY"]:
        r = requests.get(
            "https://api.mercadolibre.com/advertising/advertisers",
            params={"product_id": product_id},
            headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
        )
        results[product_id] = {"status": r.status_code, "response": r.json() if r.text else {}}

    r_all = requests.get(
        "https://api.mercadolibre.com/advertising/advertisers",
        headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
    )
    results["sem_product_id"] = {"status": r_all.status_code, "response": r_all.json()}

    return jsonify({
        "user_id": user_id,
        "nickname": seller["nickname"],
        "advertiser_lookup": results
    })

@app.route("/api/debug/<user_id>")
def debug_ads(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    advertiser_id = get_advertiser_id(user_id, token)
    aid = advertiser_id if advertiser_id else user_id

    urls = [
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/campaigns",
        "https://api.mercadolibre.com/advertising/advertisers/" + user_id + "/product_ads/campaigns",
        "https://api.mercadolibre.com/advertising/advertisers/" + user_id + "/campaigns",
    ]
    results = {}
    for url in urls:
        r = requests.get(url, headers={"Authorization": "Bearer " + token, "Api-Version": "1"})
        results[url] = {"status": r.status_code, "response": r.json() if r.text else {}}

    return jsonify({
        "user_id": user_id,
        "advertiser_id_found": advertiser_id,
        "endpoints_tested": results
    })

@app.route("/api/debug-metrics/<user_id>")
def debug_metrics(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    r_orders = requests.get(
        "https://api.mercadolibre.com/orders/search",
        params={"seller": user_id, "order.status": "paid", "limit": 1},
        headers={"Authorization": "Bearer " + token}
    )
    r_visits = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/items_visits",
        params={"date_from": "2026-05-01", "date_to": "2026-05-26"},
        headers={"Authorization": "Bearer " + token}
    )
    r_rep = requests.get(
        "https://api.mercadolibre.com/users/" + user_id,
        headers={"Authorization": "Bearer " + token}
    )
    r_promos1 = requests.get(
        "https://api.mercadolibre.com/promotions/search",
        params={"seller_id": user_id, "type": "DEAL", "status": "started"},
        headers={"Authorization": "Bearer " + token}
    )
    r_promos2 = requests.get(
        "https://api.mercadolibre.com/seller-promotions/users/" + user_id + "/promotions",
        headers={"Authorization": "Bearer " + token}
    )
    r_promos3 = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/deals",
        headers={"Authorization": "Bearer " + token}
    )

    def safe_json(r):
        try:
            return r.json() if r.text and r.text.strip() else {}
        except Exception:
            return {"raw": r.text[:200] if r.text else ""}

    return jsonify({
        "orders":           {"status": r_orders.status_code,  "response": safe_json(r_orders)},
        "visits":           {"status": r_visits.status_code,  "response": safe_json(r_visits)},
        "reputation":       {"status": r_rep.status_code,     "response": safe_json(r_rep)},
        "promotions_search":{"status": r_promos1.status_code, "response": safe_json(r_promos1)},
        "seller_promotions":{"status": r_promos2.status_code, "response": safe_json(r_promos2)},
        "deals":            {"status": r_promos3.status_code, "response": safe_json(r_promos3)}
    })


@app.route("/api/debug-campaign/<user_id>/<camp_id>")
def debug_campaign(user_id, camp_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    advertiser_id = get_advertiser_id(user_id, token)
    aid = advertiser_id if advertiser_id else user_id

    date_from = request.args.get("date_from", "2026-04-01")
    date_to   = request.args.get("date_to",   "2026-05-26")

    results = {}
    tests = [
        ("MLB_v2_metrics", "https://api.mercadolibre.com/advertising/MLB/product_ads/campaigns/" + camp_id, {"date_from": date_from, "date_to": date_to, "metrics": "clicks,prints,cost,direct_amount,indirect_amount,total_amount"}, "2"),
        ("MLB_v1", "https://api.mercadolibre.com/advertising/MLB/product_ads/campaigns/" + camp_id, {"date_from": date_from, "date_to": date_to}, "1"),
        ("aid_v2", "https://api.mercadolibre.com/advertising/" + aid + "/product_ads/campaigns/" + camp_id, {"date_from": date_from, "date_to": date_to, "metrics": "clicks,prints,cost,direct_amount,indirect_amount,total_amount"}, "2"),
        ("aid_campaigns_v2", "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns/" + camp_id, {"date_from": date_from, "date_to": date_to, "metrics": "clicks,prints,cost,direct_amount,indirect_amount,total_amount"}, "2"),
        ("MLB_no_params", "https://api.mercadolibre.com/advertising/MLB/product_ads/campaigns/" + camp_id, {}, "2"),
    ]
    for name, url, params, version in tests:
        r = requests.get(url, params=params, headers={"Authorization": "Bearer " + token, "Api-Version": version})
        try:
            results[name] = {"status": r.status_code, "url": url, "response": r.json()}
        except Exception:
            results[name] = {"status": r.status_code, "url": url, "response": r.text[:300]}

    return jsonify({
        "user_id": user_id,
        "advertiser_id": aid,
        "camp_id": camp_id,
        "results": results
    })


@app.route("/api/debug-camp-detail/<user_id>")
def debug_camp_detail(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    advertiser_id = get_advertiser_id(user_id, token)
    aid = advertiser_id if advertiser_id else user_id

    # Busca campanhas e retorna JSON completo
    r = requests.get(
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns",
        headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
    )
    try:
        camp_data = r.json()
    except Exception:
        camp_data = r.text[:500]

    # Testa endpoint de summary de metricas do advertiser
    r2 = requests.get(
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns/summary",
        params={"date_from": "2026-04-01", "date_to": "2026-05-26"},
        headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
    )
    try:
        summary_data = r2.json()
    except Exception:
        summary_data = r2.text[:500]

    # Testa endpoint de reports
    r3 = requests.get(
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/reports",
        params={"date_from": "2026-04-01", "date_to": "2026-05-26"},
        headers={"Authorization": "Bearer " + token, "Api-Version": "1"}
    )
    try:
        reports_data = r3.json()
    except Exception:
        reports_data = r3.text[:500]

    return jsonify({
        "advertiser_id": aid,
        "campaigns_raw": camp_data,
        "summary_endpoint": {"status": r2.status_code, "response": summary_data},
        "reports_endpoint": {"status": r3.status_code, "response": reports_data}
    })


@app.route("/api/debug-metrics2/<user_id>")
def debug_metrics2(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    advertiser_id = get_advertiser_id(user_id, token)
    aid = advertiser_id if advertiser_id else user_id
    camp_id = "357533684"
    sf_id = "14"
    date_from = "2026-04-01"
    date_to = "2026-05-26"

    urls = [
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns/" + camp_id + "/metrics/days",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns/" + camp_id + "/daily_metrics",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/metrics",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/metrics",
        "https://api.mercadolibre.com/advertising/advertisers/" + aid + "/product_ads/campaigns/" + camp_id + "/clicks",
        "https://api.mercadolibre.com/advertising/" + aid + "/campaigns/" + camp_id + "/metrics",
        "https://api.mercadolibre.com/pads/advertisers/" + aid + "/campaigns/" + camp_id + "/metrics",
    ]

    results = {}
    for url in urls:
        r = requests.get(url, params={"date_from": date_from, "date_to": date_to}, headers={"Authorization": "Bearer " + token, "Api-Version": "1"})
        try:
            results[url] = {"status": r.status_code, "response": r.json()}
        except Exception:
            results[url] = {"status": r.status_code, "response": r.text[:300]}

    return jsonify({"advertiser_id": aid, "camp_id": camp_id, "results": results})


@app.route("/api/debug-sales/<user_id>")
def debug_sales(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    from datetime import date as ddate, timedelta as tdelta
    today = ddate.today()
    date_from = (today - tdelta(days=7)).isoformat()
    date_to   = today.isoformat()

    # Testa diferentes status
    results = {}
    for status in ["paid", "cancelled", "delivered", "confirmed", "payment_required", "payment_in_process"]:
        r = requests.get(
            "https://api.mercadolibre.com/orders/search",
            params={
                "seller": user_id,
                "order.date_created.from": date_from + "T00:00:00.000-00:00",
                "order.date_created.to":   date_to + "T23:59:59.000-00:00",
                "order.status": status,
                "limit": 1,
                "offset": 0
            },
            headers={"Authorization": "Bearer " + token}
        )
        if r.ok and r.text:
            d = r.json()
            results[status] = {
                "total": d.get("paging", {}).get("total", 0),
                "status": r.status_code
            }
        else:
            results[status] = {"total": 0, "status": r.status_code}

    # Busca paid com paginacao e mostra totais
    all_paid = []
    offset = 0
    limit = 50
    total_api = 0
    pages = []
    while offset <= 500:
        r = requests.get(
            "https://api.mercadolibre.com/orders/search",
            params={
                "seller": user_id,
                "order.date_created.from": date_from + "T00:00:00.000-00:00",
                "order.date_created.to":   date_to + "T23:59:59.000-00:00",
                "order.status": "paid",
                "limit": limit,
                "offset": offset,
                "sort": "date_asc"
            },
            headers={"Authorization": "Bearer " + token}
        )
        if not r.ok or not r.text:
            break
        data = r.json()
        results_page = data.get("results", [])
        total_api = data.get("paging", {}).get("total", 0)
        pages.append({"offset": offset, "count": len(results_page), "total_reported": total_api})
        all_paid.extend(results_page)
        offset += limit
        if offset >= total_api or not results_page:
            break

    gmv = sum(o.get("paid_amount", 0) or o.get("total_amount", 0) for o in all_paid)

    return jsonify({
        "date_range": {"from": date_from, "to": date_to},
        "status_totals": results,
        "paid_pagination": pages,
        "paid_fetched": len(all_paid),
        "paid_total_api": total_api,
        "gmv_calculated": round(gmv, 2),
        "sample_order": all_paid[0] if all_paid else None
    })


@app.route("/api/debug-sales2/<user_id>")
def debug_sales2(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    from datetime import date as ddate, timedelta as tdelta
    today = ddate.today()
    date_from = (today - tdelta(days=29)).isoformat()  # 30 dias incluindo hoje
    date_to   = today.isoformat()

    # Busca total por cada status individualmente
    statuses = ["paid", "cancelled", "confirmed", "payment_in_process",
                "payment_required", "partially_refunded", "pending_cancel",
                "delivered", "invalid"]
    status_totals = {}
    for st in statuses:
        r = requests.get(
            "https://api.mercadolibre.com/orders/search",
            params={
                "seller": user_id,
                "order.date_created.from": date_from + "T00:00:00.000-00:00",
                "order.date_created.to":   date_to + "T23:59:59.000-00:00",
                "order.status": st,
                "limit": 1, "offset": 0
            },
            headers={"Authorization": "Bearer " + token}
        )
        if r.ok and r.text:
            try:
                status_totals[st] = r.json().get("paging", {}).get("total", 0)
            except Exception:
                status_totals[st] = 0
        else:
            status_totals[st] = {"error": r.status_code}

    # Total sem filtro
    r_all = requests.get(
        "https://api.mercadolibre.com/orders/search",
        params={
            "seller": user_id,
            "order.date_created.from": date_from + "T00:00:00.000-00:00",
            "order.date_created.to":   date_to + "T23:59:59.000-00:00",
            "limit": 1, "offset": 0
        },
        headers={"Authorization": "Bearer " + token}
    )
    total_no_filter = r_all.json().get("paging", {}).get("total", 0) if r_all.ok and r_all.text else 0

    return jsonify({
        "date_range": {"from": date_from, "to": date_to},
        "total_no_filter": total_no_filter,
        "by_status": status_totals,
        "sum_known_statuses": sum(v for v in status_totals.values() if isinstance(v, int))
    })


@app.route("/api/debug-reports/<user_id>")
def debug_reports(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    from datetime import date as ddate, timedelta as tdelta
    today = ddate.today()
    date_from = (today - tdelta(days=29)).isoformat()  # 30 dias incluindo hoje
    date_to   = today.isoformat()

    results = {}

    # Endpoint 1: data-reporting/seller_performance
    r1 = requests.get(
        "https://api.mercadolibre.com/data-reporting/seller_performance",
        params={"caller.id": user_id, "date_from": date_from, "date_to": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    results["seller_performance"] = {"status": r1.status_code, "response": r1.json() if r1.ok and r1.text else r1.text[:200]}

    # Endpoint 2: seller_performance/search
    r2 = requests.get(
        "https://api.mercadolibre.com/data-reporting/seller_performance/search",
        params={"caller.id": user_id, "date_from": date_from, "date_to": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    results["seller_performance_search"] = {"status": r2.status_code, "response": r2.json() if r2.ok and r2.text else r2.text[:200]}

    # Endpoint 3: users/{id}/orders_summary
    r3 = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/orders_summary",
        params={"date_from": date_from, "date_to": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    results["orders_summary"] = {"status": r3.status_code, "response": r3.json() if r3.ok and r3.text else r3.text[:200]}

    # Endpoint 4: billing/integration/invoices
    r4 = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/sales",
        params={"date_from": date_from, "date_to": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    results["user_sales"] = {"status": r4.status_code, "response": r4.json() if r4.ok and r4.text else r4.text[:200]}

    # Endpoint 5: seller_performance via meli API
    r5 = requests.get(
        "https://api.mercadolibre.com/users/" + user_id + "/items_visits/time_window",
        params={"last": "30", "unit": "day", "ending": date_to},
        headers={"Authorization": "Bearer " + token}
    )
    results["items_visits_window"] = {"status": r5.status_code, "response": r5.json() if r5.ok and r5.text else r5.text[:200]}

    # Endpoint 6: highlights
    r6 = requests.get(
        "https://api.mercadolibre.com/highlights/MLB/seller/" + user_id,
        headers={"Authorization": "Bearer " + token}
    )
    results["highlights"] = {"status": r6.status_code, "response": r6.json() if r6.ok and r6.text else r6.text[:200]}

    # Endpoint 7: v2 orders com status=all
    r7 = requests.get(
        "https://api.mercadolibre.com/orders/search",
        params={
            "seller": user_id,
            "order.date_created.from": date_from + "T00:00:00.000-00:00",
            "order.date_created.to":   date_to + "T23:59:59.000-00:00",
            "limit": 1,
            "offset": 0
        },
        headers={"Authorization": "Bearer " + token}
    )
    results["orders_no_status_filter"] = {
        "status": r7.status_code,
        "total": r7.json().get("paging", {}).get("total", 0) if r7.ok and r7.text else 0
    }

    return jsonify({
        "user_id": user_id,
        "date_range": {"from": date_from, "to": date_to},
        "results": results
    })


@app.route("/api/debug-promos/<user_id>")
def debug_promos(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    results = {}

    endpoints = [
        ("promotions_v1",        "https://api.mercadolibre.com/promotions",
         {"seller_id": user_id}),
        ("seller_deals",         "https://api.mercadolibre.com/seller-promotions/users/" + user_id + "/promotions",
         {}),
        ("seller_deals_v2",      "https://api.mercadolibre.com/seller-promotions/users/" + user_id + "/promotions?limit=10",
         {}),
        ("discount_campaigns",   "https://api.mercadolibre.com/discount-campaigns/users/" + user_id,
         {}),
        ("coupons",              "https://api.mercadolibre.com/users/" + user_id + "/coupons",
         {}),
        ("item_promotions",      "https://api.mercadolibre.com/users/" + user_id + "/item_promotions",
         {}),
        ("promotions_search2",   "https://api.mercadolibre.com/promotions/search",
         {"seller_id": user_id}),
        ("loyalty",              "https://api.mercadolibre.com/loyalty/users/" + user_id + "/summary",
         {}),
        ("mshops_promotions",    "https://api.mercadolibre.com/mshops/promotion-campaigns/seller/" + user_id,
         {}),
        ("price_campaigns",      "https://api.mercadolibre.com/price-campaigns/seller/" + user_id,
         {}),
    ]

    for name, url, params in endpoints:
        try:
            r = requests.get(url, params=params, headers={"Authorization": "Bearer " + token})
            try:
                body = r.json()
            except Exception:
                body = r.text[:300]
            results[name] = {"status": r.status_code, "response": body}
        except Exception as e:
            results[name] = {"error": str(e)}

    return jsonify({"user_id": user_id, "results": results})

@app.route("/notifications", methods=["GET", "POST"])
def notifications():
    # GET: validação do webhook pelo ML (envia ?challenge=xxx, devemos retornar o mesmo valor)
    challenge = request.args.get("challenge")
    if challenge:
        return jsonify({"challenge": challenge}), 200

    # POST: notificação real do ML
    try:
        payload = request.get_json(silent=True) or {}
        topic   = payload.get("topic", "")
        res_id  = payload.get("resource", "")
        user_id = str(payload.get("user_id", ""))
        print(f"[NOTIFICATION] topic={topic} resource={res_id} user_id={user_id}")
    except Exception as e:
        print(f"[NOTIFICATION][ERRO] {e}")

    return "", 200


@app.route("/api/questions/<user_id>")
def get_questions(user_id):
    ok, err = check_seller_access(user_id)
    if not ok: return err
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    r_all = requests.get(
        "https://api.mercadolibre.com/questions/search",
        params={"seller_id": user_id, "limit": 50, "sort_fields": "date_created", "sort_types": "DESC"},
        headers={"Authorization": "Bearer " + token}
    )
    all_data = r_all.json() if r_all.ok and r_all.text else {}

    r_unans = requests.get(
        "https://api.mercadolibre.com/questions/search",
        params={"seller_id": user_id, "status": "UNANSWERED", "limit": 50},
        headers={"Authorization": "Bearer " + token}
    )
    unans_data = r_unans.json() if r_unans.ok and r_unans.text else {}

    questions = all_data.get("questions", [])
    result = []
    for q in questions:
        result.append({
            "id":           q.get("id"),
            "text":         q.get("text", ""),
            "status":       q.get("status", ""),
            "date_created": q.get("date_created", "")[:16].replace("T", " "),
            "item_id":      q.get("item_id", ""),
            "item_title":   q.get("item_title", ""),
            "answer":       q.get("answer", {}).get("text", "") if q.get("answer") else "",
            "answer_date":  q.get("answer", {}).get("date_created", "")[:16].replace("T", " ") if q.get("answer") else "",
        })

    return jsonify({
        "seller_id":        user_id,
        "nickname":         seller["nickname"],
        "total":            all_data.get("total", 0),
        "total_unanswered": unans_data.get("total", 0),
        "questions":        result
    })


@app.route("/api/debug-financial/<user_id>")
def debug_financial(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    def safe_json(r):
        try: return r.json() if r.text and r.text.strip() else {}
        except: return {"raw": r.text[:300] if r.text else ""}

    results = {}
    endpoints = {
        "balance":           f"https://api.mercadolibre.com/users/{user_id}/mercadopago_account/balance",
        "money_in_accounts": f"https://api.mercadolibre.com/users/{user_id}/money_in_accounts",
        "movements":         f"https://api.mercadolibre.com/users/{user_id}/movements",
        "available_balance": f"https://api.mercadolibre.com/users/{user_id}/available_balance",
        "seller_wallet":     f"https://api.mercadolibre.com/seller-account/{user_id}/balance",
    }
    for name, url in endpoints.items():
        r = requests.get(url, headers={"Authorization": "Bearer " + token}, timeout=8)
        results[name] = {"status": r.status_code, "response": str(safe_json(r))[:200]}
    return jsonify({"user_id": user_id, "results": results})


@app.route("/api/debug-messages/<user_id>")
def debug_messages(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    def safe_json(r):
        try: return r.json() if r.text and r.text.strip() else {}
        except: return {"raw": r.text[:300] if r.text else ""}

    results = {}
    endpoints = {
        "questions":            f"https://api.mercadolibre.com/questions/search?seller_id={user_id}&limit=5",
        "questions_unanswered": f"https://api.mercadolibre.com/questions/search?seller_id={user_id}&status=UNANSWERED&limit=5",
        "messages_unread":      f"https://api.mercadolibre.com/messages/unread?user_id={user_id}",
        "claims":               f"https://api.mercadolibre.com/post-purchase/v1/claims/search?seller_id={user_id}&limit=5",
    }
    for name, url in endpoints.items():
        r = requests.get(url, headers={"Authorization": "Bearer " + token}, timeout=8)
        results[name] = {"status": r.status_code, "response": str(safe_json(r))[:300]}
    return jsonify({"user_id": user_id, "results": results})


@app.route("/api/debug-shipments/<user_id>")
def debug_shipments(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    def safe_json(r):
        try: return r.json() if r.text and r.text.strip() else {}
        except: return {"raw": r.text[:300] if r.text else ""}

    results = {}
    endpoints = {
        "shipments_search":      f"https://api.mercadolibre.com/shipments/search?seller_id={user_id}&limit=5",
        "shipments_by_seller":   f"https://api.mercadolibre.com/users/{user_id}/shipments?limit=5",
        "orders_with_shipments": f"https://api.mercadolibre.com/orders/search?seller={user_id}&limit=3",
        "flex_shipments":        f"https://api.mercadolibre.com/users/{user_id}/flex_handshakes?limit=5",
    }
    for name, url in endpoints.items():
        r = requests.get(url, headers={"Authorization": "Bearer " + token}, timeout=8)
        results[name] = {"status": r.status_code, "response": str(safe_json(r))[:300]}
    return jsonify({"user_id": user_id, "results": results})


@app.route("/api/validate/<user_id>")
def validate(user_id):
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao encontrado"}), 404

    results = {}
    today = datetime.now(timezone.utc).date().isoformat()
    date_from = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()

    def check(name, url, params=None):
        try:
            r = requests.get(url, params=params, headers={"Authorization": "Bearer " + token}, timeout=10)
            data = r.json() if r.text and r.text.strip() else {}
            results[name] = {"status": r.status_code, "ok": r.status_code==200, "preview": str(data)[:120]}
        except Exception as e:
            results[name] = {"status": 0, "ok": False, "preview": str(e)[:120]}

    check("perfil",    f"https://api.mercadolibre.com/users/{user_id}")
    check("vendas",    "https://api.mercadolibre.com/orders/search", {"seller": user_id, "limit": 1})
    check("visitas",   f"https://api.mercadolibre.com/users/{user_id}/items_visits", {"date_from": date_from, "date_to": today})
    check("reputacao", f"https://api.mercadolibre.com/users/{user_id}/seller_reputation")
    check("promocoes", f"https://api.mercadolibre.com/seller-promotions/users/{user_id}", {"app_version": "v2"})
    check("perguntas", f"https://api.mercadolibre.com/questions/search", {"seller_id": user_id, "limit": 1})

    r_adv = requests.get("https://api.mercadolibre.com/advertising/advertisers",
                         params={"product_id": "PADS"},
                         headers={"Authorization": "Bearer " + token, "Api-Version": "1"}, timeout=10)
    adv_data = r_adv.json() if r_adv.ok and r_adv.text else []
    adv_id = adv_data[0]["id"] if isinstance(adv_data, list) and adv_data else None
    results["ads_advertiser"] = {"status": r_adv.status_code, "ok": r_adv.status_code==200, "advertiser_id": adv_id}

    if adv_id:
        check("ads_campanhas", f"https://api.mercadolibre.com/advertising/advertisers/{adv_id}/product_ads/campaigns")
    else:
        results["ads_campanhas"] = {"status": 0, "ok": False, "preview": "advertiser_id nao encontrado"}

    ok_count = sum(1 for v in results.values() if v.get("ok"))
    return jsonify({"seller": seller["nickname"], "user_id": user_id, "score": f"{ok_count}/{len(results)}", "results": results})


@app.route("/api/promo-items/<user_id>/<promo_id>")
def get_promo_items(user_id, promo_id):
    ok, err = check_seller_access(user_id)
    if not ok: return err
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    promo_type = request.args.get("type", "SMART")
    start_date = request.args.get("start_date", "")

    # Busca itens da promoção
    r = requests.get(
        f"https://api.mercadolibre.com/seller-promotions/promotions/{promo_id}/items",
        params={"promotion_type": promo_type, "app_version": "v2"},
        headers={"Authorization": "Bearer " + token}
    )
    if not r.ok:
        return jsonify({"error": "Nao foi possivel buscar itens", "status": r.status_code}), 400

    data = r.json()
    items = data.get("results", [])

    if not items:
        return jsonify({"promo_id": promo_id, "items": [], "total": 0})

    # Para cada item, busca vendas desde o início da promoção
    date_from = start_date[:10] if start_date else datetime.now(timezone.utc).date().isoformat()
    date_to   = datetime.now(timezone.utc).date().isoformat()

    # Busca pedidos no período da promoção
    all_orders = []
    offset = 0
    while offset < 2000:
        r_ord = requests.get(
            "https://api.mercadolibre.com/orders/search",
            params={
                "seller": user_id,
                "order.date_created.from": date_from + "T00:00:00.000-03:00",
                "order.date_created.to":   date_to   + "T23:59:59.000-03:00",
                "limit": 50, "offset": offset, "sort": "date_asc"
            },
            headers={"Authorization": "Bearer " + token}
        )
        if not r_ord.ok: break
        batch = r_ord.json().get("results", [])
        if not batch: break
        all_orders.extend(batch)
        if len(batch) < 50: break
        offset += 50

    SALE_STATUSES = {"confirmed","payment_required","payment_in_process","paid","partially_refunded","partially_paid","pending_cancel","delivered"}
    sale_orders = [o for o in all_orders if o.get("status") in SALE_STATUSES]

    # Agrupa vendas por item_id
    sales_by_item = {}
    for o in sale_orders:
        for oi in o.get("order_items", []):
            iid = str(oi.get("item", {}).get("id", ""))
            qty = oi.get("quantity", 1)
            price = oi.get("unit_price", 0)
            if iid not in sales_by_item:
                sales_by_item[iid] = {"qty": 0, "revenue": 0.0}
            sales_by_item[iid]["qty"]     += qty
            sales_by_item[iid]["revenue"] += qty * price

    # Busca títulos dos itens em batch
    item_ids = [str(item.get("id","")) for item in items if item.get("id")]
    titles = {}
    if item_ids:
        chunk = ",".join(item_ids[:20])
        r_titles = requests.get(
            "https://api.mercadolibre.com/items",
            params={"ids": chunk, "attributes": "id,title"},
            headers={"Authorization": "Bearer " + token}
        )
        if r_titles.ok and r_titles.text:
            for entry in r_titles.json():
                body = entry.get("body", {})
                titles[str(body.get("id",""))] = body.get("title","")

    # Monta resultado
    result = []
    for item in items:
        iid    = str(item.get("id", ""))
        s      = sales_by_item.get(iid, {"qty": 0, "revenue": 0.0})
        status = item.get("status", "")
        orig   = item.get("original_price", 0) or 0

        # seller_percentage:
        # - started: vem direto do campo ou calculado pelo preço atual
        # - candidate: ML não retorna o campo — calcula via suggested_discounted_price
        seller_pct = item.get("seller_percentage", 0)
        if not seller_pct and orig > 0:
            if status == "candidate":
                suggested = item.get("suggested_discounted_price", 0) or 0
                if suggested > 0:
                    seller_pct = round((orig - suggested) / orig * 100, 1)
            elif status == "started":
                price = item.get("price", 0) or 0
                if price > 0:
                    seller_pct = round((orig - price) / orig * 100, 1)

        result.append({
            "item_id":          iid,
            "title":            titles.get(iid, iid),
            "status":           status,
            "price":            item.get("price", 0),
            "original_price":   orig,
            "meli_percentage":  item.get("meli_percentage", 0),
            "seller_percentage": seller_pct,
            "suggested_discounted_price": item.get("suggested_discounted_price", 0),
            "min_discounted_price": item.get("min_discounted_price", 0),
            "start_date":       str(item.get("start_date",""))[:10],
            "end_date":         str(item.get("end_date",""))[:10],
            "qty_sold":         s["qty"],
            "revenue":          round(s["revenue"], 2),
        })

    return jsonify({
        "promo_id":   promo_id,
        "date_from":  date_from,
        "date_to":    date_to,
        "total":      len(result),
        "items":      result
    })



@app.route("/api/item-visits/<user_id>/<item_id>")
def get_item_visits(user_id, item_id):
    ok, err = check_seller_access(user_id)
    if not ok: return err
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")

    if not date_from or not date_to:
        today    = datetime.now(timezone.utc).date()
        date_to  = today.isoformat()
        date_from = (today - tdelta(days=29)).isoformat()

    try:
        r = requests.get(
            f"https://api.mercadolibre.com/users/{user_id}/items_visits",
            params={"ids": item_id, "date_from": date_from, "date_to": date_to},
            headers={"Authorization": "Bearer " + token},
            timeout=8
        )
        if not r.ok:
            return jsonify({"visits": 0, "error": r.status_code})

        data = r.json()
        # Resposta pode ser lista ou dict com data_points
        visits = 0
        if isinstance(data, list):
            for entry in data:
                if str(entry.get("item_id", "")) == str(item_id):
                    visits = entry.get("total_visits", entry.get("visits", 0))
        elif isinstance(data, dict):
            visits = data.get("total_visits", data.get("visits", 0))
            # Pode vir como lista de items dentro do dict
            items_list = data.get("items", data.get("results", []))
            if items_list:
                for entry in items_list:
                    if str(entry.get("item_id", "")) == str(item_id):
                        visits = entry.get("total_visits", entry.get("visits", 0))

        return jsonify({
            "item_id":   item_id,
            "visits":    visits,
            "date_from": date_from,
            "date_to":   date_to
        })
    except Exception as e:
        return jsonify({"visits": 0, "error": str(e)})


@app.route("/api/price-suggestion/<user_id>/<item_id>")
def get_price_suggestion(user_id, item_id):
    ok, err = check_seller_access(user_id)
    if not ok: return err
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    try:
        r = requests.get(
            f"https://api.mercadolibre.com/items/{item_id}/price_suggestion",
            headers={"Authorization": "Bearer " + token},
            timeout=8
        )
        if not r.ok:
            return jsonify({"error": r.status_code, "available": False})

        data = r.json()

        # Extrai campos principais
        suggested     = data.get("suggested_price", data.get("price_suggestion", {}).get("price", 0)) or 0
        price_min     = data.get("min_price", data.get("price_range", {}).get("min", 0)) or 0
        price_max     = data.get("max_price", data.get("price_range", {}).get("max", 0)) or 0
        median        = data.get("median_price", data.get("price_suggestion", {}).get("median_price", 0)) or 0

        return jsonify({
            "item_id":       item_id,
            "available":     True,
            "suggested":     round(suggested, 2),
            "price_min":     round(price_min, 2),
            "price_max":     round(price_max, 2),
            "median":        round(median, 2),
            "raw":           data  # para debug
        })

    except Exception as e:
        return jsonify({"error": str(e), "available": False})


@app.route("/api/full-stock/<user_id>")
@login_required
def get_full_stock(user_id):
    ok, err = check_seller_access(user_id)
    if not ok: return err
    """Retorna apenas produtos Full ML com estoque atual — endpoint leve para alertas"""
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404
    try:
        # Busca todos os item_ids do seller
        all_ids = []
        scroll_id = None
        for _ in range(10):
            params = {"limit": 100}
            if scroll_id:
                params["scroll_id"] = scroll_id
            r = requests.get(
                f"https://api.mercadolibre.com/users/{user_id}/items/search",
                params=params,
                headers={"Authorization": "Bearer " + token},
                timeout=8
            )
            if not r.ok: break
            d     = r.json()
            batch = d.get("results", [])
            if not batch: break
            all_ids.extend(batch)
            scroll_id = d.get("scroll_id")
            if not scroll_id or len(batch) < 100: break

        # Busca atributos relevantes em chunks
        full_products = []
        for i in range(0, len(all_ids), 20):
            chunk = all_ids[i:i+20]
            r2 = requests.get(
                "https://api.mercadolibre.com/items",
                params={"ids": ",".join(chunk), "attributes": "id,title,available_quantity,fulfillment,shipping,status"},
                headers={"Authorization": "Bearer " + token},
                timeout=8
            )
            if not r2.ok: continue
            for entry in r2.json():
                body = entry.get("body", {})
                if body.get("status") not in ("active", "paused"): continue
                fulfillment = body.get("fulfillment") or {}
                shipping    = body.get("shipping") or {}
                is_full = bool(
                    fulfillment.get("fulfillment_id") or
                    fulfillment.get("status") == "active" or
                    shipping.get("fulfillment") or
                    shipping.get("logistic_type") == "fulfillment" or
                    shipping.get("logistic_type") == "meli_fulfillment"
                )
                if not is_full: continue
                full_products.append({
                    "item_id": str(body.get("id", "")),
                    "title":   body.get("title", ""),
                    "stock":   body.get("available_quantity", 0) or 0,
                    "status":  body.get("status", "active"),
                })

        return jsonify({"user_id": user_id, "products": full_products})
    except Exception as e:
        return jsonify({"error": str(e), "products": []}), 500


@app.route("/api/item-promos/<user_id>/<item_id>")
def get_item_promos(user_id, item_id):
    ok, err = check_seller_access(user_id)
    if not ok: return err
    token, seller = get_seller_token(user_id)
    if not seller:
        return jsonify({"error": "Seller nao autorizado"}), 404

    try:
        # Busca promoções usando a mesma lógica que funciona em get_promotions
        all_promos = []
        r1 = requests.get(
            f"https://api.mercadolibre.com/seller-promotions/users/{user_id}",
            params={"app_version": "v2"},
            headers={"Authorization": "Bearer " + token},
            timeout=8
        )
        if r1.ok and r1.text:
            d = r1.json()
            raw = d if isinstance(d, list) else d.get("results", d.get("promotions", []))
            all_promos = [p for p in raw if p.get("status") in ("started", "active")]

        if not all_promos:
            r2 = requests.get(
                f"https://api.mercadolibre.com/seller-promotions/users/{user_id}/promotions",
                params={"app_version": "v2"},
                headers={"Authorization": "Bearer " + token},
                timeout=8
            )
            if r2.ok and r2.text:
                d = r2.json()
                raw = d if isinstance(d, list) else d.get("results", d.get("promotions", []))
                all_promos = [p for p in raw if p.get("status") in ("started", "active")]

        if not all_promos:
            return jsonify({"promos": []})
        type_map   = {"SMART": "Smart", "DEAL": "Deal", "PRICE_MATCHING_MELI_ALL": "Price Match",
                      "LIGHTNING": "Relâmpago", "SELLER_CAMPAIGN": "Campanha"}
        result = []

        for promo in all_promos:
            promo_id   = promo.get("id", "")
            promo_type = promo.get("type", "SMART")
            if not promo_id:
                continue

            # Busca itens dessa promoção para ver se o item está incluído
            r_items = requests.get(
                f"https://api.mercadolibre.com/seller-promotions/promotions/{promo_id}/items",
                params={"promotion_type": promo_type, "app_version": "v2"},
                headers={"Authorization": "Bearer " + token},
                timeout=8
            )
            if not r_items.ok:
                continue

            items_data = r_items.json()
            items_list = items_data.get("results", items_data.get("items", [])) if isinstance(items_data, dict) else []

            for it in items_list:
                if str(it.get("id", "")) != str(item_id):
                    continue

                status   = it.get("status", "")
                orig     = it.get("original_price", 0) or 0
                price    = it.get("price", 0) or 0
                sugg     = it.get("suggested_discounted_price", 0) or 0

                # Calcula desconto
                if status == "started" and orig > 0 and price > 0:
                    discount_pct = round((orig - price) / orig * 100, 1)
                elif status == "candidate" and orig > 0 and sugg > 0:
                    discount_pct = round((orig - sugg) / orig * 100, 1)
                else:
                    discount_pct = it.get("seller_percentage", 0) or 0

                result.append({
                    "promo_id":    promo_id,
                    "name":        promo.get("name", promo_id),
                    "type":        type_map.get(promo_type, promo_type),
                    "status":      status,
                    "discount_pct": discount_pct,
                    "start_date":  str(promo.get("start_date", ""))[:10],
                    "end_date":    str(promo.get("end_date", promo.get("finish_date", promo.get("to_date", promo.get("to", "")))))[:10],
                    "original_price": orig,
                    "promo_price":    price if status == "started" else sugg,
                })
                break  # achou o item nessa promoção, próxima promoção

        return jsonify({"item_id": item_id, "promos": result})

    except Exception as e:
        return jsonify({"promos": [], "error": str(e)})


@app.route("/api/debug-promo-items/<user_id>/<promo_id>")
def debug_promo_items(user_id, promo_id):
    try:
        token, seller = get_seller_token(user_id)
        if not seller:
            return jsonify({"error": "Seller nao encontrado"}), 404

        results = {}
        for ptype in ["SMART", "DEAL", "PRICE_MATCHING_MELI_ALL", "LIGHTNING", "SELLER_CAMPAIGN"]:
            try:
                url = f"https://api.mercadolibre.com/seller-promotions/promotions/{promo_id}/items"
                r = requests.get(url,
                    params={"promotion_type": ptype, "app_version": "v2"},
                    headers={"Authorization": "Bearer " + token}, timeout=8)
                try:
                    raw = r.json()
                except Exception:
                    raw = {"raw_text": r.text[:500]}

                items_sample = []
                if isinstance(raw, dict):
                    items_sample = raw.get("results", raw.get("items", []))[:2]
                elif isinstance(raw, list):
                    items_sample = raw[:2]

                results[ptype] = {
                    "http_status": r.status_code,
                    "response_keys": list(raw.keys()) if isinstance(raw, dict) else [],
                    "first_items_raw": items_sample,
                    "raw_preview": str(raw)[:800]
                }
            except Exception as e:
                results[ptype] = {"error": str(e)}

        return jsonify({"promo_id": promo_id, "user_id": user_id, "results": results})
    except Exception as e:
        return jsonify({"fatal_error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
