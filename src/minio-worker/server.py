"""HTTP layer (Flask): receive webhooks, authenticate, enqueue, respond fast.

Contract with MinIO:
  - ``Authorization`` header carries the raw shared secret (NOT ``Bearer``); mismatch -> 401.
  - Return 200 immediately after enqueue (never call the CDN inline).
  - 4xx only for auth/parse errors (so MinIO drops poison messages instead of retrying
    forever); transient enqueue failures -> 503 so MinIO's own queue_dir retains + retries.
"""

import hmac

from flask import Flask, jsonify, request


def create_app(cfg, ctx, handlers, queue, log, ready_check):
    app = Flask(__name__)

    def _authorized() -> bool:
        presented = request.headers.get("Authorization", "")
        return bool(cfg.webhook_auth_token) and hmac.compare_digest(
            presented, cfg.webhook_auth_token
        )

    @app.post("/webhook")
    @app.post("/webhook/<source>")
    def webhook(source: str = "minio"):
        if not _authorized():
            log.warning(f"401 unauthorized webhook from {request.remote_addr} (source={source})")
            return jsonify(error="unauthorized"), 401

        handler = handlers.get(source)
        if handler is None:
            return jsonify(error=f"unknown source '{source}'"), 404

        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify(error="invalid or empty JSON body"), 400

        try:
            jobs = handler.handle(payload, ctx)
        except Exception as e:  # malformed but parseable payload
            log.warning(f"handler '{source}' could not process payload: {e}")
            return jsonify(error="unprocessable payload"), 400

        enqueued = 0
        try:
            for job in jobs:
                if queue.put(job):
                    enqueued += 1
        except OSError as e:  # queue dir full / not writable -> let MinIO retry
            log.error(f"enqueue failed (queue dir issue): {e}")
            return jsonify(error="temporarily unable to enqueue"), 503

        if jobs:
            log.info(f"accepted {len(jobs)} event(s) from '{source}' (enqueued {enqueued})")
        return jsonify(received=len(jobs), enqueued=enqueued), 200

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok"), 200

    @app.get("/readyz")
    def readyz():
        if ready_check():
            return jsonify(status="ready"), 200
        return jsonify(status="not ready"), 503

    return app
