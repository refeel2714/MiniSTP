"""STP-UI: business screens produced from definition files checked against the DB schema.

* `definitions` – screen definition format (YAML) and the validator that checks
  every grid column against the live schema and the transaction's output.
* `transactions` – read transactions by id (조회 트랜잭션 하나 = 조회 서비스 하나).
* `app` – the FastAPI server: screens, transaction API, generated screen specs.
* `static/stpui.js` – the thin client runtime that turns a definition into a screen.
"""
