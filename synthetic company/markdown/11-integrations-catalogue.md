# Integrations Catalogue

*What Relay connects to, and how*

## Pre-built connectors

Connectors are configured in Relay Connect and require credentials for the system being connected. Each connector runs on a schedule, on a webhook, or both, depending on what the far system supports.

| Integration | Category | Direction | Available on |
|---|---|---|---|
| Salesforce | CRM | Two-way | Professional and above |
| HubSpot | CRM | Two-way | Professional and above |
| Microsoft Dynamics 365 | ERP / CRM | Two-way | Enterprise |
| SAP S/4HANA | ERP | Two-way | Enterprise |
| Xero | Accounting | Outbound | Professional and above |
| QuickBooks Online | Accounting | Outbound | Professional and above |
| Microsoft Teams | Collaboration | Outbound | All plans |
| Slack | Collaboration | Outbound | All plans |
| Twilio | Messaging | Outbound | Professional and above |
| Google Maps Platform | Mapping and routing | Inbound | All plans |
| Zapier | Automation | Two-way | Professional and above |
| Webhooks | Automation | Outbound | Professional and above |

## What each connector does

#### Salesforce and HubSpot

Service cases or tickets raised in the CRM create work orders in Relay. When the work order is completed, the outcome, completion time, parts used and any customer signature are written back to the originating record. Account and contact records synchronise into Relay customers and sites.

#### Microsoft Dynamics 365 and SAP S/4HANA

Bidirectional synchronisation of customers, sites, assets and work orders, plus outbound posting of completed jobs for billing. Both are Enterprise-only because they require field mapping work that is specific to each customer's configuration.

#### Xero and QuickBooks Online

Completed work orders are posted as draft invoices with labour time, parts consumed and any agreed call-out charge. Relay does not pull financial data back; the accounting system remains the record of truth for billing.

#### Microsoft Teams and Slack

Outbound notifications for job assignment, job completion, SLA breach warnings and failed synchronisations. Channel routing is configurable per work type or region.

#### Twilio

Appointment reminders and "technician on the way" messages to the end customer by SMS. Message templates are editable and support per-tenant sender identifiers.

#### Google Maps Platform

Travel time estimates for the scheduling engine and map rendering on the schedule board. The customer supplies their own API key so usage is billed to them directly.

#### Zapier

Triggers on eleven Relay events and four actions, for customers who want lightweight automation without building against the API.

## APIs

#### REST API

- Described by an OpenAPI 3.1 document published at api.aldermoor.example/openapi.json
- Authentication by API key or OAuth 2.0 client credentials
- Available on Professional and Enterprise
- Allowance of 100,000 calls a month on Professional, 500,000 on Enterprise
- Rate limited to 20 requests per second per tenant

#### GraphQL API

- In beta, Enterprise only
- Read-only in the current release; mutations are planned
- Not covered by the availability commitment while in beta

#### Webhooks

- 34 event types across the work order, technician and scheduling domains
- Signed with HMAC-SHA256 so the receiver can verify origin
- Retried with exponential backoff for up to 24 hours
- Delivery log retained for 7 days and visible in the product

## Requesting a new connector

Customers can request a connector to a system not on this list. Requests go to their Customer Success Manager or to support@aldermoor.example. Aldermoor builds connectors where there is demand from several customers; otherwise, a bespoke integration can be delivered as a professional services engagement against the REST API.

---

*Aldermoor Technologies — Work that finds its way. — www.aldermoor.example. This document describes a company that does not exist and is provided as sample data.*
