# Skill: Production Docker & Observability (production_docker.md)

This skill manages production-ready deployments and monitoring stacks.

## Capabilities
- **Docker Multi-stage Builds**: Optimizing images for size and security.
- **Orchestration**: Managing multi-service stacks (Core, Redis, Prometheus, Grafana) via `docker-compose`.
- **Metrics Export**: Ingesting application metrics (latencies, win-rates, PnL) into Prometheus.
- **Dashboarding**: Visualizing HFT performance in Grafana.
- **Log Management**: Centralized logging and alerting.

## Usage Guidelines
- Use lightweight base images (e.g., `slim` or `alpine` where compatible).
- Explicitly define resource limits to prevent memory leaks from crashing the host.
- Ensure all sensitive data (API keys) are injected via environment variables or secret managers.
