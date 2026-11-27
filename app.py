import os, uuid
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
import psycopg
from psycopg.rows import dict_row
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from flask import Flask, request, redirect, render_template, abort, session
from werkzeug.utils import secure_filename
from functools import wraps
import secrets
app=Flask(__name__)
import hashlib, base64, tempfile, json
from cryptography.fernet import Fernet
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
app.secret_key=os.environ.get('APP_SECRET_KEY') or hashlib.sha256((os.environ.get('ADMIN_PASSWORD','')+os.environ.get('YOUTUBE_CLIENT_SECRET','')).encode()).hexdigest()
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

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
with db() as c:
 c.execute('CREATE TABLE IF NOT EXISTS thunder_youtube(id INTEGER PRIMARY KEY, token TEXT NOT NULL)')
 c.execute('ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS youtube_id TEXT')
 c.execute('ALTER TABLE thunder_jobs ADD COLUMN IF NOT EXISTS error TEXT')
def cipher():
 return Fernet(base64.urlsafe_b64encode(hashlib.sha256(app.secret_key.encode()).digest()))
def flow(state=None):
 return Flow.from_client_config({'web':{'client_id':os.environ['YOUTUBE_CLIENT_ID'],'client_secret':os.environ['YOUTUBE_CLIENT_SECRET'],'auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':'https://oauth2.googleapis.com/token'}}, scopes=['https://www.googleapis.com/auth/youtube.upload'],state=state,redirect_uri=os.environ['YOUTUBE_REDIRECT_URI'])
@app.get('/oauth/youtube')
def youtube_connect():
 f=flow(); url,state=f.authorization_url(access_type='offline',prompt='consent'); session['oauth_state']=state; session['oauth_verifier']=f.code_verifier; return redirect(url)
@app.get('/oauth/youtube/callback')
def youtube_callback():
 state=session.pop('oauth_state',None)
 if not state or not secrets.compare_digest(state,request.args.get('state','')): abort(400,'Invalid OAuth state')
 if request.args.get('error'): return 'Google authorization declined',400
 f=flow(state); f.code_verifier=session.pop('oauth_verifier',None)
 f.fetch_token(code=request.args.get('code'))
 if not f.credentials.refresh_token: abort(400,'Reconnect and grant offline access')
 token=cipher().encrypt(f.credentials.to_json().encode()).decode()
 with db() as c: c.execute('INSERT INTO thunder_youtube VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET token=EXCLUDED.token',(token,))
 return redirect('/#accounts')
@app.post('/youtube/publish/<ident>')
def youtube_publish(ident):
 with db() as c:
  r=c.execute('SELECT * FROM thunder_jobs WHERE id=%s FOR UPDATE',(ident,)).fetchone()
  t=c.execute('SELECT token FROM thunder_youtube WHERE id=1').fetchone()
  if not r or r['platform']!='YouTube' or r['status'] not in ('Pending','Due - API not connected') or not t: abort(400,'Job unavailable or account not connected')
  if not r['file'].startswith('videos/'): abort(400,'Reupload old local video')
  c.execute("UPDATE thunder_jobs SET status='Uploading' WHERE id=%s",(ident,))
 try:
  creds=Credentials.from_authorized_user_info(json.loads(cipher().decrypt(t['token'].encode())))
  with tempfile.TemporaryDirectory() as tmp:
   path=os.path.join(tmp,'video.mp4'); storage().download_file(bucket(),r['file'],path)
   status={'privacyStatus':'private','selfDeclaredMadeForKids':request.form.get('kids')=='yes'}
   result=build('youtube','v3',credentials=creds,cache_discovery=False).videos().insert(part='snippet,status',body={'snippet':{'title':r['title'][:100],'description':request.form.get('description','')[:5000],'categoryId':'22'},'status':status},media_body=MediaFileUpload(path,mimetype='video/mp4',resumable=True)).execute()
  with db() as c: c.execute("UPDATE thunder_jobs SET status='Uploaded private',youtube_id=%s,error=NULL WHERE id=%s",(result['id'],ident))
 except Exception as e:
  app.logger.warning('YouTube upload failed: %s',type(e).__name__)
  with db() as c: c.execute("UPDATE thunder_jobs SET status='Needs review',error=%s WHERE id=%s",(type(e).__name__,ident))
 return redirect('/#queue')
@app.before_request
def auth():
 password=os.getenv('ADMIN_PASSWORD')
 if not password: abort(503,'Set ADMIN_PASSWORD before use')
 a=request.authorization
 if not a or not secrets.compare_digest(a.username or '', 'admin') or not secrets.compare_digest(a.password or '',password): return ('Login required',401,{'WWW-Authenticate':'Basic realm="Thunder"'})
@app.get('/')
def home():
 with db() as c:
  rows=c.execute('SELECT * FROM thunder_jobs ORDER BY scheduled').fetchall()
  connected=bool(c.execute('SELECT id FROM thunder_youtube WHERE id=1').fetchone())
 return render_template('index.html',rows=rows, connected=connected, counts={k:sum(r['status']==k for r in rows) for k in ['Pending','Published','Failed']})
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
    c.execute('INSERT INTO thunder_jobs(id,title,platform,scheduled,file,status) VALUES(%s,%s,%s,%s,%s,%s)',(ident,title or secure_filename(f.filename),platform,scheduled,name,'Pending'))
 except Exception:
  for name in uploaded:
   try: client.delete_object(Bucket=bucket(),Key=name)
   except Exception: app.logger.warning('R2 cleanup failed for %s',name)
  raise
 return redirect('/#queue')
@app.post('/delete/<ident>')
def delete(ident):
 with db() as c:
  r=c.execute("SELECT file FROM thunder_jobs WHERE id=%s AND status <> 'Uploading' FOR UPDATE",(ident,)).fetchone()
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
 with db() as c: c.execute("UPDATE thunder_jobs SET title=%s,scheduled=%s,status='Pending' WHERE id=%s AND status IN ('Pending','Due - API not connected')",(request.form['title'],scheduled,ident))
 return redirect('/#queue')
@app.template_filter('ist')
def ist(value): return (datetime.strptime(value,'%Y-%m-%dT%H:%M')+timedelta(hours=5,minutes=30)).strftime('%Y-%m-%dT%H:%M')
@app.post('/check')
def check():
 with db() as c: c.execute("UPDATE thunder_jobs SET status='Due - API not connected' WHERE status='Pending' AND scheduled<=%s",(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M'),))
 return redirect('/')
if __name__=='__main__': app.run(port=int(os.getenv('PORT','5000')))
