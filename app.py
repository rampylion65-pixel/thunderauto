import os, sqlite3, uuid
from datetime import datetime, timezone
from flask import Flask, request, redirect, render_template_string, abort
from werkzeug.utils import secure_filename
from functools import wraps
import secrets
app=Flask(__name__)
app.config['MAX_CONTENT_LENGTH']=100*1024*1024
ROOT=os.getenv('DATA_DIR','data'); os.makedirs(ROOT,exist_ok=True)
def db():
 c=sqlite3.connect(os.path.join(ROOT,'queue.db')); c.row_factory=sqlite3.Row; return c
with db() as c: c.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,title TEXT,platform TEXT,scheduled TEXT,file TEXT,status TEXT)')
@app.before_request
def auth():
 password=os.getenv('ADMIN_PASSWORD')
 if not password: abort(503,'Set ADMIN_PASSWORD before use')
 a=request.authorization
 if not a or not secrets.compare_digest(a.username or '', 'admin') or not secrets.compare_digest(a.password or '',password): return ('Login required',401,{'WWW-Authenticate':'Basic realm="Thunder"'})
@app.get('/')
def home():
 with db() as c: rows=c.execute('SELECT * FROM jobs ORDER BY scheduled').fetchall()
 return render_template_string('''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Thunder Auto Publisher</title><style>body{background:#101323;color:#eee;font:16px system-ui;max-width:900px;margin:40px auto;padding:20px}section{background:#1d2339;padding:24px;border-radius:16px;margin:20px 0}input,select,button{padding:12px;margin:8px 0;max-width:100%;box-sizing:border-box}button{background:#a99aff;border:0;border-radius:8px}table{width:100%;text-align:left}td,th{padding:8px}small{color:#bbc1d4}</style><h1>⚡ THUNDER AUTO PUBLISHER</h1><small>Phase 2 • Local storage test • No live social publishing</small><section><h2>Schedule video</h2><form method="post" action="/upload" enctype="multipart/form-data"><input name="title" placeholder="Video title" required><br><input type="file" name="video" accept="video/mp4" required><br><select name="platform"><option>YouTube</option><option>Facebook</option><option>Instagram</option></select><br><label>Schedule (UTC)</label><br><input type="datetime-local" name="scheduled" required><br><button>Add to queue</button></form></section><section><h2>Queue ({{rows|length}} / 100)</h2><form method="post" action="/check"><button>Check due jobs</button></form><div style="overflow:auto"><table><tr><th>Title</th><th>Platform</th><th>UTC</th><th>Status</th></tr>{% for r in rows %}<tr><td>{{r.title}}</td><td>{{r.platform}}</td><td>{{r.scheduled}}</td><td>{{r.status}}</td></tr>{% endfor %}</table></div></section>''',rows=rows)
@app.post('/upload')
def upload():
 f=request.files['video']; title=request.form['title'].strip(); platform=request.form['platform']; scheduled=request.form['scheduled']; datetime.strptime(scheduled,'%Y-%m-%dT%H:%M')
 if not title or platform not in ('YouTube','Facebook','Instagram') or not f.filename.lower().endswith('.mp4'): abort(400)
 ident=uuid.uuid4().hex; name=ident+'.mp4'
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  if c.execute('SELECT count(*) FROM jobs').fetchone()[0]>=100: abort(400,'Queue full')
  f.save(os.path.join(ROOT,name)); c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(ident,title,platform,scheduled,name,'Pending'))
 return redirect('/')
@app.post('/check')
def check():
 with db() as c: c.execute("UPDATE jobs SET status='Due - API not connected' WHERE status='Pending' AND scheduled<=?",(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M'),))
 return redirect('/')
if __name__=='__main__': app.run(port=int(os.getenv('PORT','5000')))
