"""Stable, shared human-readable error presentation for UI, CLI and history."""
from __future__ import annotations
import uuid
from copy import deepcopy

_PRESENTATIONS={
    "invalid_input":("The supplied input is incomplete or invalid.","Review the highlighted input fields and try again."),
    "invalid_purpose":("The selected document purpose is not configured for this workflow.","Choose a supported purpose from the workflow selector."),
    "input_too_large":("The supplied input exceeds the configured size limit.","Shorten the input without removing clinically required facts, then start a new run."),
    "credential_missing":("A credential required by the selected provider is missing.","Open model settings, provide the credential, and retry the request."),
    "authentication_rejected":("The model provider rejected the supplied credential.","Check the credential in model settings and retry after correcting it."),
    "endpoint_unreachable":("The configured model endpoint could not be reached.","Check the endpoint, network access, and local model service, then retry."),
    "provider_timeout":("The model provider did not respond before the request timeout.","Check provider availability or timeout settings, then retry."),
    "provider_transient_failure":("The model provider reported a temporary failure.","Retry when the provider is available. Transport retries are tracked separately."),
    "provider_request_rejected":("The model provider rejected the request.","Check the selected model and provider settings before retrying."),
    "provider_incompatible_response":("The provider response could not be interpreted as a model response.","Check provider compatibility and model configuration before retrying."),
    "provider_redirect_rejected":("The configured provider attempted an unexpected redirect.","Use the final trusted API endpoint directly."),
    "output_truncated":("The provider stopped before returning the complete model output.","Retry the model operation; the incomplete response remains available for inspection."),
    "syntax_invalid":("The model returned malformed structured output.","The runtime can request a bounded output repair using the parser findings."),
    "schema_invalid":("The model output does not satisfy the required output schema.","The runtime can request a bounded output repair using all schema findings."),
    "reference_invalid":("The model output contains an invalid or unsupported reference.","The runtime can request a bounded repair without weakening the reference contract."),
    "tool_denied":("The requested tool or action is outside this workflow's allowed scope.","Use only the actions exposed for this model operation."),
    "tool_arguments":("The model supplied invalid tool arguments.","Correct the arguments to match the unchanged allowed action contract."),
    "semantic_check_exhausted":("A mandatory content check still fails after the permitted semantic revisions.","Inspect the checker findings and use an allowed human revision or start a new run."),
    "checks_not_passed":("One or more mandatory checks have not passed.","Resolve the listed check findings before approval."),
    "budget_exhausted":("An independent execution budget was exhausted.","Inspect the limiting budget and start a new run with an appropriate configured budget if justified."),
    "external_response_unknown":("A prior external dispatch may have completed, but its outcome is unknown.","Use the explicit external retry action only if repeating the provider request is acceptable."),
    "data_directory_busy":("Another writer is already using the selected state directory.","Close the other writer or choose a different state directory."),
    "storage_integrity_failure":("Committed runtime state references a missing or unreadable file.","Stop the affected run and restore the referenced file from a known-good copy before continuing."),
    "legacy_data_relocation_required":("Legacy .localmedbot data was found and has not been relocated.","Run the explicit offline relocation command before normal startup."),
    "relocation_incomplete":("A previous data relocation did not complete.","Resume the same relocation operation; do not use the half-migrated destination as runtime state."),
    "runtime_version_mismatch":("This historical run was created by a different product version and is read-only.","Inspect it historically or start a new run with the current version."),
    "execution_interrupted":("Execution stopped when the application was interrupted.","Resume to continue from the recorded state, or inspect the recorded calls and start a new run."),
    "execution_stopped":("Execution stopped unexpectedly.","Resume to continue from the recorded state, or use the diagnostic reference when inspecting logs before starting a new run."),
    "execution_dispatch_failed":("The execution worker could not start, so no model request was made.","Resume to start the run again, or check the local runtime and start a new run."),
    "external_retry_acknowledgement_required":("The previous provider request may have completed, so repeating it needs explicit acknowledgement.","Confirm that duplicate execution and billing are acceptable before retrying."),
    "run_not_resumable":("This run has no safe continuation path.","Inspect the run and start a new run if necessary."),
}


def present_error(error, *, stage=None, attempt=None, diagnostic_reference=None, fallback=False):
    code=getattr(error,"code",None) or (error.get("code") if isinstance(error,dict) else None) or "unexpected_failure"
    detail=getattr(error,"detail",None) if not isinstance(error,dict) else error.get("detail")
    findings=getattr(error,"findings",None) if not isinstance(error,dict) else error.get("findings")
    base=_PRESENTATIONS.get(code)
    if base is None:
        ref=diagnostic_reference or uuid.uuid4().hex[:12]
        explanation="The operation failed unexpectedly. The runtime cannot safely infer the cause."
        remedy="Use the diagnostic reference when inspecting logs or reporting the failure."
        fallback=True
    else:
        explanation,remedy=base; ref=diagnostic_reference
    out={"code":code,"detail":detail or "","findings":deepcopy(findings or []),"explanation":explanation,"remedy":remedy,"fallback":bool(fallback)}
    if stage is not None: out["stage"]=stage
    if attempt is not None: out["attempt"]=attempt
    if ref: out["diagnostic_reference"]=ref
    return out
