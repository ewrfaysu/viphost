import os
import json
import shutil
import signal
import subprocess
import threading
import time
import uuid
import zipfile
import ast
import sys

from pathlib import Path

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    render_template,
    session,
    jsonify,
    abort
)

from flask_socketio import SocketIO, join_room
from werkzeug.utils import secure_filename


# =========================================================
# CONFIG
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
BOTS_DIR = BASE_DIR / "bots"
CONFIG_FILE = BASE_DIR / "config.json"

BOTS_DIR.mkdir(exist_ok=True)


# =========================================================
# AUTO REQUIREMENTS DETECTION
# =========================================================

# Common Python import-name -> PyPI package-name mappings.
IMPORT_TO_PACKAGE = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "flask_socketio": "Flask-SocketIO",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "sklearn": "scikit-learn",
    "jwt": "PyJWT",
    "Crypto": "pycryptodome",
    "dateutil": "python-dateutil",
    "googleapiclient": "google-api-python-client",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "telegram": "python-telegram-bot",
    "discord": "discord.py",
    "aiohttp": "aiohttp",
    "websocket": "websocket-client",
    "websockets": "websockets",
    "numpy": "numpy",
    "pandas": "pandas",
    "requests": "requests",
}


def detect_imports(folder):
    """Return likely third-party top-level imports used by .py files."""
    imports = set()

    for py_file in folder.rglob("*.py"):
        if not py_file.is_file():
            continue
        try:
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, OSError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                # Ignore relative imports such as: from .utils import foo
                if node.level == 0:
                    imports.add(node.module.split(".")[0])

    # Python 3.10+ exposes stdlib_module_names. Add a conservative fallback
    # for older Python versions used by some hosting environments.
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    stdlib.update({
        "__future__", "os", "sys", "json", "time", "datetime", "re",
        "math", "random", "asyncio", "threading", "subprocess", "pathlib",
        "typing", "collections", "itertools", "functools", "logging",
        "sqlite3", "http", "urllib", "email", "html", "hashlib", "hmac",
        "base64", "io", "csv", "glob", "shutil", "signal", "socket",
        "ssl", "struct", "tempfile", "traceback", "uuid", "zipfile",
    })

    local_modules = {p.stem for p in folder.rglob("*.py") if p.is_file()}
    third_party = {m for m in imports if m not in stdlib and m not in local_modules}
    return third_party


def package_name(import_name):
    return IMPORT_TO_PACKAGE.get(import_name, import_name)


def ensure_requirements(folder):
    """Create/augment requirements.txt from imports. Returns package names."""
    req_path = folder / "requirements.txt"
    existing_lines = []
    existing_names = set()

    if req_path.exists():
        try:
            existing_lines = req_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            existing_lines = []

        for line in existing_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                continue
            # Handle normal requirement forms such as requests>=2.0 and package[extra].
            name = stripped.split(";")[0].strip()
            for op in ("===", ">=", "<=", "==", "~=", ">", "<", "!="):
                name = name.split(op, 1)[0]
            name = name.split("[", 1)[0].strip().lower().replace("_", "-")
            if name:
                existing_names.add(name)

    added = []
    for module in sorted(detect_imports(folder)):
        pkg = package_name(module)
        normalized = pkg.lower().replace("_", "-")
        if normalized not in existing_names:
            added.append(pkg)
            existing_names.add(normalized)

    if added or not req_path.exists():
        lines = list(existing_lines)
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(added)
        req_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    return added


# =========================================================
# LOGIN CONFIG
# =========================================================

if not CONFIG_FILE.exists():
    CONFIG_FILE.write_text(
        json.dumps(
            {
                "username": "admin",
                "password": "123"
            },
            indent=4
        ),
        encoding="utf-8"
    )


with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "PANEL_SECRET",
    "change-this-secret-key"
)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading"
)


# =========================================================
# RUNNING PROCESSES
# =========================================================

PROCESSES = {}


# =========================================================
# AUTH
# =========================================================

def logged_in():
    return session.get("logged_in") is True


# =========================================================
# BOT PATH
# =========================================================

def bot_path(bot_id):
    return BOTS_DIR / bot_id


# =========================================================
# META
# =========================================================

def read_meta(bot_id):

    path = bot_path(bot_id) / "meta.json"

    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_meta(bot_id, data):

    path = bot_path(bot_id) / "meta.json"

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


# =========================================================
# BOT LIST
# =========================================================

def get_bots():

    result = []

    for folder in BOTS_DIR.iterdir():

        if not folder.is_dir():
            continue

        meta = read_meta(folder.name)

        if not meta:
            continue

        process = PROCESSES.get(folder.name)

        if process and process.poll() is None:
            status = "RUNNING"
        else:
            status = "STOPPED"

        meta["id"] = folder.name
        meta["status"] = status

        result.append(meta)

    return result


# =========================================================
# LOG
# =========================================================

def emit_log(bot_id, line):

    socketio.emit(
        "log",
        {
            "bot_id": bot_id,
            "line": line
        },
        room=bot_id
    )


def save_log(bot_id, line):

    path = bot_path(bot_id) / "console.log"

    try:
        with open(
            path,
            "a",
            encoding="utf-8",
            errors="ignore"
        ) as f:
            f.write(line + "\n")
    except Exception:
        pass


# =========================================================
# READ LIVE OUTPUT
# =========================================================

def read_output(bot_id, process):

    try:

        while True:

            line = process.stdout.readline()

            if not line:
                break

            line = line.rstrip("\r\n")

            emit_log(
                bot_id,
                line
            )

            save_log(
                bot_id,
                line
            )


        code = process.wait()

        if code == 0:
            status = "STOPPED"
        else:
            status = "CRASHED"


        socketio.emit(
            "status",
            {
                "bot_id": bot_id,
                "status": status,
                "code": code
            },
            room=bot_id
        )


    except Exception as e:

        emit_log(
            bot_id,
            "[Panel Error] " + str(e)
        )

    finally:

        PROCESSES.pop(
            bot_id,
            None
        )


# =========================================================
# START BOT
# =========================================================

def start_bot(bot_id):

    old_process = PROCESSES.get(bot_id)

    if (
        old_process
        and
        old_process.poll() is None
    ):
        return False, "Bot already running"


    folder = bot_path(bot_id)

    if not folder.exists():
        return False, "Bot not found"


    meta = read_meta(bot_id)

    entry = meta.get("entry")

    if not entry:
        return False, "Start file not selected"


    entry_path = (
        folder / entry
    ).resolve()


    # Prevent path traversal

    if not str(entry_path).startswith(
        str(folder.resolve())
    ):
        return False, "Invalid file"


    if not entry_path.exists():
        return False, "Python file not found"


    if entry_path.suffix.lower() != ".py":
        return False, "Only Python files can be started"


    # Automatically create/augment requirements.txt from the bot's imports.
    try:
        added_packages = ensure_requirements(folder)
        req_path = folder / "requirements.txt"

        emit_log(bot_id, "[Panel] Checking requirements.txt...")
        if added_packages:
            emit_log(
                bot_id,
                "[Panel] Auto-added: " + ", ".join(added_packages)
            )

        if req_path.exists() and req_path.read_text(encoding="utf-8", errors="ignore").strip():
            emit_log(bot_id, "[Panel] Installing requirements.txt...")
            install = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req_path)],
                cwd=str(folder),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace"
            )
            for pip_line in install.stdout.splitlines():
                emit_log(bot_id, "[pip] " + pip_line)
                save_log(bot_id, "[pip] " + pip_line)

            if install.returncode != 0:
                emit_log(bot_id, "[Panel] Dependency installation failed. Bot was not started.")
                return False, "Dependency installation failed"

            emit_log(bot_id, "[Panel] Dependencies installed successfully.")
        else:
            emit_log(bot_id, "[Panel] No external Python packages detected.")

    except Exception as e:
        emit_log(bot_id, "[Panel Error] Requirements setup failed: " + str(e))
        return False, "Requirements setup failed: " + str(e)


    # Clear old console

    log_path = folder / "console.log"

    try:
        log_path.write_text(
            "",
            encoding="utf-8"
        )
    except Exception:
        pass


    try:

        emit_log(
            bot_id,
            "[Panel] Starting bot..."
        )


        # -u = unbuffered Python output

        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                entry
            ],
            cwd=str(folder),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1
        )


        PROCESSES[bot_id] = process


        socketio.emit(
            "status",
            {
                "bot_id": bot_id,
                "status": "RUNNING"
            },
            room=bot_id
        )


        threading.Thread(
            target=read_output,
            args=(bot_id, process),
            daemon=True
        ).start()


        return True, "Bot started"


    except Exception as e:

        return False, str(e)


# =========================================================
# STOP BOT
# =========================================================

def stop_bot(bot_id):

    process = PROCESSES.get(bot_id)

    if not process:
        return False, "Bot is not running"


    if process.poll() is not None:

        PROCESSES.pop(
            bot_id,
            None
        )

        return False, "Bot already stopped"


    try:

        # Android/Termux friendly terminate

        process.terminate()


        try:

            process.wait(
                timeout=5
            )

        except subprocess.TimeoutExpired:

            process.kill()

            process.wait()


        emit_log(
            bot_id,
            "[Panel] Bot stopped."
        )


        socketio.emit(
            "status",
            {
                "bot_id": bot_id,
                "status": "STOPPED"
            },
            room=bot_id
        )


        PROCESSES.pop(
            bot_id,
            None
        )


        return True, "Bot stopped"


    except Exception as e:

        return False, str(e)


# =========================================================
# LOGIN
# =========================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        )

        password = request.form.get(
            "password",
            ""
        )


        if (
            username == CONFIG.get("username")
            and
            password == CONFIG.get("password")
        ):

            session["logged_in"] = True

            return redirect(
                url_for("index")
            )


        return render_template(
            "login.html",
            error="Wrong username or password"
        )


    return render_template(
        "login.html",
        error=None
    )


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# =========================================================
# HOME
# =========================================================

@app.route("/")
def index():

    if not logged_in():
        return redirect(
            url_for("login")
        )


    return render_template(
        "index.html",
        bots=get_bots()
    )


# =========================================================
# UPLOAD
# =========================================================

@app.route(
    "/upload",
    methods=["POST"]
)
def upload():

    if not logged_in():
        abort(401)


    uploaded = request.files.get("file")


    if not uploaded or not uploaded.filename:

        return jsonify(
            ok=False,
            error="No file selected"
        ), 400


    original_name = uploaded.filename

    lower_name = original_name.lower()


    if not (
        lower_name.endswith(".py")
        or
        lower_name.endswith(".zip")
    ):

        return jsonify(
            ok=False,
            error="Only .py or .zip files are allowed"
        ), 400


    bot_id = uuid.uuid4().hex[:10]

    folder = bot_path(bot_id)

    folder.mkdir(
        parents=True,
        exist_ok=True
    )


    try:

        # =================================================
        # PY FILE
        # =================================================

        if lower_name.endswith(".py"):

            safe_name = secure_filename(
                original_name
            )


            if not safe_name:

                raise ValueError(
                    "Invalid filename"
                )


            destination = (
                folder / safe_name
            )


            uploaded.save(
                str(destination)
            )

            # Generate requirements.txt immediately for single-file uploads.
            ensure_requirements(folder)


            write_meta(
                bot_id,
                {
                    "name": Path(
                        safe_name
                    ).stem,
                    "entry": safe_name
                }
            )


        # =================================================
        # ZIP FILE
        # =================================================

        else:

            zip_path = (
                folder / "upload.zip"
            )


            uploaded.save(
                str(zip_path)
            )


            # ZIP safety validation

            base = folder.resolve()


            with zipfile.ZipFile(
                zip_path,
                "r"
            ) as archive:

                for info in archive.infolist():

                    target = (
                        folder
                        / info.filename
                    ).resolve()


                    if not str(
                        target
                    ).startswith(
                        str(base)
                    ):

                        raise ValueError(
                            "Unsafe ZIP file"
                        )


                archive.extractall(
                    str(folder)
                )


            zip_path.unlink(
                missing_ok=True
            )

            # If requirements.txt is missing/incomplete, infer dependencies
            # from all Python files in the uploaded project.
            ensure_requirements(folder)


            # Find Python files

            py_files = []


            for p in folder.rglob("*.py"):

                if p.is_file():

                    relative = (
                        p.relative_to(folder)
                        .as_posix()
                    )

                    py_files.append(
                        relative
                    )


            if not py_files:

                raise ValueError(
                    "ZIP contains no .py file"
                )


            write_meta(
                bot_id,
                {
                    "name": Path(
                        original_name
                    ).stem,
                    "entry": py_files[0]
                }
            )


    except Exception as e:

        shutil.rmtree(
            folder,
            ignore_errors=True
        )


        return jsonify(
            ok=False,
            error="Upload failed: " + str(e)
        ), 400


    return jsonify(
        ok=True,
        bot_id=bot_id
    )


# =========================================================
# PYTHON FILES
# =========================================================

@app.route(
    "/bot/<bot_id>/files"
)
def bot_files(bot_id):

    if not logged_in():
        abort(401)


    folder = bot_path(bot_id)


    if not folder.exists():
        return jsonify([])


    files = []


    for p in folder.rglob("*.py"):

        if p.is_file():

            files.append(
                p.relative_to(
                    folder
                ).as_posix()
            )


    return jsonify(files)


# =========================================================
# READ FILE CONTENT (GET)
# =========================================================

@app.route("/bot/<bot_id>/file-content", methods=["GET"])
def get_file_content(bot_id):

    if not logged_in():
        abort(401)

    folder = bot_path(bot_id)
    if not folder.exists():
        return jsonify(ok=False, error="Bot not found"), 404

    filename = request.args.get("file", "")
    if not filename:
        return jsonify(ok=False, error="Filename missing"), 400

    target = (folder / filename).resolve()

    # Path traversal protection
    if not str(target).startswith(str(folder.resolve())):
        return jsonify(ok=False, error="Invalid file path"), 400

    if not target.exists():
        return jsonify(ok=False, error="File not found"), 404

    try:
        content = target.read_text(encoding="utf-8", errors="ignore")
        return jsonify(ok=True, content=content)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# =========================================================
# SAVE FILE CONTENT (POST)
# =========================================================

@app.route("/bot/<bot_id>/save-file", methods=["POST"])
def save_file_content(bot_id):

    if not logged_in():
        abort(401)

    folder = bot_path(bot_id)
    if not folder.exists():
        return jsonify(ok=False, error="Bot not found"), 404

    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "")
    content = data.get("content")

    if not filename or content is None:
        return jsonify(ok=False, error="Filename or content missing"), 400

    target = (folder / filename).resolve()

    # Path traversal protection
    if not str(target).startswith(str(folder.resolve())):
        return jsonify(ok=False, error="Invalid file path"), 400

    try:
        target.write_text(content, encoding="utf-8")
        return jsonify(ok=True, message="File saved successfully")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# =========================================================
# SET ENTRY
# =========================================================

@app.route(
    "/bot/<bot_id>/entry",
    methods=["POST"]
)
def set_entry(bot_id):

    if not logged_in():
        abort(401)


    folder = bot_path(bot_id)


    if not folder.exists():

        return jsonify(
            ok=False,
            error="Bot not found"
        ), 404


    data = request.get_json(
        silent=True
    ) or {}


    entry = data.get(
        "entry",
        ""
    )


    target = (
        folder / entry
    ).resolve()


    if not str(target).startswith(
        str(folder.resolve())
    ):

        return jsonify(
            ok=False,
            error="Invalid file"
        ), 400


    if not target.exists():

        return jsonify(
            ok=False,
            error="File not found"
        ), 400


    if target.suffix.lower() != ".py":

        return jsonify(
            ok=False,
            error="Only .py file allowed"
        ), 400


    meta = read_meta(bot_id)

    meta["entry"] = entry

    write_meta(
        bot_id,
        meta
    )


    return jsonify(
        ok=True
    )


# =========================================================
# BOT ACTION
# =========================================================

@app.route(
    "/bot/<bot_id>/<action>",
    methods=["POST"]
)
def bot_action(
    bot_id,
    action
):

    if not logged_in():
        abort(401)


    if not bot_path(bot_id).exists():

        return jsonify(
            ok=False,
            error="Bot not found"
        ), 404


    if action == "start":

        ok, message = start_bot(
            bot_id
        )


    elif action == "stop":

        ok, message = stop_bot(
            bot_id
        )


    elif action == "restart":

        stop_bot(bot_id)

        time.sleep(0.5)

        ok, message = start_bot(
            bot_id
        )


    else:

        return jsonify(
            ok=False,
            error="Invalid action"
        ), 400


    return jsonify(
        ok=ok,
        message=message
    )


# =========================================================
# DELETE BOT
# =========================================================

@app.route(
    "/bot/<bot_id>",
    methods=["DELETE"]
)
def delete_bot(bot_id):

    if not logged_in():
        abort(401)


    stop_bot(bot_id)


    folder = bot_path(bot_id)


    if folder.exists():

        shutil.rmtree(
            folder,
            ignore_errors=True
        )


    return jsonify(
        ok=True
    )


# =========================================================
# LOG API
# =========================================================

@app.route(
    "/bot/<bot_id>/log"
)
def bot_log(bot_id):

    if not logged_in():
        abort(401)


    path = (
        bot_path(bot_id)
        / "console.log"
    )


    if not path.exists():

        return jsonify(
            log=""
        )


    try:

        text = path.read_text(
            encoding="utf-8",
            errors="ignore"
        )

    except Exception:

        text = ""


    # Last 30,000 characters

    return jsonify(
        log=text[-30000:]
    )


# =========================================================
# SOCKET JOIN
# =========================================================

@socketio.on("join")
def socket_join(data):

    if not logged_in():
        return


    bot_id = data.get(
        "bot_id"
    )


    if bot_id:

        join_room(
            bot_id
        )


# =========================================================
# START SERVER
# =========================================================

if __name__ == "__main__":

    print()
    print("=" * 45)
    print("       MULTI BOT HOSTING PANEL")
    print("=" * 45)
    print()
    print("Login: admin / 123")
    print("Panel: http://127.0.0.1:5000")
    print()


    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        allow_unsafe_werkzeug=True
    )
