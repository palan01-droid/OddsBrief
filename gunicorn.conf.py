# settings for gunicorn, the server that runs the app on render
# (only 1 worker, because the live prices and the background threads live in memory)
workers = 1
threads = 8
timeout = 120


def post_worker_init(worker):
    # start the websocket + 10 minute refresh once the server is up
    import web_app
    web_app.start_background()
