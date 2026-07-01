"""HTTP receiver (Flask): authenticate webhooks, translate, enqueue Huey tasks, respond fast.

Contract with MinIO:
  - ``Authorization`` header carries the raw shared secret (NOT ``Bearer``); mismatch -> 401.
  - Return 200 immediately after enqueue (the consumer does the slow CDN work).
  - 4xx only for auth/parse errors (so MinIO drops poison messages); a broker/enqueue
    failure -> 503 so MinIO's own queue_dir retains + retries.

The receiver does no purging itself — it only enqueues one ``purge`` task per
(object, provider), which keeps it fast and independently scalable.
"""

import hmac

from flask import Flask, jsonify, request

from tasks import purge


def create_app(cfg, ctx, handlers, log):
    app = Flask(__name__)

    def _authorized() -> bool:
        presented = request.headers.get("Authorization", "")
        return bool(cfg.webhook_auth_token) and hmac.compare_digest(presented, cfg.webhook_auth_token)

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
                for provider_name in job.get("providers", []):
                    purge(job["path"], job["bucket"], provider_name)
                    enqueued += 1
        except Exception as e:  # broker unreachable -> let MinIO retry
            log.error(f"enqueue failed: {e}")
            return jsonify(error="temporarily unable to enqueue"), 503

        if jobs:
            log.info(f"accepted {len(jobs)} event(s) from '{source}' (enqueued {enqueued} task(s))")
        return jsonify(received=len(jobs), tasks=enqueued), 200

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok"), 200

    @app.get("/readyz")
    def readyz():
        return jsonify(status="ready"), 200

    return app
