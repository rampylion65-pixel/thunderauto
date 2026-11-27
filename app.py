import os, uuid
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
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
def storage():
 return boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT_URL'], aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'], aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'], region_name='auto', config=Config(signature_version='s3v4', connect_timeout=10, read_timeout=30))
def bucket(): return os.environ['R2_BUCKET_NAME']
@app.errorhandler(ClientError)
@app.errorhandler(BotoCoreError)
def storage_error(error):
 app.logger.warning('R2 operation failed: %s', type(error).__name__)
 return 'Cloud storage operation failed. Check Render configuration and R2 permissions.', 502
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
 client=storage(); uploaded=[]
 try:
  with db() as c:
   c.execute('LOCK TABLE thunder_jobs IN EXCLUSIVE MODE')
   if c.execute('SELECT count(*) AS total FROM thunder_jobs').fetchone()['total']+len(files)>100: abort(400,'Queue full')
   for f in files:
    ident=uuid.uuid4().hex; name='videos/'+ident+'.mp4'
    uploaded.append(name)
    client.upload_fileobj(f.stream,bucket(),name,ExtraArgs={'ContentType':'video/mp4'})
    c.execute('INSERT INTO thunder_jobs VALUES(%s,%s,%s,%s,%s,%s)',(ident,title or secure_filename(f.filename),platform,scheduled,name,'Pending'))
 except Exception:
  for name in uploaded:
   try: client.delete_object(Bucket=bucket(),Key=name)
   except Exception: app.logger.warning('R2 cleanup failed for %s',name)
  raise
 return redirect('/#queue')
@app.post('/delete/<ident>')
def delete(ident):
 with db() as c:
  r=c.execute('SELECT file FROM thunder_jobs WHERE id=%s FOR UPDATE',(ident,)).fetchone()
  if r:
   c.execute('DELETE FROM thunder_jobs WHERE id=%s',(ident,))
   if r['file'].startswith('videos/'):
    storage().delete_object(Bucket=bucket(),Key=r['file'])
   else:
    try: os.remove(os.path.join(ROOT,os.path.basename(r['file'])))
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
