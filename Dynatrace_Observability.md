# Application Observability: Dynatrace Monitoring Guide

This guide defines the observability strategy for the application using Dynatrace. The goal is to move beyond simple health checks to a comprehensive view of application performance, reliability, and user experience.

## 1. Core Performance Indicators (KPIs)

To measure the "Golden Signals" of the application, the following metrics are tracked.

### Availability (Success Rate)
Measures the percentage of successful requests compared to total requests.
- **Metric:** `Success Rate %`
- **Dynatrace Calculation:** `(1 - (Sum of 5xx Errors / Total Requests)) * 100`
- **Target:** > 99.9%
- **Alerting:** Critical alert if success rate drops below 95% for a 5-minute window.

### Throughput (Traffic Volume)
Measures the demand placed on the system.
- **Metric:** `Requests per Minute (RPM)`
- **Dynatrace Calculation:** `Sum of Requests` grouped by minute.
- **Purpose:** Identify traffic spikes, DDoS attempts, or sudden drops in usage.
- **Baseline:** Established via Dynatrace AI-driven baseline (Adaptive Thresholds).

### Response Time (Latency)
Measures the time taken to process requests.
- **Metric:** `Average Response Time` & `P95 Latency`
- **Dynatrace Calculation:** 
    - **Average:** `Total response time / Total requests`
    - **P95:** The response time under which 95% of all requests fall.
- **Target:** Average < 500ms; P95 < 1.5s.
- **Focus:** Identify "long tail" latency affecting specific users.

### Error Rate
Measures the frequency of failures.
- **Metric:** `Error Rate %`
- **Dynatrace Calculation:** `(Sum of 4xx + 5xx errors / Total Requests) * 100`
- **Target:** < 1%
- **Alerting:** Warning alert if error rate exceeds 2% for 10 consecutive minutes.

---

## 2. Dynatrace Implementation Details

### Full-Stack Monitoring
The application is monitored using the **OneAgent** approach:
- **Process Level:** Tracks JVM/Node.js/Python heap usage, thread counts, and CPU saturation.
- **Service Level:** Automatic discovery of dependencies (Database, Third-party APIs, Internal Microservices).
- **Request Level:** PurePath technology provides end-to-end distributed tracing for every single request.

### Custom Metrics (Extension 2.0)
Beyond default metrics, the following custom metrics are pushed via the Dynatrace API/Metric Ingest:
- **Business Logic Failures:** Application-level errors that return 200 OK but contain a "failure" payload.
- **Token Consumption:** Total LLM tokens consumed per session (linked to Bedrock usage).
- **Cold Start Latency:** Tracking Lambda initialization time specifically for cold starts.

---

## 3. Dashboard Layout (Observability View)

The application dashboard is divided into three logical panes:

### Pane A: The "Pulse" (High Level)
- **Status Indicator:** Green/Yellow/Red based on Availability + Error Rate.
- **Current RPM:** Gauge showing real-time throughput vs. 24h average.
- **Active Sessions:** Count of unique users currently interacting with the app.

### Pane B: The "Performance" (Detailed)
- **Latency Heatmap:** Distribution of response times across all endpoints.
- **Top 5 Slowest Endpoints:** Table showing the most latent API routes.
- **Dependency Health:** Map of all external services and their current response times.

### Pane C: The "Errors" (Diagnostic)
- **Error Distribution:** Pie chart of 4xx vs 5xx errors.
- **Most Frequent Exceptions:** List of top stack traces from the logs.
- **Failure Correlation:** Timeline showing errors mapped against deployment events.

---

## 4. Alerting Strategy

| Alert Level | Trigger | Action |
| :--- | :--- | :--- |
| **Critical** | P95 Latency > 5s OR Success Rate < 90% | Immediate PagerDuty / Slack notification to On-Call. |
| **Warning** | Error Rate > 2% OR CPU Saturation > 80% | Ticket created in Jira; Slack notification to Dev team. |
| **Info** | Traffic Spike > 2x Baseline | Notification for capacity planning review. |
