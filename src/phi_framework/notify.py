"""Review-request email: tell each scope's reviewers that new items await them in the review App.

Runs as the last task of the scan job. For every OPEN batch with items awaiting review
and no REVIEW_REQUEST_SENT event yet, it sends one email:

* To: the scope's ``steward_emails``.
* Cc: the scope's ``governance_owner_emails``, except those already in To.
* Body: counts, scope, due date, and a link to the batch in the review App. No column data.

Each send records REVIEW_REQUEST_SENT. A send failure records REVIEW_REQUEST_FAILED (kind SEND), is
retried by the next scan, and fails the task after every batch has been attempted. A batch with no
recipients records REVIEW_REQUEST_FAILED (kind RECIPIENTS) once and is not retried. Recipients come
straight from the scope registry, so no group membership is read. An empty ``smtp_host`` disables sending.
"""

from __future__ import annotations

import base64
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from phi_framework import constants as c
from phi_framework.config import Settings
from phi_framework.runtime import Runtime, log
from phi_framework.sqlutil import in_list, lit

# REVIEW_REQUEST_FAILED kinds: RECIPIENTS is recorded once per batch; SEND is retried by the next scan.
FAIL_RECIPIENTS = "RECIPIENTS"
FAIL_SEND = "SEND"


@dataclass(frozen=True)
class ReviewRequest:
    review_batch_id: str
    scope_id: str
    catalog_name: str
    schema_name: str
    due_at: object
    item_count: int
    to: tuple[str, ...]
    cc: tuple[str, ...]
    link: str


def _emails(values) -> set[str]:
    return {str(v).strip().lower() for v in (values or []) if v and "@" in str(v)}


def render(req: ReviewRequest) -> tuple[str, str]:
    """Subject and plain-text body for a review request."""
    scope = f"{req.catalog_name}.{req.schema_name}"
    subject = f"Action required: PHI classification review - {scope} - batch {req.review_batch_id}"
    body = (
        "Hello,\n\n"
        f"The PHI classification scan found {req.item_count} column(s) in {scope} (scope {req.scope_id}) "
        "that need review. They are missing a direct PHI classification tag, changed since approval, "
        "or are due for periodic recertification.\n\n"
        f"Please complete the review by {req.due_at} UTC in the review app:\n{req.link}\n\n"
        "For each column, approve the suggested classification, correct it, confirm the column is not PHI, "
        "or request Privacy review. The app records your identity and the time of each decision.\n\n"
        "You are receiving this because you are a reviewer or the governance owner for this scope.\n\n"
        "Data Governance\n"
    )
    return subject, body


class Notifier:
    def __init__(self, rt: Runtime, smtp_factory=None):
        self.rt = rt
        self.s: Settings = rt.s
        self._smtp_factory = smtp_factory or self._smtp

    # ------------------------------------------------------------------
    def run(self) -> None:
        if not self.s.smtp_host:
            log.warning("smtp_host is not set; review-request emails are disabled")
            return
        pending = self._pending_batches()
        if not pending:
            log.info("No batches awaiting a review request")
            return
        base_url = self._app_url()
        send_failures = 0
        for b in pending:
            # Each batch is handled on its own, so one failure never stops the other batches.
            to_set = _emails(b["steward_emails"])
            cc_set = _emails(b["governance_owner_emails"])
            req = ReviewRequest(
                review_batch_id=b["review_batch_id"],
                scope_id=b["scope_id"],
                catalog_name=b["catalog_name"],
                schema_name=b["schema_name"],
                due_at=b["due_at"],
                item_count=int(b["item_count"]),
                to=tuple(sorted(to_set)),
                cc=tuple(sorted(cc_set - to_set)),
                link=f"{base_url}/?batch={b['review_batch_id']}" if base_url else "(review app URL not configured)",
            )
            details = {"to": list(req.to), "cc": list(req.cc), "item_count": req.item_count}
            if not req.to and not req.cc:
                # A configuration problem, not a transient one: record it once and do not retry the batch
                # or fail the scan every week. Reviewers still see the batch in the App.
                log.error("Review request for %s has no recipients", req.review_batch_id)
                self.rt.event(c.EV_REVIEW_REQUEST_FAILED, req.review_batch_id,
                              details={**details, "kind": FAIL_RECIPIENTS,
                                       "error": "steward_emails and governance_owner_emails are empty"})
            else:
                try:
                    self._send(req)
                    self.rt.event(c.EV_REVIEW_REQUEST_SENT, req.review_batch_id, details=details)
                except Exception as exc:  # one failed send must not stop the others; retried next scan
                    send_failures += 1
                    log.error("Review request for %s failed: %s", req.review_batch_id, exc)
                    self.rt.event(c.EV_REVIEW_REQUEST_FAILED, req.review_batch_id,
                                  details={**details, "kind": FAIL_SEND, "error": f"{type(exc).__name__}: {exc}"[:2000]})
            # Record each outcome immediately so a crash never causes a batch to be emailed twice.
            self.rt.flush_events()
        log.info("Review requests: %d batch(es), %d send failure(s)", len(pending), send_failures)
        if send_failures:
            raise RuntimeError(f"{send_failures} review request(s) could not be sent; see REVIEW_REQUEST_FAILED events")

    # ------------------------------------------------------------------
    def _pending_batches(self) -> list[dict]:
        return self.rt.rows(
            f"""SELECT b.review_batch_id, b.scope_id, b.due_at, s.catalog_name, s.schema_name,
                   s.steward_emails, s.governance_owner_emails, count(i.review_item_id) AS item_count
            FROM {self.rt.t(c.T_BATCH)} b
            JOIN {self.rt.t(c.T_SCOPE)} s ON s.scope_id = b.scope_id
            JOIN {self.rt.t(c.T_ITEM)} i ON i.review_batch_id = b.review_batch_id
            WHERE b.status = {lit(c.BATCH_OPEN)} AND b.last_error IS NULL
              AND i.status IN ({in_list(c.REVIEWABLE_STATUSES)})
              AND NOT EXISTS (
                SELECT 1 FROM {self.rt.t(c.T_EVENT)} e
                WHERE e.review_batch_id = b.review_batch_id
                  AND (e.event_type = {lit(c.EV_REVIEW_REQUEST_SENT)}
                       OR (e.event_type = {lit(c.EV_REVIEW_REQUEST_FAILED)}
                           AND CAST(e.event_details:kind AS STRING) = {lit(FAIL_RECIPIENTS)})))
            GROUP BY ALL
            ORDER BY b.review_batch_id"""
        )

    def _app_url(self) -> str:
        if not self.s.review_app_name:
            return ""
        try:
            return (self.rt.w.apps.get(self.s.review_app_name).url or "").rstrip("/")
        except Exception as exc:  # the email still goes out without a link
            log.warning("Could not resolve the URL of app %s: %s", self.s.review_app_name, exc)
            return ""

    def _secret(self, key: str) -> str | None:
        if not self.s.smtp_secret_scope:
            return None
        try:
            value = self.rt.w.secrets.get_secret(self.s.smtp_secret_scope, key).value
        except Exception:
            return None
        return base64.b64decode(value).decode("utf-8") if value else None

    def _smtp(self):
        server = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=60)
        if self.s.smtp_use_tls:
            server.starttls()
        user, password = self._secret("smtp_username"), self._secret("smtp_password")
        if user and password:
            server.login(user, password)
        return server

    def _send(self, req: ReviewRequest) -> None:
        subject, body = render(req)
        msg = EmailMessage()
        msg["From"] = self.s.smtp_sender
        msg["To"] = ", ".join(req.to) if req.to else self.s.smtp_sender
        if req.cc:
            msg["Cc"] = ", ".join(req.cc)
        msg["Subject"] = subject
        msg.set_content(body)
        server = self._smtp_factory()
        try:
            server.send_message(msg)
        finally:
            server.quit()


def main(settings: Settings) -> None:
    settings.validate(expected_role=c.ROLE_SCAN)
    Notifier(Runtime(settings)).run()
