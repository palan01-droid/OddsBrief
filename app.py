# render's default start command is "gunicorn app:app", so this just points it at the real app
from web_app import app
