# Frequently Asked Questions

*Common questions about Relay and about working with Aldermoor*

## Getting started

#### How long does it take to get Relay running?

A Standard implementation for around 60 technicians with one integration typically reaches go-live four to five weeks after kick-off. Self-serve customers on Essentials are often running live work within a few days, because there is less to configure.

#### Is there a free trial?

Yes. A 14-day trial of the Professional plan, with no payment card required. It includes up to 10 technician licences and 500 work orders. Trial data is kept for 30 days after the trial ends, so nothing is lost if you convert later.

#### Can we import our existing work orders?

Yes. Customers, sites, assets and open work orders import from CSV or through the REST API and are included in every implementation package. Closed historical work orders can also be imported, but this is priced separately because the volume varies enormously.

#### Do you offer on-premises deployment?

No. Relay is available only as software as a service. Customers with data residency requirements choose an EU, US or UK region when their tenant is provisioned.

## Pricing and licensing

#### How is Relay priced?

Per technician per month. Essentials is £29, Professional is £54, and Enterprise starts at £95 and is quoted individually. Those prices assume annual billing; monthly billing costs 20% more.

#### Do office users need a licence?

No. Dispatchers, planners, supervisors and administrators are unlimited and free on every plan. A licence is only needed for someone who is assigned work in Relay Mobile.

#### What happens if we add technicians mid-year?

Additional licences are invoiced pro rata to the end of the current term, so they line up with your renewal date.

#### Can we reduce our licence count mid-term?

Reductions take effect at renewal rather than mid-term, because annual fees are invoiced in advance for the committed count. You can reassign a licence from one person to another at any time and as often as you like, which covers most staffing changes.

#### Is there a discount for a longer commitment?

Yes — 10% for a two-year term and 15% for three years. Registered charities receive 20% on production of evidence, and operations above 250 technicians are priced individually.

## Using the product

#### Does Relay Mobile work without a signal?

Yes, and this is a design goal rather than a fallback. A technician can download a day's work in advance, complete every step offline — including photographs, forms and signatures — and the device synchronises when connectivity returns.

#### How does automatic scheduling decide who gets a job?

The scheduling engine weighs skills and certifications, working time rules, travel distance, parts availability on the van and any customer time window. Every proposal lists the constraints that produced it, so a planner can see why a particular technician was chosen and override the decision if they disagree.

#### Which devices are supported?

iOS 16 or later, Android 11 or later. Relay is regularly tested on Zebra TC series and Samsung Galaxy XCover rugged devices. The web application supports the current and previous major versions of Chrome, Edge, Firefox and Safari.

#### How far back does reporting go?

Twelve months of history on Professional, and twenty-four months with the Insights Advanced add-on. Scheduled exports can be sent to email, secure FTP or object storage if you need to retain more than that in your own warehouse.

## Integrations and data

#### Which systems does Relay integrate with?

Pre-built connectors cover Salesforce, HubSpot, Microsoft Dynamics 365, SAP S/4HANA, Xero, QuickBooks Online, Microsoft Teams, Slack, Twilio, Google Maps Platform and Zapier. Beyond those there is a REST API, outbound webhooks on 34 event types, and a GraphQL API in beta for Enterprise customers.

#### How many integrations can we have?

None on Essentials, three on Professional, and unlimited on Enterprise.

#### Can we get our data out?

At any time, in JSON or CSV, through the product or the API, without asking us. If you leave, your data stays retrievable for 90 days.

#### Is there an API rate limit?

The allowance is 100,000 calls a month on Professional and 500,000 on Enterprise. Additional capacity is £180 a month per 50,000 calls.

## Support

#### What are your support hours?

Standard support on Essentials is 09:00 to 17:00 GMT on weekdays by email. Priority support on Professional is 07:00 to 19:00 local on weekdays, with live chat. Premier support on Enterprise is 24 hours a day, every day, including telephone.

#### How quickly will someone respond?

For a critical issue: one hour on Premier, four business hours on Priority, and one business day on Standard. Lower severities have proportionally longer targets, all published in the Support Handbook.

#### What do I do if a ticket is stuck?

Ask for escalation by replying to the ticket — you do not need permission and it will not count against you. If that does not move things, email escalations@aldermoor.example with the ticket reference and a support manager will respond within one business day.

#### Where can I see whether Relay is down?

status.aldermoor.example, which needs no login. You can subscribe there for email or webhook notification of incidents.

## Security and compliance

#### Are you certified?

ISO/IEC 27001:2022, recertified in March 2026, and a SOC 2 Type II report covering Security, Availability and Confidentiality. Cyber Essentials Plus covers the UK operation. Both the certificate and the report are available under a mutual NDA from security@aldermoor.example.

#### Where is our data stored?

In the region chosen when your tenant was provisioned — Frankfurt for EU, Northern Virginia for US, or London for UK on Enterprise. The choice is fixed for the life of the tenant.

#### Does Relay track technician location?

While a technician is on an active shift, yes — for scheduling, travel measurement and lone-worker safety. It does not record location outside shift hours. Sampling frequency and retention are configurable, and the whole feature can be switched off at tenant level.

#### Can Aldermoor staff see our data?

Not by default. No employee has standing access to production customer data. Access requires a named business reason, lasts a maximum of eight hours, and is logged.

---

*Aldermoor Technologies — Work that finds its way. — www.aldermoor.example. This document describes a company that does not exist and is provided as sample data.*
