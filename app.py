import os, uuid
import psycopg
from psycopg.rows import dict_row
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from flask import Flask, request, redirect, render_template, abort
from werkzeug.utils import secure_filename
from functools import wraps
import secrets
app=Flask(__name__)
app.config['MAX_CONTENT_LENGTH']=100*1024*1024
ROOT=os.getenv('DATA_DIR','data'); os.makedirs(ROOT,exist_ok=True)
@contextmanager
def db():
 url=os.environ.get('DATABASE_URL')
 if not url: raise RuntimeError('DATABASE_URL is required')
 with psycopg.connect(url, row_factory=dict_row, connect_timeout=10) as c:
  yield c
with db() as c:
 c.execute('CREATE TABLE IF NOT EXISTS thunder_jobs(id TEXT PRIMARY KEY,title TEXT,platform TEXT,scheduled TEXT,file TEXT,status TEXT)')
@app.before_request
def auth():
 password=os.getenv('ADMIN_PASSWORD')
 if not password: abort(503,'Set ADMIN_PASSWORD before use')
 a=request.authorization
 if not a or not secrets.compare_digest(a.username or '', 'admin') or not secrets.compare_digest(a.password or '',password): return ('Login required',401,{'WWW-Authenticate':'Basic realm="Thunder"'})
@app.get('/')
def home():
 with db() as c: rows=c.execute('SELECT * FROM thunder_jobs ORDER BY scheduled').fetchall()
 return render_template('index.html',rows=rows, counts={k:sum(r['status']==k for r in rows) for k in ['Pending','Published','Failed']})
@app.post('/upload')
def upload():
 files=request.files.getlist('video'); title=request.form.get('title','').strip(); platform=request.form['platform']; scheduled=request.form['scheduled']
 scheduled=(datetime.strptime(scheduled,'%Y-%m-%dT%H:%M')-timedelta(hours=5,minutes=30)).strftime('%Y-%m-%dT%H:%M')
 if not files or platform not in ('YouTube','Facebook','Instagram') or any(not f.filename.lower().endswith('.mp4') for f in files): abort(400)
 with db() as c:
  c.execute('LOCK TABLE thunder_jobs IN EXCLUSIVE MODE')
  if c.execute('SELECT count(*) AS total FROM thunder_jobs').fetchone()["total"]+len(files)>100: abort(400,'Queue full')
  for f in files:
   ident=uuid.uuid4().hex; name=ident+'.mp4'; f.save(os.path.join(ROOT,name)); c.execute('INSERT INTO thunder_jobs VALUES(%s,%s,%s,%s,%s,%s)',(ident,title or secure_filename(f.filename),platform,scheduled,name,'Pending'))
 return redirect('/#queue')
@app.post('/delete/<ident>')
def delete(ident):
 with db() as c:
  r=c.execute('SELECT file FROM thunder_jobs WHERE id=%s',(ident,)).fetchone()
  if r:
   c.execute('DELETE FROM thunder_jobs WHERE id=%s',(ident,))
   try: os.remove(os.path.join(ROOT,r['file']))
   except FileNotFoundError: pass
 return redirect('/#queue')
@app.post('/edit/<ident>')
def edit(ident):
 scheduled=(datetime.strptime(request.form['scheduled'],'%Y-%m-%dT%H:%M')-timedelta(hours=5,minutes=30)).strftime('%Y-%m-%dT%H:%M')
 with db() as c: c.execute("UPDATE thunder_jobs SET title=%s,scheduled=%s,status='Pending' WHERE id=%s",(request.form['title'],scheduled,ident))
 return redirect('/#queue')
@app.template_filter('ist')
def ist(value): return (datetime.strptime(value,'%Y-%m-%dT%H:%M')+timedelta(hours=5,minutes=30)).strftime('%Y-%m-%dT%H:%M')
@app.post('/check')
def check():
 with db() as c: c.execute("UPDATE thunder_jobs SET status='Due - API not connected' WHERE status='Pending' AND scheduled<=%s",(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M'),))
 return redirect('/')
if __name__=='__main__': app.run(port=int(os.getenv('PORT','5000')))
