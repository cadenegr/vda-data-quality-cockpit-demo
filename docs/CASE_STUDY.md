# Case study: VDA Data Quality Cockpit

*48-hour hackathon · 42 Berlin × Volkswagen Group Logistics × Databricks · November 2026*

## The challenge

A car plant receives thousands of logistics messages a month from hundreds of suppliers and carriers. The
challenge asked for "an agent and an interactive cockpit that help suppliers and carriers improve the
quality of the VDA data they send".

We got one month of real, anonymised messages for one plant. Neither of us had worked in automotive
logistics before.

## Understanding the data first

Before building anything, we spent the first hours measuring:

- **What breaks the process?** The plant links three messages per truck (booking, delivery note, tracking)
  through reference numbers. Without that link the truck can't use direct gate entry.
- **The headline finding.** The booking number was missing from the large majority of delivery notes. It
  wasn't a few careless suppliers: hundreds of companies made the same systematic mistake, almost certainly
  a setting in their software.
- **Smaller issues** were placeholders such as `UNKNOWN`, `FOLGT` ("follows") or `0` instead of real
  numbers, malformed gate timeslots, plates replaced by hashes, and messages that couldn't be converted at
  all.
- **Data traps** that changed the design: the same company had differently padded codes in each message
  type, anonymised company IDs were inconsistent across message types, and one supplier reused a single
  "booking number" for weeks.

That turned into 29 rules. Each finding records *which message*, *which VDA field*, *who must fix it*
and *how*.

## Key design decisions

**1. Fix the problem for the user, don't only report it.** A score that says "your matching is bad" doesn't
help a supplier. The *detective* searches the supplier's own bookings for the one that fits each delivery
note: same supplier, pick-up on the despatch day or the next, same parts, same amounts. That works like
matching receipts to a bank statement.

**2. Measure trust and show it.** We back-tested the detective on delivery notes where the truck tracking
independently reveals the true booking. "Sure" (green) suggestions were right every time. "Check" (yellow)
ones were usually right, so a human decides. Adding the amounts as a tie-breaker clearly improved yellow
accuracy. Carrier and time of day didn't help, so we left them out.

**3. Never change the original data.** Approvals go into an append-only log. Undo is a new log entry. That's
what a plant would require before letting partners "fix" anything.

**4. Time matters more than totals.** A score is a summary of the past, but the real question is *which truck
will be stopped at the gate tomorrow?* The alarm board replays the month day by day. On any day it knows only
the messages that had arrived by then, and it raises 🔴 when the information is needed today or tomorrow.

**5. One engine, two runtimes.** The rule engine is plain Python. A script generates the Databricks notebook
from it, and a small DuckDB shim runs the same app offline. When the shared workspace hit its daily compute
limit on the morning of the final day, we switched to laptop mode and the live demo still worked.

## Result

We were the only team with a live, end-to-end demo: alarms, scores, one-click fixes, check-before-send and an
LLM assistant that can propose fixes through tool calling.

We didn't place. The jury scored applicability, technology, innovation, data/AI use **and presentation**
equally. Our pitch spent too long on how it works and not enough on why it matters to the people in the room.

## What I'd do differently

- **Write the pitch on day one, not on the last morning.** The story ("trucks stopped at the gate, and the
  supplier doesn't even know") was clear early. Building toward a rehearsed 5-minute story would have shaped
  which features mattered.
- **Pick the UI stack for iteration speed.** Streamlit made the first version fast, but fine-grained UI
  changes (layout, state, interactions) got slow. A TypeScript front end over the same engine would have been
  quicker to polish.
- **Keep the scope smaller.** Five features at 80 % make a weaker demo than three features at 100 %.

## Tech

Python (standard library engine) · Streamlit · DuckDB · pandas · Databricks: Unity Catalog, Delta, Jobs,
SQL Warehouse, AI/BI Genie, Apps, Model Serving (Qwen3, tool calling) · 47 unit tests + headless page smoke
test · built with Claude Code as a pair programmer.
