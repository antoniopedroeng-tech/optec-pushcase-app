import os
import io
import csv
from datetime import datetime, date, timedelta
from flask import Flask, render_template, render_template_string, request, redirect, url_for, session, flash, send_file
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

APP_NAME = "OPTEC PUSHCASE APP"
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DATABASE_URL = os.environ.get("DATABASE_URL")  # fornecido pelo Render Postgres
TIMEZONE_TZ = os.environ.get("TZ", "America/Fortaleza")

# SQLAlchemy Engine / Session
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ============================ DB INIT ============================

def init_db():
    # Cria tabelas no Postgres (sem Alembic por enquanto)
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
      billing INTEGER NOT NULL DEFAULT 1  -- 1 = faturamento (sim), 0 = não
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

    -- Remover índice único antigo de OS se existir (vamos permitir até 2 por OS)
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

        try:
            conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS in_stock INTEGER NOT NULL DEFAULT 0"))
        except Exception:
            pass

        try:
            conn.execute(text("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS billing INTEGER NOT NULL DEFAULT 1"))
        except Exception:
            pass

        exists = conn.execute(text("SELECT COUNT(*) AS n FROM users")).scalar_one()
        if exists == 0:
            from werkzeug.security import generate_password_hash
            conn.execute(
                text("INSERT INTO users (username, password_hash, role, created_at) VALUES (:u,:p,:r,:c)"),
                dict(u="admin", p=generate_password_hash("admin123"), r="admin", c=datetime.utcnow())
            )

# Helpers
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

# ============================ RELATÓRIOS ============================

def _excel_pack(rows, sheet_title="Relatório"):
    try:
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
        from openpyxl.styles import Font
    except ImportError as e:
        raise RuntimeError("openpyxl não está instalado") from e

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    for r in rows:
        ws.append(r)

    if rows:
        for c in ws[1]:
            c.font = Font(bold=True)
    for i in range(1, (len(rows[0]) if rows else 6) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 20

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.getvalue()

def build_excel_bytes_for_day(day_str: str) -> bytes:
    rows_db = db_all("""
        SELECT
            s.name  AS fornecedor,
            p.name  AS produto,
            p.in_stock AS in_stock,
            i.sphere, i.cylinder, i.base, i.addition,
            i.quantity, i.unit_price,
            DATE(pay.paid_at) AS data,
            pay.method AS metodo
        FROM payments pay
        JOIN purchase_orders o ON o.id = pay.order_id
        JOIN suppliers s       ON s.id = o.supplier_id
        JOIN purchase_items i  ON i.order_id = o.id
        JOIN products p        ON p.id = i.product_id
        WHERE DATE(pay.paid_at) = :day
        ORDER BY s.name, p.name
    """, day=day_str)

    def fmt_dioptria(r):
        if r["sphere"] is not None or r["cylinder"] is not None:
            esf = f"{r['sphere']:+.2f}" if r["sphere"] is not None else "-"
            cil = f"{r['cylinder']:+.2f}" if r["cylinder"] is not None else "-"
            return f"Esf {esf} / Cil {cil}"
        else:
            b = f"{r['base']:.2f}" if r["base"] is not None else "-"
            add = f"+{r['addition']:.2f}" if r["addition"] is not None else "-"
            return f"Base {b} / Adição {add}"

    header = ["Fornecedor","Produto","Estoque","Dioptria","Data","Método","Valor"]
    data_rows = [header]
    grand_total = 0.0
    for r in rows_db:
        subtotal = float(r["quantity"] or 0) * float(r["unit_price"] or 0.0)
        grand_total += subtotal
        data_rows.append([
            r["fornecedor"],
            r["produto"],
            "Sim" if int(r["in_stock"] or 0) == 1 else "Não",
            fmt_dioptria(r),
            (r["data"].isoformat() if hasattr(r["data"], "isoformat") else str(r["data"])),
            r["metodo"] or "",
            float(f"{subtotal:.2f}")
        ])
    data_rows.append(["","","","","","", ""])
    data_rows.append(["","","","","TOTAL","", float(f"{grand_total:.2f}")])
    return _excel_pack(data_rows, sheet_title="Pagamentos do Dia")

def build_excel_bytes_for_period(start_str: str, end_str: str) -> bytes:
    rows_db = db_all("""
        SELECT
            s.name  AS fornecedor,
            p.name  AS produto,
            p.in_stock AS in_stock,
            i.sphere, i.cylinder, i.base, i.addition,
            i.quantity, i.unit_price,
            DATE(pay.paid_at) AS data,
            pay.method AS metodo
        FROM payments pay
        JOIN purchase_orders o ON o.id = pay.order_id
        JOIN suppliers s       ON s.id = o.supplier_id
        JOIN purchase_items i  ON i.order_id = o.id
        JOIN products p        ON p.id = i.product_id
        WHERE DATE(pay.paid_at) BETWEEN :start AND :end
        ORDER BY DATE(pay.paid_at), s.name, p.name
    """, start=start_str, end=end_str)

    def fmt_dioptria(r):
        if r["sphere"] is not None or r["cylinder"] is not None:
            esf = f"{r['sphere']:+.2f}" if r["sphere"] is not None else "-"
            cil = f"{r['cylinder']:+.2f}" if r["cylinder"] is not None else "-"
            return f"Esf {esf} / Cil {cil}"
        else:
            b = f"{r['base']:.2f}" if r["base"] is not None else "-"
            add = f"+{r['addition']:.2f}" if r["addition"] is not None else "-"
            return f"Base {b} / Adição {add}"

    header = ["Fornecedor","Produto","Estoque","Dioptria","Data","Método","Valor"]
    data_rows = [header]
    grand_total = 0.0
    for r in rows_db:
        subtotal = float(r["quantity"] or 0) * float(r["unit_price"] or 0.0)
        grand_total += subtotal
        data_rows.append([
            r["fornecedor"],
            r["produto"],
            "Sim" if int(r["in_stock"] or 0) == 1 else "Não",
            fmt_dioptria(r),
            (r["data"].isoformat() if hasattr(r["data"], "isoformat") else str(r["data"])),
            r["metodo"] or "",
            float(f"{subtotal:.2f}")
        ])
    data_rows.append(["","","","","","", ""])
    data_rows.append(["","","","","TOTAL","", float(f"{grand_total:.2f}")])
    return _excel_pack(data_rows, sheet_title="Pagamentos (Período)")

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
    return render_template("login.html")

@app.route("/logout")
def logout():
    u = current_user(); session.clear(); flash("Sessão encerrada.", "info"); audit("logout", f"user={u['username'] if u else ''}")
    return redirect(url_for("login"))

@app.route("/")
def index():
    return render_template("index.html")

# -------- Admin: Usuários --------

@app.route("/admin/users")
def admin_users():
    if require_role("admin"): return require_role("admin")
    users = db_all("SELECT id, username, role, created_at FROM users ORDER BY id")
    return render_template("admin_users.html", users=users)

@app.route("/admin/users/create", methods=["POST"])
def admin_users_create():
    if require_role("admin"): return require_role("admin")
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
    if require_role("admin"):
        return require_role("admin")

    if uid == session.get("user_id"):
        flash("Não é possível excluir o próprio usuário logado.", "error")
        return redirect(url_for("admin_users"))

    refs = {
        "pedidos": db_one("SELECT 1 FROM purchase_orders WHERE buyer_id=:id LIMIT 1", id=uid),
        "pagamentos": db_one("SELECT 1 FROM payments WHERE payer_id=:id LIMIT 1", id=uid),
        "auditoria": db_one("SELECT 1 FROM audit_log WHERE user_id=:id LIMIT 1", id=uid),
    }
    if any(refs.values()):
        detalhes = []
        if refs["pedidos"]: detalhes.append("pedidos vinculados")
        if refs["pagamentos"]: detalhes.append("pagamentos vinculados")
        if refs["auditoria"]: detalhes.append("registros de auditoria")
        flash(
            "Não é possível excluir este usuário: há " + ", ".join(detalhes) +
            ". Você pode manter o histórico e apenas mudar o papel/credenciais.",
            "error"
        )
        return redirect(url_for("admin_users"))

    try:
        db_exec("DELETE FROM users WHERE id=:id", id=uid)
        audit("user_delete", f"id={uid}")
        flash("Usuário removido.", "success")
    except Exception as e:
        flash(f"Falha ao excluir usuário (restrições de integridade?): {e}", "error")
    return redirect(url_for("admin_users"))

# -------- Admin: Fornecedores --------

@app.route("/admin/suppliers")
def admin_suppliers():
    if require_role("admin"): return require_role("admin")
    suppliers = db_all("SELECT * FROM suppliers ORDER BY name")
    return render_template("admin_suppliers.html", suppliers=suppliers)

@app.route("/admin/suppliers/create", methods=["POST"])
def admin_suppliers_create():
    if require_role("admin"): return require_role("admin")
    name = (request.form.get("name") or "").strip()
    billing = 1 if (request.form.get("billing") in ("on","1","true","True","checked")) else 1
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
    if require_role("admin"): return require_role("admin")
    s = db_one("SELECT * FROM suppliers WHERE id=:id", id=sid)
    if not s: flash("Fornecedor não encontrado.", "error"); return redirect(url_for("admin_suppliers"))
    new_active = 0 if s["active"] else 1
    db_exec("UPDATE suppliers SET active=:a WHERE id=:id", a=new_active, id=sid)
    audit("supplier_toggle", f"id={sid} active={new_active}")
    return redirect(url_for("admin_suppliers"))

@app.route("/admin/suppliers/<int:sid>/toggle-billing", methods=["POST"])
def admin_suppliers_toggle_billing(sid):
    if require_role("admin"): return require_role("admin")
    s = db_one("SELECT * FROM suppliers WHERE id=:id", id=sid)
    if not s: flash("Fornecedor não encontrado.", "error"); return redirect(url_for("admin_suppliers"))
    new_billing = 0 if s["billing"] else 1
    db_exec("UPDATE suppliers SET billing=:b WHERE id=:id", b=new_billing, id=sid)
    audit("supplier_toggle_billing", f"id={sid} billing={new_billing}")
    flash("Faturamento atualizado.", "success")
    return redirect(url_for("admin_suppliers"))

@app.route("/admin/suppliers/<int:sid>/delete", methods=["POST"])
def admin_suppliers_delete(sid):
    if require_role("admin"): return require_role("admin")
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
    if require_role("admin"): return require_role("admin")
    products = db_all("SELECT * FROM products ORDER BY kind, name")
    return render_template("admin_products.html", products=products)

@app.route("/admin/products/create", methods=["POST"])
def admin_products_create():
    if require_role("admin"): return require_role("admin")
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
    if require_role("admin"): return require_role("admin")
    p = db_one("SELECT * FROM products WHERE id=:id", id=pid)
    if not p: flash("Produto não encontrado.", "error"); return redirect(url_for("admin_products"))
    new_active = 0 if p["active"] else 1
    db_exec("UPDATE products SET active=:a WHERE id=:id", a=new_active, id=pid)
    audit("product_toggle", f"id={pid} active={new_active}")
    return redirect(url_for("admin_products"))

@app.route("/admin/products/<int:pid>/delete", methods=["POST"])
def admin_products_delete(pid):
    if require_role("admin"): return require_role("admin")
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
    if require_role("admin"): return require_role("admin")
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
    if require_role("admin"): return require_role("admin")
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
    if require_role("admin"): return require_role("admin")
    r = db_one("SELECT * FROM rules WHERE id=:id", id=rid)
    if not r: flash("Regra não encontrada.", "error"); return redirect(url_for("admin_rules"))
    new_active = 0 if r["active"] else 1
    db_exec("UPDATE rules SET active=:a WHERE id=:id", a=new_active, id=rid)
    audit("rule_toggle", f"id={rid} active={new_active}")
    return redirect(url_for("admin_rules"))

# >>> NOVA ROTA: Excluir regra definitivamente
@app.route("/admin/rules/<int:rid>/delete", methods=["POST"])
def admin_rules_delete(rid):
    if require_role("admin"): return require_role("admin")
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
    except ImportError:
        html = """
        {% extends "base.html" %}
        {% block title %}Template de Importação{% endblock %}
        {% block content %}
        <div class="container" style="max-width:800px;margin:0 auto">
          <h2>Template de Importação</h2>
          <p style="color:#b00"><strong>Dependência ausente:</strong> o servidor não tem <code>openpyxl</code> instalado.</p>
          <p>Adicione <code>openpyxl==3.1.5</code> ao <code>requirements.txt</code> e publique novamente.</p>
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

    ws2 = wb.create_sheet("Products")
    ws2.append(["name", "code", "kind", "active", "in_stock"])
    ws2.append(["Lente Asférica 1.67", "LA167", "lente", 1, 0])
    ws2.append(["Bloco Base 4", "BB4", "bloco", 1, 1])
    for cell in ws2[1]:
        cell.font = Font(bold=True)

    ws3 = wb.create_sheet("Rules")
    ws3.append(["product_name", "product_kind", "supplier_name", "max_price", "active"])
    ws3.append(["Lente Asférica 1.67", "lente", "Fornecedor Exemplo A", 250.00, 1])
    ws3.append(["Bloco Base 4", "bloco", "Fornecedor Exemplo B", 80.00, 1])
    for cell in ws3[1]:
        cell.font = Font(bold=True)

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(bio, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="optec_import_template.xlsx")

@app.route("/admin/import", methods=["GET", "POST"])
def admin_import():
    if require_role("admin"): return require_role("admin")

    report = {"suppliers": {"inserted":0, "updated":0},
              "products": {"inserted":0, "updated":0},
              "rules": {"inserted":0, "updated":0},
              "errors": []}

    if request.method == "POST":
        try:
            from openpyxl import load_workbook
        except ImportError:
            report["errors"].append("Dependência ausente: instale 'openpyxl' no servidor.")
            flash("Instale 'openpyxl' para importar planilhas .xlsx.", "error")
        else:
            file = request.files.get("file")
            if not file or file.filename == "":
                flash("Envie um arquivo .xlsx", "error")
            else:
                try:
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
                                    billing = int(row[i_billing]) if (i_billing != -1 and row[i_billing] is not None) else 1
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
                                            INSERT INTO suppliers (name, active, billing)
                                            VALUES (:n, 1, 1)
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
        <a class="btn primary" href="{{ url_for('admin_import_template') }}">Baixar Template</a>
      </div>
      <p>Use o modelo com abas <strong>Suppliers</strong>, <strong>Products</strong> e <strong>Rules</strong>.</p>
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

# -------- Compras / Pagamentos / Relatórios (demais rotas já enviadas) --------
# (mantidos exatamente como na sua versão anterior — não repeti aqui por brevidade)

# ============================ BOOTSTRAP ============================

try:
    init_db()
except Exception as e:
    print(f"[BOOT] init_db() falhou: {e}", flush=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
