# app.py
import os
import csv
import io
from datetime import datetime, date
from collections import defaultdict

from flask import (
    Flask, request, redirect, url_for, render_template_string,
    session, flash, jsonify, send_file
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, inspect, text

# -----------------------------------------------------------------------------
# Configuração
# -----------------------------------------------------------------------------
app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-in-prod")
DB_URL = os.environ.get("DATABASE_URL", "sqlite:///dados.db")
if DB_URL.startswith("postgres://"):
    # Render antigo usa "postgres://", SQLAlchemy espera "postgresql://"
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Credenciais simples via ENV (defina no Render)
COMPRADOR_USER = os.environ.get("COMPRADOR_USER", "comprador")
COMPRADOR_PASS = os.environ.get("COMPRADOR_PASS", "123")
PAGADOR_USER = os.environ.get("PAGADOR_USER", "pagador")
PAGADOR_PASS = os.environ.get("PAGADOR_PASS", "123")

db = SQLAlchemy(app)

# -----------------------------------------------------------------------------
# Modelos
# -----------------------------------------------------------------------------
class Fornecedor(db.Model):
    __tablename__ = "fornecedores"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), unique=True, nullable=False)

    def __repr__(self):
        return f"<Fornecedor {self.nome}>"


class Pedido(db.Model):
    __tablename__ = "pedidos"
    id = db.Column(db.Integer, primary_key=True)
    data_criacao = db.Column(db.Date, nullable=False, default=date.today)
    ordem_servico = db.Column(db.String(64), nullable=False)  # OS do serviço
    produto = db.Column(db.String(200), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False, default=1)
    valor = db.Column(db.Float, nullable=False, default=0.0)  # valor total do item
    observacao = db.Column(db.String(500), nullable=True)

    fornecedor_id = db.Column(db.Integer, db.ForeignKey("fornecedores.id"), nullable=False)
    fornecedor = db.relationship("Fornecedor", backref="pedidos", lazy=True)

    pago = db.Column(db.Boolean, nullable=False, default=False)
    data_pagamento = db.Column(db.Date, nullable=True)
    forma_pagamento = db.Column(db.String(30), nullable=True)   # PIX, TED, Boleto etc.
    comprovante = db.Column(db.String(200), nullable=True)      # opcional: referência/ID

    # Evita duplicidade de OS (por padrão única por OS; ajuste se quiser por dia/fornecedor).
    __table_args__ = (
        UniqueConstraint("ordem_servico", name="uq_pedidos_os"),
    )

    def __repr__(self):
        return f"<Pedido OS={self.ordem_servico} produto={self.produto}>"


# -----------------------------------------------------------------------------
# Utilitários/Migrações leves
# -----------------------------------------------------------------------------
def ensure_minimum_data():
    """Cria DB e alguns fornecedores iniciais se não existirem.
       Também faz migração leve de colunas faltantes no Postgres/SQLite."""
    db.create_all()

    # Migração leve: garantir colunas (caso DB antigo)
    insp = inspect(db.engine)
    cols = [c["name"] for c in insp.get_columns("pedidos")]
    missing = []
    if "pago" not in cols:
        missing.append("ADD COLUMN pago BOOLEAN NOT NULL DEFAULT 0")
    if "data_pagamento" not in cols:
        missing.append("ADD COLUMN data_pagamento DATE")
    if "forma_pagamento" not in cols:
        missing.append("ADD COLUMN forma_pagamento VARCHAR(30)")
    if "comprovante" not in cols:
        missing.append("ADD COLUMN comprovante VARCHAR(200)")
    if missing:
        with db.engine.begin() as conn:
            for alter in missing:
                conn.execute(text(f"ALTER TABLE pedidos {alter}"))

    if Fornecedor.query.count() == 0:
        for nome in ["Essilor", "Zeiss", "Hoya", "Saturn", "Transitions", "Outros"]:
            db.session.add(Fornecedor(nome=nome))
        db.session.commit()


# >>>>>>>>>>>> FLASK 3.x (sem before_first_request) <<<<<<<<<<<<<<
# Executa migração/seed uma única vez ao subir o app
with app.app_context():
    ensure_minimum_data()


def require_role(role):
    def wrapper(fn):
        def inner(*args, **kwargs):
            if session.get("role") != role:
                flash("Acesso negado para este perfil.", "danger")
                return redirect(url_for("index"))
            return fn(*args, **kwargs)
        inner.__name__ = fn.__name__
        return inner
    return wrapper


# -----------------------------------------------------------------------------
# Rotas: Autenticação simples por papel
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    role = session.get("role")
    return render_template_string(TEMPLATE_INDEX, role=role)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "").strip()
        if usuario == COMPRADOR_USER and senha == COMPRADOR_PASS:
            session["role"] = "comprador"
            flash("Login efetuado como COMPRADOR.", "success")
            return redirect(url_for("comprador"))
        if usuario == PAGADOR_USER and senha == PAGADOR_PASS:
            session["role"] = "pagador"
            flash("Login efetuado como PAGADOR.", "success")
            return redirect(url_for("pagador"))
        flash("Usuário ou senha inválidos.", "danger")
    return render_template_string(TEMPLATE_LOGIN)


@app.route("/logout")
def logout():
    session.clear()
    flash("Sessão encerrada.", "info")
    return redirect(url_for("index"))


# -----------------------------------------------------------------------------
# Rotas: COMPRADOR
# -----------------------------------------------------------------------------
@app.route("/comprador", methods=["GET"])
@require_role("comprador")
def comprador():
    fornecedores = Fornecedor.query.order_by(Fornecedor.nome.asc()).all()
    q = request.args.get("q", "").strip()
    base_query = Pedido.query
    if q:
        like = f"%{q}%"
        base_query = base_query.filter(
            db.or_(
                Pedido.ordem_servico.ilike(like),
                Pedido.produto.ilike(like),
                Pedido.observacao.ilike(like),
            )
        )
    pedidos = base_query.order_by(Pedido.id.desc()).limit(200).all()
    return render_template_string(TEMPLATE_COMPRADOR, fornecedores=fornecedores, pedidos=pedidos, q=q)


@app.route("/comprador/adicionar", methods=["POST"])
@require_role("comprador")
def comprador_adicionar():
    try:
        os_num = request.form.get("ordem_servico", "").strip()
        produto = request.form.get("produto", "").strip()
        quantidade = int(request.form.get("quantidade", "1") or "1")
        valor = float(str(request.form.get("valor", "0")).replace(",", ".") or "0")
        fornecedor_id = int(request.form.get("fornecedor_id"))
        observacao = request.form.get("observacao", "").strip()

        if not os_num or not produto:
            flash("Informe Ordem de Serviço e Produto.", "warning")
            return redirect(url_for("comprador"))

        # Checa duplicidade de OS (regra: OS única na tabela)
        existente = Pedido.query.filter_by(ordem_servico=os_num).first()
        if existente:
            flash(f"OS {os_num} já cadastrada (produto: {existente.produto}). Operação bloqueada.", "danger")
            return redirect(url_for("comprador"))

        ped = Pedido(
            data_criacao=date.today(),
            ordem_servico=os_num,
            produto=produto,
            quantidade=quantidade,
            valor=valor,
            fornecedor_id=fornecedor_id,
            observacao=observacao,
        )
        db.session.add(ped)
        db.session.commit()
        flash("Pedido incluído com sucesso.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Erro ao adicionar: {e}", "danger")
    return redirect(url_for("comprador"))


@app.route("/comprador/remover/<int:pedido_id>", methods=["POST"])
@require_role("comprador")
def comprador_remover(pedido_id):
    ped = Pedido.query.get_or_404(pedido_id)
    if ped.pago:
        flash("Não é possível remover um pedido já pago.", "warning")
        return redirect(url_for("comprador"))
    db.session.delete(ped)
    db.session.commit()
    flash("Pedido removido.", "info")
    return redirect(url_for("comprador"))


# -----------------------------------------------------------------------------
# Rotas: PAGADOR
# -----------------------------------------------------------------------------
@app.route("/pagador", methods=["GET"])
@require_role("pagador")
def pagador():
    # Lista em aberto agrupado por fornecedor
    pendentes = Pedido.query.filter_by(pago=False).order_by(Pedido.fornecedor_id.asc(), Pedido.id.desc()).all()
    grupos = defaultdict(list)
    total_por_forn = defaultdict(float)
    for p in pendentes:
        grupos[p.fornecedor.nome].append(p)
        total_por_forn[p.fornecedor.nome] += (p.valor or 0.0)
    total_geral = sum(total_por_forn.values())
    return render_template_string(
        TEMPLATE_PAGADOR,
        grupos=grupos,
        total_por_forn=total_por_forn,
        total_geral=total_geral,
        date=date  # para usar date.today() no template
    )


@app.route("/pagador/pagar/<int:pedido_id>", methods=["POST"])
@require_role("pagador")
def pagador_pagar(pedido_id):
    ped = Pedido.query.get_or_404(pedido_id)
    if ped.pago:
        flash("Este pedido já está pago.", "info")
        return redirect(url_for("pagador"))

    forma = request.form.get("forma_pagamento", "PIX").upper()
    comprovante = request.form.get("comprovante", "").strip()
    data_pag = request.form.get("data_pagamento", "") or date.today().isoformat()

    try:
        ped.pago = True
        ped.forma_pagamento = forma
        ped.comprovante = comprovante or None
        ped.data_pagamento = datetime.fromisoformat(data_pag).date()
        db.session.commit()
        flash(f"Pagamento registrado (OS {ped.ordem_servico}).", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Erro ao registrar pagamento: {e}", "danger")

    return redirect(url_for("pagador"))


# -----------------------------------------------------------------------------
# Relatórios
# -----------------------------------------------------------------------------
@app.route("/relatorio", methods=["GET"])
def relatorio():
    # Acesso: comprador ou pagador
    if session.get("role") not in ("comprador", "pagador"):
        flash("Faça login para ver relatórios.", "warning")
        return redirect(url_for("index"))

    data_str = request.args.get("data", date.today().isoformat())
    try:
        d = datetime.fromisoformat(data_str).date()
    except Exception:
        d = date.today()

    pagos = Pedido.query.filter(
        Pedido.pago.is_(True),
        Pedido.data_pagamento == d
    ).order_by(Pedido.fornecedor_id.asc()).all()

    grupos = defaultdict(list)
    total_por_forn = defaultdict(float)
    total_geral = 0.0

    for p in pagos:
        grupos[p.fornecedor.nome].append(p)
        total_por_forn[p.fornecedor.nome] += (p.valor or 0.0)
        total_geral += (p.valor or 0.0)

    return render_template_string(TEMPLATE_RELATORIO, d=d, grupos=grupos, total_por_forn=total_por_forn, total_geral=total_geral)


@app.route("/relatorio/csv", methods=["GET"])
def relatorio_csv():
    # Protegido
    if session.get("role") not in ("comprador", "pagador"):
        flash("Faça login para exportar relatórios.", "warning")
        return redirect(url_for("index"))

    data_str = request.args.get("data", date.today().isoformat())
    try:
        d = datetime.fromisoformat(data_str).date()
    except Exception:
        d = date.today()

    pagos = Pedido.query.filter(
        Pedido.pago.is_(True),
        Pedido.data_pagamento == d
    ).order_by(Pedido.fornecedor_id.asc()).all()

    si = io.StringIO()
    cw = csv.writer(si, delimiter=";")
    cw.writerow(["Data Pagamento", "Fornecedor", "OS", "Produto", "Qtd", "Valor", "Forma", "Comprovante", "Observação"])
    for p in pagos:
        cw.writerow([
            p.data_pagamento.isoformat() if p.data_pagamento else "",
            p.fornecedor.nome,
            p.ordem_servico,
            p.produto,
            p.quantidade,
            f"{p.valor:.2f}",
            p.forma_pagamento or "",
            p.comprovante or "",
            (p.observacao or "").replace("\n", " ").strip()
        ])

    data_bytes = io.BytesIO(si.getvalue().encode("utf-8-sig"))
    filename = f"relatorio_{d.isoformat()}.csv"
    return send_file(
        data_bytes,
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename
    )


# -----------------------------------------------------------------------------
# APIs simples (para integrações futuras)
# -----------------------------------------------------------------------------
@app.route("/api/pedidos", methods=["GET"])
def api_pedidos():
    status = request.args.get("status", "todos")
    q = Pedido.query
    if status == "pendentes":
        q = q.filter_by(pago=False)
    elif status == "pagos":
        q = q.filter_by(pago=True)
    itens = q.order_by(Pedido.id.desc()).limit(500).all()
    return jsonify([serialize_pedido(p) for p in itens])


@app.route("/api/pagamentos", methods=["GET"])
def api_pagamentos():
    data_str = request.args.get("data")
    q = Pedido.query.filter_by(pago=True)
    if data_str:
        try:
            d = datetime.fromisoformat(data_str).date()
            q = q.filter(Pedido.data_pagamento == d)
        except Exception:
            pass
    itens = q.order_by(Pedido.data_pagamento.desc(), Pedido.id.desc()).limit(500).all()
    return jsonify([serialize_pedido(p) for p in itens])


def serialize_pedido(p: Pedido):
    return {
        "id": p.id,
        "data_criacao": p.data_criacao.isoformat() if p.data_criacao else None,
        "ordem_servico": p.ordem_servico,
        "produto": p.produto,
        "quantidade": p.quantidade,
        "valor": p.valor,
        "fornecedor": p.fornecedor.nome if p.fornecedor else None,
        "pago": p.pago,
        "data_pagamento": p.data_pagamento.isoformat() if p.data_pagamento else None,
        "forma_pagamento": p.forma_pagamento,
        "comprovante": p.comprovante,
        "observacao": p.observacao
    }


# -----------------------------------------------------------------------------
# Templates (Jinja inline)
# -----------------------------------------------------------------------------
BASE_HEAD = """
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <title>Sistema de Pedidos & Pagamentos</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
    rel="stylesheet">
  <style>
    body { padding-top: 70px; }
    .nowrap { white-space: nowrap; }
    .small { font-size: 0.9rem; }
    .table-sm td, .table-sm th { padding: .35rem; }
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark fixed-top">
  <div class="container-fluid">
    <a class="navbar-brand" href="{{ url_for('index') }}">Pedidos & Pagamentos</a>
    <div class="collapse navbar-collapse">
      <ul class="navbar-nav me-auto mb-2 mb-lg-0">
        {% if session.get('role') == 'comprador' %}
        <li class="nav-item"><a class="nav-link" href="{{ url_for('comprador') }}">Comprador</a></li>
        {% endif %}
        {% if session.get('role') == 'pagador' %}
        <li class="nav-item"><a class="nav-link" href="{{ url_for('pagador') }}">Pagador</a></li>
        {% endif %}
        {% if session.get('role') in ('comprador','pagador') %}
        <li class="nav-item"><a class="nav-link" href="{{ url_for('relatorio') }}">Relatório</a></li>
        {% endif %}
      </ul>
      <ul class="navbar-nav">
        {% if session.get('role') %}
        <li class="nav-item"><span class="navbar-text text-white me-3">Perfil: {{ session.get('role')|capitalize }}</span></li>
        <li class="nav-item"><a class="btn btn-outline-light btn-sm" href="{{ url_for('logout') }}">Sair</a></li>
        {% else %}
        <li class="nav-item"><a class="btn btn-outline-light btn-sm" href="{{ url_for('login') }}">Entrar</a></li>
        {% endif %}
      </ul>
    </div>
  </div>
</nav>
<div class="container">
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      <div class="mt-2">
      {% for category, msg in messages %}
        <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
          {{ msg }}
          <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
        </div>
      {% endfor %}
      </div>
    {% endif %}
  {% endwith %}
  {% block conteudo %}{% endblock %}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>
"""

TEMPLATE_INDEX = BASE_HEAD.replace("{% block conteudo %}{% endblock %}", """
{% block conteudo %}
  <div class="p-4 bg-light rounded">
    <h1 class="h3 mb-3">Bem-vindo</h1>
    {% if role %}
      <p>Você está logado como <strong>{{ role }}</strong>.</p>
      <p class="mb-0">
        {% if role == 'comprador' %}
          <a class="btn btn-primary" href="{{ url_for('comprador') }}">Ir para Comprador</a>
        {% elif role == 'pagador' %}
          <a class="btn btn-primary" href="{{ url_for('pagador') }}">Ir para Pagador</a>
        {% endif %}
      </p>
    {% else %}
      <p>Entre com seu perfil para começar.</p>
      <a class="btn btn-primary" href="{{ url_for('login') }}">Fazer Login</a>
    {% endif %}
  </div>
{% endblock %}
""")

TEMPLATE_LOGIN = BASE_HEAD.replace("{% block conteudo %}{% endblock %}", """
{% block conteudo %}
  <div class="row justify-content-center">
    <div class="col-md-5">
      <div class="card shadow-sm">
        <div class="card-body">
          <h1 class="h4 mb-3">Login</h1>
          <form method="post" autocomplete="off">
            <div class="mb-3">
              <label class="form-label">Usuário</label>
              <input name="usuario" class="form-control" placeholder="comprador ou pagador" required>
            </div>
            <div class="mb-3">
              <label class="form-label">Senha</label>
              <input name="senha" type="password" class="form-control" required>
            </div>
            <button class="btn btn-primary w-100">Entrar</button>
            <div class="form-text mt-2">
              Dica (dev): usuário/senha padrão: comprador/123 ou pagador/123 (ajuste por ENV).
            </div>
          </form>
        </div>
      </div>
    </div>
  </div>
{% endblock %}
""")

TEMPLATE_COMPRADOR = BASE_HEAD.replace("{% block conteudo %}{% endblock %}", """
{% block conteudo %}
  <div class="row g-3">
    <div class="col-lg-5">
      <div class="card shadow-sm">
        <div class="card-body">
          <h2 class="h5 mb-3">Nova solicitação de compra</h2>
          <form method="post" action="{{ url_for('comprador_adicionar') }}">
            <div class="mb-2">
              <label class="form-label">Ordem de Serviço (OS) *</label>
              <input name="ordem_servico" class="form-control" required>
              <div class="form-text">OS é única. Se repetir, o sistema bloqueia.</div>
            </div>
            <div class="mb-2">
              <label class="form-label">Produto *</label>
              <input name="produto" class="form-control" required>
            </div>
            <div class="row">
              <div class="col-4 mb-2">
                <label class="form-label">Quantidade</label>
                <input name="quantidade" type="number" min="1" value="1" class="form-control">
              </div>
              <div class="col-8 mb-2">
                <label class="form-label">Valor total (R$)</label>
                <input name="valor" inputmode="decimal" class="form-control" placeholder="0,00">
              </div>
            </div>
            <div class="mb-2">
              <label class="form-label">Fornecedor</label>
              <select name="fornecedor_id" class="form-select">
                {% for f in fornecedores %}
                  <option value="{{ f.id }}">{{ f.nome }}</option>
                {% endfor %}
              </select>
            </div>
            <div class="mb-3">
              <label class="form-label">Observação</label>
              <textarea name="observacao" class="form-control" rows="2"></textarea>
            </div>
            <button class="btn btn-primary">Adicionar</button>
          </form>
        </div>
      </div>
    </div>

    <div class="col-lg-7">
      <div class="card shadow-sm">
        <div class="card-body">
          <div class="d-flex justify-content-between align-items-center mb-2">
            <h2 class="h5 mb-0">Pedidos cadastrados (últimos)</h2>
            <form class="d-flex" method="get" action="{{ url_for('comprador') }}">
              <input class="form-control form-control-sm me-2" name="q" value="{{ q }}" placeholder="Buscar por OS/Produto/Obs">
              <button class="btn btn-outline-secondary btn-sm">Buscar</button>
            </form>
          </div>
          <div class="table-responsive">
            <table class="table table-sm table-striped align-middle">
              <thead class="table-light">
                <tr>
                  <th>#</th>
                  <th>Data</th>
                  <th>OS</th>
                  <th>Produto</th>
                  <th class="text-end">Qtd</th>
                  <th class="text-end">Valor</th>
                  <th>Fornecedor</th>
                  <th>Status</th>
                  <th class="text-end">Ações</th>
                </tr>
              </thead>
              <tbody>
                {% for p in pedidos %}
                <tr>
                  <td>{{ p.id }}</td>
                  <td class="nowrap">{{ p.data_criacao.strftime('%d/%m/%Y') }}</td>
                  <td class="nowrap">{{ p.ordem_servico }}</td>
                  <td>{{ p.produto }}</td>
                  <td class="text-end">{{ p.quantidade }}</td>
                  <td class="text-end">{{ 'R$ {:.2f}'.format(p.valor or 0) }}</td>
                  <td>{{ p.fornecedor.nome }}</td>
                  <td>
                    {% if p.pago %}
                      <span class="badge bg-success">Pago</span>
                    {% else %}
                      <span class="badge bg-warning text-dark">Pendente</span>
                    {% endif %}
                  </td>
                  <td class="text-end">
                    {% if not p.pago %}
                      <form method="post" action="{{ url_for('comprador_remover', pedido_id=p.id) }}" onsubmit="return confirm('Remover este pedido?')">
                        <button class="btn btn-sm btn-outline-danger">Remover</button>
                      </form>
                    {% else %}
                      <span class="text-muted small">—</span>
                    {% endif %}
                  </td>
                </tr>
                {% else %}
                <tr><td colspan="9" class="text-center text-muted">Nenhum pedido.</td></tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
          <div class="small text-muted">* A duplicidade por OS é bloqueada no cadastro.</div>
        </div>
      </div>
    </div>
  </div>
{% endblock %}
""")

TEMPLATE_PAGADOR = BASE_HEAD.replace("{% block conteudo %}{% endblock %}", """
{% block conteudo %}
  <div class="card shadow-sm">
    <div class="card-body">
      <h2 class="h5">Pagamentos pendentes</h2>
      {% if grupos %}
        {% for fornecedor, itens in grupos.items() %}
          <h3 class="h6 mt-4">{{ fornecedor }}</h3>
          <div class="table-responsive">
            <table class="table table-sm table-bordered align-middle">
              <thead class="table-light">
                <tr>
                  <th>#</th>
                  <th>Data</th>
                  <th>OS</th>
                  <th>Produto</th>
                  <th class="text-end">Qtd</th>
                  <th class="text-end">Valor</th>
                  <th>Obs</th>
                  <th class="text-center">Pagar</th>
                </tr>
              </thead>
              <tbody>
                {% for p in itens %}
                <tr>
                  <td>{{ p.id }}</td>
                  <td class="nowrap">{{ p.data_criacao.strftime('%d/%m/%Y') }}</td>
                  <td class="nowrap">{{ p.ordem_servico }}</td>
                  <td>{{ p.produto }}</td>
                  <td class="text-end">{{ p.quantidade }}</td>
                  <td class="text-end">{{ 'R$ {:.2f}'.format(p.valor or 0) }}</td>
                  <td class="small">{{ p.observacao }}</td>
                  <td class="text-center">
                    <form method="post" action="{{ url_for('pagador_pagar', pedido_id=p.id) }}" class="d-flex gap-1">
                      <input type="date" name="data_pagamento" class="form-control form-control-sm" value="{{ date.today().isoformat() }}">
                      <select name="forma_pagamento" class="form-select form-select-sm">
                        <option>PIX</option>
                        <option>TED</option>
                        <option>Boleto</option>
                        <option>Dinheiro</option>
                        <option>Cartão</option>
                      </select>
                      <input name="comprovante" class="form-control form-control-sm" placeholder="Ref/Comprovante (opcional)">
                      <button class="btn btn-sm btn-success">Baixar</button>
                    </form>
                  </td>
                </tr>
                {% endfor %}
                <tr class="table-secondary">
                  <td colspan="5"><strong>Total do fornecedor</strong></td>
                  <td class="text-end"><strong>{{ 'R$ {:.2f}'.format(total_por_forn[fornecedor]) }}</strong></td>
                  <td colspan="2"></td>
                </tr>
              </tbody>
            </table>
          </div>
        {% endfor %}
        <div class="mt-3">
          <span class="badge bg-primary fs-6">Total geral pendente: {{ 'R$ {:.2f}'.format(total_geral) }}</span>
        </div>
      {% else %}
        <p class="text-muted">Não há pendências de pagamento.</p>
      {% endif %}
    </div>
  </div>
{% endblock %}
""")

TEMPLATE_RELATORIO = BASE_HEAD.replace("{% block conteudo %}{% endblock %}", """
{% block conteudo %}
  <div class="card shadow-sm">
    <div class="card-body">
      <div class="d-flex align-items-end justify-content-between">
        <div>
          <h2 class="h5 mb-1">Relatório de pagamentos por fornecedor</h2>
          <div class="text-muted">Data: <strong>{{ d.strftime('%d/%m/%Y') }}</strong></div>
        </div>
        <form class="d-flex" method="get" action="{{ url_for('relatorio') }}">
          <input type="date" name="data" value="{{ d.isoformat() }}" class="form-control form-control-sm me-2">
          <button class="btn btn-outline-secondary btn-sm">Aplicar</button>
        </form>
      </div>

      <div class="mt-3">
        {% if grupos %}
          {% for fornecedor, itens in grupos.items() %}
            <h3 class="h6 mt-3">{{ fornecedor }}</h3>
            <div class="table-responsive">
              <table class="table table-sm table-striped">
                <thead class="table-light">
                  <tr>
                    <th>#</th>
                    <th>OS</th>
                    <th>Produto</th>
                    <th class="text-end">Qtd</th>
                    <th class="text-end">Valor</th>
                    <th>Forma</th>
                    <th>Comprovante</th>
                    <th>Obs</th>
                  </tr>
                </thead>
                <tbody>
                  {% for p in itens %}
                    <tr>
                      <td>{{ p.id }}</td>
                      <td class="nowrap">{{ p.ordem_servico }}</td>
                      <td>{{ p.produto }}</td>
                      <td class="text-end">{{ p.quantidade }}</td>
                      <td class="text-end">{{ 'R$ {:.2f}'.format(p.valor or 0) }}</td>
                      <td>{{ p.forma_pagamento or '' }}</td>
                      <td class="small">{{ p.comprovante or '' }}</td>
                      <td class="small">{{ p.observacao or '' }}</td>
                    </tr>
                  {% endfor %}
                  <tr class="table-secondary">
                    <td colspan="4"><strong>Total do fornecedor</strong></td>
                    <td class="text-end"><strong>{{ 'R$ {:.2f}'.format(total_por_forn[fornecedor]) }}</strong></td>
                    <td colspan="3"></td>
                  </tr>
                </tbody>
              </table>
            </div>
          {% endfor %}
          <div class="mt-2">
            <span class="badge bg-primary fs-6">Total geral do dia: {{ 'R$ {:.2f}'.format(total_geral) }}</span>
          </div>
          <div class="mt-3">
            <a class="btn btn-sm btn-outline-primary" href="{{ url_for('relatorio_csv', data=d.isoformat()) }}">Exportar CSV</a>
          </div>
        {% else %}
          <p class="text-muted">Sem pagamentos para a data selecionada.</p>
        {% endif %}
      </div>
    </div>
  </div>
{% endblock %}
""")

# -----------------------------------------------------------------------------
# Execução
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
