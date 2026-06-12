import os

# Bind to Railway's assigned port. Reading the env var in Python avoids
# relying on shell ($PORT) expansion inside the nixpacks start command,
# which is unreliable and silently crashes gunicorn on boot.
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = 2
threads = 4
timeout = 120
graceful_timeout = 120
accesslog = "-"
errorlog = "-"
