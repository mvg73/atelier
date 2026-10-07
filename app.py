"""Dressmaker Atelier: puts game dresses on photos of people via xAI Grok Imagine.

Copyright (c) 2026 mvg73. MIT License, see LICENSE.
"""
import base64
import io
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for
from PIL import Image, ImageOps

BASE = Path(__file__).resolve().parent
DEFAULT_DRESS_DIR = Path.home() / "Dressmaker-drive_c"
DEFAULT_PEOPLE_DIR = BASE / "people"
SETTINGS = BASE / "settings.json"
LIBRARY = BASE / "library.json"
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MODEL = os.environ.get("XAI_IMAGE_MODEL", "grok-imagine-image-2.0")
EDIT_URL = "https://api.x.ai/v1/images/edits"
PRICE_PER_IMAGE = 0.08  # observed: 2 input images + 1 output = 800M usd ticks
MAX_SIDE = 1024
WORKERS = 2

PROMPT = (
    "Image 1 is a photo of a real person. Image 2 shows a dress on a mannequin in a video game. "
    "Dress the person from image 1 in exactly the dress from image 2: same cut, neckline, sleeves, "
    "length, fabrics, lace, patterns and colors. Keep the person's face, hair, skin, body shape, pose "
    "and the original photo background unchanged. Photorealistic result, natural fabric draping and "
    "lighting matching the person's photo. Do not include the mannequin, the game room or any watermark."
)
STITCHED_PROMPT = PROMPT.replace("Image 1", "The left half").replace("image 1", "the left half") \
    .replace("Image 2", "The right half").replace("image 2", "the right half") + \
    " Output only the person (left half) wearing the dress, as a single photo."
PERSON_RULES = (
    " The person's body is never altered: keep both arms, hands, legs, feet, shoulders, neck, face, "
    "skin, body shape and pose exactly as they are, and keep the background unchanged."
)
FIX_PROMPT = (
    "Image 1 shows a real person wearing a dress. Image 2 is the original dress design "
    "(shown on a mannequin in a video game; the mannequin is not part of the design).\n"
    "Apply this correction to image 1: \"{note}\"\n"
    "How to interpret the correction: it is about the DRESS. Words like arms, shoulders, straps, "
    "neck, chest, waist, back or legs refer to the parts of the garment there (sleeves, straps, "
    "neckline, bodice, skirt, hem), never to the person's body. For example \"remove the arms\" "
    "means remove the sleeves from the dress, leaving the person's bare arms. The only things "
    "besides the dress that may change are the person's hair color and fingernail/toenail polish "
    "color, and only if the correction explicitly asks for that.\n"
    "Everything else must stay identical to image 1:" + PERSON_RULES +
    " Hair style, makeup and jewelry stay the same unless the correction is about hair color or "
    "nail color. The dress should match image 2."
)
REQUEST_PROMPT = (
    " Requested changes for this fitting, which take priority over image 2 and over the dress notes: \"{note}\". "
    "These are about the DRESS: words like arms, shoulders, straps, neck, chest, waist, back or legs mean the "
    "parts of the garment there (sleeves, straps, neckline, bodice, skirt, hem), never the person's body. The only "
    "things besides the dress that may change are the person's hair color and fingernail/toenail polish color, and "
    "only if the request asks for that." + PERSON_RULES
)


def request_text(note: str) -> str:
    """A one-off request written in the atelier's logbook for a single fitting (e.g. "make it a mini dress")."""
    note = (note or "").strip().rstrip(".")
    return REQUEST_PROMPT.format(note=note) if note else ""


def load_env():
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
app = Flask(__name__)
lib_lock = threading.Lock()


# ---------- library (aliases + notes) ----------

def load_library():
    try:
        lib = json.loads(LIBRARY.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        lib = {}
    lib.setdefault("dresses", {})
    lib.setdefault("people", {})
    lib.setdefault("gallery", [None, None, None])  # results hung in the atelier's frames
    lib.setdefault("requests", {})                 # result key -> the one-off request it was made with
    return lib


def save_library(lib):
    tmp = LIBRARY.with_suffix(".tmp")
    tmp.write_text(json.dumps(lib, indent=2, ensure_ascii=False))
    tmp.replace(LIBRARY)


def get_request(key: str) -> str:
    return load_library()["requests"].get(key, "")


def set_request(key: str, note: str):
    """Remember (or forget, when note is empty) the request a result was made with."""
    with lib_lock:
        lib = load_library()
        if note:
            lib["requests"][key] = note
        else:
            lib["requests"].pop(key, None)
        save_library(lib)


def meta(kind, path: Path):
    return load_library()[kind].get(path.name, {})


# ---------- settings (folders, plain view) ----------

def load_settings():
    try:
        s = json.loads(SETTINGS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("dress_dir", str(DEFAULT_DRESS_DIR))
    s.setdefault("models_dir", str(DEFAULT_PEOPLE_DIR))
    s.setdefault("plain_view", True)
    return s


def save_settings(s):
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False))
    tmp.replace(SETTINGS)


def as_dir(text) -> Path:
    """A folder as the user typed it (~ expanded, made absolute); shortcuts/symlinks keep their own name."""
    return Path(os.path.abspath(os.path.expanduser(str(text or "").strip())))


def dress_dir() -> Path:
    return as_dir(load_settings()["dress_dir"])


def people_dir() -> Path:
    return as_dir(load_settings()["models_dir"])


def folder_report(path: Path, kind: str):
    """Does the folder exist, and how many usable images does it hold (dresses are PNG only)?"""
    if not path.is_dir():
        return {"path": str(path), "exists": False, "count": 0}
    files = list_images(path)
    if kind == "dresses":
        files = [f for f in files if f.suffix.lower() == ".png"]
    return {"path": str(path), "exists": True, "count": len(files)}


# ---------- files ----------

def list_images(folder: Path):
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXTS)


def dresses():
    return [p for p in list_images(dress_dir()) if p.suffix.lower() == ".png"]


def people():
    return list_images(people_dir())


def render_dir():
    return BASE / f"Render{date.today().strftime('%m%d%Y')}"


def folder_date(folder: Path):
    try:
        return datetime.strptime(folder.name[len("Render"):], "%m%d%Y").date()
    except ValueError:
        return None


def all_render_dirs():
    """Every RenderMMDDYYYY folder, newest day first."""
    dirs = [p for p in BASE.glob("Render*") if p.is_dir() and folder_date(p)]
    return sorted(dirs, key=folder_date, reverse=True)


def key_of(path: Path):
    """Results are identified across days as '<folder>/<file>'."""
    return f"{path.parent.name}/{path.name}"


def out_name(dress: Path, person: Path):
    return f"{dress.stem}__{person.stem}.png"


def split_result(name: str):
    """Result filename -> (dress stem, person stem, take number). Extra takes end in __v2, __v3…"""
    parts = Path(name).stem.split("__")
    take = 1
    if len(parts) > 2 and parts[-1][:1] == "v" and parts[-1][1:].isdigit():
        take = int(parts.pop()[1:])
    if len(parts) < 2:
        return None, None, take
    return parts[0], "__".join(parts[1:]), take


def parse_result(name: str):
    """Result filename -> (dress path, person path), or (None, None)."""
    d_stem, p_stem, _ = split_result(name)
    d = next((p for p in dresses() if p.stem == d_stem), None)
    pe = next((p for p in people() if p.stem == p_stem), None)
    return d, pe


def next_take_path(path: Path):
    """First free <dress>__<person>__vN.png next to a result (N >= 2)."""
    d_stem, p_stem, _ = split_result(path.name)
    n = 2
    while (path.parent / f"{d_stem}__{p_stem}__v{n}.png").exists():
        n += 1
    return path.parent / f"{d_stem}__{p_stem}__v{n}.png"


def resolve(kind, text):
    """Match an alias or filename (case-insensitive, stem or full name)."""
    t = (text or "").strip().lower()
    files = dresses() if kind == "dresses" else people()
    lib = load_library()[kind]
    for p in files:
        if t in (p.name.lower(), p.stem.lower(), lib.get(p.name, {}).get("alias", "").strip().lower()):
            return p
    return None


def archive(path: Path):
    if path.exists():
        prev = path.parent / "_previous"
        prev.mkdir(exist_ok=True)
        path.rename(prev / f"{path.stem}.{datetime.now():%Y%m%d%H%M%S%f}{path.suffix}")


def archived_at(path: Path, version: Path):
    """When a version was archived. Older archives only carry the time of day (12 digits) in their
    name, so their file time (when that version was made, i.e. before it was archived) stands in."""
    stamp = version.stem[len(path.stem) + 1:]
    if len(stamp) == 20:
        return datetime.strptime(stamp, "%Y%m%d%H%M%S%f")
    return datetime.fromtimestamp(version.stat().st_mtime)


def previous_versions(path: Path):
    """Archived versions of a result, oldest first."""
    prev = path.parent / "_previous"
    if not prev.exists():
        return []
    return sorted((p for p in prev.glob(f"{path.stem}.*{path.suffix}")
                   if p.stem[len(path.stem) + 1:].isdigit()), key=lambda p: archived_at(path, p))


def undo(path: Path):
    """Put the most recent archived version back; the replaced one goes to _undone/."""
    versions = previous_versions(path)
    if not versions:
        return False
    if path.exists():
        undone = path.parent / "_undone"
        undone.mkdir(exist_ok=True)
        path.rename(undone / f"{path.stem}.{datetime.now():%Y%m%d%H%M%S%f}{path.suffix}")
    versions[-1].rename(path)
    return True


# ---------- soft delete ----------
# A deleted result moves to <day>/_deleted/<stem>.<timestamp>/ together with its _previous/ history.

def delete_result(path: Path):
    item = path.parent / "_deleted" / f"{path.stem}.{datetime.now():%Y%m%d%H%M%S%f}"
    (item / "_previous").mkdir(parents=True)
    for old in previous_versions(path):
        old.rename(item / "_previous" / old.name)
    path.rename(item / path.name)
    return item


def deleted_items():
    out = []
    for day in all_render_dirs():
        trash = day / "_deleted"
        if not trash.is_dir():
            continue
        for item in trash.iterdir():
            files = list_images(item) if item.is_dir() else []
            if not files:
                continue
            f = files[0]
            d, pe = parse_result(f.name)
            out.append({"id": f"{day.name}/{item.name}", "folder": day.name, "file": f.name,
                        "src": f"_deleted/{item.name}/{f.name}", "date": folder_date(day).isoformat(),
                        "deleted_at": datetime.fromtimestamp(item.stat().st_mtime).isoformat(timespec="minutes"),
                        "dress": d.name if d else None, "person": pe.name if pe else None,
                        "take": split_result(f.name)[2]})
    return sorted(out, key=lambda x: x["deleted_at"], reverse=True)


def restore_item(day: Path, item: Path):
    """Move a deleted result back. If its name was reused meanwhile, it comes back as a new take."""
    f = list_images(item)[0]
    target = day / f.name
    if target.exists():
        target = next_take_path(target)
    prev = day / "_previous"
    prev.mkdir(exist_ok=True)
    for old in sorted((item / "_previous").glob("*")):
        old.rename(prev / (target.stem + old.name[len(f.stem):]))
    f.rename(target)
    shutil.rmtree(item)
    return target


# ---------- xAI ----------

def load_img(path: Path) -> Image.Image:
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img


def data_uri(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def stitch(a: Image.Image, b: Image.Image) -> Image.Image:
    h = max(a.height, b.height)
    a = a.resize((round(a.width * h / a.height), h))
    b = b.resize((round(b.width * h / b.height), h))
    out = Image.new("RGB", (a.width + b.width, h), "white")
    out.paste(a, (0, 0))
    out.paste(b, (a.width, 0))
    return out


# Multi-image support is detected on first call and remembered for the session.
_mode = {"multi": True}


def post_edit(body):
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise RuntimeError("XAI_API_KEY is not set (put it in the .env file in the app's folder)")
    for attempt in range(3):
        r = requests.post(EDIT_URL, json=body, timeout=300,
                          headers={"Authorization": f"Bearer {key}"})
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(5 * (attempt + 1))
            continue
        return r
    return r


def notes_text(dress: Path, person: Path):
    extra = ""
    if n := meta("dresses", dress).get("notes", "").strip():
        extra += (f" Important details about this dress (these describe the garment only; words "
                  f"like arms or shoulders mean its sleeves/straps, never the person's body): {n.rstrip('.')}."
                  + PERSON_RULES)
    if n := meta("people", person).get("notes", "").strip():
        extra += f" Important details about this person: {n}"
    return extra


def try_on(person: Path, dress: Path, kind="tryon", note="") -> bytes:
    p_img, d_img = load_img(person), load_img(dress)
    extra = notes_text(dress, person) + request_text(note)
    if _mode["multi"]:
        r = post_edit({
            "model": MODEL,
            "prompt": PROMPT + extra,
            "images": [{"type": "image_url", "url": data_uri(p_img)},
                       {"type": "image_url", "url": data_uri(d_img)}],
        })
        if r.status_code in (400, 422):
            app.logger.warning("Multi-image request rejected (%s), switching to stitched mode: %s",
                               r.status_code, r.text[:300])
            _mode["multi"] = False
        else:
            return extract(r, kind, dress, person)
    r = post_edit({
        "model": MODEL,
        "prompt": STITCHED_PROMPT + extra,
        "image": {"type": "image_url", "url": data_uri(stitch(p_img, d_img))},
    })
    return extract(r, kind, dress, person)


def fix(result: Path, dress: Path, person: Path, note: str) -> bytes:
    r = post_edit({
        "model": MODEL,
        "prompt": FIX_PROMPT.format(note=note.strip().rstrip(".")) + notes_text(dress, person),
        "images": [{"type": "image_url", "url": data_uri(load_img(result))},
                   {"type": "image_url", "url": data_uri(load_img(dress))}],
    })
    return extract(r, "fix", dress, person)


def extract(r: requests.Response, kind, dress: Path, person: Path) -> bytes:
    if r.status_code != 200:
        raise RuntimeError(f"xAI API {r.status_code}: {r.text[:300]}")
    body = r.json()
    log_spend(kind, dress, person, body.get("usage", {}).get("cost_in_usd_ticks"))
    item = body["data"][0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    img = requests.get(item["url"], timeout=120)
    img.raise_for_status()
    return img.content


# ---------- spend tracking ----------

TICKS_PER_USD = 10_000_000_000
SPEND_LOG = BASE / "spend.jsonl"
spend_lock = threading.Lock()


def log_spend(kind, dress: Path, person: Path, ticks):
    """Append the exact cost xAI reported for one call (falls back to the list price)."""
    usd = ticks / TICKS_PER_USD if ticks is not None else PRICE_PER_IMAGE
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind,
             "dress": dress.name, "person": person.name, "usd": round(usd, 6),
             "reported": ticks is not None}
    with spend_lock, SPEND_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def spend_summary():
    today, month = date.today().isoformat(), date.today().isoformat()[:7]
    tot = {"today": 0.0, "month": 0.0, "all": 0.0, "calls_today": 0, "calls_all": 0, "since": None}
    if SPEND_LOG.exists():
        for line in SPEND_LOG.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            tot["since"] = tot["since"] or e["ts"][:10]
            tot["all"] += e["usd"]
            tot["calls_all"] += 1
            if e["ts"].startswith(month):
                tot["month"] += e["usd"]
            if e["ts"].startswith(today):
                tot["today"] += e["usd"]
                tot["calls_today"] += 1
    return {k: round(v, 4) if isinstance(v, float) else v for k, v in tot.items()}


MGMT_URL = "https://management-api.x.ai/v1/billing/teams/{team}"
_balance_cache = {"at": 0, "data": None}


def xai_balance():
    """Prepaid credit balance from xAI's Management API (needs a management key + team id)."""
    key, team = os.environ.get("XAI_MANAGEMENT_KEY"), os.environ.get("XAI_TEAM_ID")
    if not (key and team):
        return {"configured": False}
    if time.time() - _balance_cache["at"] < 60 and _balance_cache["data"]:
        return _balance_cache["data"]
    base, hdr = MGMT_URL.format(team=team), {"Authorization": f"Bearer {key}"}
    out = {"configured": True}
    try:
        r = requests.get(base + "/prepaid/balance", headers=hdr, timeout=20)
        r.raise_for_status()
        # The ledger is in USD cents with credits negative (a $10 top-up is "-1000").
        out["prepaid_usd"] = -float(r.json()["total"]["val"]) / 100
    except Exception as e:
        out["error"] = str(e)[:200]
    _balance_cache.update(at=time.time(), data=out)
    return out


def save_png(raw: bytes, path: Path):
    Image.open(io.BytesIO(raw)).save(path, "PNG")


# ---------- background job ----------

job = {"running": False, "done": 0, "total": 0, "current": [], "files": [], "errors": [], "folder": None}
lock = threading.Lock()


def label_for(dress: Path, person: Path):
    lib = load_library()
    d = lib["dresses"].get(dress.name, {}).get("alias") or dress.stem
    p = lib["people"].get(person.name, {}).get("alias") or person.stem
    return f"{d} on {p}"


def start_job(tasks, folder: Path):
    """tasks: list of (label, fn) or (label, fn, result_filename). Returns an error response if busy."""
    with lock:
        if job["running"]:
            return jsonify(error="A job is already running, wait for it to finish."), 409
        job.update(running=bool(tasks), done=0, total=len(tasks), current=[], errors=[],
                   files=[t[2] for t in tasks if len(t) > 2],
                   folder=folder.name)
    if tasks:
        threading.Thread(target=run_job, args=(tasks,), daemon=True).start()
    return None


def run_job(tasks):
    def work(task):
        label, fn, *rest = task
        file = rest[0] if rest else None
        with lock:
            job["current"].append(label)
        try:
            fn()
        except Exception as e:  # keep the batch going
            app.logger.error("%s failed: %s", label, e)
            with lock:
                job["errors"].append(f"{label}: {e}")
        finally:
            with lock:
                job["current"].remove(label)
                if file:
                    job["files"].remove(file)
                job["done"] += 1

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(work, tasks))
    with lock:
        job["running"] = False


def need_key():
    if not os.environ.get("XAI_API_KEY"):
        return jsonify(error="No Grok key yet. Put XAI_API_KEY=... in the .env file in the app's folder, then restart the app."), 400
    return None


def result_target(body):
    """Find the result file + its dress/person from {result} or {dress, person} (aliases ok)."""
    folder = render_dir()
    if body.get("folder"):
        folder = BASE / Path(body["folder"]).name
        if not folder_date(folder):
            abort(jsonify(error="Unknown results folder."), 404)
    if body.get("result"):
        path = folder / Path(body["result"]).name
        d, p = parse_result(path.name)
    else:
        # From chat: newest day that has this pair, else today.
        d, p = resolve("dresses", body.get("dress")), resolve("people", body.get("person"))
        path = None
        if d and p:
            path = next((f / out_name(d, p) for f in all_render_dirs() if (f / out_name(d, p)).exists()),
                        folder / out_name(d, p))
    if not (path and d and p):
        abort(jsonify(error="Couldn't find that dress/person/result."), 404)
    return path, d, p


# ---------- routes ----------

@app.get("/")
def index():
    if not load_settings()["plain_view"]:
        return redirect(url_for("atelier"))
    return render_template("index.html", dress_dir=dress_dir(), people_dir=people_dir(),
                           price=PRICE_PER_IMAGE, has_key=bool(os.environ.get("XAI_API_KEY")))


@app.get("/atelier")
def atelier():
    """Prototype: the app as an illustrated dressmaker's workroom."""
    return render_template("atelier.html", price=PRICE_PER_IMAGE, has_key=bool(os.environ.get("XAI_API_KEY")),
                           plain_view=load_settings()["plain_view"])


@app.get("/api/state")
def state():
    folder = render_dir()
    results = []
    for p in (p for f in all_render_dirs() for p in list_images(f)):
        d, pe = parse_result(p.name)
        results.append({"file": p.name, "folder": p.parent.name, "key": key_of(p),
                        "date": folder_date(p.parent).isoformat(), "dress": d.name if d else None,
                        "person": pe.name if pe else None,
                        "take": split_result(p.name)[2],
                        "group": "__".join(map(str, split_result(p.name)[:2])),
                        "version": int(p.stat().st_mtime_ns // 1000),
                        "undos": len(previous_versions(p))})
    results.sort(key=lambda r: (-int(r["date"].replace("-", "")), split_result(r["file"])))
    return jsonify(dresses=[p.name for p in dresses()], people=[p.name for p in people()],
                   folder=folder.name, results=results, library=load_library())


@app.post("/api/meta")
def set_meta():
    b = request.get_json(force=True)
    kind = b.get("kind")
    if kind not in ("dresses", "people"):
        abort(400)
    with lib_lock:
        lib = load_library()
        lib[kind][b["name"]] = {"alias": (b.get("alias") or "").strip(),
                                "notes": (b.get("notes") or "").strip()}
        save_library(lib)
    return jsonify(ok=True)


def plan_pairs(items, folder: Path):
    """Explicit {dress, person, retake, note} pairs (the atelier's logbook)
    -> ([(dress, person, target, retake, note)], skipped).
    A new pair renders into today's folder; an existing pair with retake gets the next free take beside it
    and, without a note of its own, keeps the request the existing look was made with."""
    ds, ps = {p.name: p for p in dresses()}, {p.name: p for p in people()}
    days = all_render_dirs() or [folder]
    plan, skipped = [], 0
    for item in items:
        d, p = ds.get(item.get("dress")), ps.get(item.get("person"))
        existing = d and p and next((f / out_name(d, p) for f in days if (f / out_name(d, p)).exists()), None)
        if not (d and p) or (existing and not item.get("retake")):
            skipped += 1
            continue
        target = next_take_path(existing) if existing else folder / out_name(d, p)
        if any(x[2] == target for x in plan):
            skipped += 1
            continue
        note = (item.get("note") or "").strip()[:600] or (get_request(key_of(existing)) if existing else "")
        plan.append((d, p, target, bool(existing), note))
    return plan, skipped


def render_into(person: Path, dress: Path, target: Path, kind: str, note=""):
    try:
        save_png(try_on(person, dress, kind, note), target)
        set_request(key_of(target), note)
    finally:
        if target.exists() and target.stat().st_size == 0:
            target.unlink()  # a reserved take that failed


def render_pairs(items):
    folder = render_dir()
    folder.mkdir(exist_ok=True)
    with lock:
        plan, skipped = plan_pairs(items, folder)
        for _d, _p, target, retake, _n in plan:
            if retake:
                target.touch()  # reserve the take number
    tasks = [(label_for(d, p) + (" (new take)" if retake else ""),
              lambda d=d, p=p, t=target, k="newtake" if retake else "tryon", n=note: render_into(p, d, t, k, n),
              *([key_of(target)] if retake else []))
             for d, p, target, retake, note in plan]
    if err := start_job(tasks, folder):
        for _d, _p, target, retake, _n in plan:
            if retake:
                target.unlink(missing_ok=True)
        return err
    return jsonify(started=len(plan), skipped=skipped, folder=folder.name, hint="")


@app.post("/api/gallery")
def gallery():
    """Hang a result in one of the atelier's three frames (key None takes it down)."""
    b = request.get_json(force=True)
    slot, key = b.get("slot"), b.get("key")
    if slot not in (0, 1, 2):
        return jsonify(error="There are only three frames."), 400
    if key is not None:
        fold, _, name = key.partition("/")
        path = BASE / Path(fold).name / Path(name).name
        if not (folder_date(path.parent) and path.is_file()):
            return jsonify(error="That look doesn't exist."), 404
        key = key_of(path)
    with lib_lock:
        lib = load_library()
        lib["gallery"][slot] = key
        save_library(lib)
    return jsonify(ok=True, gallery=lib["gallery"])


@app.post("/render")
def render():
    if err := need_key():
        return err
    want = request.get_json(silent=True) or {}
    if "pairs" in want:
        return render_pairs(want["pairs"])
    ds, ps = dresses(), people()
    if "dresses" in want:
        ds = [p for p in ds if p.name in set(want["dresses"])]
    if "people" in want:
        ps = [p for p in ps if p.name in set(want["people"])]
    if not ds or not ps:
        return jsonify(error="Select at least one dress and one person."), 400
    folder = render_dir()
    folder.mkdir(exist_ok=True)
    days = all_render_dirs() or [folder]  # a pair done on any day counts as done
    pairs = [(d, p) for d in ds for p in ps
             if not any((f / out_name(d, p)).exists() for f in days)]
    tasks = [(label_for(d, p), lambda d=d, p=p: render_into(p, d, folder / out_name(d, p), "tryon"))
             for d, p in pairs]
    if err := start_job(tasks, folder):
        return err
    skipped = len(ds) * len(ps) - len(pairs)
    hint = ("Use ⧉ Duplicate or ＋ New take on a result to make another version."
            if skipped and not pairs else "")
    return jsonify(started=len(pairs), skipped=skipped, folder=folder.name, hint=hint)


@app.post("/api/redo")
def redo():
    if err := need_key():
        return err
    path, d, p = result_target(request.get_json(force=True))
    path.parent.mkdir(exist_ok=True)

    note = get_request(key_of(path))  # keep the request the look was made with

    def task():
        raw = try_on(p, d, "redo", note)
        archive(path)
        save_png(raw, path)

    if err := start_job([(f"Redo {label_for(d, p)}", task, key_of(path))], path.parent):
        return err
    return jsonify(started=1, result=path.name, key=key_of(path))


@app.post("/api/fix")
def fix_route():
    if err := need_key():
        return err
    b = request.get_json(force=True)
    note = (b.get("note") or "").strip()
    if not note:
        return jsonify(error="Describe what's wrong."), 400
    path, d, p = result_target(b)
    if not path.exists():
        return jsonify(error="There's no result for that pair yet, run Try On first."), 404
    if b.get("remember"):
        with lib_lock:
            lib = load_library()
            m = lib["dresses"].setdefault(d.name, {"alias": "", "notes": ""})
            old = m.get("notes", "").strip().rstrip(".")
            m["notes"] = f"{old}. {note}" if old else note
            save_library(lib)

    def task():
        raw = fix(path, d, p, note)
        archive(path)
        save_png(raw, path)

    if err := start_job([(f"Fix {label_for(d, p)}", task, key_of(path))], path.parent):
        return err
    return jsonify(started=1, result=path.name, key=key_of(path))


@app.post("/api/duplicate")
def duplicate():
    """Copy a result (and its undo history) into a new take, free and instant."""
    path, d, p = result_target(request.get_json(force=True))
    if not path.exists():
        return jsonify(error="There's no result to duplicate."), 404
    with lock:
        new = next_take_path(path)
        shutil.copy2(path, new)
        for old in previous_versions(path):
            suffix = old.stem[len(path.stem) + 1:]
            shutil.copy2(old, old.parent / f"{new.stem}.{suffix}{new.suffix}")
    set_request(key_of(new), get_request(key_of(path)))
    return jsonify(ok=True, result=new.name, key=key_of(new))


@app.post("/api/newtake")
def newtake():
    """Render a fresh try-on of the same dress + person into a new take."""
    if err := need_key():
        return err
    path, d, p = result_target(request.get_json(force=True))
    with lock:
        new = next_take_path(path)
        new.touch()  # reserve the name so a quick second click gets the next number
    note = get_request(key_of(path))

    def task():
        try:
            save_png(try_on(p, d, "newtake", note), new)
            set_request(key_of(new), note)
        finally:
            if new.exists() and new.stat().st_size == 0:
                new.unlink()
    if err := start_job([(f"New take {label_for(d, p)}", task, key_of(new))], path.parent):
        new.unlink(missing_ok=True)
        return err
    return jsonify(started=1, result=new.name, key=key_of(new))


@app.post("/api/undo")
def undo_route():
    path, d, p = result_target(request.get_json(force=True))
    with lock:
        if key_of(path) in job["files"]:
            return jsonify(error="That result is being worked on right now."), 409
        if not undo(path):
            return jsonify(error="Nothing to undo for that result."), 404
    return jsonify(ok=True, result=path.name, remaining=len(previous_versions(path)))


@app.get("/api/versions")
def versions_route():
    """The current image plus its archived versions, newest first."""
    path, d, p = result_target(request.args)
    made = lambda f: datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes")
    out = [{"name": path.name, "src": path.name, "current": True, "made": made(path)}] if path.exists() else []
    out += [{"name": f.name, "src": f"_previous/{f.name}", "current": False, "made": made(f)}
            for f in reversed(previous_versions(path))]
    return jsonify(versions=out)


@app.post("/api/use_version")
def use_version():
    """Bring back an archived version. The current one is archived first, so this can be undone too."""
    b = request.get_json(force=True)
    path, d, p = result_target(b)
    with lock:
        if key_of(path) in job["files"]:
            return jsonify(error="That result is being worked on right now."), 409
        old = next((f for f in previous_versions(path) if f.name == Path(b.get("version") or "").name), None)
        if not old:
            return jsonify(error="That version is gone."), 404
        archive(path)
        shutil.copy(old, path)
    return jsonify(ok=True, result=path.name, key=key_of(path))


@app.post("/api/delete")
def delete_route():
    path, d, p = result_target(request.get_json(force=True))
    with lock:
        if key_of(path) in job["files"]:
            return jsonify(error="That result is being worked on right now."), 409
        if not path.exists():
            return jsonify(error="That result doesn't exist."), 404
        item = delete_result(path)
    return jsonify(ok=True, id=f"{path.parent.name}/{item.name}")


@app.get("/api/trash")
def trash():
    return jsonify(items=deleted_items())


@app.post("/api/restore")
def restore_route():
    day_name, _, item_name = (request.get_json(force=True).get("id") or "").partition("/")
    day = BASE / Path(day_name).name
    item = day / "_deleted" / Path(item_name).name
    if not (folder_date(day) and item_name and item.is_dir() and list_images(item)):
        return jsonify(error="That deleted result is gone."), 404
    old_key = f"{day.name}/{list_images(item)[0].name}"
    with lock:
        target = restore_item(day, item)
    if key_of(target) != old_key:  # it came back as a new take; its request follows it
        set_request(key_of(target), get_request(old_key))
    return jsonify(ok=True, result=target.name, key=key_of(target))


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    return jsonify(plain_view=s["plain_view"],
                   dresses=folder_report(as_dir(s["dress_dir"]), "dresses"),
                   models=folder_report(as_dir(s["models_dir"]), "models"))


@app.post("/api/settings")
def set_settings():
    b = request.get_json(force=True)
    with lock:
        if job["running"]:
            return jsonify(error="Fittings are being made right now. Change settings when they're done."), 409
    s = load_settings()
    for key, label in (("dress_dir", "dresses"), ("models_dir", "models")):
        if key in b:
            path = as_dir(b[key])
            if not path.is_dir():
                return jsonify(error=f"The {label} folder doesn't exist: {path}"), 400
            s[key] = str(path)
    if "plain_view" in b:
        s["plain_view"] = bool(b["plain_view"])
    save_settings(s)
    d, m = folder_report(as_dir(s["dress_dir"]), "dresses"), folder_report(as_dir(s["models_dir"]), "models")
    warn = [w for w, bad in (("No dress PNGs in that dresses folder.", not d["count"]),
                             ("No model photos in that models folder.", not m["count"])) if bad]
    return jsonify(ok=True, plain_view=s["plain_view"], dresses=d, models=m, warnings=warn)


@app.get("/api/browse")
def browse():
    """Sub-folders of a folder, for picking the dresses/models folder. Lists folder names only, never file contents."""
    asked = as_dir(request.args.get("path") or Path.home())
    path = asked if asked.is_dir() else Path.home()
    try:
        subs = sorted((p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name.lower())
    except PermissionError:
        subs = []
    return jsonify(path=str(path), exists=asked.is_dir(), parent=str(path.parent) if path.parent != path else None,
                   dresses=folder_report(path, "dresses")["count"], models=folder_report(path, "models")["count"],
                   folders=[{"name": p.name, "path": str(p)} for p in subs[:300]])


@app.get("/api/spend")
def spend():
    return jsonify(local=spend_summary(), xai=xai_balance())


@app.get("/status")
def status():
    with lock:
        return jsonify(job)


@app.get("/img/dress/<path:name>")
def dress_img(name):
    return send_from_directory(dress_dir(), name)


@app.get("/img/person/<path:name>")
def person_img(name):
    return send_from_directory(people_dir(), name)


@app.get("/output/<folder>/<path:name>")
def output_img(folder, name):
    if not folder.startswith("Render"):
        abort(404)
    return send_from_directory(BASE / folder, name)


if __name__ == "__main__":
    DEFAULT_PEOPLE_DIR.mkdir(exist_ok=True)
    app.run(host="127.0.0.1", port=5000, debug=False)
