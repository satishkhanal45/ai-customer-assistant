# Relay Release Notes

*Platform releases from version 4.2 onwards*

Relay is released on a six-week cadence. Releases are applied to all tenants in the same window and do not require customer action unless stated. Enterprise customers receive the release in their sandbox two weeks ahead of production.

## Relay 4.6 — 3 September 2026

#### Added

- Capacity forecasting extended from 60 to 90 days ahead in Relay Dispatch.
- Lone-worker check-in escalation now supports a second escalation contact.
- Webhook delivery log is now visible in the product for the last 7 days.

#### Changed

- The scheduling engine now explains rejected candidates as well as the chosen one.
- Attachment upload from Relay Mobile compresses images above 4 MB before transmission, cutting sync time on poor connections by roughly 40%.

#### Fixed

- Work orders imported through the API with a null time window could be scheduled outside the site's opening hours.
- The German translation of the parts consumption screen truncated long part names.

## Relay 4.5 — 23 July 2026

#### Added

- GraphQL API released in beta for Enterprise customers, read-only.
- UK data residency option (London) for Enterprise tenants.
- SCIM 2.0 user provisioning alongside existing SAML single sign-on.

#### Changed

- Insights history on Professional increased from 6 to 12 months at no additional cost.
- TLS 1.1 is no longer accepted from any client.

#### Fixed

- Recurring maintenance schedules generated a duplicate work order when the clocks changed.

## Relay 4.4 — 11 June 2026

#### Added

- Van stock tracking in Relay Mobile, including low-stock warnings.
- Zapier connector with 11 triggers and 4 actions.

#### Changed

- Schedule board redesigned to render 500 technicians without pagination.
- Photo metadata now records device model alongside location and timestamp.

#### Fixed

- Offline jobs completed either side of midnight were occasionally attributed to the wrong day.
- Salesforce write-back retried indefinitely when the target record had been deleted.

## Relay 4.3 — 30 April 2026

#### Added

- Custom dashboards in Insights Advanced.
- Twilio connector for appointment reminders and "on the way" messages.

#### Changed

- Minimum supported iOS version raised to 16; Android minimum raised to 11.

#### Fixed

- Certificate expiry warnings were sent to the technician rather than the supervisor.

## Relay 4.2 — 19 March 2026

#### Added

- Explainable scheduling — every automatic assignment now lists the constraints that produced it.
- Sandbox environments for Enterprise tenants.

#### Changed

- Work order search rewritten; median query time reduced from 1.9 s to 240 ms.

#### Fixed

- Bulk reassignment of more than 200 work orders could time out without reporting failure.

---

*Aldermoor Technologies — Work that finds its way. — www.aldermoor.example. This document describes a company that does not exist and is provided as sample data.*
