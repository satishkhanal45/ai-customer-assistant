"""The corpus for Aldermoor Technologies, a company that does not exist.

Every fact below is invented. The point is a knowledge base that is
*internally consistent* — prices quoted in the pricing document match the
ones in the FAQ, the SLA credits match the service agreement, the office
addresses match everywhere they appear — because an assistant that answers
from contradictory sources is untestable.

Conventions that keep the data obviously synthetic:

* the ``.example`` top-level domain is reserved by RFC 2606 and can never
  be registered, so no address or URL here can reach a real system;
* UK numbers use Ofcom's ``0117 496 0xxx`` drama range and US numbers the
  ``555-01xx`` fictional block.

Document content is data, not prose in code: ``DOCUMENTS`` is a list of
blocks that ``generate.py`` renders into PDF, DOCX or Markdown. Adding a
document means adding an entry here, not writing a new renderer.
"""

COMPANY = "Aldermoor Technologies"
TAGLINE = "Work that finds its way."
WEBSITE = "www.aldermoor.example"

# --------------------------------------------------------------------------
# Shared facts. Referenced by several documents so they cannot drift apart.
# --------------------------------------------------------------------------

PLANS = [
    ["Plan", "Price (per technician / month)", "Technician limit", "Included"],
    ["Essentials", "£29", "Up to 25", "Relay Dispatch, Relay Mobile"],
    ["Professional", "£54", "Unlimited", "Adds Relay Insights, REST API, 3 integrations"],
    ["Enterprise", "From £95 (custom)", "Unlimited", "Adds SSO/SAML, SCIM, unlimited integrations, sandbox, dedicated CSM"],
]

ADDONS = [
    ["Add-on", "Price", "Notes"],
    ["Relay Insights Advanced", "£12 per technician / month", "Custom dashboards, scheduled exports, 24-month history"],
    ["Additional sandbox", "£250 / month", "Enterprise only; one sandbox is already included"],
    ["Premium onboarding", "£4,500 one-off", "Dedicated implementation consultant for 12 weeks"],
    ["Extra API capacity", "£180 / month per 50,000 calls", "On top of the plan allowance"],
]

SUPPORT_TIERS = [
    ["Tier", "Available on", "Channels", "Hours", "First response (P1)"],
    ["Standard", "Essentials", "Email", "09:00–17:00 GMT, Mon–Fri", "1 business day"],
    ["Priority", "Professional", "Email, live chat", "07:00–19:00 local, Mon–Fri", "4 business hours"],
    ["Premier", "Enterprise", "Email, chat, phone", "24 / 7 / 365", "1 hour"],
]

SEVERITIES = [
    ["Severity", "Definition", "Example"],
    ["P1 — Critical", "Relay is unavailable, or a defect stops all field work, with no workaround.", "Technicians cannot receive jobs on any device."],
    ["P2 — High", "A major function is degraded or unavailable; a workaround exists but is costly.", "Scheduling optimiser is failing; jobs must be assigned by hand."],
    ["P3 — Medium", "A function behaves incorrectly with a reasonable workaround.", "A report exports the wrong date column."],
    ["P4 — Low", "Cosmetic issue, documentation gap, or a question.", "A label is mis-spelled on the settings page."],
]

UPTIME_CREDITS = [
    ["Monthly uptime achieved", "Service credit"],
    ["99.90% or above (Enterprise target met)", "None"],
    ["Below 99.90% but at or above 99.00%", "10% of that month's fee"],
    ["Below 99.00% but at or above 95.00%", "25% of that month's fee"],
    ["Below 95.00%", "50% of that month's fee"],
]

OFFICES = [
    ["Office", "Address", "Phone", "Role"],
    ["Bristol (HQ)", "14 Whitcombe Yard, Bristol BS1 4TR, United Kingdom", "+44 117 496 0184", "Head office, product, customer success"],
    ["Kraków", "ul. Jasnogórska 41, 31-358 Kraków, Poland", "+48 12 350 7722", "Engineering and platform operations"],
    ["Austin", "3300 Glenmore Loop, Suite 220, Austin, TX 78704, United States", "+1 512 555 0147", "North American sales and support"],
]

LEADERSHIP = [
    ["Name", "Role", "Based in", "Joined"],
    ["Imogen Hale", "Chief Executive Officer, co-founder", "Bristol", "2016"],
    ["Rafal Nowicki", "Chief Technology Officer, co-founder", "Kraków", "2016"],
    ["Dele Adeyemi", "Chief Operating Officer", "Bristol", "2019"],
    ["Marta Silva", "VP Customer Success", "Bristol", "2020"],
    ["Yuki Tanabe", "VP Engineering", "Kraków", "2021"],
    ["Peter Oduya", "Head of Information Security", "Bristol", "2022"],
    ["Fiona Brennan", "VP Sales, North America", "Austin", "2022"],
]

INTEGRATIONS = [
    ["Integration", "Category", "Direction", "Available on"],
    ["Salesforce", "CRM", "Two-way", "Professional and above"],
    ["HubSpot", "CRM", "Two-way", "Professional and above"],
    ["Microsoft Dynamics 365", "ERP / CRM", "Two-way", "Enterprise"],
    ["SAP S/4HANA", "ERP", "Two-way", "Enterprise"],
    ["Xero", "Accounting", "Outbound", "Professional and above"],
    ["QuickBooks Online", "Accounting", "Outbound", "Professional and above"],
    ["Microsoft Teams", "Collaboration", "Outbound", "All plans"],
    ["Slack", "Collaboration", "Outbound", "All plans"],
    ["Twilio", "Messaging", "Outbound", "Professional and above"],
    ["Google Maps Platform", "Mapping and routing", "Inbound", "All plans"],
    ["Zapier", "Automation", "Two-way", "Professional and above"],
    ["Webhooks", "Automation", "Outbound", "Professional and above"],
]

# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

DOCUMENTS = [
    # ---------------------------------------------------------------- PDF --
    {
        "slug": "01-company-overview",
        "format": "pdf",
        "title": "Aldermoor Technologies — Company Overview",
        "subtitle": "Who we are, what we build, and how we work",
        "blocks": [
            ("h1", "About Aldermoor Technologies"),
            ("p", "Aldermoor Technologies is a software company that builds Relay, a field service management platform used by utilities, telecommunications operators and facilities management firms to plan, dispatch and evidence work carried out away from the office."),
            ("p", "The company was founded in 2016 in Bristol, United Kingdom, by Imogen Hale and Rafal Nowicki, who had spent the previous decade building scheduling systems for a regional water utility and were frustrated that every tool they encountered treated the field technician as an afterthought. Aldermoor now employs 180 people across three offices and serves approximately 340 customers in 18 countries."),
            ("p", "Our tagline, \"Work that finds its way\", describes the product's central idea: a job should reach the right technician, with the right parts and the right information, without a dispatcher having to hold the whole schedule in their head."),

            ("h2", "Mission"),
            ("p", "To make field work predictable for the people who plan it and straightforward for the people who do it."),

            ("h2", "What we believe"),
            ("bullets", [
                "The technician is the primary user. If the mobile app is slow or confusing, nothing else about the system matters.",
                "Offline is not an edge case. Field work happens in basements, plant rooms and rural sites, and the product is designed for intermittent connectivity rather than patched for it.",
                "Scheduling decisions should be explainable. Every automatic assignment Relay makes can be traced to the constraints that produced it.",
                "Customer data belongs to the customer. Export is available at any time, in an open format, without asking us.",
            ]),

            ("h2", "Company facts"),
            ("table", [
                ["Item", "Detail"],
                ["Legal name", "Aldermoor Technologies Ltd."],
                ["Founded", "2016"],
                ["Headquarters", "Bristol, United Kingdom"],
                ["Employees", "180"],
                ["Customers", "Approximately 340 organisations in 18 countries"],
                ["Product", "Relay — field service management platform"],
                ["Ownership", "Privately held; Series B closed in 2023"],
                ["Website", WEBSITE],
            ]),

            ("h2", "Leadership team"),
            ("table", LEADERSHIP),

            ("h2", "Offices"),
            ("table", OFFICES),

            ("h2", "History"),
            ("bullets", [
                "2016 — Aldermoor Technologies founded in Bristol. First version of Relay Dispatch built for a single water utility customer.",
                "2018 — Relay Mobile released for iOS and Android with full offline job execution.",
                "2019 — Kraków engineering office opened. Dele Adeyemi joins as Chief Operating Officer.",
                "2021 — ISO 27001 certification achieved. Relay Insights released.",
                "2022 — Austin office opened to serve North American customers. First SOC 2 Type II report issued.",
                "2023 — Series B funding round closed. Relay Connect released, bringing integrations under one framework.",
                "2025 — Relay 4.0 released, introducing the constraint-based scheduling engine.",
                "2026 — Relay reaches 340 customers; EU and US data residency options become generally available.",
            ]),

            ("h2", "Industries we serve"),
            ("bullets", [
                "Water and wastewater utilities",
                "Electricity distribution network operators",
                "Gas distribution and metering",
                "Telecommunications and fibre installation",
                "Facilities management and building services",
                "Renewable energy operations and maintenance",
            ]),
        ],
    },
    {
        "slug": "02-relay-platform-guide",
        "format": "pdf",
        "title": "The Relay Platform — Product Guide",
        "subtitle": "Modules, capabilities and how they fit together",
        "blocks": [
            ("h1", "What Relay is"),
            ("p", "Relay is a field service management platform. It takes work that needs doing at a physical location, decides who should do it and when, puts that work in a technician's hand on a mobile device, and records what actually happened so it can be billed, audited and improved."),
            ("p", "Relay is delivered as software as a service. There is no on-premises deployment option. Customers access the platform through a web application for office users and native mobile applications for field users."),

            ("h2", "Modules"),
            ("h3", "Relay Dispatch"),
            ("p", "The planning and scheduling module, used by dispatchers and planners in the office. Dispatch holds the work order lifecycle, the technician roster, skills and certifications, parts availability, and the constraint-based scheduling engine that proposes assignments."),
            ("bullets", [
                "Work order management from creation through to invoicing hand-off",
                "Drag-and-drop schedule board with day, week and technician views",
                "Constraint-based automatic scheduling considering skills, certifications, working time, travel, parts and customer time windows",
                "Explainable assignments — every proposal lists the constraints that produced it",
                "Recurring and planned preventive maintenance schedules",
                "Capacity forecasting up to 90 days ahead",
            ]),

            ("h3", "Relay Mobile"),
            ("p", "The native application technicians use on site, available for iOS 16 and above and Android 11 and above. Relay Mobile is designed to work without connectivity: a technician can download a day's work, complete every step offline, and synchronise when signal returns."),
            ("bullets", [
                "Full offline job execution with automatic conflict-safe synchronisation",
                "Guided job steps, checklists and dynamic forms",
                "Photo, video and signature capture with location and timestamp metadata",
                "Parts consumption and van stock tracking",
                "Health and safety checks, including lone-worker check-in",
                "Navigation hand-off to the device's preferred mapping application",
            ]),

            ("h3", "Relay Insights"),
            ("p", "The reporting and analytics module. Insights provides first-time-fix rate, mean time to repair, schedule adherence, travel time, utilisation and SLA attainment, broken down by team, region, work type or customer."),
            ("bullets", [
                "Twenty-two standard reports covering operations, quality and cost",
                "Scheduled exports to email, secure FTP or object storage",
                "Twelve months of history on Professional; twenty-four months with Insights Advanced",
                "Custom dashboards (Insights Advanced only)",
            ]),

            ("h3", "Relay Connect"),
            ("p", "The integration framework. Connect provides pre-built connectors to common business systems, a REST API, outbound webhooks, and a GraphQL API currently in beta."),
            ("bullets", [
                "Pre-built connectors — see the integrations catalogue for the current list",
                "REST API with an OpenAPI 3.1 description",
                "Outbound webhooks on 34 event types",
                "GraphQL API (beta, Enterprise only)",
                "API allowance of 100,000 calls per month on Professional and 500,000 on Enterprise",
            ]),

            ("h2", "How a job flows through Relay"),
            ("bullets", [
                "A work order is created — raised by a planner, generated by a maintenance schedule, or pushed in from a CRM or ERP through Relay Connect.",
                "Dispatch evaluates constraints and proposes a technician and time slot. A planner can accept, override or lock the proposal.",
                "The job appears on the technician's device. If they will be working without signal, the day's work downloads in advance.",
                "The technician completes the job — guided steps, checklists, photographs, parts used, customer signature.",
                "The completed job synchronises back, closing the work order and releasing it to billing.",
                "Insights records the outcome against first-time-fix, SLA attainment and utilisation.",
            ]),

            ("h2", "Platform limits"),
            ("table", [
                ["Limit", "Essentials", "Professional", "Enterprise"],
                ["Technicians", "25", "Unlimited", "Unlimited"],
                ["Work orders per month", "5,000", "50,000", "Unlimited"],
                ["Custom forms", "10", "100", "Unlimited"],
                ["API calls per month", "Not available", "100,000", "500,000"],
                ["Integrations", "None", "3", "Unlimited"],
                ["Attachment storage", "50 GB", "500 GB", "2 TB"],
                ["Sandbox environments", "None", "None", "1 included"],
            ]),

            ("h2", "Supported browsers and devices"),
            ("bullets", [
                "Web application — the current and previous major versions of Chrome, Edge, Firefox and Safari",
                "Relay Mobile for iOS — iOS 16 or later",
                "Relay Mobile for Android — Android 11 or later",
                "Rugged devices — tested on Zebra TC series and Samsung Galaxy XCover",
            ]),
        ],
    },
    {
        "slug": "03-pricing-and-plans",
        "format": "pdf",
        "title": "Relay Pricing and Plans",
        "subtitle": "Effective 1 January 2026",
        "blocks": [
            ("h1", "Plans"),
            ("p", "Relay is licensed per technician per month. A technician licence is required for anyone who is assigned work in Relay Mobile. Office users — dispatchers, planners, supervisors and administrators — are included at no additional cost on every plan."),
            ("p", "Prices below are in pounds sterling and assume annual billing. Monthly billing is available on Essentials and Professional at a 20% premium. Prices exclude VAT and any applicable local sales tax."),
            ("table", PLANS),

            ("h2", "Add-ons"),
            ("table", ADDONS),

            ("h2", "What is included on every plan"),
            ("bullets", [
                "Unlimited office users",
                "Relay Dispatch and Relay Mobile",
                "Standard reports",
                "Mobile applications for iOS and Android",
                "Data export in JSON and CSV at any time",
                "Security updates and all platform releases",
            ]),

            ("h2", "Free trial"),
            ("p", "A 14-day free trial of the Professional plan is available with no payment card required. The trial includes up to 10 technician licences and 500 work orders. Trial data is retained for 30 days after the trial ends, so an account can be converted without losing configuration."),

            ("h2", "Billing"),
            ("bullets", [
                "Annual plans are invoiced in advance for the full term.",
                "Monthly plans are invoiced in advance each month.",
                "Payment terms are 30 days from the invoice date.",
                "Accepted methods are bank transfer, direct debit and corporate card. Cheques are not accepted.",
                "Invoices are issued in pounds sterling, euros or United States dollars, fixed at the start of each term.",
                "Adding technician licences mid-term is billed pro rata to the end of the current term.",
                "Reducing technician licences takes effect at the next renewal, not mid-term.",
            ]),

            ("h2", "Discounts"),
            ("bullets", [
                "Two-year term — 10% discount",
                "Three-year term — 15% discount",
                "Registered charities and non-profits — 20% discount, evidence required",
                "Volume above 250 technicians — priced individually",
            ]),

            ("h2", "Worked example"),
            ("p", "A distribution network operator with 120 technicians on the Professional plan, taking Insights Advanced and one premium onboarding engagement, would pay:"),
            ("table", [
                ["Line", "Calculation", "Annual total"],
                ["Professional licences", "120 × £54 × 12", "£77,760"],
                ["Insights Advanced", "120 × £12 × 12", "£17,280"],
                ["Premium onboarding", "One-off", "£4,500"],
                ["Total, first year", "", "£99,540"],
                ["Total, subsequent years", "", "£95,040"],
            ]),
        ],
    },
    {
        "slug": "04-support-and-sla",
        "format": "pdf",
        "title": "Support Handbook",
        "subtitle": "How to reach us, what to expect, and how issues are escalated",
        "blocks": [
            ("h1", "Support tiers"),
            ("p", "Every Relay customer has access to support. The tier is determined by the plan, and sets the channels, the hours and the response commitments."),
            ("table", SUPPORT_TIERS),

            ("h2", "How to raise a ticket"),
            ("bullets", [
                "Email support@aldermoor.example from the address registered on your account.",
                "Use the in-product help widget, available to any signed-in user.",
                "Live chat in the web application (Professional and Enterprise).",
                "Telephone +44 117 496 0184, option 2 (Enterprise only, 24/7).",
            ]),
            ("p", "A ticket should include the affected work order or technician identifier, the time the problem occurred, the expected and actual behaviour, and a screenshot or short screen recording where the issue is visual. Tickets that include a reproducible sequence of steps are typically resolved in less than half the time of those that do not."),

            ("h2", "Severity levels"),
            ("table", SEVERITIES),

            ("h2", "Response and resolution targets"),
            ("table", [
                ["Severity", "First response (Premier)", "First response (Priority)", "Target resolution"],
                ["P1 — Critical", "1 hour", "4 business hours", "Continuous effort until resolved or worked around"],
                ["P2 — High", "4 hours", "1 business day", "3 business days"],
                ["P3 — Medium", "1 business day", "2 business days", "Next scheduled release"],
                ["P4 — Low", "2 business days", "3 business days", "Backlog, no committed date"],
            ]),

            ("h2", "Escalation"),
            ("p", "If a ticket is not progressing, any customer may escalate. Escalation does not require permission and does not affect the standing of the account."),
            ("bullets", [
                "First — reply to the ticket asking for escalation. The support engineer will involve a senior engineer the same working day.",
                "Second — email escalations@aldermoor.example with the ticket reference. A support manager responds within one business day.",
                "Third — Enterprise customers may contact their Customer Success Manager directly, who can convene an incident review.",
            ]),

            ("h2", "Service availability commitment"),
            ("p", "Aldermoor commits to a monthly uptime of 99.5% on the Professional plan and 99.9% on the Enterprise plan, measured across the production web application and API. Planned maintenance, announced at least five business days in advance and carried out within the published maintenance window, is excluded from the calculation."),
            ("p", "The standard maintenance window is Sunday 02:00 to 06:00 in the region hosting the customer's data. Not every window is used."),

            ("h2", "Service credits"),
            ("p", "Where the monthly uptime commitment is missed, a service credit is available on request. A claim must be made within 30 days of the end of the affected month. Credits are applied against the next invoice and are the sole remedy for missed availability."),
            ("table", UPTIME_CREDITS),

            ("h2", "Status and incident communication"),
            ("bullets", [
                "Live status is published at status.aldermoor.example and does not require a login.",
                "Subscribers receive email or webhook notification when an incident is opened, updated or resolved.",
                "A written post-incident review is published within five business days of any P1 incident.",
            ]),

            ("h2", "What support does not cover"),
            ("bullets", [
                "Writing custom integrations on the customer's behalf — this is available as a professional services engagement.",
                "Fixing data quality problems that originate in a connected system.",
                "Training end users, beyond the onboarding sessions included with the plan.",
                "Supporting devices below the published minimum operating system versions.",
            ]),
        ],
    },
    {
        "slug": "05-security-and-compliance",
        "format": "pdf",
        "title": "Security and Compliance Overview",
        "subtitle": "Certifications, architecture and data handling",
        "blocks": [
            ("h1", "Certifications and audits"),
            ("table", [
                ["Framework", "Status", "Most recent", "Scope"],
                ["ISO/IEC 27001:2022", "Certified", "Recertified March 2026", "Relay platform, supporting infrastructure, Bristol and Kraków offices"],
                ["SOC 2 Type II", "Report issued annually", "Period ending 31 December 2025", "Security, Availability and Confidentiality"],
                ["UK GDPR and EU GDPR", "Compliant", "Data Protection Officer appointed", "All personal data processed on behalf of customers"],
                ["Cyber Essentials Plus", "Certified", "January 2026", "United Kingdom operations"],
            ]),
            ("p", "The current ISO 27001 certificate and the most recent SOC 2 Type II report are available to customers and prospects under a mutual non-disclosure agreement. Request them from security@aldermoor.example."),

            ("h2", "Data residency"),
            ("p", "Customers choose a data region when their tenant is provisioned. The choice is fixed for the life of the tenant; moving an existing tenant between regions is a professional services engagement."),
            ("table", [
                ["Region", "Location", "Availability"],
                ["EU", "Frankfurt, Germany", "All plans"],
                ["US", "Northern Virginia, United States", "All plans"],
                ["UK", "London, United Kingdom", "Enterprise only"],
            ]),

            ("h2", "Encryption"),
            ("bullets", [
                "In transit — TLS 1.3, with TLS 1.2 accepted for legacy rugged devices. Older protocol versions are refused.",
                "At rest — AES-256 applied to databases, object storage and backups.",
                "Keys are managed in a hardware security module and rotated annually.",
                "Attachments uploaded from Relay Mobile are encrypted on the device before transmission.",
            ]),

            ("h2", "Access control"),
            ("bullets", [
                "Role-based access control with seven built-in roles and optional custom roles on Enterprise.",
                "SAML 2.0 single sign-on and SCIM 2.0 user provisioning on Enterprise.",
                "Multi-factor authentication available on all plans and enforceable by policy.",
                "Aldermoor staff access to customer data requires a named business reason, is granted for a maximum of eight hours, and is logged.",
                "No Aldermoor employee has standing access to production customer data.",
            ]),

            ("h2", "Resilience and recovery"),
            ("table", [
                ["Measure", "Commitment"],
                ["Backup frequency", "Hourly incremental, daily full"],
                ["Backup retention", "35 days"],
                ["Recovery point objective (RPO)", "1 hour"],
                ["Recovery time objective (RTO)", "4 hours"],
                ["Disaster recovery test", "Twice yearly, results shared with Enterprise customers on request"],
                ["Availability architecture", "Multi-availability-zone within the chosen region"],
            ]),

            ("h2", "Testing and vulnerability management"),
            ("bullets", [
                "An independent penetration test is carried out annually; a summary letter is available to customers.",
                "Automated dependency scanning runs on every build.",
                "Critical vulnerabilities are remediated within 7 days, high within 30 days, medium within 90 days.",
                "A responsible disclosure programme is published; reports go to security@aldermoor.example.",
            ]),

            ("h2", "Sub-processors"),
            ("p", "Aldermoor uses a small number of sub-processors. Customers are notified at least 30 days before a new sub-processor is added, and may object in writing."),
            ("table", [
                ["Sub-processor", "Purpose", "Region"],
                ["Cloud infrastructure provider", "Hosting, storage, backup", "EU, US or UK per tenant"],
                ["Transactional email provider", "System notification email", "EU"],
                ["Error monitoring provider", "Application error reporting", "EU"],
                ["Customer support platform", "Ticket handling", "EU"],
            ]),

            ("h2", "Incident response"),
            ("p", "Aldermoor maintains a documented incident response plan, rehearsed twice a year. Where a personal data breach affecting a customer is confirmed, that customer is notified without undue delay and in any event within 72 hours of confirmation, with the facts known at the time and a named contact for follow-up."),
        ],
    },
    {
        "slug": "06-onboarding-and-implementation",
        "format": "pdf",
        "title": "Onboarding and Implementation Guide",
        "subtitle": "From signed contract to steady-state operation",
        "blocks": [
            ("h1", "Implementation packages"),
            ("table", [
                ["Package", "Duration", "Included with", "Consultant time"],
                ["Self-serve", "Customer-led", "Essentials", "Documentation and email support only"],
                ["Standard", "4 weeks", "Professional", "Shared implementation consultant, 15 hours"],
                ["Guided", "8 weeks", "Enterprise", "Named implementation consultant, 40 hours"],
                ["Premium", "12 weeks", "Paid add-on (£4,500)", "Dedicated implementation consultant, 90 hours"],
            ]),

            ("h2", "Phases"),
            ("h3", "1. Discovery — week 1"),
            ("bullets", [
                "Confirm the work types, regions and teams in scope",
                "Map existing work order statuses onto the Relay lifecycle",
                "Identify the systems to integrate and who owns them",
                "Agree the success measures for go-live",
            ]),
            ("h3", "2. Configuration — weeks 2 to 3"),
            ("bullets", [
                "Build the technician roster, skills matrix and certifications",
                "Configure work types, checklists and custom forms",
                "Set scheduling constraints, working time rules and travel assumptions",
                "Configure roles and single sign-on where applicable",
            ]),
            ("h3", "3. Data migration — weeks 3 to 4"),
            ("bullets", [
                "Import customers, sites and assets from CSV or through the API",
                "Import open work orders; closed history is optional and priced separately",
                "Reconcile record counts with the customer before sign-off",
            ]),
            ("h3", "4. Pilot — weeks 4 to 6"),
            ("bullets", [
                "One team, one region, running live work in Relay alongside the existing process",
                "Daily stand-up with the implementation consultant for the first week",
                "Configuration adjusted from what the pilot team reports",
            ]),
            ("h3", "5. Go-live"),
            ("bullets", [
                "Remaining teams cut over in waves, typically a region a week",
                "The old system is placed in read-only mode rather than switched off",
                "Support moves from the implementation consultant to the standard support channel",
            ]),
            ("h3", "6. Hypercare — 30 days after go-live"),
            ("bullets", [
                "Response targets one severity level higher than the contracted tier",
                "Twice-weekly check-in with the implementation consultant",
                "A written handover to the Customer Success Manager at the end of the period",
            ]),

            ("h2", "What we need from the customer"),
            ("bullets", [
                "A named project owner with authority to make configuration decisions",
                "A subject matter expert for scheduling rules, available for roughly four hours a week",
                "An IT contact for single sign-on and integration credentials",
                "A pilot team of between six and fifteen technicians",
                "Data extracts in CSV from the systems being replaced",
            ]),

            ("h2", "Training"),
            ("table", [
                ["Audience", "Format", "Duration", "Included"],
                ["Technicians", "Live remote session or recorded", "45 minutes", "All packages"],
                ["Dispatchers and planners", "Live remote workshop", "Half day", "Standard and above"],
                ["Administrators", "Live remote workshop", "Full day", "Guided and Premium"],
                ["Train the trainer", "On site", "Two days", "Premium only"],
            ]),

            ("h2", "Typical timeline"),
            ("p", "A Standard implementation for a 60-technician operation with one CRM integration usually reaches go-live four to five weeks after kick-off. The most common cause of delay is not the software — it is waiting for credentials to a system being integrated, or for a decision about how an existing work type should map onto the Relay lifecycle."),
        ],
    },

    # --------------------------------------------------------------- DOCX --
    {
        "slug": "07-master-service-agreement",
        "format": "docx",
        "title": "Master Service Agreement",
        "subtitle": "Aldermoor Technologies Ltd. — standard terms, version 4.2, January 2026",
        "blocks": [
            ("p", "This agreement is a synthetic sample created for testing. It is not legal advice and has no force."),

            ("h1", "1. Definitions"),
            ("bullets", [
                "\"Platform\" means Relay and any module of it made available to the Customer.",
                "\"Customer Data\" means all data submitted to the Platform by or on behalf of the Customer.",
                "\"Technician Licence\" means the right for one named individual to be assigned work in Relay Mobile.",
                "\"Order Form\" means the document recording the plan, licence count, term and fees.",
            ]),

            ("h1", "2. Provision of the service"),
            ("p", "Aldermoor grants the Customer a non-exclusive, non-transferable right to access and use the Platform for its internal business purposes during the term, for the number of Technician Licences recorded on the Order Form."),
            ("p", "Office users — dispatchers, planners, supervisors and administrators — do not consume a Technician Licence and are unlimited on every plan."),

            ("h1", "3. Term and renewal"),
            ("bullets", [
                "The initial term is recorded on the Order Form and is twelve months unless stated otherwise.",
                "Annual agreements renew automatically for successive twelve-month terms.",
                "Either party may prevent renewal by giving written notice at least 60 days before the end of the current term.",
                "Monthly agreements may be cancelled with 30 days' written notice, effective at the end of the following billing month.",
            ]),

            ("h1", "4. Fees and payment"),
            ("bullets", [
                "Fees are as recorded on the Order Form and are exclusive of VAT and other applicable taxes.",
                "Invoices are payable within 30 days of the invoice date.",
                "Technician Licences added during a term are invoiced pro rata to the end of that term.",
                "Technician Licences may be reduced at renewal only; mid-term reductions do not generate a refund.",
                "Aldermoor may increase fees at renewal by giving 60 days' notice. Increases are capped at 5% per annum for agreements of two years or more.",
                "Aldermoor may suspend access where an undisputed invoice is more than 45 days overdue, after giving 14 days' written notice.",
            ]),

            ("h1", "5. Customer data"),
            ("p", "The Customer retains all right, title and interest in Customer Data. Aldermoor processes Customer Data only to provide the Platform, to comply with law, and where instructed by the Customer."),
            ("bullets", [
                "The Customer may export Customer Data at any time in JSON or CSV through the Platform or the API.",
                "On termination, Customer Data remains retrievable for 90 days.",
                "After the 90-day retrieval window, Customer Data is deleted from production within 30 days and from backups within a further 35 days.",
                "A certificate of deletion is available on written request.",
            ]),

            ("h1", "6. Service levels"),
            ("p", "Availability commitments, response targets and service credits are set out in the Support Handbook, which forms part of this agreement. Service credits are the Customer's sole and exclusive remedy for failure to meet an availability commitment."),

            ("h1", "7. Confidentiality"),
            ("p", "Each party shall keep the other's confidential information in confidence and use it only for the purposes of this agreement. This obligation survives termination by three years, and indefinitely in respect of personal data and source code."),

            ("h1", "8. Warranties"),
            ("bullets", [
                "Aldermoor warrants that the Platform will perform materially in accordance with its published documentation.",
                "Aldermoor warrants that it will provide the Platform with reasonable skill and care.",
                "The Customer warrants that it has the right to submit Customer Data and that doing so does not infringe any third-party right.",
                "Except as stated, all other warranties are excluded to the extent permitted by law.",
            ]),

            ("h1", "9. Limitation of liability"),
            ("p", "Neither party limits liability for death or personal injury caused by negligence, for fraud, or for any other liability that cannot lawfully be limited."),
            ("p", "Subject to the above, each party's total aggregate liability arising out of this agreement is limited to the fees paid or payable in the twelve months preceding the event giving rise to the claim. Neither party is liable for indirect or consequential loss, loss of profit, or loss of anticipated saving."),

            ("h1", "10. Termination"),
            ("bullets", [
                "Either party may terminate for material breach that is not remedied within 30 days of written notice.",
                "Either party may terminate immediately on the other's insolvency.",
                "Aldermoor may terminate where the Customer's use breaches the Acceptable Use Policy and is not corrected within 7 days of notice.",
                "Termination does not relieve the Customer of the obligation to pay fees accrued before the termination date.",
            ]),

            ("h1", "11. Governing law"),
            ("p", "This agreement is governed by the laws of England and Wales, and the courts of England and Wales have exclusive jurisdiction."),
        ],
    },
    {
        "slug": "08-refund-and-cancellation-policy",
        "format": "docx",
        "title": "Refund and Cancellation Policy",
        "subtitle": "Version 2.4 — effective 1 January 2026",
        "blocks": [
            ("h1", "Free trial"),
            ("p", "The 14-day Professional trial requires no payment card and converts to a paid plan only when the customer chooses to subscribe. A trial that is not converted simply expires. Trial data is retained for 30 days after expiry and then deleted."),

            ("h1", "Money-back guarantee"),
            ("p", "A customer taking an annual plan for the first time may cancel within 30 days of the start of the initial term and receive a full refund of the licence fees paid. The guarantee applies once per organisation and does not extend to renewal terms."),
            ("bullets", [
                "Onboarding and professional services fees are not covered by the guarantee where the work has already been delivered.",
                "Where onboarding has begun but not completed, the unused portion is refunded on a pro rata basis.",
                "Refunds are made by the original payment method within 14 days of the request being approved.",
            ]),

            ("h1", "Cancelling a monthly plan"),
            ("bullets", [
                "Written notice of 30 days is required, sent to billing@aldermoor.example or through the in-product billing page.",
                "Cancellation takes effect at the end of the billing month following the notice period.",
                "Fees already invoiced for the notice period remain payable.",
                "No partial-month refunds are given.",
            ]),

            ("h1", "Cancelling an annual plan"),
            ("bullets", [
                "An annual plan runs to the end of its term and cannot be cancelled mid-term except for material breach.",
                "To prevent automatic renewal, written notice is required at least 60 days before the term ends.",
                "Notice given fewer than 60 days before the end of a term takes effect at the end of the following term.",
            ]),

            ("h1", "Reducing licence counts"),
            ("p", "Technician licence counts may be reduced at renewal. Mid-term reductions do not produce a refund or a credit, because annual fees are invoiced in advance for the committed count. Licences may be reassigned between individuals at any time and as often as required."),

            ("h1", "Refunds outside the guarantee"),
            ("p", "Aldermoor will consider a refund outside the circumstances above where the Platform has been materially unavailable and service credits do not reasonably compensate for the disruption. Requests are reviewed by the Customer Success and Finance teams and answered within 10 business days."),

            ("h1", "Service credits are not refunds"),
            ("p", "Service credits arising from missed availability commitments are applied against a future invoice. They are not paid in cash and are not refundable on termination."),

            ("h1", "Data after cancellation"),
            ("bullets", [
                "Customer Data remains exportable for 90 days after the service ends.",
                "After 90 days, data is deleted from production systems within 30 days.",
                "Backup copies age out within a further 35 days.",
                "A certificate of deletion is issued on request at no charge.",
            ]),

            ("h1", "How to request a refund"),
            ("p", "Email billing@aldermoor.example from an address registered on the account, quoting the invoice number and the reason for the request. Requests are acknowledged within two business days and decided within ten."),
        ],
    },
    {
        "slug": "09-data-processing-addendum",
        "format": "docx",
        "title": "Data Processing Addendum",
        "subtitle": "Aldermoor Technologies Ltd. as processor — version 3.1",
        "blocks": [
            ("p", "This addendum is a synthetic sample created for testing. It is not legal advice and has no force."),

            ("h1", "1. Roles"),
            ("p", "For personal data contained in Customer Data, the Customer is the controller and Aldermoor is the processor. Aldermoor processes such personal data only on the Customer's documented instructions, which include the instruction to provide the Platform in accordance with the agreement."),

            ("h1", "2. Categories of data"),
            ("table", [
                ["Category of data subject", "Personal data processed"],
                ["Customer's field technicians", "Name, work email, work telephone, employee reference, skills and certifications, working time, device location while on shift"],
                ["Customer's office staff", "Name, work email, role, authentication identifiers, audit log entries"],
                ["Customer's own end customers", "Name, service address, contact telephone, contact email, appointment history, signature captured at job completion"],
            ]),

            ("h1", "3. Purpose and duration"),
            ("p", "Personal data is processed for the purpose of scheduling, dispatching, executing and evidencing field work, and for producing operational reporting for the Customer. Processing continues for the duration of the agreement and the retention periods that follow it."),

            ("h1", "4. Location tracking"),
            ("p", "Relay Mobile records device location while a technician is on an active shift, in order to support scheduling, travel measurement and lone-worker safety. Location is not recorded outside shift hours. The Customer is responsible for informing its technicians of this processing and for establishing a lawful basis for it."),
            ("bullets", [
                "Location sampling frequency is configurable by the Customer between 1 and 15 minutes.",
                "Location history retention is configurable between 30 and 400 days.",
                "Location recording can be disabled entirely at the tenant level.",
            ]),

            ("h1", "5. Security measures"),
            ("p", "Aldermoor implements the technical and organisational measures described in the Security and Compliance Overview, including encryption in transit and at rest, role-based access control, time-bounded and logged staff access, annual penetration testing and a rehearsed incident response plan."),

            ("h1", "6. Sub-processors"),
            ("p", "The Customer gives general authorisation for Aldermoor to engage sub-processors. The current list is published in the Security and Compliance Overview. Aldermoor gives at least 30 days' notice before adding a sub-processor, during which the Customer may object on reasonable grounds relating to data protection."),

            ("h1", "7. International transfers"),
            ("p", "Customer Data is stored in the region selected at provisioning. Where support requires access from another country, that access takes place under the UK International Data Transfer Addendum or the EU Standard Contractual Clauses as applicable, and is limited to what is necessary to resolve the matter at hand."),

            ("h1", "8. Assistance to the controller"),
            ("bullets", [
                "Aldermoor assists the Customer in responding to data subject requests, using the Platform's own export and deletion tools where possible.",
                "Aldermoor assists with data protection impact assessments on request.",
                "Aldermoor notifies the Customer of a confirmed personal data breach without undue delay and within 72 hours of confirmation.",
            ]),

            ("h1", "9. Audit"),
            ("p", "Aldermoor makes available its ISO 27001 certificate, SOC 2 Type II report and penetration test summary to demonstrate compliance. Where these do not satisfy a Customer's regulator, an on-site audit may be arranged once per year with 30 days' notice, at the Customer's cost."),

            ("h1", "10. Deletion and return"),
            ("p", "On termination, Customer Data is retrievable for 90 days, deleted from production within 30 days thereafter, and removed from backups within a further 35 days. A certificate of deletion is available on request."),
        ],
    },

    # ----------------------------------------------------------- MARKDOWN --
    {
        "slug": "10-frequently-asked-questions",
        "format": "md",
        "title": "Frequently Asked Questions",
        "subtitle": "Common questions about Relay and about working with Aldermoor",
        "blocks": [
            ("h1", "Getting started"),
            ("h3", "How long does it take to get Relay running?"),
            ("p", "A Standard implementation for around 60 technicians with one integration typically reaches go-live four to five weeks after kick-off. Self-serve customers on Essentials are often running live work within a few days, because there is less to configure."),
            ("h3", "Is there a free trial?"),
            ("p", "Yes. A 14-day trial of the Professional plan, with no payment card required. It includes up to 10 technician licences and 500 work orders. Trial data is kept for 30 days after the trial ends, so nothing is lost if you convert later."),
            ("h3", "Can we import our existing work orders?"),
            ("p", "Yes. Customers, sites, assets and open work orders import from CSV or through the REST API and are included in every implementation package. Closed historical work orders can also be imported, but this is priced separately because the volume varies enormously."),
            ("h3", "Do you offer on-premises deployment?"),
            ("p", "No. Relay is available only as software as a service. Customers with data residency requirements choose an EU, US or UK region when their tenant is provisioned."),

            ("h1", "Pricing and licensing"),
            ("h3", "How is Relay priced?"),
            ("p", "Per technician per month. Essentials is £29, Professional is £54, and Enterprise starts at £95 and is quoted individually. Those prices assume annual billing; monthly billing costs 20% more."),
            ("h3", "Do office users need a licence?"),
            ("p", "No. Dispatchers, planners, supervisors and administrators are unlimited and free on every plan. A licence is only needed for someone who is assigned work in Relay Mobile."),
            ("h3", "What happens if we add technicians mid-year?"),
            ("p", "Additional licences are invoiced pro rata to the end of the current term, so they line up with your renewal date."),
            ("h3", "Can we reduce our licence count mid-term?"),
            ("p", "Reductions take effect at renewal rather than mid-term, because annual fees are invoiced in advance for the committed count. You can reassign a licence from one person to another at any time and as often as you like, which covers most staffing changes."),
            ("h3", "Is there a discount for a longer commitment?"),
            ("p", "Yes — 10% for a two-year term and 15% for three years. Registered charities receive 20% on production of evidence, and operations above 250 technicians are priced individually."),

            ("h1", "Using the product"),
            ("h3", "Does Relay Mobile work without a signal?"),
            ("p", "Yes, and this is a design goal rather than a fallback. A technician can download a day's work in advance, complete every step offline — including photographs, forms and signatures — and the device synchronises when connectivity returns."),
            ("h3", "How does automatic scheduling decide who gets a job?"),
            ("p", "The scheduling engine weighs skills and certifications, working time rules, travel distance, parts availability on the van and any customer time window. Every proposal lists the constraints that produced it, so a planner can see why a particular technician was chosen and override the decision if they disagree."),
            ("h3", "Which devices are supported?"),
            ("p", "iOS 16 or later, Android 11 or later. Relay is regularly tested on Zebra TC series and Samsung Galaxy XCover rugged devices. The web application supports the current and previous major versions of Chrome, Edge, Firefox and Safari."),
            ("h3", "How far back does reporting go?"),
            ("p", "Twelve months of history on Professional, and twenty-four months with the Insights Advanced add-on. Scheduled exports can be sent to email, secure FTP or object storage if you need to retain more than that in your own warehouse."),

            ("h1", "Integrations and data"),
            ("h3", "Which systems does Relay integrate with?"),
            ("p", "Pre-built connectors cover Salesforce, HubSpot, Microsoft Dynamics 365, SAP S/4HANA, Xero, QuickBooks Online, Microsoft Teams, Slack, Twilio, Google Maps Platform and Zapier. Beyond those there is a REST API, outbound webhooks on 34 event types, and a GraphQL API in beta for Enterprise customers."),
            ("h3", "How many integrations can we have?"),
            ("p", "None on Essentials, three on Professional, and unlimited on Enterprise."),
            ("h3", "Can we get our data out?"),
            ("p", "At any time, in JSON or CSV, through the product or the API, without asking us. If you leave, your data stays retrievable for 90 days."),
            ("h3", "Is there an API rate limit?"),
            ("p", "The allowance is 100,000 calls a month on Professional and 500,000 on Enterprise. Additional capacity is £180 a month per 50,000 calls."),

            ("h1", "Support"),
            ("h3", "What are your support hours?"),
            ("p", "Standard support on Essentials is 09:00 to 17:00 GMT on weekdays by email. Priority support on Professional is 07:00 to 19:00 local on weekdays, with live chat. Premier support on Enterprise is 24 hours a day, every day, including telephone."),
            ("h3", "How quickly will someone respond?"),
            ("p", "For a critical issue: one hour on Premier, four business hours on Priority, and one business day on Standard. Lower severities have proportionally longer targets, all published in the Support Handbook."),
            ("h3", "What do I do if a ticket is stuck?"),
            ("p", "Ask for escalation by replying to the ticket — you do not need permission and it will not count against you. If that does not move things, email escalations@aldermoor.example with the ticket reference and a support manager will respond within one business day."),
            ("h3", "Where can I see whether Relay is down?"),
            ("p", "status.aldermoor.example, which needs no login. You can subscribe there for email or webhook notification of incidents."),

            ("h1", "Security and compliance"),
            ("h3", "Are you certified?"),
            ("p", "ISO/IEC 27001:2022, recertified in March 2026, and a SOC 2 Type II report covering Security, Availability and Confidentiality. Cyber Essentials Plus covers the UK operation. Both the certificate and the report are available under a mutual NDA from security@aldermoor.example."),
            ("h3", "Where is our data stored?"),
            ("p", "In the region chosen when your tenant was provisioned — Frankfurt for EU, Northern Virginia for US, or London for UK on Enterprise. The choice is fixed for the life of the tenant."),
            ("h3", "Does Relay track technician location?"),
            ("p", "While a technician is on an active shift, yes — for scheduling, travel measurement and lone-worker safety. It does not record location outside shift hours. Sampling frequency and retention are configurable, and the whole feature can be switched off at tenant level."),
            ("h3", "Can Aldermoor staff see our data?"),
            ("p", "Not by default. No employee has standing access to production customer data. Access requires a named business reason, lasts a maximum of eight hours, and is logged."),
        ],
    },
    {
        "slug": "11-integrations-catalogue",
        "format": "md",
        "title": "Integrations Catalogue",
        "subtitle": "What Relay connects to, and how",
        "blocks": [
            ("h1", "Pre-built connectors"),
            ("p", "Connectors are configured in Relay Connect and require credentials for the system being connected. Each connector runs on a schedule, on a webhook, or both, depending on what the far system supports."),
            ("table", INTEGRATIONS),

            ("h1", "What each connector does"),
            ("h3", "Salesforce and HubSpot"),
            ("p", "Service cases or tickets raised in the CRM create work orders in Relay. When the work order is completed, the outcome, completion time, parts used and any customer signature are written back to the originating record. Account and contact records synchronise into Relay customers and sites."),
            ("h3", "Microsoft Dynamics 365 and SAP S/4HANA"),
            ("p", "Bidirectional synchronisation of customers, sites, assets and work orders, plus outbound posting of completed jobs for billing. Both are Enterprise-only because they require field mapping work that is specific to each customer's configuration."),
            ("h3", "Xero and QuickBooks Online"),
            ("p", "Completed work orders are posted as draft invoices with labour time, parts consumed and any agreed call-out charge. Relay does not pull financial data back; the accounting system remains the record of truth for billing."),
            ("h3", "Microsoft Teams and Slack"),
            ("p", "Outbound notifications for job assignment, job completion, SLA breach warnings and failed synchronisations. Channel routing is configurable per work type or region."),
            ("h3", "Twilio"),
            ("p", "Appointment reminders and \"technician on the way\" messages to the end customer by SMS. Message templates are editable and support per-tenant sender identifiers."),
            ("h3", "Google Maps Platform"),
            ("p", "Travel time estimates for the scheduling engine and map rendering on the schedule board. The customer supplies their own API key so usage is billed to them directly."),
            ("h3", "Zapier"),
            ("p", "Triggers on eleven Relay events and four actions, for customers who want lightweight automation without building against the API."),

            ("h1", "APIs"),
            ("h3", "REST API"),
            ("bullets", [
                "Described by an OpenAPI 3.1 document published at api.aldermoor.example/openapi.json",
                "Authentication by API key or OAuth 2.0 client credentials",
                "Available on Professional and Enterprise",
                "Allowance of 100,000 calls a month on Professional, 500,000 on Enterprise",
                "Rate limited to 20 requests per second per tenant",
            ]),
            ("h3", "GraphQL API"),
            ("bullets", [
                "In beta, Enterprise only",
                "Read-only in the current release; mutations are planned",
                "Not covered by the availability commitment while in beta",
            ]),
            ("h3", "Webhooks"),
            ("bullets", [
                "34 event types across the work order, technician and scheduling domains",
                "Signed with HMAC-SHA256 so the receiver can verify origin",
                "Retried with exponential backoff for up to 24 hours",
                "Delivery log retained for 7 days and visible in the product",
            ]),

            ("h1", "Requesting a new connector"),
            ("p", "Customers can request a connector to a system not on this list. Requests go to their Customer Success Manager or to support@aldermoor.example. Aldermoor builds connectors where there is demand from several customers; otherwise, a bespoke integration can be delivered as a professional services engagement against the REST API."),
        ],
    },
    {
        "slug": "12-contact-and-offices",
        "format": "md",
        "title": "Contact Us",
        "subtitle": "How to reach the right team",
        "blocks": [
            ("h1", "By purpose"),
            ("table", [
                ["I want to…", "Contact", "Response time"],
                ["Ask a sales question or request a demo", "sales@aldermoor.example", "1 business day"],
                ["Raise a support ticket", "support@aldermoor.example", "Per your support tier"],
                ["Escalate an existing ticket", "escalations@aldermoor.example", "1 business day"],
                ["Ask about an invoice or a refund", "billing@aldermoor.example", "2 business days"],
                ["Request security documentation", "security@aldermoor.example", "3 business days"],
                ["Report a vulnerability", "security@aldermoor.example", "1 business day"],
                ["Ask a data protection question", "privacy@aldermoor.example", "5 business days"],
                ["Apply for a job", "careers@aldermoor.example", "10 business days"],
                ["Anything else", "hello@aldermoor.example", "2 business days"],
            ]),

            ("h1", "Offices"),
            ("table", OFFICES),

            ("h1", "Opening hours"),
            ("table", [
                ["Office", "Hours", "Time zone"],
                ["Bristol (HQ)", "08:30 – 17:30, Monday to Friday", "GMT / BST"],
                ["Kraków", "09:00 – 17:00, Monday to Friday", "CET / CEST"],
                ["Austin", "08:00 – 17:00, Monday to Friday", "CST / CDT"],
            ]),
            ("p", "Offices are closed on public holidays in their own country. Premier support for Enterprise customers runs 24 hours a day, every day, including public holidays, and is not affected by office hours."),

            ("h1", "Telephone"),
            ("bullets", [
                "Bristol head office — +44 117 496 0184",
                "Option 1 — sales",
                "Option 2 — Enterprise support (24/7)",
                "Option 3 — billing",
                "Kraków office — +48 12 350 7722",
                "Austin office — +1 512 555 0147",
            ]),

            ("h1", "Online"),
            ("bullets", [
                "Website — www.aldermoor.example",
                "Service status — status.aldermoor.example",
                "Documentation — docs.aldermoor.example",
                "API reference — api.aldermoor.example",
            ]),

            ("h1", "Registered office"),
            ("p", "Aldermoor Technologies Ltd., 14 Whitcombe Yard, Bristol BS1 4TR, United Kingdom. Registered in England and Wales."),
        ],
    },
    {
        "slug": "13-careers",
        "format": "md",
        "title": "Careers at Aldermoor",
        "subtitle": "How we hire, what we offer, and what we are looking for",
        "blocks": [
            ("h1", "Working here"),
            ("p", "Aldermoor employs 180 people across Bristol, Kraków and Austin. Engineering is concentrated in Kraków, product and customer success in Bristol, and sales and support for North America in Austin. Roughly a third of the company works remotely within the countries where we have an entity."),

            ("h1", "Open roles"),
            ("table", [
                ["Role", "Team", "Location", "Type"],
                ["Senior Backend Engineer", "Platform", "Kraków or remote (Poland)", "Permanent"],
                ["Mobile Engineer (Android)", "Relay Mobile", "Kraków or remote (Poland)", "Permanent"],
                ["Implementation Consultant", "Customer Success", "Bristol or remote (UK)", "Permanent"],
                ["Support Engineer", "Support", "Austin", "Permanent"],
                ["Product Designer", "Product", "Bristol or remote (UK)", "Permanent"],
                ["Data Engineer", "Insights", "Kraków or remote (Poland)", "Permanent"],
                ["Account Executive, Utilities", "Sales", "Austin", "Permanent"],
                ["Engineering Intern", "Platform", "Kraków", "12-month placement"],
            ]),

            ("h1", "How we hire"),
            ("p", "Four steps, and we aim to complete them inside three weeks. We tell candidates where they stand after each one, whether or not the news is good."),
            ("bullets", [
                "1. Introductory call — 30 minutes with a recruiter, covering the role, the team and what you are looking for.",
                "2. Technical or craft conversation — 60 minutes with an engineer or a practitioner from the team. For engineering roles this is a discussion of a real problem, not a whiteboard algorithm test.",
                "3. Practical exercise — a take-home task scoped to under three hours, or a paid half-day pairing session if you prefer. You keep whatever you produce.",
                "4. Team conversation — 60 minutes with two or three people you would work with, plus the hiring manager.",
            ]),
            ("p", "We do not ask for unpaid work beyond the scoped exercise, and we do not run panel interviews longer than an hour without a break."),

            ("h1", "Benefits"),
            ("table", [
                ["Benefit", "Detail"],
                ["Annual leave", "28 days plus public holidays, rising to 33 after four years"],
                ["Pension", "Employer contribution of 8% with no matching requirement"],
                ["Private medical", "Available in all three countries, family cover included"],
                ["Parental leave", "26 weeks at full pay for the primary carer, 8 weeks for the secondary carer"],
                ["Learning budget", "£1,500 per person per year, no approval needed under £500"],
                ["Equipment", "Your choice of laptop, refreshed every three years"],
                ["Remote working", "Fully supported within a country where we have an entity"],
                ["Four-day week", "Every other Friday is a company-wide non-working day"],
            ]),

            ("h1", "What we look for"),
            ("bullets", [
                "Curiosity about the customer's actual work. Our users are technicians in plant rooms, not people at desks.",
                "Comfort with ambiguity, and the habit of narrowing it by asking rather than guessing.",
                "Willingness to write things down. Most of our decisions are recorded, and that is deliberate.",
                "Care for the people who will maintain what you build after you have moved on.",
            ]),

            ("h1", "Applying"),
            ("p", "Send a CV and a short note about why the role interests you to careers@aldermoor.example, quoting the role title. We respond to every application within ten business days. We do not use automated CV screening."),
        ],
    },
    {
        "slug": "14-release-notes",
        "format": "md",
        "title": "Relay Release Notes",
        "subtitle": "Platform releases from version 4.2 onwards",
        "blocks": [
            ("p", "Relay is released on a six-week cadence. Releases are applied to all tenants in the same window and do not require customer action unless stated. Enterprise customers receive the release in their sandbox two weeks ahead of production."),

            ("h1", "Relay 4.6 — 3 September 2026"),
            ("h3", "Added"),
            ("bullets", [
                "Capacity forecasting extended from 60 to 90 days ahead in Relay Dispatch.",
                "Lone-worker check-in escalation now supports a second escalation contact.",
                "Webhook delivery log is now visible in the product for the last 7 days.",
            ]),
            ("h3", "Changed"),
            ("bullets", [
                "The scheduling engine now explains rejected candidates as well as the chosen one.",
                "Attachment upload from Relay Mobile compresses images above 4 MB before transmission, cutting sync time on poor connections by roughly 40%.",
            ]),
            ("h3", "Fixed"),
            ("bullets", [
                "Work orders imported through the API with a null time window could be scheduled outside the site's opening hours.",
                "The German translation of the parts consumption screen truncated long part names.",
            ]),

            ("h1", "Relay 4.5 — 23 July 2026"),
            ("h3", "Added"),
            ("bullets", [
                "GraphQL API released in beta for Enterprise customers, read-only.",
                "UK data residency option (London) for Enterprise tenants.",
                "SCIM 2.0 user provisioning alongside existing SAML single sign-on.",
            ]),
            ("h3", "Changed"),
            ("bullets", [
                "Insights history on Professional increased from 6 to 12 months at no additional cost.",
                "TLS 1.1 is no longer accepted from any client.",
            ]),
            ("h3", "Fixed"),
            ("bullets", [
                "Recurring maintenance schedules generated a duplicate work order when the clocks changed.",
            ]),

            ("h1", "Relay 4.4 — 11 June 2026"),
            ("h3", "Added"),
            ("bullets", [
                "Van stock tracking in Relay Mobile, including low-stock warnings.",
                "Zapier connector with 11 triggers and 4 actions.",
            ]),
            ("h3", "Changed"),
            ("bullets", [
                "Schedule board redesigned to render 500 technicians without pagination.",
                "Photo metadata now records device model alongside location and timestamp.",
            ]),
            ("h3", "Fixed"),
            ("bullets", [
                "Offline jobs completed either side of midnight were occasionally attributed to the wrong day.",
                "Salesforce write-back retried indefinitely when the target record had been deleted.",
            ]),

            ("h1", "Relay 4.3 — 30 April 2026"),
            ("h3", "Added"),
            ("bullets", [
                "Custom dashboards in Insights Advanced.",
                "Twilio connector for appointment reminders and \"on the way\" messages.",
            ]),
            ("h3", "Changed"),
            ("bullets", [
                "Minimum supported iOS version raised to 16; Android minimum raised to 11.",
            ]),
            ("h3", "Fixed"),
            ("bullets", [
                "Certificate expiry warnings were sent to the technician rather than the supervisor.",
            ]),

            ("h1", "Relay 4.2 — 19 March 2026"),
            ("h3", "Added"),
            ("bullets", [
                "Explainable scheduling — every automatic assignment now lists the constraints that produced it.",
                "Sandbox environments for Enterprise tenants.",
            ]),
            ("h3", "Changed"),
            ("bullets", [
                "Work order search rewritten; median query time reduced from 1.9 s to 240 ms.",
            ]),
            ("h3", "Fixed"),
            ("bullets", [
                "Bulk reassignment of more than 200 work orders could time out without reporting failure.",
            ]),
        ],
    },
    {
        "slug": "15-glossary",
        "format": "md",
        "title": "Glossary",
        "subtitle": "Terms used across Relay and Aldermoor documentation",
        "blocks": [
            ("table", [
                ["Term", "Meaning"],
                ["Asset", "A piece of equipment at a site that work is carried out on — a meter, a pump, a cabinet."],
                ["Certification", "A time-limited qualification a technician holds. The scheduling engine will not assign work requiring a certification that has expired."],
                ["Dispatcher", "An office user who assigns and monitors work. Does not consume a technician licence."],
                ["First-time fix rate", "The proportion of work orders completed on the first visit, with no follow-up required."],
                ["Hypercare", "The 30 days after go-live, during which response targets are one severity level higher than contracted."],
                ["Job", "The unit of work a technician sees on their device. One work order produces one or more jobs."],
                ["Lone-worker check-in", "A periodic confirmation from a technician working alone. A missed check-in raises an alert to a nominated contact."],
                ["Office user", "Any user who does not receive work on a mobile device. Unlimited and free on every plan."],
                ["RPO", "Recovery point objective — the maximum data loss acceptable in a disaster, measured in time. Aldermoor's is 1 hour."],
                ["RTO", "Recovery time objective — the maximum time to restore service after a disaster. Aldermoor's is 4 hours."],
                ["Schedule adherence", "How closely completed work matched the planned time slot."],
                ["Service credit", "A percentage of a monthly fee credited against a future invoice when an availability commitment is missed."],
                ["Site", "A physical location where work is carried out. A customer may have many sites."],
                ["Skills matrix", "The mapping of technicians to the work types they are competent to perform."],
                ["Technician licence", "The right for one named individual to be assigned work in Relay Mobile. The unit Relay is priced by."],
                ["Tenant", "A single customer's isolated instance of Relay, including its data region."],
                ["Time window", "A period agreed with the end customer within which a job must be carried out."],
                ["Van stock", "Parts held on a technician's vehicle, tracked in Relay Mobile."],
                ["Work order", "The record of work that needs doing, from creation through to completion and billing hand-off."],
                ["Work type", "A category of work with its own checklist, required skills, expected duration and parts."],
            ]),
        ],
    },
]
