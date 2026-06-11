import csv
import io
import os
from collections import defaultdict
from datetime import datetime, date, time, timedelta
from functools import wraps

from flask import Flask, Response, flash, render_template, request, redirect, send_file, session, url_for
from flask_sqlalchemy import SQLAlchemy
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

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


class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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
    "gafas_lentes": ["gafa", "lente", "glass", "safety glass", "eye"],
}


def product_control_type(product):
    text = f"{product.code} {product.name} {product.category}".lower()
    for control_type, words in CONTROL_KEYWORDS.items():
        if any(word in text for word in words):
            return control_type
    return "otro"


def parse_date(value, default_date):
    if not value:
        return default_date
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return default_date


def default_month_range():
    today = datetime.utcnow().date()
    start = today.replace(day=1)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month - timedelta(days=1)
    return start, end


def default_week_range():
    today = datetime.utcnow().date()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    return start, end


def date_range_from_request(default="month"):
    if default == "week":
        start_default, end_default = default_week_range()
    else:
        start_default, end_default = default_month_range()
    start_date = parse_date(request.args.get("start"), start_default)
    end_date = parse_date(request.args.get("end"), end_default)
    return start_date, end_date


def datetime_bounds(start_date, end_date):
    return datetime.combine(start_date, time.min), datetime.combine(end_date, time.max)


def delivery_query_between(start_date, end_date):
    start_dt, end_dt = datetime_bounds(start_date, end_date)
    return Delivery.query.filter(Delivery.date >= start_dt, Delivery.date <= end_dt)


def movement_query_between(start_date, end_date):
    start_dt, end_dt = datetime_bounds(start_date, end_date)
    return StockMovement.query.filter(StockMovement.date >= start_dt, StockMovement.date <= end_dt)


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
    start_month, end_month = default_month_range()
    total_products = Product.query.count()
    total_companies = Company.query.count()
    total_employees = Employee.query.filter_by(active=True).count()
    low_stock = Product.query.filter(Product.quantity <= Product.min_stock).all()
    last_deliveries = Delivery.query.order_by(Delivery.date.desc()).limit(5).all()
    month_deliveries = delivery_query_between(start_month, end_month).count()
    month_entries = movement_query_between(start_month, end_month).filter(StockMovement.movement_type == "entrada").count()
    return render_template(
        "index.html",
        total_products=total_products,
        total_employees=total_employees,
        total_companies=total_companies,
        low_stock=low_stock,
        last_deliveries=last_deliveries,
        month_deliveries=month_deliveries,
        month_entries=month_entries,
    )


@app.route("/companies", methods=["GET", "POST"])
@login_required
def companies():
    if request.method == "POST":
        name = request.form["name"].strip()
        if not name:
            flash("El nombre de la empresa es obligatorio", "danger")
            return redirect(url_for("companies"))
        company = Company(name=name)
        db.session.add(company)
        try:
            db.session.commit()
            flash("Empresa creada correctamente", "success")
        except Exception:
            db.session.rollback()
            flash("La empresa ya existe", "danger")
        return redirect(url_for("companies"))
    companies = Company.query.order_by(Company.name).all()
    return render_template("companies.html", companies=companies)


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


@app.route("/products/<int:product_id>/stock/<movement_type>", methods=["GET", "POST"])
@login_required
def product_stock(product_id, movement_type):
    if movement_type not in ["entrada", "salida", "ajuste"]:
        flash("Movimiento inválido", "danger")
        return redirect(url_for("products"))
    product = Product.query.get_or_404(product_id)
    if request.method == "POST":
        qty = int(request.form.get("quantity", 0))
        notes = request.form.get("notes", "").strip()
        if qty < 0:
            flash("La cantidad no puede ser negativa", "danger")
            return redirect(url_for("product_stock", product_id=product.id, movement_type=movement_type))
        if movement_type == "entrada":
            product.quantity += qty
            movement_qty = qty
        elif movement_type == "salida":
            if product.quantity < qty:
                flash("No hay suficiente stock", "danger")
                return redirect(url_for("product_stock", product_id=product.id, movement_type=movement_type))
            product.quantity -= qty
            movement_qty = qty
        else:
            product.quantity = qty
            movement_qty = qty
        movement = StockMovement(product_id=product.id, movement_type=movement_type, quantity=movement_qty, notes=notes)
        db.session.add(movement)
        db.session.commit()
        flash("Stock actualizado correctamente", "success")
        return redirect(url_for("products"))
    return render_template("stock_product.html", product=product, movement_type=movement_type)


@app.route("/employees", methods=["GET", "POST"])
@login_required
def employees():
    if request.method == "POST":
        emp = Employee(
            sticker=request.form["sticker"].strip(),
            name=request.form["name"].strip(),
            position=request.form.get("position", "").strip(),
            company=request.form.get("company", "").strip(),
            active=True,
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
    companies = Company.query.filter_by(active=True).order_by(Company.name).all()
    return render_template("employees.html", employees=employees, companies=companies)


@app.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
@login_required
def edit_employee(employee_id):
    employee = Employee.query.get_or_404(employee_id)
    companies = Company.query.filter_by(active=True).order_by(Company.name).all()
    if request.method == "POST":
        employee.sticker = request.form["sticker"].strip()
        employee.name = request.form["name"].strip()
        employee.position = request.form.get("position", "").strip()
        employee.company = request.form.get("company", "").strip()
        employee.active = request.form.get("active") == "1"
        try:
            db.session.commit()
            flash("Empleado actualizado correctamente", "success")
            return redirect(url_for("employees"))
        except Exception:
            db.session.rollback()
            flash("No se pudo actualizar. Verifica que el sticker no esté repetido.", "danger")
    return render_template("employee_edit.html", employee=employee, companies=companies)


@app.route("/employees/<int:employee_id>/toggle")
@login_required
def toggle_employee(employee_id):
    employee = Employee.query.get_or_404(employee_id)
    employee.active = not employee.active
    db.session.commit()
    flash("Estado del empleado actualizado", "success")
    return redirect(url_for("employees"))


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
        else:
            flash("Movimiento inválido", "danger")
            return redirect(url_for("stock"))

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
            week_start, week_end_date = default_week_range()
            week_end_dt = datetime.combine(week_end_date, time.max)
            week_start_dt = datetime.combine(week_start, time.min)
            previous = Delivery.query.filter(
                Delivery.employee_id == employee.id,
                Delivery.date >= week_start_dt,
                Delivery.date <= week_end_dt,
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
    start_date, end_date = date_range_from_request(default="week")
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
    start_date, end_date = date_range_from_request(default="week")
    employees = Employee.query.filter_by(active=True).order_by(Employee.name).all()
    lines = ["Sticker,Empleado,Empresa,Cargo,Guantes unidades,Guantes entregas,Gafas/Lentes unidades,Gafas/Lentes entregas,Desde,Hasta"]
    for emp in employees:
        summary = employee_control_summary(emp.id, start_date, end_date)
        if summary["guantes_qty"] or summary["gafas_qty"]:
            lines.append(
                f'"{emp.sticker}","{emp.name}","{emp.company}","{emp.position}",{summary["guantes_qty"]},{summary["guantes_deliveries"]},{summary["gafas_qty"]},{summary["gafas_deliveries"]},{start_date},{end_date}'
            )
    return Response("\n".join(lines), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=reporte_epp_{start_date}_{end_date}.csv"})


@app.route("/reports/company")
@login_required
def company_report():
    start_date, end_date = date_range_from_request(default="month")
    deliveries_q = delivery_query_between(start_date, end_date)
    deliveries = deliveries_q.order_by(Delivery.date.desc()).all()
    company_filter = request.args.get("company", "").strip()

    company_data = {}
    product_names = set()
    for d in deliveries:
        company = (d.employee.company or "Sin empresa").strip() or "Sin empresa"
        if company_filter and company != company_filter:
            continue
        product_label = f"{d.product.code} - {d.product.name}"
        product_names.add(product_label)
        if company not in company_data:
            company_data[company] = {
                "employees": set(),
                "deliveries": 0,
                "units": 0,
                "guantes": 0,
                "gafas": 0,
                "products": defaultdict(int),
            }
        row = company_data[company]
        row["employees"].add(d.employee.id)
        row["deliveries"] += 1
        row["units"] += d.quantity
        row["products"][product_label] += d.quantity
        control = product_control_type(d.product)
        if control == "guantes":
            row["guantes"] += d.quantity
        elif control == "gafas_lentes":
            row["gafas"] += d.quantity

    rows = []
    for company, data in company_data.items():
        rows.append({
            "company": company,
            "employees": len(data["employees"]),
            "deliveries": data["deliveries"],
            "units": data["units"],
            "guantes": data["guantes"],
            "gafas": data["gafas"],
            "products": dict(data["products"]),
        })
    rows.sort(key=lambda r: r["units"], reverse=True)
    company_names = sorted({(e.company or "Sin empresa").strip() or "Sin empresa" for e in Employee.query.all()})
    return render_template("company_report.html", rows=rows, start_date=start_date, end_date=end_date, company_names=company_names, company_filter=company_filter)


@app.route("/reports/company/export.csv")
@login_required
def company_report_export_csv():
    start_date, end_date = date_range_from_request(default="month")
    deliveries = delivery_query_between(start_date, end_date).all()
    data = defaultdict(lambda: {"employees": set(), "deliveries": 0, "units": 0, "guantes": 0, "gafas": 0})
    for d in deliveries:
        company = (d.employee.company or "Sin empresa").strip() or "Sin empresa"
        data[company]["employees"].add(d.employee.id)
        data[company]["deliveries"] += 1
        data[company]["units"] += d.quantity
        control = product_control_type(d.product)
        if control == "guantes":
            data[company]["guantes"] += d.quantity
        elif control == "gafas_lentes":
            data[company]["gafas"] += d.quantity
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Empresa", "Empleados", "Entregas", "Unidades", "Guantes", "Gafas/Lentes", "Desde", "Hasta"])
    for company, row in sorted(data.items(), key=lambda item: item[1]["units"], reverse=True):
        writer.writerow([company, len(row["employees"]), row["deliveries"], row["units"], row["guantes"], row["gafas"], start_date, end_date])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=reporte_empresas_{start_date}_{end_date}.csv"})


@app.route("/reports/product")
@login_required
def product_report():
    start_date, end_date = date_range_from_request(default="month")
    rows = []
    for product in Product.query.order_by(Product.code).all():
        movements = movement_query_between(start_date, end_date).filter(StockMovement.product_id == product.id).all()
        entradas = sum(m.quantity for m in movements if m.movement_type == "entrada")
        salidas = sum(m.quantity for m in movements if m.movement_type in ["salida", "entrega"])
        ajustes = sum(1 for m in movements if m.movement_type == "ajuste")
        if entradas or salidas or ajustes or product.quantity <= product.min_stock:
            rows.append({
                "product": product,
                "entradas": entradas,
                "salidas": salidas,
                "ajustes": ajustes,
                "stock": product.quantity,
                "low": product.quantity <= product.min_stock,
            })
    rows.sort(key=lambda r: (r["low"], r["salidas"]), reverse=True)
    return render_template("product_report.html", rows=rows, start_date=start_date, end_date=end_date)


@app.route("/reports/employee")
@login_required
def employee_report():
    start_date, end_date = date_range_from_request(default="month")
    employee_id = request.args.get("employee_id", "")
    employees = Employee.query.order_by(Employee.name).all()
    selected_employee = Employee.query.get(int(employee_id)) if employee_id else None
    deliveries = []
    totals = defaultdict(int)
    if selected_employee:
        deliveries = delivery_query_between(start_date, end_date).filter(Delivery.employee_id == selected_employee.id).order_by(Delivery.date.desc()).all()
        for d in deliveries:
            totals[f"{d.product.code} - {d.product.name}"] += d.quantity
    return render_template("employee_report.html", employees=employees, selected_employee=selected_employee, deliveries=deliveries, totals=dict(totals), start_date=start_date, end_date=end_date)


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


@app.route("/sw.js")
def service_worker():
    return app.send_static_file("sw.js")


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
