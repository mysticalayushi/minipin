import os
import uuid
import sqlite3
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, g, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB max upload

ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
DB_PATH = "minipin.db"


# ---------- Database helpers ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row  # lets us use row["column"]
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            image TEXT NOT NULL,
            description TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            user_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
        CREATE TABLE IF NOT EXISTS saves (
            user_id INTEGER NOT NULL,
            pin_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, pin_id),
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (pin_id) REFERENCES pins (id)
        );
        CREATE TABLE IF NOT EXISTS boards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, name),
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
        CREATE TABLE IF NOT EXISTS board_pins (
            board_id INTEGER NOT NULL,
            pin_id INTEGER NOT NULL,
            PRIMARY KEY (board_id, pin_id),
            FOREIGN KEY (board_id) REFERENCES boards (id),
            FOREIGN KEY (pin_id) REFERENCES pins (id)
        );
    """)

    # Migration: add new columns to an OLD pins table (from before this update)
    cols = [row[1] for row in db.execute("PRAGMA table_info(pins)")]
    if "description" not in cols:
        db.execute("ALTER TABLE pins ADD COLUMN description TEXT DEFAULT ''")
    if "tags" not in cols:
        db.execute("ALTER TABLE pins ADD COLUMN tags TEXT DEFAULT ''")

    db.commit()
    db.close()


# ---------- Helpers ----------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def clean_tags(raw):
    """'Travel, #Food, travel' -> 'travel, food' (lowercase, no duplicates, max 10)."""
    tags = []
    for t in raw.replace("#", "").split(","):
        t = t.strip().lower()
        if t and t not in tags:
            tags.append(t)
    return ", ".join(tags[:10])


def fetch_pins(where="1=1", params=()):
    """Get pins with username, save count, and whether current user saved each."""
    me = session.get("user_id", 0)
    query = f"""
        SELECT pins.*, users.username,
               (SELECT COUNT(*) FROM saves WHERE saves.pin_id = pins.id) AS save_count,
               EXISTS(SELECT 1 FROM saves
                      WHERE saves.pin_id = pins.id AND saves.user_id = ?) AS saved
        FROM pins JOIN users ON pins.user_id = users.id
        WHERE {where}
        ORDER BY pins.created_at DESC
    """
    return get_db().execute(query, (me, *params)).fetchall()


@app.template_filter("taglist")
def taglist(tags):
    """Lets templates loop over tags: {% for t in pin['tags']|taglist %}"""
    return [t.strip() for t in (tags or "").split(",") if t.strip()]


# ---------- Auth routes ----------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        if not username or not email or len(password) < 6:
            flash("Fill all fields. Password must be at least 6 characters.")
            return redirect(url_for("signup"))

        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                (username, email, generate_password_hash(password)),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("Username or email already exists.")
            return redirect(url_for("signup"))

        flash("Account created! Please log in.")
        return redirect(url_for("login"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        user = get_db().execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))

        flash("Invalid email or password.")
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------- Pin routes ----------
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    if q:
        like = f"%{q}%"
        pins = fetch_pins(
            "(pins.title LIKE ? OR pins.description LIKE ? OR pins.tags LIKE ?)",
            (like, like, like),
        )
    else:
        pins = fetch_pins()
    return render_template("index.html", pins=pins, q=q)


@app.route("/create", methods=["GET", "POST"])
@login_required
def create():
    if request.method == "POST":
        title = request.form["title"].strip()
        description = request.form.get("description", "").strip()[:500]
        tags = clean_tags(request.form.get("tags", ""))
        file = request.files.get("image")

        if not title or not file or file.filename == "":
            flash("Add a title and choose an image.")
            return redirect(url_for("create"))
        if not allowed_file(file.filename):
            flash("Only png, jpg, jpeg, gif, webp allowed.")
            return redirect(url_for("create"))

        # unique name so two files with the same name don't overwrite
        filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        db = get_db()
        cur = db.execute(
            "INSERT INTO pins (title, image, description, tags, user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, filename, description, tags, session["user_id"]),
        )
        db.commit()
        return redirect(url_for("pin_detail", pin_id=cur.lastrowid))
    return render_template("create.html")


@app.route("/pin/<int:pin_id>")
def pin_detail(pin_id):
    rows = fetch_pins("pins.id = ?", (pin_id,))
    if not rows:
        abort(404)
    pin = rows[0]

    my_boards, in_boards = [], []
    if "user_id" in session:
        db = get_db()
        my_boards = db.execute(
            "SELECT * FROM boards WHERE user_id = ? ORDER BY name",
            (session["user_id"],),
        ).fetchall()
        in_boards = db.execute(
            "SELECT boards.* FROM boards "
            "JOIN board_pins ON board_pins.board_id = boards.id "
            "WHERE boards.user_id = ? AND board_pins.pin_id = ?",
            (session["user_id"], pin_id),
        ).fetchall()

    return render_template("pin.html", pin=pin,
                           my_boards=my_boards, in_boards=in_boards)


@app.route("/save/<int:pin_id>", methods=["POST"])
@login_required
def toggle_save(pin_id):
    db = get_db()
    existing = db.execute(
        "SELECT 1 FROM saves WHERE user_id = ? AND pin_id = ?",
        (session["user_id"], pin_id),
    ).fetchone()
    if existing:
        db.execute("DELETE FROM saves WHERE user_id = ? AND pin_id = ?",
                   (session["user_id"], pin_id))
    else:
        db.execute("INSERT INTO saves (user_id, pin_id) VALUES (?, ?)",
                   (session["user_id"], pin_id))
    db.commit()
    return redirect(request.referrer or url_for("index"))


@app.route("/delete/<int:pin_id>", methods=["POST"])
@login_required
def delete_pin(pin_id):
    db = get_db()
    pin = db.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
    if pin is None:
        abort(404)
    if pin["user_id"] != session["user_id"]:
        abort(403)  # you can only delete your own pins

    # remove the image file, then every row that points at this pin
    image_path = os.path.join(app.config["UPLOAD_FOLDER"], pin["image"])
    if os.path.exists(image_path):
        os.remove(image_path)
    db.execute("DELETE FROM saves WHERE pin_id = ?", (pin_id,))
    db.execute("DELETE FROM board_pins WHERE pin_id = ?", (pin_id,))
    db.execute("DELETE FROM pins WHERE id = ?", (pin_id,))
    db.commit()
    flash("Pin deleted.")

    # if we were on that pin's own page, go home instead of back to a 404
    if request.referrer and f"/pin/{pin_id}" in request.referrer:
        return redirect(url_for("index"))
    return redirect(request.referrer or url_for("index"))


# ---------- Board routes ----------
@app.route("/boards/create", methods=["POST"])
@login_required
def create_board():
    name = request.form.get("name", "").strip()[:50]
    if not name:
        flash("Give your board a name.")
    else:
        try:
            db = get_db()
            db.execute("INSERT INTO boards (user_id, name) VALUES (?, ?)",
                       (session["user_id"], name))
            db.commit()
            flash(f'Board "{name}" created.')
        except sqlite3.IntegrityError:
            flash("You already have a board with that name.")
    return redirect(url_for("profile", username=session["username"], tab="boards"))


@app.route("/pin/<int:pin_id>/board", methods=["POST"])
@login_required
def add_to_board(pin_id):
    db = get_db()
    if db.execute("SELECT 1 FROM pins WHERE id = ?", (pin_id,)).fetchone() is None:
        abort(404)

    new_name = request.form.get("new_board", "").strip()[:50]
    board_id = request.form.get("board_id", "")

    if new_name:  # user typed a new board name -> create it (or reuse if it exists)
        try:
            cur = db.execute("INSERT INTO boards (user_id, name) VALUES (?, ?)",
                             (session["user_id"], new_name))
            board_id = cur.lastrowid
        except sqlite3.IntegrityError:
            row = db.execute("SELECT id FROM boards WHERE user_id = ? AND name = ?",
                             (session["user_id"], new_name)).fetchone()
            board_id = row["id"]
    elif not board_id:
        flash("Choose a board or type a new board name.")
        return redirect(url_for("pin_detail", pin_id=pin_id))

    # make sure the board really belongs to the logged-in user
    board = db.execute("SELECT * FROM boards WHERE id = ? AND user_id = ?",
                       (board_id, session["user_id"])).fetchone()
    if board is None:
        abort(403)

    db.execute("INSERT OR IGNORE INTO board_pins (board_id, pin_id) VALUES (?, ?)",
               (board["id"], pin_id))
    db.commit()
    flash(f'Added to "{board["name"]}".')
    return redirect(url_for("pin_detail", pin_id=pin_id))


@app.route("/board/<int:board_id>")
def board_view(board_id):
    db = get_db()
    board = db.execute(
        "SELECT boards.*, users.username FROM boards "
        "JOIN users ON boards.user_id = users.id WHERE boards.id = ?",
        (board_id,),
    ).fetchone()
    if board is None:
        abort(404)

    pins = fetch_pins(
        "pins.id IN (SELECT pin_id FROM board_pins WHERE board_id = ?)", (board_id,)
    )
    is_owner = session.get("user_id") == board["user_id"]
    return render_template("board.html", board=board, pins=pins, is_owner=is_owner,
                           current_board=board if is_owner else None)


@app.route("/board/<int:board_id>/remove/<int:pin_id>", methods=["POST"])
@login_required
def remove_from_board(board_id, pin_id):
    db = get_db()
    board = db.execute("SELECT * FROM boards WHERE id = ? AND user_id = ?",
                       (board_id, session["user_id"])).fetchone()
    if board is None:
        abort(403)
    db.execute("DELETE FROM board_pins WHERE board_id = ? AND pin_id = ?",
               (board_id, pin_id))
    db.commit()
    return redirect(url_for("board_view", board_id=board_id))


@app.route("/board/<int:board_id>/delete", methods=["POST"])
@login_required
def delete_board(board_id):
    db = get_db()
    board = db.execute("SELECT * FROM boards WHERE id = ? AND user_id = ?",
                       (board_id, session["user_id"])).fetchone()
    if board is None:
        abort(403)
    db.execute("DELETE FROM board_pins WHERE board_id = ?", (board_id,))
    db.execute("DELETE FROM boards WHERE id = ?", (board_id,))
    db.commit()
    flash(f'Board "{board["name"]}" deleted. (Your pins are safe.)')
    return redirect(url_for("profile", username=session["username"], tab="boards"))


# ---------- Profile ----------
@app.route("/profile/<username>")
def profile(username):
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    if user is None:
        abort(404)

    is_me = session.get("user_id") == user["id"]
    tab = request.args.get("tab", "created")
    pins, boards = [], []

    if tab == "boards":
        boards = db.execute("""
            SELECT boards.*,
                   (SELECT COUNT(*) FROM board_pins
                    WHERE board_pins.board_id = boards.id) AS pin_count,
                   (SELECT pins.image FROM board_pins
                    JOIN pins ON pins.id = board_pins.pin_id
                    WHERE board_pins.board_id = boards.id
                    ORDER BY board_pins.rowid DESC LIMIT 1) AS cover
            FROM boards WHERE boards.user_id = ?
            ORDER BY boards.created_at DESC
        """, (user["id"],)).fetchall()
    elif tab == "saved" and is_me:
        pins = fetch_pins(
            "pins.id IN (SELECT pin_id FROM saves WHERE user_id = ?)", (user["id"],)
        )
    else:
        tab = "created"
        pins = fetch_pins("pins.user_id = ?", (user["id"],))

    return render_template("profile.html", profile_user=user, pins=pins,
                           boards=boards, tab=tab, is_me=is_me)


if __name__ == "__main__":
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    init_db()
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")