import os
from datetime import datetime, date, time, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, Response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads", "deliveries")
REPORT_DIR = os.path.join(BASE_DIR, "reports")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///local_inventory.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR

db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default="admin")
    active = db.Column(db.Boolean, default=True)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, default=0)
    min_stock = db.Column(db.Integer, default=5)
    location = db.Column(db.String(120), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sticker = db.Column(db.String(60), unique=True, nullable=False)
    name = db.Column(db.String(160), nullable=False)
    position = db.Column(db.String(120), default="")
    company = db.Column(db.String(120), default="")
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class StockMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    movement_type = db.Column(db.String(30), nullable=False)  # entrada, salida, ajuste, entrega
    quantity = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.String(255), default="")
    date = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship("Product")


class Delivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    photo = db.Column(db.String(255), default="")
    date = db.Column(db.DateTime, default=datetime.utcnow)

    employee = db.relationship("Employee")
    product = db.relationship("Product")



CONTROL_KEYWORDS = {
    "guantes": ["guante", "glove"],
    "gafas_lentes": ["gafa", "lente", "glass", "safety glass", "eye"]
}


def product_control_type(product):
    text = f"{product.code} {product.name} {product.category}".lower()
    for control_type, words in CONTROL_KEYWORDS.items():
        if any(word in text for word in words):
            return control_type
    return "otro"


def week_range_for(dt=None):
    dt = dt or datetime.utcnow()
    start = datetime.combine((dt.date() - timedelta(days=dt.weekday())), time.min)
    end = start + timedelta(days=7)
    return start, end


def parse_date(value, default_date):
    if not value:
        return default_date
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return default_date


def delivery_query_between(start_date, end_date):
    start_dt = datetime.combine(start_date, time.min)
    end_dt = datetime.combine(end_date, time.max)
    return Delivery.query.filter(Delivery.date >= start_dt, Delivery.date <= end_dt)


def employee_control_summary(employee_id, start_date, end_date):
    deliveries = delivery_query_between(start_date, end_date).filter(Delivery.employee_id == employee_id).order_by(Delivery.date.desc()).all()
    summary = {
        "guantes_qty": 0,
        "guantes_deliveries": 0,
        "gafas_qty": 0,
        "gafas_deliveries": 0,
        "last_guantes": None,
        "last_gafas": None,
        "deliveries": deliveries,
    }
    for d in deliveries:
        control_type = product_control_type(d.product)
        if control_type == "guantes":
            summary["guantes_qty"] += d.quantity
            summary["guantes_deliveries"] += 1
            if not summary["last_guantes"] or d.date > summary["last_guantes"].date:
                summary["last_guantes"] = d
        elif control_type == "gafas_lentes":
            summary["gafas_qty"] += d.quantity
            summary["gafas_deliveries"] += 1
            if not summary["last_gafas"] or d.date > summary["last_gafas"].date:
                summary["last_gafas"] = d
    return summary


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username, active=True).first()
        if user and check_password_hash(user.password_hash, password):
            session["user_id"] = user.id
            session["username"] = user.username
            return redirect(url_for("index"))
        flash("Usuario o contraseña incorrectos", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    total_products = Product.query.count()
    total_employees = Employee.query.filter_by(active=True).count()
    low_stock = Product.query.filter(Product.quantity <= Product.min_stock).all()
    last_deliveries = Delivery.query.order_by(Delivery.date.desc()).limit(5).all()
    return render_template("index.html", total_products=total_products, total_employees=total_employees, low_stock=low_stock, last_deliveries=last_deliveries)


@app.route("/products")
@login_required
def products():
    q = request.args.get("q", "").strip()
    query = Product.query
    if q:
        query = query.filter((Product.code.ilike(f"%{q}%")) | (Product.name.ilike(f"%{q}%")) | (Product.category.ilike(f"%{q}%")))
    products = query.order_by(Product.code).all()
    return render_template("products.html", products=products, q=q)


@app.route("/products/new", methods=["GET", "POST"])
@login_required
def new_product():
    if request.method == "POST":
        product = Product(
            code=request.form["code"].strip().upper(),
            name=request.form["name"].strip(),
            category=request.form["category"].strip(),
            quantity=int(request.form.get("quantity", 0)),
            min_stock=int(request.form.get("min_stock", 5)),
            location=request.form.get("location", "").strip(),
        )
        db.session.add(product)
        try:
            db.session.commit()
            flash("Producto creado correctamente", "success")
            return redirect(url_for("products"))
        except Exception:
            db.session.rollback()
            flash("Ese código ya existe o hay un dato inválido", "danger")
    return render_template("product_form.html")


@app.route("/employees", methods=["GET", "POST"])
@login_required
def employees():
    if request.method == "POST":
        emp = Employee(
            sticker=request.form["sticker"].strip(),
            name=request.form["name"].strip(),
            position=request.form.get("position", "").strip(),
            company=request.form.get("company", "").strip(),
        )
        db.session.add(emp)
        try:
            db.session.commit()
            flash("Empleado creado correctamente", "success")
        except Exception:
            db.session.rollback()
            flash("Ese sticker ya existe", "danger")
        return redirect(url_for("employees"))
    employees = Employee.query.order_by(Employee.name).all()
    return render_template("employees.html", employees=employees)


@app.route("/stock", methods=["GET", "POST"])
@login_required
def stock():
    products = Product.query.order_by(Product.code).all()
    if request.method == "POST":
        product = Product.query.get_or_404(int(request.form["product_id"]))
        movement_type = request.form["movement_type"]
        qty = int(request.form["quantity"])
        notes = request.form.get("notes", "")

        if movement_type == "entrada":
            product.quantity += qty
        elif movement_type == "salida":
            if product.quantity < qty:
                flash("No hay suficiente stock", "danger")
                return redirect(url_for("stock"))
            product.quantity -= qty
        elif movement_type == "ajuste":
            product.quantity = qty

        movement = StockMovement(product_id=product.id, movement_type=movement_type, quantity=qty, notes=notes)
        db.session.add(movement)
        db.session.commit()
        flash("Movimiento guardado", "success")
        return redirect(url_for("products"))
    return render_template("stock.html", products=products)


@app.route("/deliveries", methods=["GET", "POST"])
@login_required
def deliveries():
    employees = Employee.query.filter_by(active=True).order_by(Employee.name).all()
    products = Product.query.order_by(Product.code).all()
    if request.method == "POST":
        employee = Employee.query.get_or_404(int(request.form["employee_id"]))
        product = Product.query.get_or_404(int(request.form["product_id"]))
        qty = int(request.form["quantity"])
        control_type = product_control_type(product)
        if control_type in ["guantes", "gafas_lentes"]:
            week_start, week_end = week_range_for()
            previous = Delivery.query.filter(
                Delivery.employee_id == employee.id,
                Delivery.date >= week_start,
                Delivery.date < week_end
            ).all()
            previous_qty = sum(d.quantity for d in previous if product_control_type(d.product) == control_type)
            if previous_qty > 0:
                item_name = "guantes" if control_type == "guantes" else "gafas/lentes"
                flash(f"Alerta: {employee.name} ya recibió {previous_qty} unidad(es) de {item_name} esta semana.", "warning")

        if product.quantity < qty:
            flash("No hay suficiente stock para la entrega", "danger")
            return redirect(url_for("deliveries"))

        photo_file = request.files.get("photo")
        photo_name = ""
        if photo_file and photo_file.filename:
            filename = secure_filename(photo_file.filename)
            photo_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
            photo_file.save(os.path.join(app.config["UPLOAD_FOLDER"], photo_name))

        product.quantity -= qty
        delivery = Delivery(employee_id=employee.id, product_id=product.id, quantity=qty, photo=photo_name)
        movement = StockMovement(product_id=product.id, movement_type="entrega", quantity=qty, notes=f"Entrega a sticker {employee.sticker} - {employee.name}")
        db.session.add(delivery)
        db.session.add(movement)
        db.session.commit()
        flash("Entrega guardada", "success")
        return redirect(url_for("history"))
    return render_template("delivery_form.html", employees=employees, products=products)


@app.route("/history")
@login_required
def history():
    deliveries = Delivery.query.order_by(Delivery.date.desc()).all()
    movements = StockMovement.query.order_by(StockMovement.date.desc()).limit(100).all()
    return render_template("history.html", deliveries=deliveries, movements=movements)


@app.route("/low-stock")
@login_required
def low_stock():
    products = Product.query.filter(Product.quantity <= Product.min_stock).order_by(Product.quantity).all()
    return render_template("low_stock.html", products=products)



@app.route("/reports")
@login_required
def reports():
    today = datetime.utcnow().date()
    start_default = today - timedelta(days=today.weekday())
    end_default = start_default + timedelta(days=6)
    start_date = parse_date(request.args.get("start"), start_default)
    end_date = parse_date(request.args.get("end"), end_default)
    employee_id = request.args.get("employee_id", "")

    employees = Employee.query.filter_by(active=True).order_by(Employee.name).all()
    selected_employee = None
    employee_summary = None

    if employee_id:
        selected_employee = Employee.query.get(int(employee_id))
        if selected_employee:
            employee_summary = employee_control_summary(selected_employee.id, start_date, end_date)

    rows = []
    for emp in employees:
        summary = employee_control_summary(emp.id, start_date, end_date)
        if summary["guantes_qty"] or summary["gafas_qty"]:
            rows.append({"employee": emp, **summary})

    rows.sort(key=lambda r: (r["guantes_qty"] + r["gafas_qty"]), reverse=True)
    return render_template(
        "reports.html",
        employees=employees,
        selected_employee=selected_employee,
        employee_summary=employee_summary,
        rows=rows,
        start_date=start_date,
        end_date=end_date,
    )


@app.route("/reports/export.csv")
@login_required
def reports_export_csv():
    today = datetime.utcnow().date()
    start_default = today - timedelta(days=today.weekday())
    end_default = start_default + timedelta(days=6)
    start_date = parse_date(request.args.get("start"), start_default)
    end_date = parse_date(request.args.get("end"), end_default)
    employees = Employee.query.filter_by(active=True).order_by(Employee.name).all()

    lines = ["Sticker,Empleado,Empresa,Cargo,Guantes unidades,Guantes entregas,Gafas/Lentes unidades,Gafas/Lentes entregas,Desde,Hasta"]
    for emp in employees:
        summary = employee_control_summary(emp.id, start_date, end_date)
        if summary["guantes_qty"] or summary["gafas_qty"]:
            lines.append(
                f'"{emp.sticker}","{emp.name}","{emp.company}","{emp.position}",{summary["guantes_qty"]},{summary["guantes_deliveries"]},{summary["gafas_qty"]},{summary["gafas_deliveries"]},{start_date},{end_date}'
            )
    csv_data = "\n".join(lines)
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=reporte_epp_{start_date}_{end_date}.csv"},
    )


@app.route("/delivery/<int:delivery_id>/pdf")
@login_required
def delivery_pdf(delivery_id):
    delivery = Delivery.query.get_or_404(delivery_id)
    pdf_path = os.path.join(REPORT_DIR, f"delivery_{delivery.id}.pdf")
    c = canvas.Canvas(pdf_path, pagesize=letter)
    width, height = letter
    y = height - 50
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, "Entrega de Dotación EPP - Santander")
    y -= 40
    c.setFont("Helvetica", 11)
    lines = [
        f"Fecha: {delivery.date.strftime('%Y-%m-%d %H:%M')}",
        f"Sticker empleado: {delivery.employee.sticker}",
        f"Empleado: {delivery.employee.name}",
        f"Cargo: {delivery.employee.position}",
        f"Empresa: {delivery.employee.company}",
        f"Código producto: {delivery.product.code}",
        f"Producto: {delivery.product.name}",
        f"Cantidad entregada: {delivery.quantity}",
    ]
    for line in lines:
        c.drawString(50, y, line)
        y -= 22
    y -= 30
    c.drawString(50, y, "Firma trabajador: ________________________________")
    y -= 35
    c.drawString(50, y, "Entregado por: _________________________________")
    c.save()
    return send_file(pdf_path, as_attachment=True)


def init_db():
    db.create_all()
    if not User.query.filter_by(username="admin").first():
        user = User(username="admin", password_hash=generate_password_hash("admin123"), role="admin")
        db.session.add(user)
        db.session.commit()


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True)
