THUNDER AUTO PUBLISHER - Phase 2 starter
Local: pip install -r requirements.txt
Windows PowerShell: $env:ADMIN_PASSWORD="choose-a-strong-password"
Run: python app.py ; open http://127.0.0.1:5000 ; username admin.
Render: upload these files to a private GitHub repository. Create a Python web service with build command pip install -r requirements.txt and start command gunicorn app:app. Select free plan if available. Set ADMIN_PASSWORD to a strong secret in Environment. Open the service URL and log in as admin.
IMPORTANT: This is a testing backend, not a production publisher. SQLite and uploaded videos are local and may disappear on free Render restarts. Do not upload irreplaceable or sensitive files. Schedule times are UTC (IST minus 5h30m). Check due jobs manually; no background worker or social API is connected. Supabase, R2, OAuth and durable scheduling are future integration steps. Max request 100 MB; 100 queue records, subject to disk capacity. Use HTTPS on hosted service. No account creation or deletion UI yet.
