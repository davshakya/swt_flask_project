# SaleWell Smart Tank Documentation

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

- Keep customer instructions short and free of internal credentials.
- Keep prices and plan details in the FAQ and chatbot knowledge base synchronized.
- Put server commands and environment variables only in technical documents.
- Update the customer HTML source and regenerate its PDF whenever the customer installation process or diagrams change.
- Keep technical details in the technical installation guide and installer runbook.
- Never place passwords, API keys, device keys, or customer data in documentation.
