import os
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
REPORT_FOLDER = os.path.join(BASE_DIR, "reports")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(REPORT_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

DATABASE_URL = os.environ.get("DATABASE_URL")

# Local fallback only for testing on your computer.
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///local_inventory.db"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, default=0)
    min_stock = db.Column(db.Integer, default=5)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    position = db.Column(db.String(100))
    company = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Delivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    photo_filename = db.Column(db.String(255))
    notes = db.Column(db.Text)
    delivered_at = db.Column(db.DateTime, default=datetime.utcnow)

    employee = db.relationship("Employee")
    product = db.relationship("Product")


class StockMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    movement_type = db.Column(db.String(20), nullable=False)  # IN / OUT
    quantity = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship("Product")


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in {"png", "jpg", "jpeg", "webp"}


@app.route("/")
def index():
    total_products = Product.query.count()
    total_employees = Employee.query.count()
    total_deliveries = Delivery.query.count()
    low_stock_products = Product.query.filter(Product.quantity <= Product.min_stock).order_by(Product.name).all()
    recent_deliveries = Delivery.query.order_by(Delivery.delivered_at.desc()).limit(8).all()
    return render_template(
        "index.html",
        total_products=total_products,
        total_employees=total_employees,
        total_deliveries=total_deliveries,
        low_stock_products=low_stock_products,
        recent_deliveries=recent_deliveries,
    )


@app.route("/products")
def products():
    products = Product.query.order_by(Product.name).all()
    return render_template("products.html", products=products)


@app.route("/products/add", methods=["GET", "POST"])
def add_product():
    if request.method == "POST":
        product = Product(
            name=request.form.get("name", "").strip(),
            category=request.form.get("category", "EPP").strip(),
            quantity=int(request.form.get("quantity", 0)),
            min_stock=int(request.form.get("min_stock", 5)),
        )
        db.session.add(product)
        db.session.commit()
        flash("Producto agregado correctamente.", "success")
        return redirect(url_for("products"))
    return render_template("product_form.html")


@app.route("/products/<int:product_id>/stock", methods=["POST"])
def update_stock(product_id):
    product = Product.query.get_or_404(product_id)
    movement_type = request.form.get("movement_type")
    quantity = int(request.form.get("quantity", 0))
    notes = request.form.get("notes", "")

    if quantity <= 0:
        flash("La cantidad debe ser mayor a cero.", "danger")
        return redirect(url_for("products"))

    if movement_type == "IN":
        product.quantity += quantity
    elif movement_type == "OUT":
        if product.quantity < quantity:
            flash("No hay suficiente stock para retirar esa cantidad.", "danger")
            return redirect(url_for("products"))
        product.quantity -= quantity
    else:
        flash("Tipo de movimiento no válido.", "danger")
        return redirect(url_for("products"))

    movement = StockMovement(product_id=product.id, movement_type=movement_type, quantity=quantity, notes=notes)
    db.session.add(movement)
    db.session.commit()
    flash("Stock actualizado correctamente.", "success")
    return redirect(url_for("products"))


@app.route("/employees", methods=["GET", "POST"])
def employees():
    if request.method == "POST":
        employee = Employee(
            name=request.form.get("name", "").strip(),
            position=request.form.get("position", "").strip(),
            company=request.form.get("company", "").strip(),
        )
        db.session.add(employee)
        db.session.commit()
        flash("Empleado agregado correctamente.", "success")
        return redirect(url_for("employees"))
    employees = Employee.query.order_by(Employee.name).all()
    return render_template("employees.html", employees=employees)


@app.route("/deliveries")
def deliveries():
    deliveries = Delivery.query.order_by(Delivery.delivered_at.desc()).all()
    return render_template("deliveries.html", deliveries=deliveries)


@app.route("/deliveries/add", methods=["GET", "POST"])
def add_delivery():
    products = Product.query.order_by(Product.name).all()
    employees = Employee.query.order_by(Employee.name).all()

    if request.method == "POST":
        employee_id = int(request.form.get("employee_id"))
        product_id = int(request.form.get("product_id"))
        quantity = int(request.form.get("quantity", 0))
        notes = request.form.get("notes", "")
        product = Product.query.get_or_404(product_id)

        if quantity <= 0:
            flash("La cantidad debe ser mayor a cero.", "danger")
            return redirect(url_for("add_delivery"))

        if product.quantity < quantity:
            flash("No hay suficiente stock para esta entrega.", "danger")
            return redirect(url_for("add_delivery"))

        photo_filename = None
        photo = request.files.get("photo")
        if photo and photo.filename and allowed_file(photo.filename):
            filename = secure_filename(photo.filename)
            photo_filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
            photo.save(os.path.join(app.config["UPLOAD_FOLDER"], photo_filename))

        product.quantity -= quantity
        delivery = Delivery(
            employee_id=employee_id,
            product_id=product_id,
            quantity=quantity,
            photo_filename=photo_filename,
            notes=notes,
        )
        db.session.add(delivery)
        db.session.add(StockMovement(product_id=product.id, movement_type="OUT", quantity=quantity, notes="Entrega de dotación"))
        db.session.commit()

        flash("Entrega registrada correctamente.", "success")
        return redirect(url_for("deliveries"))

    return render_template("delivery_form.html", products=products, employees=employees)


@app.route("/low-stock")
def low_stock():
    products = Product.query.filter(Product.quantity <= Product.min_stock).order_by(Product.name).all()
    return render_template("low_stock.html", products=products)


@app.route("/movements")
def movements():
    movements = StockMovement.query.order_by(StockMovement.created_at.desc()).all()
    return render_template("movements.html", movements=movements)


@app.route("/deliveries/<int:delivery_id>/pdf")
def delivery_pdf(delivery_id):
    delivery = Delivery.query.get_or_404(delivery_id)
    pdf_path = os.path.join(REPORT_FOLDER, f"delivery_{delivery.id}.pdf")

    c = canvas.Canvas(pdf_path, pagesize=letter)
    width, height = letter
    y = height - 50

    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, "Comprobante de Entrega de Dotación / EPP")
    y -= 40

    c.setFont("Helvetica", 11)
    c.drawString(50, y, f"Entrega ID: {delivery.id}")
    y -= 20
    c.drawString(50, y, f"Fecha: {delivery.delivered_at.strftime('%Y-%m-%d %H:%M')}")
    y -= 20
    c.drawString(50, y, f"Empleado: {delivery.employee.name}")
    y -= 20
    c.drawString(50, y, f"Cargo: {delivery.employee.position or ''}")
    y -= 20
    c.drawString(50, y, f"Empresa: {delivery.employee.company or ''}")
    y -= 30
    c.drawString(50, y, f"Producto entregado: {delivery.product.name}")
    y -= 20
    c.drawString(50, y, f"Categoría: {delivery.product.category}")
    y -= 20
    c.drawString(50, y, f"Cantidad: {delivery.quantity}")
    y -= 30
    c.drawString(50, y, f"Notas: {delivery.notes or ''}")
    y -= 60

    c.line(50, y, 250, y)
    c.drawString(50, y - 15, "Firma del trabajador")
    c.line(330, y, 530, y)
    c.drawString(330, y - 15, "Firma de quien entrega")

    c.save()
    return send_file(pdf_path, as_attachment=True)


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(debug=True)
