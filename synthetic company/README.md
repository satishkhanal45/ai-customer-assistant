# Aldermoor Technologies — synthetic corpus

A complete, internally consistent knowledge base for a company that **does not
exist**, for exercising the AI Customer Assistant end to end without using
anyone's real documents.

**Aldermoor Technologies Ltd.** is an invented British software company that
builds **Relay**, a field service management platform for utilities, telecoms
and facilities firms. Fifteen documents cover the company, its product,
pricing, support, security, contracts, policies, integrations, hiring and
release history — enough that the assistant can be asked realistic customer
questions and either answer them with a citation or correctly say it does not
know.

## What's here

| | Format | Document | Covers |
|---|---|---|---|
| 01 | PDF | Company Overview | History, mission, leadership, offices, industries served |
| 02 | PDF | Relay Platform Guide | The four modules, how a job flows, plan limits, supported devices |
| 03 | PDF | Pricing and Plans | Tiers, add-ons, trial, billing, discounts, a worked example |
| 04 | PDF | Support Handbook | Support tiers, severities, response targets, escalation, uptime credits |
| 05 | PDF | Security and Compliance | Certifications, data residency, encryption, recovery, sub-processors |
| 06 | PDF | Onboarding and Implementation | Packages, six phases, training, what we need from the customer |
| 07 | DOCX | Master Service Agreement | Term, fees, data ownership, liability, termination |
| 08 | DOCX | Refund and Cancellation Policy | Money-back guarantee, notice periods, data after cancellation |
| 09 | DOCX | Data Processing Addendum | Controller/processor roles, location tracking, transfers, deletion |
| 10 | MD | Frequently Asked Questions | 25 questions across six themes |
| 11 | MD | Integrations Catalogue | Connectors, what each does, REST/GraphQL/webhooks |
| 12 | MD | Contact Us | Who to email for what, offices, hours, phone menu |
| 13 | MD | Careers | Open roles, hiring process, benefits |
| 14 | MD | Release Notes | Five releases, 4.2 through 4.6 |
| 15 | MD | Glossary | Twenty domain terms |

Only PDF, DOCX and Markdown are used, because those are the only three formats
the ingestion pipeline accepts (`_UPLOAD_MIME_TO_FILE_TYPE` in
`backend/src/ai_customer_assistant/api/ingest.py`). A `.txt` or `.xlsx` here
would be refused at upload with HTTP 415.

## Ingesting it

> **This adds to whatever is already in the corpus.** If a different company's
> documents are already indexed, the assistant will answer from both and
> contradict itself. Run `make trunc` from the repository root first to empty
> the knowledge tables while keeping the schema.

```bash
cd "synthetic company"
APP_PORT=8000 ./ingest.sh you@example.com
```

The script signs in, uploads each file with the correct MIME type, and polls
each job to completion. The account needs the `member` role or higher. Expect
roughly ten to twenty minutes for all fifteen documents — most of that is LLM
entity extraction, and the worker processes one job at a time.

You can also drag the files onto the **Ingest** page in the web application,
which does the same thing.

## Questions worth asking afterwards

These are answerable from the corpus, and each exercises a different retrieval
path:

- *"How much does Relay cost per technician?"* — a fact in a table
- *"Do office users need a licence?"* — a fact contradicted by the obvious guess
- *"What happens if we cancel halfway through the year?"* — spans two documents
- *"Where is our data stored?"* — conditional on plan and region
- *"Does the mobile app work without a signal?"* — a product capability
- *"Who is the CTO?"* — an entity relationship
- *"What is your uptime commitment and what do we get if you miss it?"* — two linked facts
- *"Can I get a refund after eight months?"* — requires reading a policy, not pattern-matching
- *"Do you support on-premises deployment?"* — the answer is no, and it should say so
- *"What is your VAT number?"* — **not** in the corpus; the assistant should decline rather than invent

## Regenerating

The documents are generated, not hand-edited. `content.py` is the source of
truth; `generate.py` renders it.

```bash
cd "synthetic company"
uv run --with reportlab --with python-docx python generate.py
```

Editing a price in `content.py` changes it everywhere it appears, which is the
reason the content lives in one file rather than in fifteen. An assistant
tested against a corpus that contradicts itself teaches you nothing.

## What makes it safe to publish

- The `.example` top-level domain is reserved by RFC 2606 and can never be
  registered, so no address or URL here can reach a real system.
- UK telephone numbers use Ofcom's `0117 496 0xxx` drama range; US numbers use
  the `555-01xx` fictional block.
- Every person, customer, certificate and figure is invented.
- The company name was chosen to avoid collision with any real business, and
  the contracts carry an explicit line saying they are samples with no force.
