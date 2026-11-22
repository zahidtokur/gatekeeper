import http from "k6/http";
import { Counter } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const statuses = {
  200: new Counter("status_200"),
  500: new Counter("status_500"),
  503: new Counter("status_503"),
};

// failing: upstream returns 500 for 20s. The circuit opens after 5 failures.
// recovery: starts after the 30s open window and hits a healthy endpoint.
export const options = {
  scenarios: {
    failing: {
      executor: "constant-arrival-rate",
      rate: 5,
      timeUnit: "1s",
      duration: "20s",
      preAllocatedVUs: 5,
      exec: "failing",
    },
    recovery: {
      executor: "constant-arrival-rate",
      rate: 5,
      timeUnit: "1s",
      duration: "10s",
      startTime: "35s",
      preAllocatedVUs: 5,
      exec: "recovery",
    },
  },
};

function record(res) {
  const counter = statuses[res.status];
  if (counter) counter.add(1);
}

export function failing() {
  record(http.get(`${BASE_URL}/api/orders/error/?error_code=500`));
}

export function recovery() {
  record(http.get(`${BASE_URL}/api/orders/1/`));
}
