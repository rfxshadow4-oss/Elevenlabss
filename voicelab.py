"""
NexVoice - God Level AI Voice Platform
=======================================
Single file — host on PythonAnywhere / Hostinger

Routes:
  /           → login
  /dashboard  → main dashboard
  /clone      → clone voice
  /voices     → my voices
  /library    → voice library (Language/Gender/Age/Category filters)
  /tts        → text to speech + download
  /history    → TTS history
  /logout
  /admin      → admin dashboard
  /admin/login
  /admin/users
  /admin/users/<u>
  /admin/create
  /admin/voices
  /admin/log

WSGI (PythonAnywhere):
  import sys
  sys.path.insert(0, '/home/MOAZBOTS/mysite')
  from voicelab import application
"""

from flask import Flask, request, jsonify, session, redirect, url_for, Response
from flask_cors import CORS
from functools import wraps
import json, time, uuid, os, requests as rq, boto3, hashlib
from botocore.client import Config
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "nexvoice-2026-change-me")
CORS(app)

# ═══════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════
AKOOL_API_KEY    = os.environ.get("AKOOL_API_KEY",    "uFINUrCcJ14bR4oKYd0p8BfDq267KTTX")
AKOOL_BASE       = "https://openapi.akool.com/api/open/v4"
CLONE_MODEL      = "Akool Multilingual 1"
R2_ACCOUNT_ID    = os.environ.get("R2_ACCOUNT_ID",    "cd74599dc208874e2c7710d4a8415b30")
R2_BUCKET        = os.environ.get("R2_BUCKET",        "vgfg")
R2_ACCESS_KEY    = os.environ.get("R2_ACCESS_KEY",    "af9e671d1d10ac99c2241d80935c4698")
R2_SECRET_KEY    = os.environ.get("R2_SECRET_KEY",    "884282d257e6f3f824542a47853b458b0ccb7df501ab9caa2eab50e1209a51ec")
R2_PUBLIC_DOMAIN = os.environ.get("R2_PUBLIC_DOMAIN", "https://pub-68fe697698b848b18e85765e592e2d0c.r2.dev")
R2_ENDPOINT      = "https://" + R2_ACCOUNT_ID + ".r2.cloudflarestorage.com"
ADMIN_USER       = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS       = os.environ.get("ADMIN_PASS", "Admin@999")
MAX_CLONES       = 10
MAX_HISTORY      = 5
K_USERS          = "db/users.json"
K_LOG            = "db/log.json"
K_SHARED         = "db/shared_voices.json"  # admin-cloned voices available to all users

# PythonAnywhere network — detect environment
import socket as _socket
try:
    _hn = _socket.gethostname()
    ON_PA = "pythonanywhere" in _hn.lower() or os.path.exists("/var/www")
except:
    ON_PA = False

# PythonAnywhere ONLY allows outbound to whitelisted domains on free plan.
# openapi.akool.com must be whitelisted via:
#   Account > Network > Add openapi.akool.com
# The proxy below is for paid plan or after whitelisting:
PA_PROXIES = None  # set automatically below
if ON_PA:
    # Use environment proxy if set, else try standard PA proxy
    _ep = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
    if _ep:
        PA_PROXIES = {"http": _ep, "https": _ep}
    else:
        PA_PROXIES = {"http": "http://proxy.server:3128", "https": "http://proxy.server:3128"}

# ═══════════════════════════════════════════════════════
#  R2
# ═══════════════════════════════════════════════════════
s3 = boto3.client("s3", endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY, aws_secret_access_key=R2_SECRET_KEY,
    config=Config(signature_version="s3v4"), region_name="auto")

def r2_read(key, default=None):
    try: return json.loads(s3.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read())
    except: return default if default is not None else {}

def r2_write(key, data):
    try:
        s3.put_object(Bucket=R2_BUCKET, Key=key,
            Body=json.dumps(data, indent=2, ensure_ascii=False).encode(),
            ContentType="application/json")
    except Exception as e: print("[R2]", e)

def r2_upload(key, data, ct="audio/mpeg"):
    s3.put_object(Bucket=R2_BUCKET, Key=key, Body=data, ContentType=ct)
    return R2_PUBLIC_DOMAIN + "/" + key

def r2_get_bytes(key):
    return s3.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read()

def ukey(u, *p): return "users/" + u + "/" + "/".join(p)

def load_users():      return r2_read(K_USERS, {})
def save_users(u):     r2_write(K_USERS, u)
def load_voices(u):    return r2_read(ukey(u,"voices.json"), [])
def save_voices(u, v): r2_write(ukey(u,"voices.json"), v)
def load_hist(u):      return r2_read(ukey(u,"history.json"), [])

def save_hist(u, h):
    # auto-delete oldest beyond MAX_HISTORY
    h = h[:MAX_HISTORY]
    r2_write(ukey(u,"history.json"), h)

def get_user(un):      return load_users().get(un)
def save_user(un, d):  u=load_users(); u[un]=d; save_users(u)

def add_hist(un, tid, url, text, vname):
    h = load_hist(un)
    h.insert(0, {"task_id":tid,"audio_url":url,"text":text[:120],"voice_name":vname,"ts":now()})
    save_hist(un, h)

def write_log(un, action, detail="", cost=0):
    lg = r2_read(K_LOG, [])
    lg.insert(0, {"user":un,"action":action,"detail":detail,"cost":cost,"time":now()})
    r2_write(K_LOG, lg[:1000])

def load_shared_voices():   return r2_read(K_SHARED, [])
def save_shared_voices(v):  r2_write(K_SHARED, v)

def hp(pw):       return hashlib.sha256(pw.encode()).hexdigest()
def now():        return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
def J(d,s=200):   return jsonify(d), s
def ERR(m,c=400): return jsonify({"error":m}), c
def get_un():     return session.get("username","")

def is_banned(u):  return u.get("banned", False)
def is_expired(u):
    e = u.get("expires_at")
    if not e: return False
    try:   return datetime.utcnow() > datetime.strptime(e, "%Y-%m-%d")
    except: return False

def user_status(u):
    if is_banned(u):  return "banned"
    if is_expired(u): return "expired"
    return "active"

def check_access(user):
    if not user: return "User not found"
    s = user_status(user)
    if s=="banned":  return "Account banned: " + user.get("ban_reason","No reason given")
    if s=="expired": return "Account expired on " + user.get("expires_at","") + ". Contact admin."
    return None

# Build a session once with correct proxy settings
_ai_session = None
def _get_session():
    global _ai_session
    if _ai_session is None:
        _ai_session = rq.Session()
        if PA_PROXIES:
            _ai_session.proxies.update(PA_PROXIES)
    return _ai_session

def akool(method, path, body=None):
    h  = {"x-api-key": AKOOL_API_KEY, "Content-Type": "application/json"}
    sess = _get_session()
    kw = {"headers":h, "timeout":60}
    if body: kw["json"] = body
    try:
        fn = sess.post if method=="POST" else sess.get
        r  = fn(AKOOL_BASE+path, **kw)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("[AI Engine]", str(e))
        raise

# ── Voice tags ─────────────────────────────────────────
VKW = {
    "aria":["Female","Storytelling","Warm","Narration"],
    "sarah":["Female","Conversational","Young","Friendly"],
    "laura":["Female","Storytelling","Expressive","Calm"],
    "charlie":["Male","Conversational","Young","Casual"],
    "george":["Male","Deep","Narration","Professional"],
    "callum":["Male","Intense","Characters","Dramatic"],
    "river":["Neutral","Calm","Meditation","Relaxing"],
    "liam":["Male","Narration","Documentary","Clear"],
    "charlotte":["Female","Warm","Young"],
    "alice":["Female","British","Confident","News"],
    "matilda":["Female","Warm","Friendly","American"],
    "will":["Male","Young","Casual","Friendly"],
    "jessica":["Female","Expressive","Young","Passionate"],
    "eric":["Male","Friendly","Positive"],
    "chris":["Male","Young","Casual","Relatable"],
    "brian":["Male","Deep","Narration","American"],
    "daniel":["Male","British","News","Professional"],
    "lily":["Female","Warm","British","Young"],
    "bill":["Male","Documentary","Trustworthy"],
    "rachel":["Female","Calm","Narration","Clear"],
    "adam":["Male","Deep","Narration","Strong"],
    "emma":["Female","Soft","Meditation","Soothing"],
    "sophie":["Female","Young","Bright","Conversational"],
    "james":["Male","Calm","Serious","Documentary"],
    "kate":["Female","Professional","News","Clear"],
    "mike":["Male","Casual","Friendly","American"],
    "nina":["Female","Young","Energetic","Animated"],
    "oliver":["Male","British","Storytelling","Warm"],
    "rose":["Female","Elegant","Dramatic"],
    "sky":["Female","Young","Dreamy","Soft"],
    "tom":["Male","Deep","Realistic","Narration"],
    "grace":["Female","Gentle","Audiobook","Storytelling"],
    "zara":["Female","Dynamic","Young","Expressive"],
    "samuel":["Male","Professional","Deep","Clear"],
    "mei":["Female","Chinese","Young","Clear"],
    "yuki":["Female","Japanese","Soft","Young"],
    "hana":["Female","Korean","Young","Clear"],
    "carlos":["Male","Spanish","Warm","Conversational"],
    "sofia":["Female","Spanish","Warm","Young"],
    "pierre":["Male","French","Sophisticated","Calm"],
    "marie":["Female","French","Elegant","Warm"],
    "hans":["Male","German","Professional","Clear"],
    "anna":["Female","German","Professional","Young"],
    "max":["Male","Young","Energetic","Casual"],
    "alex":["Neutral","Young","Conversational","Clear"],
}
GTAG = {"male":["Male"],"female":["Female"]}
LTAG = {"en":["English"],"zh":["Chinese"],"es":["Spanish"],"fr":["French"],
        "de":["German"],"ja":["Japanese"],"ko":["Korean"],"pt":["Portuguese"],
        "ar":["Arabic"],"hi":["Hindi"],"it":["Italian"],"ru":["Russian"]}

def get_tags(v):
    tags = set()
    name = (v.get("name") or "").lower()
    g    = (v.get("gender") or "").lower()
    lc   = (v.get("locale") or "").lower()[:2]
    for frag, kw in VKW.items():
        if frag in name: tags.update(kw); break
    tags.update(GTAG.get(g,[]))
    tags.update(LTAG.get(lc,[]))
    if not tags:
        tags.add("Male" if g=="male" else "Female" if g=="female" else "Voice")
    return sorted(tags)

# ── Auth ───────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def w(*a,**kw):
        if not session.get("username"):
            return redirect("/login?next="+request.path)
        return f(*a,**kw)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a,**kw):
        if not session.get("is_admin"):
            return redirect("/admin/login")
        return f(*a,**kw)
    return w

def gi(g):
    g=(g or "").lower()
    return "F" if g=="female" else "M" if g=="male" else "N"

# ═══════════════════════════════════════════════════════
#  SHARED STYLES + JS
# ═══════════════════════════════════════════════════════
HEAD = """
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#020408;--bg2:#060b12;--bg3:#0a1020;
  --card:#0d1526;--card2:#111c30;--card3:#152036;
  --b1:rgba(99,179,237,.12);--b2:rgba(99,179,237,.06);
  --cyan:#00d4ff;--cyan2:#0099cc;--cyan3:#00ffcc;
  --pu:#7c3aed;--pu2:#a855f7;
  --gr:#00ff88;--gr2:#10b981;
  --ye:#ffd60a;--or:#ff6b35;--re:#ff3366;
  --tx:#e8f4fd;--tx2:#7aa8c7;--tx3:#3d6b8a;
  --sw:260px;
  --glow-c:rgba(0,212,255,.4);
  --glow-p:rgba(124,58,237,.4);
  --glow-g:rgba(0,255,136,.4);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
*{scrollbar-width:thin;scrollbar-color:var(--b1) transparent}
*::-webkit-scrollbar{width:4px}
*::-webkit-scrollbar-thumb{background:var(--b1);border-radius:2px}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--tx);font-family:'Space Grotesk',sans-serif;
     min-height:100vh;display:flex;overflow-x:hidden}

/* ── ANIMATED BG ── */
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:
    radial-gradient(ellipse 80% 60% at 0% 0%,rgba(0,212,255,.04),transparent 60%),
    radial-gradient(ellipse 60% 50% at 100% 100%,rgba(124,58,237,.05),transparent 60%),
    radial-gradient(ellipse 40% 40% at 50% 50%,rgba(0,255,136,.02),transparent 70%)}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%2300d4ff' fill-opacity='0.015'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E") repeat}

/* ── SIDEBAR ── */
.sb{width:var(--sw);min-height:100vh;background:rgba(6,11,18,.95);
    border-right:1px solid var(--b1);display:flex;flex-direction:column;
    position:fixed;top:0;left:0;bottom:0;z-index:100;overflow-y:auto;
    backdrop-filter:blur(20px)}
.sb::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
            background:linear-gradient(90deg,transparent,var(--cyan),transparent)}
.sb-logo{padding:24px 20px 20px;border-bottom:1px solid var(--b1);
         display:flex;align-items:center;gap:12px;flex-shrink:0;cursor:pointer;
         text-decoration:none}
.logo-ic{position:relative;width:40px;height:40px;flex-shrink:0}
.logo-ic svg{width:40px;height:40px}
.logo-ring{animation:spin 8s linear infinite}
.logo-dot{animation:pulse-logo 2s ease-in-out infinite}
.sb-name{display:flex;flex-direction:column}
.sb-name-t{font-size:18px;font-weight:700;letter-spacing:-.3px;
           background:linear-gradient(135deg,var(--cyan),var(--pu2));
           -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sb-name-s{font-size:10px;color:var(--tx3);letter-spacing:.15em;text-transform:uppercase;margin-top:1px}
.sb-nav{flex:1;padding:16px 12px}
.sb-sec{font-size:9px;font-weight:600;letter-spacing:.15em;text-transform:uppercase;
        color:var(--tx3);padding:10px 10px 6px;margin-top:8px}
.si{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;
    font-size:13px;font-weight:500;color:var(--tx2);text-decoration:none;
    transition:all .2s;margin-bottom:2px;position:relative;overflow:hidden}
.si::before{content:'';position:absolute;inset:0;opacity:0;
            background:linear-gradient(90deg,rgba(0,212,255,.08),transparent);
            transition:opacity .2s}
.si:hover::before,.si.on::before{opacity:1}
.si:hover{color:var(--tx);transform:translateX(2px)}
.si.on{color:var(--cyan);background:rgba(0,212,255,.06);
       border:1px solid rgba(0,212,255,.15)}
.si.on::after{content:'';position:absolute;left:0;top:50%;transform:translateY(-50%);
              width:3px;height:60%;background:var(--cyan);border-radius:0 2px 2px 0;
              box-shadow:0 0 8px var(--cyan)}
.si-ic{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;
       justify-content:center;font-size:15px;background:var(--b2);flex-shrink:0;
       transition:all .2s}
.si.on .si-ic{background:rgba(0,212,255,.1);box-shadow:0 0 12px rgba(0,212,255,.2)}
.sb-bot{margin-top:auto}
.sb-cred{padding:14px 16px;border-top:1px solid var(--b1)}
.cbox{background:linear-gradient(135deg,rgba(0,212,255,.06),rgba(124,58,237,.04));
      border:1px solid rgba(0,212,255,.15);border-radius:12px;padding:14px 16px;position:relative;overflow:hidden}
.cbox::before{content:'';position:absolute;top:-20px;right:-20px;width:80px;height:80px;
              background:radial-gradient(circle,rgba(0,212,255,.15),transparent 70%)}
.cbox-l{font-size:9px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
        color:var(--tx3);margin-bottom:6px}
.cbox-n{font-family:'Space Mono',monospace;font-size:26px;font-weight:700;
        color:var(--cyan);line-height:1;text-shadow:0 0 20px rgba(0,212,255,.4)}
.cbox-s{font-size:10px;color:var(--tx3);margin-top:4px}
.exp-pill{margin-top:8px;padding:5px 10px;background:rgba(255,214,10,.06);
          border:1px solid rgba(255,214,10,.2);border-radius:6px;font-size:10px;color:var(--ye)}
.sb-user{padding:12px 16px;border-top:1px solid var(--b1);display:flex;align-items:center;gap:10px}
.u-av{width:34px;height:34px;border-radius:10px;flex-shrink:0;
      background:linear-gradient(135deg,var(--cyan2),var(--pu));
      display:flex;align-items:center;justify-content:center;
      font-family:'Space Mono',monospace;font-size:13px;font-weight:700;color:#fff;
      box-shadow:0 0 12px rgba(0,212,255,.3)}
.u-inf{flex:1;min-width:0}
.u-nm{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.u-r2{font-size:9px;color:var(--tx3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.u-out{color:var(--tx3);text-decoration:none;font-size:16px;padding:4px;
       transition:color .2s;flex-shrink:0;border-radius:6px}
.u-out:hover{color:var(--re);background:rgba(255,51,102,.1)}

/* ── MAIN ── */
.main{margin-left:var(--sw);flex:1;display:flex;flex-direction:column;
      position:relative;z-index:1;min-width:0}
.topbar{height:62px;background:rgba(2,4,8,.8);backdrop-filter:blur(20px);
        border-bottom:1px solid var(--b1);display:flex;align-items:center;
        padding:0 28px;position:sticky;top:0;z-index:50;gap:14px;flex-shrink:0}
.topbar::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;
               background:linear-gradient(90deg,transparent,var(--b1),transparent)}
.tb-t{font-size:16px;font-weight:600;flex:1;letter-spacing:-.2px}
.tb-pill{font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;
         color:var(--cyan);background:rgba(0,212,255,.08);border:1px solid rgba(0,212,255,.2);
         border-radius:20px;padding:5px 14px;display:flex;align-items:center;gap:6px;
         flex-shrink:0}
.tb-pill::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--cyan);
                 box-shadow:0 0 8px var(--cyan);animation:pulse-dot 2s ease-in-out infinite}
@keyframes pulse-dot{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.6;transform:scale(1.3)}}
.content{flex:1;padding:30px;overflow-y:auto}

/* ── PAGE HEADER ── */
.ph{display:flex;align-items:flex-start;justify-content:space-between;
    margin-bottom:28px;flex-wrap:wrap;gap:14px}
.ph-t{font-size:24px;font-weight:700;letter-spacing:-.5px;
      background:linear-gradient(135deg,var(--tx) 40%,var(--tx2));
      -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.ph-s{color:var(--tx3);font-size:13px;margin-top:5px}

/* ── CARD ── */
.card{background:var(--card);border:1px solid var(--b1);border-radius:16px;
      padding:24px;margin-bottom:20px;position:relative;overflow:hidden;
      transition:border-color .2s}
.card:hover{border-color:rgba(0,212,255,.2)}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
              background:linear-gradient(90deg,transparent,rgba(0,212,255,.1),transparent)}
.card-t{font-size:14px;font-weight:600;margin-bottom:18px;display:flex;align-items:center;gap:8px;
        color:var(--tx)}

/* ── BUTTONS ── */
.btn{cursor:pointer;border:none;border-radius:10px;font-family:'Space Grotesk',sans-serif;
     font-weight:600;transition:all .2s;display:inline-flex;align-items:center;
     justify-content:center;gap:7px;text-decoration:none;line-height:1;
     white-space:nowrap;position:relative;overflow:hidden}
.btn::before{content:'';position:absolute;inset:0;opacity:0;
             background:linear-gradient(135deg,rgba(255,255,255,.1),transparent);
             transition:opacity .2s}
.btn:hover::before{opacity:1}
.bsm{padding:8px 16px;font-size:12px}
.bmd{padding:11px 22px;font-size:13px}
.blg{padding:14px 28px;font-size:14px}
.bfl{width:100%}
.bcyan{background:linear-gradient(135deg,var(--cyan2),var(--cyan));color:#000;
       font-weight:700;box-shadow:0 4px 20px rgba(0,212,255,.3)}
.bcyan:hover{transform:translateY(-1px);box-shadow:0 6px 28px rgba(0,212,255,.5)}
.bpu{background:linear-gradient(135deg,var(--pu),var(--pu2));color:#fff;
     box-shadow:0 4px 20px rgba(124,58,237,.3)}
.bpu:hover{transform:translateY(-1px);box-shadow:0 6px 28px rgba(124,58,237,.5)}
.bgr{background:linear-gradient(135deg,var(--gr2),var(--gr));color:#000;font-weight:700;
     box-shadow:0 4px 20px rgba(0,255,136,.25)}
.bgr:hover{transform:translateY(-1px);box-shadow:0 6px 28px rgba(0,255,136,.4)}
.bgh{background:rgba(255,255,255,.04);color:var(--tx2);border:1px solid var(--b1)}
.bgh:hover{background:rgba(255,255,255,.08);color:var(--tx);border-color:rgba(0,212,255,.2)}
.bdr{background:rgba(255,51,102,.08);color:var(--re);border:1px solid rgba(255,51,102,.2)}
.bdr:hover{background:rgba(255,51,102,.15)}
.bye{background:rgba(255,214,10,.08);color:var(--ye);border:1px solid rgba(255,214,10,.2)}
.bor{background:rgba(255,107,53,.08);color:var(--or);border:1px solid rgba(255,107,53,.2)}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none!important;box-shadow:none!important}

/* ── FORM ── */
.fd{margin-bottom:18px}
.flbl{display:block;font-size:10px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
      color:var(--tx3);margin-bottom:8px}
.finp{width:100%;background:rgba(255,255,255,.03);border:1px solid var(--b1);border-radius:10px;
      padding:12px 16px;color:var(--tx);font-family:'Space Grotesk',sans-serif;font-size:13px;
      outline:none;transition:all .2s}
.finp:focus{border-color:rgba(0,212,255,.4);background:rgba(0,212,255,.03);
            box-shadow:0 0 0 3px rgba(0,212,255,.08)}
.finp::placeholder{color:var(--tx3)}
textarea.finp{resize:vertical;min-height:130px;line-height:1.7}
select.finp option{background:var(--card)}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:4px;
                  background:var(--b1);border-radius:2px;outline:none;cursor:pointer;border:none;padding:0}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;
  border-radius:50%;background:linear-gradient(135deg,var(--cyan2),var(--cyan));cursor:pointer;
  box-shadow:0 0 8px rgba(0,212,255,.5)}

/* ── UPLOAD ZONE ── */
.uzone{border:2px dashed rgba(0,212,255,.15);border-radius:16px;padding:48px 24px;
       text-align:center;cursor:pointer;transition:all .3s;
       background:radial-gradient(ellipse at center,rgba(0,212,255,.03),transparent 70%);
       position:relative;overflow:hidden}
.uzone:hover,.uzone.drag{border-color:rgba(0,212,255,.5);
  background:radial-gradient(ellipse at center,rgba(0,212,255,.07),transparent 70%);
  box-shadow:inset 0 0 60px rgba(0,212,255,.05),0 0 30px rgba(0,212,255,.08)}
.uzone input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.uz-ic{font-size:56px;margin-bottom:16px;display:block;filter:drop-shadow(0 0 20px rgba(0,212,255,.4));
       animation:float 3s ease-in-out infinite}
@keyframes float{0%,100%{transform:translateY(0) rotate(0deg)}50%{transform:translateY(-10px) rotate(5deg)}}
.uz-t{font-size:18px;font-weight:700;margin-bottom:8px;
      background:linear-gradient(135deg,var(--tx),var(--tx2));
      -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.uz-s{font-size:13px;color:var(--tx3);margin-bottom:5px}
.uz-h{font-size:11px;color:var(--tx3);letter-spacing:.05em}
.uz-cta{display:inline-flex;align-items:center;gap:8px;margin-top:18px;
        padding:12px 28px;background:linear-gradient(135deg,var(--cyan2),var(--cyan));
        border-radius:10px;color:#000;font-size:13px;font-weight:700;pointer-events:none;
        box-shadow:0 4px 20px rgba(0,212,255,.3)}
.fchip{background:rgba(0,255,136,.06);border:1px solid rgba(0,255,136,.2);border-radius:10px;
       padding:11px 16px;font-size:12px;color:var(--gr);display:none;align-items:center;gap:10px;margin-top:14px}
.fchip.show{display:flex;animation:fadeIn .3s ease}
.fchip-nm{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ── CLONE OVERLAY ── */
.clov{position:fixed;inset:0;z-index:900;display:none;
      align-items:center;justify-content:center;flex-direction:column;
      background:rgba(2,4,8,.96);backdrop-filter:blur(20px)}
.clov.show{display:flex;animation:fadeIn .4s ease}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.clov-bg{position:absolute;inset:0;overflow:hidden;pointer-events:none}
.clov-bg::before{content:'';position:absolute;top:50%;left:50%;width:600px;height:600px;
  transform:translate(-50%,-50%);
  background:radial-gradient(circle,rgba(0,212,255,.06) 0%,transparent 70%);
  animation:expand 3s ease-in-out infinite}
@keyframes expand{0%,100%{transform:translate(-50%,-50%) scale(1)}50%{transform:translate(-50%,-50%) scale(1.2)}}
.ring-wrap{position:relative;width:180px;height:180px;margin-bottom:32px;z-index:1}
.ring-svg{transform:rotate(-90deg);filter:drop-shadow(0 0 20px rgba(0,212,255,.5))}
.r-bg{fill:none;stroke:rgba(0,212,255,.1);stroke-width:8}
.r-arc{fill:none;stroke:url(#rg);stroke-width:8;stroke-linecap:round;
       stroke-dasharray:471;stroke-dashoffset:471;transition:stroke-dashoffset .6s cubic-bezier(.4,0,.2,1)}
.ring-inside{position:absolute;inset:0;display:flex;flex-direction:column;
             align-items:center;justify-content:center}
.ring-n{font-family:'Space Mono',monospace;font-size:42px;font-weight:700;
        color:var(--cyan);line-height:1;text-shadow:0 0 30px rgba(0,212,255,.6)}
.ring-u{font-size:11px;color:var(--tx3);margin-top:2px;letter-spacing:.1em}
.ov-t{font-size:22px;font-weight:700;color:var(--tx);margin-bottom:8px;
      text-align:center;letter-spacing:-.3px;z-index:1}
.ov-m{font-size:13px;color:var(--tx3);text-align:center;margin-bottom:28px;
      max-width:300px;line-height:1.6;z-index:1}
.ov-steps{display:flex;flex-direction:column;gap:10px;width:320px;z-index:1}
.ovs{display:flex;align-items:center;gap:12px;padding:12px 16px;
     background:rgba(255,255,255,.02);border:1px solid var(--b1);
     border-radius:12px;font-size:13px;color:var(--tx3);transition:all .4s}
.ovs.act{border-color:rgba(0,212,255,.3);color:var(--cyan);background:rgba(0,212,255,.06);
         box-shadow:0 0 20px rgba(0,212,255,.08)}
.ovs.done{border-color:rgba(0,255,136,.2);color:var(--gr);background:rgba(0,255,136,.04)}
.ovs .sd{width:8px;height:8px;border-radius:50%;background:currentColor;flex-shrink:0}
.ovs.act .sd{animation:blink-dot 1s ease-in-out infinite;box-shadow:0 0 8px currentColor}
@keyframes blink-dot{0%,100%{transform:scale(1)}50%{transform:scale(1.8)}}

/* ── LIBRARY FILTERS ── */
.lib-wrap{display:grid;grid-template-columns:220px 1fr;gap:22px;align-items:start}
@media(max-width:860px){.lib-wrap{grid-template-columns:1fr}}
.fpanel{background:var(--card);border:1px solid var(--b1);border-radius:16px;
        padding:20px;position:sticky;top:80px}
.fp-t{font-size:10px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
      color:var(--tx3);margin-bottom:14px}
.fcats{display:flex;flex-direction:column;gap:3px;max-height:520px;overflow-y:auto}
.fcat{display:flex;align-items:center;justify-content:space-between;padding:9px 11px;
      border-radius:9px;font-size:13px;font-weight:500;color:var(--tx3);
      cursor:pointer;border:none;background:none;width:100%;text-align:left;transition:all .2s}
.fcat:hover{background:rgba(0,212,255,.06);color:var(--tx)}
.fcat.on{background:rgba(0,212,255,.1);color:var(--cyan);border-left:2px solid var(--cyan)}
.fcat-n{font-size:9px;color:var(--tx3);background:var(--b1);border-radius:4px;padding:1px 6px;
        font-family:'Space Mono',monospace}
.lib-s{width:100%;background:rgba(255,255,255,.03);border:1px solid var(--b1);border-radius:9px;
       padding:10px 13px;color:var(--tx);font-size:12px;outline:none;margin-bottom:14px;
       font-family:'Space Grotesk',sans-serif;transition:all .2s}
.lib-s:focus{border-color:rgba(0,212,255,.4)}
.lib-s::placeholder{color:var(--tx3)}

/* ── VOICE GRID ── */
.vgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
.vc{background:var(--card);border:1px solid var(--b1);border-radius:14px;padding:18px;
    transition:all .3s;position:relative;overflow:hidden;cursor:default}
.vc::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
            background:linear-gradient(90deg,transparent,rgba(0,212,255,.1),transparent);
            opacity:0;transition:opacity .3s}
.vc:hover{border-color:rgba(0,212,255,.25);transform:translateY(-2px);
          box-shadow:0 8px 32px rgba(0,212,255,.08)}
.vc:hover::before{opacity:1}
.vc-avatar{width:48px;height:48px;border-radius:12px;margin-bottom:12px;
           display:flex;align-items:center;justify-content:center;font-size:20px;position:relative}
.vc-avatar.f{background:linear-gradient(135deg,rgba(168,85,247,.2),rgba(0,212,255,.1))}
.vc-avatar.m{background:linear-gradient(135deg,rgba(0,212,255,.2),rgba(0,255,136,.1))}
.vc-avatar.n{background:linear-gradient(135deg,rgba(255,214,10,.2),rgba(255,107,53,.1))}
.vc-n{font-size:13px;font-weight:600;margin-bottom:8px;
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vtags{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}
.vt{font-size:9px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;padding:3px 8px;border-radius:4px}
.vt-f{background:rgba(168,85,247,.15);color:#c4b5fd}
.vt-m{background:rgba(0,212,255,.12);color:#67e8f9}
.vt-s{background:rgba(0,255,136,.1);color:#6ee7b7}
.vt-y{background:rgba(255,214,10,.1);color:var(--ye)}
.vt-c{background:rgba(255,107,53,.1);color:var(--or)}
.vt-n{background:rgba(0,212,255,.08);color:var(--cyan)}

/* ── TTS PAGE ── */
.tts-wrap{display:grid;grid-template-columns:1fr 300px;gap:22px;align-items:start}
@media(max-width:900px){.tts-wrap{grid-template-columns:1fr}}
.sv-bar{background:rgba(0,212,255,.06);border:1px solid rgba(0,212,255,.2);
        border-radius:12px;padding:12px 16px;display:none;align-items:center;gap:12px;margin-bottom:16px}
.sv-bar.show{display:flex;animation:fadeIn .3s ease}
.sv-dot{width:8px;height:8px;border-radius:50%;background:var(--cyan);
        box-shadow:0 0 8px var(--cyan);flex-shrink:0;animation:blink-dot 1.5s ease-in-out infinite}
.sv-nm{font-size:13px;font-weight:600;color:var(--cyan);flex:1}
.sv-clr{background:none;border:none;font-size:14px;color:var(--tx3);cursor:pointer;padding:3px}
.sv-clr:hover{color:var(--re)}
.cc{font-size:11px;color:var(--tx3);text-align:right;margin-top:6px}
.cc.warn{color:var(--ye)}
.cpill{display:inline-flex;align-items:center;gap:6px;background:rgba(255,214,10,.06);
       border:1px solid rgba(255,214,10,.2);border-radius:8px;padding:5px 14px;
       font-size:12px;font-weight:600;color:var(--ye);margin-bottom:16px;
       font-family:'Space Mono',monospace}

/* ── WAVE ANIMATION ── */
.wavebox{background:var(--card);border:1px solid rgba(0,212,255,.2);border-radius:14px;
         padding:20px;margin-top:16px;display:none;align-items:center;gap:18px}
.wavebox.show{display:flex;animation:fadeIn .3s ease}
.bars{display:flex;align-items:center;gap:3px;height:42px;flex-shrink:0}
.bar{width:3px;border-radius:2px;
     background:linear-gradient(180deg,var(--cyan),rgba(0,212,255,.2));
     animation:wb .9s ease-in-out infinite;transform-origin:bottom}
.bar:nth-child(1){height:12px;animation-delay:0s}
.bar:nth-child(2){height:22px;animation-delay:.08s}
.bar:nth-child(3){height:32px;animation-delay:.16s}
.bar:nth-child(4){height:40px;animation-delay:.24s}
.bar:nth-child(5){height:32px;animation-delay:.32s}
.bar:nth-child(6){height:22px;animation-delay:.40s}
.bar:nth-child(7){height:12px;animation-delay:.48s}
.bar:nth-child(8){height:22px;animation-delay:.40s}
.bar:nth-child(9){height:32px;animation-delay:.32s}
.bar:nth-child(10){height:40px;animation-delay:.24s}
.bar:nth-child(11){height:32px;animation-delay:.16s}
.bar:nth-child(12){height:22px;animation-delay:.08s}
.bar:nth-child(13){height:12px;animation-delay:0s}
.bar:nth-child(14){height:22px;animation-delay:.08s}
.bar:nth-child(15){height:32px;animation-delay:.16s}
@keyframes wb{0%,100%{transform:scaleY(.15);opacity:.3}50%{transform:scaleY(1);opacity:1}}
.wave-t{font-size:14px;font-weight:600;color:var(--cyan);margin-bottom:4px;
        text-shadow:0 0 15px rgba(0,212,255,.4)}
.wave-m{font-size:12px;color:var(--tx3)}

/* ── AUDIO RESULT ── */
.ares{background:rgba(0,255,136,.04);border:1px solid rgba(0,255,136,.2);
      border-radius:14px;padding:18px;margin-top:16px;display:none}
.ares.show{display:block;animation:slideUp .4s cubic-bezier(.34,1.56,.64,1)}
@keyframes slideUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
.ares-t{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.ares-badge{font-size:11px;color:var(--gr);font-weight:600;display:flex;align-items:center;gap:6px}
.ares-badge::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--gr);
                    box-shadow:0 0 8px var(--gr);animation:blink-dot 2s ease-in-out infinite}
audio{width:100%;height:38px;border-radius:9px;accent-color:var(--cyan);outline:none}
.dl-btn{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
        color:#000;background:linear-gradient(135deg,var(--cyan2),var(--cyan));
        border-radius:8px;padding:7px 14px;text-decoration:none;transition:all .2s;
        box-shadow:0 4px 14px rgba(0,212,255,.3);display:flex;align-items:center;gap:5px}
.dl-btn:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(0,212,255,.5)}
.a-url{font-size:10px;color:var(--tx3);margin-top:8px;word-break:break-all;
       font-family:'Space Mono',monospace}

/* ── VOICE PICKER ── */
.vpc{background:var(--card);border:1px solid var(--b1);border-radius:16px;
     padding:20px;position:sticky;top:80px}
.vpc h3{font-size:10px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
        color:var(--tx3);margin-bottom:13px}
.vsrch{width:100%;background:rgba(255,255,255,.03);border:1px solid var(--b1);border-radius:9px;
       padding:9px 13px;color:var(--tx);font-size:12px;outline:none;margin-bottom:11px;
       font-family:'Space Grotesk',sans-serif;transition:all .2s}
.vsrch:focus{border-color:rgba(0,212,255,.4)}.vsrch::placeholder{color:var(--tx3)}
.vp-list{display:flex;flex-direction:column;gap:4px;max-height:420px;overflow-y:auto;padding-right:4px}
.vi{background:rgba(255,255,255,.02);border:1px solid var(--b2);border-radius:9px;
    padding:9px 13px;display:flex;align-items:center;gap:9px;cursor:pointer;transition:all .2s}
.vi:hover{border-color:rgba(0,212,255,.2);background:rgba(0,212,255,.04)}
.vi.on{border-color:rgba(0,212,255,.4);background:rgba(0,212,255,.08)}
.vi-d{width:6px;height:6px;border-radius:50%;background:var(--tx3);flex-shrink:0;transition:all .2s}
.vi.on .vi-d{background:var(--cyan);box-shadow:0 0 6px var(--cyan)}
.vi-n{font-size:12px;font-weight:500;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vi-s{font-size:9px;color:var(--tx3);background:var(--b1);border-radius:3px;padding:1px 6px;
      font-family:'Space Mono',monospace}

/* ── HISTORY ── */
.hgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.hc{background:var(--card);border:1px solid var(--b1);border-radius:14px;padding:18px;
    transition:all .2s;position:relative;overflow:hidden}
.hc:hover{border-color:rgba(0,212,255,.2);box-shadow:0 4px 20px rgba(0,212,255,.06)}
.hc::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
            background:linear-gradient(90deg,transparent,rgba(0,212,255,.15),transparent)}
.hc-v{font-size:10px;color:var(--cyan);font-weight:600;letter-spacing:.08em;
      text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.hc-v::before{content:'';width:4px;height:4px;border-radius:50%;background:var(--cyan);
              box-shadow:0 0 6px var(--cyan)}
.hc-t{font-size:13px;color:var(--tx2);margin-bottom:10px;line-height:1.5;
      display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.hc-d{font-size:10px;color:var(--tx3);margin-bottom:12px;font-family:'Space Mono',monospace}

/* ── MY VOICES ── */
.cvc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px}
.cvc{background:var(--card);border:1px solid var(--b1);border-radius:16px;padding:20px;
     transition:all .3s;position:relative;overflow:hidden}
.cvc::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
             background:linear-gradient(90deg,transparent,rgba(0,212,255,.1),transparent);
             opacity:0;transition:opacity .3s}
.cvc:hover{border-color:rgba(0,212,255,.25);transform:translateY(-2px);
           box-shadow:0 8px 32px rgba(0,212,255,.08)}
.cvc:hover::before{opacity:1}
.cvc-av{width:52px;height:52px;border-radius:14px;display:flex;align-items:center;
        justify-content:center;font-size:24px;margin-bottom:14px;position:relative}
.cvc-av.f{background:linear-gradient(135deg,rgba(168,85,247,.25),rgba(0,212,255,.15))}
.cvc-av.m{background:linear-gradient(135deg,rgba(0,212,255,.25),rgba(0,255,136,.15))}
.cvc-av.n{background:linear-gradient(135deg,rgba(255,214,10,.25),rgba(255,107,53,.15))}
.cvc-n{font-size:15px;font-weight:700;margin-bottom:5px;
       white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cvc-badge{font-size:9px;color:var(--cyan);font-weight:700;letter-spacing:.1em;
           text-transform:uppercase;background:rgba(0,212,255,.1);border:1px solid rgba(0,212,255,.2);
           border-radius:4px;padding:2px 8px;display:inline-block;margin-bottom:10px}
.cvc-date{font-size:10px;color:var(--tx3);margin-bottom:14px;font-family:'Space Mono',monospace}
.cvc-btns{display:flex;gap:8px}

/* ── STATS GRID ── */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin-bottom:24px}
.sc{background:var(--card);border:1px solid var(--b1);border-radius:14px;padding:18px;
    position:relative;overflow:hidden;transition:all .3s}
.sc:hover{transform:translateY(-2px)}
.sc::before{content:'';position:absolute;top:-20px;right:-20px;width:80px;height:80px;
            border-radius:50%;filter:blur(30px);pointer-events:none}
.sc.c1::before{background:rgba(0,212,255,.3)}
.sc.c2::before{background:rgba(0,255,136,.3)}
.sc.c3::before{background:rgba(255,214,10,.3)}
.sc.c4::before{background:rgba(255,51,102,.3)}
.sc-l{font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.12em;
      color:var(--tx3);margin-bottom:10px}
.sc-v{font-family:'Space Mono',monospace;font-size:28px;font-weight:700;line-height:1}

/* ── ADMIN ── */
.sb.adm .sb-name-t{background:linear-gradient(135deg,var(--re),var(--or));
                   -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sb.adm .si.on{color:var(--re);background:rgba(255,51,102,.06);border-color:rgba(255,51,102,.15)}
.sb.adm .si.on::after{background:var(--re)}
.adm-pill{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
          color:var(--re);background:rgba(255,51,102,.1);border:1px solid rgba(255,51,102,.2);
          border-radius:20px;padding:5px 14px}
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{text-align:left;padding:10px 14px;font-size:9px;font-weight:600;letter-spacing:.12em;
        text-transform:uppercase;color:var(--tx3);border-bottom:1px solid var(--b1)}
.tbl td{padding:11px 14px;border-bottom:1px solid rgba(255,255,255,.03);color:var(--tx2);vertical-align:middle}
.tbl tr:last-child td{border-bottom:none}
.tbl tr:hover td{background:rgba(255,255,255,.02)}
.tbl .nm{font-weight:600;color:var(--tx)}
.crn{font-family:'Space Mono',monospace;font-size:14px;font-weight:700;color:var(--cyan)}
.r2p{font-size:9px;color:var(--cyan);background:rgba(0,212,255,.08);border-radius:4px;
     padding:2px 8px;font-family:'Space Mono',monospace}
.badge{display:inline-flex;padding:3px 10px;border-radius:5px;font-size:9px;
       font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.ba{background:rgba(0,255,136,.1);color:var(--gr)}
.bb{background:rgba(255,51,102,.12);color:var(--re)}
.be{background:rgba(255,214,10,.1);color:var(--ye)}
.alert-ok{background:rgba(0,255,136,.06);border:1px solid rgba(0,255,136,.2);border-radius:12px;
          padding:13px 16px;font-size:13px;color:var(--gr);margin-bottom:16px}
.alert-er{background:rgba(255,51,102,.06);border:1px solid rgba(255,51,102,.2);border-radius:12px;
          padding:13px 16px;font-size:13px;color:var(--re);margin-bottom:16px}
.srow{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap;align-items:center}
.sbr{background:rgba(255,255,255,.03);border:1px solid var(--b1);border-radius:10px;
     padding:10px 14px;color:var(--tx);font-family:'Space Grotesk',sans-serif;font-size:13px;
     outline:none;transition:all .2s;flex:1;min-width:160px}
.sbr:focus{border-color:rgba(0,212,255,.4)}.sbr::placeholder{color:var(--tx3)}
.pager{display:flex;gap:10px;align-items:center;margin-top:16px;
       justify-content:center;font-size:12px;color:var(--tx3)}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:700px){.two-col{grid-template-columns:1fr}}
.ifrow{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.ifrow .finp{width:90px!important;flex-shrink:0}
.code-box{font-family:'Space Mono',monospace;font-size:12px;color:var(--tx2);
          line-height:2.2;background:rgba(0,0,0,.4);border:1px solid var(--b1);
          border-radius:10px;padding:16px}

/* ── AUTH PAGE ── */
.auth-page{flex:1;display:flex;align-items:center;justify-content:center;
           padding:20px;position:relative}
.auth-bg{position:absolute;inset:0;overflow:hidden;pointer-events:none}
.auth-bg::before{content:'';position:absolute;top:50%;left:50%;width:800px;height:800px;
  transform:translate(-50%,-50%);
  background:radial-gradient(circle,rgba(0,212,255,.05) 0%,transparent 65%);
  animation:expand 4s ease-in-out infinite}
.abox{background:rgba(13,21,38,.95);border:1px solid rgba(0,212,255,.15);border-radius:24px;
      padding:44px 42px;max-width:440px;width:100%;text-align:center;position:relative;
      box-shadow:0 24px 80px rgba(0,0,0,.5),0 0 0 1px rgba(0,212,255,.08);
      backdrop-filter:blur(20px);animation:popIn .5s cubic-bezier(.34,1.56,.64,1)}
@keyframes popIn{from{opacity:0;transform:scale(.88) translateY(24px)}to{opacity:1;transform:scale(1) translateY(0)}}
.abox::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
              background:linear-gradient(90deg,transparent,var(--cyan),transparent)}
.abox-logo-wrap{margin-bottom:20px;display:flex;justify-content:center}
.abox-logo{width:72px;height:72px}
.abox h2{font-size:28px;font-weight:700;letter-spacing:-.5px;margin-bottom:8px;
         background:linear-gradient(135deg,var(--tx) 30%,var(--cyan));
         -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.abox p{color:var(--tx3);font-size:13px;margin-bottom:30px;line-height:1.5}
.abox-btn{width:100%;padding:15px;background:linear-gradient(135deg,var(--cyan2),var(--cyan));
          border:none;border-radius:12px;color:#000;font-family:'Space Grotesk',sans-serif;
          font-size:15px;font-weight:700;cursor:pointer;margin-top:6px;
          box-shadow:0 6px 28px rgba(0,212,255,.4);transition:all .2s;letter-spacing:.02em}
.abox-btn:hover{transform:translateY(-1px);box-shadow:0 8px 36px rgba(0,212,255,.6)}
.abox-btn:disabled{opacity:.6;cursor:not-allowed;transform:none}

/* ── EMPTY STATE ── */
.empty{text-align:center;padding:64px 20px}
.empty-ic{font-size:56px;margin-bottom:18px;display:block;
          filter:drop-shadow(0 0 20px rgba(0,212,255,.3));
          animation:float 3s ease-in-out infinite}
.empty h3{font-size:20px;font-weight:700;margin-bottom:10px;letter-spacing:-.3px}
.empty p{font-size:13px;color:var(--tx3);line-height:1.8}
.empty a{color:var(--cyan)}

/* ── TOAST ── */
.toasts{position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px}
.toast{background:rgba(13,21,38,.96);border:1px solid var(--b1);border-radius:12px;
       padding:13px 18px;font-size:13px;min-width:240px;max-width:360px;
       box-shadow:0 16px 48px rgba(0,0,0,.6),0 0 0 1px rgba(255,255,255,.04);
       animation:slideInToast .25s ease;display:flex;align-items:center;gap:11px;
       line-height:1.4;backdrop-filter:blur(20px)}
.toast.ok{border-color:rgba(0,255,136,.3)}
.toast.er{border-color:rgba(255,51,102,.3)}
.toast.in{border-color:rgba(0,212,255,.3)}
@keyframes slideInToast{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:translateX(0)}}

/* ── ANIMATIONS ── */
@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
@keyframes pulse-logo{0%,100%{opacity:1}50%{opacity:.4}}
</style>
"""

SHARED_JS = """
<script>
function toast(msg, type){
  type = type||'in';
  var ic={ok:'✅',er:'❌',in:'💡'};
  var t=document.createElement('div');
  t.className='toast '+type;
  t.innerHTML='<span>'+ic[type]+'</span><span>'+esc(msg)+'</span>';
  var b=document.getElementById('toasts');
  if(!b){b=document.createElement('div');b.id='toasts';b.className='toasts';document.body.appendChild(b);}
  b.appendChild(t);setTimeout(function(){t.remove();},4500);
}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function sl(ms){return new Promise(function(r){setTimeout(r,ms);});}
</script>
"""

LOGO_SVG = """
<svg viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="lg1" x1="0" y1="0" x2="40" y2="40" gradientUnits="userSpaceOnUse">
      <stop stop-color="#00d4ff"/>
      <stop offset="1" stop-color="#7c3aed"/>
    </linearGradient>
  </defs>
  <circle cx="20" cy="20" r="18" stroke="url(#lg1)" stroke-width="1.5" stroke-dasharray="4 2" class="logo-ring"/>
  <circle cx="20" cy="20" r="12" fill="rgba(0,212,255,0.08)" stroke="rgba(0,212,255,0.3)" stroke-width="1"/>
  <circle cx="20" cy="20" r="5" fill="url(#lg1)" class="logo-dot"/>
  <path d="M14 20 Q17 14 20 20 Q23 26 26 20" stroke="rgba(0,212,255,0.8)" stroke-width="1.5" fill="none" stroke-linecap="round"/>
</svg>"""

def page(title, body, extra=""):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>""" + title + """ — NexVoice</title>
""" + HEAD + extra + """
</head>
<body>
""" + body + """
<div class="toasts" id="toasts"></div>
""" + SHARED_JS + """
</body>
</html>"""

# ── sidebar builders ────────────────────────────────────
USER_NAV = [
    ("🏠","Dashboard",   "/dashboard"),
    ("✨","Clone Voice", "/clone"),
    ("🎤","My Voices",   "/voices"),
    ("📚","Library",     "/library"),
    ("🔊","Text to Speech","/tts"),
    ("📋","History",     "/history"),
]
ADMIN_NAV = [
    ("📊","Dashboard",   "/admin"),
    ("👥","All Users",   "/admin/users"),
    ("➕","Create User", "/admin/create"),
    ("🎤","All Voices",  "/admin/voices"),
    ("🌐","Shared Library","/admin/library"),
    ("📋","Activity Log","/admin/log"),
]

def user_sb(active=""):
    un  = get_un()
    u   = get_user(un) or {}
    cr  = u.get("credits",0)
    exp = u.get("expires_at","")
    warn = ('<div class="exp-pill">⏳ Expires: '+exp+'</div>') if exp else ""
    nav = ""
    for ic,label,href in USER_NAV:
        on = "on" if href==active else ""
        nav += '<a href="'+href+'" class="si '+on+'"><div class="si-ic">'+ic+'</div>'+label+'</a>\n'
    return """
<aside class="sb">
  <a href="/dashboard" class="sb-logo" style="text-decoration:none">
    <div class="logo-ic">"""+LOGO_SVG+"""</div>
    <div class="sb-name">
      <span class="sb-name-t">NexVoice</span>
      <span class="sb-name-s">AI Voice Platform</span>
    </div>
  </a>
  <nav class="sb-nav">
    <div class="sb-sec">Navigation</div>
    """+nav+"""
  </nav>
  <div class="sb-bot">
    <div class="sb-cred">
      <div class="cbox">
        <div class="cbox-l">💰 Credits Balance</div>
        <div class="cbox-n">"""+f"{cr:,}"+"""</div>
        <div class="cbox-s">1 credit = 1 character</div>
      </div>"""+warn+"""
    </div>
    <div class="sb-user">
      <div class="u-av">"""+un[0].upper()+"""</div>
      <div class="u-inf">
        <div class="u-nm">"""+un+"""</div>
        <div class="u-r2">users/"""+un+"""/</div>
      </div>
      <a href="/logout" class="u-out" title="Logout">⏻</a>
    </div>
  </div>
</aside>"""

def admin_sb(active=""):
    nav = ""
    for ic,label,href in ADMIN_NAV:
        on = "on" if href==active else ""
        nav += '<a href="'+href+'" class="si '+on+'"><div class="si-ic">'+ic+'</div>'+label+'</a>\n'
    return """
<aside class="sb adm">
  <a href="/admin" class="sb-logo" style="text-decoration:none">
    <div class="logo-ic">"""+LOGO_SVG+"""</div>
    <div class="sb-name">
      <span class="sb-name-t">NexVoice</span>
      <span class="sb-name-s">Admin Console</span>
    </div>
  </a>
  <nav class="sb-nav">
    <div class="sb-sec">Management</div>
    """+nav+"""
  </nav>
  <div class="sb-bot">
    <div class="sb-user" style="flex-direction:column;align-items:flex-start;gap:8px">
      <a href="/admin/logout" style="color:var(--re);text-decoration:none;font-size:12px;font-weight:600">⏻ Sign Out</a>
      <a href="/" style="color:var(--cyan);text-decoration:none;font-size:12px">← User App</a>
    </div>
  </div>
</aside>"""

def main_open(title, pill="AI Voice Engine"):
    return """
<div class="main">
  <div class="topbar">
    <span class="tb-t">"""+title+"""</span>
    <span class="tb-pill">"""+pill+"""</span>
  </div>
  <div class="content">"""

def admin_open(title):
    return """
<div class="main">
  <div class="topbar">
    <span class="tb-t">"""+title+"""</span>
    <span class="adm-pill">Admin</span>
  </div>
  <div class="content">"""

END = "</div></div>"

def tcls(t):
    t=t.lower()
    if t in ["female","warm","storytelling","gentle","soft","expressive","elegant","soothing","dreamy","passionate"]: return "vt-f"
    if t in ["male","deep","authoritative","strong","intense","dramatic"]: return "vt-m"
    if t in ["calm","meditation","relaxing","neutral"]: return "vt-s"
    if t in ["young","casual","friendly","energetic","conversational"]: return "vt-y"
    if t in ["narration","news","documentary","audiobook","professional","clear"]: return "vt-c"
    return "vt-n"

# ═══════════════════════════════════════════════════════
#  USER AUTH
# ═══════════════════════════════════════════════════════
@app.route("/")
def root():
    return redirect("/dashboard" if session.get("username") else "/login")

@app.route("/login", methods=["GET","POST"])
def login():
    err=""
    if request.method=="POST":
        un=request.form.get("username","").strip().lower()
        pw=request.form.get("password","").strip()
        if not un or not pw: err="Enter username and password"
        else:
            u=get_user(un)
            if not u or u.get("password","")!=hp(pw): err="Invalid username or password"
            else:
                e=check_access(u)
                if e: err=e
                else:
                    session["username"]=un
                    write_log(un,"Login")
                    return redirect(request.args.get("next","/dashboard"))
    al = ('<div class="alert-er" style="margin-bottom:18px">'+err+'</div>') if err else ""
    body = """
<div class="auth-page">
  <div class="auth-bg"></div>
  <div class="abox">
    <div class="abox-logo-wrap">
      <svg class="abox-logo" viewBox="0 0 72 72" fill="none" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="alg" x1="0" y1="0" x2="72" y2="72" gradientUnits="userSpaceOnUse">
            <stop stop-color="#00d4ff"/><stop offset="1" stop-color="#7c3aed"/>
          </linearGradient>
        </defs>
        <circle cx="36" cy="36" r="34" stroke="url(#alg)" stroke-width="1.5" stroke-dasharray="6 3" style="animation:spin 12s linear infinite;transform-origin:36px 36px"/>
        <circle cx="36" cy="36" r="24" fill="rgba(0,212,255,0.06)" stroke="rgba(0,212,255,0.2)" stroke-width="1"/>
        <circle cx="36" cy="36" r="10" fill="url(#alg)" style="animation:pulse-logo 2.5s ease-in-out infinite"/>
        <path d="M22 36 Q29 22 36 36 Q43 50 50 36" stroke="rgba(0,212,255,0.9)" stroke-width="2" fill="none" stroke-linecap="round"/>
      </svg>
    </div>
    <h2>NexVoice</h2>
    <p>AI Voice Cloning &amp; Text to Speech<br>Sign in to your studio</p>
    """+al+"""
    <form method="POST">
      <div class="fd">
        <label class="flbl">Username</label>
        <input type="text" name="username" class="finp" style="text-align:center" placeholder="your username" required autofocus>
      </div>
      <div class="fd">
        <label class="flbl">Password</label>
        <input type="password" name="password" class="finp" style="text-align:center" placeholder="••••••••" required>
      </div>
      <button type="submit" class="abox-btn">Access Studio →</button>
    </form>
  </div>
</div>"""
    return page("Sign In", body)

@app.route("/logout")
def logout():
    session.clear(); return redirect("/login")

# ═══════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════
@app.route("/dashboard")
@login_required
def dashboard():
    un   = get_un()
    u    = get_user(un) or {}
    vs   = load_voices(un)
    hist = load_hist(un)
    cr   = u.get("credits",0)
    done = [v for v in vs if v.get("voice_id")]
    proc = [v for v in vs if not v.get("voice_id")]

    stat_html = """
<div class="stats">
  <div class="sc c1">
    <div class="sc-l">Credits Balance</div>
    <div class="sc-v" style="color:var(--cyan)">"""+f"{cr:,}"+"""</div>
  </div>
  <div class="sc c2">
    <div class="sc-l">Cloned Voices</div>
    <div class="sc-v" style="color:var(--gr)">"""+str(len(done))+"""/"""+str(MAX_CLONES)+"""</div>
  </div>
  <div class="sc c3">
    <div class="sc-l">TTS Generated</div>
    <div class="sc-v" style="color:var(--ye)">"""+str(len(hist))+"""</div>
  </div>
  <div class="sc c4">
    <div class="sc-l">Processing</div>
    <div class="sc-v" style="color:var(--or)">"""+str(len(proc))+"""</div>
  </div>
</div>"""

    # Recent voices
    if not done:
        recent_v = '<div class="empty" style="padding:32px"><span class="empty-ic" style="font-size:36px">🎤</span><h3 style="font-size:16px">No voices yet</h3><p><a href="/clone">Clone your first voice</a></p></div>'
    else:
        cards = ""
        for v in done[:4]:
            g = (v.get("gender","") or "").lower()
            gc = "f" if g=="female" else ("m" if g=="male" else "n")
            em = "👩" if g=="female" else ("👨" if g=="male" else "🎙")
            cards += '<div class="cvc" style="cursor:default"><div class="cvc-av '+gc+'">'+em+'</div><div class="cvc-n">'+v.get("name","")+'</div><div class="cvc-date">'+v.get("created_at","")+'</div>'
            if v.get("voice_id"):
                cards += '<a href="/tts?vid='+v["voice_id"]+'&vn='+v.get("name","Voice")+'" class="btn bcyan bsm bfl">Use in TTS</a>'
            cards += '</div>'
        recent_v = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px">'+cards+'</div>'

    # Recent history
    if not hist:
        recent_h = '<div style="text-align:center;padding:24px;color:var(--tx3);font-size:13px">No TTS history yet. <a href="/tts" style="color:var(--cyan)">Generate audio →</a></div>'
    else:
        rows = ""
        for h in hist[:3]:
            url  = h.get("audio_url","")
            ael  = ('<audio controls src="'+url+'" style="width:100%;height:34px;border-radius:8px;accent-color:var(--cyan)"></audio>') if url else '<div style="font-size:11px;color:var(--tx3)">Processing…</div>'
            rows += '<div class="hc"><div class="hc-v">'+h.get("voice_name","?")+'</div><div class="hc-t">'+h.get("text","")[:80]+'</div><div class="hc-d">'+h.get("ts","")+'</div>'+ael+'</div>'
        recent_h = '<div class="hgrid">'+rows+'</div>'

    # Quick actions
    qa = """
<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px;margin-bottom:24px">
  <a href="/clone" class="card" style="text-decoration:none;cursor:pointer;text-align:center;transition:all .3s;padding:20px">
    <div style="font-size:32px;margin-bottom:10px;filter:drop-shadow(0 0 12px rgba(0,212,255,.5))">✨</div>
    <div style="font-size:14px;font-weight:600;margin-bottom:4px">Clone Voice</div>
    <div style="font-size:11px;color:var(--tx3)">Upload audio to clone</div>
  </a>
  <a href="/library" class="card" style="text-decoration:none;cursor:pointer;text-align:center;transition:all .3s;padding:20px">
    <div style="font-size:32px;margin-bottom:10px;filter:drop-shadow(0 0 12px rgba(168,85,247,.5))">📚</div>
    <div style="font-size:14px;font-weight:600;margin-bottom:4px">Voice Library</div>
    <div style="font-size:11px;color:var(--tx3)">596 built-in voices</div>
  </a>
  <a href="/tts" class="card" style="text-decoration:none;cursor:pointer;text-align:center;transition:all .3s;padding:20px">
    <div style="font-size:32px;margin-bottom:10px;filter:drop-shadow(0 0 12px rgba(0,255,136,.4))">🔊</div>
    <div style="font-size:14px;font-weight:600;margin-bottom:4px">Generate Speech</div>
    <div style="font-size:11px;color:var(--tx3)">Text to speech</div>
  </a>
  <a href="/history" class="card" style="text-decoration:none;cursor:pointer;text-align:center;transition:all .3s;padding:20px">
    <div style="font-size:32px;margin-bottom:10px;filter:drop-shadow(0 0 12px rgba(255,214,10,.4))">📋</div>
    <div style="font-size:14px;font-weight:600;margin-bottom:4px">My History</div>
    <div style="font-size:11px;color:var(--tx3)">Download generated audio</div>
  </a>
</div>"""

    body = user_sb("/dashboard") + main_open("Dashboard") + """
<div class="ph">
  <div>
    <div class="ph-t">Welcome back, """+un+"""</div>
    <div class="ph-s">Your AI voice studio is ready</div>
  </div>
</div>
"""+stat_html+qa+"""
<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
  <div class="card">
    <div class="card-t">🎤 My Voices <a href="/voices" style="font-size:11px;color:var(--cyan);margin-left:auto;text-decoration:none">View all →</a></div>
    """+recent_v+"""
  </div>
  <div class="card">
    <div class="card-t">📋 Recent TTS <a href="/history" style="font-size:11px;color:var(--cyan);margin-left:auto;text-decoration:none">View all →</a></div>
    """+recent_h+"""
  </div>
</div>
"""+END
    return page("Dashboard", body)

# ═══════════════════════════════════════════════════════
#  CLONE VOICE
# ═══════════════════════════════════════════════════════
@app.route("/clone")
@login_required
def clone_page():
    un  = get_un()
    cnt = len(load_voices(un))
    pct = int(cnt/MAX_CLONES*100)
    body = user_sb("/clone") + main_open("Clone Voice") + """
<div class="ph">
  <div>
    <div class="ph-t">Clone a Voice</div>
    <div class="ph-s">Upload any audio → AI extracts the voice → saved to your library forever</div>
  </div>
</div>
<div style="display:grid;grid-template-columns:1fr 360px;gap:22px;align-items:start">
  <div class="card">
    <div class="uzone" id="uz" ondragover="ev(event,true)" ondragleave="ev(event,false)" ondrop="dp(event)">
      <input type="file" id="fi" accept="audio/*,video/mp4,video/webm" onchange="pf(this)">
      <span class="uz-ic">🎵</span>
      <div class="uz-t">Drop Audio File Here</div>
      <div class="uz-s">MP3, WAV, MP4, M4A, OGG, WEBM — up to 30 MB</div>
      <div class="uz-h">Minimum 5 seconds · Clear speech recommended</div>
      <span class="uz-cta">📂 Browse Files</span>
    </div>
    <div class="fchip" id="fc">
      <span>🎵</span>
      <span class="fchip-nm" id="fcn"></span>
      <button class="btn bdr bsm" onclick="clf()" style="padding:4px 10px">✕</button>
    </div>
    <div style="margin-top:20px">
      <div class="fd">
        <label class="flbl">Voice Name</label>
        <input type="text" id="cn" class="finp" placeholder="e.g. My Professional Voice">
      </div>
      <button class="btn bcyan blg bfl" id="cb" onclick="doClone()">✨ Clone Voice — Free</button>
    </div>
  </div>
  <div>
    <div class="card">
      <div class="card-t">⚡ How it works</div>
      <div style="display:flex;flex-direction:column;gap:14px;font-size:13px;color:var(--tx2)">
        <div style="display:flex;gap:12px;align-items:flex-start">
          <span style="font-size:20px;flex-shrink:0">1️⃣</span>
          <div><div style="font-weight:600;margin-bottom:2px">Upload Audio File</div><div style="color:var(--tx3);font-size:12px">Upload any audio sample of the voice (5s–30MB)</div></div>
        </div>
        <div style="display:flex;gap:12px;align-items:flex-start">
          <span style="font-size:20px;flex-shrink:0">2️⃣</span>
          <div><div style="font-weight:600;margin-bottom:2px">Secure Cloud Storage</div><div style="color:var(--tx3);font-size:12px">Your audio is saved securely in your private cloud space</div></div>
        </div>
        <div style="display:flex;gap:12px;align-items:flex-start">
          <span style="font-size:20px;flex-shrink:0">3️⃣</span>
          <div><div style="font-weight:600;margin-bottom:2px">AI Voice Model Created</div><div style="color:var(--tx3);font-size:12px">Advanced AI creates a high-fidelity voice model (~1 min)</div></div>
        </div>
        <div style="display:flex;gap:12px;align-items:flex-start">
          <span style="font-size:20px;flex-shrink:0">4️⃣</span>
          <div><div style="font-weight:600;margin-bottom:2px">Use in Text to Speech</div><div style="color:var(--tx3);font-size:12px">Generate unlimited audio files with your cloned voice</div></div>
        </div>
      </div>
      <div style="margin-top:18px;padding:13px;background:rgba(0,255,136,.05);border:1px solid rgba(0,255,136,.15);border-radius:10px;font-size:12px;color:var(--gr);display:flex;align-items:center;gap:8px">
        <span>🔒</span> Voice cloning uses <strong>zero credits</strong>
      </div>
    </div>
    <div class="card" style="margin-top:0">
      <div class="card-t">📊 Clone Slots</div>
      <div style="background:rgba(255,255,255,.04);border-radius:6px;height:6px;overflow:hidden;margin-bottom:10px;border:1px solid var(--b1)">
        <div style="height:100%;background:linear-gradient(90deg,var(--cyan2),var(--cyan));border-radius:6px;width:"""+str(pct)+"""%;transition:width .5s;box-shadow:0 0 10px rgba(0,212,255,.4)"></div>
      </div>
      <div style="font-size:13px;color:var(--tx2)">
        <span style="font-family:'Space Mono',monospace;font-size:22px;font-weight:700;color:var(--cyan)">"""+str(cnt)+"""</span>
        <span style="color:var(--tx3)"> / """+str(MAX_CLONES)+""" slots used</span>
      </div>
    </div>
  </div>
</div>

<!-- CLONE OVERLAY -->
<div class="clov" id="clov">
  <div class="clov-bg"></div>
  <div class="ring-wrap">
    <svg class="ring-svg" width="180" height="180" viewBox="0 0 180 180">
      <defs>
        <linearGradient id="rg" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stop-color="#00d4ff"/>
          <stop offset="50%" stop-color="#7c3aed"/>
          <stop offset="100%" stop-color="#00ffcc"/>
        </linearGradient>
      </defs>
      <circle class="r-bg" cx="90" cy="90" r="75"/>
      <circle class="r-arc" id="ra" cx="90" cy="90" r="75"/>
    </svg>
    <div class="ring-inside">
      <div class="ring-n" id="rn">0</div>
      <div class="ring-u">PERCENT</div>
    </div>
  </div>
  <div class="ov-t">Cloning Your Voice</div>
  <div class="ov-m" id="om">Initializing AI engine…</div>
  <div class="ov-steps">
    <div class="ovs" id="s0"><div class="sd"></div>Uploading to Cloudflare R2</div>
    <div class="ovs" id="s1"><div class="sd"></div>Sending to AI Voice Engine</div>
    <div class="ovs" id="s2"><div class="sd"></div>Cloning voice (~1 minute)</div>
    <div class="ovs" id="s3"><div class="sd"></div>Saving to your library</div>
  </div>
</div>
"""+END+"""
<script>
var sf=null,rp=0,rt=null;
function ev(e,o){e.preventDefault();document.getElementById('uz').classList.toggle('drag',o);}
function dp(e){e.preventDefault();ev(e,false);var f=e.dataTransfer.files[0];if(f)setF(f);}
function pf(i){if(i.files[0])setF(i.files[0]);}
function setF(f){
  sf=f;
  document.getElementById('fcn').textContent=f.name+' ('+(f.size/1024/1024).toFixed(2)+' MB)';
  document.getElementById('fc').classList.add('show');
  var n=document.getElementById('cn');
  if(!n.value)n.placeholder=f.name.replace(/\\.[^.]+$/,'');
}
function clf(){sf=null;document.getElementById('fc').classList.remove('show');document.getElementById('fi').value='';}
function sr(p){
  rp=Math.min(p,99);
  document.getElementById('rn').textContent=Math.round(rp);
  var c=2*Math.PI*75;
  document.getElementById('ra').style.strokeDashoffset=c-(c*rp/100);
}
function ar(to,dur){
  var inc=(to-rp)/(dur/50);
  if(rt)clearInterval(rt);
  rt=setInterval(function(){rp=Math.min(rp+inc,to);sr(rp);if(rp>=to)clearInterval(rt);},50);
}
function ss(i,st){var e=document.getElementById('s'+i);if(e)e.className='ovs '+st;}
function sm(m){document.getElementById('om').textContent=m;}
function showOv(){sr(0);document.getElementById('clov').classList.add('show');}
function hideOv(){document.getElementById('clov').classList.remove('show');if(rt)clearInterval(rt);}
async function doClone(){
  if(!sf){toast('Upload an audio file first','er');return;}
  var name=document.getElementById('cn').value.trim()||('Voice '+new Date().toLocaleDateString());
  document.getElementById('cb').disabled=true;
  [0,1,2,3].forEach(function(i){ss(i,'');});
  showOv();
  try{
    ss(0,'act');sm('Uploading audio to Cloudflare R2…');ar(25,6000);
    var fd=new FormData();fd.append('file',sf);fd.append('name',name);
    var ur=await fetch('/api/upload',{method:'POST',body:fd});
    var ud=await ur.json();
    if(!ur.ok){ss(0,'fail');toast(ud.error||'Upload failed','er');hideOv();document.getElementById('cb').disabled=false;return;}
    ss(0,'done');ar(40,600);
    ss(1,'act');sm('Sending to AI voice engine…');ar(50,1500);
    var cr=await fetch('/api/clone',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_voice_file:ud.r2_url,name:name})});
    var cd=await cr.json();
    if(!cr.ok){ss(1,'fail');toast(cd.error||'Clone start failed','er');hideOv();document.getElementById('cb').disabled=false;return;}
    ss(1,'done');ar(55,600);
    ss(2,'act');sm('AI is cloning your voice — please wait…');ar(90,55000);
    var done=false;
    for(var i=0;i<60;i++){
      await sl(3500);
      try{
        var sr2=await fetch('/api/clone/status/'+cd.task_id);
        var sd=await sr2.json();
        if(sd.status==='completed'){done=true;break;}
        if(sd.status==='failed')break;
      }catch(e){}
    }
    if(!done){ss(2,'fail');toast('Clone failed. Please try again.','er');hideOv();document.getElementById('cb').disabled=false;return;}
    ss(2,'done');ar(97,600);
    ss(3,'act');sm('Saving voice to your library…');await sl(700);ss(3,'done');ar(100,400);await sl(800);
    hideOv();
    toast('✅ Voice cloned and saved to your library!','ok');
    await sl(1000);
    window.location.href='/voices';
  }catch(e){hideOv();toast('Error: '+e.message,'er');}
  finally{document.getElementById('cb').disabled=false;}
}
</script>"""
    return page("Clone Voice", body)

# ═══════════════════════════════════════════════════════
#  MY VOICES
# ═══════════════════════════════════════════════════════
@app.route("/voices")
@login_required
def voices_page():
    un  = get_un()
    vvs = load_voices(un)

    if not vvs:
        content = '<div class="empty"><span class="empty-ic">🎤</span><h3>No cloned voices yet</h3><p>Go to <a href="/clone">Clone Voice</a> to create your first voice</p></div>'
    else:
        items = ""
        for v in vvs:
            vid  = v.get("voice_id","")
            tid  = v.get("task_id","")
            nm   = v.get("name","Voice")
            prev = v.get("preview","")
            date = v.get("created_at","")
            g    = (v.get("gender","") or "").lower()
            gc   = "f" if g=="female" else ("m" if g=="male" else "n")
            em   = "👩" if g=="female" else ("👨" if g=="male" else "🎙")
            use  = ('<a href="/tts?vid='+vid+'&vn='+nm+'" class="btn bcyan bsm" style="flex:1">Use in TTS</a>') if vid else '<span class="btn bgh bsm" style="flex:1;opacity:.5;cursor:default">Processing…</span>'
            pbt  = ('<button class="btn bgh bsm" onclick="new Audio(\''+prev+'\').play()">▶</button>') if prev else ""
            items += '<div class="cvc"><div class="cvc-av '+gc+'">'+em+'</div><div class="cvc-n">'+nm+'</div><div class="cvc-badge">MY CLONE</div><div class="cvc-date">'+date+'</div><div class="cvc-btns">'+use+pbt+'<button class="btn bdr bsm" onclick="dv(\''+tid+'\',\''+nm+'\')">🗑</button></div></div>'
        content = '<div class="cvc-grid">'+items+'</div>'

    body = user_sb("/voices") + main_open("My Voices") + """
<div class="ph">
  <div>
    <div class="ph-t">My Cloned Voices</div>
    <div class="ph-s">All your voices — stored permanently in Cloudflare R2</div>
  </div>
  <a href="/clone" class="btn bcyan bmd">✨ Clone New</a>
</div>
"""+content+"""
"""+END+"""
<script>
async function dv(tid,name){
  if(!confirm('Delete "'+name+'"? This cannot be undone.'))return;
  var r=await fetch('/api/clone/'+tid,{method:'DELETE'});
  var d=await r.json();
  if(!r.ok){toast(d.error||'Delete failed','er');return;}
  toast('"'+name+'" deleted successfully','ok');
  setTimeout(function(){location.reload();},900);
}
</script>"""
    return page("My Voices", body)

# ═══════════════════════════════════════════════════════
#  VOICE LIBRARY
# ═══════════════════════════════════════════════════════
@app.route("/library")
@login_required
def library_page():
    filt = request.args.get("f","All")
    srch = request.args.get("q","").lower()
    lang = request.args.get("lang","All")
    gend = request.args.get("gend","All")
    cat  = request.args.get("cat","All")

    try:
        resp = akool("GET", "/voice/list?type=2&page=1&size=100")
        raw  = resp["data"]["result"] if resp.get("code")==1000 else []
    except Exception as e:
        print("[library]",e); raw=[]
    for v in raw: v["tags"] = get_tags(v)

    CATS = ["All","Female","Male","Storytelling","Narration","Young","Professional",
            "Deep","Calm","Warm","Conversational","News","Audiobook","Meditation","British"]

    cats_html = ""
    for c in CATS:
        cnt = len(raw) if c=="All" else sum(1 for v in raw if c in v.get("tags",[]))
        on  = "on" if c==filt else ""
        cats_html += '<a href="/library?f='+c+'&q='+srch+'&lang='+lang+'&gend='+gend+'&cat='+cat+'" class="fcat '+on+'">'+c+' <span class="fcat-n">'+str(cnt)+'</span></a>\n'

    vs = raw
    if filt!="All": vs=[v for v in vs if filt in v.get("tags",[])]
    if lang!="All": vs=[v for v in vs if lang in v.get("tags",[])]
    if gend!="All": vs=[v for v in vs if gend in v.get("tags",[])]
    if srch:        vs=[v for v in vs if srch in (v.get("name","")).lower() or any(srch in t.lower() for t in v.get("tags",[]))]

    if not vs:
        grid = '<div class="empty"><span class="empty-ic">🔍</span><h3>No voices match</h3><p>Try a different filter or search term</p></div>'
    else:
        cards = ""
        for v in vs:
            vid  = v.get("voice_id","")
            nm   = v.get("name","Voice")
            g    = (v.get("gender","") or "").lower()
            gc   = "f" if g=="female" else ("m" if g=="male" else "n")
            em   = "👩" if g=="female" else ("👨" if g=="male" else "🎙")
            tags = "".join('<span class="vt '+tcls(t)+'">'+t+'</span>' for t in v.get("tags",[])[:3])
            prev = v.get("preview","")
            pbt  = ('<button class="btn bgh bsm" onclick="new Audio(\''+prev+'\').play()" style="padding:7px 10px">▶</button>') if prev else ""
            cards += '<div class="vc"><div class="vc-avatar '+gc+'">'+em+'</div><div class="vc-n">'+nm+'</div><div class="vtags">'+tags+'</div><div style="display:flex;gap:7px;margin-top:6px"><a href="/tts?vid='+vid+'&vn='+nm+'" class="btn bcyan bsm" style="flex:1">Use in TTS</a>'+pbt+'</div></div>'
        grid = '<div class="vgrid">'+cards+'</div>'

    body = user_sb("/library") + main_open("Voice Library", "596 Built-in Voices") + """
<div class="ph">
  <div>
    <div class="ph-t">Voice Library</div>
    <div class="ph-s">"""+str(len(raw))+""" professional voices with Language, Gender, Age and Category filters</div>
  </div>
</div>
<div class="lib-wrap">
  <div class="fpanel">
    <div class="fp-t">Filters</div>
    <form method="GET" id="sf">
      <input type="text" name="q" class="lib-s" placeholder="Search by name or keyword…"
             value=\""""+srch+"""\" oninput="this.form.submit()">
      <input type="hidden" name="f" value=\""""+filt+"""\">
      <input type="hidden" name="lang" value=\""""+lang+"""\">
      <input type="hidden" name="gend" value=\""""+gend+"""\">
    </form>
    <div class="fp-t" style="margin-top:14px">Category</div>
    <div class="fcats">"""+cats_html+"""</div>
    <div class="fp-t" style="margin-top:14px">Language</div>
    <div class="fcats">
      <a href="/library?f="""+filt+"""&lang=All&gend="""+gend+"""&q="""+srch+"""" class="fcat """+("on" if lang=="All" else "")+"""">All Languages</a>
      <a href="/library?f="""+filt+"""&lang=English&gend="""+gend+"""&q="""+srch+"""" class="fcat """+("on" if lang=="English" else "")+"""">English</a>
      <a href="/library?f="""+filt+"""&lang=Chinese&gend="""+gend+"""&q="""+srch+"""" class="fcat """+("on" if lang=="Chinese" else "")+"""">Chinese</a>
      <a href="/library?f="""+filt+"""&lang=Spanish&gend="""+gend+"""&q="""+srch+"""" class="fcat """+("on" if lang=="Spanish" else "")+"""">Spanish</a>
      <a href="/library?f="""+filt+"""&lang=French&gend="""+gend+"""&q="""+srch+"""" class="fcat """+("on" if lang=="French" else "")+"""">French</a>
      <a href="/library?f="""+filt+"""&lang=German&gend="""+gend+"""&q="""+srch+"""" class="fcat """+("on" if lang=="German" else "")+"""">German</a>
      <a href="/library?f="""+filt+"""&lang=Japanese&gend="""+gend+"""&q="""+srch+"""" class="fcat """+("on" if lang=="Japanese" else "")+"""">Japanese</a>
      <a href="/library?f="""+filt+"""&lang=Korean&gend="""+gend+"""&q="""+srch+"""" class="fcat """+("on" if lang=="Korean" else "")+"""">Korean</a>
    </div>
    <div class="fp-t" style="margin-top:14px">Gender</div>
    <div class="fcats">
      <a href="/library?f="""+filt+"""&gend=All&lang="""+lang+"""&q="""+srch+"""" class="fcat """+("on" if gend=="All" else "")+"""">All Genders</a>
      <a href="/library?f="""+filt+"""&gend=Female&lang="""+lang+"""&q="""+srch+"""" class="fcat """+("on" if gend=="Female" else "")+"""">Female</a>
      <a href="/library?f="""+filt+"""&gend=Male&lang="""+lang+"""&q="""+srch+"""" class="fcat """+("on" if gend=="Male" else "")+"""">Male</a>
    </div>
  </div>
  <div>"""+grid+"""</div>
</div>
"""+END
    return page("Voice Library", body)

# ═══════════════════════════════════════════════════════
#  TEXT TO SPEECH
# ═══════════════════════════════════════════════════════
@app.route("/tts")
@login_required
def tts_page():
    un    = get_un()
    user  = get_user(un) or {}
    creds = user.get("credits",0)
    pvid  = request.args.get("vid","")
    pvn   = request.args.get("vn","")

    my = [v for v in load_voices(un) if v.get("voice_id")]
    shared = [v for v in load_shared_voices() if v.get("voice_id")]
    try:
        resp = akool("GET", "/voice/list?type=2&page=1&size=100")
        akv  = resp["data"]["result"] if resp.get("code")==1000 else []
    except: akv=[]

    all_v = [{"voice_id":v["voice_id"],"name":v["name"],"src":"CLONE"} for v in my] +             [{"voice_id":v["voice_id"],"name":v.get("name","")+" ✦","src":"SHARED"} for v in shared] +             [{"voice_id":v["voice_id"],"name":v.get("name","Voice"),"src":(v.get("locale","EN")).upper()[:5]} for v in akv]

    vp = ""
    for v in all_v:
        on = "on" if v["voice_id"]==pvid else ""
        vp += '<div class="vi '+on+'" onclick="pv(\''+v["voice_id"]+'\',\''+v["name"].replace("'","")+'\',this)"><div class="vi-d"></div><span class="vi-n">'+v["name"]+'</span><span class="vi-s">'+v["src"]+'</span></div>'

    sv_show = "display:flex" if pvid else "display:none"

    body = user_sb("/tts") + main_open("Text to Speech") + """
<div class="ph">
  <div>
    <div class="ph-t">Text to Speech</div>
    <div class="ph-s">Generate high-quality speech · 1 credit = 1 character · Voice cloning is free</div>
  </div>
</div>
<div class="tts-wrap">
  <div>
    <div class="sv-bar" id="svb" style=\""""+sv_show+"""\">
      <div class="sv-dot"></div>
      <span class="sv-nm" id="svn">"""+pvn+"""</span>
      <button class="sv-clr" onclick="cv()">✕</button>
    </div>
    <div class="cpill">💰 <span id="tc">0</span> credits &nbsp;|&nbsp; Balance: <span id="tb">"""+f"{creds:,}"+"""</span></div>
    <div class="card">
      <div class="fd">
        <label class="flbl">Your Text</label>
        <textarea id="tt" class="finp" rows="7" placeholder="Enter the text you want to convert to speech…" maxlength="50000" oninput="ot(this)"></textarea>
        <div class="cc" id="cc">0 / 50,000 characters</div>
      </div>
      <div class="fd">
        <label class="flbl">Speed — <span id="sv" style="color:var(--cyan);font-family:'Space Mono',monospace">1.0×</span></label>
        <input type="range" id="sp" min="0.7" max="1.2" step="0.05" value="1.0"
               oninput="document.getElementById('sv').textContent=parseFloat(this.value).toFixed(2)+'×'">
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--tx3);margin-top:4px">
          <span>0.7× Slow</span><span>1.0× Normal</span><span>1.2× Fast</span>
        </div>
      </div>
    </div>
    <button class="btn bcyan blg bfl" style="margin-top:16px" id="tb2" onclick="doTTS()">🔊 Generate Speech</button>
    <div class="wavebox" id="wb">
      <div class="bars">
        <div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div>
        <div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div>
        <div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div>
      </div>
      <div>
        <div class="wave-t">Generating Audio…</div>
        <div class="wave-m" id="wm">Initializing voice synthesis…</div>
      </div>
    </div>
    <div class="ares" id="ar">
      <div class="ares-t">
        <div class="ares-badge">Audio Ready — Saved to your R2 folder</div>
        <a id="da" href="#" download class="dl-btn">⬇ Download MP3</a>
      </div>
      <audio controls id="ap"></audio>
      <div class="a-url" id="au"></div>
    </div>
  </div>
  <div class="vpc">
    <h3>Select Voice</h3>
    <input type="text" class="vsrch" placeholder="Search voices…" oninput="fv(this.value)">
    <div class="vp-list" id="vpl">"""+vp+"""</div>
  </div>
</div>
"""+END+"""
<script>
var vid='"""+pvid+"""',vn='"""+pvn+"""',mc="""+str(creds)+""";
function pv(id,name,el){
  vid=id;vn=name;
  document.querySelectorAll('.vi').forEach(function(e){e.classList.remove('on');});
  el.classList.add('on');
  document.getElementById('svb').style.display='flex';
  document.getElementById('svn').textContent=name;
}
function cv(){vid='';vn='';document.getElementById('svb').style.display='none';document.querySelectorAll('.vi').forEach(function(e){e.classList.remove('on');});}
function fv(q){q=q.toLowerCase();document.querySelectorAll('#vpl .vi').forEach(function(e){e.style.display=e.querySelector('.vi-n').textContent.toLowerCase().includes(q)?'flex':'none';});}
function ot(el){
  var l=el.value.length;
  document.getElementById('cc').textContent=l.toLocaleString()+' / 50,000 characters';
  document.getElementById('cc').className='cc'+(l>45000?' warn':'');
  document.getElementById('tc').textContent=l.toLocaleString();
  document.getElementById('tc').style.color=l>mc?'var(--re)':'';
}
async function doTTS(){
  var text=document.getElementById('tt').value.trim();
  if(!text){toast('Enter some text first','er');return;}
  if(!vid){toast('Select a voice from the right panel','er');return;}
  if(text.length>mc){toast('Not enough credits. Need '+text.length.toLocaleString()+', have '+mc.toLocaleString(),'er');return;}
  var btn=document.getElementById('tb2');
  btn.disabled=true;btn.textContent='⏳ Generating…';
  document.getElementById('wb').classList.add('show');
  document.getElementById('ar').classList.remove('show');
  try{
    var r=await fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({input_text:text,voice_id:vid,voice_name:vn||'',speed:parseFloat(document.getElementById('sp').value)})});
    var d=await r.json();
    if(!r.ok){toast(d.error||'TTS failed','er');return;}
    mc=d.credits_left||0;
    document.getElementById('tb').textContent=mc.toLocaleString();
    await pollTTS(d.task_id);
  }catch(e){toast('Error: '+e.message,'er');}
  finally{btn.disabled=false;btn.textContent='🔊 Generate Speech';document.getElementById('wb').classList.remove('show');}
}
async function pollTTS(tid){
  var msgs=['Processing your text…','Synthesizing voice…','Applying voice model…','Saving to R2…','Finalizing audio…'];
  var mi=0;
  for(var i=0;i<30;i++){
    await sl(4000);
    document.getElementById('wm').textContent=msgs[Math.min(mi++,msgs.length-1)];
    try{
      var r=await fetch('/api/tts/result/'+tid);
      var d=await r.json();
      if(d.status==='completed'&&d.audio_url){
        document.getElementById('ap').src=d.audio_url;
        document.getElementById('da').href=d.audio_url;
        document.getElementById('da').download='nexvoice_'+Date.now()+'.mp3';
        document.getElementById('au').textContent=d.audio_url;
        document.getElementById('ar').classList.add('show');
        document.getElementById('wb').classList.remove('show');
        document.getElementById('ap').play();
        toast('✅ Audio generated successfully!','ok');
        return;
      }
      if(d.status==='failed'){toast('Generation failed. Please try again.','er');return;}
    }catch(e){}
  }
  toast('Taking longer than usual. Check history later.','in');
}
</script>"""
    return page("Text to Speech", body)

# ═══════════════════════════════════════════════════════
#  HISTORY
# ═══════════════════════════════════════════════════════
@app.route("/history")
@login_required
def history_page():
    un   = get_un()
    hist = load_hist(un)

    if not hist:
        content = '<div class="empty"><span class="empty-ic">📋</span><h3>No history yet</h3><p>Generate some speech in <a href="/tts">Text to Speech</a></p></div>'
    else:
        items = ""
        for h in hist:
            url  = h.get("audio_url","")
            text = h.get("text","")
            vn   = h.get("voice_name","?")
            ts   = h.get("ts","")
            if url:
                ael = '<div style="display:flex;gap:8px;align-items:center"><audio controls src="'+url+'" style="flex:1;height:36px;border-radius:9px;accent-color:var(--cyan)"></audio><a href="'+url+'" download class="dl-btn" style="padding:8px 12px;font-size:10px;white-space:nowrap">⬇ MP3</a></div>'
            else:
                ael = '<div style="font-size:11px;color:var(--tx3)">Processing…</div>'
            items += '<div class="hc"><div class="hc-v">'+vn+'</div><div class="hc-t">'+text+'</div><div class="hc-d">'+ts+'</div>'+ael+'</div>'
        content = '<div class="hgrid">'+items+'</div>'

    body = user_sb("/history") + main_open("TTS History") + """
<div class="ph">
  <div>
    <div class="ph-t">My TTS History</div>
    <div class="ph-s">Last """+str(MAX_HISTORY)+""" generated files — download anytime · Older files auto-deleted</div>
  </div>
</div>
"""+content+"""
"""+END
    return page("My History", body)

# ═══════════════════════════════════════════════════════
#  API ROUTES
# ═══════════════════════════════════════════════════════
@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    un=get_un()
    if "file" not in request.files: return ERR("No file")
    f=request.files["file"]
    ext=os.path.splitext(f.filename or "")[1].lower() or ".mp3"
    key=ukey(un,"uploads",str(int(time.time()))+"_"+uuid.uuid4().hex[:6]+ext)
    try:
        data=f.read(); url=r2_upload(key,data,f.content_type or "audio/mpeg")
        return J({"r2_url":url})
    except Exception as e: return ERR(str(e),500)

@app.route("/api/clone", methods=["POST"])
@login_required
def api_clone():
    un=get_un(); d=request.json or {}
    url=d.get("source_voice_file","").strip()
    nm=d.get("name","Voice").strip()
    if not url: return ERR("Audio URL required")
    u=get_user(un); err=check_access(u)
    if err: return ERR(err,403)
    vs=load_voices(un)
    if len(vs)>=MAX_CLONES: return ERR("Clone limit ("+str(MAX_CLONES)+") reached. Delete a voice first.",429)
    try:
        resp=akool("POST","/voice/clone",{"source_voice_file":url,"name":nm,
             "voice_model_name":CLONE_MODEL,"voice_options":{"remove_background_noise":True}})
    except Exception as e: return ERR(str(e),500)
    if resp.get("code")!=1000: return ERR("AI engine: "+str(resp.get("msg")))
    tid=resp["data"]["_id"]
    vs.append({"task_id":tid,"voice_id":None,"name":nm,"model":CLONE_MODEL,
               "preview":None,"gender":resp["data"].get("gender","Unknown"),
               "status":"processing","created_at":now()})
    save_voices(un,vs); write_log(un,"Voice Clone","'"+nm+"'")
    return J({"task_id":tid})

@app.route("/api/clone/status/<tid>")
@login_required
def api_clone_status(tid):
    un=get_un()
    try: resp=akool("GET","/voice/detail/"+tid)
    except Exception as e: return ERR(str(e),500)
    if resp.get("code")!=1000: return ERR(str(resp.get("msg")))
    d=resp["data"]
    status={1:"queued",2:"processing",3:"completed",4:"failed"}.get(d.get("status"),"unknown")
    if status=="completed":
        vs=load_voices(un)
        for v in vs:
            if v["task_id"]==tid:
                v.update({"voice_id":d.get("voice_id"),"preview":d.get("preview"),"status":"completed"}); break
        save_voices(un,vs)
    return J({"status":status,"voice_id":d.get("voice_id")})

@app.route("/api/clone/<tid>", methods=["DELETE"])
@login_required
def api_delete_clone(tid):
    un=get_un(); vs=load_voices(un)
    v=next((x for x in vs if x["task_id"]==tid),None)
    if not v: return ERR("Not found",404)
    try: akool("POST","/voice/del",{"_ids":[tid]})
    except: pass
    save_voices(un,[x for x in vs if x["task_id"]!=tid])
    write_log(un,"Delete Voice","'"+v["name"]+"'")
    return J({"success":True})

@app.route("/api/tts", methods=["POST"])
@login_required
def api_tts():
    un=get_un(); d=request.json or {}
    txt=d.get("input_text","").strip()
    vid=d.get("voice_id","").strip()
    vn=d.get("voice_name","")
    spd=float(d.get("speed",1.0))
    if not txt: return ERR("Text required")
    if not vid: return ERR("Voice required")
    u=get_user(un); err=check_access(u)
    if err: return ERR(err,403)
    cost=len(txt)
    if u.get("credits",0)<cost:
        return ERR("Not enough credits. Need "+str(cost)+", have "+str(u.get("credits",0)),402)
    try:
        resp=akool("POST","/voice/tts",{"input_text":txt,"voice_id":vid,
             "voice_options":{"speed":max(0.7,min(1.2,spd))}})
    except Exception as e: return ERR(str(e),500)
    if resp.get("code")!=1000: return ERR("AI engine: "+str(resp.get("msg")))
    u["credits"]=u.get("credits",0)-cost; save_user(un,u)
    tid=resp["data"]["_id"]
    add_hist(un,tid,None,txt,vn or vid[:20])
    write_log(un,"TTS",str(cost)+" chars",cost)
    return J({"task_id":tid,"credits_left":u["credits"]})

@app.route("/api/tts/result/<tid>")
@login_required
def api_tts_result(tid):
    un=get_un()
    try: resp=akool("GET","/voice/resource/detail/"+tid)
    except Exception as e: return ERR(str(e),500)
    if resp.get("code")!=1000: return ERR(str(resp.get("msg")))
    result=resp["data"].get("result") or resp["data"]
    status={1:"queued",2:"processing",3:"completed",4:"failed"}.get(result.get("status"),"unknown")
    a_url=result.get("preview"); r2_url=None
    if status=="completed" and a_url:
        try:
            audio=rq.get(a_url,timeout=30).content
            key=ukey(un,"tts",tid+".mp3")
            r2_url=r2_upload(key,audio,"audio/mpeg")
            hist=load_hist(un)
            for h in hist:
                if h.get("task_id")==tid: h["audio_url"]=r2_url; break
            save_hist(un,hist)
        except Exception as e: print("[TTS R2]",e); r2_url=a_url
    return J({"status":status,"audio_url":r2_url or a_url})

# ═══════════════════════════════════════════════════════
#  ADMIN AUTH
# ═══════════════════════════════════════════════════════
@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    err=""
    if request.method=="POST":
        u=request.form.get("u",""); p=request.form.get("p","")
        if u==ADMIN_USER and p==ADMIN_PASS:
            session["is_admin"]=True; return redirect("/admin")
        err="Wrong credentials"
    al = ('<div class="alert-er" style="margin-bottom:18px">'+err+'</div>') if err else ""
    body="""
<div class="auth-page">
  <div class="auth-bg"></div>
  <div class="abox" style="border-color:rgba(255,51,102,.2);box-shadow:0 24px 80px rgba(0,0,0,.5),0 0 0 1px rgba(255,51,102,.08)">
    <div class="abox-logo-wrap">
      <svg class="abox-logo" viewBox="0 0 72 72" fill="none">
        <defs><linearGradient id="alg2" x1="0" y1="0" x2="72" y2="72" gradientUnits="userSpaceOnUse">
          <stop stop-color="#ff3366"/><stop offset="1" stop-color="#ff6b35"/>
        </linearGradient></defs>
        <circle cx="36" cy="36" r="34" stroke="url(#alg2)" stroke-width="1.5" stroke-dasharray="6 3" style="animation:spin 12s linear infinite;transform-origin:36px 36px"/>
        <circle cx="36" cy="36" r="20" fill="rgba(255,51,102,0.08)" stroke="rgba(255,51,102,0.25)" stroke-width="1"/>
        <path d="M26 36 L36 26 L46 36 L36 46 Z" fill="url(#alg2)" opacity="0.9"/>
      </svg>
    </div>
    <h2 style="background:linear-gradient(135deg,var(--tx) 30%,var(--re));-webkit-background-clip:text;-webkit-text-fill-color:transparent">Admin Console</h2>
    <p>NexVoice Administration Panel</p>
    """+al+"""
    <form method="POST">
      <div class="fd"><label class="flbl">Username</label>
        <input type="text" name="u" class="finp" style="text-align:center" required autofocus></div>
      <div class="fd"><label class="flbl">Password</label>
        <input type="password" name="p" class="finp" style="text-align:center" required></div>
      <button type="submit" class="abox-btn" style="background:linear-gradient(135deg,#ff3366,#ff6b35);box-shadow:0 6px 28px rgba(255,51,102,.4)">Enter Console →</button>
    </form>
  </div>
</div>"""
    return page("Admin Login", body)

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin",None); return redirect("/admin/login")

# ═══════════════════════════════════════════════════════
#  ADMIN DASHBOARD
# ═══════════════════════════════════════════════════════
@app.route("/admin")
@admin_required
def admin_dash():
    users=load_users()
    tcr=sum(u.get("credits",0) for u in users.values())
    tcl=sum(len(load_voices(un)) for un in users)
    tts=sum(len(load_hist(un)) for un in users)
    ban=sum(1 for u in users.values() if u.get("banned"))
    lg=r2_read(K_LOG,[])[:12]

    lrows_list=[]
    for r in lg:
        cst=("−"+str(r["cost"])) if r.get("cost") else "—"
        lrows_list.append("<tr><td class='nm'>"+r.get("user","")+"</td><td>"+r.get("action","")+"</td><td style='color:var(--tx2)'>"+r.get("detail","")[:40]+"</td><td style='color:var(--ye)'>"+cst+"</td><td style='font-size:11px;color:var(--tx3)'>"+r.get("time","")+"</td></tr>")
    lrows="".join(lrows_list) or "<tr><td colspan='5' style='text-align:center;color:var(--tx3);padding:16px'>No activity yet</td></tr>"

    urows_list=[]
    for un,u in list(users.items())[:8]:
        bc="ba"; blt="ACTIVE"
        if u.get("banned"): bc="bb"; blt="BANNED"
        elif is_expired(u): bc="be"; blt="EXPIRED"
        urows_list.append("<tr><td class='nm'>"+un+"</td><td><span class='crn'>"+f"{u.get('credits',0):,}"+"</span></td><td>"+str(len(load_voices(un)))+"/10</td><td><span class='badge "+bc+"'>"+blt+"</span></td><td><a href='/admin/users/"+un+"' class='btn bgh bsm'>Manage</a></td></tr>")
    urows="".join(urows_list) or "<tr><td colspan='5' style='text-align:center;color:var(--tx3);padding:16px'>No users</td></tr>"

    body=admin_sb("/admin")+admin_open("Dashboard")+"""
<div class="stats">
  <div class="sc c1"><div class="sc-l">Total Users</div><div class="sc-v" style="color:var(--cyan)">"""+str(len(users))+"""</div></div>
  <div class="sc c2"><div class="sc-l">Total Credits</div><div class="sc-v" style="color:var(--gr)">"""+f"{tcr:,}"+"""</div></div>
  <div class="sc c3"><div class="sc-l">Total Clones</div><div class="sc-v" style="color:var(--ye)">"""+str(tcl)+"""</div></div>
  <div class="sc c4"><div class="sc-l">Banned</div><div class="sc-v" style="color:var(--re)">"""+str(ban)+"""</div></div>
</div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
  <div class="card">
    <div class="card-t">📋 Recent Activity</div>
    <table class="tbl"><thead><tr><th>User</th><th>Action</th><th>Detail</th><th>Credits</th><th>Time</th></tr></thead>
    <tbody>"""+lrows+"""</tbody></table>
  </div>
  <div class="card">
    <div class="card-t" style="display:flex;align-items:center;justify-content:space-between">
      <span>👥 Users</span>
      <a href="/admin/users" style="font-size:11px;color:var(--cyan);text-decoration:none">View all →</a>
    </div>
    <table class="tbl"><thead><tr><th>Username</th><th>Credits</th><th>Clones</th><th>Status</th><th></th></tr></thead>
    <tbody>"""+urows+"""</tbody></table>
  </div>
</div>
"""+END
    return page("Admin Dashboard", body)

# ═══════════════════════════════════════════════════════
#  ADMIN ALL USERS
# ═══════════════════════════════════════════════════════
@app.route("/admin/users")
@admin_required
def admin_users():
    users=load_users()
    q=request.args.get("q","").lower()
    flt=request.args.get("f","all")
    rows=""
    for un,u in users.items():
        if q and q not in un.lower(): continue
        s=user_status(u)
        if flt!="all" and flt!=s: continue
        bc="ba"; blt="ACTIVE"
        if s=="banned": bc="bb"; blt="BANNED"
        elif s=="expired": bc="be"; blt="EXPIRED"
        exp=u.get("expires_at","—") or "—"
        rows+="<tr><td class='nm'>"+un+"</td><td><span class='crn'>"+f"{u.get('credits',0):,}"+"</span></td><td>"+str(len(load_voices(un)))+"/10</td><td><span class='badge "+bc+"'>"+blt+"</span></td><td>"+exp+"</td><td><span class='r2p'>users/"+un+"/</span></td><td><a href='/admin/users/"+un+"' class='btn bgh bsm'>👁 Manage</a></td></tr>"

    body=admin_sb("/admin/users")+admin_open("All Users")+"""
<div class="srow">
  <form method="GET" style="display:flex;gap:9px;flex:1;flex-wrap:wrap">
    <input type="text" name="q" class="sbr" placeholder="Search username…" value=\""""+q+"""\">
    <select name="f" class="sbr" style="max-width:130px" onchange="this.form.submit()">
      <option value="all" """+("selected" if flt=="all" else "")+""">All</option>
      <option value="active" """+("selected" if flt=="active" else "")+""">Active</option>
      <option value="banned" """+("selected" if flt=="banned" else "")+""">Banned</option>
      <option value="expired" """+("selected" if flt=="expired" else "")+""">Expired</option>
    </select>
    <button type="submit" class="btn bcyan bmd">Search</button>
  </form>
  <a href="/admin/create" class="btn bgr bmd">➕ Create User</a>
</div>
<div class="card">
  <table class="tbl">
    <thead><tr><th>Username</th><th>Credits</th><th>Clones</th><th>Status</th><th>Expires</th><th>R2 Folder</th><th>Actions</th></tr></thead>
    <tbody>"""+( rows or "<tr><td colspan='7' style='text-align:center;color:var(--tx3);padding:20px'>No users found</td></tr>")+"""</tbody>
  </table>
</div>
"""+END
    return page("All Users", body)

# ═══════════════════════════════════════════════════════
#  ADMIN USER DETAIL
# ═══════════════════════════════════════════════════════
@app.route("/admin/users/<username>", methods=["GET","POST"])
@admin_required
def admin_user_detail(username):
    users=load_users(); user=users.get(username)
    msg=""; mok=False

    if not user:
        body=admin_sb("/admin/users")+admin_open("User Not Found")+'<div class="alert-er">User not found.</div><a href="/admin/users" class="btn bgh bmd">← Back</a>'+END
        return page("Not Found",body)

    if request.method=="POST":
        act=request.form.get("action","")
        if act=="add_cr":
            a=int(request.form.get("amount",0) or 0); user["credits"]=user.get("credits",0)+a; save_user(username,user); write_log(username,"Admin +credits","+"+str(a)); msg="Added "+f"{a:,}"+" credits"; mok=True
        elif act=="set_cr":
            a=int(request.form.get("amount",0) or 0); user["credits"]=a; save_user(username,user); write_log(username,"Admin set credits","="+str(a)); msg="Credits set to "+f"{a:,}"; mok=True
        elif act=="rm_cr":
            a=int(request.form.get("amount",0) or 0); user["credits"]=max(0,user.get("credits",0)-a); save_user(username,user); write_log(username,"Admin -credits","-"+str(a)); msg="Removed "+f"{a:,}"+" credits"; mok=True
        elif act=="ban":
            r=request.form.get("reason","No reason").strip(); user["banned"]=True; user["ban_reason"]=r; save_user(username,user); write_log(username,"Admin: BANNED",r); msg="User banned: "+r; mok=True
        elif act=="unban":
            user["banned"]=False; user["ban_reason"]=""; save_user(username,user); write_log(username,"Admin: UNBANNED"); msg="User unbanned"; mok=True
        elif act=="set_exp":
            e=request.form.get("exp_date","").strip(); user["expires_at"]=e or None; save_user(username,user); write_log(username,"Admin: set expiry",e or "removed"); msg="Expiry: "+(e or "Never"); mok=True
        elif act=="rm_exp":
            user.pop("expires_at",None); save_user(username,user); write_log(username,"Admin: removed expiry"); msg="Expiry removed"; mok=True
        elif act=="chpw":
            pw=request.form.get("new_pw","").strip()
            if pw: user["password"]=hp(pw); save_user(username,user); write_log(username,"Admin: chpw"); msg="Password changed"; mok=True
            else: msg="Password cannot be empty"
        elif act=="delete":
            del users[username]; save_users(users); write_log(username,"Admin: USER DELETED"); return redirect("/admin/users")
        user=get_user(username) or user

    s=user_status(user)
    vs=load_voices(username); hs=load_hist(username)
    al_cls="alert-ok" if mok else "alert-er"
    al=('<div class="'+al_cls+'">'+msg+'</div>') if msg else ""
    s_bc="ba"; s_blt="ACTIVE"
    if s=="banned": s_bc="bb"; s_blt="BANNED"
    elif s=="expired": s_bc="be"; s_blt="EXPIRED"
    exp_clr="var(--re)" if s=="expired" else "var(--gr)"
    exp_val=user.get("expires_at") or "Never"
    cur_exp=user.get("expires_at","") or ""

    if s=="banned":
        ban_html='<form method="POST"><button name="action" value="unban" class="btn bgr bmd">✅ Unban User</button></form>'
    else:
        ban_html='<form method="POST"><div class="fd" style="margin-bottom:8px"><label class="flbl">Ban Reason</label><input type="text" name="reason" class="finp" placeholder="reason for ban"></div><button name="action" value="ban" class="btn bdr bmd">🚫 Ban User</button></form>'

    vrows_list=[]
    for v in vs:
        sc="var(--gr)" if v.get("status")=="completed" else "var(--ye)"
        vrows_list.append("<tr><td class='nm'>"+v.get("name","")+"</td><td>"+v.get("gender","?")+"</td><td style='color:"+sc+"'>"+v.get("status","?")+"</td><td style='font-size:11px;color:var(--tx3)'>"+v.get("created_at","")+"</td></tr>")
    vrows="".join(vrows_list) or "<tr><td colspan='4' style='text-align:center;color:var(--tx3);padding:12px'>No voices</td></tr>"

    hrows_list=[]
    for h in hs:
        txt=h.get("text","")
        t50=txt[:50]+("…" if len(txt)>50 else "")
        aurl=h.get("audio_url","")
        play=('<a href="'+aurl+'" target="_blank" class="btn bgh bsm">▶</a>') if aurl else "—"
        hrows_list.append("<tr><td style='color:var(--cyan);font-weight:600'>"+h.get("voice_name","?")+"</td><td style='color:var(--tx2)'>"+t50+"</td><td style='font-size:11px;color:var(--tx3)'>"+h.get("ts","")+"</td><td>"+play+"</td></tr>")
    hrows="".join(hrows_list) or "<tr><td colspan='4' style='text-align:center;color:var(--tx3);padding:12px'>No history</td></tr>"

    body=admin_sb("/admin/users")+admin_open("User: "+username)+"""
"""+al+"""
<div style="display:flex;align-items:center;gap:14px;margin-bottom:28px;flex-wrap:wrap">
  <a href="/admin/users" class="btn bgh bsm">← Back</a>
  <div style="width:48px;height:48px;background:linear-gradient(135deg,var(--cyan2),var(--pu));border-radius:13px;display:flex;align-items:center;justify-content:center;font-family:'Space Mono',monospace;font-size:20px;font-weight:700;color:#fff;box-shadow:0 0 16px rgba(0,212,255,.3)">"""+username[0].upper()+"""</div>
  <div>
    <div style="font-size:20px;font-weight:700;letter-spacing:-.3px">"""+username+"""</div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:5px">
      <span class="badge """+s_bc+"""">"""+s_blt+"""</span>
      <span class="r2p">users/"""+username+"""/</span>
      <span style="font-size:11px;color:var(--tx3)">Joined """+user.get("created_at","?")+"""</span>
    </div>
  </div>
  <div style="margin-left:auto;text-align:right">
    <div style="font-family:'Space Mono',monospace;font-size:28px;font-weight:700;color:var(--cyan);text-shadow:0 0 20px rgba(0,212,255,.4)">"""+f"{user.get('credits',0):,}"+"""</div>
    <div style="font-size:11px;color:var(--tx3)">credits</div>
  </div>
</div>

<div class="two-col">
  <div class="card">
    <div class="card-t">💰 Credits</div>
    <form method="POST">
      <div class="fd"><label class="flbl">Amount</label>
        <input type="number" name="amount" class="finp" placeholder="e.g. 1000" min="0"></div>
      <div class="ifrow" style="margin-bottom:10px">
        <button name="action" value="add_cr" class="btn bgr bsm">+ Add</button>
        <button name="action" value="set_cr" class="btn bye bsm">= Set</button>
        <button name="action" value="rm_cr"  class="btn bdr bsm">– Remove</button>
      </div>
      <div class="ifrow">
        <button name="action" value="set_cr" onclick="this.form.querySelector('[name=amount]').value=0" class="btn bdr bsm">🔴 Zero</button>
        <button name="action" value="set_cr" onclick="this.form.querySelector('[name=amount]').value=999999" class="btn bye bsm">♾️ Unlimited</button>
      </div>
    </form>
  </div>
  <div class="card">
    <div class="card-t">🔑 Account</div>
    <form method="POST" style="margin-bottom:14px">
      <div class="fd"><label class="flbl">New Password</label>
        <input type="password" name="new_pw" class="finp" placeholder="new password"></div>
      <button name="action" value="chpw" class="btn bpu bsm">Change Password</button>
    </form>
    <hr style="border:none;border-top:1px solid var(--b1);margin:14px 0">
    """+ban_html+"""
  </div>
  <div class="card">
    <div class="card-t">📅 Account Expiry</div>
    <div style="font-size:13px;color:var(--tx2);margin-bottom:14px">
      Current: <strong style="color:"""+exp_clr+"""">"""+exp_val+"""</strong>
    </div>
    <form method="POST">
      <div class="fd"><label class="flbl">Set Expiry Date</label>
        <input type="date" name="exp_date" id="ed" class="finp" value=\""""+cur_exp+"""\"></div>
      <div style="display:flex;gap:7px;flex-wrap:wrap;margin-bottom:10px">
        <button type="button" class="btn bor bsm" onclick="addD(7)">+7 Days</button>
        <button type="button" class="btn bor bsm" onclick="addD(30)">+30 Days</button>
        <button type="button" class="btn bor bsm" onclick="addD(90)">+90 Days</button>
        <button type="button" class="btn bor bsm" onclick="addD(365)">+1 Year</button>
      </div>
      <div style="display:flex;gap:7px">
        <button name="action" value="set_exp" class="btn bye bsm">Set Date</button>
        <button name="action" value="rm_exp"  class="btn bgh bsm">Never Expire</button>
      </div>
    </form>
  </div>
  <div class="card" style="border-color:rgba(255,51,102,.2)">
    <div class="card-t" style="color:var(--re)">⚠️ Danger Zone</div>
    <div style="font-size:13px;color:var(--tx2);margin-bottom:16px">Permanently delete this user and all their data.</div>
    <form method="POST" onsubmit="return confirm('DELETE """+username+""" permanently?')">
      <button name="action" value="delete" class="btn bdr bmd">🗑 Delete User</button>
    </form>
  </div>
</div>

<div class="card">
  <div class="card-t">🎤 Cloned Voices ("""+str(len(vs))+"""/"""+str(MAX_CLONES)+""")</div>
  <table class="tbl"><thead><tr><th>Name</th><th>Gender</th><th>Status</th><th>Created</th></tr></thead>
  <tbody>"""+vrows+"""</tbody></table>
</div>
<div class="card">
  <div class="card-t">📋 TTS History (last """+str(MAX_HISTORY)+""")</div>
  <table class="tbl"><thead><tr><th>Voice</th><th>Text</th><th>Time</th><th>Audio</th></tr></thead>
  <tbody>"""+hrows+"""</tbody></table>
</div>
"""+END+"""
<script>function addD(n){var d=new Date();d.setDate(d.getDate()+n);document.getElementById('ed').value=d.toISOString().slice(0,10);}</script>"""
    return page("User: "+username, body)

# ═══════════════════════════════════════════════════════
#  ADMIN CREATE USER
# ═══════════════════════════════════════════════════════
@app.route("/admin/create", methods=["GET","POST"])
@admin_required
def admin_create():
    msg=""; mok=False; r2info=""
    if request.method=="POST":
        un=request.form.get("username","").strip().lower()
        pw=request.form.get("password","").strip()
        cr=int(request.form.get("credits",0) or 0)
        exp=request.form.get("exp_date","").strip() or None
        if not un or not pw: msg="Username and password required"
        elif len(un)<3: msg="Username must be at least 3 characters"
        elif get_user(un): msg="Username '"+un+"' already exists"
        else:
            users=load_users()
            users[un]={"password":hp(pw),"credits":cr,"created_at":now(),"expires_at":exp,"banned":False}
            save_users(users)
            r2_write(ukey(un,"profile.json"),{"username":un,"created_at":now()})
            r2_write(ukey(un,"voices.json"),[])
            r2_write(ukey(un,"history.json"),[])
            write_log(un,"User Created","credits="+str(cr)+", expires="+(exp or "never"))
            msg="✅ User '"+un+"' created successfully!"; mok=True
            r2info='<div class="alert-ok">R2 folder created: <strong style="font-family:\'Space Mono\',monospace">users/'+un+'/</strong> — profile.json, voices.json, history.json initialized</div>'
    al_cls2="alert-ok" if mok else "alert-er"
    al=('<div class="'+al_cls2+'">'+msg+'</div>') if msg else ""

    body=admin_sb("/admin/create")+admin_open("Create User")+"""
"""+al+r2info+"""
<div class="two-col">
  <div class="card">
    <div class="card-t">➕ New User Account</div>
    <form method="POST">
      <div class="fd"><label class="flbl">Username *</label>
        <input type="text" name="username" class="finp" placeholder="e.g. john_doe (min 3 chars)" required></div>
      <div class="fd"><label class="flbl">Password *</label>
        <input type="password" name="password" class="finp" placeholder="user's password" required></div>
      <div class="fd"><label class="flbl">Starting Credits</label>
        <input type="number" name="credits" class="finp" value="100" min="0"></div>
      <div class="fd"><label class="flbl">Account Expiry (optional)</label>
        <input type="date" name="exp_date" id="ed2" class="finp">
        <div style="display:flex;gap:7px;margin-top:8px;flex-wrap:wrap">
          <button type="button" class="btn bor bsm" onclick="addD2(7)">+7 Days</button>
          <button type="button" class="btn bor bsm" onclick="addD2(30)">+1 Month</button>
          <button type="button" class="btn bor bsm" onclick="addD2(90)">+3 Months</button>
          <button type="button" class="btn bor bsm" onclick="addD2(365)">+1 Year</button>
          <button type="button" class="btn bgh bsm" onclick="document.getElementById('ed2').value=''">Never</button>
        </div>
      </div>
      <button type="submit" class="btn bgr blg bfl">✅ Create User &amp; R2 Folder</button>
    </form>
  </div>
  <div class="card">
    <div class="card-t">📁 Per-User R2 Storage</div>
    <div class="code-box">
      <div style="color:var(--cyan)">users/{username}/</div>
      <div style="padding-left:18px;color:var(--gr)">├─ profile.json</div>
      <div style="padding-left:18px;color:var(--gr)">├─ voices.json</div>
      <div style="padding-left:18px;color:var(--gr)">├─ history.json</div>
      <div style="padding-left:18px;color:var(--ye)">├─ audio/ (source files)</div>
      <div style="padding-left:18px;color:var(--ye)">└─ tts/ (generated mp3)</div>
    </div>
    <div style="margin-top:16px;display:flex;flex-direction:column;gap:10px;font-size:13px;color:var(--tx2)">
      <div style="display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--b1)"><span>1 credit =</span><strong style="color:var(--cyan);font-family:'Space Mono',monospace">1 TTS character</strong></div>
      <div style="display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--b1)"><span>Voice cloning =</span><strong style="color:var(--gr)">FREE</strong></div>
      <div style="display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--b1)"><span>History kept =</span><strong style="color:var(--ye)">Last """+str(MAX_HISTORY)+"""</strong></div>
      <div style="display:flex;justify-content:space-between;padding:9px 0"><span>Max clones =</span><strong style="color:var(--cyan)">"""+str(MAX_CLONES)+"""</strong></div>
    </div>
  </div>
</div>
"""+END+"""
<script>function addD2(n){var d=new Date();d.setDate(d.getDate()+n);document.getElementById('ed2').value=d.toISOString().slice(0,10);}</script>"""
    return page("Create User", body)

# ═══════════════════════════════════════════════════════
#  ADMIN ALL VOICES
# ═══════════════════════════════════════════════════════
@app.route("/admin/voices")
@admin_required
def admin_voices():
    users=load_users(); rows=""; total=0
    for un in users:
        for v in load_voices(un):
            total+=1
            sc="var(--gr)" if v.get("status")=="completed" else "var(--ye)"
            rows+="<tr><td class='nm'>"+un+"</td><td>"+v.get("name","")+"</td><td>"+v.get("gender","?")+"</td><td style='color:"+sc+"'>"+v.get("status","?")+"</td><td><span class='r2p'>users/"+un+"/</span></td><td style='font-size:11px;color:var(--tx3)'>"+v.get("created_at","")+"</td></tr>"
    body=admin_sb("/admin/voices")+admin_open("All Voices")+"""
<div class="card">
  <div style="font-size:13px;color:var(--tx2);margin-bottom:16px">
    Total: <strong style="color:var(--cyan);font-family:'Space Mono',monospace">"""+str(total)+"""</strong> voices across all users
  </div>
  <table class="tbl">
    <thead><tr><th>User</th><th>Voice Name</th><th>Gender</th><th>Status</th><th>R2 Folder</th><th>Created</th></tr></thead>
    <tbody>"""+( rows or "<tr><td colspan='6' style='text-align:center;color:var(--tx3);padding:20px'>No voices yet</td></tr>")+"""</tbody>
  </table>
</div>
"""+END
    return page("All Voices", body)

# ═══════════════════════════════════════════════════════
#  ADMIN ACTIVITY LOG
# ═══════════════════════════════════════════════════════
@app.route("/admin/log")
@admin_required
def admin_log():
    lg=r2_read(K_LOG,[])
    p=int(request.args.get("p",1)); pp=50; total=len(lg)
    items=lg[(p-1)*pp:p*pp]
    log_rows=[]
    for r in items:
        cst=("−"+str(r["cost"])) if r.get("cost") else "—"
        log_rows.append("<tr><td class='nm'>"+r.get("user","")+"</td><td>"+r.get("action","")+"</td><td style='color:var(--tx2)'>"+r.get("detail","")[:50]+"</td><td style='color:var(--ye)'>"+cst+"</td><td style='font-size:11px;color:var(--tx3)'>"+r.get("time","")+"</td></tr>")
    rows="".join(log_rows) or "<tr><td colspan='5' style='text-align:center;color:var(--tx3);padding:20px'>No activity</td></tr>"
    mp=max(1,(total+pp-1)//pp)
    prev_link=('<a href="?p='+str(p-1)+'" class="btn bgh bsm">← Prev</a>') if p>1 else ""
    next_link=('<a href="?p='+str(p+1)+'" class="btn bgh bsm">Next →</a>') if p*pp<total else ""
    pg='<div class="pager">'+prev_link+'<span>Page '+str(p)+' of '+str(mp)+' &nbsp;('+f"{total:,}"+' entries)</span>'+next_link+'</div>'

    body=admin_sb("/admin/log")+admin_open("Activity Log")+"""
<div class="card">
  <table class="tbl">
    <thead><tr><th>User</th><th>Action</th><th>Detail</th><th>Credits</th><th>Time</th></tr></thead>
    <tbody>"""+rows+"""</tbody>
  </table>
  """+pg+"""
</div>
"""+END
    return page("Activity Log", body)


# ═══════════════════════════════════════════════════════
#  ADMIN SHARED LIBRARY  —  clone voices for ALL users
# ═══════════════════════════════════════════════════════
@app.route("/admin/library", methods=["GET","POST"])
@admin_required
def admin_library():
    msg=""; mok=False
    shared = load_shared_voices()

    if request.method=="POST":
        act = request.form.get("action","")

        if act=="clone":
            audio_url = request.form.get("audio_url","").strip()
            nm        = request.form.get("name","Shared Voice").strip()
            if not audio_url:
                msg="Audio URL required"
            else:
                try:
                    resp = akool("POST","/voice/clone",{
                        "source_voice_file": audio_url,
                        "name": nm,
                        "voice_model_name": CLONE_MODEL,
                        "voice_options": {"remove_background_noise": True}
                    })
                    if resp.get("code")!=1000:
                        msg="AI engine error: "+str(resp.get("msg"))
                    else:
                        tid = resp["data"]["_id"]
                        shared.append({
                            "task_id":   tid,
                            "voice_id":  None,
                            "name":      nm,
                            "preview":   None,
                            "gender":    resp["data"].get("gender","Unknown"),
                            "status":    "processing",
                            "created_at": now()
                        })
                        save_shared_voices(shared)
                        write_log("admin","Shared Clone","'"+nm+"'")
                        msg="✅ Cloning '"+nm+"' — check status in ~1 minute"; mok=True
                except Exception as e:
                    msg="Error: "+str(e)

        elif act=="check":
            updated=0
            for v in shared:
                if v.get("status")=="processing" and v.get("task_id"):
                    try:
                        r=akool("GET","/voice/detail/"+v["task_id"])
                        if r.get("code")==1000:
                            d=r["data"]
                            st={1:"queued",2:"processing",3:"completed",4:"failed"}.get(d.get("status"),"unknown")
                            if st=="completed":
                                v["voice_id"]=d.get("voice_id")
                                v["preview"]=d.get("preview")
                                v["status"]="completed"
                                updated+=1
                            elif st=="failed":
                                v["status"]="failed"
                    except: pass
            save_shared_voices(shared)
            msg="Checked — "+str(updated)+" voice(s) completed"; mok=True

        elif act=="delete":
            tid=request.form.get("tid","")
            v=next((x for x in shared if x.get("task_id")==tid),None)
            if v:
                try: akool("POST","/voice/del",{"_ids":[tid]})
                except: pass
                save_shared_voices([x for x in shared if x.get("task_id")!=tid])
                msg="✅ Shared voice deleted"; mok=True
            else:
                msg="Voice not found"
        shared = load_shared_voices()

    al_cls="alert-ok" if mok else "alert-er"
    al=('<div class="'+al_cls+'">'+msg+'</div>') if msg else ""

    rows=""
    for v in shared:
        sc="var(--gr)" if v.get("status")=="completed" else ("var(--re)" if v.get("status")=="failed" else "var(--ye)")
        blt="✅ Ready" if v.get("status")=="completed" else ("❌ Failed" if v.get("status")=="failed" else "⏳ Processing")
        prev = v.get("preview","")
        pbt  = ('<a href="'+prev+'" target="_blank" class="btn bgh bsm">▶ Play</a>') if prev else ""
        dbt  = '''<form method="POST" style="display:inline"><input type="hidden" name="action" value="delete"><input type="hidden" name="tid" value="'''+v.get("task_id","")+'''"><button type="submit" class="btn bdr bsm">🗑</button></form>'''
        rows+="<tr><td class='nm'>"+v.get("name","")+"</td><td>"+v.get("gender","?")+"</td><td style='color:"+sc+"'>"+blt+"</td><td style='font-size:11px;color:var(--tx3)'>"+v.get("created_at","")+"</td><td style='display:flex;gap:6px'>"+pbt+dbt+"</td></tr>"

    body=admin_sb("/admin/library")+admin_open("Shared Voice Library")+"""
"""+al+"""
<div class="ph">
  <div>
    <div class="ph-t">Shared Voice Library</div>
    <div class="ph-s">Clone voices here — they become available to ALL users in their Voice Picker and TTS page (shown with ✦)</div>
  </div>
</div>
<div class="two-col">
  <div class="card">
    <div class="card-t">✨ Add Shared Voice</div>
    <form method="POST">
      <input type="hidden" name="action" value="clone">
      <div class="fd">
        <label class="flbl">Audio URL *</label>
        <input type="text" name="audio_url" class="finp" placeholder="https://... direct audio file URL" required>
        <div style="font-size:11px;color:var(--tx3);margin-top:6px">Must be a direct link to an MP3/WAV/MP4 file (publicly accessible)</div>
      </div>
      <div class="fd">
        <label class="flbl">Voice Name *</label>
        <input type="text" name="name" class="finp" placeholder="e.g. Professional Male, News Anchor..." required>
      </div>
      <button type="submit" class="btn bcyan blg bfl">✨ Clone as Shared Voice</button>
    </form>
  </div>
  <div class="card">
    <div class="card-t">ℹ️ How Shared Voices Work</div>
    <div style="font-size:13px;color:var(--tx2);line-height:1.8">
      <div style="padding:8px 0;border-bottom:1px solid var(--b1)">🌐 Cloned here → <strong style="color:var(--cyan)">Available to ALL users</strong></div>
      <div style="padding:8px 0;border-bottom:1px solid var(--b1)">✦ Shown with ✦ symbol in user voice pickers</div>
      <div style="padding:8px 0;border-bottom:1px solid var(--b1)">👤 User own clones shown as <strong>CLONE</strong></div>
      <div style="padding:8px 0;border-bottom:1px solid var(--b1)">⏳ Takes ~1 minute per voice to process</div>
      <div style="padding:8px 0">📊 Total shared: <strong style="color:var(--cyan)">"""+str(len(shared))+"""</strong> voices</div>
    </div>
    <form method="POST" style="margin-top:14px">
      <input type="hidden" name="action" value="check">
      <button type="submit" class="btn bgh bmd">🔄 Refresh Processing Status</button>
    </form>
  </div>
</div>
<div class="card">
  <div class="card-t">🌐 Shared Voices ("""+str(len(shared))+""" total)</div>
  <table class="tbl">
    <thead><tr><th>Name</th><th>Gender</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead>
    <tbody>"""+( rows or "<tr><td colspan='5' style='text-align:center;color:var(--tx3);padding:20px'>No shared voices yet — add one above</td></tr>")+"""</tbody>
  </table>
</div>
"""+END
    return page("Shared Library", body)

# ═══════════════════════════════════════════════════════
#  WSGI ENTRY POINT
# ═══════════════════════════════════════════════════════
application = app

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════╗
║  NexVoice  —  AI Voice Platform                      ║
║  http://localhost:5000           User App            ║
║  http://localhost:5000/admin     Admin Console       ║
╚══════════════════════════════════════════════════════╝
""")
    app.run(debug=True, port=5000, host="0.0.0.0")