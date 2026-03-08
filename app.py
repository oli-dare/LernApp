# =============================================================================
# StudyFyn – KI-Lernhelfer
# =============================================================================

import io
import json
import os
import random
import re
import sqlite3
import uuid
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as st_components
import google.generativeai as genai
from PIL import Image, ImageOps

# =============================================================================
# KONFIGURATION
# =============================================================================

GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# Home-Dir überlebt Redeployments; App-Dir wird bei jedem Push gelöscht
_db_dir = Path.home() / ".studyfyn"
_db_dir.mkdir(parents=True, exist_ok=True)
DB_PATH = os.environ.get("STUDYFYN_DB_PATH") or str(_db_dir / "studyfyn_data.db")

# =============================================================================
# DATENBANK
# =============================================================================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL DEFAULT 'legacy',
        name TEXT NOT NULL,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS packs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL DEFAULT 'legacy',
        name TEXT NOT NULL,
        created TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        cards TEXT NOT NULL,
        folder_id INTEGER,
        FOREIGN KEY(folder_id) REFERENCES folders(id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS progress (
        pack_id INTEGER NOT NULL,
        card_idx INTEGER NOT NULL,
        streak INTEGER NOT NULL DEFAULT 0,
        UNIQUE(pack_id, card_idx),
        FOREIGN KEY(pack_id) REFERENCES packs(id)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS user_profiles (
        user_id TEXT PRIMARY KEY,
        xp INTEGER NOT NULL DEFAULT 0,
        username TEXT NOT NULL DEFAULT "Du"
    )''')
    # Migrationen für bestehende DBs
    for stmt in [
        'ALTER TABLE packs ADD COLUMN folder_id INTEGER REFERENCES folders(id)',
        'ALTER TABLE packs ADD COLUMN user_id TEXT NOT NULL DEFAULT "legacy"',
        'ALTER TABLE folders ADD COLUMN user_id TEXT NOT NULL DEFAULT "legacy"',
    ]:
        try:
            conn.execute(stmt)
        except Exception:
            pass
    conn.commit()
    conn.close()


init_db()

# --- User-Profil-Funktionen ---

def db_get_xp(user_id):
    conn = get_db()
    row = conn.execute('SELECT xp FROM user_profiles WHERE user_id=?', (user_id,)).fetchone()
    conn.close()
    return row['xp'] if row else 0


def db_set_xp(xp, user_id):
    conn = get_db()
    conn.execute('''
        INSERT INTO user_profiles (user_id, xp, username) VALUES (?, ?, "Du")
        ON CONFLICT(user_id) DO UPDATE SET xp=excluded.xp
    ''', (user_id, xp))
    conn.commit()
    conn.close()


def db_get_username(user_id):
    conn = get_db()
    row = conn.execute('SELECT username FROM user_profiles WHERE user_id=?', (user_id,)).fetchone()
    conn.close()
    return row['username'] if row and row['username'] else 'Du'


def db_set_username(username, user_id):
    conn = get_db()
    conn.execute('''
        INSERT INTO user_profiles (user_id, xp, username) VALUES (?, 0, ?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
    ''', (user_id, username))
    conn.commit()
    conn.close()


def db_get_all_users():
    conn = get_db()
    rows = conn.execute('SELECT user_id, xp, username FROM user_profiles ORDER BY xp DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Pack-Funktionen ---

def db_save_pack(name, cards_json, folder_id=None, user_id='legacy'):
    conn = get_db()
    cur = conn.execute('INSERT INTO packs (name, cards, folder_id, user_id) VALUES (?, ?, ?, ?)',
                       (name, cards_json, folder_id, user_id))
    pack_id = cur.lastrowid
    conn.commit()
    conn.close()
    return pack_id


def db_load_packs(folder_id=None, user_id='legacy'):
    conn = get_db()
    if folder_id is not None:
        rows = conn.execute(
            'SELECT * FROM packs WHERE folder_id = ? AND user_id = ? ORDER BY created DESC',
            (folder_id, user_id)).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM packs WHERE folder_id IS NULL AND user_id = ? ORDER BY created DESC',
            (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_save_progress(pack_id, card_idx, streak):
    conn = get_db()
    conn.execute('''INSERT INTO progress (pack_id, card_idx, streak) VALUES (?, ?, ?)
                    ON CONFLICT(pack_id, card_idx) DO UPDATE SET streak = ?''',
                 (pack_id, card_idx, streak, streak))
    conn.commit()
    conn.close()


def db_load_progress(pack_id):
    conn = get_db()
    rows = conn.execute('SELECT card_idx, streak FROM progress WHERE pack_id = ?', (pack_id,)).fetchall()
    conn.close()
    return {r['card_idx']: r['streak'] for r in rows}


def db_delete_pack(pack_id):
    conn = get_db()
    conn.execute('DELETE FROM progress WHERE pack_id = ?', (pack_id,))
    conn.execute('DELETE FROM packs WHERE id = ?', (pack_id,))
    conn.commit()
    conn.close()


def db_move_pack(pack_id, folder_id):
    conn = get_db()
    conn.execute('UPDATE packs SET folder_id = ? WHERE id = ?', (folder_id, pack_id))
    conn.commit()
    conn.close()


# --- Ordner-Funktionen ---

def db_create_folder(name, user_id='legacy'):
    conn = get_db()
    cur = conn.execute('INSERT INTO folders (name, user_id) VALUES (?, ?)', (name, user_id))
    fid = cur.lastrowid
    conn.commit()
    conn.close()
    return fid


def db_load_folders(user_id='legacy'):
    conn = get_db()
    rows = conn.execute('SELECT * FROM folders WHERE user_id = ? ORDER BY created DESC', (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_delete_folder(folder_id):
    conn = get_db()
    conn.execute('UPDATE packs SET folder_id = NULL WHERE folder_id = ?', (folder_id,))
    conn.execute('DELETE FROM folders WHERE id = ?', (folder_id,))
    conn.commit()
    conn.close()


def db_count_packs_in_folder(folder_id, user_id='legacy'):
    conn = get_db()
    c = conn.execute('SELECT COUNT(*) as c FROM packs WHERE folder_id = ? AND user_id = ?',
                     (folder_id, user_id)).fetchone()['c']
    conn.close()
    return c


# =============================================================================
# GERÄTE-ID (dauerhaft per localStorage im Browser)
# =============================================================================

st_components.html("""
<script>
(function() {
    // UID aus localStorage ODER Cookie lesen (beide sichern gegenseitig ab)
    function getCookie(n) {
        var m = document.cookie.match('(^|;)\\s*' + n + '\\s*=\\s*([^;]+)');
        return m ? m.pop() : null;
    }
    var uid = localStorage.getItem('studyfyn_uid') || getCookie('studyfyn_uid');
    if (!uid) {
        uid = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
            var r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }
    // In beiden Speichern sichern
    try { localStorage.setItem('studyfyn_uid', uid); } catch(e) {}
    try { document.cookie = 'studyfyn_uid=' + uid + '; max-age=31536000; path=/; SameSite=Lax'; } catch(e) {}
    // In URL-Parameter schreiben damit Python es lesen kann
    try {
        var url = new URL(window.parent.location.href);
        if (url.searchParams.get('uid') !== uid) {
            url.searchParams.set('uid', uid);
            window.parent.history.replaceState({}, '', url.toString());
        }
    } catch(e) {}
})();
</script>
""", height=0)

_uid_from_url = st.query_params.get("uid", None)

# Cookie-Fallback (Streamlit 1.37+)
if not _uid_from_url:
    try:
        _uid_from_url = st.context.cookies.get("studyfyn_uid")
        if _uid_from_url:
            st.query_params["uid"] = _uid_from_url
    except Exception:
        pass

if _uid_from_url:
    st.session_state["user_id"] = _uid_from_url
elif "user_id" not in st.session_state:
    # Bis zu 3 Zyklen warten damit das JS die UID injizieren kann
    _wait = st.session_state.get("_uid_wait", 0)
    if _wait < 3:
        st.session_state["_uid_wait"] = _wait + 1
        st.rerun()
    else:
        _fallback_id = str(uuid.uuid4())
        st.session_state["user_id"] = _fallback_id
        st.query_params["uid"] = _fallback_id

_user_id = st.session_state["user_id"]

# XP und Name passend zum Geräte-Nutzer laden
if st.session_state.get("loaded_user_id") != _user_id:
    st.session_state["xp"] = db_get_xp(_user_id)
    st.session_state["sett_name"] = db_get_username(_user_id)
    st.session_state["loaded_user_id"] = _user_id

# =============================================================================
# NAVIGATION + GLOBALES CSS
# =============================================================================

if "page" not in st.query_params:
    st.query_params["page"] = "home"
active_page = st.query_params.get("page", "home")


def nav_class(page):
    return "nav-item active" if active_page == page else "nav-item"


def nav_color(page):
    return "#ffffff" if active_page == page else "#888"


st.markdown(f"""
<style>
footer {{ display: none !important; }}
.stAppDeployButton {{ display: none !important; }}
#MainMenu {{ display: none !important; }}
.stMainBlockContainer, .block-container {{ padding-bottom: 5em !important; }}
.bottom-nav {{
    position: fixed; left: 0; bottom: 0; width: 100vw;
    display: flex; justify-content: space-around; align-items: center;
    padding: 0.6em 0 0.4em 0;
    background: rgba(30,30,30,0.97); z-index: 999999;
    border-top: 2px solid rgba(220,220,220,0.2);
    pointer-events: auto;
}}
.nav-item {{
    display: flex; flex-direction: column; align-items: center;
    text-decoration: none !important; font-size: 1.7em; line-height: 1;
    padding: 0.2em 0.6em; border-radius: 1.5em; transition: background 0.2s;
    cursor: pointer;
}}
.nav-item:hover {{ text-decoration: none !important; }}
.nav-item.active {{
    background: rgba(255,255,255,0.18); color: #fff !important;
    text-decoration: none !important;
}}
.headline {{
    font-size: 1.25em !important; margin-top: -0.3em !important;
    margin-bottom: 0.4em !important; font-weight: 700;
}}
.plus-circle {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 32px; height: 32px; border-radius: 50%;
    border: 2px solid #666; font-size: 1.2em; color: #ccc;
    cursor: pointer; transition: border 0.15s, color 0.15s;
    text-decoration: none; margin-left: 0.3em;
}}
.plus-circle:hover {{ border-color: #ffd200; color: #ffd200; }}
.premium-box {{
    border: 2px solid #ffd200; border-radius: 16px; padding: 1.2em 1.3em;
    background: linear-gradient(135deg, rgba(255,210,0,0.07), rgba(255,150,30,0.04));
    box-shadow: 0 0 24px rgba(255,210,0,0.08); margin: 1em 0;
}}
.rank-gm {{
    display: inline-block; padding: 0.25em 0.7em; border-radius: 10px;
    border: 2px solid #ffd200;
    background: linear-gradient(135deg, rgba(255,210,0,0.18), rgba(255,150,30,0.08));
    box-shadow: 0 0 10px rgba(255,210,0,0.13); font-weight: 700;
}}
.lib-header-row {{
    display: flex; flex-direction: row; align-items: center;
    justify-content: flex-start; gap: 0.7em; margin-bottom: 0.4em;
}}
@media (max-width: 600px) {{
    .lib-header-row {{ flex-wrap: nowrap !important; }}
}}
</style>
<div class="bottom-nav">
    <a class="{nav_class('home')}"     href="?page=home&uid={_user_id}"     style="color:{nav_color('home')};">&#127968;</a>
    <a class="{nav_class('packs')}"    href="?page=packs&uid={_user_id}"    style="color:{nav_color('packs')};">&#128218;</a>
    <a class="{nav_class('ranking')}"  href="?page=ranking&uid={_user_id}"  style="color:{nav_color('ranking')};">&#127942;</a>
    <a class="{nav_class('settings')}" href="?page=settings&uid={_user_id}" style="color:{nav_color('settings')};">&#9881;&#65039;</a>
</div>
""", unsafe_allow_html=True)

# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def build_card_queue(num_cards):
    queue, seen = [], []
    streak = st.session_state.get("streak", {})
    for i in range(num_cards):
        if streak.get(i, 0) >= 7:
            continue
        queue.append({"idx": i, "is_review": False})
        seen.append(i)
        if len(seen) % 3 == 0 and len(seen) > 1:
            cands = [idx for idx in seen[:-1] if streak.get(idx, 0) < 4]
            if cands:
                queue.append({"idx": random.choice(cands), "is_review": True})
    return queue


def analyze_image_with_ai(image):
    # Bild auf 600px verkleinern + auf JPEG-Bytes reduzieren
    # → Bytes direkt an Gemini (kein doppeltes Encoding durch SDK)
    img = image.convert("RGB")
    img.thumbnail((600, 600), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=60, optimize=True)
    img_bytes = buf.getvalue()

    prompt = (
        "Bild ansehen. Hauptthema (2-3 Wörter) + 3-5 Stichpunkte mit Emoji. "
        "Erste Zeile = Oberthema (kein Emoji). Danach Stichpunkte, je eine Zeile."
    )
    image_part = {"mime_type": "image/jpeg", "data": img_bytes}
    try:
        model = genai.GenerativeModel(
            "gemini-3.1-flash-lite-preview",
            generation_config=genai.GenerationConfig(max_output_tokens=120, temperature=0.1),
        )
        response = model.generate_content([prompt, image_part])
        return [line.strip() for line in response.text.split("\n") if line.strip()]
    except Exception as e:
        return [f"Fehler bei der KI-Analyse: {e}"]


def generate_srs_cards(topic, num_cards):
    prompt = (
        f"Erstelle genau {num_cards} SRS-Lernkarten auf Deutsch zum Thema: '{topic}'. "
        "Antworte NUR mit einem JSON-Array, kein Markdown, keine Erklärung. "
        "Jedes Objekt hat folgende Felder:\n"
        '[{"merke_dir": "Sehr kurzer, aber vollständiger, verständlicher Satz mit Subjekt, keine Platzhalter, '
        'keine Listen, keine Abkürzungen. Wichtige Begriffe fett (Markdown, z.B. **Französische Revolution**). '
        'Beispiel: **Die Französische Revolution** begann 1789.", '
        '"frage": "Sehr kurze, aber 100% eindeutige, relevante und didaktisch sinnvolle Frage, '
        'keine offenen/groben Fragen, keine Platzhalter, keine Listen, keine Abkürzungen. '
        'Wichtige Begriffe fett (Markdown). Beispiel: Wann begann die **Französische Revolution**?", '
        '"optionen": ["Option1", "Option2", "Option3"], "richtig": 0}]\n'
        '"richtig" ist der 0-basierte Index der EINZIG richtigen Antwort in "optionen". '
        'Die anderen Optionen sind klar falsch oder eindeutig abgrenzbar. '
        'Alle Antwortoptionen sind ca. 2-6 Wörter lang, keine Labels wie A/B/C, keine Sätze, keine Erklärungen. '
        'Die Fragen und Antworten müssen logisch, eindeutig und für das Oberthema wirklich relevant sein. '
        'Keine Trivia, sondern Kernwissen.\n'
        f"Exakt {num_cards} Karten, nur das JSON-Array."
    )
    try:
        model = genai.GenerativeModel("gemini-3.1-flash-lite-preview")
        response = model.generate_content(prompt)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rstrip("`").strip()
        return json.loads(raw)
    except Exception as e:
        st.error(f"Fehler bei der Kartenerstellung: {e}")
        return []


def generate_srs_cards_no_duplicates(topic, num_cards, existing_cards):
    existing_questions = [c.get("frage", "") for c in existing_cards]
    existing_facts = [c.get("merke_dir", "") for c in existing_cards]
    existing_summary = "\n".join(
        f"- {q}" for q in existing_questions if q
    )
    prompt = (
        f"Erstelle genau {num_cards} SRS-Lernkarten auf Deutsch zum Thema: '{topic}'. "
        "Die Karten MÜSSEN thematisch zum Oberthema passen. "
        "WICHTIG: Es gibt bereits diese Karten im Paket – erzeuge KEINE Wiederholungen "
        "und keine inhaltlich gleichen Fragen:\n"
        f"{existing_summary}\n\n"
        "Antworte NUR mit einem JSON-Array, kein Markdown, keine Erklärung. "
        "Jedes Objekt hat folgende Felder:\n"
        '[{"merke_dir": "Sehr kurzer, aber vollständiger, verständlicher Satz mit Subjekt, keine Platzhalter, '
        'keine Listen, keine Abkürzungen. Wichtige Begriffe fett (Markdown, z.B. **Französische Revolution**). '
        'Beispiel: **Die Französische Revolution** begann 1789.", '
        '"frage": "Sehr kurze, aber 100% eindeutige, relevante und didaktisch sinnvolle Frage, '
        'keine offenen/groben Fragen, keine Platzhalter, keine Listen, keine Abkürzungen. '
        'Wichtige Begriffe fett (Markdown). Beispiel: Wann begann die **Französische Revolution**?", '
        '"optionen": ["Option1", "Option2", "Option3"], "richtig": 0}]\n'
        '"richtig" ist der 0-basierte Index der EINZIG richtigen Antwort in "optionen". '
        'Die anderen Optionen sind klar falsch oder eindeutig abgrenzbar. '
        'Alle Antwortoptionen sind ca. 2-6 Wörter lang, keine Labels wie A/B/C, keine Sätze, keine Erklärungen. '
        'Die Fragen und Antworten müssen logisch, eindeutig und für das Oberthema wirklich relevant sein. '
        'Keine Trivia, sondern Kernwissen.\n'
        f"Exakt {num_cards} Karten, nur das JSON-Array."
    )
    try:
        model = genai.GenerativeModel("gemini-3.1-flash-lite-preview")
        response = model.generate_content(prompt)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rstrip("`").strip()
        return json.loads(raw)
    except Exception as e:
        st.error(f"Fehler bei der Kartenerstellung: {e}")
        return []


def make_lueckentext(merke_dir, correct_answer):
    answer_clean = re.sub(r'\*\*', '', correct_answer).strip()
    if not answer_clean:
        return merke_dir + " (______)"
    result = re.sub(r'\*\*' + re.escape(answer_clean) + r'\*\*',
                    '**______**', merke_dir, flags=re.IGNORECASE, count=1)
    if result == merke_dir:
        result = re.sub(re.escape(answer_clean), '______', merke_dir, flags=re.IGNORECASE, count=1)
    if result == merke_dir:
        result = merke_dir.rstrip('.').rstrip() + ': ______'
    return result


def check_answer(user_input, correct_answer):
    return user_input.strip().lower() == re.sub(r'\*\*', '', correct_answer).strip().lower()


def handle_answer(correct, card_idx, streak, mastered, xp, cur_streak, pid):
    """Verarbeitet eine richtige oder falsche Antwort. Gibt (xp_gain,) zurück."""
    if correct:
        ns = cur_streak + 1
        streak[card_idx] = ns
        st.session_state.streak = streak
        xp_gain = 0
        if cur_streak == 0:
            xp_gain += 30
        if ns >= 7 and cur_streak < 7:
            st.session_state.mastered = mastered + 1
            xp_gain += 100
        st.session_state.xp = xp + xp_gain
        db_set_xp(st.session_state.xp, _user_id)
        st.session_state.last_xp_gain = xp_gain
        db_save_progress(pid, card_idx, ns)
        st.session_state.card_mode = "correct"
    else:
        streak[card_idx] = 0
        st.session_state.streak = streak
        st.session_state.last_xp_gain = 0
        db_save_progress(pid, card_idx, 0)
        st.session_state.card_mode = "wrong"
    st.rerun()


def open_pack(pack):
    cards = json.loads(pack["cards"])
    progress = db_load_progress(pack["id"])
    total = len(cards)
    streak = {i: progress.get(i, 0) for i in range(total)}
    mastered_count = sum(1 for s in progress.values() if s >= 7)
    st.session_state.active_pack_id = pack["id"]
    st.session_state.main_topic = pack["name"]
    st.session_state.cards = cards
    st.session_state.streak = streak
    st.session_state.mastered = mastered_count
    st.session_state.queue = build_card_queue(total)
    st.session_state.queue_pos = 0
    st.session_state.new_card_pos = 0
    queue = st.session_state.queue
    if queue and streak.get(queue[0]["idx"], 0) > 0:
        st.session_state.card_mode = "question"
    else:
        st.session_state.card_mode = "merke_dir"
    st.session_state.pack_started = True
    st.rerun()


# =============================================================================
# SEITE: HOME
# =============================================================================
if active_page == "home":
    st.markdown('<div class="headline">Neues Paket erstellen</div>', unsafe_allow_html=True)

    uploaded_file = st.file_uploader(
        "Lade ein Foto deiner Buchseite/Notiz hoch", type=["png", "jpg", "jpeg"]
    )

    if uploaded_file is not None:
        if ("last_uploaded_file" not in st.session_state or
                st.session_state.last_uploaded_file != uploaded_file):
            image = Image.open(uploaded_file)
            image = ImageOps.exif_transpose(image)
            if image.width > image.height:
                image = image.rotate(90, expand=True)
            st.session_state.last_uploaded_file = uploaded_file
            st.session_state.last_image = image
            st.session_state.aufbereitet = False

        image = st.session_state.get("last_image")
        st.image(image, caption="Hochgeladenes Bild", use_container_width=True)

        if st.button("🔍 Mit KI analysieren"):
            with st.spinner("KI analysiert das Bild..."):
                bullets = analyze_image_with_ai(image)
            st.session_state.aufbereitet = True
            st.session_state.bullets = bullets

        if st.session_state.get("aufbereitet"):
            bullets = st.session_state.get("bullets", [])
            main_topic = bullets[0] if bullets else "Thema"
            subpoints = bullets[1:] if len(bullets) > 1 else []

            st.subheader("Das erwartet dich:")
            st.markdown(f"**{main_topic}**")
            for sub in subpoints:
                st.write(sub)
            st.divider()

            st.subheader("Wie viele Karteikarten möchtest du?")
            pack_option = st.radio(
                "Wähle dein Lernpaket:",
                ("🚀 Quick Pack (5-7 Karten)", "🌟 Focus Pack (10-15 Karten)"),
                index=0
            )

            if st.button("Auswahl bestätigen"):
                num_cards = 6 if "Quick" in pack_option else 12
                main_topic = bullets[0] if bullets else "Thema"
                with st.spinner("KI erstellt deine Lernkarten..."):
                    cards = generate_srs_cards(main_topic, num_cards)
                if not cards:
                    st.error("Kartenerstellung fehlgeschlagen.")
                    st.stop()
                st.session_state.main_topic = main_topic
                st.session_state.cards = cards
                st.session_state.streak = {i: 0 for i in range(len(cards))}
                st.session_state.queue = build_card_queue(len(cards))
                st.session_state.queue_pos = 0
                st.session_state.new_card_pos = 0
                st.session_state.card_mode = None
                st.session_state.aufbereitet = False
                st.session_state.mastered = 0
                st.session_state.pack_started = False
                pack_id = db_save_pack(main_topic, json.dumps(cards), user_id=_user_id)
                st.session_state.active_pack_id = pack_id
                st.query_params["page"] = "packs"
                st.rerun()

# =============================================================================
# SEITE: BIBLIOTHEK (Packs + Ordner)
# =============================================================================
elif active_page == "packs":

    if st.session_state.get("pack_started", False):
        if st.button("⬅️ Zurück"):
            st.session_state.pack_started = False
            st.rerun()

    if not st.session_state.get("pack_started", False):
        current_folder = st.session_state.get("current_folder")

        # ---- HEADER ----
        if current_folder:
            folders = db_load_folders(_user_id)
            folder_name = next((f["name"] for f in folders if f["id"] == current_folder), "Ordner")
            h1, h2 = st.columns([8, 1])
            with h1:
                st.markdown(f'<div class="headline">📁 {folder_name}</div>', unsafe_allow_html=True)
            with h2:
                if st.button("⬅️", key="back_from_folder"):
                    st.session_state.pop("current_folder", None)
                    st.rerun()
        else:
            _lh1, _lh2 = st.columns([10, 1])
            with _lh1:
                st.markdown('<div class="headline">Bibliothek</div>', unsafe_allow_html=True)
            with _lh2:
                with st.popover("➕"):
                    if st.button("📁 Neuer Ordner", use_container_width=True):
                        st.session_state.creating_folder = True
                        st.rerun()
                    if st.button("📦 Neues Paket", use_container_width=True):
                        st.query_params["page"] = "home"
                        st.rerun()

        # ---- Ordner erstellen ----
        if st.session_state.get("creating_folder"):
            with st.form("create_folder_form"):
                fname = st.text_input("Ordnername:")
                if st.form_submit_button("Erstellen"):
                    if fname.strip():
                        db_create_folder(fname.strip(), _user_id)
                    st.session_state.creating_folder = False
                    st.rerun()

        # ---- Ordner anzeigen (nur Root-Ebene) ----
        if not current_folder:
            folders = db_load_folders(_user_id)
            for folder in folders:
                fid = folder["id"]
                fname = folder["name"]
                fcount = db_count_packs_in_folder(fid, _user_id)
                with st.container(border=True):
                    cols = st.columns([10, 1])
                    with cols[0]:
                        if st.button(f"📁 {fname}  ·  {fcount} Paket{'e' if fcount != 1 else ''}",
                                     key=f"fopen_{fid}", use_container_width=True):
                            st.session_state.current_folder = fid
                            st.rerun()
                    with cols[1]:
                        with st.popover("⋯"):
                            if st.button("🗑️ Ordner löschen", key=f"fdel_{fid}"):
                                db_delete_folder(fid)
                                st.rerun()

        # ---- Packs anzeigen ----
        packs = db_load_packs(folder_id=current_folder, user_id=_user_id)
        all_folders = db_load_folders(_user_id)

        if not packs and not (not current_folder and all_folders):
            st.info("Noch keine Pakete vorhanden. Erstelle ein neues über ➕ oder auf der Startseite.")

        for pack in packs:
            pack_id = pack["id"]
            name = pack["name"]
            cards = json.loads(pack["cards"])
            progress = db_load_progress(pack_id)
            total = len(cards)
            mastered_count = sum(1 for s in progress.values() if s >= 7)

            with st.container(border=True):
                cols = st.columns([10, 1])
                with cols[0]:
                    if st.button(
                        f"📦 {name}\n{total} Karten · {mastered_count}/{total} gemeistert",
                        key=f"packbtn_{pack_id}", use_container_width=True,
                        help=f"{mastered_count} von {total} Karten gemeistert"
                    ):
                        open_pack(pack)
                with cols[1]:
                    with st.popover("⋯"):
                        if st.button("✏️ Umbenennen", key=f"rename_btn_{pack_id}", use_container_width=True):
                            st.session_state[f"renaming_{pack_id}"] = True
                            st.rerun()
                        if st.button("➕ 10 KI-Karten", key=f"add10_btn_{pack_id}", use_container_width=True):
                            st.session_state[f"adding_cards_{pack_id}"] = True
                            st.rerun()
                        if st.button("📋 Duplizieren", key=f"dup_{pack_id}", use_container_width=True):
                            db_save_pack(name + " (Kopie)", pack["cards"], pack.get("folder_id"), _user_id)
                            st.rerun()
                        if all_folders:
                            st.caption("📂 In Ordner verschieben:")
                            for folder in all_folders:
                                if st.button(f"→ {folder['name']}", key=f"mv_{pack_id}_{folder['id']}"):
                                    db_move_pack(pack_id, folder['id'])
                                    st.rerun()
                        if pack.get("folder_id"):
                            if st.button("↩️ Aus Ordner entfernen", key=f"unfold_{pack_id}", use_container_width=True):
                                db_move_pack(pack_id, None)
                                st.rerun()
                        if st.button("🗑️ Löschen", key=f"del_{pack_id}", use_container_width=True):
                            db_delete_pack(pack_id)
                            st.rerun()

                # ---- Umbenennen (erscheint unter dem Paket) ----
                if st.session_state.get(f"renaming_{pack_id}"):
                    new_pack_name = st.text_input("Neuer Paketname:", value=name, key=f"rename_{pack_id}")
                    rc1, rc2 = st.columns(2)
                    with rc1:
                        if st.button("💾 Speichern", key=f"save_rename_{pack_id}", use_container_width=True):
                            if new_pack_name.strip() and new_pack_name != name:
                                conn = get_db()
                                conn.execute('UPDATE packs SET name = ? WHERE id = ?', (new_pack_name.strip(), pack_id))
                                conn.commit()
                                conn.close()
                            st.session_state.pop(f"renaming_{pack_id}", None)
                            st.rerun()
                    with rc2:
                        if st.button("❌ Abbrechen", key=f"cancel_rename_{pack_id}", use_container_width=True):
                            st.session_state.pop(f"renaming_{pack_id}", None)
                            st.rerun()

                # ---- 10 KI-Karten hinzufügen ----
                if st.session_state.get(f"adding_cards_{pack_id}"):
                    st.info(f"🤖 Erstelle 10 neue Karten zum Thema **{name}** ohne Wiederholungen…")
                    new_cards = generate_srs_cards_no_duplicates(name, 10, cards)
                    if new_cards:
                        all_cards = cards + new_cards
                        conn = get_db()
                        conn.execute('UPDATE packs SET cards = ? WHERE id = ?', (json.dumps(all_cards), pack_id))
                        conn.commit()
                        conn.close()
                        st.success(f"✅ {len(new_cards)} neue Karten hinzugefügt!")
                    st.session_state.pop(f"adding_cards_{pack_id}", None)
                    st.rerun()

    else:
        # =================================================================
        # AKTIVE LERNSESSION
        # =================================================================
        cards = st.session_state.cards
        topic = st.session_state.main_topic
        queue = st.session_state.queue
        pos = st.session_state.queue_pos
        mode = st.session_state.card_mode
        new_card_pos = st.session_state.get("new_card_pos", 0)
        streak = st.session_state.get("streak", {i: 0 for i in range(len(cards))})
        mastered = st.session_state.get("mastered", 0)
        xp = st.session_state.get("xp", 0)
        total_new = sum(1 for i in range(len(cards)) if streak.get(i, 0) < 7)
        capped_pos = min(new_card_pos, total_new)

        st.subheader(topic)
        st.progress(min(capped_pos / total_new if total_new else 1.0, 1.0),
                    text=f"Karte {capped_pos} von {total_new}")
        st.markdown(f"<div style='text-align:right;font-size:1em;margin-bottom:0.5em;'>🏅 <b>{xp:,} XP</b></div>",
                    unsafe_allow_html=True)

        mastered_pct = int((mastered / len(cards)) * 100) if cards else 0
        st.markdown(f"""
        <div style="margin-bottom:0.5em;">
            <span style="font-size:1em;">&#128081; Karten gemeistert: <b>{mastered} von {len(cards)}</b></span>
            <div style="background:#333; border-radius:8px; height:10px; margin-top:4px;">
                <div style="width:{mastered_pct}%; height:10px; border-radius:8px;
                    background: linear-gradient(90deg, #f7971e, #ffd200);"></div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        all_mastered = mastered >= len(cards)
        if all_mastered:
            st.success("🏆 Pack gemeistert! Alle Karten 7/7 – du bist der Boss!")
        elif not queue:
            st.info("Alle Karten in dieser Runde abgeschlossen.")
        else:
            real_pos = pos % len(queue)
            entry = queue[real_pos]
            card = cards[entry["idx"]]
            card_idx = entry["idx"]
            is_review = entry["is_review"]
            cur_streak = streak.get(card_idx, 0)
            pid = st.session_state.active_pack_id

            with st.container(border=True):
                top_left, top_right = st.columns([3, 1])
                if mode != "merke_dir":
                    with top_left:
                        if not is_review and cur_streak == 0:
                            st.caption("🆕 Neue Karte")
                        else:
                            st.caption("🔁 Wiederholungskarte")
                    with top_right:
                        if mode in ("question", "correct"):
                            st.markdown(
                                f"<div style='text-align:right;font-size:0.9em;'>{cur_streak}/7 ✅</div>",
                                unsafe_allow_html=True)
                        elif mode == "wrong":
                            st.markdown(
                                "<div style='text-align:right;font-size:0.9em;'>❌ Reset</div>",
                                unsafe_allow_html=True)

                if mode == "merke_dir":
                    st.markdown("### Merke dir:")
                    st.info(card["merke_dir"])
                    if st.button("Weiter", key="btn_weiter"):
                        st.session_state.card_mode = "question"
                        st.rerun()

                elif mode == "question":
                    correct_answer = card["optionen"][card["richtig"]]

                    if cur_streak < 4:
                        # Multiple Choice – Optionen einmalig mischen, Reihenfolge pro Karte merken
                        _shuf_key = f"shuf_{pid}_{card_idx}_{pos}"
                        if _shuf_key not in st.session_state:
                            _opts = list(card["optionen"])
                            _correct_val = _opts[card["richtig"]]
                            random.shuffle(_opts)
                            st.session_state[_shuf_key] = (_opts, _opts.index(_correct_val))
                        _opts, _correct_idx = st.session_state[_shuf_key]
                        st.markdown(f"### {card['frage']}")
                        st.write("")
                        for i, option in enumerate(_opts):
                            if st.button(option, key=f"opt_{pos}_{i}", use_container_width=True):
                                handle_answer(i == _correct_idx, card_idx, streak, mastered, xp, cur_streak, pid)

                    elif cur_streak < 6:
                        # Lückentext
                        st.markdown("### Füll die Lücke aus:")
                        st.markdown(make_lueckentext(card["merke_dir"], correct_answer))
                        with st.form(key=f"fill_form_{pos}"):
                            user_answer = st.text_input("Deine Antwort:", placeholder="Hier eingeben…")
                            submitted = st.form_submit_button("✔️ Prüfen", use_container_width=True)
                        if submitted:
                            handle_answer(check_answer(user_answer, correct_answer),
                                          card_idx, streak, mastered, xp, cur_streak, pid)

                    else:
                        # Free Recall
                        st.markdown(f"### {card['frage']}")
                        with st.form(key=f"recall_form_{pos}"):
                            user_answer = st.text_input("Deine Antwort:", placeholder="Hier eingeben…")
                            submitted = st.form_submit_button("✔️ Prüfen", use_container_width=True)
                        if submitted:
                            handle_answer(check_answer(user_answer, correct_answer),
                                          card_idx, streak, mastered, xp, cur_streak, pid)

                elif mode == "correct":
                    xp_gain = st.session_state.get("last_xp_gain", 0)
                    if xp_gain > 0:
                        st.success(f"Richtig! +{xp_gain} XP ✨")
                    else:
                        st.success("Richtig! Weiter so! ✨")
                    if st.button("Nächste Karte", key="btn_next"):
                        next_pos = pos + 1
                        if next_pos >= len(queue):
                            next_pos = 0
                        st.session_state.queue_pos = next_pos
                        ne = queue[next_pos]
                        if not ne["is_review"] and streak.get(ne["idx"], 0) == 0:
                            st.session_state.card_mode = "merke_dir"
                            st.session_state.new_card_pos = new_card_pos + 1
                        else:
                            st.session_state.card_mode = "question"
                        st.rerun()

                elif mode == "wrong":
                    st.error("Nicht ganz – versuch es nochmal!")
                    if st.button("Nochmal", key="btn_retry"):
                        st.session_state.card_mode = "question"
                        st.rerun()

# =============================================================================
# SEITE: RANKING
# =============================================================================
elif active_page == "ranking":

    RANKS = [
        (12000, "Großmeister", "👑"),
        (9000,  "Meister",     "🏅"),
        (6000,  "Diamant",     "💎"),
        (3000,  "Gold",        "🥇"),
        (1000,  "Silber",      "🥈"),
        (0,     "Bronze",      "🥉"),
    ]

    def get_rank(xp):
        for threshold, name, emoji in RANKS:
            if xp >= threshold:
                return name, emoji
        return "Bronze", "🥉"

    def get_next_rank_info(xp):
        for i, (threshold, name, emoji) in enumerate(RANKS):
            if xp >= threshold:
                if i == 0:
                    return "gm"
                return RANKS[i - 1]
        return RANKS[-2]

    session_xp = st.session_state.get("xp", 0)
    user_name = st.session_state.get("sett_name") or db_get_username(_user_id) or "Du"
    st.session_state["sett_name"] = user_name

    _all_raw = db_get_all_users()
    for _u in _all_raw:
        if _u["user_id"] == _user_id:
            _u["xp"] = session_xp
            _u["username"] = user_name
    all_users = sorted(_all_raw, key=lambda u: u["xp"], reverse=True)

    user_pos = next((i + 1 for i, u in enumerate(all_users) if u["user_id"] == _user_id), None)
    is_number_one = user_pos == 1

    rank_name, rank_emoji = get_rank(session_xp)
    next_info = get_next_rank_info(session_xp)
    h1, h2 = st.columns([3, 2])
    with h1:
        st.markdown('<div class="headline">Globale Bestenliste</div>', unsafe_allow_html=True)
    with h2:
        if is_number_one:
            st.markdown(
                "<div style='border:2px solid #ffd200;border-radius:12px;padding:0.3em 0.8em;"
                "background:linear-gradient(135deg,rgba(255,210,0,0.18),rgba(255,150,0,0.08));"
                "text-align:center;font-weight:700;font-size:0.95em;margin-top:0.35em;"
                "box-shadow:0 0 12px rgba(255,210,0,0.25);'>🌟 #1 Global</div>",
                unsafe_allow_html=True)
        elif next_info == "gm":
            st.markdown(
                f"<div style='text-align:right;font-size:0.9em;margin-top:0.5em;color:#aaa;'>"
                f"Ziel: {rank_emoji} {rank_name} → <b style='color:#ffd200;'>👑 #1 GM</b></div>",
                unsafe_allow_html=True)
        else:
            next_t, next_n, next_e = next_info
            xp_needed = next_t - session_xp
            st.markdown(
                f"<div style='text-align:right;font-size:0.9em;margin-top:0.5em;color:#aaa;'>"
                f"Ziel: {rank_emoji} {rank_name} → <b style='color:#fff;'>{next_e} {next_n}</b>"
                f"<br><span style='font-size:0.85em;color:#888;'>noch {xp_needed:,} XP</span></div>",
                unsafe_allow_html=True)

    MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
    others = [(pos + 1, u["username"], u["xp"]) for pos, u in enumerate(all_users) if u["user_id"] != _user_id]
    display_others = others[:5]
    show_sep = user_pos is not None and user_pos > len(display_others) + 1

    for real_pos, uname, xp in display_others:
        rank_n, rank_e = get_rank(xp)
        with st.container(border=True):
            c1, c2, c3 = st.columns([1, 6, 3])
            with c1:
                if real_pos in MEDALS:
                    st.markdown(f"### {MEDALS[real_pos]}")
                else:
                    st.markdown(f"**#{real_pos}**")
            with c2:
                if real_pos == 1:
                    st.markdown(f"<span style='color:#ffd200;font-weight:700;font-size:1.1em;'>🏆 {uname}</span>",
                                unsafe_allow_html=True)
                else:
                    st.markdown(uname)
            with c3:
                if rank_n == "Großmeister":
                    st.markdown(f"<span class='rank-gm'>{rank_e} {rank_n}</span> "
                                f"<span style='color:#ffd200;font-weight:700;'>{xp:,} XP</span>",
                                unsafe_allow_html=True)
                else:
                    st.markdown(f"{rank_e} {rank_n} · **{xp:,} XP**")

    if show_sep:
        st.markdown('<div style="text-align:center;font-size:1.5em;color:#888;">…</div>', unsafe_allow_html=True)

    if user_pos is not None:
        rank_n, rank_e = get_rank(session_xp)
        with st.container(border=True):
            c1, c2, c3 = st.columns([1, 6, 3])
            with c1:
                if user_pos in MEDALS:
                    st.markdown(f"### {MEDALS[user_pos]}")
                else:
                    st.markdown(f"**#{user_pos}**")
            with c2:
                if is_number_one:
                    st.markdown(f"<span style='color:#ffd200;font-weight:700;'>🌟 {user_name} (du)</span>",
                                unsafe_allow_html=True)
                else:
                    st.markdown(f"<span style='color:#ffd200;font-weight:600;'>⭐ {user_name} (du)</span>",
                                unsafe_allow_html=True)
            with c3:
                st.markdown(f"{rank_e} {rank_n} · **{session_xp:,} XP**")

# =============================================================================
# SEITE: EINSTELLUNGEN
# =============================================================================
elif active_page == "settings":

    st.markdown('<div class="headline">Einstellungen</div>', unsafe_allow_html=True)

    # --- Feedback Box ---
    st.markdown("##### 💬 Feedback")
    with st.form("feedback_form"):
        feedback_text = st.text_area("Was sollte ich an StudyFyn verbessern?", placeholder="Dein Feedback hilft mir sehr!", key="feedback_text")
        submitted = st.form_submit_button("Absenden")
        if submitted and feedback_text.strip():
            st.success("Danke für dein Feedback! 🙏")
            # Hier könntest du das Feedback speichern oder per Mail senden

    st.markdown("##### 👤 Profil")
    with st.container(border=True):
        col1, col2 = st.columns(2)
        with col1:
            new_name = st.text_input("Name", value=st.session_state.get("sett_name", "Du"), key="sett_name_input")
        with col2:
            st.text_input("E-Mail", value="oli@studyfyn.de", key="sett_email", disabled=True)
        if st.button("💾 Name speichern", key="save_name_btn"):
            name_to_save = new_name.strip() or "Du"
            st.session_state["sett_name"] = name_to_save
            st.session_state["loaded_user_id"] = None
            db_set_username(name_to_save, _user_id)
            st.session_state["show_name_saved"] = True
            st.rerun()
        if st.session_state.get("show_name_saved"):
            st.success(f"Name gespeichert: {st.session_state.get('sett_name','Du')}")
            # Erfolgsmeldung bleibt bis Seite gewechselt wird
        if active_page != "settings" and st.session_state.get("show_name_saved"):
            st.session_state.pop("show_name_saved")

    st.markdown("##### 📚 Lerneinstellungen")
    with st.container(border=True):
        st.selectbox("Sprache der Karteikarten", ["Deutsch", "English", "Español"], index=0, key="sett_lang")
        st.slider("Tägliches Lernziel (Karten)", 5, 50, 15, key="sett_daily_goal")
        st.toggle("Erinnerungen aktivieren", value=True, key="sett_reminders")
        st.selectbox("Erinnerungszeit", ["08:00", "10:00", "12:00", "14:00", "18:00", "20:00"],
                     index=4, key="sett_reminder_time")

    st.markdown("##### 🎨 Darstellung")
    with st.container(border=True):
        st.toggle("Dark Mode", value=True, key="sett_dark", disabled=True)
        st.selectbox("Schriftgröße", ["Klein", "Normal", "Groß"], index=1, key="sett_font")

    st.markdown("##### ⭐ Premium")
    st.markdown("""
    <div class="premium-box">
        <div style="font-size:1.35em; font-weight:700; color:#ffd200; margin-bottom:0.6em;">
            ✨ StudyFyn Premium
        </div>
        <div style="font-size:1.02em; color:#ddd; margin-bottom:0.8em;">
            Hol dir das volle Lernerlebnis – ohne Limits, ohne Werbung.
        </div>
        <div style="display:flex; flex-direction:column; gap:0.5em;">
            <div>🚀 <b>Unbegrenzte Pakete</b> erstellen</div>
            <div>🏆 <b>Master Pack</b> (17–20 Karten pro Paket)</div>
            <div>📊 <b>Erweiterte Statistiken</b> & Lernverlauf</div>
            <div>🎨 <b>Custom Ordner-Farben</b> & Themes</div>
            <div>⚡ <b>Prioritäts-KI</b> – schnellere Kartenerstellung</div>
            <div>🔕 <b>Keine Werbung</b> – für immer</div>
            <div>💬 <b>Prioritäts-Support</b></div>
            <div>🌟 <b>Exklusive Themen</b> & Vorlagen</div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.button("💎 Premium freischalten", use_container_width=True, type="primary", key="btn_premium")

    st.markdown("##### ℹ️ Über StudyFyn")
    with st.container(border=True):
        st.markdown("**StudyFyn** · Version 1.0 MVP")
        st.caption("Dein persönlicher KI-Lernhelfer. Buchseiten digitalisieren, Karteikarten erstellen, intelligent lernen.")
        st.caption("© 2026 StudyFyn")

    # --- Geräte-ID ganz nach hinten ---
    with st.container(border=True):
        st.markdown("##### 📱 Geräte-ID")
        st.code(_user_id, language=None)
        st.caption("Diese ID identifiziert dein Gerät eindeutig. Teile sie nicht mit anderen.")
