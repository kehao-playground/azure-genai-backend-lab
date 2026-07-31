import logging

from azgenai_lab.core.correlation import correlation_id_var
from azgenai_lab.core.tenant_context import tenant_id_var

# The stock factory, captured once at import time so the wrapper below can
# delegate to it instead of hardcoding LogRecord's constructor signature.
_base_record_factory = logging.getLogRecordFactory()


def _correlation_record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
    # Stamped on every record, not just the LLM adapter's manual lines: this
    # is what makes the search adapter's per-call line and any future stage
    # log joinable on correlation_id without each call site remembering to
    # read the ContextVar itself. "-" outside a request (e.g. a background
    # task or a log line emitted before the middleware runs) so the field is
    # always present and greppable rather than sometimes-missing.
    record = _base_record_factory(*args, **kwargs)
    record.correlation_id = correlation_id_var.get() or "-"
    # tenant_id_var already defaults to "-" outside require_principal's scope,
    # unlike correlation_id_var's None default, so no `or "-"` fallback is
    # needed here. Group ids never enter log records — only the tenant id.
    record.tenant_id = tenant_id_var.get()
    return record


def configure_logging(level: str = "INFO") -> None:
    # setLogRecordFactory is global and independent of handler wiring, so it
    # survives the force=True re-configuration below.
    logging.setLogRecordFactory(_correlation_record_factory)
    # force=True: pytest (and other callers) may already have installed
    # handlers on the root logger, which would make a plain basicConfig()
    # call a silent no-op. This must actually take effect every time.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s correlation_id=%(correlation_id)s %(message)s",
        force=True,
    )
