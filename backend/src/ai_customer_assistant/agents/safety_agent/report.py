"""
Report the groundedness outcome in the exact shape the Supervisor agent
expects.

Contract confirmed directly from the Supervisor's own code
(agents/supervisor/routing.py:decide_post_downstream), not guessed:

    downstream_result = {
        "status": "UNGROUNDED" | <anything else>,
        "response": <str>,
        "customer_wants_escalation": <bool>,
    }

decide_post_downstream() only specifically checks for the literal string
"UNGROUNDED" — any other status value falls through to FINALIZE. This
module uses "GROUNDED" for the other case for clarity/logging, even
though the Supervisor itself does not currently distinguish it from any
other non-"UNGROUNDED" value.

No queue, no callback, no API call: per inspection of graph.py, this
dict is returned as part of a LangGraph node's partial state update
(merged into SupervisorState["downstream_result"]) — there is nothing
to "call". The functions in this module build that dict; wiring them
into an actual Knowledge Agent node (the function that would replace
graph.py's `_placeholder_agent_node("Knowledge Agent")`) is a separate
piece, out of Safety Agent's scope.

Citations note: for the grounded case, this module accepts an
already-formatted response_text and passes it through unchanged. Citation
formatting is NOT done here (per project decision — see design log): the
Supervisor's own assemble_final_response() does a plain passthrough of
downstream_result["response"] with no citation logic, which confirms
formatting must happen upstream of this module, not inside it.
"""

from __future__ import annotations

from agents.safety_agent.fallback_response import generate_fallback_response
from agents.safety_agent.types import GroundednessResult

STATUS_GROUNDED = "GROUNDED"
STATUS_UNGROUNDED = "UNGROUNDED"


def report_grounded(response_text: str) -> dict:
    """
    Build the downstream_result payload for a grounded answer.

    Args:
        response_text: The final, already-formatted answer text (with
                        citations applied upstream, if applicable) to
                        show the customer.

    Returns:
        A dict matching the Supervisor's expected downstream_result
        shape, with status="GROUNDED" and customer_wants_escalation
        always False (escalation is only ever relevant to the
        ungrounded path).
    """
    return {
        "status": STATUS_GROUNDED,
        "response": response_text,
        "customer_wants_escalation": False,
    }


def report_ungrounded(
    query: str,
    result: GroundednessResult,
    *,
    customer_wants_escalation: bool = False,
) -> dict:
    """
    Build the downstream_result payload for an ungrounded answer.

    Args:
        query: The customer's original question — used to build the
               fallback response.
        result: The GroundednessResult that determined this answer was
                 ungrounded. Not currently embedded in the returned
                 dict (the Supervisor's contract has no field for it),
                 but accepted here so a caller cannot build this report
                 without actually having a groundedness verdict in
                 hand — and so this signature has an obvious place to
                 extend into if the Supervisor's contract grows a
                 diagnostics field later.
        customer_wants_escalation: Whether the customer has already
                confirmed they want human help. Defaults to False —
                the first time an ungrounded answer is reported, this
                is not yet known; a later turn (after the customer
                responds to the fallback question) would call this
                again with True to trigger the Supervisor's escalation
                path (see routing.py:_build_post_escalation).

    Returns:
        A dict matching the Supervisor's expected downstream_result
        shape, with status="UNGROUNDED" and a templated fallback
        response.
    """
    return {
        "status": STATUS_UNGROUNDED,
        "response": generate_fallback_response(query),
        "customer_wants_escalation": customer_wants_escalation,
    }
