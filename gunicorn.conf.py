# Loaded automatically by gunicorn (it reads ./gunicorn.conf.py).
# Start the bot's background threads as soon as the worker boots, so a restart never leaves
# the bot (and its keep-awake ping) idle until someone opens the dashboard.


def post_worker_init(worker):
    import app
    app.start_background()
