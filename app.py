import os, sqlite3, uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, request, redirect, render_template, abort
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
 return render_template('index.html',rows=rows, counts={k:sum(r['status']==k for r in rows) for k in ['Pending','Published','Failed']})
@app.post('/upload')
def upload():
 files=request.files.getlist('video'); title=request.form.get('title','').strip(); platform=request.form['platform']; scheduled=request.form['scheduled']
 scheduled=(datetime.strptime(scheduled,'%Y-%m-%dT%H:%M')-timedelta(hours=5,minutes=30)).strftime('%Y-%m-%dT%H:%M')
 if not files or platform not in ('YouTube','Facebook','Instagram') or any(not f.filename.lower().endswith('.mp4') for f in files): abort(400)
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  if c.execute('SELECT count(*) FROM jobs').fetchone()[0]+len(files)>100: abort(400,'Queue full')
  for f in files:
   ident=uuid.uuid4().hex; name=ident+'.mp4'; f.save(os.path.join(ROOT,name)); c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(ident,title or secure_filename(f.filename),platform,scheduled,name,'Pending'))
 return redirect('/#queue')
@app.post('/delete/<ident>')
def delete(ident):
 with db() as c:
  r=c.execute('SELECT file FROM jobs WHERE id=?',(ident,)).fetchone()
  if r:
   c.execute('DELETE FROM jobs WHERE id=?',(ident,))
   try: os.remove(os.path.join(ROOT,r['file']))
   except FileNotFoundError: pass
 return redirect('/#queue')
@app.post('/edit/<ident>')
def edit(ident):
 scheduled=(datetime.strptime(request.form['scheduled'],'%Y-%m-%dT%H:%M')-timedelta(hours=5,minutes=30)).strftime('%Y-%m-%dT%H:%M')
 with db() as c: c.execute("UPDATE jobs SET title=?,scheduled=?,status='Pending' WHERE id=?",(request.form['title'],scheduled,ident))
 return redirect('/#queue')
@app.template_filter('ist')
def ist(value): return (datetime.strptime(value,'%Y-%m-%dT%H:%M')+timedelta(hours=5,minutes=30)).strftime('%Y-%m-%dT%H:%M')
@app.post('/check')
def check():
 with db() as c: c.execute("UPDATE jobs SET status='Due - API not connected' WHERE status='Pending' AND scheduled<=?",(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M'),))
 return redirect('/')
if __name__=='__main__': app.run(port=int(os.getenv('PORT','5000')))
