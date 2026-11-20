"""Dark gaming theme (black + red) as plain CSS/HTML helpers. No Streamlit import, so it is testable."""
from __future__ import annotations

from html import escape

CSS = """
<style>
:root{--red:#E10600;--red2:#ff3b30;--panel:#0c0c0f;--line:#26262b;--muted:#9a9aa2;}
.stApp{background:radial-gradient(1100px 480px at 50% -8%, rgba(225,6,0,.20), transparent 62%), #000;}
[data-testid="stHeader"]{background:transparent;}
.block-container{padding-top:1.2rem;padding-bottom:4rem;max-width:1180px;}
section[data-testid="stSidebar"]{background:#060608;border-right:1px solid var(--line);}

/* brand header */
.brand{display:flex;align-items:center;gap:16px;padding:18px 22px;margin-bottom:18px;
  border:1px solid var(--line);border-left:4px solid var(--red);border-radius:14px;
  background:linear-gradient(90deg,rgba(225,6,0,.16),rgba(12,12,15,.92) 58%);}
.brand .logo{flex:0 0 auto;width:46px;height:46px;border-radius:12px;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(180deg,#ff1a12,#a50400);color:#fff;font-weight:900;font-size:1.05rem;letter-spacing:.04em;
  box-shadow:0 0 22px rgba(225,6,0,.45);}
.brand h1{margin:0;padding:0;font-size:2.1rem;font-weight:900;letter-spacing:.09em;line-height:1.1;color:#fff;}
.brand h1 span{color:var(--red);}
.brand p{margin:4px 0 0;color:var(--muted);font-size:.92rem;}

/* dashboard cards */
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:4px 0 6px;}
.stat{position:relative;overflow:hidden;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px 12px;}
.stat:before{content:"";position:absolute;left:0;top:0;width:100%;height:3px;background:linear-gradient(90deg,var(--red),transparent);}
.stat .k{color:var(--muted);font-size:.7rem;letter-spacing:.16em;text-transform:uppercase;}
.stat .v{font-size:1.9rem;font-weight:800;margin-top:6px;line-height:1.1;color:#fff;}
.stat .v small{font-size:1rem;color:var(--muted);font-weight:600;}
.stat .s{color:var(--muted);font-size:.78rem;margin-top:3px;}
.pill{display:inline-block;padding:4px 12px;border-radius:999px;font-size:.82rem;font-weight:800;letter-spacing:.12em;}
.pill.idle{background:#16161a;color:#c9c9d1;border:1px solid #33333a;}
.pill.busy{background:rgba(225,6,0,.16);color:#ff5a53;border:1px solid var(--red);animation:pulse 1.2s infinite;}
.pill.ok{background:rgba(0,200,83,.12);color:#2ee57d;border:1px solid #1e9e57;}
.pill.err{background:rgba(255,0,0,.2);color:#ff6b66;border:1px solid var(--red2);}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(225,6,0,.55);}50%{box-shadow:0 0 0 8px rgba(225,6,0,0);}}

/* section headings */
.sec{display:flex;align-items:center;gap:10px;margin:28px 0 8px;font-weight:800;letter-spacing:.15em;font-size:.95rem;text-transform:uppercase;color:#fff;}
.sec b{color:var(--red);}
.sec:after{content:"";flex:1;height:1px;background:linear-gradient(90deg,var(--line),transparent);}

/* containers, inputs, buttons */
[data-testid="stVerticalBlockBorderWrapper"]{border-radius:14px;}
.stTextInput input,.stNumberInput input,.stTextArea textarea{background:#08080a!important;border:1px solid #2a2a30!important;}
.stTextInput input:focus,.stNumberInput input:focus{border-color:var(--red)!important;box-shadow:0 0 0 1px var(--red)!important;}
.stButton>button,.stDownloadButton>button{width:100%;border-radius:12px;border:1px solid #3a3a42;background:#101014;color:#fff;font-weight:700;}
.stButton>button:hover,.stDownloadButton>button:hover{border-color:var(--red);color:#fff;}
.stButton>button[kind="primary"],button[data-testid="stBaseButton-primary"]{
  background:linear-gradient(180deg,#ff1a12,#c40500)!important;border:1px solid var(--red2)!important;color:#fff!important;
  font-weight:900!important;letter-spacing:.14em!important;padding:.75rem 1rem!important;box-shadow:0 0 24px rgba(225,6,0,.35);}
.stButton>button[kind="primary"]:hover,button[data-testid="stBaseButton-primary"]:hover{filter:brightness(1.12);}
[data-testid="stFileUploaderDropzone"]{background:#08080a;border:1px dashed #3a3a42;border-radius:12px;}
[data-testid="stFileUploaderDropzone"]:hover{border-color:var(--red);}
video{border-radius:12px;border:1px solid var(--line);}
.clip-title{font-weight:800;letter-spacing:.08em;font-size:.85rem;margin:6px 0 2px;color:#fff;}
.clip-meta{color:var(--muted);font-size:.76rem;margin-bottom:6px;}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;background:#444;}
.dot.ok{background:#2ee57d;box-shadow:0 0 8px #2ee57d;} .dot.no{background:var(--red);box-shadow:0 0 8px var(--red);}
.footnote{color:var(--muted);font-size:.78rem;margin-top:34px;border-top:1px solid var(--line);padding-top:14px;}

/* phones */
@media (max-width:640px){
  .brand{padding:14px;gap:12px;} .brand h1{font-size:1.35rem;letter-spacing:.06em;} .brand .logo{width:38px;height:38px;font-size:.9rem;}
  .stat-grid{grid-template-columns:repeat(2,1fr);} .stat .v{font-size:1.5rem;}
  .block-container{padding-left:.8rem;padding-right:.8rem;}
}
</style>
"""


def header_html() -> str:
    return ('<div class="brand"><div class="logo">G6</div><div>'
            '<h1>GTA 6 <span>CLIPPER AI</span></h1>'
            '<p>Turn live streams into captioned, fully-credited vertical Shorts.</p></div></div>')


def section(num: str, title: str) -> str:
    return f'<div class="sec"><b>{escape(num)}</b> {escape(title)}</div>'


_STATUS_CLASS = {"IDLE": "idle", "PROCESSING": "busy", "COMPLETE": "ok", "ERROR": "err"}


def dashboard_html(total: int, today: int, cap: int, status: str, date_label: str) -> str:
    remaining = max(0, cap - today)
    cls = _STATUS_CLASS.get(status, "idle")
    return (
        '<div class="stat-grid">'
        f'<div class="stat"><div class="k">Total clips generated</div><div class="v">{int(total)}</div><div class="s">all time</div></div>'
        f'<div class="stat"><div class="k">Today</div><div class="v">{int(today)} <small>/ {int(cap)}</small></div>'
        f'<div class="s">{escape(date_label)}</div></div>'
        f'<div class="stat"><div class="k">Left today</div><div class="v">{remaining}</div><div class="s">before the daily cap</div></div>'
        f'<div class="stat"><div class="k">Processing status</div><div class="v"><span class="pill {cls}">{escape(status)}</span></div>'
        '<div class="s">&nbsp;</div></div>'
        '</div>'
    )
