"""Request-scoped labels for the operator interface."""

import json
import re
from pathlib import Path

LANGUAGE_COOKIE = "agentforge_language"
LANGUAGES = {"ru", "en"}
_RUSSIAN = json.loads(Path(__file__).with_name("ru.json").read_text(encoding="utf-8"))

_ERRORS = {
    "csrf_or_confirmation_required": (
        "Reload the page, review the action and confirm it again."
    ),
    "invalid_arguments": "Check the form values and try again.",
    "not_found": "This page or record could not be found.",
    "task_not_found": "This task could not be found.",
    "council_not_found": "This council could not be found.",
    "method_not_allowed": "This action is unavailable here.",
    "request_rejected": "The request was rejected. Reload and check the form.",
    "request_conflict": (
        "This form is out of date or the request is still running. Reload "
        "before trying again."
    ),
    "request_too_large": "Enter a request within the displayed length limit.",
    "too_many_requests": "The Director is busy. Wait for the active request to finish.",
    "conversation_capacity": (
        "The shared conversation journal is full. Continue an existing conversation "
        "or ask the operator to increase limits.sessions."
    ),
    "gateway_disconnected": "Could not reach Gateway. Reload to reconnect.",
    "service_unavailable": (
        "The service is unavailable. Check Gateway and the local model."
    ),
    "storage_unavailable": "Stored results are unavailable. Check local storage.",
    "internal_error": "An unexpected server error occurred. Check the operator log.",
    "specialist_unavailable": "The tool or its output is unavailable.",
    "coding_unavailable": "The coding workspace is unavailable.",
    "artifact_unavailable": "The file is missing, expired, or failed verification.",
    "workflow_not_found": "This workflow could not be found.",
    "approval_scope_changed": (
        "The approval data has changed. Reload and review the current action."
    ),
    "timeout": "The request took too long.",
    "provider_timeout": "The model took too long to respond.",
    "provider_error": "The model could not complete the request.",
    "access_denied": "The configured permission does not allow this operation.",
    "approval_rejected": "The approved action was rejected.",
    "approval_required": "Review is required before this step can run.",
    "incompatible_artifact": "The output from an earlier step is unavailable.",
    "dependency_failed": "The step cannot run until its dependencies succeed.",
    "execution_interrupted": (
        "Execution was interrupted. Check the worker before retrying."
    ),
    "binding_changed": "The configured target changed after this workflow was created.",
    "invalid_response": "The worker returned no usable response.",
    "stream_failed": "The worker did not send a visible streaming response.",
}


def translate(language, key):
    if language == "ru":
        return _RUSSIAN.get(key, key)
    return key


def error_message(language, code):
    return translate(
        language, _ERRORS.get(code, "Review the current status and try again.")
    )


_PROGRESS = re.compile(
    r"(Running model turn|Reading repository with|Searching with|Running tool) (.+)"
)


def progress_message(language: str, text: str) -> str:
    if language != "ru":
        return text
    match = _PROGRESS.fullmatch(text)
    if match is None:
        return translate(language, text)
    action, subject = match.groups()
    return f"{translate(language, action)} {subject}"
