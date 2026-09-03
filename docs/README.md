# SaleWell Smart Tank Documentation

The workspace-wide maintained-source index is [`../../docs/DOCUMENTATION_INDEX.md`](../../docs/DOCUMENTATION_INDEX.md).

Last refreshed: `2026-08-24`

Municipal-water documentation uses the shared status vocabulary: `Available`,
`No Flow`, `Disabled`, and controller-level `Offline`. Do not claim that a basic
three-wire pulse sensor detects physical disconnection; zero flow and an
unplugged sensor both produce no pulses. Installer documents must also state
that flow and pressure detection are mutually exclusive runtime choices and
simulators must be disabled for hardware tests.

Choose the document that matches your work. Customer instructions use simple language. Technical and production documents remain separate so customers are not exposed to server or security details.

## Customers and installers

- [Customer Installation Guide — English and Hindi (PDF)](SaleWell-Smart-Tank-Customer-Installation-Guide-English-Hindi.pdf): simple steps and visual diagrams for customers.
- [Customer Installation Guide source](CUSTOMER_INSTALLATION_GUIDE_EN_HI.html): print-ready source used to create the PDF.
- [Technical Installation Guide — English and Hindi](INSTALLATION_GUIDE_EN_HI.md): detailed installation, electrical safety, commissioning, and troubleshooting for installers.
- [Customer FAQ](CUSTOMER_FAQ.md): plans, operation, app access, installation, and support questions.
- [Chatbot Knowledge Base](CHATBOT_KNOWLEDGE_BASE.md): approved information used by the public SaleWell assistant.

## Installation and support teams

- [Installer and Support Runbook](INSTALLER_SUPPORT_RUNBOOK.md): device preparation, commissioning, handover, diagnosis, and escalation.
- [Production Readiness Guide](PRODUCTION_READINESS.md): checks required before a customer rollout.

## Developers and hosting administrators

- [Backend README](../README.md): architecture, local development, configuration, routes, tests, and operations.
- [Deployment Guide](../Flask_deployment_README.md): cPanel/Passenger upload, configuration, restart, and deployment checks.

## Document rules

- The cross-platform pump-control contract is [`../config/pump_control.json`](../config/pump_control.json); documentation may explain its values but must not become a competing source of truth.
- cPanel instructions must include the tracked `config/` directory because Passenger loads the pump contract during startup.
- UI path names use `M`, `S`, `R1`, `R2`, and `WIFI_LAN` consistently across Flask and Android.
- Markdown and HTML files are maintained sources. Regenerate derived PDF/DOCX artifacts through their source scripts when their content changes.

- Keep customer instructions short and free of internal credentials.
- Keep prices and plan details in the FAQ and chatbot knowledge base synchronized.
- Put server commands and environment variables only in technical documents.
- Update the customer HTML source and regenerate its PDF whenever the customer installation process or diagrams change.
- Keep technical details in the technical installation guide and installer runbook.
- Never place passwords, API keys, device keys, or customer data in documentation.
