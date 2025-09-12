import os
import io
import csv
import traceback
from datetime import datetime, date, timedelta
from flask import Flask, render_template, render_template_string, request, redirect, url_for, session, flash, send_file
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from werkzeug.exceptions import HTTPException

APP_NAME = "OPTEC PUSHCASE APP"
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DATABASE_URL = os.environ.get("DATABASE_URL")  # fornecido pelo Render Postgres
TIMEZONE_TZ = os.environ.get("TZ", "America/Fortaleza")

# ============================ ENGINE / SESSION ============================
if not DATABASE_URL:
    print("[BOOT] ATENÇÃO: DATABASE_URL não está definido! Defina a variável de ambiente.", flush=True)

engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False) if engine else None

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ============================ ERROS / DIAGNÓSTICO ============================

@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    tb = traceback.format_exc()
    print(f"[ERROR] Rota: {request.path}\n{tb}", flush=True)
    html = """
    {% extends "base.html" %}
    {% block title %}Erro Interno (500){% endblock %}
    {% block content %}
      <h2>Erro Interno (500)</h2>
      <p>Ocorreu um erro ao processar <code>{{ path }}</code>.</p>
      <p>Tente novamente. O administrador pode verificar os logs do servidor para mais detalhes.</p>
      <details style="margin-top:12px;">
        <summary>Mostrar detalhes técnicos (stack trace)</summary>
        <pre style="white-space:pre-wrap;background:#f7f7f7;border:1px solid #ddd;padding:8px;border-radius:8px;">{{ tb }}</pre>
      </details>
    {% endblock %}
    """
    try:
        return render_template_string(html, path=request.path, tb=tb), 500
    except Exception:
        return (f"<h1>500 - Erro Interno</h1><p>Rota: {request.path}</p>"
                "<p>Veja os logs do servidor para detalhes.</p>"), 500

@app.route("/_diag")
def diag():
    info = {
        "app_name": APP_NAME,
        "now_utc": datetime.utcnow().isoformat(sep=" ", timespec="seconds"),
        "database_url_prefix": (DATABASE_URL.split("://")[0] + "://***") if DATABASE_URL else "(vazio)",
        "engine_ready": bool(engine),
        "openpyxl": None,
        "db_ok": None,
        "tables": [],
        "users_count": None,
    }
    try:
        import openpyxl  # noqa
        info["openpyxl"] = "instalado"
    except Exception as e:
        info["openpyxl"] = f"ausente ({e})"

    if engine:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                info["db_ok"] = True
                rs = conn.execute(text("""
                    SELECT tablename FROM pg_tables
                    WHERE schemaname NOT IN ('pg_catalog','information_schema')
                    ORDER BY tablename
                """))
                info["tables"] = [r[0] for r in rs.fetchall()]
                try:
                    cnt = conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
                    info["users_count"] = int(cnt)
                except Exception as e:
                    info["users_count"] = f"falhou ({e})"
        except Exception as e:
            info["db_ok"] = f"falhou ({e})"
    else:
        info["db_ok"] = "engine não inicializado (DATABASE_URL?)"

    html = """
    <!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><title>Diagnóstico</title>
    <style>
      body{font-family:Arial,sans-serif;max-width:880px;margin:24px auto;padding:0 12px;}
      code,pre{background:#f7f7f7;border:1px solid #ddd;padding:4px 6px;border-radius:6px;}
      table{border-collapse:collapse;width:100%;margin-top:10px;}
      th,td{border:1px solid #ddd;padding:8px;text-align:left;}
      th{background:#eee;}
    </style></head><body>
    <h2>Diagnóstico</h2>
    <ul>
      <li><b>App:</b> {{ info.app_name }}</li>
      <li><b>Agora (UTC):</b> {{ info.now_utc }}</li>
      <li><b>DATABASE_URL:</b> {{ info.database_url_prefix }}</li>
      <li><b>Engine pronto:</b> {{ info.engine_ready }}</li>
      <li><b>Conexão DB:</b> {{ info.db_ok }}</li>
      <li><b>openpyxl:</b> {{ info.openpyxl }}</li>
      <li><b>Users count:</b> {{ info.users_count }}</li>
    </ul>
    <h3>Tabelas</h3>
    {% if info.tables %}
      <ul>{% for t in info.tables %}<li><code>{{ t }}</code></li>{% endfor %}</ul>
    {% else %}
      <p>(Nenhuma tabela retornada.)</p>
    {% endif %}
    </body></html>
    """
    return render_template_string(html, info=info)

# ============================ DB INIT ============================

def init_db():
    if not engine:
        print("[BOOT] init_db(): pulado porque engine não foi criado (DATABASE_URL ausente).", flush=True)
        return

    ddl = """
    CREATE TABLE IF NOT EXISTS users (
      id SERIAL PRIMARY KEY,
      username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL CHECK (role IN ('admin','comprador','pagador')),
      created_at TIMESTAMP NOT NULL
    );

    CREATE TABLE IF NOT EXISTS suppliers (
      id SERIAL PRIMARY KEY,
      name TEXT UNIQUE NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,
      billing INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS products (
      id SERIAL PRIMARY KEY,
      name TEXT NOT NULL,
      code TEXT,
      kind TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,
      in_stock INTEGER NOT NULL DEFAULT 0,
      UNIQUE(name, kind)
    );

    CREATE TABLE IF NOT EXISTS rules (
      id SERIAL PRIMARY KEY,
      product_id INTEGER NOT NULL REFERENCES products(id),
      supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
      max_price DOUBLE PRECISION NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,
      UNIQUE(product_id, supplier_id)
    );

    CREATE TABLE IF NOT EXISTS purchase_orders (
      id SERIAL PRIMARY KEY,
      buyer_id INTEGER NOT NULL REFERENCES users(id),
      supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
      status TEXT NOT NULL CHECK (status IN ('PENDENTE_PAGAMENTO','PAGO','CANCELADO')),
      total DOUBLE PRECISION NOT NULL,
      note TEXT,
      created_at TIMESTAMP NOT NULL,
      updated_at TIMESTAMP NOT NULL
    );

    CREATE TABLE IF NOT EXISTS purchase_items (
      id SERIAL PRIMARY KEY,
      order_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
      product_id INTEGER NOT NULL REFERENCES products(id),
      quantity INTEGER NOT NULL,
      unit_price DOUBLE PRECISION NOT NULL,
      sphere DOUBLE PRECISION,
      cylinder DOUBLE PRECISION,
      base DOUBLE PRECISION,
      addition DOUBLE PRECISION,
      os_number TEXT
    );

    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_purchase_items_os') THEN
        EXECUTE 'DROP INDEX idx_purchase_items_os';
      END IF;
    EXCEPTION WHEN others THEN
      NULL;
    END $$;

    CREATE TABLE IF NOT EXISTS payments (
      id SERIAL PRIMARY KEY,
      order_id INTEGER NOT NULL UNIQUE REFERENCES purchase_orders(id) ON DELETE CASCADE,
      payer_id INTEGER NOT NULL REFERENCES users(id),
      method TEXT,
      reference TEXT,
      paid_at TIMESTAMP NOT NULL,
      amount DOUBLE PRECISION NOT NULL
    );

    CREATE TABLE IF NOT EXISTS audit_log (
      id SERIAL PRIMARY KEY,
      user_id INTEGER REFERENCES users(id),
      action TEXT NOT NULL,
      details TEXT,
      created_at TIMESTAMP NOT NULL
    );
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))
        # --- INÍCIO: suporte a EXTORNO / CRÉDITOS DE FORNECEDOR ---
        try:
            conn.execute(text("""
                DO $$
                BEGIN
                  IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.check_constraints c
                    JOIN information_schema.constraint_column_usage u
                         ON c.constraint_name = u.constraint_name
                    WHERE c.check_clause LIKE '%EXTORNADA%'
                      AND u.table_name = 'purchase_orders'
                      AND u.column_name = 'status'
                  ) THEN
                    ALTER TABLE purchase_orders DROP CONSTRAINT IF EXISTS purchase_orders_status_check;
                    ALTER TABLE purchase_orders
                      ADD CONSTRAINT purchase_orders_status_check
                      CHECK (status IN ('PENDENTE_PAGAMENTO','PAGO','CANCELADO','EXTORNADA'));
                  END IF;
                END$$;
            """))
        except Exception:
            pass

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS supplier_credits (
              id SERIAL PRIMARY KEY,
              supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
              amount DOUBLE PRECISION NOT NULL,
              remaining DOUBLE PRECISION NOT NULL,
              created_at TIMESTAMP NOT NULL,
              note TEXT,
              item_id INTEGER UNIQUE
            );
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS supplier_credit_uses (
              id SERIAL PRIMARY KEY,
              credit_id INTEGER NOT NULL REFERENCES supplier_credits(id),
              order_id INTEGER NOT NULL REFERENCES purchase_orders(id),
              amount DOUBLE PRECISION NOT NULL,
              used_at TIMESTAMP NOT NULL
            );
        """))
        # --- FIM: suporte a EXTORNO / CRÉDITOS DE FORNECEDOR ---

        try:
            conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS in_stock INTEGER NOT NULL DEFAULT 0"))
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS billing INTEGER NOT NULL DEFAULT 0"))
        except Exception:
            pass

        exists = conn.execute(text("SELECT COUNT(*) AS n FROM users")).scalar_one()
        if exists == 0:
            from werkzeug.security import generate_password_hash
            conn.execute(
                text("INSERT INTO users (username, password_hash, role, created_at) VALUES (:u,:p,:r,:c)"),
                dict(u="admin", p=generate_password_hash("admin123"), r="admin", c=datetime.utcnow())
            )

# Helpers comuns
def db_all(sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).mappings().all()

def db_one(sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).mappings().first()

def db_exec(sql, **params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)

def audit(action, details=""):
    u = current_user()
    db_exec("INSERT INTO audit_log (user_id, action, details, created_at) VALUES (:uid,:a,:d,:c)",
            uid=(u["id"] if u else None), a=action, d=details, c=datetime.utcnow())


def supplier_credit_balance(supplier_id: int) -> float:
    row = db_one("SELECT COALESCE(SUM(remaining),0) AS bal FROM supplier_credits WHERE supplier_id=:s", s=supplier_id)
    return float(row["bal"] or 0.0)

def consume_supplier_credit(conn, supplier_id: int, order_id: int, desired_amount: float) -> float:
    """
    Consome créditos do fornecedor no modelo FIFO. Retorna quanto foi de fato abatido.
    """
    if desired_amount <= 0:
        return 0.0
    rows = conn.execute(text("""
        SELECT id, remaining FROM supplier_credits
        WHERE supplier_id=:s AND remaining > 0
        ORDER BY created_at ASC, id ASC
    """), dict(s=supplier_id)).mappings().all()
    to_consume = desired_amount
    consumed = 0.0
    for r in rows:
        if to_consume <= 0:
            break
        take = float(min(r["remaining"], to_consume))
        if take > 0:
            conn.execute(text("UPDATE supplier_credits SET remaining=remaining-:v WHERE id=:id"), dict(v=take, id=r["id"]))
            conn.execute(text("""
                INSERT INTO supplier_credit_uses (credit_id, order_id, amount, used_at)
                VALUES (:cid, :oid, :amt, :ts)
            """), dict(cid=r["id"], oid=order_id, amt=take, ts=datetime.utcnow()))
            consumed += take
            to_consume -= take
    return consumed

# ============================ AUTH/CTX ============================

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    u = db_one("SELECT * FROM users WHERE id=:id", id=uid)
    return u

def require_role(*roles):
    u = current_user()
    if not u or u["role"] not in roles:
        flash("Acesso negado.", "error")
        return redirect(url_for("index"))

@app.context_processor
def inject_globals():
    return {"now": datetime.utcnow(), "role": session.get("role"), "user": current_user(), "app_name": APP_NAME}

# ============================ RELATÓRIOS (Excel in-memory) ============================

def build_excel_bytes_for_day(day_str: str) -> bytes:
    rows = db_all("""
        SELECT
            s.name  AS fornecedor,
            p.name  AS produto,
            p.in_stock AS in_stock,
            i.sphere, i.cylinder, i.base, i.addition,
            i.quantity, i.unit_price,
            pay.method AS metodo,
            DATE(pay.paid_at) AS data
        FROM payments pay
        JOIN purchase_orders o ON o.id = pay.order_id
        JOIN suppliers s       ON s.id = o.supplier_id
        JOIN purchase_items i  ON i.order_id = o.id
        JOIN products p        ON p.id = i.product_id
        WHERE DATE(pay.paid_at) = :day
        ORDER BY s.name, p.name
    """, day=day_str)

    try:
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
        from openpyxl.styles import Font
    except ImportError as e:
        raise RuntimeError("openpyxl não está instalado") from e

    wb = Workbook()
    ws = wb.active
    ws.title = "Pagamentos do Dia"
    ws.append(["Fornecedor", "Produto", "Estoque", "Dioptria", "Data", "Método", "Valor"])

    def fmt_dioptria(r):
        if r["sphere"] is not None or r["cylinder"] is not None:
            esf = f"{r['sphere']:+.2f}" if r["sphere"] is not None else "-"
            cil = f"{r['cylinder']:+.2f}" if r["cylinder"] is not None else "-"
            return f"Esf {esf} / Cil {cil}"
        else:
            b = f"{r['base']:.2f}" if r["base"] is not None else "-"
            add = f"+{r['addition']:.2f}" if r["addition"] is not None else "-"
            return f"Base {b} / Adição {add}"

    grand_total = 0.0
    for r in rows:
        subtotal = float(r["quantity"] or 0) * float(r["unit_price"] or 0.0)
        grand_total += subtotal
        ws.append([
            r["fornecedor"],
            r["produto"],
            "Sim" if int(r["in_stock"] or 0) == 1 else "Não",
            fmt_dioptria(r),
            r["data"].isoformat() if hasattr(r["data"], "isoformat") else str(r["data"]),
            r["metodo"] or "",
            float(f"{subtotal:.2f}")
        ])

    ws.append(["", "", "", "", "", "", ""])
    from openpyxl.styles import Font
    ws.append(["", "", "", "", "", "TOTAL", float(f"{grand_total:.2f}")])
    ws.cell(row=ws.max_row, column=6).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=7).font = Font(bold=True)

    from openpyxl.utils import get_column_letter
    for i, w in enumerate([18, 28, 12, 26, 12, 14, 14], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.getvalue()

def build_excel_bytes_for_period(start_str: str, end_str: str) -> bytes:
    rows = db_all("""
        SELECT
            s.name  AS fornecedor,
            p.name  AS produto,
            p.in_stock AS in_stock,
            i.sphere, i.cylinder, i.base, i.addition,
            i.quantity, i.unit_price,
            pay.method AS metodo,
            DATE(pay.paid_at) AS data
        FROM payments pay
        JOIN purchase_orders o ON o.id = pay.order_id
        JOIN suppliers s       ON s.id = o.supplier_id
        JOIN purchase_items i  ON i.order_id = o.id
        JOIN products p        ON p.id = i.product_id
        WHERE DATE(pay.paid_at) BETWEEN :d1 AND :d2
        ORDER BY DATE(pay.paid_at), s.name, p.name
    """, d1=start_str, d2=end_str)

    try:
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
        from openpyxl.styles import Font
    except ImportError as e:
        raise RuntimeError("openpyxl não está instalado") from e

    wb = Workbook()
    ws = wb.active
    ws.title = "Pagamentos por Período"
    ws.append(["Fornecedor", "Produto", "Estoque", "Dioptria", "Data", "Método", "Valor"])

    def fmt_dioptria(r):
        if r["sphere"] is not None or r["cylinder"] is not None:
            esf = f"{r['sphere']:+.2f}" if r["sphere"] is not None else "-"
            cil = f"{r['cylinder']:+.2f}" if r["cylinder"] is not None else "-"
            return f"Esf {esf} / Cil {cil}"
        else:
            b = f"{r['base']:.2f}" if r["base"] is not None else "-"
            add = f"+{r['addition']:.2f}" if r["addition"] is not None else "-"
            return f"Base {b} / Adição {add}"

    grand_total = 0.0
    for r in rows:
        subtotal = float(r["quantity"] or 0) * float(r["unit_price"] or 0.0)
        grand_total += subtotal
        ws.append([
            r["fornecedor"],
            r["produto"],
            "Sim" if int(r["in_stock"] or 0) == 1 else "Não",
            fmt_dioptria(r),
            r["data"].isoformat() if hasattr(r["data"], "isoformat") else str(r["data"]),
            r["metodo"] or "",
            float(f"{subtotal:.2f}")
        ])

    ws.append(["", "", "", "", "", "", ""])
    ws.append(["", "", "", "", "", "TOTAL", float(f"{grand_total:.2f}")])
    ws.cell(row=ws.max_row, column=6).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=7).font = Font(bold=True)

    from openpyxl.utils import get_column_letter
    for i, w in enumerate([18, 28, 12, 26, 12, 14, 14], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.getvalue()

# ============================ ROTAS ============================

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        from werkzeug.security import check_password_hash
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        u = db_one("SELECT * FROM users WHERE username=:u", u=username)
        if u and check_password_hash(u["password_hash"], password):
            session["user_id"] = u["id"]; session["role"] = u["role"]
            flash(f"Bem-vindo, {u['username']}!", "success"); audit("login", f"user={u['username']}")
            return redirect(url_for("index"))
        flash("Credenciais inválidas", "error")
    try:
        return render_template("login.html")
    except Exception:
        return render_template_string("""
        <!doctype html><meta charset="utf-8"><title>Login</title>
        <h2>Login</h2>
        <form method="post">
          <div><label>Usuário</label><br><input name="username" required></div>
          <div><label>Senha</label><br><input name="password" type="password" required></div>
          <button>Entrar</button>
        </form>
        """)

@app.route("/logout")
def logout():
    u = current_user(); session.clear(); flash("Sessão encerrada.", "info"); audit("logout", f"user={u['username'] if u else ''}")
    return redirect(url_for("login"))

@app.route("/")
def index():
    try:
        return render_template("index.html")
    except Exception:
        return render_template_string("""
        {% extends "base.html" %}
        {% block content %}
          <h2>Bem-vindo ao {{ app_name }}</h2>
          <p>Use o menu superior para navegar.</p>
        {% endblock %}
        """, app_name=APP_NAME)

# -------- Admin: Usuários --------

@app.route("/admin/users")
def admin_users():
    ret = require_role("admin")
    if ret: return ret
    users = db_all("SELECT id, username, role, created_at FROM users ORDER BY id")
    return render_template("admin_users.html", users=users)

@app.route("/admin/users/create", methods=["POST"])
def admin_users_create():
    ret = require_role("admin")
    if ret: return ret
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    role = request.form.get("role") or "comprador"
    if not username or not password or role not in ("admin","comprador","pagador"):
        flash("Dados inválidos.", "error"); return redirect(url_for("admin_users"))
    from werkzeug.security import generate_password_hash
    try:
        db_exec("INSERT INTO users (username, password_hash, role, created_at) VALUES (:u,:p,:r,:c)",
                u=username, p=generate_password_hash(password), r=role, c=datetime.utcnow())
        audit("user_create", f"{username}/{role}"); flash("Usuário criado.", "success")
    except Exception:
        flash("Usuário já existe.", "error")
    return redirect(url_for("admin_users"))
@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
def admin_users_delete(uid):
    ret = require_role("admin")
    if ret: return ret

    if uid == session.get("user_id"):
        flash("Não é possível excluir o próprio usuário logado.", "error")
        return redirect(url_for("admin_users"))

    # Confere se o usuário existe
    urow = db_one("SELECT id, username FROM users WHERE id=:id", id=uid)
    if not urow:
        flash("Usuário não encontrado.", "error")
        return redirect(url_for("admin_users"))

    try:
        from werkzeug.security import generate_password_hash
        with engine.begin() as conn:
            # Garante usuário sentinela "(deleted)" para manter integridade (colunas NOT NULL)
            sentinel = conn.execute(text("SELECT id FROM users WHERE username='(deleted)'")).mappings().first()
            if not sentinel:
                # Cria com um hash qualquer, papel 'comprador' (irrelevante; ninguém logará)
                sid = conn.execute(text("""
                    INSERT INTO users (username, password_hash, role, created_at)
                    VALUES (:u, :p, 'comprador', :c)
                    RETURNING id
                """), dict(u="(deleted)", p=generate_password_hash("!deleted!"), c=datetime.utcnow())).scalar_one()
            else:
                sid = sentinel["id"]

            # Reatribui referências ao sentinela para não violar NOT NULL / FK
            conn.execute(text("UPDATE purchase_orders SET buyer_id = :sid WHERE buyer_id = :uid"),
                         dict(sid=sid, uid=uid))
            conn.execute(text("UPDATE payments       SET payer_id = :sid WHERE payer_id = :uid"),
                         dict(sid=sid, uid=uid))
            conn.execute(text("UPDATE audit_log      SET user_id  = :sid WHERE user_id  = :uid"),
                         dict(sid=sid, uid=uid))

            # Exclui o usuário original
            conn.execute(text("DELETE FROM users WHERE id=:id"), dict(id=uid))

        audit("user_delete", f"id={uid}/username={urow['username']}")
        flash("Usuário excluído com sucesso.", "success")
    except Exception as e:
        flash(f"Falha ao excluir usuário: {e}", "error")

    return redirect(url_for("admin_users"))

# -------- Admin: Fornecedores --------

@app.route("/admin/suppliers")
def admin_suppliers():
    ret = require_role("admin")
    if ret: return ret
    suppliers = db_all("SELECT * FROM suppliers ORDER BY name")
    return render_template("admin_suppliers.html", suppliers=suppliers)

@app.route("/admin/suppliers/create", methods=["POST"])
def admin_suppliers_create():
    ret = require_role("admin")
    if ret: return ret
    name = (request.form.get("name") or "").strip()
    billing = 1 if (request.form.get("billing") in ("on","1","true","True")) else 0
    if not name:
        flash("Nome inválido.", "error"); return redirect(url_for("admin_suppliers"))
    try:
        db_exec("INSERT INTO suppliers (name, active, billing) VALUES (:n,1,:b)", n=name, b=billing)
        audit("supplier_create", f"{name} billing={billing}"); flash("Fornecedor criado.", "success")
    except Exception:
        flash("Fornecedor já existe.", "error")
    return redirect(url_for("admin_suppliers"))

@app.route("/admin/suppliers/<int:sid>/toggle", methods=["POST"])
def admin_suppliers_toggle(sid):
    ret = require_role("admin")
    if ret: return ret
    s = db_one("SELECT * FROM suppliers WHERE id=:id", id=sid)
    if not s: flash("Fornecedor não encontrado.", "error"); return redirect(url_for("admin_suppliers"))
    new_active = 0 if s["active"] else 1
    db_exec("UPDATE suppliers SET active=:a WHERE id=:id", a=new_active, id=sid)
    audit("supplier_toggle", f"id={sid} active={new_active}")
    return redirect(url_for("admin_suppliers"))

@app.route("/admin/suppliers/<int:sid>/toggle-billing", methods=["POST"])
def admin_suppliers_toggle_billing(sid):
    ret = require_role("admin")
    if ret: return ret
    s = db_one("SELECT * FROM suppliers WHERE id=:id", id=sid)
    if not s:
        flash("Fornecedor não encontrado.", "error"); return redirect(url_for("admin_suppliers"))
    new_billing = 0 if (s["billing"] or 0) == 1 else 1
    db_exec("UPDATE suppliers SET billing=:b WHERE id=:id", b=new_billing, id=sid)
    audit("supplier_toggle_billing", f"id={sid} billing={new_billing}")
    return redirect(url_for("admin_suppliers"))

@app.route("/admin/suppliers/<int:sid>/delete", methods=["POST"])
def admin_suppliers_delete(sid):
    ret = require_role("admin")
    if ret: return ret
    used_rule = db_one("SELECT 1 FROM rules WHERE supplier_id=:id LIMIT 1", id=sid)
    used_order = db_one("SELECT 1 FROM purchase_orders WHERE supplier_id=:id LIMIT 1", id=sid)
    if used_rule or used_order:
        flash("Não é possível excluir: fornecedor em uso (regras ou pedidos).", "error")
        return redirect(url_for("admin_suppliers"))
    db_exec("DELETE FROM suppliers WHERE id=:id", id=sid)
    audit("supplier_delete", f"id={sid}")
    flash("Fornecedor excluído.", "success")
    return redirect(url_for("admin_suppliers"))

# -------- Admin: Produtos --------

@app.route("/admin/products")
def admin_products():
    ret = require_role("admin")
    if ret: return ret
    products = db_all("SELECT * FROM products ORDER BY kind, name")
    return render_template("admin_products.html", products=products)

@app.route("/admin/products/create", methods=["POST"])
def admin_products_create():
    ret = require_role("admin")
    if ret: return ret
    name = (request.form.get("name") or "").strip()
    code = (request.form.get("code") or "").strip()
    kind = (request.form.get("kind") or "lente").lower()
    in_stock = 1 if (request.form.get("in_stock") in ("on","1","true","True")) else 0
    if kind not in ("lente","bloco") or not name:
        flash("Dados inválidos.", "error"); return redirect(url_for("admin_products"))
    try:
        db_exec(
            "INSERT INTO products (name, code, kind, in_stock, active) "
            "VALUES (:n,:c,:k,:instock,1)",
            n=name, c=code, k=kind, instock=in_stock
        )
        audit("product_create", f"{name}/{kind}/in_stock={in_stock}"); flash("Produto criado.", "success")
    except Exception:
        flash("Produto já existe para este tipo.", "error")
    return redirect(url_for("admin_products"))

@app.route("/admin/products/<int:pid>/toggle", methods=["POST"])
def admin_products_toggle(pid):
    ret = require_role("admin")
    if ret: return ret
    p = db_one("SELECT * FROM products WHERE id=:id", id=pid)
    if not p: flash("Produto não encontrado.", "error"); return redirect(url_for("admin_products"))
    new_active = 0 if p["active"] else 1
    db_exec("UPDATE products SET active=:a WHERE id=:id", a=new_active, id=pid)
    audit("product_toggle", f"id={pid} active={new_active}")
    return redirect(url_for("admin_products"))

@app.route("/admin/products/<int:pid>/delete", methods=["POST"])
def admin_products_delete(pid):
    ret = require_role("admin")
    if ret: return ret
    used_rule = db_one("SELECT 1 FROM rules WHERE product_id=:id LIMIT 1", id=pid)
    used_item = db_one("SELECT 1 FROM purchase_items WHERE product_id=:id LIMIT 1", id=pid)
    if used_rule or used_item:
        flash("Não é possível excluir: produto em uso (regras ou pedidos).", "error")
        return redirect(url_for("admin_products"))
    db_exec("DELETE FROM products WHERE id=:id", id=pid)
    audit("product_delete", f"id={pid}")
    flash("Produto excluído.", "success")
    return redirect(url_for("admin_products"))

# -------- Admin: Regras --------

@app.route("/admin/rules")
def admin_rules():
    ret = require_role("admin")
    if ret: return ret
    rules = db_all("""
        SELECT r.id, r.max_price, r.active,
               p.name as product_name, p.kind as product_kind, p.id as product_id,
               s.name as supplier_name, s.id as supplier_id
        FROM rules r
        JOIN products p ON p.id = r.product_id
        JOIN suppliers s ON s.id = r.supplier_id
        ORDER BY p.kind, p.name, s.name
    """)
    products = db_all("SELECT * FROM products WHERE active=1 ORDER BY kind, name")
    suppliers = db_all("SELECT * FROM suppliers WHERE active=1 ORDER BY name")
    return render_template("admin_rules.html", rules=rules, products=products, suppliers=suppliers)

@app.route("/admin/rules/create", methods=["POST"])
def admin_rules_create():
    ret = require_role("admin")
    if ret: return ret
    product_id = request.form.get("product_id", type=int)
    supplier_id = request.form.get("supplier_id", type=int)
    max_price = request.form.get("max_price", type=float)
    if not product_id or not supplier_id or max_price is None:
        flash("Dados inválidos.", "error"); return redirect(url_for("admin_rules"))
    try:
        db_exec("INSERT INTO rules (product_id, supplier_id, max_price, active) VALUES (:p,:s,:m,1)",
                p=product_id, s=supplier_id, m=max_price)
        audit("rule_create", f"product={product_id} supplier={supplier_id} max={max_price}"); flash("Regra criada.", "success")
    except Exception:
        flash("Essa combinação já existe.", "error")
    return redirect(url_for("admin_rules"))

@app.route("/admin/rules/<int:rid>/toggle", methods=["POST"])
def admin_rules_toggle(rid):
    ret = require_role("admin")
    if ret: return ret
    r = db_one("SELECT * FROM rules WHERE id=:id", id=rid)
    if not r: flash("Regra não encontrada.", "error"); return redirect(url_for("admin_rules"))
    new_active = 0 if r["active"] else 1
    db_exec("UPDATE rules SET active=:a WHERE id=:id", a=new_active, id=rid)
    audit("rule_toggle", f"id={rid} active={new_active}")
    return redirect(url_for("admin_rules"))

@app.route("/admin/rules/<int:rid>/delete", methods=["POST"])
def admin_rules_delete(rid):
    ret = require_role("admin")
    if ret: return ret
    try:
        db_exec("DELETE FROM rules WHERE id=:id", id=rid)
        audit("rule_delete", f"id={rid}")
        flash("Regra excluída.", "success")
    except Exception as e:
        flash(f"Falha ao excluir regra: {e}", "error")
    return redirect(url_for("admin_rules"))

# -------- Importação em massa (ADMIN) --------

@app.route("/admin/import/template.xlsx")
def admin_import_template():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        html = """
        {% extends "base.html" %}
        {% block title %}Template de Importação{% endblock %}
        {% block content %}
        <div class="container" style="max-width:800px;margin:0 auto">
          <h2>Template de Importação</h2>
          <p style="color:#b00"><strong>Dependência ausente:</strong> o servidor não tem <code>openpyxl</code> instalado.</p>
          <p>Adicione <code>openpyxl</code> ao seu <code>requirements.txt</code> e faça o deploy novamente:</p>
          <pre>openpyxl==3.1.5</pre>
        </div>
        {% endblock %}
        """
        return render_template_string(html)

    wb = Workbook()

    ws1 = wb.active
    ws1.title = "Suppliers"
    ws1.append(["name", "active", "billing"])
    ws1.append(["Fornecedor Exemplo A", 1, 1])
    ws1.append(["Fornecedor Exemplo B", 1, 0])
    for cell in ws1[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="DDDDDD", end_color="DDDDDD", fill_type="solid")

    ws2 = wb.create_sheet("Products")
    ws2.append(["name", "code", "kind", "active", "in_stock"])
    ws2.append(["Lente Asférica 1.67", "LA167", "lente", 1, 0])
    ws2.append(["Bloco Base 4", "BB4", "bloco", 1, 1])
    for cell in ws2[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="DDDDDD", end_color="DDDDDD", fill_type="solid")

    ws3 = wb.create_sheet("Rules")
    ws3.append(["product_name", "product_kind", "supplier_name", "max_price", "active"])
    ws3.append(["Lente Asférica 1.67", "lente", "Fornecedor Exemplo A", 250.00, 1])
    ws3.append(["Bloco Base 4", "bloco", "Fornecedor Exemplo B", 80.00, 1])
    for cell in ws3[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="DDDDDD", end_color="DDDDDD", fill_type="solid")

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(bio, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="optec_import_template.xlsx")

@app.route("/admin/import", methods=["GET", "POST"])
def admin_import():
    ret = require_role("admin")
    if ret: return ret

    report = {"suppliers": {"inserted":0, "updated":0},
              "products": {"inserted":0, "updated":0},
              "rules": {"inserted":0, "updated":0},
              "errors": []}

    if request.method == "POST":
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("Envie um arquivo .xlsx", "error")
        else:
            try:
                from openpyxl import load_workbook
                wb = load_workbook(file, data_only=True)
                with engine.begin() as conn:
                    # Suppliers
                    if "Suppliers" in wb.sheetnames:
                        ws = wb["Suppliers"]
                        headers = [str(c.value).strip().lower() if c.value is not None else "" for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=False))]
                        def idx(col): return headers.index(col) if col in headers else -1
                        i_name = idx("name"); i_active = idx("active"); i_billing = idx("billing")
                        if i_name == -1:
                            report["errors"].append("Suppliers: coluna obrigatória 'name' não encontrada.")
                        else:
                            for row in ws.iter_rows(min_row=2, values_only=True):
                                if row is None: continue
                                name = (row[i_name] or "").strip() if row[i_name] else ""
                                if not name: continue
                                active = int(row[i_active]) if (i_active != -1 and row[i_active] is not None) else 1
                                billing = int(row[i_billing]) if (i_billing != -1 and row[i_billing] is not None) else 0
                                res = conn.execute(text("""
                                    INSERT INTO suppliers (name, active, billing)
                                    VALUES (:n, :a, :b)
                                    ON CONFLICT (name) DO UPDATE SET active=EXCLUDED.active, billing=EXCLUDED.billing
                                    RETURNING (xmax = 0) AS inserted
                                """), dict(n=name, a=active, b=billing))
                                inserted = res.fetchone()[0]
                                if inserted: report["suppliers"]["inserted"] += 1
                                else: report["suppliers"]["updated"] += 1

                    # Products
                    if "Products" in wb.sheetnames:
                        ws = wb["Products"]
                        headers = [str(c.value).strip().lower() if c.value is not None else "" for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=False))]
                        def idx(col): return headers.index(col) if col in headers else -1
                        i_name = idx("name"); i_code = idx("code"); i_kind = idx("kind"); i_active = idx("active"); i_stock = idx("in_stock")
                        if i_name == -1 or i_kind == -1:
                            report["errors"].append("Products: colunas obrigatórias 'name' e 'kind' não encontradas.")
                        else:
                            for row in ws.iter_rows(min_row=2, values_only=True):
                                if row is None: continue
                                name = (row[i_name] or "").strip() if row[i_name] else ""
                                if not name: continue
                                code = (row[i_code] or "").strip() if (i_code != -1 and row[i_code]) else ""
                                kind = (row[i_kind] or "").strip().lower() if row[i_kind] else ""
                                if kind not in ("lente", "bloco"):
                                    report["errors"].append(f"Products: kind inválido '{kind}' para '{name}'. Use 'lente' ou 'bloco'.")
                                    continue
                                active = int(row[i_active]) if (i_active != -1 and row[i_active] is not None) else 1
                                in_stock = int(row[i_stock]) if (i_stock != -1 and row[i_stock] is not None) else 0
                                res = conn.execute(text("""
                                    INSERT INTO products (name, code, kind, active, in_stock)
                                    VALUES (:n, :c, :k, :a, :instock)
                                    ON CONFLICT (name, kind) DO UPDATE SET code=EXCLUDED.code, active=EXCLUDED.active, in_stock=EXCLUDED.in_stock
                                    RETURNING (xmax = 0) AS inserted
                                """), dict(n=name, c=code, k=kind, a=active, instock=in_stock))
                                inserted = res.fetchone()[0]
                                if inserted: report["products"]["inserted"] += 1
                                else: report["products"]["updated"] += 1

                    # Rules
                    if "Rules" in wb.sheetnames:
                        ws = wb["Rules"]
                        headers = [str(c.value).strip().lower() if c.value is not None else "" for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=False))]
                        def idx(col): return headers.index(col) if col in headers else -1
                        i_pn = idx("product_name"); i_pk = idx("product_kind"); i_sn = idx("supplier_name"); i_mp = idx("max_price"); i_active = idx("active")
                        if i_pn == -1 or i_pk == -1 or i_sn == -1 or i_mp == -1:
                            report["errors"].append("Rules: colunas obrigatórias 'product_name', 'product_kind', 'supplier_name', 'max_price' não encontradas.")
                        else:
                            for row in ws.iter_rows(min_row=2, values_only=True):
                                if row is None: continue
                                pn = (row[i_pn] or "").strip() if row[i_pn] else ""
                                pk = (row[i_pk] or "").strip().lower() if row[i_pk] else ""
                                sn = (row[i_sn] or "").strip() if row[i_sn] else ""
                                try:
                                    mp = float(row[i_mp]) if row[i_mp] is not None else None
                                except:
                                    mp = None
                                if not pn or pk not in ("lente","bloco") or not sn or mp is None:
                                    report["errors"].append(f"Rules: dados inválidos (produto='{pn}', kind='{pk}', fornecedor='{sn}', max_price='{row[i_mp]}').")
                                    continue
                                active = int(row[i_active]) if (i_active != -1 and row[i_active] is not None) else 1

                                # Garantir IDs
                                prod = conn.execute(text("SELECT id FROM products WHERE name=:n AND kind=:k"), dict(n=pn, k=pk)).mappings().first()
                                if not prod:
                                    prod = conn.execute(text("""
                                        INSERT INTO products (name, code, kind, active)
                                        VALUES (:n, '', :k, 1)
                                        ON CONFLICT (name, kind) DO NOTHING
                                        RETURNING id
                                    """), dict(n=pn, k=pk)).mappings().first()
                                    if not prod:
                                        prod = conn.execute(text("SELECT id FROM products WHERE name=:n AND kind=:k"), dict(n=pn, k=pk)).mappings().first()
                                supp = conn.execute(text("SELECT id FROM suppliers WHERE name=:n"), dict(n=sn)).mappings().first()
                                if not supp:
                                    supp = conn.execute(text("""
                                        INSERT INTO suppliers (name, active)
                                        VALUES (:n, 1)
                                        ON CONFLICT (name) DO NOTHING
                                        RETURNING id
                                    """), dict(n=sn)).mappings().first()
                                    if not supp:
                                        supp = conn.execute(text("SELECT id FROM suppliers WHERE name=:n"), dict(n=sn)).mappings().first()

                                if not prod or not supp:
                                    report["errors"].append(f"Rules: não foi possível identificar produto/fornecedor ('{pn}'/'{pk}' | '{sn}').")
                                    continue

                                res = conn.execute(text("""
                                    INSERT INTO rules (product_id, supplier_id, max_price, active)
                                    VALUES (:p, :s, :m, :a)
                                    ON CONFLICT (product_id, supplier_id) DO UPDATE SET max_price=EXCLUDED.max_price, active=EXCLUDED.active
                                    RETURNING (xmax = 0) AS inserted
                                """), dict(p=prod["id"], s=supp["id"], m=mp, a=active))
                                inserted = res.fetchone()[0]
                                if inserted: report["rules"]["inserted"] += 1
                                else: report["rules"]["updated"] += 1

                flash("Importação concluída.", "success")
            except ImportError:
                report["errors"].append("Dependência ausente: instale 'openpyxl' no servidor.")
                flash("Instale 'openpyxl' para importar planilhas .xlsx.", "error")
            except Exception as e:
                report["errors"].append(str(e))
                flash("Falha na importação. Veja os erros.", "error")

    html = """
    {% extends "base.html" %}
    {% block title %}Importação em Massa{% endblock %}
    {% block content %}
    <div class="container" style="max-width: 900px; margin: 0 auto;">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:16px;">
        <h2>Importar planilha (Excel .xlsx)</h2>
        <a class="btn btn-sm btn-primary" href="{{ url_for('admin_import_template') }}">Baixar Template</a>
      </div>
      <p>Use o modelo com abas <strong>Suppliers</strong> (com <code>billing</code>), <strong>Products</strong> e <strong>Rules</strong>.</p>
      <form method="post" enctype="multipart/form-data" style="margin-top: 16px;">
        <input type="file" name="file" accept=".xlsx" required />
        <button type="submit">Importar</button>
      </form>
      {% if report %}
      <hr/>
      <h3>Resultado</h3>
      <ul>
        <li>Fornecedores: {{ report.suppliers.inserted }} inseridos, {{ report.suppliers.updated }} atualizados</li>
        <li>Produtos: {{ report.products.inserted }} inseridos, {{ report.products.updated }} atualizados</li>
        <li>Regras: {{ report.rules.inserted }} inseridos, {{ report.rules.updated }} atualizados</li>
      </ul>
      {% if report.errors and report.errors|length > 0 %}
        <h4>Erros</h4>
        <ul>
          {% for e in report.errors %}
            <li style="color:#b00">{{ e }}</li>
          {% endfor %}
        </ul>
      {% endif %}
      {% endif %}
    </div>
    {% endblock %}
    """
    return render_template_string(html, report=report)

# -------- Comprador: Novo Pedido (CORRIGIDO: divide por fornecedor e limpa o form ao adicionar) --------

@app.route("/compras/novo", methods=["GET","POST"])
def compras_novo():
    ret = require_role("comprador","admin")
    if ret: return ret

    combos = db_all("""
        SELECT r.id as rule_id, p.id as product_id, p.name as product_name, p.code as product_code, p.kind,
               s.id as supplier_id, s.name as supplier_name, r.max_price
        FROM rules r
        JOIN products p ON p.id = r.product_id
        JOIN suppliers s ON s.id = r.supplier_id
        WHERE r.active=1 AND p.active=1 AND s.active=1
        ORDER BY s.name, p.kind, p.name
    """)
    products = db_all("SELECT id, name, code, kind FROM products WHERE active=1 ORDER BY kind, name")

    combos = [dict(r) for r in combos]
    products = [dict(p) for p in products]

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()

        os_number = (request.form.get("os_number") or "").strip()
        pair_option = request.form.get("pair_option")  # 'meio' ou 'par'
        tipo = (request.form.get("tipo") or "").lower()  # 'lente' ou 'bloco'
        product_id = request.form.get("product_id", type=int)
        product_code = (request.form.get("product_code") or "").strip()
        supplier_main = request.form.get("supplier_main", type=int)
        price_main = request.form.get("price_main", type=float)

        supplier_distinto = request.form.get("supplier_distinto") == "on"
        supplier_second = request.form.get("supplier_second", type=int) if supplier_distinto else None
        price_second = request.form.get("price_second", type=float) if supplier_distinto else None

        if not os_number:
            flash("Informe o número da OS.", "error")
            return render_template("compras_novo.html", combos=combos, products=products)

        existing = db_one("SELECT COUNT(*) AS n FROM purchase_items WHERE os_number=:os", os=os_number)
        existing_n = int(existing["n"] if existing else 0)

        if pair_option not in ("meio","par"):
            flash("Selecione se é meio par ou um par.", "error")
            return render_template("compras_novo.html", combos=combos, products=products)

        if tipo not in ("lente","bloco"):
            flash("Selecione o tipo (lente/bloco).", "error")
            return render_template("compras_novo.html", combos=combos, products=products)

        if not product_id and product_code:
            p = db_one("SELECT id FROM products WHERE code=:c AND kind=:k AND active=1", c=product_code, k=tipo)
            if p:
                product_id = int(p["id"])

        if not product_id:
            flash("Selecione o produto (ou informe um código válido).", "error")
            return render_template("compras_novo.html", combos=combos, products=products)

        rule_main = db_one("""
            SELECT r.*, p.kind as product_kind
            FROM rules r JOIN products p ON p.id = r.product_id
            WHERE r.product_id=:pid AND r.supplier_id=:sid AND r.active=1
        """, pid=product_id, sid=supplier_main)
        if not rule_main:
            flash("Fornecedor principal indisponível para este produto.", "error")
            return render_template("compras_novo.html", combos=combos, products=products)
        if price_main is None or price_main <= 0 or price_main > float(rule_main["max_price"]) + 1e-6:
            flash(f"Preço do item principal inválido ou acima do máximo (R$ {float(rule_main['max_price']):.2f}).", "error")
            return render_template("compras_novo.html", combos=combos, products=products)

        def _step_ok(x: float) -> bool:
            return (abs(x * 100) % 25) == 0

        def validate_lente(prefix):
            sphere = request.form.get(f"{prefix}_sphere", type=float)
            cylinder_raw = request.form.get(f"{prefix}_cylinder", type=float)
            cylinder = None
            if cylinder_raw is not None:
                cylinder = -abs(cylinder_raw)
            if sphere is None or sphere < -20 or sphere > 20 or not _step_ok(sphere):
                return None, "Esférico inválido (−20 a +20 em passos de 0,25)."
            if cylinder is None or cylinder > 0 or cylinder < -15 or not _step_ok(cylinder):
                return None, "Cilíndrico inválido (0 até −15 em passos de 0,25)."
            return {"sphere": sphere, "cylinder": cylinder, "base": None, "addition": None}, None

        def validate_bloco(prefix):
            base = request.form.get(f"{prefix}_base", type=float)
            addition = request.form.get(f"{prefix}_addition", type=float)
            allowed_bases = {0.5,1.0,2.0,4.0,6.0,8.0,10.0}
            if base is None or base not in allowed_bases:
                return None, "Base inválida (0,5; 1; 2; 4; 6; 8; 10)."
            if addition is None or addition < 1.0 or addition > 4.0 or not _step_ok(addition):
                return None, "Adição inválida (+1,00 até +4,00 em 0,25)."
            return {"sphere": None, "cylinder": None, "base": base, "addition": addition}, None

        items_to_add = []

        if tipo == "lente":
            d1, err = validate_lente("d1")
            if err:
                flash(err, "error"); return render_template("compras_novo.html", combos=combos, products=products)
        else:
            d1, err = validate_bloco("d1")
            if err:
                flash(err, "error"); return render_template("compras_novo.html", combos=combos, products=products)
        items_to_add.append({"product_id": product_id, "supplier_id": supplier_main, "price": price_main, "d": d1})

        if pair_option == "par":
            if supplier_distinto:
                if not supplier_second:
                    flash("Selecione o fornecedor do segundo item.", "error"); return render_template("compras_novo.html", combos=combos, products=products)
                rule_second = db_one("""
                    SELECT r.*, p.kind as product_kind
                    FROM rules r JOIN products p ON p.id = r.product_id
                    WHERE r.product_id=:pid AND r.supplier_id=:sid AND r.active=1
                """, pid=product_id, sid=supplier_second)
                if not rule_second:
                    flash("Fornecedor do segundo item indisponível para este produto.", "error"); return render_template("compras_novo.html", combos=combos, products=products)
                if price_second is None or price_second <= 0 or price_second > float(rule_second["max_price"]) + 1e-6:
                    flash(f"Preço do segundo item inválido ou acima do máximo (R$ {float(rule_second['max_price']):.2f}).", "error"); return render_template("compras_novo.html", combos=combos, products=products)
            else:
                supplier_second, price_second = supplier_main, price_main

            if tipo == "lente":
                d2, err = validate_lente("d2")
                if err:
                    flash(err, "error"); return render_template("compras_novo.html", combos=combos, products=products)
            else:
                d2, err = validate_bloco("d2")
                if err:
                    flash(err, "error"); return render_template("compras_novo.html", combos=combos, products=products)

            items_to_add.append({"product_id": product_id, "supplier_id": supplier_second, "price": price_second, "d": d2})

        if existing_n + len(items_to_add) > 2:
            flash("Cada número de OS só pode ter no máximo um par (2 unidades).", "error")
            return render_template("compras_novo.html", combos=combos, products=products)

        # === CORREÇÃO: criar 1 pedido por fornecedor ===
        from collections import defaultdict
        by_supplier = defaultdict(list)
        for it in items_to_add:
            by_supplier[it["supplier_id"]].append(it)

        created_orders = []
        with engine.begin() as conn:
            for sup_id, its in by_supplier.items():
                supplier_row = conn.execute(text("SELECT * FROM suppliers WHERE id=:id"), dict(id=sup_id)).mappings().first()
                faturado = (supplier_row and (supplier_row.get("billing") or 0) == 1)

                total_group = sum(float(it["price"]) for it in its)
                status = 'PAGO' if faturado else 'PENDENTE_PAGAMENTO'

                res = conn.execute(text("""
                    INSERT INTO purchase_orders (buyer_id, supplier_id, status, total, note, created_at, updated_at)
                    VALUES (:b,:s,:st,:t,:n,:c,:u) RETURNING id
                """), dict(b=session["user_id"], s=sup_id, st=status, t=total_group,
                           n=f"OS {os_number} ({pair_option})", c=datetime.utcnow(), u=datetime.utcnow()))
                order_id = res.scalar_one()

                for it in its:
                    conn.execute(text("""
                        INSERT INTO purchase_items (order_id, product_id, quantity, unit_price, sphere, cylinder, base, addition, os_number)
                        VALUES (:o,:p,1,:pr,:sf,:cl,:ba,:ad,:os)
                    """), dict(o=order_id, p=it["product_id"], pr=it["price"],
                               sf=it["d"]["sphere"], cl=it["d"]["cylinder"],
                               ba=it["d"]["base"], ad=it["d"]["addition"], os=os_number))

                if faturado:
                    conn.execute(text("""
                        INSERT INTO payments (order_id, payer_id, method, reference, paid_at, amount)
                        VALUES (:o,:p,:m,:r,:d,:a)
                    """), dict(o=order_id, p=session["user_id"], m="FATURADO",
                               r=f"OS {os_number}", d=datetime.utcnow(), a=total_group))

                created_orders.append((order_id, faturado))

        for oid, fat in created_orders:
            audit("order_create", f"id={oid} os={os_number} faturado={int(fat)}")

        # Se o botão foi "Adicionar à lista", limpa via redirect (PRG)
        if action in ("add","adicionar","add_to_list"):
            flash("Item(ns) adicionados à lista.", "success")
            return redirect(url_for("compras_novo"))

        if any(fat for _, fat in created_orders) and any((not fat) for _, fat in created_orders):
            flash("Pedidos criados: 1 FATURADO (lançado) e 1 PENDENTE (enviado ao pagador).", "success")
        elif all(fat for _, fat in created_orders):
            flash("Pedido(s) criado(s) como FATURADO(s) e incluído(s) diretamente no relatório.", "success")
        else:
            flash("Pedido(s) criado(s) e enviado(s) ao pagador.", "success")

        return redirect(url_for("compras_lista"))

    return render_template("compras_novo.html", combos=combos, products=products)

# -------- Comprador: lista/detalhe --------

@app.route("/compras")
def compras_lista():
    ret = require_role("comprador","admin")
    if ret: return ret
    orders = db_all("""
        SELECT
            o.*,
            s.name AS supplier_name,
            CASE
              WHEN o.status = 'PAGO' AND EXISTS (
                SELECT 1 FROM payments pay
                WHERE pay.order_id = o.id AND pay.method = 'FATURADO'
              )
              THEN 'FATURADO'
              ELSE o.status
            END AS status_exibido
        FROM purchase_orders o
        JOIN suppliers s ON s.id = o.supplier_id
        WHERE o.buyer_id=:b
        ORDER BY o.id DESC
    """, b=session["user_id"])

    return render_template("compras_lista.html", orders=orders)

@app.route("/compras/<int:oid>")
def compras_detalhe(oid):
    ret = require_role("comprador","admin")
    if ret: return ret
    order = db_one("""
        SELECT o.*, s.name as supplier_name
        FROM purchase_orders o JOIN suppliers s ON s.id = o.supplier_id
        WHERE o.id=:id
    """, id=oid)
    if not order:
        flash("Pedido não encontrado.", "error"); return redirect(url_for("compras_lista"))
    if session.get("role") != "admin" and order["buyer_id"] != session.get("user_id"):
        flash("Acesso negado ao pedido.", "error"); return redirect(url_for("compras_lista"))
    items = db_all("""
        SELECT i.*, p.name as product_name, p.kind as product_kind
        FROM purchase_items i JOIN products p ON p.id = i.product_id
        WHERE i.order_id=:id ORDER BY i.id
    """, id=oid)
    return render_template("compras_detalhe.html", order=order, items=items)

# -------- Pagador --------


@app.route("/pagamentos")
def pagamentos_lista():
    ret = require_role("pagador","admin")
    if ret: return ret
    orders = db_all("""
        SELECT o.*,
               u.username as buyer_name,
               s.name as supplier_name,
               COALESCE((
                 SELECT SUM(sc.remaining) FROM supplier_credits sc
                 WHERE sc.supplier_id = o.supplier_id
               ),0) AS supplier_credit
        FROM purchase_orders o
        JOIN users u ON u.id = o.buyer_id
        JOIN suppliers s ON s.id = o.supplier_id
        WHERE o.status='PENDENTE_PAGAMENTO'
        ORDER BY o.created_at ASC
    """)
    return render_template("pagamentos_lista.html", orders=orders)


@app.route("/pagamentos/<int:oid>", methods=["GET","POST"])
def pagamentos_detalhe(oid):
    ret = require_role("pagador","admin")
    if ret: return ret
    order = db_one("""
        SELECT o.*, u.username as buyer_name, s.name as supplier_name, s.id as supplier_id
        FROM purchase_orders o
        JOIN users u ON u.id = o.buyer_id
        JOIN suppliers s ON s.id = o.supplier_id
        WHERE o.id=:id
    """, id=oid)
    items = db_all("""
        SELECT i.*, p.name as product_name, p.kind as product_kind
        FROM purchase_items i JOIN products p ON p.id = i.product_id
        WHERE i.order_id=:id
    """, id=oid)
    if not order:
        flash("Pedido não encontrado.", "error"); return redirect(url_for("pagamentos_lista"))

    credit_avail = supplier_credit_balance(order["supplier_id"])
    suggested_due = max(float(order["total"]) - credit_avail, 0.0)

    if request.method == "POST":
        method = (request.form.get("method") or "PIX").strip()
        reference = (request.form.get("reference") or "").strip()
        amt_form = request.form.get("amount", type=float)
        amount_to_pay = suggested_due if (amt_form is None or amt_form < 0) else amt_form

        with engine.begin() as conn:
            consumed = consume_supplier_credit(conn, order["supplier_id"], oid, float(order["total"]))
            amount_due_now = max(float(order["total"]) - consumed, 0.0)

            if amount_due_now > 0:
                conn.execute(text("""
                    INSERT INTO payments (order_id, payer_id, method, reference, paid_at, amount)
                    VALUES (:o,:p,:m,:r,:d,:a)
                """), dict(o=oid, p=session["user_id"], m=method, r=reference, d=datetime.utcnow(), a=amount_due_now))
            conn.execute(text("UPDATE purchase_orders SET status='PAGO', updated_at=:u WHERE id=:id"),
                         dict(u=datetime.utcnow(), id=oid))

        audit("order_paid_with_credit", f"id={oid} total={order['total']} credit_used={consumed:.2f} cash={amount_due_now:.2f}")
        flash(f"Pagamento registrado. Crédito usado: R$ {consumed:.2f}. Valor pago: R$ {amount_due_now:.2f}.", "success")
        return redirect(url_for("pagamentos_lista"))

    return render_template("pagamentos_detalhe.html", order=order, items=items, credit_avail=credit_avail, suggested_due=suggested_due)

@app.route("/relatorios")
def relatorios_index():
    ret = require_role("admin","pagador")
    if ret: return ret
    hoje = date.today().isoformat()
    html = """
    {% extends "base.html" %}
    {% block title %}Relatórios{% endblock %}
    {% block content %}
    <div class="container" style="max-width: 760px; margin: 0 auto;">
      <h2>Relatórios</h2>

      <div class="card" style="padding:12px; margin-bottom:16px;">
        <h3 style="margin:0 0 8px;">Relatório Diário</h3>
        <form method="get" action="{{ url_for('relatorio_diario_xlsx') }}" style="display:flex; gap:12px; align-items:flex-end; flex-wrap:wrap;">
          <div>
            <label for="date"><strong>Data do relatório</strong></label><br/>
            <input type="date" id="date" name="date" value="{{ hoje }}"/>
          </div>
          <div>
            <button class="btn primary" type="submit">Baixar Excel (.xlsx)</button>
          </div>
          <div>
            <a class="btn" href="{{ url_for('relatorio_diario_csv', date=hoje) }}">Baixar CSV</a>
          </div>
        </form>
        <small class="muted">O Excel contém: <b>Fornecedor, Produto, Estoque, Dioptria, Data, Método, Valor</b> e o <b>TOTAL</b>.</small>
      </div>

      <div class="card" style="padding:12px;">
        <h3 style="margin:0 0 8px;">Relatório por Período</h3>
        <form method="get" action="{{ url_for('relatorio_periodo_xlsx') }}" style="display:flex; gap:12px; align-items:flex-end; flex-wrap:wrap;">
          <div>
            <label for="start"><strong>De</strong></label><br/>
            <input type="date" id="start" name="start" value="{{ hoje }}"/>
          </div>
          <div>
            <label for="end"><strong>Até</strong></label><br/>
            <input type="date" id="end" name="end" value="{{ hoje }}"/>
          </div>
          <div>
            <button class="btn primary" type="submit">Baixar Excel (.xlsx)</button>
          </div>
        </form>
        <small class="muted">Inclui pagamentos feitos e faturados (<b>FATURADO</b>) dentro do intervalo.</small>
      </div>
    </div>
    {% endblock %}
    """
    return render_template_string(html, hoje=hoje)

@app.route("/relatorios/diario.xlsx")
def relatorio_diario_xlsx():
    ret = require_role("admin","pagador")
    if ret: return ret
    day = request.args.get("date") or date.today().isoformat()
    try:
        xbytes = build_excel_bytes_for_day(day)
        return send_file(io.BytesIO(xbytes),
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"pagamentos_{day}.xlsx")
    except Exception as e:
        print(f"[RELATORIO] Falha ao gerar XLSX: {e}", flush=True)
        flash("Excel indisponível no momento. Baixando em CSV.", "warning")
        return redirect(url_for("relatorio_diario_csv", date=day))

@app.route("/relatorios/diario.csv")
def relatorio_diario_csv():
    ret = require_role("admin","pagador")
    if ret: return ret
    day = request.args.get("date") or date.today().isoformat()
    rows = db_all("""
        SELECT pay.paid_at, pay.amount, pay.method, pay.reference,
               o.id as order_id, s.name as supplier_name, u.username as payer_name
        FROM payments pay
        JOIN purchase_orders o ON o.id = pay.order_id
        JOIN suppliers s ON s.id = o.supplier_id
        JOIN users u ON u.id = pay.payer_id
        WHERE DATE(pay.paid_at)=:day
        ORDER BY pay.paid_at ASC
    """, day=day)
    output = io.StringIO(); writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["paid_at","amount","method","reference","order_id","supplier","payer"])
    for r in rows:
        paid_at = r["paid_at"].isoformat(sep=" ", timespec="seconds") if hasattr(r["paid_at"], "isoformat") else str(r["paid_at"])
        writer.writerow([paid_at, f"{float(r['amount']):.2f}", r["method"], r["reference"], r["order_id"], r["supplier_name"], r["payer_name"]])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode("utf-8-sig")), mimetype="text/csv; charset=utf-8",
                     as_attachment=True, download_name=f"pagamentos_{day}.csv")

@app.route("/relatorios/periodo.xlsx")
def relatorio_periodo_xlsx():
    ret = require_role("admin","pagador")
    if ret: return ret
    start = request.args.get("start") or date.today().isoformat()
    end   = request.args.get("end")   or start
    try:
        xbytes = build_excel_bytes_for_period(start, end)
        return send_file(io.BytesIO(xbytes),
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"pagamentos_{start}_a_{end}.xlsx")
    except Exception as e:
        print(f"[RELATORIO-PERIODO] Falha ao gerar XLSX: {e}", flush=True)
        flash("Excel indisponível no momento para o período.", "warning")
        return redirect(url_for("relatorio_diario_csv", date=start))

# -------- Admin: excluir pedidos --------

@app.route("/admin/orders/<int:oid>/delete", methods=["POST"])
def admin_orders_delete(oid):
    ret = require_role("admin")
    if ret: return ret
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM payments WHERE order_id=:id"), dict(id=oid))
        conn.execute(text("DELETE FROM purchase_items WHERE order_id=:id"), dict(id=oid))
        conn.execute(text("DELETE FROM purchase_orders WHERE id=:id"), dict(id=oid))
    audit("order_delete", f"id={oid}")
    flash("Pedido excluído.", "success")
    return redirect(url_for("compras_lista"))


# -------- Extornos (crédito ao fornecedor) --------

@app.route("/extornos")
def extornos_index():
    ret = require_role("pagador","admin")
    if ret: return ret
    day = request.args.get("date") or date.today().isoformat()

    items = db_all("""
        SELECT
          i.id            AS item_id,
          o.id            AS order_id,
          s.id            AS supplier_id,
          s.name          AS supplier_name,
          p.code          AS product_code,
          p.name          AS product_name,
          p.kind          AS product_kind,
          i.quantity, i.unit_price,
          i.sphere, i.cylinder, i.base, i.addition,
          pay.paid_at,
          (SELECT 1 FROM supplier_credits sc WHERE sc.item_id = i.id LIMIT 1) AS already
        FROM payments pay
        JOIN purchase_orders o ON o.id = pay.order_id
        JOIN purchase_items  i ON i.order_id = o.id
        JOIN products       p ON p.id = i.product_id
        JOIN suppliers      s ON s.id = o.supplier_id
        WHERE DATE(pay.paid_at) = :day
        ORDER BY s.name, p.name, i.id
    """, day=day)

    def fmt_dioptria(r):
        if r["product_kind"] == "lente":
            esf = f"{r['sphere']:+.2f}" if r["sphere"] is not None else "-"
            cil = f"{r['cylinder']:+.2f}" if r["cylinder"] is not None else "-"
            return f"Esf {esf} / Cil {cil}"
        else:
            b = f"{r['base']:.2f}" if r["base"] is not None else "-"
            add = f"+{r['addition']:.2f}" if r["addition"] is not None else "-"
            return f"Base {b} / Adição {add}"

    return render_template("extornos.html", items=items, day=day, fmt_dioptria=fmt_dioptria)

@app.post("/extornos/<int:item_id>/criar")
def extornos_criar(item_id):
    ret = require_role("pagador","admin")
    if ret: return ret
    day = request.args.get("date") or date.today().isoformat()

    row = db_one("""
        SELECT i.*, o.id AS order_id, o.supplier_id, s.name AS supplier_name, p.name AS product_name
        FROM purchase_items i
        JOIN purchase_orders o ON o.id = i.order_id
        JOIN suppliers s ON s.id = o.supplier_id
        JOIN products p  ON p.id = i.product_id
        WHERE i.id=:id
    """, id=item_id)
    if not row:
        flash("Item não encontrado.", "error")
        return redirect(url_for("extornos_index", date=day))

    already = db_one("SELECT 1 FROM supplier_credits WHERE item_id=:id", id=item_id)
    if already:
        flash("Este item já foi extornado.", "warning")
        return redirect(url_for("extornos_index", date=day))

    amount = float(row["quantity"] or 0) * float(row["unit_price"] or 0.0)
    if amount <= 0:
        flash("Valor do item inválido para extorno.", "error")
        return redirect(url_for("extornos_index", date=day))

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO supplier_credits (supplier_id, amount, remaining, created_at, note, item_id)
            VALUES (:sid, :amt, :amt, :ts, :note, :iid)
        """), dict(sid=row["supplier_id"], amt=amount, ts=datetime.utcnow(),
                   note=f"Extorno item #{item_id} (OS {row['order_id']}, {row['product_name']})",
                   iid=item_id))
        conn.execute(text("UPDATE purchase_orders SET status='EXTORNADA', updated_at=:ts WHERE id=:oid"),
                     dict(ts=datetime.utcnow(), oid=row["order_id"]))

    audit("extorno_create", f"order={row['order_id']} item={item_id} fornecedor={row['supplier_id']} valor={amount:.2f}")
    flash(f"Extorno registrado. Crédito de R$ {amount:.2f} para {row['supplier_name']}.", "success")
    return redirect(url_for("extornos_index", date=day))

# ============================ BOOTSTRAP ============================

try:
    init_db()
except Exception as e:
    print(f"[BOOT] init_db() falhou: {e}", flush=True)

if __name__ == "__main__":
    # Para rodar local, defina DATABASE_URL (ex.: sqlite:///local.db) antes de executar
    app.run(host="0.0.0.0", port=5000, debug=True)
